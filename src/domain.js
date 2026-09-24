import crypto from 'node:crypto';
import { addMinutes, contains, overlap, toIso } from './time.js';

export class DomainError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function fail(status, code, message, details) {
  throw new DomainError(status, code, message, details);
}

function requireString(value, field, { min = 1 } = {}) {
  if (typeof value !== 'string' || value.trim().length < min) {
    fail(400, 'INVALID_REQUEST', `${field} is required and must be a non-empty string`);
  }
  return value;
}

function normalizeWindow(input, index, effectiveStart, effectiveEnd) {
  const prefix = `windows[${index}]`;
  const vesselId = requireString(input?.vesselId, `${prefix}.vesselId`);
  const windowId = input.windowId ? requireString(input.windowId, `${prefix}.windowId`) : `${vesselId}:${index + 1}`;
  const start = toIso(input?.start, `${prefix}.start`);
  const end = toIso(input?.end, `${prefix}.end`);
  if (Date.parse(end) <= Date.parse(start)) {
    fail(400, 'INVALID_FORECAST', `${prefix}.end must be later than start`);
  }
  if (!contains(effectiveStart, effectiveEnd, start, end)) {
    fail(400, 'INVALID_FORECAST', `${prefix} must be fully inside the forecast effective interval`, {
      windowId,
      effectiveStart,
      effectiveEnd
    });
  }
  return { windowId, vesselId, start, end };
}

export function normalizeForecastInput(input) {
  const versionId = requireString(input?.versionId, 'versionId');
  const source = requireString(input?.source ?? 'offline-tide-table', 'source');
  const revision = input.revision;
  if (!Number.isInteger(revision) || revision < 1) {
    fail(400, 'INVALID_FORECAST', 'revision must be a positive integer');
  }
  const effectiveStart = toIso(input?.effectiveStart, 'effectiveStart');
  const effectiveEnd = toIso(input?.effectiveEnd, 'effectiveEnd');
  if (Date.parse(effectiveEnd) <= Date.parse(effectiveStart)) {
    fail(400, 'INVALID_FORECAST', 'effectiveEnd must be later than effectiveStart');
  }
  if (!Array.isArray(input?.windows) || input.windows.length === 0) {
    fail(400, 'INVALID_FORECAST', 'windows must be a non-empty array');
  }
  const windows = input.windows
    .map((window, index) => normalizeWindow(window, index, effectiveStart, effectiveEnd))
    .sort((a, b) =>
      Date.parse(a.start) - Date.parse(b.start) ||
      Date.parse(a.end) - Date.parse(b.end) ||
      a.windowId.localeCompare(b.windowId)
    );
  const seen = new Set();
  for (const window of windows) {
    const key = window.windowId;
    if (seen.has(key)) fail(400, 'INVALID_FORECAST', `duplicate window id ${key}`);
    seen.add(key);
  }
  const contentHash = hashCanonical({
    source,
    revision,
    effectiveStart,
    effectiveEnd,
    windows
  });
  return {
    versionId,
    source,
    revision,
    status: 'draft',
    effectiveStart,
    effectiveEnd,
    windows,
    createdAt: null,
    publishedAt: null,
    contentHash,
    // A version's entire solving input is captured before publication. It is never mutated.
    inputSnapshot: {
      source,
      revision,
      effectiveStart,
      effectiveEnd,
      windows
    }
  };
}

export function normalizeTask(input, index, intervalStart, intervalEnd) {
  const prefix = `tasks[${index}]`;
  const taskId = requireString(input?.taskId, `${prefix}.taskId`);
  const vesselId = requireString(input?.vesselId, `${prefix}.vesselId`);
  const durationMinutes = input.durationMinutes;
  if (!Number.isInteger(durationMinutes) || durationMinutes <= 0) {
    fail(400, 'INVALID_TASK', `${prefix}.durationMinutes must be a positive integer`);
  }
  const earliestStart = toIso(input?.earliestStart ?? intervalStart, `${prefix}.earliestStart`);
  const latestFinish = toIso(input?.latestFinish ?? intervalEnd, `${prefix}.latestFinish`);
  if (Date.parse(latestFinish) <= Date.parse(earliestStart)) {
    fail(400, 'INVALID_TASK', `${prefix}.latestFinish must be later than earliestStart`);
  }
  if (!contains(intervalStart, intervalEnd, earliestStart, latestFinish)) {
    fail(400, 'INVALID_TASK', `${prefix} must be fully inside the forecast effective interval`);
  }
  if (durationMinutes * 60_000 > Date.parse(latestFinish) - Date.parse(earliestStart)) {
    fail(422, 'NO_FEASIBLE_WINDOW', `${prefix} duration exceeds its permitted interval`, {
      taskId,
      vesselId,
      reason: 'TASK_DURATION_EXCEEDS_INTERVAL'
    });
  }
  return { taskId, vesselId, durationMinutes, earliestStart, latestFinish };
}

