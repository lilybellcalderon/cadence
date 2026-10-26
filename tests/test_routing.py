"""RoutingAgent scenarios, driven by a simulated clock (one worker tick per simulated second)."""
import inspect
import json
import random
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import actions, alerts, clock, config, routing, simulator, tasks
from app.db import connect, init_schema
from app.routing import RoutingAgent
from app.seed import seed
from app.contributor_sim import ContributorSimulator
from app.tracking import TrackingAgent
from app.worker import tick_all

T0 = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
WORKING = ("accepted", "in_progress")     # accepted; with a confirmed estimate the contributor starts work a few seconds later


class Sys:
    """A seeded system plus helpers to advance simulated time."""

    def __init__(self, path=":memory:", fresh=True):
        clock.freeze(T0)
        self.conn = connect(path)
        if fresh:
            init_schema(self.conn)
            seed(self.conn)
        self.agent = RoutingAgent(self.conn, random.Random(7))
        self.contributors = ContributorSimulator(self.conn)
        self.tracking = TrackingAgent(self.conn, random.Random(7))
        self.sec = 0

    @property
    def now(self):
        return T0 + timedelta(seconds=self.sec)

    def run_to(self, sec, step=1):
        while self.sec < sec:
            self.sec = min(sec, self.sec + step)
            tick_all(self.conn, self.agent, self.contributors, self.tracking, self.now)

    def submit(self, tid):
        actions.submit_task(self.conn, tid, "ops", now=self.now)

    def one(self, sql, *args):
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row else None

    def rows(self, sql, *args):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def status(self, tid):
        return self.one("SELECT status FROM tasks WHERE id = ?", tid)

    def question(self, tid, qtype=None):
        sql = "SELECT * FROM operator_questions WHERE task_id = ? AND status = 'open'" + (" AND type = ?" if qtype else "") + " ORDER BY id DESC"
        return self.conn.execute(sql, (tid, qtype) if qtype else (tid,)).fetchone()

    def alert(self, tid, trigger):
        return self.conn.execute("SELECT * FROM alerts WHERE task_id = ? AND trigger = ? ORDER BY id DESC", (tid, trigger)).fetchone()

    def answer(self, qid, option, **params):
        return actions.answer_question(self.conn, qid, option, "ops", params=params, now=self.now, rng=random.Random(3))

    def events(self, tid, etype):
        return self.rows("SELECT * FROM events WHERE task_id = ? AND event_type = ? ORDER BY id", tid, etype)


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sys()

    def tearDown(self):
        clock.freeze(None)
        self.s.conn.close()


class TestRoutine(Base):
    def test_routine_assignment_flows_to_accepted_with_estimate(self):
        s = self.s
        s.submit("T-1001")
        self.assertEqual(s.status("T-1001"), "submitted")
        s.run_to(60)
        self.assertIn(s.status("T-1001"), WORKING)
        req = s.rows("SELECT * FROM routing_requests WHERE task_id = 'T-1001'")
        self.assertEqual(len(req), 1)
        latency = (clock.parse(req[0]["finished_at"]) - clock.parse(req[0]["started_at"])).total_seconds()
        self.assertTrue(5 <= latency <= 21, latency)                       # 5-20s engine latency, polled each second
        a = s.rows("SELECT * FROM assignments WHERE task_id = 'T-1001'")
        self.assertEqual((len(a), a[0]["status"], a[0]["plan_health"], a[0]["source"]), (1, "in_progress", "complete", "routing"))
        self.assertIn(a[0]["contributor_id"], {"C-01", "C-02", "C-05", "C-07"})
        est = s.rows("SELECT * FROM estimate_revisions WHERE assignment_id = ?", a[0]["id"])
        self.assertEqual((est[0]["hours_total"], est[0]["confidence"], est[0]["source"]), (14, "high", "contributor"))
        self.assertEqual(s.one("SELECT COUNT(*) FROM alerts"), 0)           # a normal run raises nothing

    def test_engine_prefers_the_contributor_with_the_most_headroom(self):
        self.s.submit("T-1001")
        self.s.run_to(2)
        # all four eligible contributors fit; C-02 has the most available hours so the lowest utilisation (20h / 60h)
        self.assertEqual(self.s.one("SELECT sim_pick FROM routing_requests WHERE task_id = 'T-1001'"), "C-02")
        eligible = json.loads(self.s.one("SELECT eligible FROM routing_requests WHERE task_id = 'T-1001'"))
        self.assertEqual([e["id"] for e in eligible][0], "C-02")
        self.assertTrue(all(e["fits"] for e in eligible))


