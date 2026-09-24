import crypto from 'node:crypto';
import {
  DomainError,
  buildImpactReport,
  hashCanonical,
  normalizeForecastInput,
  normalizeTask,
  solve
} from './domain.js';
import { overlap } from './time.js';

export function createServices(store) {
  function requireForecast(state, versionId) {
    const forecast = state.forecasts.get(versionId);
    if (!forecast) throw new DomainError(404, 'FORECAST_NOT_FOUND', `forecast version ${versionId} was not found`);
    return forecast;
  }

  function requirePublishedForecast(state, versionId) {
    const forecast = requireForecast(state, versionId);
    if (forecast.status !== 'published') {
      throw new DomainError(409, 'FORECAST_NOT_PUBLISHED', `forecast version ${versionId} is not a complete published version`);
    }
    return forecast;
  }

  function requirePlan(state, planId) {
    const plan = state.plans.get(planId);
    if (!plan) throw new DomainError(404, 'PLAN_NOT_FOUND', `plan ${planId} was not found`);
    return plan;
  }

  function ensureSourceRevisionUnused(state, forecast) {
    for (const existing of state.forecasts.values()) {
      if (existing.source === forecast.source &&
          existing.revision === forecast.revision &&
          existing.versionId !== forecast.versionId) {
        throw new DomainError(409, 'SOURCE_REVISION_EXISTS',
          `${forecast.source} revision ${forecast.revision} already exists as ${existing.versionId}`);
      }
    }
  }

  function isOutOfOrder(state, candidate) {
    const highestRevision = Array.from(state.forecasts.values())
      .filter((f) => f.source === candidate.source &&
        f.versionId !== candidate.versionId &&
        f.status === 'published')
      .reduce((max, f) => Math.max(max, f.revision), 0);
    return candidate.revision < highestRevision;
  }

  function registerForecast(input, options = {}) {
    return store.transact((tx) => {
      const forecast = normalizeForecastInput(input);
      const existing = tx.state.forecasts.get(forecast.versionId);
      if (existing) {
        if (existing.contentHash !== forecast.contentHash) {
          throw new DomainError(409, 'FORECAST_VERSION_CONFLICT',
            `versionId ${forecast.versionId} already exists with different content`);
        }
        if (!options.publish || existing.status === 'published') return structuredClone(existing);
      } else {
        ensureSourceRevisionUnused(tx.state, forecast);
        forecast.createdAt = tx.timestamp;
        store.appendEvent(tx, 'forecast-registered', forecast);
      }
      if (options.publish) {
        const outOfOrder = isOutOfOrder(tx.state, forecast);
        store.appendEvent(tx, 'forecast-published', {
          versionId: forecast.versionId,
          publishedAt: tx.timestamp
        });
        forecast.status = 'published';
        forecast.publishedAt = tx.timestamp;
        if (!outOfOrder) appendAutomaticImpacts(tx, forecast);
      }
      return () => structuredClone(tx.state.forecasts.get(forecast.versionId));
    }, options);
  }

  function publishForecast(versionId, options = {}) {
    return store.transact((tx) => {
      const forecast = requireForecast(tx.state, versionId);
      if (forecast.status === 'published') return structuredClone(forecast);
      const outOfOrder = isOutOfOrder(tx.state, forecast);
      store.appendEvent(tx, 'forecast-published', {
        versionId,
        publishedAt: tx.timestamp
      });
      forecast.status = 'published';
      forecast.publishedAt = tx.timestamp;
      if (!outOfOrder) appendAutomaticImpacts(tx, forecast);
      return () => structuredClone(tx.state.forecasts.get(versionId));
    }, options);
  }

  function appendAutomaticImpacts(tx, candidate) {
    for (const plan of Array.from(tx.state.plans.values()).sort(comparePlans)) {
      if (plan.status !== 'active') continue;
      const baseline = tx.state.forecasts.get(plan.forecastVersionId);
      if (!baseline || baseline.source !== candidate.source) continue;
      // Old revisions arriving after a newer complete publication are out of order;
      // they may be retained for replay but never generate automatic plan impacts.
      if (isOutOfOrder(tx.state, candidate)) continue;
      if (!overlap(plan.intervalStart, plan.intervalEnd, candidate.effectiveStart, candidate.effectiveEnd)) continue;
      const report = buildImpactReport({
        plan,
        baselineForecast: baseline,
        candidateForecast: candidate,
        tasks: plan.inputSnapshot.tasks,
        generatedAt: tx.timestamp,
        source: 'forecast-publish',
        auto: true
      });
      if (report.impacts.length === 0) continue;
      if (reportExists(tx.state, report.id)) continue;
      store.appendEvent(tx, 'impact-report-generated', report);
    }
  }

  function createPlan(input, options = {}) {
    return store.transact((tx) => {
      const forecast = requirePublishedForecast(tx.state, requireString(input?.forecastVersionId, 'forecastVersionId'));
      if (!Array.isArray(input?.tasks) || input.tasks.length === 0) {
        throw new DomainError(400, 'INVALID_TASK', 'tasks must be a non-empty array');
      }
      const taskIds = new Set();
      const tasks = input.tasks.map((task, index) => {
        const normalized = normalizeTask(task, index, forecast.effectiveStart, forecast.effectiveEnd);
        if (taskIds.has(normalized.taskId)) {
          throw new DomainError(400, 'INVALID_TASK', `duplicate taskId ${normalized.taskId}`);
        }
        taskIds.add(normalized.taskId);
        return normalized;
      });
      tasks.sort((a, b) => a.vesselId.localeCompare(b.vesselId) || a.taskId.localeCompare(b.taskId));

      const solution = solve(forecast, tasks);
      if (!solution.feasible) {
        throw new DomainError(422, 'PLAN_INFEASIBLE', 'the selected forecast version cannot solve all tasks', {
          forecastVersionId: forecast.versionId,
          failures: solution.failures
        });
      }

      const now = tx.timestamp;
      const intervalStart = tasks.map((t) => t.earliestStart).sort()[0];
      const intervalEnd = tasks.map((t) => t.latestFinish).sort().at(-1);
      const plan = {
        id: `plan_${crypto.randomUUID().replaceAll('-', '').slice(0, 24)}`,
        name: typeof input.name === 'string' && input.name.trim() ? input.name : null,
        status: 'active',
        forecastVersionId: forecast.versionId,
        forecastRevision: forecast.revision,
        forecastSource: forecast.source,
        effectiveStart: forecast.effectiveStart,
        effectiveEnd: forecast.effectiveEnd,
        intervalStart,
        intervalEnd,
        assignments: solution.assignments,
        locked: true,
        lockedAt: now,
        createdAt: now,
        supersededBy: null,
        inputSnapshot: {
          forecast: forecastSnapshot(forecast),
          tasks: structuredClone(tasks),
          solver: {
            algorithm: 'earliest-feasible-tide-window',
            version: 1,
            vesselOverlap: 'forbidden',
            deterministic: true
          },
          contentHash: null
        }
      };
      plan.inputSnapshot.contentHash = hashCanonical(plan.inputSnapshot);
      store.appendEvent(tx, 'plan-created', plan);
      return () => structuredClone(tx.state.plans.get(plan.id));
    }, options);
  }

  function generateImpact(planId, candidateVersionId, options = {}) {
    return store.transact((tx) => {
      const plan = requirePlan(tx.state, planId);
      if (plan.status === 'proposed') throw new DomainError(409, 'PLAN_NOT_LOCKED', 'proposed candidate plans cannot be reviewed directly');
      const candidate = requirePublishedForecast(tx.state, candidateVersionId);
      const baseline = requireForecast(tx.state, plan.forecastVersionId);
      if (candidate.versionId === baseline.versionId) {
        throw new DomainError(409, 'SAME_FORECAST_VERSION', 'the candidate version is already the version bound to this plan');
      }
      if (!overlap(plan.intervalStart, plan.intervalEnd, candidate.effectiveStart, candidate.effectiveEnd)) {
        throw new DomainError(422, 'OUTSIDE_EFFECTIVE_INTERVAL',
          'candidate forecast effective interval does not overlap this plan');
      }
      const source = options.source || 'manual-review';
      const auto = Boolean(options.auto);
      const report = buildImpactReport({
        plan,
        baselineForecast: baseline,
        candidateForecast: candidate,
        tasks: plan.inputSnapshot.tasks,
        generatedAt: tx.timestamp,
        source,
        auto
      });
      const existing = tx.state.reports.get(report.id) || findReport(tx.state, plan.id, candidate.versionId);
      if (existing) return { report: structuredClone(existing), created: false };
      store.appendEvent(tx, 'impact-report-generated', report);
      return () => ({ report: structuredClone(tx.state.reports.get(report.id)), created: true });
    }, options);
  }

  function proposeRevision(planId, candidateVersionId, impactReportId, options = {}) {
    return store.transact((tx) => {
      const plan = requirePlan(tx.state, planId);
      if (plan.status !== 'active') throw new DomainError(409, 'PLAN_NOT_ACTIVE', 'only an active locked plan can start an explicit revision');
      const candidate = requirePublishedForecast(tx.state, candidateVersionId);
      const report = tx.state.reports.get(impactReportId);
      if (!report) throw new DomainError(404, 'IMPACT_REPORT_NOT_FOUND', `impact report ${impactReportId} was not found`);
      if (report.planId !== plan.id || report.candidateVersionId !== candidate.versionId) {
        throw new DomainError(409, 'IMPACT_REPORT_MISMATCH', 'impact report does not belong to this plan and candidate complete version');
      }
      if (report.status !== 'open') throw new DomainError(409, 'IMPACT_REPORT_CLOSED', `impact report is already ${report.status}`);
      if (!report.feasible) throw new DomainError(422, 'REVISION_INFEASIBLE', 'candidate forecast is infeasible; it cannot be adopted');

      for (const revision of tx.state.revisions.values()) {
        if (revision.planId === plan.id && revision.candidateVersionId === candidate.versionId &&
            ['proposed', 'adopted'].includes(revision.status)) {
          throw new DomainError(409, 'REVISION_EXISTS', `revision ${revision.id} already exists for this candidate`);
        }
      }

      const tasks = plan.inputSnapshot.tasks;
      const solution = solve(candidate, tasks);
      if (!solution.feasible) throw new DomainError(422, 'REVISION_INFEASIBLE', 'candidate forecast solve failed');

      const revisionNumber = 1 + Array.from(tx.state.revisions.values())
        .filter((r) => r.planId === plan.id).length;
      const now = tx.timestamp;
      const intervalStart = tasks.map((t) => t.earliestStart).sort()[0];
      const intervalEnd = tasks.map((t) => t.latestFinish).sort().at(-1);
      const candidatePlan = {
        id: `${plan.id}_r${revisionNumber}`,
        name: plan.name,
        status: 'proposed',
        forecastVersionId: candidate.versionId,
        forecastRevision: candidate.revision,
        forecastSource: candidate.source,
        effectiveStart: candidate.effectiveStart,
        effectiveEnd: candidate.effectiveEnd,
        intervalStart,
        intervalEnd,
        assignments: solution.assignments,
        locked: true,
        lockedAt: now,
        createdAt: now,
        supersededBy: null,
        inputSnapshot: {
          forecast: forecastSnapshot(candidate),
          tasks: structuredClone(tasks),
          solver: { ...plan.inputSnapshot.solver },
          contentHash: null
        }
      };
      candidatePlan.inputSnapshot.contentHash = hashCanonical(candidatePlan.inputSnapshot);
      const revision = {
        id: `rev_${plan.id.replace(/^plan_/, '')}_${revisionNumber}`,
        revisionNumber,
        planId: plan.id,
        candidatePlanId: candidatePlan.id,
        candidatePlan,
        baselineVersionId: plan.forecastVersionId,
        candidateVersionId: candidate.versionId,
        impactReportId: report.id,
        status: 'proposed',
        proposedAt: now,
        proposedBy: tx.actor,
        decidedAt: null,
        decidedBy: null,
        reviewNote: null
      };
      store.appendEvent(tx, 'revision-proposed', revision);
      return () => structuredClone(tx.state.revisions.get(revision.id));
    }, options);
  }

  function adoptRevision(revisionId, options = {}) {
    return store.transact((tx) => {
      const revision = tx.state.revisions.get(revisionId);
      if (!revision) throw new DomainError(404, 'REVISION_NOT_FOUND', `revision ${revisionId} was not found`);
      if (revision.status !== 'proposed') {
        throw new DomainError(409, 'REVISION_NOT_OPEN', `revision is ${revision.status}`);
      }
      store.appendEvent(tx, 'revision-adopted', {
        revisionId,
        decidedAt: tx.timestamp,
        decidedBy: tx.actor
      });
      return () => structuredClone(tx.state.revisions.get(revisionId));
    }, options);
  }

  function rejectRevision(revisionId, reviewNote = '', options = {}) {
    return store.transact((tx) => {
      const revision = tx.state.revisions.get(revisionId);
      if (!revision) throw new DomainError(404, 'REVISION_NOT_FOUND', `revision ${revisionId} was not found`);
      if (revision.status !== 'proposed') {
        throw new DomainError(409, 'REVISION_NOT_OPEN', `revision is ${revision.status}`);
      }
      store.appendEvent(tx, 'revision-rejected', {
        revisionId,
        decidedAt: tx.timestamp,
        decidedBy: tx.actor,
        reviewNote
      });
      return () => structuredClone(tx.state.revisions.get(revisionId));
    }, options);
  }

  function listForecasts(query = {}) {
    const values = Array.from(store.state.forecasts.values())
      .sort((a, b) => a.source.localeCompare(b.source) || a.revision - b.revision || a.versionId.localeCompare(b.versionId));
    return structuredClone(filterByStatus(values, query.status));
  }

  function listPlans(query = {}) {
    const values = Array.from(store.state.plans.values())
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id));
    return structuredClone(filterByStatus(values, query.status));
  }

  function listReports(query = {}) {
    let values = Array.from(store.state.reports.values())
      .sort((a, b) => a.generatedAt.localeCompare(b.generatedAt) || a.id.localeCompare(b.id));
    if (query.planId) values = values.filter((r) => r.planId === query.planId);
    if (query.candidateVersionId) values = values.filter((r) => r.candidateVersionId === query.candidateVersionId);
    if (query.status) values = values.filter((r) => r.status === query.status);
    return structuredClone(values);
  }

  function listRevisions(query = {}) {
    let values = Array.from(store.state.revisions.values())
      .sort((a, b) => a.planId.localeCompare(b.planId) || a.revisionNumber - b.revisionNumber);
    if (query.planId) values = values.filter((r) => r.planId === query.planId);
    if (query.status) values = values.filter((r) => r.status === query.status);
    return structuredClone(values);
  }

  function listAudit(query = {}) {
    let values = [...store.state.auditEvents];
    if (query.entityType) values = values.filter((e) => e.entityType === query.entityType);
    if (query.entityId) values = values.filter((e) => e.entityId === query.entityId);
    if (query.action) values = values.filter((e) => e.action === query.action);
    return structuredClone(values);
  }

  function replaySnapshot() {
    store.reload();
    const replayChecks = [];
    for (const plan of store.state.plans.values()) {
      const snapshot = plan.inputSnapshot;
      const expectedHash = hashCanonical({ ...snapshot, contentHash: null });
      if (expectedHash !== snapshot.contentHash) {
        throw new DomainError(500, 'SNAPSHOT_CORRUPTED', `snapshot hash mismatch for plan ${plan.id}`);
      }
      const replaySolution = solve(
        {
          versionId: snapshot.forecast.versionId,
          effectiveStart: snapshot.forecast.effectiveStart,
          effectiveEnd: snapshot.forecast.effectiveEnd,
          windows: snapshot.forecast.windows
        },
        snapshot.tasks
      );
      if (JSON.stringify(replaySolution.assignments) !== JSON.stringify(plan.assignments)) {
        throw new DomainError(500, 'SNAPSHOT_REPLAY_MISMATCH', `deterministic replay mismatch for plan ${plan.id}`);
      }
      replayChecks.push({ planId: plan.id, forecastVersionId: plan.forecastVersionId, verified: true });
    }
    return {
      forecasts: listForecasts().length,
      plans: listPlans().length,
      revisions: listRevisions().length,
      impactReports: listReports().length,
      auditEvents: store.state.auditEvents.length,
      lastSeq: store.state.seq,
      replayChecks
    };
  }

  return {
    registerForecast,
    publishForecast,
    createPlan,
    generateImpact,
    proposeRevision,
    adoptRevision,
    rejectRevision,
    listForecasts,
    listPlans,
    listReports,
    listRevisions,
    listAudit,
    replaySnapshot,
    getForecast: (id) => structuredClone(requireForecast(store.state, id)),
    getPlan: (id) => structuredClone(requirePlan(store.state, id)),
    getRevision: (id) => {
      const value = store.state.revisions.get(id);
      if (!value) throw new DomainError(404, 'REVISION_NOT_FOUND', `revision ${id} was not found`);
      return structuredClone(value);
    },
    getReport: (id) => {
      const value = store.state.reports.get(id);
      if (!value) throw new DomainError(404, 'IMPACT_REPORT_NOT_FOUND', `impact report ${id} was not found`);
      return structuredClone(value);
    }
  };
}

function forecastSnapshot(forecast) {
  return {
    versionId: forecast.versionId,
    source: forecast.source,
    revision: forecast.revision,
    status: forecast.status,
    effectiveStart: forecast.effectiveStart,
    effectiveEnd: forecast.effectiveEnd,
    windows: structuredClone(forecast.windows),
    publishedAt: forecast.publishedAt,
    contentHash: forecast.contentHash
  };
}

function reportExists(state, id) {
  return state.reports.has(id) ||
    Array.from(state.reports.values()).some((r) => r.id === id);
}

function findReport(state, planId, candidateVersionId) {
  return Array.from(state.reports.values())
    .filter((r) => r.planId === planId && r.candidateVersionId === candidateVersionId)
    .sort((a, b) => b.generatedAt.localeCompare(a.generatedAt))[0];
}

function comparePlans(a, b) {
  return a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id);
}

function filterByStatus(values, status) {
  return status ? values.filter((value) => value.status === status) : values;
}

function requireString(value, field) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new DomainError(400, 'INVALID_REQUEST', `${field} is required`);
  }
  return value;
}
