from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS vessels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_versions (
    id TEXT PRIMARY KEY,
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'published', 'superseded')),
    content_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS forecast_windows (
    forecast_id TEXT NOT NULL REFERENCES forecast_versions(id) ON DELETE CASCADE,
    vessel_id TEXT NOT NULL REFERENCES vessels(id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (forecast_id, vessel_id, ordinal)
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    current_revision_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (id, current_revision_number) REFERENCES plan_revisions(plan_id, revision_number) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS plan_revisions (
    plan_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    forecast_id TEXT NOT NULL REFERENCES forecast_versions(id),
    status TEXT NOT NULL CHECK (status IN ('locked', 'superseded')),
    input_snapshot_json TEXT NOT NULL,
    forecast_snapshot_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    replay_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    adopted_report_id TEXT,
    PRIMARY KEY (plan_id, revision_number),
    FOREIGN KEY (plan_id) REFERENCES plans(id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (adopted_report_id) REFERENCES impact_reports(id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS plan_assignments (
    plan_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    vessel_id TEXT NOT NULL,
    window_ordinal INTEGER NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, revision_number, task_id),
    FOREIGN KEY (plan_id, revision_number) REFERENCES plan_revisions(plan_id, revision_number) ON DELETE CASCADE,
    FOREIGN KEY (vessel_id) REFERENCES vessels(id)
);

CREATE TABLE IF NOT EXISTS impact_reports (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    base_revision_number INTEGER NOT NULL,
    base_forecast_id TEXT NOT NULL REFERENCES forecast_versions(id),
    target_forecast_id TEXT NOT NULL REFERENCES forecast_versions(id),
    status TEXT NOT NULL CHECK (status IN ('open', 'adopted', 'dismissed')),
    feasible INTEGER NOT NULL CHECK (feasible IN (0, 1)),
    created_at TEXT NOT NULL,
    adopted_at TEXT,
    adopted_revision_number INTEGER,
    UNIQUE (plan_id, base_revision_number, target_forecast_id),
    FOREIGN KEY (plan_id, base_revision_number) REFERENCES plan_revisions(plan_id, revision_number),
    FOREIGN KEY (plan_id, adopted_revision_number) REFERENCES plan_revisions(plan_id, revision_number) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS impact_report_items (
    report_id TEXT NOT NULL REFERENCES impact_reports(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    vessel_id TEXT NOT NULL,
    impact_type TEXT NOT NULL CHECK (impact_type IN ('window_changed', 'infeasible')),
    reason TEXT NOT NULL,
    original_window_starts_at TEXT,
    original_window_ends_at TEXT,
    new_window_starts_at TEXT,
    new_window_ends_at TEXT,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (report_id, task_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE
);

CREATE TRIGGER IF NOT EXISTS trg_plan_requires_published_forecast_insert
BEFORE INSERT ON plan_revisions
BEGIN
    SELECT CASE
        WHEN (SELECT status FROM forecast_versions WHERE id = NEW.forecast_id) <> 'published'
        THEN RAISE(ABORT, 'plan revision requires a complete published forecast version')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_plan_revision_immutable_update
BEFORE UPDATE ON plan_revisions
BEGIN
    SELECT CASE
        WHEN NEW.plan_id <> OLD.plan_id
          OR NEW.revision_number <> OLD.revision_number
          OR NEW.forecast_id <> OLD.forecast_id
          OR NEW.input_snapshot_json <> OLD.input_snapshot_json
          OR NEW.forecast_snapshot_json <> OLD.forecast_snapshot_json
          OR NEW.result_json <> OLD.result_json
          OR NEW.replay_hash <> OLD.replay_hash
          OR NEW.created_at <> OLD.created_at
          OR NEW.adopted_report_id IS NOT OLD.adopted_report_id
          OR OLD.status NOT IN ('locked', 'superseded')
          OR (OLD.status = 'superseded' AND NEW.status <> 'superseded')
          OR (OLD.status = 'locked' AND NEW.status NOT IN ('locked', 'superseded'))
        THEN RAISE(ABORT, 'locked plan revision inputs and results are immutable')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_plan_revision_no_delete
BEFORE DELETE ON plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'plan revisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_plan_assignment_no_update
BEFORE UPDATE ON plan_assignments
BEGIN
    SELECT RAISE(ABORT, 'locked assignments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_plan_assignment_no_delete
BEFORE DELETE ON plan_assignments
BEGIN
    SELECT RAISE(ABORT, 'locked assignments cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_forecast_window_no_update
BEFORE UPDATE ON forecast_windows
BEGIN
    SELECT RAISE(ABORT, 'forecast windows are immutable after persistence');
END;

CREATE TRIGGER IF NOT EXISTS trg_forecast_window_no_delete
BEFORE DELETE ON forecast_windows
BEGIN
    SELECT RAISE(ABORT, 'forecast windows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_forecast_version_status_transition
BEFORE UPDATE ON forecast_versions
BEGIN
    SELECT CASE
        WHEN NEW.id <> OLD.id
          OR NEW.effective_from <> OLD.effective_from
          OR NEW.effective_to <> OLD.effective_to
          OR NEW.content_hash <> OLD.content_hash
          OR NEW.created_at <> OLD.created_at
          OR (OLD.status = 'draft' AND NEW.status NOT IN ('draft', 'published'))
          OR (OLD.status = 'published' AND NEW.status NOT IN ('published', 'superseded'))
          OR (OLD.status = 'superseded' AND NEW.status <> 'superseded')
        THEN RAISE(ABORT, 'forecast content is immutable and status transitions are controlled')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_forecast_version_no_delete
BEFORE DELETE ON forecast_versions
BEGIN
    SELECT RAISE(ABORT, 'forecast versions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events cannot be deleted');
END;

CREATE INDEX IF NOT EXISTS idx_windows_vessel_time
    ON forecast_windows(vessel_id, starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_reports_plan ON impact_reports(plan_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id);
"""


def dict_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = dict_factory
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize SQLite writers and never leave partial writes visible."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