class TestUnresolvedRouting(Base):
    def test_hang_is_flagged_never_failed(self):
        s = self.s
        s.submit("T-1002")
        s.run_to(25)
        self.assertIsNone(s.alert("T-1002", "stuck_request"))
        s.run_to(28)
        a = s.alert("T-1002", "stuck_request")
        self.assertEqual((a["severity"], a["agent"], a["status"]), ("critical", "RoutingAgent", "open"))
        self.assertEqual(s.one("SELECT COUNT(*) FROM outbox WHERE alert_id = ?", a["id"]), 1)   # critical -> simulated external message
        q = s.question("T-1002", "stuck_request")
        self.assertEqual(json.loads(q["options"]), ["keep_waiting", "resubmit_routing", "assign_manually", "cancel_task"])
        s.run_to(1000, step=5)
        self.assertEqual(s.status("T-1002"), "routing")                      # still waiting: nothing auto-failed or reassigned
        self.assertEqual(s.one("SELECT status FROM routing_requests WHERE task_id = 'T-1002'"), "pending")
        self.assertEqual(s.one("SELECT COUNT(*) FROM assignments WHERE task_id = 'T-1002'"), 0)

    def test_unacknowledged_critical_alert_re_alerts_then_escalates(self):
        s = self.s
        s.submit("T-1002")
        s.run_to(1000, step=2)
        a = s.alert("T-1002", "stuck_request")
        self.assertEqual(a["re_alert_count"], 3)                             # every 5 min, max 3
        self.assertIsNone(a["next_re_alert_at"])
        self.assertEqual(s.one("SELECT COUNT(*) FROM outbox WHERE alert_id = ?", a["id"]), 4)   # original + 3 reminders
        q = s.question("T-1002", "stuck_request")
        self.assertGreaterEqual(q["safe_default_applied_count"], 3)          # safe default = keep waiting, applied each deadline
        self.assertEqual(q["status"], "open")

    def test_acknowledging_stops_re_alerts(self):
        s = self.s
        s.submit("T-1002")
        s.run_to(40)
        a = s.alert("T-1002", "stuck_request")
        actions.acknowledge_alert(s.conn, a["id"], "ops", now=s.now)
        s.run_to(700, step=2)
        a = s.alert("T-1002", "stuck_request")
        self.assertEqual((a["status"], a["re_alert_count"], a["acknowledged_by"]), ("acknowledged", 0, "operator:ops"))

    def test_keep_waiting_asks_again_after_the_timeout_if_still_pending(self):
        s = self.s
        s.submit("T-1002")
        s.run_to(40)
        s.answer(s.question("T-1002")["id"], "keep_waiting")
        self.assertEqual(s.alert("T-1002", "stuck_request")["status"], "resolved")
        s.run_to(300)
        self.assertEqual(s.one("SELECT COUNT(*) FROM alerts WHERE task_id = 'T-1002' AND status <> 'resolved'"), 0)
        s.run_to(360)                                                        # critical answer timeout is 5 minutes
        self.assertEqual(s.one("SELECT COUNT(*) FROM alerts WHERE task_id = 'T-1002' AND trigger = 'stuck_request'"), 2)
        self.assertIsNotNone(s.question("T-1002", "stuck_request"))

    def test_resubmit_supersedes_and_sends_a_new_request(self):
        s = self.s
        s.submit("T-1002")
        s.run_to(40)
        s.answer(s.question("T-1002")["id"], "resubmit_routing")
        reqs = s.rows("SELECT id, status, attempt FROM routing_requests WHERE task_id = 'T-1002' ORDER BY id")
        self.assertEqual([(r["attempt"], r["status"]) for r in reqs], [(1, "superseded"), (2, "pending")])
        self.assertEqual(s.status("T-1002"), "routing")


