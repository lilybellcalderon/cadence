"""TrackingAgent scenarios. Simulated clock, demo compression 60: one simulated SLA/cadence minute = one real second."""
import json
import random
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app import actions, assignments, clock, config, tasks, tracking
from app.db import transaction
from app.worker import tick_all

try:
    from tests.test_routing import Sys, T0, WORKING
except ImportError:                      # python -m unittest discover -s tests
    from test_routing import Sys, T0, WORKING


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sys()

    def tearDown(self):
        clock.freeze(None)
        self.s.conn.close()

    def assignment(self, tid, status=None):
        sql = "SELECT * FROM assignments WHERE task_id = ?" + (" AND status = ?" if status else "") + " ORDER BY id DESC"
        row = self.s.conn.execute(sql, (tid, status) if status else (tid,)).fetchone()
        return dict(row) if row else None

    def run_to_accept(self, tid, limit=200):
        """Submit and advance until the contributor has accepted; returns the assignment."""
        self.s.submit(tid)
        while self.s.sec < limit:
            self.s.run_to(self.s.sec + 1)
            a = self.assignment(tid)
            if a and a["decided_at"]:
                return a
        self.fail(f"{tid} was not accepted within {limit}s")


class TestEstimateCapture(Base):
    def test_estimate_given_at_accept_means_plan_complete_and_work_starts(self):
        s = self.s
        self.run_to_accept("T-1001")
        s.run_to(s.sec + 10)
        a = self.assignment("T-1001")
        self.assertEqual((a["status"], a["plan_health"]), ("in_progress", "complete"))
        self.assertEqual(s.status("T-1001"), "in_progress")
        self.assertEqual(s.one("SELECT COUNT(*) FROM estimate_requests WHERE assignment_id = ?", a["id"]), 0)   # nothing to chase
        rev = s.rows("SELECT * FROM estimate_revisions WHERE assignment_id = ?", a["id"])
        self.assertEqual([(r["hours_total"], r["source"]) for r in rev], [(14, "contributor")])
        self.assertEqual(s.events("T-1001", "plan_health_changed")[-1]["payload"].count("complete"), 1)

    def test_work_cannot_start_without_a_confirmed_estimate(self):
        s = self.s
        a = self.run_to_accept("T-1006")                                       # scripted: never gives an estimate
        with self.assertRaises(assignments.EstimateRequired):
            with transaction(s.conn):
                assignments.start(s.conn, a["id"], "contributor:C-06", s.now)
        s.run_to(s.sec + 60)
        self.assertEqual(s.status("T-1006"), "accepted")                       # the simulator did not start it either
        self.assertIsNone(self.assignment("T-1006")["started_at"])

    def test_variance_against_the_requester_estimate_is_flagged(self):
        s = self.s
        self.run_to_accept("T-1008")                                           # contributor says 28h, requester said 16h
        s.run_to(s.sec + 5)
        a = s.alert("T-1008", "estimate_variance")
        self.assertEqual((a["severity"], a["agent"], a["status"]), ("normal", "TrackingAgent", "open"))   # high - 1
        q = s.question("T-1008", "estimate_variance")
        ctx = json.loads(q["context"])
        self.assertEqual((ctx["contributor_hours"], ctx["requester_hours"], ctx["delta_pct"]), (28, 16, "+75"))
        self.assertIn("at 28h versus 16.0h from the requester (+75%)", q["prompt"])      # prompt is filled from the context
        out = s.answer(q["id"], "accept_contributor_estimate")
        self.assertTrue(out.resolved)
        self.assertEqual(s.alert("T-1008", "estimate_variance")["status"], "resolved")

    def test_a_close_estimate_raises_nothing(self):
        s = self.s
        self.run_to_accept("T-1001")                                           # 14h vs 16h
        s.run_to(s.sec + 30)
        self.assertEqual(s.one("SELECT COUNT(*) FROM alerts WHERE task_id = 'T-1001'"), 0)


