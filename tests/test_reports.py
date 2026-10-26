"""Daily report built from a played-out day."""
import json
import unittest
from datetime import timedelta

from app import actions, clock, config, reports
from app.worker import tick_all

try:
    from tests.test_routing import Sys, T0
except ImportError:
    from test_routing import Sys, T0


class TestDailyReport(unittest.TestCase):
    def setUp(self):
        self.s = Sys()
        s = self.s
        for t in ("T-1001", "T-1002", "T-1004", "T-1005", "T-1006"):
            s.submit(t)
        s.run_to(60)
        actions.assign_task(s.conn, "T-1002", "C-03", "ops", reason="critical, cannot wait", now=s.now)   # operator intervenes on the stuck one
        s.run_to(1000, step=5)
        self.cfg = config.load(s.conn)
        self.report = reports.generate(s.conn, self.cfg, "2026-10-26", s.now)

    def tearDown(self):
        clock.freeze(None)
        self.s.conn.close()

    def test_lists_every_record_that_needed_attention(self):
        na = self.report["needing_attention"]
        self.assertEqual([a["task_id"] for a in na["stuck_request"]], ["T-1002"])
        self.assertEqual([a["task_id"] for a in na["no_match"]], ["T-1005"])
        self.assertEqual([a["task_id"] for a in na["request_declined"]], ["T-1004"])
        self.assertEqual(na["stuck_request"][0]["status"], "resolved")                 # resolved when the operator assigned
        self.assertEqual(na["stuck_request"][0]["severity"], "critical")
        self.assertGreaterEqual(na["stuck_request"][0]["outbox_messages"], 1)
        self.assertNotIn("late_return_superseded", na)                                  # info alerts are log-only

    def test_interventions_are_listed_with_the_operator_as_actor(self):
        acts = self.report["operator_interventions"]
        self.assertTrue(any(a["event"] == "assignment_offered" and a["task_id"] == "T-1002" and a["actor"] == "operator:ops" for a in acts))
        self.assertTrue(any(a["event"] == "routing_superseded" and a["task_id"] == "T-1002" for a in acts))

    def test_flow_and_questions(self):
        self.assertEqual(self.report["flow"]["submitted"], 5)
        self.assertGreaterEqual(self.report["flow"]["started"], 2)
        qs = self.report["questions"]
        self.assertGreaterEqual(qs["asked"], 3)
        self.assertIn("T-1005", [q["task_id"] for q in qs["still_open"]])             # no_match is still waiting for the operator
        self.assertEqual(qs["answered_by_operator"], 0)

    def test_estimate_quality_metrics(self):
        q = self.report["estimate_quality"]
        self.assertGreaterEqual(q["assignments_accepted"], 3)
        self.assertEqual(q["estimate_coverage_pct"], round(100 * q["estimates_within_sla"] / q["assignments_accepted"], 1))
        self.assertIn("T-1006", [a["task_id"] for a in q["currently_overdue_or_stale"]])   # T-1006 never gave an estimate
        self.assertIn("high", q["median_minutes_to_estimate_by_priority"])

    def test_tasks_touched_have_current_state_and_age(self):
        by_id = {t["id"]: t for t in self.report["tasks_touched"]}
        self.assertEqual(by_id["T-1005"]["status"], "unmatched")
        self.assertEqual(by_id["T-1002"]["assignee"], "Dr. Elena Vasquez")
        self.assertGreater(by_id["T-1006"]["age_minutes"], 1400)

    def test_persisted_regenerable_and_renderable(self):
        s = self.s
        stored = json.loads(s.one("SELECT content FROM daily_reports WHERE report_date = '2026-10-26'"))
        self.assertEqual(stored["flow"], self.report["flow"])
        again = reports.generate(s.conn, self.cfg, "2026-10-26", s.now)
        self.assertEqual(s.one("SELECT COUNT(*) FROM daily_reports WHERE report_date = '2026-10-26'"), 1)   # upsert, not duplicate
        self.assertEqual(again["needing_attention"].keys(), self.report["needing_attention"].keys())
        text = reports.render_text(self.report)
        self.assertIn("NEEDING ATTENTION", text)
        self.assertIn("T-1005", text)

    def test_worker_writes_yesterdays_report_when_the_date_rolls_over(self):
        s = self.s
        s.conn.execute("DELETE FROM daily_reports")
        s.sec = int((T0 + timedelta(days=1, hours=1) - T0).total_seconds()) - 1
        tick_all(s.conn, s.agent, s.contributors, s.tracking, s.now)
        self.assertEqual(s.one("SELECT COUNT(*) FROM daily_reports WHERE report_date = '2026-10-26'"), 1)


if __name__ == "__main__":
    unittest.main()