class TestIntervention(Base):
    def test_operator_assigns_while_pending_and_the_late_result_is_ignored(self):
        s = self.s
        s.submit("T-1003")                                                   # engine will answer after 120s
        s.run_to(40)
        self.assertIsNotNone(s.alert("T-1003", "stuck_request"))
        warnings = actions.assign_task(s.conn, "T-1003", "C-05", "ops", reason="deadline", now=s.now)
        self.assertEqual(warnings, [])
        self.assertEqual(s.status("T-1003"), "offered")
        self.assertEqual(s.one("SELECT status FROM routing_requests WHERE task_id = 'T-1003'"), "superseded")
        self.assertEqual(s.alert("T-1003", "stuck_request")["status"], "resolved")
        self.assertIsNone(s.question("T-1003"))                              # its question was withdrawn
        s.run_to(60)
        self.assertIn(s.status("T-1003"), WORKING)
        s.run_to(140)                                                        # the engine finally answers
        late = s.events("T-1003", "late_return_superseded")
        self.assertEqual(len(late), 1)
        self.assertEqual(json.loads(late[0]["payload"])["action"], "ignored")
        self.assertIn(s.status("T-1003"), WORKING)                     # nothing changed
        self.assertEqual(s.rows("SELECT contributor_id, status FROM assignments WHERE task_id = 'T-1003'"), [{"contributor_id": "C-05", "status": "in_progress"}])
        self.assertIsNotNone(s.one("SELECT late_returned_at FROM routing_requests WHERE task_id = 'T-1003'"))
        self.assertEqual(s.alert("T-1003", "late_return_superseded")["severity"], "info")   # log-only

    def test_without_intervention_the_late_result_is_used(self):
        s = self.s
        s.submit("T-1003")
        s.run_to(130)                                                        # returned ~121s, contributor answered ~3s later
        self.assertIn(s.status("T-1003"), WORKING)
        self.assertEqual(s.one("SELECT source FROM assignments WHERE task_id = 'T-1003'"), "routing")
        self.assertEqual(s.events("T-1003", "late_return_superseded"), [])

    def test_manual_assign_warns_but_does_not_block(self):
        s = self.s
        warnings = actions.assign_task(s.conn, "T-1005", "C-11", "ops", now=s.now)   # C-11 is away and below level 4
        self.assertIn("not eligible by area/level/availability", warnings)
        self.assertEqual(s.status("T-1005"), "offered")

    def test_reassigning_an_accepted_task_cancels_the_old_assignment(self):
        s = self.s
        s.submit("T-1001")
        s.run_to(60)
        first = s.one("SELECT contributor_id FROM assignments WHERE task_id = 'T-1001'")
        other = "C-01" if first != "C-01" else "C-02"
        actions.assign_task(s.conn, "T-1001", other, "ops", now=s.now)
        self.assertEqual(s.rows("SELECT status FROM assignments WHERE task_id = 'T-1001' ORDER BY id"), [{"status": "cancelled"}, {"status": "offered"}])

    def test_cancel_supersedes_the_pending_request(self):
        s = self.s
        s.submit("T-1002")
        s.run_to(40)
        actions.cancel_task(s.conn, "T-1002", "ops", "no longer needed", now=s.now)
        self.assertEqual(s.status("T-1002"), "cancelled")
        self.assertEqual(s.one("SELECT status FROM routing_requests WHERE task_id = 'T-1002'"), "superseded")
        with self.assertRaises(actions.ActionError):
            actions.assign_task(s.conn, "T-1002", "C-03", "ops", now=s.now)