class TestChaseLadder(Base):
    def test_late_estimate_is_chased_with_a_reminder_then_arrives(self):
        s = self.s
        a = self.run_to_accept("T-1003")                                       # high: due 30, reminder every 20, answers after 1 reminder
        self.assertEqual(self.assignment("T-1003")["plan_health"], "awaiting_estimate")
        self.assertEqual(s.one("SELECT sequence FROM estimate_requests WHERE assignment_id = ?", a["id"]), 0)
        due = clock.parse(a["estimate_due_at"])
        self.assertEqual((due - clock.parse(a["decided_at"])).total_seconds(), 30)
        s.run_to(int((due - T0).total_seconds()) + 1)
        self.assertEqual(self.assignment("T-1003")["plan_health"], "overdue_estimate")
        self.assertEqual(s.one("SELECT COUNT(*) FROM estimate_requests WHERE assignment_id = ? AND kind = 'reminder'", a["id"]), 1)
        s.run_to(s.sec + 8)                                                    # contributor answers the reminder
        self.assertEqual(self.assignment("T-1003")["plan_health"], "complete")
        self.assertEqual(s.one("SELECT hours_total FROM estimate_revisions WHERE assignment_id = ?", a["id"]), 26)
        self.assertEqual(s.one("SELECT COUNT(*) FROM estimate_requests WHERE answered_at IS NULL"), 0)
        self.assertEqual(s.one("SELECT COUNT(*) FROM alerts WHERE trigger = 'estimate_overdue'"), 0)     # resolved before it reached the operator

    def test_unanswered_ladder_ends_with_an_alert_and_a_question(self):
        s = self.s
        a = self.run_to_accept("T-1006")                                       # low: due 480, reminders every 240, max 2 -> alert at 960
        start = clock.parse(a["decided_at"])
        s.run_to(int((start - T0).total_seconds()) + 950, step=5)
        self.assertIsNone(s.alert("T-1006", "estimate_overdue"))
        self.assertEqual(s.one("SELECT COUNT(*) FROM estimate_requests WHERE assignment_id = ?", a["id"]), 3)   # request + 2 reminders
        s.run_to(int((start - T0).total_seconds()) + 975, step=5)
        al = s.alert("T-1006", "estimate_overdue")
        self.assertEqual((al["severity"], al["agent"]), ("low", "TrackingAgent"))
        q = s.question("T-1006", "estimate_missing")
        ctx = json.loads(q["context"])
        self.assertEqual((ctx["reminders"], ctx["fallback_hours"]), (2, 25.0))          # planning falls back to 20h x 1.25
        self.assertIn("requester estimate x1.25", ctx["fallback_source"])
        self.assertEqual(self.assignment("T-1006")["plan_health"], "overdue_estimate")

    def test_operator_can_enter_an_estimate_on_the_contributors_behalf(self):
        s = self.s
        a = self.run_to_accept("T-1006")
        s.run_to(int((clock.parse(a["decided_at"]) - T0).total_seconds()) + 975, step=5)
        q = s.question("T-1006", "estimate_missing")
        with self.assertRaises(actions.ActionError):
            s.answer(q["id"], "enter_estimate_on_their_behalf")                        # needs hours_total
        s.answer(q["id"], "enter_estimate_on_their_behalf", hours_total=18, note="called, agreed 18h")
        rev = s.rows("SELECT * FROM estimate_revisions WHERE assignment_id = ?", a["id"])[0]
        self.assertEqual((rev["hours_total"], rev["source"], rev["confidence"]), (18, "operator_on_behalf", "low"))
        s.run_to(s.sec + 10)
        self.assertEqual(s.alert("T-1006", "estimate_overdue")["status"], "resolved")
        self.assertEqual(s.status("T-1006"), "in_progress")                           # the gate is open, work starts

    def test_provisional_requester_estimate_is_labelled_low_confidence(self):
        s = self.s
        a = self.run_to_accept("T-1006")
        s.run_to(int((clock.parse(a["decided_at"]) - T0).total_seconds()) + 975, step=5)
        s.answer(s.question("T-1006", "estimate_missing")["id"], "use_requester_estimate_provisionally")
        rev = s.rows("SELECT * FROM estimate_revisions WHERE assignment_id = ?", a["id"])[0]
        self.assertEqual((rev["hours_total"], rev["confidence"], rev["source"]), (20, "low", "operator_on_behalf"))
        self.assertIn("provisional", rev["note"])

    def test_default_for_an_unanswered_estimate_question_is_a_reminder_not_a_reassignment(self):
        s = self.s
        a = self.run_to_accept("T-1006")
        s.run_to(int((clock.parse(a["decided_at"]) - T0).total_seconds()) + 975, step=5)
        q = s.question("T-1006", "estimate_missing")
        self.assertEqual(q["safe_default"], "send_reminder")
        before = s.one("SELECT COUNT(*) FROM estimate_requests WHERE assignment_id = ?", a["id"])
        s.tracking.apply_safe_default(q, s.now)
        self.assertEqual(s.one("SELECT COUNT(*) FROM estimate_requests WHERE assignment_id = ?", a["id"]), before + 1)
        self.assertEqual(s.question("T-1006")["id"], q["id"])                          # still open
        self.assertEqual(self.assignment("T-1006")["status"], "accepted")              # nobody was moved
        self.assertEqual(s.one("SELECT COUNT(*) FROM assignments WHERE task_id = 'T-1006'"), 1)


