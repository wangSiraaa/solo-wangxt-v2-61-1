import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { EventStore } from '../src/store.js';
import { createServices } from '../src/services.js';
import { createApp } from '../src/app.js';
import { solve } from '../src/domain.js';

const EFFECTIVE_START = '2026-01-01T00:00:00.000Z';
const EFFECTIVE_END = '2026-01-03T00:00:00.000Z';

function tempDb() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tide-scheduler-'));
  return path.join(dir, 'events.jsonl');
}

function window(vesselId, start, end, index = 0) {
  return {
    windowId: `${vesselId}-w${index + 1}`,
    vesselId,
    start,
    end
  };
}

const tasks = [
  {
    taskId: 'alpha-mooring',
    vesselId: 'alpha',
    durationMinutes: 120,
    earliestStart: '2026-01-01T21:00:00.000Z',
    latestFinish: '2026-01-02T04:00:00.000Z'
  },
  {
    taskId: 'beta-cargo',
    vesselId: 'beta',
    durationMinutes: 60,
    earliestStart: '2026-01-01T08:00:00.000Z',
    latestFinish: '2026-01-01T10:00:00.000Z'
  }
];

function forecast(versionId, revision, alphaWindow, betaWindow = window('beta', '2026-01-01T06:00:00.000Z', '2026-01-01T10:00:00.000Z')) {
  return {
    versionId,
    source: 'harbor-master',
    revision,
    effectiveStart: EFFECTIVE_START,
    effectiveEnd: EFFECTIVE_END,
    windows: [alphaWindow, betaWindow]
  };
}

const v1 = forecast(
  'tide-v1',
  1,
  window('alpha', '2026-01-01T22:00:00.000Z', '2026-01-02T02:00:00.000Z')
);

// Revision 3 is byte-for-byte identical tide data for these vessels/windows.
const v3Unchanged = forecast(
  'tide-v3-unchanged',
  3,
  window('alpha', '2026-01-01T22:00:00.000Z', '2026-01-02T02:00:00.000Z')
);

const v4Narrowed = forecast(
  'tide-v4-narrowed',
  4,
  window('alpha', '2026-01-01T23:00:00.000Z', '2026-01-02T01:00:00.000Z')
);

const v5TooNarrow = forecast(
  'tide-v5-too-narrow',
  5,
  window('alpha', '2026-01-01T23:30:00.000Z', '2026-01-02T00:30:00.000Z')
);

// Delayed lower revision after revision 4 has already been seen.
const lateV2Moved = forecast(
  'tide-v2-late',
  2,
  window('alpha', '2026-01-01T23:15:00.000Z', '2026-01-02T01:15:00.000Z')
);

function servicesFor(db) {
  return createServices(new EventStore(db));
}

test('cross-midnight tide window is solved at its deterministic earliest slot', async () => {
  const store = new EventStore(tempDb());
  const services = createServices(store);
  await services.registerForecast(v1, { publish: true });
  const plan = await services.createPlan({ forecastVersionId: v1.versionId, tasks });

  assert.equal(plan.locked, true);
  assert.equal(plan.forecastVersionId, 'tide-v1');
  assert.equal(plan.forecastRevision, 1);
  const alpha = plan.assignments.find((a) => a.vesselId === 'alpha');
  assert.deepEqual(
    { start: alpha.start, end: alpha.end, windowId: alpha.windowId },
    { start: '2026-01-01T22:00:00.000Z', end: '2026-01-02T00:00:00.000Z', windowId: 'alpha-w1' }
  );
  assert.equal(plan.inputSnapshot.forecast.versionId, 'tide-v1');
  assert.match(plan.inputSnapshot.contentHash, /^[a-f0-9]{64}$/);
});