class TestRecovery(Base):
    def test_decline_raises_a_recovery_question_and_reroute_excludes_the_decliner(self):
        s = self.s
        s.submit("T-1004")                                                   # engine picks C-04, who declines
        s.run_to(45)
        self.assertEqual(s.status("T-1004"), "declined")
        a = s.alert("T-1004", "request_declined")
        self.assertEqual((a["severity"], a["status"]), ("normal", "open"))   # medium priority -> normal
        q = s.question("T-1004", "recovery")
        self.assertEqual(json.loads(q["context"])["contributor_id"], "C-04")
        s.answer(q["id"], "reroute_excluding_contributor")
        self.assertEqual(s.one("SELECT contributor_id FROM routing_exclusions WHERE task_id = 'T-1004'"), "C-04")
        s.run_to(120)
        self.assertIn(s.status("T-1004"), WORKING)
        self.assertNotEqual(s.one("SELECT contributor_id FROM assignments WHERE task_id = 'T-1004' AND status IN ('accepted', 'in_progress')"), "C-04")
        self.assertEqual(s.one("SELECT COUNT(*) FROM assignments WHERE task_id = 'T-1004'"), 2)      # history kept
        self.assertEqual(s.alert("T-1004", "request_declined")["status"], "resolved")

    def test_failed_assignment_is_raised_one_level_and_offer_to_next_works(self):
        s = self.s
        s.submit("T-1007")                                                   # engine picks C-02; assignment fails
        s.run_to(45)
        self.assertEqual(s.status("T-1007"), "failed")
        a = s.alert("T-1007", "assignment_failed")
        self.assertEqual(a["severity"], "high")                              # medium + 1
        self.assertEqual(s.one("SELECT COUNT(*) FROM outbox WHERE alert_id = ?", a["id"]), 1)
        s.answer(s.question("T-1007")["id"], "offer_to_next_eligible")
        s.run_to(60)
        self.assertIn(s.status("T-1007"), WORKING)
        self.assertEqual(s.one("SELECT contributor_id FROM assignments WHERE task_id = 'T-1007' AND status IN ('accepted', 'in_progress')"), "C-01")

    def test_contact_contributor_keeps_the_question_open(self):
        s = self.s
        s.submit("T-1004")
        s.run_to(45)
        q = s.question("T-1004")
        out = s.answer(q["id"], "contact_contributor", note="called, no answer")
        self.assertFalse(out.resolved)
        self.assertEqual(s.question("T-1004")["id"], q["id"])
        self.assertEqual(len(s.events("T-1004", "question_followup")), 1)
        self.assertEqual(s.status("T-1004"), "declined")

    def test_unanswered_recovery_applies_only_its_declared_default(self):
        s = self.s
        s.submit("T-1004")
        s.run_to(45)
        self.assertEqual(s.question("T-1004")["safe_default"], "reroute_excluding_contributor")
        s.run_to(45 + 3700, step=10)                                         # medium: 60 minute answer timeout
        self.assertEqual(s.one("SELECT COUNT(*) FROM assignments WHERE task_id = 'T-1004' AND status = 'cancelled'"), 0)
        self.assertIn(s.status("T-1004"), WORKING)                     # re-routed to someone else, not cancelled
        q = s.rows("SELECT * FROM operator_questions WHERE task_id = 'T-1004'")[0]
        self.assertEqual((q["status"], q["answered_by"]), ("answered", "RoutingAgent(safe_default)"))


