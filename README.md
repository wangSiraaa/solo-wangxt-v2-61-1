# Versioned offline-tide scheduling service

This service treats every offline tide table revision as an immutable, explicitly versioned input. Plans solve against exactly one **published complete forecast version** and store a hash-protected input snapshot. Later forecasts never silently mutate a locked plan: they produce reviewable impact reports. A plan changes only when an explicit revision is proposed and then adopted.

## Run

```bash
npm test
PORT=8080 SCHEDULER_DB=./data/scheduler-events.jsonl npm start
```

No external database is required. The durable log is an append-only JSONL event journal; current state and audit history are rebuilt by deterministic event replay.

## Core rules

- Forecast versions have:
  - `versionId`
  - offline `source`
  - monotonic `revision`
  - `effectiveStart` / `effectiveEnd`
  - complete tide-window input
  - `draft` or `published` status
  - a canonical `contentHash`
- Plan creation requires a `forecastVersionId` whose status is `published`; drafts are rejected.
- Plans are locked and persist:
  - the selected forecast version and revision
  - the complete forecast snapshot
  - normalized task input
  - deterministic solver metadata
  - a snapshot content hash
- Publishing a newer relevant forecast creates an impact report, but does not modify a locked plan.
- Repeated/duplicate publication or report requests are idempotent.
- Delayed older revisions are accepted for historical completeness but do not generate out-of-order automatic impacts.
- Explicit revisions create a separate candidate plan. Adoption marks the old plan `superseded` and candidate `active`; rejection leaves the original active.

## API overview

OpenAPI is served at `GET /openapi.json`.

| Operation | Endpoint |
| --- | --- |
| Register draft/published version | `POST /forecast-versions` |
| List versions | `GET /forecast-versions?status=published` |
| Get version | `GET /forecast-versions/{versionId}` |
| Publish draft | `POST /forecast-versions/{versionId}/publish` |
| Solve/lock plan | `POST /plans` |
| List/get plans | `GET /plans`, `GET /plans/{planId}` |
| Generate/fetch impact reports | `POST/GET /plans/{planId}/impact-reports` |
| Propose explicit revision | `POST /plans/{planId}/revisions` |
| Adopt/reject revision | `POST /revisions/{revisionId}/adopt` / `reject` |
| Audit events | `GET /audit-events` |
| Rebuild and verify replay | `POST /admin/replay` |

Pass the actor in the `x-actor` header. Every durable command is serialized, journaled, and recorded in the audit trail.

## Example forecast

```json
{
  "versionId": "tide-v1",
  "source": "harbor-master",
  "revision": 1,
  "effectiveStart": "2026-01-01T00:00:00.000Z",
  "effectiveEnd": "2026-01-03T00:00:00.000Z",
  "publish": true,
  "windows": [
    {
      "windowId": "alpha-w1",
      "vesselId": "alpha",
      "start": "2026-01-01T22:00:00.000Z",
      "end": "2026-01-02T02:00:00.000Z"
    }
  ]
}
```

## Example solve request

```json
{
  "forecastVersionId": "tide-v1",
  "name": "night operations",
  "tasks": [
    {
      "taskId": "alpha-mooring",
      "vesselId": "alpha",
      "durationMinutes": 120,
      "earliestStart": "2026-01-01T21:00:00.000Z",
      "latestFinish": "2026-01-02T04:00:00.000Z"
    }
  ]
}
```

## Impact report contents

Each affected task includes:

- task and vessel identifiers
- `infeasible`, `window_moved`, or `window_changed`
- original feasible tide windows
- candidate windows with feasibility flags
- original assignment, if one existed
- new assignment, when feasible
- precise reason such as a missing vessel window, narrowed continuous window, or vessel conflict

Cross-midnight windows use absolute UTC instants, so narrowing across midnight is compared precisely rather than by calendar date.
