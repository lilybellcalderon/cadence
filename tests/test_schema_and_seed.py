"""Run:  python -m unittest discover -s tests -v   (standard library only)"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import clock
from app.db import DATA_DIR, connect, init_schema
from app.seed import SeedError, seed

FROZEN = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


def fresh() -> sqlite3.Connection:
    conn = connect(":memory:")
    init_schema(conn)
    return conn


class SeededCase(unittest.TestCase):
    def setUp(self):
        clock.freeze(FROZEN)
        self.conn = fresh()
        self.counts = seed(self.conn)

    def tearDown(self):
        clock.freeze(None)
        self.conn.close()

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def assign(self, task, contributor, status="accepted", **kw):
        cols = {"task_id": task, "contributor_id": contributor, "source": "operator", "status": status,
                "offered_at": clock.iso(), **kw}
        cur = self.conn.execute(f"INSERT INTO assignments ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                list(cols.values()))
        return cur.lastrowid

    def revise(self, assignment_id, revision, total, remaining, source="contributor"):
        self.conn.execute(
            "INSERT INTO estimate_revisions (assignment_id, revision, hours_total, hours_remaining, confidence, basis, "
            "source, submitted_by, submitted_at) VALUES (?, ?, ?, ?, 'medium', 'spec_review', ?, 'test', ?)",
            (assignment_id, revision, total, remaining, source, clock.iso()))


class TestSeedContents(SeededCase):
    def test_row_counts_match_the_datasets(self):
        self.assertEqual(self.one("SELECT COUNT(*) FROM expertise_levels"), 4)
        self.assertEqual(self.one("SELECT COUNT(*) FROM expertise_areas"), 6)
        self.assertEqual(self.one("SELECT COUNT(*) FROM area_sub_skills"), 30)
        self.assertEqual(self.one("SELECT COUNT(*) FROM contributors"), 11)
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks"), 10)
        self.assertEqual(self.one("SELECT COUNT(*) FROM task_attachments"), 22)

    def test_foreign_keys_are_clean(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_all_tasks_start_as_draft(self):
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks WHERE status <> 'draft'"), 0)

    def test_time_since_created_is_relative_to_load_time(self):
        self.assertEqual(self.one("SELECT created_at FROM tasks WHERE id = 'T-1006'"), "2026-10-25T11:00:00Z")  # 1500 min earlier
        self.assertEqual(self.one("SELECT due_at FROM tasks WHERE id = 'T-1002'"), "2026-10-28T12:00:00Z")      # +48h

    def test_attachments_are_hashed_and_sized(self):
        row = self.conn.execute("SELECT size_bytes, sha256 FROM task_attachments WHERE id = 'A-1001-1'").fetchone()
        self.assertEqual(row["size_bytes"], (DATA_DIR / "attachments/T-1001/requirements.md").stat().st_size)
        self.assertEqual(len(row["sha256"]), 64)

    def test_every_task_has_requirements_and_acceptance_criteria(self):
        missing = self.conn.execute(
            "SELECT t.id FROM tasks t WHERE (SELECT COUNT(DISTINCT kind) FROM task_attachments a WHERE a.task_id = t.id "
            "AND kind IN ('requirements', 'acceptance_criteria')) < 2").fetchall()
        self.assertEqual(missing, [])

    def test_sim_block_is_stored_as_json_and_creation_is_logged(self):
        sim = json.loads(self.one("SELECT sim FROM tasks WHERE id = 'T-1003'"))
        self.assertEqual((sim["routing"], sim["late_return_seconds"]), ("late_return", 120))
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE event_type = 'task_created'"), 10)

    def test_config_snapshot_and_buffer_setting_loaded(self):
        self.assertEqual(self.one("SELECT value FROM settings WHERE key = 'unconfirmed_buffer'"), "1.25")
        self.assertEqual(self.one("SELECT COUNT(*) FROM config_snapshots"), 1)


class TestIdempotentSeed(SeededCase):
    def test_second_run_adds_nothing_and_keeps_runtime_state(self):
        self.conn.execute("UPDATE tasks SET status = 'submitted' WHERE id = 'T-1001'")
        again = seed(self.conn)
        self.assertEqual((again["tasks_new"], again["attachments_new"], again["config_snapshot_new"]), (0, 0, False))
        self.assertEqual(self.one("SELECT status FROM tasks WHERE id = 'T-1001'"), "submitted")
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks"), 10)
        self.assertEqual(self.one("SELECT COUNT(*) FROM events WHERE event_type = 'task_created'"), 10)

    def test_reference_edits_in_json_are_picked_up(self):
        self.conn.execute("UPDATE contributors SET availability = 'offline' WHERE id = 'C-01'")
        seed(self.conn)   # JSON says 'available'
        self.assertEqual(self.one("SELECT availability FROM contributors WHERE id = 'C-01'"), "available")

    def test_missing_attachment_aborts_and_rolls_back(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(DATA_DIR, tmp / "data")
            (tmp / "data/attachments/T-1001/requirements.md").unlink()
            conn = fresh()
            with self.assertRaises(SeedError):
                seed(conn, tmp / "data")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contributors").fetchone()[0], 0)
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEligibilityView(SeededCase):
    def eligible(self, task):
        return {r[0] for r in self.conn.execute("SELECT contributor_id FROM v_eligible_contributors WHERE task_id = ?", (task,))}

    def test_matches_the_design_scenarios(self):
        self.assertEqual(self.eligible("T-1001"), {"C-01", "C-02", "C-05", "C-07"})
        self.assertEqual(self.eligible("T-1002"), {"C-03", "C-04", "C-09"})
        self.assertEqual(self.eligible("T-1005"), set())            # no_match: nobody at level 4 in legal_compliance
        self.assertEqual(self.eligible("T-1010"), {"C-03"})         # only Expert healthcare contributor
        self.assertEqual(self.eligible("T-1007"), {"C-01", "C-02"})

    def test_away_contributor_is_excluded_even_when_qualified(self):
        self.assertNotIn("C-11", self.eligible("T-1006"))           # C-11: technical_writing 3 but away
        self.conn.execute("UPDATE contributors SET availability = 'available' WHERE id = 'C-11'")
        self.assertIn("C-11", self.eligible("T-1006"))

    def test_capacity_does_not_affect_eligibility(self):
        self.assign("T-1002", "C-03")
        self.assign("T-1010", "C-03")
        self.assertIn("C-03", self.eligible("T-1010"))


class TestInvariants(SeededCase):
    def test_only_one_active_assignee_per_task(self):
        self.assign("T-1001", "C-01", status="offered")
        with self.assertRaises(sqlite3.IntegrityError):
            self.assign("T-1001", "C-02", status="offered")

    def test_task_can_be_reassigned_after_decline(self):
        first = self.assign("T-1004", "C-04", status="offered")
        self.conn.execute("UPDATE assignments SET status = 'declined' WHERE id = ?", (first,))
        self.assign("T-1004", "C-07", status="offered")              # no error: the first is no longer active
        self.assertEqual(self.one("SELECT COUNT(*) FROM assignments WHERE task_id = 'T-1004'"), 2)

    def _request(self, task, attempt, status="pending"):
        return self.conn.execute(
            "INSERT INTO routing_requests (task_id, attempt, started_at, status, eligible) VALUES (?, ?, ?, ?, '[]')",
            (task, attempt, clock.iso(), status))

    def test_only_one_pending_routing_request_per_task(self):
        self._request("T-1002", 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self._request("T-1002", 2)                                # restart must not double-submit
        self.conn.execute("UPDATE routing_requests SET status = 'superseded' WHERE task_id = 'T-1002'")
        self._request("T-1002", 2)                                    # allowed once the first is superseded

    def test_events_are_append_only(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE events SET actor = 'x' WHERE id = 1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM events WHERE id = 1")

    def test_estimate_revisions_are_append_only(self):
        a = self.assign("T-1001", "C-01")
        self.revise(a, 1, 10, 10)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE estimate_revisions SET hours_total = 99")
        with self.assertRaises(sqlite3.IntegrityError):
            self.revise(a, 1, 12, 12)                                 # revision numbers are unique per assignment

    def test_rejects_bad_values(self):
        for sql in ("UPDATE tasks SET status = 'bogus' WHERE id = 'T-1001'",
                    "UPDATE tasks SET priority = 'urgent' WHERE id = 'T-1001'",
                    "UPDATE contributors SET available_hours = 0 WHERE id = 'C-01'",
                    "UPDATE tasks SET area_id = 'astrology' WHERE id = 'T-1001'"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql)

    def test_duplicate_open_alert_is_rejected_but_allowed_after_resolve(self):
        ins = ("INSERT INTO alerts (task_id, trigger, agent, severity, message, dedupe_key, created_at) "
               "VALUES ('T-1002', 'stuck_request', 'RoutingAgent', 'critical', 'm', 'stuck_request:T-1002:1', ?)")
        self.conn.execute(ins, (clock.iso(),))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(ins, (clock.iso(),))
        self.conn.execute("UPDATE alerts SET status = 'resolved'")
        self.conn.execute(ins, (clock.iso(),))


class TestLoadView(SeededCase):
    """DESIGN 9.1: capacity uses the latest estimate revision, else requester estimate x 1.25 labelled unconfirmed."""

    def load(self, contributor):
        return dict(self.conn.execute("SELECT * FROM v_contributor_load WHERE contributor_id = ?", (contributor,)).fetchone())

    def test_idle_contributor_has_zero_load(self):
        row = self.load("C-03")
        self.assertEqual((row["active_tasks"], row["load_hours"], row["max_concurrent_tasks"], row["available_hours"]), (0, 0, 1, 40))

    def test_unconfirmed_requester_estimates_are_buffered(self):
        self.assign("T-1002", "C-03")                                 # requester 12h
        self.assign("T-1010", "C-03")                                 # requester 30h
        row = self.load("C-03")
        self.assertEqual((row["active_tasks"], row["load_hours"], row["unconfirmed_assignments"]), (2, 52.5, 2))

    def test_confirmed_estimate_replaces_the_buffered_guess(self):
        a1, a2 = self.assign("T-1002", "C-03"), self.assign("T-1010", "C-03")
        self.revise(a1, 1, 12, 12)
        self.revise(a2, 1, 30, 30)
        row = self.load("C-03")
        self.assertEqual((row["load_hours"], row["unconfirmed_assignments"]), (42, 0))   # still > 40h: a conflict either way

    def test_latest_revision_wins_including_check_in_remaining(self):
        a = self.assign("T-1002", "C-03")
        self.revise(a, 1, 12, 12)
        self.revise(a, 2, 12, 5, source="check_in")                   # contributor reports 5h left
        basis = self.conn.execute("SELECT load_hours, load_basis FROM v_assignment_load WHERE assignment_id = ?", (a,)).fetchone()
        self.assertEqual((basis["load_hours"], basis["load_basis"]), (5, "revision:check_in"))

    def test_finished_assignments_stop_counting(self):
        a = self.assign("T-1002", "C-03")
        self.conn.execute("UPDATE assignments SET status = 'completed' WHERE id = ?", (a,))
        self.assertEqual(self.load("C-03")["active_tasks"], 0)

    def test_overdue_estimates_are_counted_for_estimate_debt(self):
        self.assign("T-1006", "C-10", plan_health="overdue_estimate")
        self.assign("T-1004", "C-10", plan_health="overdue_estimate")
        self.assertEqual(self.load("C-10")["overdue_estimates"], 2)


class TestSchemaVersioning(unittest.TestCase):
    def test_init_is_idempotent_and_guards_foreign_versions(self):
        conn = fresh()
        init_schema(conn)                                             # second call is a no-op
        conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            init_schema(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main()