class TestNoMatch(Base):
    def setUp(self):
        super().setUp()
        self.s.submit("T-1005")
        self.s.run_to(30)

    def test_no_match_is_reported_not_guessed(self):
        s = self.s
        self.assertEqual(s.status("T-1005"), "unmatched")
        self.assertEqual(s.one("SELECT json_extract(result, '$.outcome') FROM routing_requests WHERE task_id = 'T-1005'"), "no_match")
        self.assertEqual(s.alert("T-1005", "no_match")["severity"], "normal")
        self.assertEqual(json.loads(s.question("T-1005")["options"])[0], "lower_min_level_with_expert_review")

    def test_lowering_the_bar_requires_expert_review_and_is_recorded(self):
        s = self.s
        q = s.question("T-1005")
        with self.assertRaises(actions.ActionError):
            s.answer(q["id"], "lower_min_level_with_expert_review", min_level=4)       # not lower
        with self.assertRaises(actions.ActionError):
            s.answer(q["id"], "lower_min_level_with_expert_review")                    # missing param
        self.assertEqual(s.question("T-1005")["id"], q["id"])                          # still open after rejections
        s.answer(q["id"], "lower_min_level_with_expert_review", min_level=3)
        s.run_to(120)
        self.assertIn(s.status("T-1005"), WORKING)
        self.assertEqual(s.one("SELECT contributor_id FROM assignments WHERE task_id = 'T-1005' AND status IN ('accepted', 'in_progress')"), "C-09")
        self.assertIn("expert_review_required", json.loads(s.one("SELECT tags FROM tasks WHERE id = 'T-1005'")))
        change = json.loads(s.events("T-1005", "task_requirements_changed")[0]["payload"])
        self.assertEqual(change["min_level"], {"from": 4, "to": 3})

    def test_unanswered_default_is_hold_and_never_cancels(self):
        s = self.s
        s.run_to(30 + 7300, step=10)                                       # two timeouts at 60 min
        self.assertEqual(s.status("T-1005"), "unmatched")
        q = s.question("T-1005")
        self.assertGreaterEqual(q["safe_default_applied_count"], 2)

    def test_hold_for_availability_reroutes_when_someone_qualifies(self):
        s = self.s
        s.answer(s.question("T-1005")["id"], "hold_for_availability")
        s.run_to(60)
        self.assertEqual(s.status("T-1005"), "unmatched")
        s.conn.execute("UPDATE contributor_expertise SET level = 4 WHERE contributor_id = 'C-09' AND area_id = 'legal_compliance'")
        s.run_to(150)
        self.assertEqual(len(s.events("T-1005", "eligibility_restored")), 1)
        self.assertIn(s.status("T-1005"), WORKING)

    def test_split_creates_children_and_cancels_the_parent(self):
        s = self.s
        q = s.question("T-1005")
        with self.assertRaises(actions.ActionError):
            s.answer(q["id"], "split_task", children=[{"title": "only one", "estimated_effort_hours": 5}])
        s.answer(q["id"], "split_task", children=[{"title": "Retention schedule review", "estimated_effort_hours": 20},
                                                  {"title": "Legal-basis review", "estimated_effort_hours": 20}])
        self.assertEqual(s.status("T-1005"), "cancelled")
        kids = s.rows("SELECT id, status, parent_task_id, min_level FROM tasks WHERE parent_task_id = 'T-1005' ORDER BY id")
        self.assertEqual([k["id"] for k in kids], ["T-1005-1", "T-1005-2"])
        self.assertTrue(all(k["parent_task_id"] == "T-1005" and k["status"] == "submitted" for k in kids))