test('irrelevant revision leaves locked plan untouched; later narrowing is only reported', async () => {
  const db = tempDb();
  const services = servicesFor(db);
  await services.registerForecast(v1, { publish: true });
  const original = await services.createPlan({ name: 'night schedule', forecastVersionId: v1.versionId, tasks });

  await services.registerForecast(v3Unchanged, { publish: true });
  assert.equal(services.listReports({ planId: original.id }).length, 0);
  let current = services.getPlan(original.id);
  assert.equal(current.status, 'active');
  assert.equal(current.forecastVersionId, 'tide-v1');
  assert.deepEqual(current.assignments, original.assignments);

  await services.registerForecast(v4Narrowed, { publish: true });
  const reports = services.listReports({ planId: original.id });
  assert.equal(reports.length, 1);
  const report = reports[0];
  assert.equal(report.candidateVersionId, 'tide-v4-narrowed');
  assert.deepEqual(report.affectedVesselIds, ['alpha']);
  assert.equal(report.impacts.length, 1);
  const impact = report.impacts[0];
  assert.equal(impact.vesselId, 'alpha');
  assert.equal(impact.taskId, 'alpha-mooring');
  assert.equal(impact.impact, 'window_moved');
  assert.equal(impact.oldAssignment.start, '2026-01-01T22:00:00.000Z');
  assert.equal(impact.newAssignment.start, '2026-01-01T23:00:00.000Z');
  assert.deepEqual(impact.oldFeasibleWindows, [{
    windowId: 'alpha-w1',
    vesselId: 'alpha',
    start: '2026-01-01T22:00:00.000Z',
    end: '2026-01-02T02:00:00.000Z'
  }]);
  assert.deepEqual(impact.newWindows, [{
    windowId: 'alpha-w1',
    vesselId: 'alpha',
    start: '2026-01-01T23:00:00.000Z',
    end: '2026-01-02T01:00:00.000Z',
    feasible: true
  }]);

  // Publishing the same complete version is idempotent and creates no second report.
  await services.publishForecast('tide-v4-narrowed');
  const manual = await services.generateImpact(original.id, 'tide-v4-narrowed');
  assert.equal(manual.created, false);
  assert.equal(services.listReports({ planId: original.id }).length, 1);

  current = services.getPlan(original.id);
  assert.equal(current.status, 'active');
  assert.equal(current.forecastVersionId, 'tide-v1');
  assert.deepEqual(current.assignments, original.assignments);
});

test('out-of-order older publication never generates an automatic duplicate impact', async () => {
  const db = tempDb();
  const services = servicesFor(db);
  await services.registerForecast(v1, { publish: true });
  const plan = await services.createPlan({ forecastVersionId: v1.versionId, tasks });
  await services.registerForecast(v4Narrowed, { publish: true });
  assert.equal(services.listReports({ planId: plan.id }).length, 1);

  await services.registerForecast(lateV2Moved, { publish: true });
  const automaticReports = services.listReports().filter((r) => r.source === 'forecast-publish');
  assert.equal(automaticReports.length, 1);
  assert.equal(automaticReports[0].candidateVersionId, 'tide-v4-narrowed');

  // Explicit review remains available, but does not replace the automatic report.
  const explicit = await services.generateImpact(plan.id, 'tide-v2-late');
  assert.equal(explicit.created, true);
  assert.equal(explicit.report.source, 'manual-review');
  assert.equal(services.listReports({ planId: plan.id }).length, 2);
});

test('infeasible narrowed window reports old window, new window, and reason without adopting it', async () => {
  const db = tempDb();
  const services = servicesFor(db);
  await services.registerForecast(v4Narrowed, { publish: true });
  const plan = await services.createPlan({ forecastVersionId: v4Narrowed.versionId, tasks });
  await services.registerForecast(v5TooNarrow, { publish: true });

  const report = services.listReports({ planId: plan.id })[0];
  assert.equal(report.feasible, false);
  assert.equal(report.impacts[0].impact, 'infeasible');
  assert.equal(report.impacts[0].reason, 'WINDOW_TOO_SHORT_OR_MISSING');
  assert.match(report.impacts[0].reasonDetail, /narrowed tide window/);
  assert.equal(report.impacts[0].oldAssignment.windowId, 'alpha-w1');
  assert.equal(report.impacts[0].newAssignment, null);
  assert.deepEqual(report.impacts[0].newWindows, [{
    windowId: 'alpha-w1',
    vesselId: 'alpha',
    start: '2026-01-01T23:30:00.000Z',
    end: '2026-01-02T00:30:00.000Z',
    feasible: false
  }]);

  await assert.rejects(
    () => services.proposeRevision(plan.id, 'tide-v5-too-narrow', report.id),
    (error) => error.status === 422 && error.code === 'REVISION_INFEASIBLE'
  );
  assert.equal(services.getPlan(plan.id).forecastVersionId, 'tide-v4-narrowed');
});

