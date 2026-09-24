import fs from 'node:fs';
import path from 'node:path';

const DEFAULT_DB = process.env.SCHEDULER_DB || path.resolve('data', 'scheduler-events.jsonl');

export function initialState() {
  return {
    seq: 0,
    forecasts: new Map(),
    plans: new Map(),
    revisions: new Map(),
    reports: new Map(),
    auditEvents: []
  };
}

export class EventStore {
  constructor(file = DEFAULT_DB) {
    this.file = path.resolve(file);
    this.state = initialState();
    this.queue = Promise.resolve();
    this._load();
  }

  _load() {
    if (!fs.existsSync(this.file)) return;
    const lines = fs.readFileSync(this.file, 'utf8').split(/\r?\n/).filter(Boolean);
    for (const line of lines) {
      const event = JSON.parse(line);
      this._apply(event);
    }
  }

  reload() {
    this.state = initialState();
    this._load();
  }

  async transact(fn, options = {}) {
    return new Promise((resolve, reject) => {
      this.queue = this.queue.then(async () => {
        const actor = options.actor || 'system';
        const timestamp = options.now ? new Date(options.now).toISOString() : new Date().toISOString();
        const context = {
          state: this.state,
          actor,
          timestamp,
          events: [],
          nextSeq: this.state.seq + 1
        };
        try {
          const result = await fn(context);
          if (context.events.length === 0) {
            resolve(result);
            return;
          }
          const envelopes = [];
          // Validate the complete transaction against a cloned state before durable append.
          const clonedState = cloneState(this.state);
          for (const event of context.events) {
            const envelope = {
              seq: context.nextSeq,
              id: `evt_${String(context.nextSeq).padStart(12, '0')}`,
              timestamp,
              actor,
              ...event
            };
            reduceOne(clonedState, envelope);
            envelopes.push(envelope);
            context.nextSeq += 1;
          }

          fs.mkdirSync(path.dirname(this.file), { recursive: true });
          const serialized = envelopes.map((event) => JSON.stringify(event)).join('\n') + '\n';
          const fd = fs.openSync(this.file, 'a');
          try {
            fs.writeSync(fd, serialized);
            fs.fsyncSync(fd);
          } finally {
            fs.closeSync(fd);
          }
          for (const envelope of envelopes) this._apply(envelope);
          resolve(typeof result === 'function' ? result() : result);
        } catch (error) {
          reject(error);
        }
      });
      // Prevent a rejected transaction from poisoning the serialized queue.
      this.queue = this.queue.catch(() => {});
    });
  }

  appendEvent(tx, type, payload) {
    tx.events.push({ type, payload });
  }

  _apply(event) {
    reduceOne(this.state, event);
  }

  snapshot() {
    return cloneState(this.state);
  }
}

function reduceOne(state, event) {
  state.seq = event.seq;
  const audit = toAudit(event);
  const reducer = reducers[event.type];
  if (!reducer) throw new Error(`Unknown event ${event.type}`);
  reducer(state, event.payload);
  state.auditEvents.push(audit);
}

const reducers = {
  'forecast-registered': (state, forecast) => {
    if (state.forecasts.has(forecast.versionId)) throw new Error(`forecast version ${forecast.versionId} already exists`);
    state.forecasts.set(forecast.versionId, structuredClone(forecast));
  },
  'forecast-published': (state, event) => {
    const forecast = state.forecasts.get(event.versionId);
    if (!forecast) throw new Error(`forecast version ${event.versionId} does not exist`);
    if (forecast.status === 'published') {
      if (forecast.publishedAt !== event.publishedAt) throw new Error('published forecast cannot be republished');
      return;
    }
    forecast.status = 'published';
    forecast.publishedAt = event.publishedAt;
  },
  'plan-created': (state, plan) => {
    if (state.plans.has(plan.id)) throw new Error(`plan ${plan.id} already exists`);
    state.plans.set(plan.id, structuredClone(plan));
  },
  'impact-report-generated': (state, report) => {
    if (state.reports.has(report.id)) throw new Error(`impact report ${report.id} already exists`);
    state.reports.set(report.id, structuredClone(report));
  },
  'revision-proposed': (state, revision) => {
    if (state.revisions.has(revision.id)) throw new Error(`revision ${revision.id} already exists`);
    state.revisions.set(revision.id, structuredClone(revision));
    if (!state.plans.has(revision.candidatePlanId)) {
      state.plans.set(revision.candidatePlanId, structuredClone(revision.candidatePlan));
    }
  },
  'revision-adopted': (state, event) => {
    const revision = state.revisions.get(event.revisionId);
    if (!revision) throw new Error(`revision ${event.revisionId} does not exist`);
    if (revision.status !== 'proposed') throw new Error('only proposed revisions can be adopted');
    const candidatePlan = state.plans.get(revision.candidatePlanId);
    const oldPlan = state.plans.get(revision.planId);
    if (!candidatePlan || !oldPlan) throw new Error('revision plans are missing');
    revision.status = 'adopted';
    revision.decidedAt = event.decidedAt;
    revision.decidedBy = event.decidedBy;
    candidatePlan.status = 'active';
    oldPlan.status = 'superseded';
    oldPlan.supersededBy = revision.id;
    const report = state.reports.get(revision.impactReportId);
    if (report) {
      report.status = 'adopted';
      report.adoptedBy = event.decidedBy;
      report.adoptedAt = event.decidedAt;
      report.revisionId = revision.id;
    }
  },
  'revision-rejected': (state, event) => {
    const revision = state.revisions.get(event.revisionId);
    if (!revision) throw new Error(`revision ${event.revisionId} does not exist`);
    if (revision.status !== 'proposed') throw new Error('only proposed revisions can be rejected');
    revision.status = 'rejected';
    revision.decidedAt = event.decidedAt;
    revision.decidedBy = event.decidedBy;
    revision.reviewNote = event.reviewNote;
    const candidate = state.plans.get(revision.candidatePlanId);
    if (candidate) candidate.status = 'rejected';
    const report = state.reports.get(reportIdOr(revision));
    if (report) {
      report.status = 'rejected';
      report.rejectedBy = event.decidedBy;
      report.rejectedAt = event.decidedAt;
      report.revisionId = revision.id;
    }
  }
};

function reportIdOr(revision) {
  return revision.impactReportId;
}

function toAudit(event) {
  const p = event.payload;
  let entityType;
  let entityId;
  if (event.type.startsWith('forecast')) {
    entityType = 'forecast_version';
    entityId = p.versionId;
  } else if (event.type === 'plan-created') {
    entityType = 'plan';
    entityId = p.id;
  } else if (event.type === 'impact-report-generated') {
    entityType = 'impact_report';
    entityId = p.id;
  } else {
    entityType = 'plan_revision';
    entityId = p.revisionId;
  }
  return {
    id: event.id,
    seq: event.seq,
    timestamp: event.timestamp,
    actor: event.actor,
    action: event.type,
    entityType,
    entityId,
    details: structuredClone(p)
  };
}

function cloneState(state) {
  return {
    seq: state.seq,
    forecasts: new Map(Array.from(state.forecasts, ([k, v]) => [k, structuredClone(v)])),
    plans: new Map(Array.from(state.plans, ([k, v]) => [k, structuredClone(v)])),
    revisions: new Map(Array.from(state.revisions, ([k, v]) => [k, structuredClone(v)])),
    reports: new Map(Array.from(state.reports, ([k, v]) => [k, structuredClone(v)])),
    auditEvents: structuredClone(state.auditEvents)
  };
}