class TestCapacity(Base):
    def prepare(self):
        """C-03 (1 task, 40h) already holds T-1002 with a confirmed 12h estimate; T-1010 then arrives."""
        s = self.s
        actions.assign_task(s.conn, "T-1002", "C-03", "ops", now=s.now)
        s.run_to(10)
        self.assertIn(s.status("T-1002"), WORKING)
        s.submit("T-1010")
        s.run_to(60)

    def test_over_capacity_is_logged_and_queued_with_a_resource_warning(self):
        s = self.s
        self.prepare()
        self.assertEqual(s.status("T-1010"), "queued")
        self.assertEqual(s.one("SELECT COUNT(*) FROM assignments WHERE task_id = 'T-1010'"), 0)
        a = s.alert("T-1010", "capacity_conflict")
        self.assertEqual((a["severity"], a["status"]), ("low", "open"))      # medium priority, capacity_conflict adjusts down one
        ctx = json.loads(s.question("T-1010", "capacity_conflict")["context"])
        self.assertEqual(ctx["reasons_list"], ["2 tasks > max 1", "49.5h > 40h available"])
        self.assertIn("requester estimate x1.25, unconfirmed", ctx["estimate_basis"])
        self.assertIn("current load 12h across 1 task(s)", ctx["estimate_basis"])
        self.assertEqual(len(s.events("T-1010", "capacity_conflict")), 1)    # logged once, not on every tick

    def test_raising_hours_releases_the_queue(self):
        s = self.s
        self.prepare()
        with self.assertRaises(actions.ActionError):
            s.answer(s.question("T-1010")["id"], "raise_contributor_hours")           # needs params
        s.answer(s.question("T-1010")["id"], "raise_contributor_hours", available_hours=60, max_concurrent_tasks=2)
        s.run_to(100)
        self.assertIn(s.status("T-1010"), WORKING)
        self.assertEqual(s.one("SELECT contributor_id FROM assignments WHERE task_id = 'T-1010'"), "C-03")
        self.assertEqual(s.alert("T-1010", "capacity_conflict")["status"], "resolved")

    def test_next_eligible_is_rejected_when_nobody_else_fits_and_nothing_changes(self):
        s = self.s
        self.prepare()
        q = s.question("T-1010")
        with self.assertRaises(actions.ActionError):
            s.answer(q["id"], "next_eligible_contributor")                                # C-03 is the only eligible contributor
        self.assertEqual((s.question("T-1010")["id"], s.status("T-1010")), (q["id"], "queued"))

    def test_assign_anyway_overrides_and_is_logged(self):
        s = self.s
        self.prepare()
        s.answer(s.question("T-1010")["id"], "assign_anyway")
        self.assertEqual(s.status("T-1010"), "offered")
        self.assertEqual(len(s.events("T-1010", "capacity_override")), 1)
        warn = json.loads(s.events("T-1010", "assignment_offered")[0]["payload"])["warnings"]
        self.assertTrue(any(w.startswith("capacity:") for w in warn))

    def test_wait_for_capacity_then_auto_offer_when_a_slot_frees(self):
        s = self.s
        self.prepare()
        s.answer(s.question("T-1010")["id"], "wait_for_capacity")
        s.run_to(400, step=5)
        self.assertEqual(s.status("T-1010"), "queued")                                    # operator chose to wait: no nagging
        self.assertEqual(s.one("SELECT COUNT(*) FROM alerts WHERE task_id = 'T-1010' AND status <> 'resolved'"), 0)
        s.conn.execute("UPDATE assignments SET status = 'completed' WHERE task_id = 'T-1002'")
        s.conn.execute("UPDATE tasks SET status = 'completed' WHERE id = 'T-1002'")
        s.run_to(420)
        self.assertIn(s.status("T-1010"), WORKING)

    def test_estimate_debt_makes_a_contributor_capacity_unknown(self):
        s = self.s
        a1 = s.conn.execute("INSERT INTO assignments (task_id, contributor_id, source, status, offered_at, plan_health) "
                            "VALUES ('T-1006', 'C-10', 'operator', 'accepted', ?, 'overdue_estimate')", (clock.iso(s.now),)).lastrowid
        s.conn.execute("INSERT INTO assignments (task_id, contributor_id, source, status, offered_at, plan_health) "
                       "VALUES ('T-1004', 'C-10', 'operator', 'accepted', ?, 'overdue_estimate')", (clock.iso(s.now),))
        cfg = config.load(s.conn)
        chk = routing.capacity.check(s.conn, cfg, "C-10", tasks.get(s.conn, "T-1008"))
        self.assertFalse(chk["fits"])
        self.assertTrue(any(r.startswith("estimate debt") for r in chk["reasons"]))
        ranked = routing.capacity.eligible(s.conn, cfg, tasks.get(s.conn, "T-1004"))
        self.assertFalse(next(e for e in ranked if e["id"] == "C-10")["fits"])          # ranked behind contributors who fit
        self.assertTrue(ranked[0]["fits"])


