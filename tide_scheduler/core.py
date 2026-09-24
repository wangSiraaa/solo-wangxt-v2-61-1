from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import transaction


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_time(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise ServiceError(422, "invalid_time", f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ServiceError(422, "invalid_time", f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ServiceError(422, "missing_timezone", f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def time_str(value: str | datetime, field: str = "time") -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = parse_time(value, field)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def utc_datetime_str(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ServiceError(422, "invalid_request", f"{name} must be an object")
    return value


def require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(422, "invalid_field", f"{field} is required")
    return value.strip()


def require_positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ServiceError(422, "invalid_field", f"{field} must be a positive integer")
    return value


def require_field(value: Any, field: str) -> Any:
    if value is None:
        raise ServiceError(422, "invalid_field", f"{field} is required")
    return value


def row_by_id(conn: sqlite3.Connection, table: str, id_: str) -> dict[str, Any] | None:
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (id_,)).fetchone()


def get_forecast_or_404(conn: sqlite3.Connection, forecast_id: str) -> dict[str, Any]:
    row = row_by_id(conn, "forecast_versions", forecast_id)
    if row is None:
        raise ServiceError(404, "forecast_not_found", f"forecast {forecast_id} does not exist")
    return row


def normalize_windows(payload: Any, vessel_ids: set[str], effective_from: datetime, effective_to: datetime) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise ServiceError(422, "invalid_windows", "windows must be a non-empty array")
    windows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(payload):
        item = require_object(item, f"windows[{index}]")
        vessel_id = require_str(item.get("vessel_id"), f"windows[{index}].vessel_id")
        if vessel_id not in vessel_ids:
            raise ServiceError(422, "unknown_vessel", f"vessel {vessel_id} is not registered")
        start = parse_time(require_field(item.get("starts_at"), f"windows[{index}].starts_at"), f"windows[{index}].starts_at")
        end = parse_time(require_field(item.get("ends_at"), f"windows[{index}].ends_at"), f"windows[{index}].ends_at")
        if end <= start:
            raise ServiceError(422, "invalid_window", "window ends_at must be after starts_at")
        if start < effective_from or end > effective_to:
            raise ServiceError(422, "window_outside_effective_range", "all windows must lie in forecast effective range")
        key = (vessel_id, start.isoformat(), end.isoformat())
        if key in seen:
            raise ServiceError(422, "duplicate_window", "duplicate tide window supplied")
        seen.add(key)
        windows.append({
            "vessel_id": vessel_id,
            "starts_at": utc_datetime_str(start),
            "ends_at": utc_datetime_str(end),
        })
    windows.sort(key=lambda w: (w["vessel_id"], w["starts_at"], w["ends_at"]))
    return windows


def ensure_forecast_complete(conn: sqlite3.Connection, forecast_id: str) -> None:
    forecast = get_forecast_or_404(conn, forecast_id)
    expected_vessels = {row["id"] for row in conn.execute("SELECT id FROM vessels")}
    covered_vessels = {
        row["vessel_id"] for row in conn.execute(
            "SELECT DISTINCT vessel_id FROM forecast_windows WHERE forecast_id = ?", (forecast_id,)
        )
    }
    missing = expected_vessels - covered_vessels
    if missing:
        raise ServiceError(409, "incomplete_forecast", "cannot publish a forecast missing vessel tide windows")



def forecast_payload_hash(effective_from: str, effective_to: str, windows: list[dict[str, Any]]) -> str:
    hash_windows = [
        {"vessel_id": window["vessel_id"], "starts_at": window["starts_at"], "ends_at": window["ends_at"]}
        for window in windows
    ]
    return content_hash({
        "effective_from": effective_from,
        "effective_to": effective_to,
        "windows": hash_windows,
    })


def load_windows(conn: sqlite3.Connection, forecast_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT vessel_id, starts_at, ends_at, ordinal
        FROM forecast_windows
        WHERE forecast_id = ?
        ORDER BY vessel_id, ordinal
        """,
        (forecast_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def forecast_snapshot(conn: sqlite3.Connection, forecast: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": forecast["id"],
        "effective_from": forecast["effective_from"],
        "effective_to": forecast["effective_to"],
        "status": forecast["status"],
        "content_hash": forecast["content_hash"],
        "windows": load_windows(conn, forecast["id"]),
    }


def append_audit(
    conn: sqlite3.Connection,
    event_type: str,
    actor: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    previous = conn.execute("SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
    previous_hash = previous["entry_hash"] if previous else "GENESIS"
    occurred_at = utc_now()
    entry_payload = {
        "event_type": event_type,
        "actor": actor,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "details": details,
        "occurred_at": occurred_at,
        "previous_hash": previous_hash,
    }
    entry_hash = sha256_text(canonical_json(entry_payload))
    cur = conn.execute(
        """
        INSERT INTO audit_events
            (event_type, actor, entity_type, entity_id, details_json, occurred_at, previous_hash, entry_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_type, actor, entity_type, entity_id, canonical_json(details), occurred_at, previous_hash, entry_hash),
    )
    return {"id": cur.lastrowid, "entry_hash": entry_hash, "occurred_at": occurred_at}


def create_vessel(conn: sqlite3.Connection, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    vessel_id = require_str(payload.get("id"), "id")
    name = require_str(payload.get("name"), "name")
    with transaction(conn):
        try:
            conn.execute(
                "INSERT INTO vessels (id, name, created_at) VALUES (?, ?, ?)",
                (vessel_id, name, utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(409, "vessel_exists", "vessel id or name already exists") from exc
        append_audit(conn, "vessel.created", actor, "vessel", vessel_id, {"name": name})
    return {"id": vessel_id, "name": name}


def create_forecast(conn: sqlite3.Connection, payload: dict[str, Any], actor: str) -> tuple[dict[str, Any], bool]:
    forecast_id = require_str(payload.get("id"), "id")
    effective_from_dt = parse_time(require_field(payload.get("effective_from"), "effective_from"), "effective_from")
    effective_to_dt = parse_time(require_field(payload.get("effective_to"), "effective_to"), "effective_to")
    if effective_to_dt <= effective_from_dt:
        raise ServiceError(422, "invalid_effective_range", "effective_to must be after effective_from")
    effective_from = utc_datetime_str(effective_from_dt)
    effective_to = utc_datetime_str(effective_to_dt)

    vessel_rows = conn.execute("SELECT id FROM vessels").fetchall()
    vessel_ids = {row["id"] for row in vessel_rows}
    windows = normalize_windows(payload.get("windows"), vessel_ids, effective_from_dt, effective_to_dt)
    hash_value = forecast_payload_hash(effective_from, effective_to, windows)

    existing = conn.execute(
        "SELECT * FROM forecast_versions WHERE content_hash = ?", (hash_value,)
    ).fetchone()
    if existing:
        return existing, False

    with transaction(conn):
        try:
            conn.execute(
                """
                INSERT INTO forecast_versions
                    (id, effective_from, effective_to, status, content_hash, created_at, published_at)
                VALUES (?, ?, ?, 'draft', ?, ?, NULL)
                """,
                (forecast_id, effective_from, effective_to, hash_value, utc_now()),
            )
            ordinal_by_vessel: dict[str, int] = {}
            for window in windows:
                ordinal = ordinal_by_vessel.get(window["vessel_id"], 0)
                ordinal_by_vessel[window["vessel_id"]] = ordinal + 1
                conn.execute(
                    """
                    INSERT INTO forecast_windows
                        (forecast_id, vessel_id, starts_at, ends_at, ordinal)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (forecast_id, window["vessel_id"], window["starts_at"], window["ends_at"], ordinal),
                )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(409, "forecast_exists", "forecast id or identical content already exists") from exc
        append_audit(conn, "forecast.created", actor, "forecast", forecast_id, {
            "status": "draft",
            "content_hash": hash_value,
            "window_count": len(windows),
        })
    return get_forecast_or_404(conn, forecast_id), True


def publish_forecast(conn: sqlite3.Connection, forecast_id: str, actor: str) -> tuple[dict[str, Any], bool]:
    with transaction(conn):
        forecast = get_forecast_or_404(conn, forecast_id)
        if forecast["status"] == "published":
            return forecast, False
        if forecast["status"] != "draft":
            raise ServiceError(409, "forecast_not_draft", "only draft forecasts can be published")
        ensure_forecast_complete(conn, forecast_id)
        published_at = utc_now()
        conn.execute(
            "UPDATE forecast_versions SET status = 'published', published_at = ? WHERE id = ?",
            (published_at, forecast_id),
        )
        append_audit(conn, "forecast.published", actor, "forecast", forecast_id, {
            "content_hash": forecast["content_hash"],
            "published_at": published_at,
        })
    return get_forecast_or_404(conn, forecast_id), True


def get_published_forecast(conn: sqlite3.Connection, forecast_id: str) -> dict[str, Any]:
    forecast = get_forecast_or_404(conn, forecast_id)
    if forecast["status"] != "published":
        raise ServiceError(409, "forecast_not_published", "solving requires a complete published forecast version")
    return forecast


def normalize_tasks(payload: Any, known_vessels: set[str]) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise ServiceError(422, "invalid_tasks", "tasks must be a non-empty array")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        item = require_object(item, f"tasks[{index}]")
        task_id = require_str(item.get("id"), f"tasks[{index}].id")
        vessel_id = require_str(item.get("vessel_id"), f"tasks[{index}].vessel_id")
        if task_id in seen:
            raise ServiceError(422, "duplicate_task", f"duplicate task id {task_id}")
        if vessel_id not in known_vessels:
            raise ServiceError(422, "unknown_vessel", f"vessel {vessel_id} is not registered")
        duration = require_positive_int(item.get("duration_minutes"), f"tasks[{index}].duration_minutes")
        earliest = item.get("earliest_start")
        latest = item.get("latest_start")
        earliest_dt = parse_time(earliest, f"tasks[{index}].earliest_start") if earliest is not None else None
        latest_dt = parse_time(latest, f"tasks[{index}].latest_start") if latest is not None else None
        if earliest_dt and latest_dt and latest_dt < earliest_dt:
            raise ServiceError(422, "invalid_task_bounds", "latest_start cannot precede earliest_start")
        seen.add(task_id)
        tasks.append({
            "id": task_id,
            "vessel_id": vessel_id,
            "duration_minutes": duration,
            "earliest_start": utc_datetime_str(earliest_dt) if earliest_dt else None,
            "latest_start": utc_datetime_str(latest_dt) if latest_dt else None,
        })
    tasks.sort(key=lambda task: task["id"])
    return tasks


def solve(
    tasks: list[dict[str, Any]],
    forecast: dict[str, Any],
    windows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministically place every task at the earliest feasible tide window."""
    effective_from = parse_time(forecast["effective_from"], "forecast.effective_from")
    effective_to = parse_time(forecast["effective_to"], "forecast.effective_to")
    windows_by_vessel: dict[str, list[dict[str, Any]]] = {}
    for window in sorted(windows, key=lambda w: (w["vessel_id"], w["ordinal"], w["starts_at"])):
        windows_by_vessel.setdefault(window["vessel_id"], []).append(window)

    assignments: list[dict[str, Any]] = []
    for task in tasks:
        duration = task["duration_minutes"]
        earliest = parse_time(task["earliest_start"], "task.earliest_start") if task.get("earliest_start") else effective_from
        latest = parse_time(task["latest_start"], "task.latest_start") if task.get("latest_start") else effective_to
        if latest < earliest:
            raise ServiceError(422, "infeasible_task", f"task {task['id']} has invalid time bounds")
        candidates = windows_by_vessel.get(task["vessel_id"], [])
        if not candidates:
            raise ServiceError(422, "infeasible_plan", f"task {task['id']} has no tide windows")
        chosen: dict[str, Any] | None = None
        saw_short_window = False
        for window in candidates:
            window_start = parse_time(window["starts_at"], "window.starts_at")
            window_end = parse_time(window["ends_at"], "window.ends_at")
            if window_start < effective_from or window_end > effective_to:
                continue
            start = max(earliest, window_start)
            end = start + timedelta(minutes=duration)
            if end <= window_end and start <= latest and end <= effective_to:
                chosen = {
                    "task_id": task["id"],
                    "vessel_id": task["vessel_id"],
                    "window_ordinal": window["ordinal"],
                    "starts_at": utc_datetime_str(start),
                    "ends_at": utc_datetime_str(end),
                }
                break
            if (window_end - window_start).total_seconds() / 60 < duration:
                saw_short_window = True
        if chosen is None:
            if saw_short_window:
                reason = "tide_window_too_short"
            else:
                reason = "no_window_meets_task_bounds"
            raise ServiceError(422, "infeasible_plan", f"task {task['id']} is infeasible: {reason}")
        assignments.append(chosen)

    assignments.sort(key=lambda assignment: assignment["task_id"])
    return {
        "forecast_version_id": forecast["id"],
        "forecast_content_hash": forecast["content_hash"],
        "assignments": assignments,
    }


def input_snapshot(tasks: list[dict[str, Any]], forecast: dict[str, Any]) -> dict[str, Any]:
    return {
        "tasks": tasks,
        "forecast_version_id": forecast["id"],
    }


def fetch_revision(conn: sqlite3.Connection, plan_id: str, revision_number: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM plan_revisions WHERE plan_id = ? AND revision_number = ?",
        (plan_id, revision_number),
    ).fetchone()
    if row is None:
        raise ServiceError(404, "plan_revision_not_found", "plan revision does not exist")
    return row


def insert_revision(
    conn: sqlite3.Connection,
    plan_id: str,
    revision_number: int,
    forecast: dict[str, Any],
    snapshot: dict[str, Any],
    forecast_snap: dict[str, Any],
    result: dict[str, Any],
    adopted_report_id: str | None = None,
) -> None:
    replay_payload = {"input": snapshot, "result": result}
    conn.execute(
        """
        INSERT INTO plan_revisions (
            plan_id, revision_number, forecast_id, status, input_snapshot_json,
            forecast_snapshot_json, result_json, replay_hash, created_at,
            superseded_at, adopted_report_id
        ) VALUES (?, ?, ?, 'locked', ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            plan_id,
            revision_number,
            forecast["id"],
            canonical_json(snapshot),
            canonical_json(forecast_snap),
            canonical_json(result),
            content_hash(replay_payload),
            utc_now(),
            adopted_report_id,
        ),
    )
    for assignment in result["assignments"]:
        conn.execute(
            """
            INSERT INTO plan_assignments
                (plan_id, revision_number, task_id, vessel_id, window_ordinal, starts_at, ends_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                revision_number,
                assignment["task_id"],
                assignment["vessel_id"],
                assignment["window_ordinal"],
                assignment["starts_at"],
                assignment["ends_at"],
            ),
        )


def revision_response(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    assignments = conn.execute(
        """
        SELECT task_id, vessel_id, window_ordinal, starts_at, ends_at
        FROM plan_assignments
        WHERE plan_id = ? AND revision_number = ?
        ORDER BY task_id
        """,
        (row["plan_id"], row["revision_number"]),
    ).fetchall()
    return {
        "plan_id": row["plan_id"],
        "revision_number": row["revision_number"],
        "status": row["status"],
        "forecast_version_id": row["forecast_id"],
        "input_snapshot": json.loads(row["input_snapshot_json"]),
        "forecast_snapshot": json.loads(row["forecast_snapshot_json"]),
        "result": json.loads(row["result_json"]),
        "assignments": [dict(row_) for row_ in assignments],
        "replay_hash": row["replay_hash"],
        "created_at": row["created_at"],
        "superseded_at": row["superseded_at"],
        "adopted_report_id": row["adopted_report_id"],
    }


def solve_plan(conn: sqlite3.Connection, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    body = require_object(payload, "body")
    forecast_id = require_str(body.get("forecast_version_id"), "forecast_version_id")
    plan_id = body.get("plan_id") or new_id("plan")
    plan_id = require_str(plan_id, "plan_id")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone():
            raise ServiceError(409, "plan_exists", "plan already exists; use the explicit revision flow")
        forecast = get_published_forecast(conn, forecast_id)
        vessels = {row["id"] for row in conn.execute("SELECT id FROM vessels")}
        tasks = normalize_tasks(require_field(body.get("tasks"), "tasks"), vessels)
        windows = load_windows(conn, forecast_id)
        if forecast_payload_hash(forecast["effective_from"], forecast["effective_to"], windows) != forecast["content_hash"]:
            raise ServiceError(500, "forecast_corrupt", "stored forecast fails content verification")
        result = solve(tasks, forecast, windows)
        snapshot = input_snapshot(tasks, forecast)
        forecast_snap = forecast_snapshot(conn, forecast)
        insert_revision(conn, plan_id, 1, forecast, snapshot, forecast_snap, result)
        conn.execute(
            "INSERT INTO plans (id, current_revision_number, created_at) VALUES (?, 1, ?)",
            (plan_id, utc_now()),
        )
        append_audit(conn, "plan.locked", actor, "plan", plan_id, {
            "revision_number": 1,
            "forecast_version_id": forecast_id,
            "forecast_content_hash": forecast["content_hash"],
            "replay_hash": content_hash({"input": snapshot, "result": result}),
        })
        return revision_response(conn, fetch_revision(conn, plan_id, 1))


def get_plan_current(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    plan = row_by_id(conn, "plans", plan_id)
    if plan is None:
        raise ServiceError(404, "plan_not_found", "plan does not exist")
    return fetch_revision(conn, plan_id, plan["current_revision_number"])


def report_response(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    items = conn.execute(
        """
        SELECT task_id, vessel_id, impact_type, reason,
               original_window_starts_at, original_window_ends_at,
               new_window_starts_at, new_window_ends_at
        FROM impact_report_items
        WHERE report_id = ?
        ORDER BY ordinal, task_id
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "plan_id": row["plan_id"],
        "base_revision_number": row["base_revision_number"],
        "base_forecast_id": row["base_forecast_id"],
        "target_forecast_id": row["target_forecast_id"],
        "status": row["status"],
        "feasible": bool(row["feasible"]),
        "items": [dict(item) for item in items],
        "created_at": row["created_at"],
        "adopted_at": row["adopted_at"],
        "adopted_revision_number": row["adopted_revision_number"],
    }


def _window_interval(assignment_window: dict[str, Any] | None) -> dict[str, str] | None:
    if assignment_window is None:
        return None
    return {
        "starts_at": assignment_window["starts_at"],
        "ends_at": assignment_window["ends_at"],
    }


def evaluate_impact(
    tasks: list[dict[str, Any]],
    base_forecast: dict[str, Any],
    base_windows: list[dict[str, Any]],
    base_result: dict[str, Any],
    target_forecast: dict[str, Any],
    target_windows: list[dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]]]:
    base_assignments = {item["task_id"]: item for item in base_result["assignments"]}
    target_windows_by_vessel: dict[str, list[dict[str, Any]]] = {}
    for window in target_windows:
        target_windows_by_vessel.setdefault(window["vessel_id"], []).append(window)

    target_effective_from = parse_time(target_forecast["effective_from"], "target.effective_from")
    target_effective_to = parse_time(target_forecast["effective_to"], "target.effective_to")
    base_by_ordinal = {
        (window["vessel_id"], window["ordinal"]): window
        for window in base_windows
    }
    target_by_ordinal = {
        (window["vessel_id"], window["ordinal"]): window
        for window in target_windows
    }

    items: list[dict[str, Any]] = []
    feasible = True
    for task in sorted(tasks, key=lambda item: item["id"]):
        assignment = base_assignments[task["id"]]
        vessel_id = task["vessel_id"]
        base_window = base_by_ordinal[(vessel_id, assignment["window_ordinal"])]
        original_interval = {
            "starts_at": base_window["starts_at"],
            "ends_at": base_window["ends_at"],
        }
        assignment_start = parse_time(assignment["starts_at"], "assignment.starts_at")
        assignment_end = parse_time(assignment["ends_at"], "assignment.ends_at")
        if assignment_start < target_effective_from or assignment_end > target_effective_to:
            feasible = False
            items.append({
                "task_id": task["id"],
                "vessel_id": vessel_id,
                "impact_type": "infeasible",
                "reason": "effective_range_excludes_window",
                "original_window": original_interval,
                "new_window": None,
            })
            continue

        earliest = parse_time(task["earliest_start"], "task.earliest_start") if task.get("earliest_start") else target_effective_from
        latest = parse_time(task["latest_start"], "task.latest_start") if task.get("latest_start") else target_effective_to
        try:
            target_result = solve([task], target_forecast, target_windows)
            new_assignment = target_result["assignments"][0]
            new_window = target_by_ordinal[(vessel_id, new_assignment["window_ordinal"])]
            if (
                new_window["starts_at"] != base_window["starts_at"]
                or new_window["ends_at"] != base_window["ends_at"]
                or new_assignment["starts_at"] != assignment["starts_at"]
                or new_assignment["ends_at"] != assignment["ends_at"]
            ):
                items.append({
                    "task_id": task["id"],
                    "vessel_id": vessel_id,
                    "impact_type": "window_changed",
                    "reason": "tide_window_changed",
                    "original_window": original_interval,
                    "new_window": {
                        "starts_at": new_window["starts_at"],
                        "ends_at": new_window["ends_at"],
                    },
                    "new_assignment": {
                        "starts_at": new_assignment["starts_at"],
                        "ends_at": new_assignment["ends_at"],
                    },
                })
        except ServiceError:
            feasible = False
            candidates = target_windows_by_vessel.get(vessel_id, [])
            relevant_window: dict[str, Any] | None = None
            if not candidates:
                reason = "missing_tide_window"
            else:
                for candidate in sorted(candidates, key=lambda w: (w["ordinal"], w["starts_at"])):
                    candidate_start = parse_time(candidate["starts_at"], "window.starts_at")
                    candidate_end = parse_time(candidate["ends_at"], "window.ends_at")
                    available_start = max(candidate_start, earliest)
                    available_end = min(candidate_end, latest + timedelta(minutes=task["duration_minutes"]))
                    if candidate_start < assignment_end and candidate_end > assignment_start:
                        relevant_window = candidate
                        if (available_end - available_start).total_seconds() / 60 < task["duration_minutes"]:
                            break
                if relevant_window is None:
                    relevant_window = sorted(candidates, key=lambda w: (w["ordinal"], w["starts_at"]))[0]
                available_start = max(
                    parse_time(relevant_window["starts_at"], "window.starts_at"), earliest
                )
                available_end = min(
                    parse_time(relevant_window["ends_at"], "window.ends_at"),
                    latest + timedelta(minutes=task["duration_minutes"]),
                )
                if (available_end - available_start).total_seconds() / 60 < task["duration_minutes"]:
                    reason = "tide_window_too_short"
                else:
                    reason = "no_window_meets_task_bounds"
            items.append({
                "task_id": task["id"],
                "vessel_id": vessel_id,
                "impact_type": "infeasible",
                "reason": reason,
                "original_window": original_interval,
                "new_window": None if relevant_window is None else {
                    "starts_at": relevant_window["starts_at"],
                    "ends_at": relevant_window["ends_at"],
                },
            })
    return feasible, items

def create_impact_report(conn: sqlite3.Connection, plan_id: str, payload: dict[str, Any], actor: str) -> tuple[dict[str, Any], bool]:
    body = require_object(payload, "body")
    target_id = require_str(body.get("target_forecast_version_id"), "target_forecast_version_id")
    with transaction(conn):
        plan = row_by_id(conn, "plans", plan_id)
        if plan is None:
            raise ServiceError(404, "plan_not_found", "plan does not exist")
        base_number = body.get("base_revision_number") or plan["current_revision_number"]
        base_number = require_positive_int(base_number, "base_revision_number")
        base = fetch_revision(conn, plan_id, base_number)
        target = get_published_forecast(conn, target_id)
        if target["id"] == base["forecast_id"]:
            raise ServiceError(422, "same_forecast", "impact target must differ from the bound forecast")

        duplicate = conn.execute(
            """
            SELECT * FROM impact_reports
            WHERE plan_id = ? AND base_revision_number = ? AND target_forecast_id = ?
            """,
            (plan_id, base_number, target_id),
        ).fetchone()
        if duplicate:
            return report_response(conn, duplicate), False

        base_forecast = get_forecast_or_404(conn, base["forecast_id"])
        base_windows = json.loads(base["forecast_snapshot_json"])["windows"]
        target_windows = load_windows(conn, target_id)
        snapshot = json.loads(base["input_snapshot_json"])
        base_result = json.loads(base["result_json"])
        feasible, items = evaluate_impact(
            snapshot["tasks"], base_forecast, base_windows, base_result, target, target_windows
        )
        report_id = new_id("impact")
        conn.execute(
            """
            INSERT INTO impact_reports (
                id, plan_id, base_revision_number, base_forecast_id, target_forecast_id,
                status, feasible, created_at, adopted_at, adopted_revision_number
            ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, NULL, NULL)
            """,
            (report_id, plan_id, base_number, base["forecast_id"], target_id, 1 if feasible else 0, utc_now()),
        )
        for ordinal, item in enumerate(items):
            new_window = item["new_window"] or {}
            conn.execute(
                """
                INSERT INTO impact_report_items (
                    report_id, task_id, vessel_id, impact_type, reason,
                    original_window_starts_at, original_window_ends_at,
                    new_window_starts_at, new_window_ends_at, ordinal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    item["task_id"],
                    item["vessel_id"],
                    item["impact_type"],
                    item["reason"],
                    item["original_window"]["starts_at"],
                    item["original_window"]["ends_at"],
                    new_window.get("starts_at"),
                    new_window.get("ends_at"),
                    ordinal,
                ),
            )
        append_audit(conn, "impact_report.created", actor, "impact_report", report_id, {
            "plan_id": plan_id,
            "base_revision_number": base_number,
            "target_forecast_version_id": target_id,
            "feasible": feasible,
            "affected_count": len(items),
        })
        return report_response(conn, conn.execute(
            "SELECT * FROM impact_reports WHERE id = ?", (report_id,)
        ).fetchone()), True


def adopt_impact_report(conn: sqlite3.Connection, report_id: str, actor: str) -> dict[str, Any]:
    with transaction(conn):
        report = row_by_id(conn, "impact_reports", report_id)
        if report is None:
            raise ServiceError(404, "impact_report_not_found", "impact report does not exist")
        if report["status"] != "open":
            raise ServiceError(409, "report_not_open", "only open reports can be adopted")
        plan = row_by_id(conn, "plans", report["plan_id"])
        if plan["current_revision_number"] != report["base_revision_number"]:
            raise ServiceError(409, "stale_report", "report base revision is no longer current")
        if not report["feasible"]:
            raise ServiceError(422, "infeasible_revision", "infeasible forecasts cannot be adopted without changing plan inputs")
        base = fetch_revision(conn, report["plan_id"], report["base_revision_number"])
        target = get_published_forecast(conn, report["target_forecast_id"])
        tasks = json.loads(base["input_snapshot_json"])["tasks"]
        windows = load_windows(conn, target["id"])
        result = solve(tasks, target, windows)
        next_number = report["base_revision_number"] + 1
        snapshot = input_snapshot(tasks, target)
        forecast_snap = forecast_snapshot(conn, target)
        insert_revision(conn, report["plan_id"], next_number, target, snapshot, forecast_snap, result, report_id)
        superseded_at = utc_now()
        conn.execute(
            "UPDATE plan_revisions SET status = 'superseded', superseded_at = ? WHERE plan_id = ? AND revision_number = ?",
            (superseded_at, report["plan_id"], report["base_revision_number"]),
        )
        conn.execute(
            "UPDATE plans SET current_revision_number = ? WHERE id = ?",
            (next_number, report["plan_id"]),
        )
        conn.execute(
            """
            UPDATE impact_reports
            SET status = 'adopted', adopted_at = ?, adopted_revision_number = ?
            WHERE id = ?
            """,
            (superseded_at, next_number, report_id),
        )
        conn.execute(
            "UPDATE impact_reports SET status = 'dismissed' WHERE plan_id = ? AND id != ? AND status = 'open'",
            (report["plan_id"], report_id),
        )
        append_audit(conn, "plan.revision_adopted", actor, "plan", report["plan_id"], {
            "report_id": report_id,
            "revision_number": next_number,
            "forecast_version_id": target["id"],
            "forecast_content_hash": target["content_hash"],
        })
        return revision_response(conn, fetch_revision(conn, report["plan_id"], next_number))


def dismiss_impact_report(conn: sqlite3.Connection, report_id: str, actor: str) -> dict[str, Any]:
    with transaction(conn):
        report = row_by_id(conn, "impact_reports", report_id)
        if report is None:
            raise ServiceError(404, "impact_report_not_found", "impact report does not exist")
        if report["status"] != "open":
            return report_response(conn, report)
        conn.execute("UPDATE impact_reports SET status = 'dismissed' WHERE id = ?", (report_id,))
        append_audit(conn, "impact_report.dismissed", actor, "impact_report", report_id, {
            "plan_id": report["plan_id"],
            "target_forecast_version_id": report["target_forecast_id"],
        })
        return report_response(conn, row_by_id(conn, "impact_reports", report_id))


def replay_revision(conn: sqlite3.Connection, plan_id: str, revision_number: int) -> dict[str, Any]:
    revision = fetch_revision(conn, plan_id, revision_number)
    forecast_snap = json.loads(revision["forecast_snapshot_json"])
    forecast = {
        "id": forecast_snap["id"],
        "effective_from": forecast_snap["effective_from"],
        "effective_to": forecast_snap["effective_to"],
        "content_hash": forecast_snap["content_hash"],
    }
    tasks = json.loads(revision["input_snapshot_json"])["tasks"]
    result = solve(tasks, forecast, forecast_snap["windows"])
    expected = json.loads(revision["result_json"])
    replayed_hash = content_hash({"input": json.loads(revision["input_snapshot_json"]), "result": result})
    if result != expected or replayed_hash != revision["replay_hash"]:
        raise ServiceError(409, "replay_mismatch", "historical snapshot does not replay to the locked result")
    return {
        "plan_id": plan_id,
        "revision_number": revision_number,
        "replay_hash": replayed_hash,
        "matches": True,
        "result": result,
    }
