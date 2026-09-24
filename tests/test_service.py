from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tide_scheduler import core
from tide_scheduler.api import OPENAPI, build_server
from tide_scheduler.db import connect, initialize


VESSEL_ALPHA = {"id": "v_alpha", "name": "Alpha"}
VESSEL_BETA = {"id": "v_beta", "name": "Beta"}

# A deliberately cross-midnight planning horizon/window.
EFFECTIVE_A = "2026-01-01T00:00:00Z"
EFFECTIVE_B = "2026-01-02T12:00:00Z"
ORIGINAL_WINDOW = {
    "vessel_id": "v_alpha",
    "starts_at": "2026-01-01T22:00:00Z",
    "ends_at": "2026-01-02T02:00:00Z",
}
NARROW_WINDOW = {
    "vessel_id": "v_alpha",
    "starts_at": "2026-01-01T22:30:00Z",
    "ends_at": "2026-01-02T00:30:00Z",
}
BETA_WINDOW = {
    "vessel_id": "v_beta",
    "starts_at": "2026-01-01T00:00:00Z",
    "ends_at": "2026-01-01T04:00:00Z",
}
BETA_CHANGED_WINDOW = {
    "vessel_id": "v_beta",
    "starts_at": "2026-01-01T01:00:00Z",
    "ends_at": "2026-01-01T05:00:00Z",
}
ALPHA_TASK = {
    "id": "task_alpha",
    "vessel_id": "v_alpha",
    "duration_minutes": 120,
    "earliest_start": "2026-01-01T23:00:00Z",
    "latest_start": "2026-01-02T01:00:00Z",
}


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "scheduler.sqlite3")
        initialize(self.db_path)
        self.conn = connect(self.db_path)
        core.create_vessel(self.conn, VESSEL_ALPHA, "setup")
        core.create_vessel(self.conn, VESSEL_BETA, "setup")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def forecast(self, forecast_id: str, windows, status: str = "published"):
        forecast, _ = core.create_forecast(self.conn, {
            "id": forecast_id,
            "effective_from": EFFECTIVE_A,
            "effective_to": EFFECTIVE_B,
            "windows": list(windows),
        }, "test")
        if status == "published":
            forecast, _ = core.publish_forecast(self.conn, forecast_id, "test")
        return forecast

    def solve_alpha(self, forecast_id="f_v1", plan_id="plan_alpha"):
        return core.solve_plan(self.conn, {
            "plan_id": plan_id,
            "forecast_version_id": forecast_id,
            "tasks": [ALPHA_TASK],
        }, "planner")

    def audit(self):
        return self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()

    def test_explicit_version_solves_cross_midnight_window_and_snapshot_replays(self):
        self.forecast("f_v1", [ORIGINAL_WINDOW, BETA_WINDOW])
        revision = self.solve_alpha()

        self.assertEqual(revision["revision_number"], 1)
        self.assertEqual(revision["status"], "locked")
        self.assertEqual(revision["forecast_version_id"], "f_v1")
        self.assertEqual(revision["assignments"], [{
            "task_id": "task_alpha",
            "vessel_id": "v_alpha",
            "window_ordinal": 0,
            "starts_at": "2026-01-01T23:00Z",
            "ends_at": "2026-01-02T01:00Z",
        }])
        self.assertEqual(
            revision["forecast_snapshot"]["windows"][0],
            {**ORIGINAL_WINDOW,
             "starts_at": "2026-01-01T22:00Z",
             "ends_at": "2026-01-02T02:00Z",
             "ordinal": 0},
        )

        replay = core.replay_revision(self.conn, "plan_alpha", 1)
        self.assertTrue(replay["matches"])
        self.assertEqual(replay["result"], revision["result"])
        self.assertEqual(replay["replay_hash"], revision["replay_hash"])

    def test_draft_or_omitted_forecast_cannot_solve(self):
        self.forecast("f_draft", [ORIGINAL_WINDOW], status="draft")
        with self.assertRaises(core.ServiceError) as caught:
            self.solve_alpha(forecast_id="f_draft")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "forecast_not_published")

        partial_window = {**ORIGINAL_WINDOW, "starts_at": "2026-01-01T22:01:00Z"}
        partial = self.forecast("f_partial", [partial_window], status="draft")
        self.assertEqual(partial["status"], "draft")
        with self.assertRaises(core.ServiceError) as incomplete:
            core.publish_forecast(self.conn, "f_partial", "publisher")
        self.assertEqual(incomplete.exception.code, "incomplete_forecast")
        with self.assertRaises(core.ServiceError) as still_draft:
            self.solve_alpha(forecast_id="f_partial")
        self.assertEqual(still_draft.exception.code, "forecast_not_published")

        with self.assertRaises(core.ServiceError) as missing:
            core.solve_plan(self.conn, {"plan_id": "p", "tasks": [ALPHA_TASK]}, "planner")
        self.assertEqual(missing.exception.code, "invalid_field")

    def test_unrelated_revision_leaves_locked_plan_unchanged_until_explicit_adoption(self):
        self.forecast("f_v1", [ORIGINAL_WINDOW, BETA_WINDOW])
        revision = self.solve_alpha()
        self.forecast("f_unrelated", [ORIGINAL_WINDOW, BETA_CHANGED_WINDOW])

        # Publishing alone never rewrites the locked plan.
        current = core.revision_response(self.conn, core.get_plan_current(self.conn, "plan_alpha"))
        self.assertEqual(current["revision_number"], 1)
        self.assertEqual(current["forecast_version_id"], "f_v1")

        report, created = core.create_impact_report(self.conn, "plan_alpha", {
            "target_forecast_version_id": "f_unrelated"
        }, "reviewer")
        self.assertTrue(created)
        self.assertTrue(report["feasible"])
        self.assertEqual(report["items"], [])

        current = core.revision_response(self.conn, core.get_plan_current(self.conn, "plan_alpha"))
        self.assertEqual(current["revision_number"], 1)

        revised = core.adopt_impact_report(self.conn, report["id"], "manager")
        self.assertEqual(revised["revision_number"], 2)
        self.assertEqual(revised["forecast_version_id"], "f_unrelated")
        self.assertEqual(revised["assignments"], revision["assignments"])
        old = core.fetch_revision(self.conn, "plan_alpha", 1)
        self.assertEqual(old["status"], "superseded")

    def test_cross_midnight_narrowing_lists_exact_vessel_windows_and_reason(self):
        self.forecast("f_v1", [ORIGINAL_WINDOW, BETA_WINDOW])
        self.solve_alpha()
        self.forecast("f_narrow", [NARROW_WINDOW, BETA_WINDOW])

        report, _ = core.create_impact_report(self.conn, "plan_alpha", {
            "target_forecast_version_id": "f_narrow"
        }, "reviewer")
        self.assertFalse(report["feasible"])
        self.assertEqual(len(report["items"]), 1)
        item = report["items"][0]
        self.assertEqual(item, {
            "task_id": "task_alpha",
            "vessel_id": "v_alpha",
            "impact_type": "infeasible",
            "reason": "tide_window_too_short",
            "original_window_starts_at": "2026-01-01T22:00Z",
            "original_window_ends_at": "2026-01-02T02:00Z",
            "new_window_starts_at": "2026-01-01T22:30Z",
            "new_window_ends_at": "2026-01-02T00:30Z",
        })

        # The locked plan is not silently changed, and an infeasible review cannot be adopted.
        self.assertEqual(core.get_plan_current(self.conn, "plan_alpha")["revision_number"], 1)
        with self.assertRaises(core.ServiceError) as caught:
            core.adopt_impact_report(self.conn, report["id"], "manager")
        self.assertEqual(caught.exception.code, "infeasible_revision")
        self.assertEqual(core.get_plan_current(self.conn, "plan_alpha")["revision_number"], 1)

    def test_duplicate_and_out_of_order_publication_do_not_duplicate_reports(self):
        self.forecast("f_v1", [ORIGINAL_WINDOW, BETA_WINDOW])
        self.solve_alpha()

        # Drafts can be inserted in any id/content order and published out of order.
        f3 = self.forecast("f_003", [{**ORIGINAL_WINDOW, "vessel_id": "v_alpha"}, BETA_CHANGED_WINDOW], status="draft")
        f2 = self.forecast("f_002", [NARROW_WINDOW, BETA_WINDOW], status="draft")
        core.publish_forecast(self.conn, "f_003", "publisher")
        report3, created3 = core.create_impact_report(self.conn, "plan_alpha", {
            "target_forecast_version_id": "f_003"
        }, "reviewer")
        self.assertTrue(created3)
        core.publish_forecast(self.conn, "f_002", "publisher")
        report2, created2 = core.create_impact_report(self.conn, "plan_alpha", {
            "target_forecast_version_id": "f_002"
        }, "reviewer")
        self.assertTrue(created2)

        repeat2, repeated2 = core.create_impact_report(self.conn, "plan_alpha", {
            "target_forecast_version_id": "f_002"
        }, "reviewer")
        repeat3, repeated3 = core.create_impact_report(self.conn, "plan_alpha", {
            "target_forecast_version_id": "f_003"
        }, "reviewer")
        self.assertFalse(repeated2)
        self.assertFalse(repeated3)
        self.assertEqual(repeat2["id"], report2["id"])
        self.assertEqual(repeat3["id"], report3["id"])

        rows = self.conn.execute(
            "SELECT target_forecast_id, COUNT(*) AS n FROM impact_reports GROUP BY target_forecast_id"
        ).fetchall()
        self.assertEqual(rows, [
            {"target_forecast_id": "f_002", "n": 1},
            {"target_forecast_id": "f_003", "n": 1},
        ])
        # Repeated publish and identical-content create calls are idempotent, not duplicate revisions.
        _, publish_again = core.publish_forecast(self.conn, "f_002", "publisher")
        self.assertFalse(publish_again)
        duplicate, duplicate_created = core.create_forecast(self.conn, {
            "id": "f_other_id_same_hash",
            "effective_from": EFFECTIVE_A,
            "effective_to": EFFECTIVE_B,
            "windows": [NARROW_WINDOW, BETA_WINDOW],
        }, "publisher")
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate["id"], "f_002")
        self.assertEqual(f2["status"], "draft")
        self.assertEqual(f3["status"], "draft")

    def test_concurrent_publish_and_solve_never_binds_a_partial_version(self):
        self.forecast("f_v1", [ORIGINAL_WINDOW, BETA_WINDOW])

        for i in range(8):
            forecast_id = f"f_concurrent_{i}"
            plan_id = f"plan_concurrent_{i}"
            shifted_beta_window = {
                "vessel_id": "v_beta",
                "starts_at": f"2026-01-01T00:{i + 1:02d}:00Z",
                "ends_at": f"2026-01-01T04:{i + 1:02d}:00Z",
            }
            self.forecast(forecast_id, [ORIGINAL_WINDOW, shifted_beta_window], status="draft")
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def publish():
                conn = connect(self.db_path)
                try:
                    barrier.wait(5)
                    try:
                        core.publish_forecast(conn, forecast_id, "publisher")
                        outcomes.append("published")
                    except Exception as exc:
                        outcomes.append(f"publish-error:{exc}")
                finally:
                    conn.close()

            def solve():
                conn = connect(self.db_path)
                try:
                    barrier.wait(5)
                    try:
                        revision = core.solve_plan(conn, {
                            "plan_id": plan_id,
                            "forecast_version_id": forecast_id,
                            "tasks": [ALPHA_TASK],
                        }, "planner")
                        outcomes.append(f"solved:{revision['forecast_version_id']}")
                    except core.ServiceError as exc:
                        outcomes.append(f"rejected:{exc.code}")
                finally:
                    conn.close()

            threads = [threading.Thread(target=publish), threading.Thread(target=solve)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())

            self.assertIn("published", outcomes)
            solve_outcomes = [value for value in outcomes if value.startswith(("solved:", "rejected:"))]
            self.assertEqual(len(solve_outcomes), 1)
            outcome = solve_outcomes[0]
            forecast = core.get_forecast_or_404(self.conn, forecast_id)
            self.assertEqual(forecast["status"], "published")
            if outcome.startswith("solved:"):
                self.assertEqual(outcome, f"solved:{forecast_id}")
                revision = core.revision_response(
                    self.conn, core.get_plan_current(self.conn, plan_id)
                )
                self.assertEqual(revision["forecast_version_id"], forecast_id)
                self.assertEqual(revision["status"], "locked")
            else:
                self.assertEqual(outcome, "rejected:forecast_not_published")
                self.assertIsNone(self.conn.execute(
                    "SELECT id FROM plans WHERE id = ?", (plan_id,)
                ).fetchone())

    def test_audit_events_are_append_only_and_hash_chained(self):
        self.forecast("f_v1", [ORIGINAL_WINDOW, BETA_WINDOW])
        self.solve_alpha()
        self.forecast("f_narrow", [NARROW_WINDOW, BETA_WINDOW])
        report, _ = core.create_impact_report(self.conn, "plan_alpha", {
            "target_forecast_version_id": "f_narrow"
        }, "reviewer")
        with self.assertRaises(core.ServiceError):
            core.adopt_impact_report(self.conn, report["id"], "manager")

        events = self.audit()
        self.assertGreaterEqual(len(events), 5)
        self.assertEqual(events[0]["previous_hash"], "GENESIS")
        for previous, event in zip(events, events[1:]):
            self.assertEqual(event["previous_hash"], previous["entry_hash"])

        # Audit history is append-only at the service boundary: forecast publication has no update API.
        event_types = [event["event_type"] for event in events]
        self.assertIn("forecast.published", event_types)
        self.assertIn("plan.locked", event_types)
        self.assertIn("impact_report.created", event_types)
        self.assertEqual(event_types.count("impact_report.created"), 1)

    def test_database_forbids_direct_rewrites_of_versions_revisions_and_audit(self):
        self.forecast("f_v1", [ORIGINAL_WINDOW, BETA_WINDOW])
        self.solve_alpha()
        direct_rewrites = [
            ("UPDATE forecast_windows SET ends_at = ? WHERE forecast_id = ?", ("2026-01-02T03:00Z", "f_v1")),
            ("UPDATE plan_revisions SET result_json = ? WHERE plan_id = ?", ('{"tampered":true}', "plan_alpha")),
            ("UPDATE plan_assignments SET ends_at = ? WHERE plan_id = ?", ("2026-01-02T02:00Z", "plan_alpha")),
            ("DELETE FROM audit_events", ()),
        ]
        for sql, params in direct_rewrites:
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql, params)
        self.conn.rollback()
        replay = core.replay_revision(self.conn, "plan_alpha", 1)
        self.assertTrue(replay["matches"])

    def test_restart_replays_historical_snapshot_from_persistent_database(self):
        self.forecast("f_v1", [ORIGINAL_WINDOW, BETA_WINDOW])
        revision = self.solve_alpha()
        self.conn.close()

        with connect(self.db_path) as restarted:
            replay = core.replay_revision(restarted, "plan_alpha", 1)
        self.assertTrue(replay["matches"])
        self.assertEqual(replay["replay_hash"], revision["replay_hash"])

        # Reopen for remaining test fixture teardown.
        self.conn = connect(self.db_path)


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "scheduler.sqlite3")
        self.server = build_server(self.db_path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json", "X-Actor": "api-test"} if data else {}
            conn.request(method, path, data, headers)
            response = conn.getresponse()
            payload = json.loads(response.read().decode())
            return response.status, payload
        finally:
            conn.close()

    def test_api_openapi_publish_solve_impact_and_restart_replay(self):
        status, body = self.request("GET", "/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(body["openapi"], "3.1.0")
        self.assertIn("/api/v1/plans:solve", body["paths"])
        self.assertIn("/api/v1/plans/{planId}/impact-reports", body["paths"])

        status, _ = self.request("POST", "/api/v1/vessels", VESSEL_ALPHA)
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/api/v1/vessels", VESSEL_BETA)
        self.assertEqual(status, 201)

        forecast_body = {
            "id": "f_v1",
            "effective_from": EFFECTIVE_A,
            "effective_to": EFFECTIVE_B,
            "windows": [ORIGINAL_WINDOW, BETA_WINDOW],
        }
        status, forecast = self.request("POST", "/api/v1/forecasts", forecast_body)
        self.assertEqual(status, 201)
        self.assertEqual(forecast["status"], "draft")
        status, forecast = self.request("POST", "/api/v1/forecasts/f_v1/publish", {})
        self.assertEqual(status, 200)
        self.assertEqual(forecast["status"], "published")

        status, revision = self.request("POST", "/api/v1/plans:solve", {
            "plan_id": "plan_api",
            "forecast_version_id": "f_v1",
            "tasks": [ALPHA_TASK],
        })
        self.assertEqual(status, 201)
        self.assertEqual(revision["revision_number"], 1)
        self.assertEqual(revision["forecast_snapshot"]["id"], "f_v1")

        status, forecast = self.request("POST", "/api/v1/forecasts", {
            "id": "f_narrow",
            "effective_from": EFFECTIVE_A,
            "effective_to": EFFECTIVE_B,
            "windows": [NARROW_WINDOW, BETA_WINDOW],
        })
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/api/v1/forecasts/f_narrow/publish", {})
        self.assertEqual(status, 200)

        status, report = self.request("POST", "/api/v1/plans/plan_api/impact-reports", {
            "target_forecast_version_id": "f_narrow"
        })
        self.assertEqual(status, 201)
        self.assertFalse(report["feasible"])
        self.assertEqual(report["items"][0]["reason"], "tide_window_too_short")

        # Restart the HTTP service on the same persistent database and replay version 1.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = build_server(self.db_path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        status, replay = self.request("GET", "/api/v1/plans/plan_api/revisions/1:replay")
        self.assertEqual(status, 200)
        self.assertTrue(replay["matches"])

        status, audit = self.request("GET", "/api/v1/audit-events")
        self.assertEqual(status, 200)
        self.assertIn("plan.locked", [event["event_type"] for event in audit])
        self.assertEqual(OPENAPI["info"]["title"], "Offline Tide-aware Scheduling API")


if __name__ == "__main__":
    unittest.main()