class TestContention(Base):
    def prepare(self):
        """Only C-03 can take T-1002 or T-1010. C-03 is full (T-1009), both queue, then C-03 frees up."""
        s = self.s
        s.conn.execute("UPDATE contributors SET availability = 'away' WHERE id IN ('C-04', 'C-09')")
        s.conn.execute("UPDATE tasks SET sim = ? WHERE id = 'T-1002'", (json.dumps({"routing": "normal", "contributor_response": "accept"}),))
        actions.assign_task(s.conn, "T-1009", "C-03", "ops", now=s.now)
        s.run_to(10)
        s.submit("T-1002")
        s.submit("T-1010")
        s.run_to(60)
        self.assertEqual((s.status("T-1002"), s.status("T-1010")), ("queued", "queued"))
        s.conn.execute("UPDATE assignments SET status = 'completed' WHERE task_id = 'T-1009'")
        s.conn.execute("UPDATE tasks SET status = 'completed' WHERE id = 'T-1009'")
        s.run_to(65)

    def test_two_tasks_that_fit_alone_but_not_together_ask_the_operator(self):
        s = self.s
        self.prepare()
        q = s.question("T-1002", "priority_contention")
        self.assertIsNotNone(q)
        ctx = json.loads(q["context"])
        self.assertEqual((ctx["task_ids"], ctx["contributor_id"]), (["T-1002", "T-1010"], "C-03"))
        self.assertEqual(s.alert("T-1002", "priority_contention")["severity"], "critical")
        self.assertEqual((s.status("T-1002"), s.status("T-1010")), ("queued", "queued"))   # held, not silently picked
        self.assertEqual(s.one("SELECT COUNT(*) FROM assignments WHERE status IN ('offered', 'accepted')"), 0)

    def test_operator_answer_decides_who_goes_first(self):
        s = self.s
        self.prepare()
        s.answer(s.question("T-1002", "priority_contention")["id"], "pick_explicitly", task_id="T-1010")
        self.assertEqual((s.status("T-1010"), s.status("T-1002")), ("offered", "queued"))   # operator overrode priority

    def test_by_priority_answer_offers_the_critical_task(self):
        s = self.s
        self.prepare()
        s.answer(s.question("T-1002", "priority_contention")["id"], "by_priority")
        self.assertEqual((s.status("T-1002"), s.status("T-1010")), ("offered", "queued"))
        s.run_to(100)
        self.assertIn(s.status("T-1002"), WORKING)
        self.assertIsNotNone(s.alert("T-1010", "capacity_conflict"))                      # the loser is told why it waits

    def test_unanswered_contention_falls_back_to_priority_after_the_timeout(self):
        s = self.s
        self.prepare()
        s.run_to(65 + 305, step=5)                                                        # critical answer timeout: 5 minutes
        self.assertIn(s.status("T-1002"), WORKING)
        self.assertEqual(s.status("T-1010"), "queued")
        q = s.rows("SELECT status, answered_by FROM operator_questions WHERE type = 'priority_contention'")[0]
        self.assertEqual((q["status"], q["answered_by"]), ("answered", "RoutingAgent(safe_default)"))


class TestRestartRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "t.db"

    def tearDown(self):
        clock.freeze(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def restart(self, old: Sys) -> Sys:
        sec = old.sec
        old.conn.close()
        new = Sys(self.path, fresh=False)
        new.sec = sec
        self.addCleanup(new.conn.close)
        return new

    def test_pending_request_survives_a_restart_and_is_not_duplicated(self):
        a = Sys(self.path)
        a.submit("T-1001")
        a.run_to(3)
        returns_at = a.one("SELECT sim_returns_at FROM routing_requests WHERE task_id = 'T-1001'")
        b = self.restart(a)
        found = b.agent.recover(b.now)
        self.assertEqual(found["pending_requests"], 1)
        self.assertEqual(b.one("SELECT sim_returns_at FROM routing_requests WHERE task_id = 'T-1001'"), returns_at)   # outcome not re-rolled
        b.run_to(60)
        self.assertIn(b.status("T-1001"), WORKING)
        self.assertEqual(b.one("SELECT COUNT(*) FROM routing_requests WHERE task_id = 'T-1001'"), 1)
        self.assertEqual(b.one("SELECT COUNT(*) FROM events WHERE event_type = 'agent_started'"), 1)

    def test_restart_after_a_stuck_flag_does_not_raise_it_twice(self):
        a = Sys(self.path)
        a.submit("T-1002")
        a.run_to(40)
        b = self.restart(a)
        b.agent.recover(b.now)
        b.run_to(120)
        self.assertEqual(b.one("SELECT COUNT(*) FROM alerts WHERE task_id = 'T-1002' AND trigger = 'stuck_request'"), 1)
        self.assertEqual(b.one("SELECT COUNT(*) FROM routing_requests WHERE task_id = 'T-1002'"), 1)

    def test_a_request_that_came_due_while_the_app_was_down_is_handled_on_start(self):
        a = Sys(self.path)
        a.submit("T-1001")
        a.run_to(2)
        b = self.restart(a)
        b.sec = 600                                                       # ten minutes of downtime
        self.assertEqual(b.agent.recover(b.now)["already_due"], 1)
        b.run_to(620)
        self.assertIn(b.status("T-1001"), WORKING)

    def test_second_submit_of_the_same_task_cannot_create_a_second_pending_request(self):
        a = Sys(self.path)
        a.submit("T-1001")
        a.run_to(2)
        with self.assertRaises(Exception):
            a.conn.execute("INSERT INTO routing_requests (task_id, attempt, started_at, eligible) VALUES ('T-1001', 9, ?, '[]')", (clock.iso(a.now),))
        with self.assertRaises(actions.ActionError):
            a.submit("T-1001")


class TestEngineAndRules(Base):
    def test_default_engine_hangs_about_a_fifth_of_the_time(self):
        cfg = config.load(self.s.conn)
        task = {"sim": None}
        rng = random.Random(11)
        hangs = sum(simulator.plan_engine(task, [{"id": "C-01"}], cfg, rng, T0)[0] is None for _ in range(2000))
        self.assertTrue(0.17 <= hangs / 2000 <= 0.23, hangs / 2000)

    def test_latency_is_between_5_and_20_seconds(self):
        cfg = config.load(self.s.conn)
        rng = random.Random(5)
        for _ in range(200):
            at, _ = simulator.plan_engine({"sim": json.dumps({"routing": "normal"})}, [{"id": "C-01"}], cfg, rng, T0)
            self.assertTrue(5 <= (clock.parse(at) - T0).total_seconds() <= 20)

    def test_invalid_transitions_are_refused(self):
        with self.assertRaises(tasks.InvalidTransition):
            tasks.move(self.s.conn, "T-1001", "accepted", "x", now=T0)         # draft -> accepted
        self.s.submit("T-1001")
        with self.assertRaises(actions.ActionError):
            self.s.submit("T-1001")

    def test_every_routing_question_option_has_a_handler_branch(self):
        cfg = config.load(self.s.conn)
        self.assertEqual(set(routing.HANDLERS), alerts.ROUTING_QUESTIONS)
        for qtype, fn in routing.HANDLERS.items():
            src = inspect.getsource(fn)
            for option in cfg["operator_questions"]["types"][qtype]["options"]:
                self.assertIn(f'"{option}"', src, f"{qtype}: option {option} has no branch")

    def test_safe_defaults_never_reassign_or_cancel(self):
        cfg = config.load(self.s.conn)
        for qtype in alerts.ROUTING_QUESTIONS:
            self.assertNotIn(cfg["operator_questions"]["types"][qtype]["safe_default"],
                             {"cancel_task", "assign_anyway", "assign_manually", "reassign_task", "resubmit_routing"})


if __name__ == "__main__":
    unittest.main()