class TestStatusCheckIns(Base):
    def test_check_in_hours_replace_the_estimate_in_planning(self):
        s = self.s
        a = self.run_to_accept("T-1001")                                       # high: check-in every 480; scripted on_track, 6h remaining
        s.run_to(int((clock.parse(a["decided_at"]) - T0).total_seconds()) + 495)
        revs = s.rows("SELECT revision, hours_total, hours_remaining, source FROM estimate_revisions WHERE assignment_id = ? ORDER BY revision", a["id"])
        self.assertEqual([(r["hours_remaining"], r["source"]) for r in revs], [(14, "contributor"), (6, "check_in")])
        load = s.conn.execute("SELECT load_hours, load_basis FROM v_assignment_load WHERE assignment_id = ?", (a["id"],)).fetchone()
        self.assertEqual((load["load_hours"], load["load_basis"]), (6, "revision:check_in"))
        self.assertEqual(s.one("SELECT COUNT(*) FROM alerts WHERE task_id = 'T-1001'"), 0)

    def test_at_risk_reply_raises_an_alert_and_a_question_and_accept_risk_closes_it(self):
        s = self.s
        a = self.run_to_accept("T-1003")
        s.run_to(int((clock.parse(a["decided_at"]) - T0).total_seconds()) + 500)
        al = s.alert("T-1003", "status_at_risk")
        self.assertEqual((al["severity"], al["status"]), ("normal", "open"))            # high - 1
        self.assertEqual(self.assignment("T-1003")["plan_health"], "at_risk")
        q = s.question("T-1003", "at_risk")
        self.assertIn("Event tracking gap", al["message"])
        self.assertEqual(q["safe_default"], "accept_risk")
        s.answer(q["id"], "accept_risk", note="analysis team will cover")
        self.assertEqual(s.alert("T-1003", "status_at_risk")["status"], "resolved")
        self.assertEqual(len(s.events("T-1003", "risk_accepted")), 1)

    def test_unanswered_at_risk_question_never_closes_itself(self):
        s = self.s
        a = self.run_to_accept("T-1003")
        s.run_to(int((clock.parse(a["decided_at"]) - T0).total_seconds()) + 500)
        q = s.question("T-1003", "at_risk")
        s.tracking.apply_safe_default(q, s.now)
        self.assertEqual(s.question("T-1003", "at_risk")["id"], q["id"])
        self.assertEqual(s.question("T-1003", "at_risk")["safe_default_applied_count"], 1)

    def test_blocked_reply_is_high_severity_for_a_high_priority_task_and_reaches_the_outbox(self):
        s = self.s
        a = self.run_to_accept("T-1008")
        s.run_to(int((clock.parse(a["decided_at"]) - T0).total_seconds()) + 500)
        al = s.alert("T-1008", "status_blocked")
        self.assertEqual(al["severity"], "high")
        self.assertEqual(s.one("SELECT COUNT(*) FROM outbox WHERE alert_id = ?", al["id"]), 1)

    def test_on_track_later_resolves_the_earlier_at_risk_alert(self):
        s = self.s
        a = self.run_to_accept("T-1003")
        base = int((clock.parse(a["decided_at"]) - T0).total_seconds())
        s.run_to(base + 500)
        self.assertEqual(s.alert("T-1003", "status_at_risk")["status"], "open")
        s.conn.execute("UPDATE tasks SET sim = ? WHERE id = 'T-1003'", (json.dumps({"routing": "late_return", "status_check": {"reply": "on_track", "hours_remaining": 12}}),))
        s.run_to(base + 1000)
        self.assertEqual(s.alert("T-1003", "status_at_risk")["status"], "resolved")
        self.assertEqual(self.assignment("T-1003")["plan_health"], "complete")

    def test_two_missed_check_ins_raise_contributor_silent(self):
        s = self.s
        a = self.run_to_accept("T-1006")                                       # scripted: never replies; low cadence 2880
        base = int((clock.parse(a["decided_at"]) - T0).total_seconds())
        s.run_to(base + 2880 + 100, step=10)
        self.assertEqual(s.one("SELECT COUNT(*) FROM status_checks WHERE assignment_id = ? AND status = 'no_response'", a["id"]), 1)
        self.assertIsNone(s.alert("T-1006", "status_no_response"))                     # one miss is not enough
        s.run_to(base + 2 * 2880 + 150, step=10)
        al = s.alert("T-1006", "status_no_response")
        self.assertEqual((al["severity"], al["status"]), ("low", "open"))
        q = s.question("T-1006", "contributor_silent")
        self.assertEqual(json.loads(q["context"])["missed"], 2)
        s.answer(q["id"], "escalate_to_manager")
        self.assertEqual(s.one("SELECT COUNT(*) FROM outbox WHERE recipient = 'manager'"), 1)