export function solve(forecast, tasks) {
  const windowsByVessel = new Map();
  for (const window of forecast.windows) {
    if (!windowsByVessel.has(window.vesselId)) windowsByVessel.set(window.vesselId, []);
    windowsByVessel.get(window.vesselId).push(window);
  }

  const assignments = [];
  const failures = [];
  // Task order is the canonical solving order; within each vessel, selected intervals
  // are enforced non-overlapping, so independent vessels and tasks remain deterministic.
  for (const task of tasks) {
    const windows = windowsByVessel.get(task.vesselId);
    if (!windows || windows.length === 0) {
      failures.push({
        taskId: task.taskId,
        vesselId: task.vesselId,
        reason: 'NO_TIDE_WINDOW_FOR_VESSEL',
        reasonDetail: `forecast ${forecast.versionId} contains no tide window for vessel ${task.vesselId}`
      });
      continue;
    }
    const feasible = [];
    for (const window of windows) {
      const intersectionStart = maxIso(task.earliestStart, window.start);
      const intersectionEnd = minIso(task.latestFinish, window.end);
      if (Date.parse(intersectionEnd) - Date.parse(intersectionStart) < task.durationMinutes * 60_000) {
        continue;
      }
      // Earliest legal start in this tide window.
      const start = intersectionStart;
      const end = addMinutes(start, task.durationMinutes);
      const busy = assignments.some((assignment) =>
        assignment.vesselId === task.vesselId &&
        overlap(assignment.start, assignment.end, start, end)
      );
      if (!busy) feasible.push({ window, start, end });
    }
    feasible.sort((a, b) =>
      Date.parse(a.start) - Date.parse(b.start) ||
      a.window.windowId.localeCompare(b.window.windowId)
    );
    const selected = feasible[0];
    if (!selected) {
      failures.push({
        taskId: task.taskId,
        vesselId: task.vesselId,
        reason: 'NO_FEASIBLE_WINDOW',
        reasonDetail: 'no tide window is long enough inside the task interval without a vessel conflict'
      });
    } else {
      assignments.push({
        taskId: task.taskId,
        vesselId: task.vesselId,
        windowId: selected.window.windowId,
        windowStart: selected.window.start,
        windowEnd: selected.window.end,
        start: selected.start,
        end: selected.end,
        durationMinutes: task.durationMinutes
      });
    }
  }

  assignments.sort((a, b) =>
    a.vesselId.localeCompare(b.vesselId) ||
    a.taskId.localeCompare(b.taskId) ||
    Date.parse(a.start) - Date.parse(b.start));
  return {
    feasible: failures.length === 0,
    assignments,
    failures
  };
}

export function feasibleWindowsForTask(forecast, task) {
  return forecast.windows
    .filter((window) => window.vesselId === task.vesselId)
    .map((window) => {
      const hasOverlap = overlap(task.earliestStart, task.latestFinish, window.start, window.end);
      const intersectionStart = hasOverlap ? maxIso(task.earliestStart, window.start) : window.start;
      const intersectionEnd = hasOverlap ? minIso(task.latestFinish, window.end) : window.end;
      const feasible = hasOverlap &&
        Date.parse(intersectionEnd) - Date.parse(intersectionStart) >= task.durationMinutes * 60_000;
      return { window, intersectionStart, intersectionEnd, feasible };
    })
    .sort((a, b) =>
      Date.parse(a.intersectionStart) - Date.parse(b.intersectionStart) ||
      a.window.windowId.localeCompare(b.window.windowId));
}

