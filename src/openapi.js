export const openapi = {
  openapi: '3.1.0',
  info: {
    title: 'Versioned Tide Scheduling API',
    version: '1.0.0',
    description: 'Offline tide forecast versions, immutable locked plans, reviewable impact reports, and explicit plan revisions.'
  },
  paths: {
    '/openapi.json': {
      get: {
        summary: 'OpenAPI document',
        responses: { '200': { description: 'OpenAPI JSON' } }
      }
    },
    '/health': {
      get: {
        summary: 'Service health',
        responses: { '200': { description: 'OK' } }
      }
    },
    '/forecast-versions': {
      get: {
        summary: 'List forecast versions',
        parameters: [queryParameter('status', 'draft|published')],
        responses: { '200': { description: 'Forecast versions' } }
      },
      post: {
        summary: 'Register a complete draft forecast version',
        responses: {
          '201': { description: 'Draft created' },
          '400': { description: 'Invalid forecast' },
          '409': { description: 'Version or source revision conflict' }
        }
      }
    },
    '/forecast-versions/{versionId}/publish': {
      post: {
        summary: 'Publish a forecast version; repeated publication is idempotent',
        parameters: [pathParameter('versionId')],
        responses: {
          '200': { description: 'Published forecast' },
          '404': { description: 'Forecast not found' }
        }
      }
    },
    '/forecast-versions/{versionId}': {
      get: {
        summary: 'Get forecast version',
        parameters: [pathParameter('versionId')],
        responses: { '200': { description: 'Forecast' }, '404': { description: 'Not found' } }
      },
      post: {
        summary: 'Publish an existing draft forecast version',
        parameters: [pathParameter('versionId')],
        responses: { '200': { description: 'Published forecast' }, '404': { description: 'Not found' } }
      }
    },
    '/plans': {
      get: {
        summary: 'List plans',
        parameters: [queryParameter('status', 'active|proposed|superseded|rejected')],
        responses: { '200': { description: 'Plans' } }
      },
      post: {
        summary: 'Solve and lock a plan against one explicitly selected published forecast version',
        responses: {
          '201': { description: 'Locked plan' },
          '409': { description: 'Forecast is not published' },
          '422': { description: 'Plan infeasible' }
        }
      }
    },
    '/plans/{planId}': {
      get: {
        summary: 'Get plan including immutable forecast and task input snapshot',
        parameters: [pathParameter('planId')],
        responses: { '200': { description: 'Plan' }, '404': { description: 'Not found' } }
      }
    },
    '/plans/{planId}/impact-reports': {
      get: {
        summary: 'List impact reports for a plan',
        parameters: [pathParameter('planId')],
        responses: { '200': { description: 'Impact reports' } }
      },
      post: {
        summary: 'Generate a reviewable impact report for a candidate published forecast',
        parameters: [pathParameter('planId')],
        responses: {
          '200': { description: 'Existing equivalent report returned' },
          '201': { description: 'Impact report generated' },
          '409': { description: 'Draft/version mismatch' },
          '422': { description: 'Candidate does not cover plan interval' }
        }
      }
    },
    '/plans/{planId}/revisions': {
      get: {
        summary: 'List explicit revisions for a plan',
        parameters: [pathParameter('planId')],
        responses: { '200': { description: 'Revisions' } }
      },
      post: {
        summary: 'Propose an explicit revision bound to one complete candidate version',
        parameters: [pathParameter('planId')],
        responses: {
          '201': { description: 'Revision proposed' },
          '409': { description: 'Open report/revision conflict or incomplete version' },
          '422': { description: 'Candidate infeasible' }
        }
      }
    },
    '/impact-reports': {
      get: {
        summary: 'List all impact reports',
        parameters: [
          queryParameter('planId'),
          queryParameter('candidateVersionId'),
          queryParameter('status', 'open|adopted|rejected')
        ],
        responses: { '200': { description: 'Reports' } }
      }
    },
    '/impact-reports/{reportId}': {
      get: {
        summary: 'Get impact report',
        parameters: [pathParameter('reportId')],
        responses: { '200': { description: 'Report' }, '404': { description: 'Not found' } }
      }
    },
    '/revisions': {
      get: {
        summary: 'List revisions',
        parameters: [queryParameter('planId'), queryParameter('status', 'proposed|adopted|rejected')],
        responses: { '200': { description: 'Revisions' } }
      }
    },
    '/revisions/{revisionId}': {
      get: {
        summary: 'Get revision',
        parameters: [pathParameter('revisionId')],
        responses: { '200': { description: 'Revision' }, '404': { description: 'Not found' } }
      }
    },
    '/revisions/{revisionId}/adopt': {
      post: {
        summary: 'Adopt a feasible proposed revision',
        parameters: [pathParameter('revisionId')],
        responses: {
          '200': { description: 'Revision adopted' },
          '409': { description: 'Revision is not open' }
        }
      }
    },
    '/revisions/{revisionId}/reject': {
      post: {
        summary: 'Reject a proposed revision',
        parameters: [pathParameter('revisionId')],
        responses: {
          '200': { description: 'Revision rejected' },
          '409': { description: 'Revision is not open' }
        }
      }
    },
    '/audit-events': {
      get: {
        summary: 'List append-only audit events',
        parameters: [
          queryParameter('entityType'),
          queryParameter('entityId'),
          queryParameter('action')
        ],
        responses: { '200': { description: 'Audit events' } }
      }
    },
    '/admin/replay': {
      post: {
        summary: 'Rebuild current state from durable event history and verify input snapshots are replayable',
        responses: { '200': { description: 'Replay summary' } }
      }
    }
  },
  components: {
    schemas: {
      Error: {
        type: 'object',
        required: ['error'],
        properties: {
          error: {
            type: 'object',
            properties: {
              code: { type: 'string' },
              message: { type: 'string' },
              details: {}
            }
          }
        }
      },
      ForecastVersion: {
        type: 'object',
        required: ['versionId', 'source', 'revision', 'status', 'effectiveStart', 'effectiveEnd', 'windows', 'inputSnapshot'],
        properties: {
          versionId: { type: 'string' },
          source: { type: 'string' },
          revision: { type: 'integer', minimum: 1 },
          status: { type: 'string', enum: ['draft', 'published'] },
          effectiveStart: { type: 'string', format: 'date-time' },
          effectiveEnd: { type: 'string', format: 'date-time' },
          windows: { type: 'array', items: { $ref: '#/components/schemas/TideWindow' } },
          publishedAt: { type: ['string', 'null'], format: 'date-time' },
          contentHash: { type: 'string' },
          inputSnapshot: { $ref: '#/components/schemas/ForecastInputSnapshot' }
        }
      },
      TideWindow: {
        type: 'object',
        required: ['windowId', 'vesselId', 'start', 'end'],
        properties: {
          windowId: { type: 'string' },
          vesselId: { type: 'string' },
          start: { type: 'string', format: 'date-time' },
          end: { type: 'string', format: 'date-time' }
        }
      },
      ForecastInputSnapshot: {
        type: 'object',
        required: ['source', 'revision', 'effectiveStart', 'effectiveEnd', 'windows'],
        properties: {
          source: { type: 'string' },
          revision: { type: 'integer' },
          effectiveStart: { type: 'string', format: 'date-time' },
          effectiveEnd: { type: 'string', format: 'date-time' },
          windows: { type: 'array', items: { $ref: '#/components/schemas/TideWindow' } }
        }
      },
      Plan: {
        type: 'object',
        required: ['id', 'status', 'forecastVersionId', 'assignments', 'locked', 'inputSnapshot'],
        properties: {
          id: { type: 'string' },
          status: { type: 'string', enum: ['active', 'proposed', 'superseded', 'rejected'] },
          forecastVersionId: { type: 'string' },
          assignments: { type: 'array', items: { $ref: '#/components/schemas/Assignment' } },
          locked: { type: 'boolean' },
          lockedAt: { type: 'string', format: 'date-time' },
          inputSnapshot: { $ref: '#/components/schemas/PlanInputSnapshot' }
        }
      },
      Assignment: {
        type: 'object',
        required: ['taskId', 'vesselId', 'windowId', 'start', 'end'],
        properties: {
          taskId: { type: 'string' },
          vesselId: { type: 'string' },
          windowId: { type: 'string' },
          windowStart: { type: 'string', format: 'date-time' },
          windowEnd: { type: 'string', format: 'date-time' },
          start: { type: 'string', format: 'date-time' },
          end: { type: 'string', format: 'date-time' },
          durationMinutes: { type: 'integer' }
        }
      },
      PlanInputSnapshot: {
        type: 'object',
        required: ['forecast', 'tasks', 'solver', 'contentHash'],
        properties: {
          forecast: { $ref: '#/components/schemas/ForecastInputSnapshot' },
          tasks: { type: 'array' },
          solver: { type: 'object' },
          contentHash: { type: 'string' }
        }
      },
      ImpactReport: {
        type: 'object',
        required: ['id', 'planId', 'baselineVersionId', 'candidateVersionId', 'status', 'impacts'],
        properties: {
          id: { type: 'string' },
          planId: { type: 'string' },
          baselineVersionId: { type: 'string' },
          candidateVersionId: { type: 'string' },
          status: { type: 'string', enum: ['open', 'adopted', 'rejected'] },
          affectedVesselIds: { type: 'array', items: { type: 'string' } },
          impacts: { type: 'array', items: { $ref: '#/components/schemas/ImpactItem' } }
        }
      },
      ImpactItem: {
        type: 'object',
        required: ['taskId', 'vesselId', 'impact', 'reason', 'oldFeasibleWindows', 'newWindows'],
        properties: {
          taskId: { type: 'string' },
          vesselId: { type: 'string' },
          impact: { type: 'string', enum: ['infeasible', 'window_moved', 'window_changed'] },
          reason: { type: 'string' },
          reasonDetail: { type: 'string' },
          oldAssignment: { oneOf: [{ $ref: '#/components/schemas/Assignment' }, { type: 'null' }] },
          newAssignment: { oneOf: [{ $ref: '#/components/schemas/Assignment' }, { type: 'null' }] },
          oldFeasibleWindows: { type: 'array', items: { $ref: '#/components/schemas/TideWindow' } },
          newWindows: { type: 'array', items: { $ref: '#/components/schemas/TideWindowWithFeasibility' } }
        }
      },
      TideWindowWithFeasibility: {
        allOf: [
          { $ref: '#/components/schemas/TideWindow' },
          { type: 'object', required: ['feasible'], properties: { feasible: { type: 'boolean' } } }
        ]
      },
      PlanRevision: {
        type: 'object',
        required: ['id', 'planId', 'candidatePlanId', 'baselineVersionId', 'candidateVersionId', 'impactReportId', 'status'],
        properties: {
          id: { type: 'string' },
          revisionNumber: { type: 'integer' },
          planId: { type: 'string' },
          candidatePlanId: { type: 'string' },
          baselineVersionId: { type: 'string' },
          candidateVersionId: { type: 'string' },
          impactReportId: { type: 'string' },
          status: { type: 'string', enum: ['proposed', 'adopted', 'rejected'] },
          candidatePlan: { $ref: '#/components/schemas/Plan' }
        }
      }
    }
  }
};

function pathParameter(name) {
  return { name, in: 'path', required: true, schema: { type: 'string' } };
}

function queryParameter(name, description) {
  return { name, in: 'query', required: false, schema: { type: 'string' }, description };
}