class TestDueDateRisk(Base):
    def test_remaining_hours_beyond_the_working_time_before_due_is_flagged_until_accepted(self):
        s = self.s
        actions.assign_task(s.conn, "T-1007", "C-01", "ops", now=s.now)
        s.run_to(15)
        aid = self.assignment("T-1007")["id"]
        self.assertIsNone(s.alert("T-1007", "due_date_at_risk"))                       # 24h vs 32 working hours
        with transaction(s.conn):                                                      # operator records 40h on the contributor's behalf
            tracking.submit_estimate(s.conn, config.load(s.conn), aid, hours_total=40, confidence="low", actor="operator:ops",
                                     now=s.now, source="operator_on_behalf")
        s.run_to(s.sec + 3)
        al = s.alert("T-1007", "due_date_at_risk")
        self.assertEqual(al["severity"], "normal")
        self.assertEqual(self.assignment("T-1007")["plan_health"], "at_risk")
        q = s.question("T-1007", "at_risk")
        s.answer(q["id"], "accept_risk")
        s.run_to(s.sec + 10)
        self.assertEqual(s.one("SELECT COUNT(*) FROM alerts WHERE trigger = 'due_date_at_risk' AND status <> 'resolved'"), 0)   # not re-raised

    def test_extending_the_due_date_clears_the_risk(self):
        s = self.s
        actions.assign_task(s.conn, "T-1007", "C-01", "ops", now=s.now)
        s.run_to(15)
        aid = self.assignment("T-1007")["id"]
        with transaction(s.conn):
            tracking.submit_estimate(s.conn, config.load(s.conn), aid, hours_total=40, actor="operator:ops", now=s.now, source="operator_on_behalf")
        s.run_to(s.sec + 3)
        before = s.one("SELECT due_at FROM tasks WHERE id = 'T-1007'")
        s.answer(s.question("T-1007", "at_risk")["id"], "extend_due_date", extend_hours=48)
        self.assertEqual(clock.parse(s.one("SELECT due_at FROM tasks WHERE id = 'T-1007'")) - clock.parse(before), timedelta(hours=48))
        s.run_to(s.sec + 5)
        self.assertEqual(self.assignment("T-1007")["plan_health"], "complete")

    def test_reassign_from_a_tracking_question_moves_the_task(self):
        s = self.s
        actions.assign_task(s.conn, "T-1007", "C-01", "ops", now=s.now)
        s.run_to(15)
        aid = self.assignment("T-1007")["id"]
        with transaction(s.conn):
            tracking.submit_estimate(s.conn, config.load(s.conn), aid, hours_total=40, actor="operator:ops", now=s.now, source="operator_on_behalf")
        s.run_to(s.sec + 3)
        s.answer(s.question("T-1007", "at_risk")["id"], "reassign_task", contributor_id="C-02")
        self.assertEqual(s.rows("SELECT contributor_id, status FROM assignments WHERE task_id = 'T-1007' ORDER BY id")[0]["status"], "cancelled")
        self.assertEqual(self.assignment("T-1007")["contributor_id"], "C-02")