export function buildImpactReport({ plan, baselineForecast, candidateForecast, tasks, generatedAt, source, auto = false }) {
  const candidateSolution = solve(candidateForecast, tasks);
  const newAssignments = new Map(candidateSolution.assignments.map((a) => [a.taskId, a]));
  const impacts = [];

  for (const task of tasks) {
    const oldAssignment = plan.assignments.find((a) => a.taskId === task.taskId);
    const oldWindows = feasibleWindowsForTask(baselineForecast, task);
    const newWindows = feasibleWindowsForTask(candidateForecast, task);
    const oldFeasible = oldWindows.filter((x) => x.feasible);
    const newFeasible = newWindows.filter((x) => x.feasible);
    const newAssignment = newAssignments.get(task.taskId);

    if (!newAssignment) {
      const reason = newFeasible.length === 0 ? 'WINDOW_TOO_SHORT_OR_MISSING' : 'NO_FEASIBLE_WINDOW';
      impacts.push({
        taskId: task.taskId,
        vesselId: task.vesselId,
        impact: 'infeasible',
        reason,
        reasonDetail: explain(task, newWindows, candidateForecast),
        oldAssignment: oldAssignment ?? null,
        newAssignment: null,
        oldFeasibleWindows: oldFeasible.map(windowView),
        newWindows: newWindows.map((x) => ({ ...windowView(x), feasible: x.feasible }))
      });
      continue;
    }

    const assignmentChanged = !oldAssignment ||
      oldAssignment.windowId !== newAssignment.windowId ||
      oldAssignment.start !== newAssignment.start ||
      oldAssignment.end !== newAssignment.end;
    const windowsChanged = JSON.stringify(oldFeasible.map(signature).sort()) !==
      JSON.stringify(newFeasible.map(signature).sort());

    if (assignmentChanged || windowsChanged) {
      impacts.push({
        taskId: task.taskId,
        vesselId: task.vesselId,
        impact: assignmentChanged ? 'window_moved' : 'window_changed',
        reason: assignmentChanged ? 'original window no longer supports the locked assignment' : 'feasible tide window changed',
        reasonDetail: assignmentChanged
          ? 'the locked start/end or tide window is no longer valid under the candidate forecast'
          : 'the candidate still solves this task, but its feasible window set is different',
        oldAssignment: oldAssignment ?? null,
        newAssignment,
        oldFeasibleWindows: oldFeasible.map(windowView),
        newWindows: newWindows.map((x) => ({ ...windowView(x), feasible: x.feasible }))
      });
    }
  }

  impacts.sort((a, b) => a.vesselId.localeCompare(b.vesselId) || a.taskId.localeCompare(b.taskId));
  const idSource = `${plan.id}:${candidateForecast.versionId}:${hashCanonical(impacts)}:${source}`;
  return {
    id: `ir_${crypto.createHash('sha256').update(idSource).digest('hex').slice(0, 24)}`,
    planId: plan.id,
    baselineVersionId: baselineForecast.versionId,
    candidateVersionId: candidateForecast.versionId,
    source,
    status: 'open',
    feasible: candidateSolution.feasible,
    affectedVesselIds: [...new Set(impacts.map((i) => i.vesselId))].sort(),
    impacts,
    generatedAt,
    auto
  };
}

function explain(task, windows, forecast) {
  if (!windows.some((x) => x.window.vesselId === task.vesselId)) {
    return `forecast ${forecast.versionId} has no tide window for vessel ${task.vesselId}`;
  }
  if (windows.length === 0 || windows.every((x) => !x.feasible)) {
    return 'the narrowed tide window does not contain enough continuous time for the task duration';
  }
  return 'no selected interval avoids another task on the same vessel';
}

function windowView(entry) {
  return {
    windowId: entry.window.windowId,
    vesselId: entry.window.vesselId,
    start: entry.window.start,
    end: entry.window.end
  };
}

function signature(window) {
  return `${window.windowId}:${window.start}:${window.end}`;
}

function maxIso(a, b) {
  return Date.parse(a) >= Date.parse(b) ? a : b;
}

function minIso(a, b) {
  return Date.parse(a) <= Date.parse(b) ? a : b;
}

export function hashCanonical(value) {
  return crypto.createHash('sha256').update(stableStringify(value)).digest('hex');
}

export function stableStringify(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`;
}