test('explicit revision review adopts a feasible candidate and preserves historical snapshots', async () => {
  const db = tempDb();
  const services = servicesFor(db);
  await services.registerForecast(v1, { publish: true });
  const oldPlan = await services.createPlan({ forecastVersionId: v1.versionId, tasks });
  await services.registerForecast(v4Narrowed, { publish: true });
  const report = services.listReports({ planId: oldPlan.id })[0];

  const revision = await services.proposeRevision(oldPlan.id, 'tide-v4-narrowed', report.id);
  assert.equal(revision.status, 'proposed');
  assert.equal(revision.candidatePlan.forecastVersionId, 'tide-v4-narrowed');
  assert.equal(services.getPlan(oldPlan.id).status, 'active');
  assert.equal(services.getPlan(revision.candidatePlanId).status, 'proposed');

  await services.adoptRevision(revision.id);
  const adoptedOld = services.getPlan(oldPlan.id);
  const adoptedNew = services.getPlan(revision.candidatePlanId);
  assert.equal(adoptedOld.status, 'superseded');
  assert.equal(adoptedOld.supersededBy, revision.id);
  assert.deepEqual(adoptedOld.assignments, oldPlan.assignments);
  assert.equal(adoptedNew.status, 'active');
  assert.equal(adoptedNew.forecastVersionId, 'tide-v4-narrowed');
  assert.equal(adoptedNew.assignments.find((a) => a.vesselId === 'alpha').start,
    '2026-01-01T23:00:00.000Z');
  assert.equal(services.getReport(report.id).status, 'adopted');
});

test('plans never bind drafts during concurrent publication and revision operations', async () => {
  const db = tempDb();
  const services = servicesFor(db);
  await services.registerForecast(v1, { publish: true });
  const plan = await services.createPlan({ forecastVersionId: v1.versionId, tasks });

  const draft = await services.registerForecast(v4Narrowed);
  assert.equal(draft.status, 'draft');
  const draftReportAttempt = await services.generateImpact(plan.id, 'tide-v4-narrowed')
    .then(() => assert.fail('draft report should fail'))
    .catch((error) => {
      assert.equal(error.code, 'FORECAST_NOT_PUBLISHED');
    });
  assert.equal(draftReportAttempt, undefined);

  const outcomes = await Promise.allSettled([
    services.proposeRevision(plan.id, 'tide-v4-narrowed', 'missing-report'),
    services.publishForecast('tide-v4-narrowed')
  ]);
  assert.equal(outcomes[0].status, 'rejected');
  assert.ok(
    ['FORECAST_NOT_PUBLISHED', 'IMPACT_REPORT_NOT_FOUND'].includes(outcomes[0].reason.code),
    `unexpected rejection code ${outcomes[0].reason.code}`
  );
  assert.equal(outcomes[1].value.status, 'published');
  assert.equal(services.listRevisions().length, 0);

  // A plan-creation/publication race has exactly one serialized outcome: the plan either
  // waits for the complete version or is rejected; it is never persisted against a draft.
  await services.registerForecast(v3Unchanged);
  const race = await Promise.allSettled([
    services.createPlan({ forecastVersionId: v3Unchanged.versionId, tasks }),
    services.publishForecast(v3Unchanged.versionId)
  ]);
  const v3Plans = services.listPlans().filter((plan) => plan.forecastVersionId === v3Unchanged.versionId);
  if (race[0].status === 'fulfilled') {
    assert.equal(race[1].status, 'fulfilled');
    assert.equal(v3Plans.length, 1);
    assert.equal(v3Plans[0].inputSnapshot.forecast.status, 'published');
  } else {
    assert.equal(race[0].reason.code, 'FORECAST_NOT_PUBLISHED');
    assert.equal(v3Plans.length, 0);
    assert.equal(race[1].status, 'fulfilled');
  }

  for (const revision of services.listRevisions()) {
    const forecast = services.listForecasts().find((f) => f.versionId === revision.candidateVersionId);
    assert.equal(forecast.status, 'published');
  }
  for (const storedPlan of services.listPlans()) {
    const forecast = services.listForecasts().find((f) => f.versionId === storedPlan.forecastVersionId);
    assert.equal(forecast.status, 'published');
    assert.equal(storedPlan.inputSnapshot.forecast.status, 'published');
  }
});