class TestEstimateDebt(Base):
    def prepare(self):
        s = self.s
        for tid in ("T-1006", "T-1004"):                                        # two assignments for C-10 that never got an estimate
            s.conn.execute("INSERT INTO assignments (task_id, contributor_id, source, status, offered_at, decided_at, estimate_due_at, plan_health) "
                           "VALUES (?, 'C-10', 'operator', 'accepted', ?, ?, ?, 'awaiting_estimate')",
                           (tid, clock.iso(T0), clock.iso(T0), clock.iso(T0)))
            s.conn.execute("UPDATE tasks SET status = 'accepted' WHERE id = ?", (tid,))
        s.conn.execute("UPDATE contributors SET max_concurrent_tasks = 5, available_hours = 200 WHERE id = 'C-10'")   # capacity is not the issue
        s.conn.execute("UPDATE tasks SET sim = ? WHERE id IN ('T-1006', 'T-1004')", (json.dumps({"estimate": {"mode": "never"}}),))
        s.run_to(10)

    def test_two_overdue_estimates_raise_estimate_debt(self):
        s = self.s
        self.prepare()
        self.assertEqual(s.one("SELECT overdue_estimates FROM v_contributor_load WHERE contributor_id = 'C-10'"), 2)
        al = s.conn.execute("SELECT * FROM alerts WHERE trigger = 'estimate_debt'").fetchone()
        self.assertIsNotNone(al)
        self.assertEqual((al["severity"], al["task_id"]), ("low", "T-1004"))           # medium - 1, attached to the higher-priority task
        self.assertEqual(s.question("T-1004", "estimate_missing")["type"], "estimate_missing")

    def test_routing_then_avoids_the_contributor_and_queues_when_they_are_the_only_option(self):
        s = self.s
        self.prepare()
        cfg = config.load(s.conn)
        chk = __import__("app.capacity", fromlist=["x"]).check(s.conn, cfg, "C-10", tasks.get(s.conn, "T-1001"))
        self.assertFalse(chk["fits"])
        self.assertTrue(any("estimate debt" in r for r in chk["reasons"]))
        # T-1006 (technical writing, min 2) is eligible for C-06 and C-10; with C-10 in debt the engine ranks C-06 first
        ranked = __import__("app.capacity", fromlist=["x"]).eligible(s.conn, cfg, tasks.get(s.conn, "T-1006"))
        self.assertEqual(ranked[0]["id"], "C-06")
        self.assertFalse(next(e for e in ranked if e["id"] == "C-10")["fits"])

    def test_clearing_one_overdue_estimate_resolves_the_debt_alert(self):
        s = self.s
        self.prepare()
        aid = self.assignment("T-1004")["id"]
        with transaction(s.conn):
            tracking.submit_estimate(s.conn, config.load(s.conn), aid, hours_total=30, actor="contributor:C-10", now=s.now)
        s.run_to(s.sec + 5)
        self.assertEqual(s.conn.execute("SELECT status FROM alerts WHERE trigger = 'estimate_debt'").fetchone()["status"], "resolved")
        self.assertEqual(s.one("SELECT overdue_estimates FROM v_contributor_load WHERE contributor_id = 'C-10'"), 1)


