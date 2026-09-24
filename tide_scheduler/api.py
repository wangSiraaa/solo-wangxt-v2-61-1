from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from . import core
from .db import connect, initialize


class AppState:
    def __init__(self, database_path: str):
        self.database_path = database_path
        initialize(database_path)


class Handler(BaseHTTPRequestHandler):
    server_version = "TideScheduler/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def actor(self) -> str:
        return self.headers.get("X-Actor", "anonymous")

    def send_json(self, status_code: int, value: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise core.ServiceError(400, "invalid_json", "request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise core.ServiceError(422, "invalid_request", "request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        self.handle_request("GET")

    def do_POST(self) -> None:
        self.handle_request("POST")

    def handle_request(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in {"/health", "/healthz"} and method == "GET":
            self.send_json(200, {"status": "ok"})
            return
        if path in {"/openapi.json", "/api/openapi.json", "/api/v1/spec", "/api/v1/openapi"} and method == "GET":
            self.send_json(200, OPENAPI)
            return
        if path == "/api/v1/vessels" and method == "POST":
            self.with_connection(lambda conn: core.create_vessel(conn, self.read_json(), self.actor()), 201)
            return
        if path == "/api/v1/forecasts" and method == "POST":
            def action(conn):
                forecast, created = core.create_forecast(conn, self.read_json(), self.actor())
                return core.forecast_snapshot(conn, forecast), 201 if created else 200
            self.with_connection(action)
            return
        match = re.fullmatch(r"/api/v1/forecasts/([^/]+)/publish", path)
        if match and method == "POST":
            def publish_action(conn):
                forecast, _ = core.publish_forecast(conn, match.group(1), self.actor())
                return core.forecast_snapshot(conn, forecast)
            self.with_connection(publish_action)
            return
        match = re.fullmatch(r"/api/v1/forecasts/([^/]+)", path)
        if match and method == "GET":
            def action(conn):
                return core.forecast_snapshot(conn, core.get_forecast_or_404(conn, match.group(1)))
            self.with_connection(action)
            return
        if path == "/api/v1/plans:solve" and method == "POST":
            self.with_connection(lambda conn: core.solve_plan(conn, self.read_json(), self.actor()), 201)
            return
        match = re.fullmatch(r"/api/v1/plans/([^/]+)", path)
        if match and method == "GET":
            self.with_connection(lambda conn: core.revision_response(conn, core.get_plan_current(conn, match.group(1))))
            return
        match = re.fullmatch(r"/api/v1/plans/([^/]+)/revisions/(\d+):replay", path)
        if match and method == "GET":
            self.with_connection(lambda conn: core.replay_revision(conn, match.group(1), int(match.group(2))))
            return
        match = re.fullmatch(r"/api/v1/plans/([^/]+)/impact-reports", path)
        if match and method == "POST":
            def action(conn):
                report, created = core.create_impact_report(conn, match.group(1), self.read_json(), self.actor())
                return report, 201 if created else 200
            self.with_connection(action)
            return
        match = re.fullmatch(r"/api/v1/impact-reports/([^/]+)", path)
        if match and method == "GET":
            def action(conn):
                row = conn.execute("SELECT * FROM impact_reports WHERE id = ?", (match.group(1),)).fetchone()
                if row is None:
                    raise core.ServiceError(404, "impact_report_not_found", "impact report does not exist")
                return core.report_response(conn, row)
            self.with_connection(action)
            return
        match = re.fullmatch(r"/api/v1/impact-reports/([^/]+):adopt", path)
        if match and method == "POST":
            self.with_connection(lambda conn: core.adopt_impact_report(conn, match.group(1), self.actor()), 201)
            return
        match = re.fullmatch(r"/api/v1/impact-reports/([^/]+):dismiss", path)
        if match and method == "POST":
            self.with_connection(lambda conn: core.dismiss_impact_report(conn, match.group(1), self.actor()))
            return
        if path == "/api/v1/audit-events" and method == "GET":
            self.with_connection(lambda conn: [
                {**row, "details": json.loads(row.pop("details_json"))}
                for row in conn.execute(
                    "SELECT id, event_type, actor, entity_type, entity_id, details_json, occurred_at, previous_hash, entry_hash FROM audit_events ORDER BY id"
                ).fetchall()
            ])
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"{method} {path} not found"}})

    def with_connection(self, action, success_status: int = 200) -> None:
        conn = connect(self.state.database_path)
        try:
            result = action(conn)
            status = success_status
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
                result, status = result
            self.send_json(status, result)
        except core.ServiceError as exc:
            self.send_json(exc.status_code, {"error": {"code": exc.code, "message": exc.message}})
        finally:
            conn.close()