test('event history replays after restart and reproduced solve results are deterministic', async () => {
  const db = tempDb();
  const services = servicesFor(db);
  await services.registerForecast(v1, { publish: true });
  const plan = await services.createPlan({ forecastVersionId: v1.versionId, tasks });
  await services.registerForecast(v4Narrowed, { publish: true });

  const restartedStore = new EventStore(db);
  const restarted = createServices(restartedStore);
  assert.equal(restarted.listForecasts().length, 2);
  assert.equal(restarted.listPlans().length, 1);
  const replayedPlan = restarted.getPlan(plan.id);
  assert.deepEqual(replayedPlan.assignments, plan.assignments);
  assert.deepEqual(replayedPlan.inputSnapshot, plan.inputSnapshot);

  const replayedForecast = replayedPlan.inputSnapshot.forecast;
  const replaySolution = solve(
    {
      versionId: replayedForecast.versionId,
      effectiveStart: EFFECTIVE_START,
      effectiveEnd: EFFECTIVE_END,
      windows: replayedForecast.windows
    },
    replayedPlan.inputSnapshot.tasks
  );
  assert.deepEqual(replaySolution.assignments, plan.assignments);

  const summary = restarted.replaySnapshot();
  assert.equal(summary.forecasts, 2);
  assert.equal(summary.plans, 1);
  assert.equal(summary.impactReports, 1);
});

test('audit trail records versions, locked plans, reports, and explicit revision decisions', async () => {
  const db = tempDb();
  const services = servicesFor(db);
  await services.registerForecast(v1, { publish: true, actor: 'tide-office' });
  const plan = await services.createPlan({ forecastVersionId: v1.versionId, tasks }, { actor: 'dispatcher' });
  await services.registerForecast(v4Narrowed, { publish: true, actor: 'tide-office' });
  const report = services.listReports({ planId: plan.id })[0];
  const revision = await services.proposeRevision(plan.id, 'tide-v4-narrowed', report.id, { actor: 'reviewer' });
  await services.adoptRevision(revision.id, { actor: 'manager' });

  const actions = services.listAudit().map((event) => event.action);
  assert.deepEqual(actions, [
    'forecast-registered',
    'forecast-published',
    'plan-created',
    'forecast-registered',
    'forecast-published',
    'impact-report-generated',
    'revision-proposed',
    'revision-adopted'
  ]);
  assert.equal(services.listAudit({ action: 'impact-report-generated' })[0].entityId, report.id);
  assert.equal(services.listAudit({ entityType: 'plan_revision' }).at(-1).actor, 'manager');
});

test('HTTP API exposes OpenAPI and enforces published-version solving', async () => {
  const db = tempDb();
  const app = createApp(db);
  const server = http.createServer(app.handle);
  await new Promise((resolve) => server.listen(0, resolve));
  const port = server.address().port;
  const base = `http://127.0.0.1:${port}`;

  async function api(method, route, body, actor = 'tester') {
    const response = await fetch(`${base}${route}`, {
      method,
      headers: { 'content-type': 'application/json', 'x-actor': actor },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    const json = await response.json();
    return { status: response.status, json };
  }

  try {
    const spec = await api('GET', '/openapi.json');
    assert.equal(spec.status, 200);
    assert.equal(spec.json.openapi, '3.1.0');
    assert.ok(spec.json.paths['/plans/{planId}/impact-reports'].post);

    let response = await api('POST', '/forecast-versions', {
      ...v4Narrowed
    });
    assert.equal(response.status, 201);
    assert.equal(response.json.forecastVersion.status, 'draft');

    response = await api('POST', '/plans', { forecastVersionId: 'tide-v4-narrowed', tasks });
    assert.equal(response.status, 409);
    assert.equal(response.json.error.code, 'FORECAST_NOT_PUBLISHED');

    response = await api('POST', '/forecast-versions', { ...v1, publish: true });
    assert.equal(response.status, 201);
    response = await api('POST', '/plans', { forecastVersionId: 'tide-v1', tasks });
    assert.equal(response.status, 201);
    const planId = response.json.plan.id;

    response = await api('POST', '/forecast-versions/tide-v4-narrowed/publish', {});
    assert.equal(response.status, 200);
    response = await api('POST', `/plans/${planId}/impact-reports`, {
      forecastVersionId: 'tide-v4-narrowed'
    });
    assert.ok([200, 201].includes(response.status), `unexpected impact status ${response.status}`);
    assert.equal(response.json.impactReport.candidateVersionId, 'tide-v4-narrowed');
    const reportId = response.json.impactReport.id;

    response = await api('POST', `/plans/${planId}/revisions`, {
      forecastVersionId: 'tide-v4-narrowed',
      impactReportId: reportId
    });
    assert.equal(response.status, 201);
    const revisionId = response.json.revision.id;
    response = await api('POST', `/revisions/${revisionId}/adopt`, {});
    assert.equal(response.status, 200);
    assert.equal(response.json.revision.status, 'adopted');

    response = await api('GET', '/audit-events');
    assert.equal(response.status, 200);
    assert.ok(response.json.auditEvents.length >= 8);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