class TestStaleAndRestart(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "t.db"
        self.systems = []

    def tearDown(self):
        clock.freeze(None)
        for x in self.systems:
            x.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, fresh):
        x = Sys(self.path, fresh=fresh)
        self.systems.append(x)
        return x

    def test_stale_estimate_is_flagged(self):
        s = self.make(True)
        s.conn.execute("UPDATE tasks SET sim = ? WHERE id = 'T-1001'", (json.dumps({"routing": "normal", "status_check": {"reply": "no_response"},
                                                                           "estimate": {"mode": "with_accept", "hours_total": 14, "confidence": "high", "basis": "similar_work"}}),))
        s.submit("T-1001")                                                      # high: stale after 720 sim minutes, check-ins go unanswered
        s.run_to(900, step=5)
        a = s.one("SELECT plan_health FROM assignments WHERE task_id = 'T-1001'")
        self.assertEqual(a, "stale")
        al = s.alert("T-1001", "estimate_stale")
        self.assertEqual(al["severity"], "normal")                              # high - 1

    def test_restart_does_not_duplicate_requests_or_alerts(self):
        a = self.make(True)
        a.submit("T-1006")
        a.run_to(400, step=5)
        reqs = a.one("SELECT COUNT(*) FROM estimate_requests")
        b = self.make(False)
        b.sec = a.sec
        a.conn.close()
        self.assertEqual(b.tracking.recover(b.now)["active_assignments"], 1)
        b.run_to(1100, step=5)
        seqs = [r["sequence"] for r in b.rows("SELECT sequence FROM estimate_requests ORDER BY sequence")]
        self.assertEqual(seqs, [0, 1, 2])                                       # request + two reminders, each exactly once
        self.assertGreaterEqual(len(seqs), reqs)
        dup = b.rows("SELECT dedupe_key, COUNT(*) n FROM alerts WHERE status <> 'resolved' GROUP BY dedupe_key HAVING n > 1")
        self.assertEqual(dup, [])
        self.assertEqual(b.one("SELECT COUNT(*) FROM alerts WHERE trigger = 'estimate_overdue'"), 1)


class TestOwnership(Base):
    def test_every_tracking_question_option_has_a_handler_branch(self):
        import inspect
        cfg = config.load(self.s.conn)
        self.assertEqual(set(tracking.HANDLERS), tracking.TRACKING_QUESTIONS)
        for qtype, fn in tracking.HANDLERS.items():
            src = inspect.getsource(fn)
            for option in cfg["operator_questions"]["types"][qtype]["options"]:
                self.assertIn(f'"{option}"', src, f"{qtype}: option {option} has no branch")

    def test_each_trigger_is_owned_by_exactly_one_agent_and_its_question_matches(self):
        cfg = config.load(self.s.conn)
        from app import alerts
        for name, trg in cfg["alerts"]["triggers"].items():
            q = trg["question"]
            if q:
                owner = "RoutingAgent" if q in alerts.ROUTING_QUESTIONS else "TrackingAgent"
                self.assertEqual(trg["agent"], owner, f"{name}: trigger agent and question owner disagree")


if __name__ == "__main__":
    unittest.main()