def build_server(database_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.state = AppState(database_path)  # type: ignore[attr-defined]
    return server


_ERROR_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
        }
    },
}
_TIME = {"type": "string", "format": "date-time"}
_COMMON_ERRORS = {
    "400": {"description": "Malformed JSON", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
    "404": {"description": "Resource not found", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
    "409": {"description": "State conflict", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
    "422": {"description": "Validation or infeasibility error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
}


def _request(schema_name: str) -> dict[str, Any]:
    return {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema_name}"}}}}


OPENAPI: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Offline Tide-aware Scheduling API",
        "version": "1.0.0",
        "description": (
            "Forecast-versioned scheduling. Plans bind an explicit published forecast and persist both "
            "the plan input and complete forecast snapshot. New forecasts only create reviewable impact "
            "reports; locked plans change only through explicit report adoption."
        ),
    },
    "components": {
        "schemas": {
            "Error": _ERROR_SCHEMA,
            "Vessel": {"type": "object", "required": ["id", "name"], "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
            "TideWindow": {
                "type": "object",
                "required": ["vessel_id", "starts_at", "ends_at"],
                "properties": {"vessel_id": {"type": "string"}, "starts_at": _TIME, "ends_at": _TIME},
            },
            "ForecastCreate": {
                "type": "object",
                "required": ["id", "effective_from", "effective_to", "windows"],
                "properties": {
                    "id": {"type": "string"},
                    "effective_from": _TIME,
                    "effective_to": _TIME,
                    "windows": {"type": "array", "minItems": 1, "items": {"$ref": "#/components/schemas/TideWindow"}},
                },
            },
            "Forecast": {
                "allOf": [
                    {"$ref": "#/components/schemas/ForecastCreate"},
                    {"type": "object", "required": ["status", "content_hash", "windows"], "properties": {
                        "status": {"enum": ["draft", "published", "superseded"]},
                        "content_hash": {"type": "string"},
                        "windows": {"type": "array", "items": {"$ref": "#/components/schemas/StoredTideWindow"}},
                    }},
                ]
            },
            "StoredTideWindow": {"allOf": [
                {"$ref": "#/components/schemas/TideWindow"},
                {"type": "object", "required": ["ordinal"], "properties": {"ordinal": {"type": "integer", "minimum": 0}}},
            ]},
            "Task": {
                "type": "object",
                "required": ["id", "vessel_id", "duration_minutes"],
                "properties": {
                    "id": {"type": "string"},
                    "vessel_id": {"type": "string"},
                    "duration_minutes": {"type": "integer", "exclusiveMinimum": 0},
                    "earliest_start": _TIME,
                    "latest_start": _TIME,
                },
            },
            "PlanSolve": {
                "type": "object",
                "required": ["forecast_version_id", "tasks"],
                "properties": {
                    "plan_id": {"type": "string"},
                    "forecast_version_id": {"type": "string", "description": "Must be a complete published version."},
                    "tasks": {"type": "array", "minItems": 1, "items": {"$ref": "#/components/schemas/Task"}},
                },
            },
            "PlanRevision": {
                "type": "object",
                "required": ["plan_id", "revision_number", "status", "forecast_version_id", "input_snapshot", "forecast_snapshot", "result"],
                "properties": {
                    "plan_id": {"type": "string"},
                    "revision_number": {"type": "integer"},
                    "status": {"enum": ["locked", "superseded"]},
                    "forecast_version_id": {"type": "string"},
                    "input_snapshot": {"type": "object"},
                    "forecast_snapshot": {"type": "object"},
                    "result": {"type": "object"},
                    "replay_hash": {"type": "string"},
                },
            },
            "ImpactCreate": {
                "type": "object",
                "required": ["target_forecast_version_id"],
                "properties": {"target_forecast_version_id": {"type": "string"}, "base_revision_number": {"type": "integer", "minimum": 1}},
            },
            "ImpactItem": {
                "type": "object",
                "required": ["task_id", "vessel_id", "impact_type", "reason",
                             "original_window_starts_at", "original_window_ends_at"],
                "properties": {
                    "task_id": {"type": "string"},
                    "vessel_id": {"type": "string"},
                    "impact_type": {"enum": ["window_changed", "infeasible"]},
                    "reason": {"enum": ["tide_window_changed", "tide_window_too_short", "missing_tide_window",
                                        "no_window_meets_task_bounds", "effective_range_excludes_window"]},
                    "original_window_starts_at": _TIME,
                    "original_window_ends_at": _TIME,
                    "new_window_starts_at": {"oneOf": [_TIME, {"type": "null"}]},
                    "new_window_ends_at": {"oneOf": [_TIME, {"type": "null"}]},
                },
            },
            "ImpactReport": {
                "type": "object",
                "required": ["id", "plan_id", "base_revision_number", "target_forecast_id", "status", "feasible", "items"],
                "properties": {
                    "id": {"type": "string"},
                    "plan_id": {"type": "string"},
                    "base_revision_number": {"type": "integer"},
                    "base_forecast_id": {"type": "string"},
                    "target_forecast_id": {"type": "string"},
                    "status": {"enum": ["open", "adopted", "dismissed"]},
                    "feasible": {"type": "boolean"},
                    "items": {"type": "array", "items": {"$ref": "#/components/schemas/ImpactItem"}},
                },
            },
            "AuditEvent": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "event_type": {"type": "string"},
                    "actor": {"type": "string"},
                    "entity_type": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "details": {"type": "object"},
                    "occurred_at": _TIME,
                    "previous_hash": {"type": "string"},
                    "entry_hash": {"type": "string"},
                },
        },
        },
    },
    "paths": {
        "/api/v1/vessels": {"post": {"summary": "Register a vessel", "requestBody": _request("Vessel"), "responses": {"201": {"description": "Created", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Vessel"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/forecasts": {"post": {"summary": "Create a complete draft forecast version", "requestBody": _request("ForecastCreate"), "responses": {"201": {"description": "Draft created", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Forecast"}}}}, "200": {"description": "Identical content already existed", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Forecast"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/forecasts/{forecastId}": {"get": {"summary": "Get forecast version, effective range, status and windows", "parameters": [{"name": "forecastId", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Forecast", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Forecast"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/forecasts/{forecastId}/publish": {"post": {"summary": "Atomically publish a complete draft; repeated calls are idempotent", "parameters": [{"name": "forecastId", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Published or already published", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Forecast"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/plans:solve": {"post": {"summary": "Solve and lock revision 1 against an explicit published forecast", "requestBody": _request("PlanSolve"), "responses": {"201": {"description": "Locked revision", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PlanRevision"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/plans/{planId}": {"get": {"summary": "Get current locked plan revision", "parameters": [{"name": "planId", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Current revision", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PlanRevision"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/plans/{planId}/revisions/{revisionNumber}:replay": {"get": {"summary": "Deterministically replay a historical input/forecast snapshot", "parameters": [{"name": "planId", "in": "path", "required": True, "schema": {"type": "string"}}, {"name": "revisionNumber", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Replay matched stored hash and result"}, **_COMMON_ERRORS}}},
        "/api/v1/plans/{planId}/impact-reports": {"post": {"summary": "Create an idempotent reviewable impact report without changing a locked plan", "parameters": [{"name": "planId", "in": "path", "required": True, "schema": {"type": "string"}}], "requestBody": _request("ImpactCreate"), "responses": {"201": {"description": "New report", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ImpactReport"}}}}, "200": {"description": "Existing identical report", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ImpactReport"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/impact-reports/{reportId}": {"get": {"summary": "Get impact report", "parameters": [{"name": "reportId", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Report", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ImpactReport"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/impact-reports/{reportId}:adopt": {"post": {"summary": "Explicitly adopt a feasible open report and lock the next revision", "parameters": [{"name": "reportId", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"201": {"description": "New locked revision", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PlanRevision"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/impact-reports/{reportId}:dismiss": {"post": {"summary": "Dismiss an open impact report", "parameters": [{"name": "reportId", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Dismissed report", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ImpactReport"}}}}, **_COMMON_ERRORS}}},
        "/api/v1/audit-events": {"get": {"summary": "List append-only hash-chained audit events", "responses": {"200": {"description": "Audit events", "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/AuditEvent"}}}}}}}},
    },
}
