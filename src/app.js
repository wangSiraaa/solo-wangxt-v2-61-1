import { EventStore } from './store.js';
import { createServices } from './services.js';
import { DomainError } from './domain.js';
import { openapi } from './openapi.js';

export function createApp(dbPath = process.env.SCHEDULER_DB) {
  const store = new EventStore(dbPath);
  const services = createServices(store);
  return { store, services, handle: (req, res) => handle(req, res, services) };
}

async function handle(req, res, services) {
  setCors(res);
  if (req.method === 'OPTIONS') return send(res, 204, '');
  const url = new URL(req.url, 'http://localhost');
  const parts = url.pathname.split('/').filter(Boolean);
  const actor = req.headers['x-actor'] || 'api-client';
  const options = { actor };
  try {
    if (req.method === 'GET' && url.pathname === '/health') return send(res, 200, { ok: true });
    if (req.method === 'GET' && url.pathname === '/openapi.json') return send(res, 200, openapi);

    if (url.pathname === '/forecast-versions') {
      if (req.method === 'GET') return send(res, 200, { forecastVersions: services.listForecasts(query(url)) });
      if (req.method === 'POST') {
        const body = await readJson(req);
        const publish = body.publish === true || body.status === 'published';
        const forecast = await services.registerForecast(body, { ...options, publish });
        return send(res, publish ? 201 : 201, { forecastVersion: forecast });
      }
    }
    if (parts[0] === 'forecast-versions' && parts[2] === 'publish' && req.method === 'POST') {
      const forecast = await services.publishForecast(decode(parts[1]), options);
      return send(res, 200, { forecastVersion: forecast });
    }
    if (parts[0] === 'forecast-versions' && parts.length === 2) {
      if (req.method === 'GET') return send(res, 200, { forecastVersion: services.getForecast(decode(parts[1])) });
      if (req.method === 'POST') {
        const body = await readJson(req);
        if (body.publish === true || body.status === 'published') {
          const forecast = await services.publishForecast(decode(parts[1]), options);
          return send(res, 200, { forecastVersion: forecast });
        }
        throw new DomainError(400, 'INVALID_REQUEST', 'set publish=true to publish this forecast version');
      }
    }

    if (url.pathname === '/plans') {
      if (req.method === 'GET') return send(res, 200, { plans: services.listPlans(query(url)) });
      if (req.method === 'POST') {
        const plan = await services.createPlan(await readJson(req), options);
        return send(res, 201, { plan });
      }
    }
    if (parts[0] === 'plans' && parts.length === 2 && req.method === 'GET') {
      return send(res, 200, { plan: services.getPlan(decode(parts[1])) });
    }
    if (parts[0] === 'plans' && parts[2] === 'impact-reports') {
      const planId = decode(parts[1]);
      if (req.method === 'GET') return send(res, 200, { impactReports: services.listReports({ planId }) });
      if (req.method === 'POST') {
        const body = await readJson(req);
        const candidateVersionId = requiredString(body.forecastVersionId ?? body.candidateVersionId, 'candidateVersionId');
        const result = await services.generateImpact(planId, candidateVersionId, options);
        return send(res, result.created ? 201 : 200, { impactReport: result.report, deduped: !result.created });
      }
    }
    if (parts[0] === 'plans' && parts[2] === 'revisions') {
      const planId = decode(parts[1]);
      if (req.method === 'GET') return send(res, 200, { revisions: services.listRevisions({ planId }) });
      if (req.method === 'POST') {
        const body = await readJson(req);
        const revision = await services.proposeRevision(
          planId,
          requiredString(body.forecastVersionId ?? body.candidateVersionId, 'candidateVersionId'),
          requiredString(body.impactReportId, 'impactReportId'),
          options
        );
        return send(res, 201, { revision });
      }
    }

    if (url.pathname === '/impact-reports' && req.method === 'GET') {
      return send(res, 200, { impactReports: services.listReports(query(url)) });
    }
    if (parts[0] === 'impact-reports' && parts.length === 2 && req.method === 'GET') {
      return send(res, 200, { impactReport: services.getReport(decode(parts[1])) });
    }
    if (url.pathname === '/revisions' && req.method === 'GET') {
      return send(res, 200, { revisions: services.listRevisions(query(url)) });
    }
    if (parts[0] === 'revisions' && parts.length === 2 && req.method === 'GET') {
      return send(res, 200, { revision: services.getRevision(decode(parts[1])) });
    }
    if (parts[0] === 'revisions' && parts[2] === 'adopt' && req.method === 'POST') {
      const revision = await services.adoptRevision(decode(parts[1]), options);
      return send(res, 200, { revision });
    }
    if (parts[0] === 'revisions' && parts[2] === 'reject' && req.method === 'POST') {
      const body = await readJson(req).catch(() => ({}));
      const revision = await services.rejectRevision(decode(parts[1]), body.reviewNote || '', options);
      return send(res, 200, { revision });
    }
    if (url.pathname === '/audit-events' && req.method === 'GET') {
      return send(res, 200, { auditEvents: services.listAudit(query(url)) });
    }
    if (url.pathname === '/admin/replay' && req.method === 'POST') {
      return send(res, 200, { replay: services.replaySnapshot() });
    }

    return send(res, 404, { error: { code: 'ROUTE_NOT_FOUND', message: `${req.method} ${url.pathname} is not defined` } });
  } catch (error) {
    const status = error instanceof DomainError ? error.status : 500;
    const code = error instanceof DomainError ? error.code : 'INTERNAL_ERROR';
    const message = error instanceof DomainError ? error.message : 'unexpected server error';
    return send(res, status, { error: { code, message, details: error.details } });
  }
}

function query(url) {
  return Object.fromEntries(url.searchParams.entries());
}

function decode(value) {
  return decodeURIComponent(value);
}

function requiredString(value, field) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new DomainError(400, 'INVALID_REQUEST', `${field} is required`);
  }
  return value;
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString('utf8');
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    throw new DomainError(400, 'INVALID_JSON', 'request body must be valid JSON');
  }
}

function send(res, status, body) {
  if (body === '') {
    res.writeHead(status);
    res.end();
    return;
  }
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(payload)
  });
  res.end(payload);
}

function setCors(res) {
  res.setHeader('access-control-allow-origin', '*');
  res.setHeader('access-control-allow-methods', 'GET,POST,OPTIONS');
  res.setHeader('access-control-allow-headers', 'content-type,x-actor');
}
