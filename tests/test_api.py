"""HTTP API against a real server on an ephemeral port (agents off, so every step is deterministic)."""
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import alerts, assignments, clock, config, tasks
from app.db import ROOT, connect, init_schema, transaction
from app.seed import seed
from app.server import make_server


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "api.db"
        conn = connect(cls.db)
        init_schema(conn)
        seed(conn)
        conn.close()
        cls.server = make_server(cls.db, port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None, raw=False, operator="Dana"):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Operator": operator})
        try:
            with urllib.request.urlopen(req) as r:
                data = r.read().decode("utf-8")
                return r.status, (data if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            data = e.read().decode("utf-8")
            return e.code, (data if raw else json.loads(data))

    def db_conn(self):
        return connect(self.db)

    def new_task(self, **over):
        body = {"title": "Reconcile petty cash", "area_id": "finance", "min_level": 2, "priority": "high", "estimated_effort_hours": 6, "due_in_hours": 48,
                "user_story": {"as_a": "controller", "i_want": "petty cash reconciled", "so_that": "the audit is clean"}, "description": "Reconcile Q3 petty cash.",
                "attachments": [{"kind": "requirements", "filename": "requirements.md", "content": "# Requirements\nReconcile all vouchers."},
                                {"kind": "acceptance_criteria", "filename": "acceptance_criteria.md", "content": "- totals match the ledger"}]}
        body.update(over)
        return self.call("POST", "/api/tasks", body)


class TestReads(ApiCase):
    def test_board_lists_every_task_with_age_and_state(self):
        status, b = self.call("GET", "/api/board")
        self.assertEqual(status, 200)
        by_id = {t["id"]: t for t in b["tasks"]}
        self.assertTrue({"T-1001", "T-1010"} <= set(by_id))
        t = by_id["T-1006"]
        self.assertEqual((t["status"], t["priority"], t["assignee"]), ("draft", "low", None))
        self.assertGreaterEqual(t["age_minutes"], 1499)                           # time since created

    def test_simulation_scripting_is_never_exposed(self):
        _, board = self.call("GET", "/api/board", raw=True)
        _, detail = self.call("GET", "/api/tasks/T-1002", raw=True)
        for text in (board, detail):
            for secret in ("forced_pick", "late_return_seconds", "sim_returns_at", "sim_pick", "contributor_response", "unresolved_routing"):
                self.assertNotIn(secret, text)

    def test_task_detail_has_story_attachments_and_candidates(self):
        status, d = self.call("GET", "/api/tasks/T-1005")
        self.assertEqual(status, 200)
        self.assertEqual(d["task"]["story_as_a"], "privacy officer")
        self.assertEqual({a["kind"] for a in d["attachments"]}, {"requirements", "acceptance_criteria"})
        by_c = {c["id"]: c for c in d["contributors"]}
        self.assertFalse(by_c["C-09"]["eligible"])                                  # level 3 < required 4
        self.assertFalse(by_c["C-11"]["eligible"])                                  # away
        self.assertEqual(self.call("GET", "/api/tasks/T-9999")[0], 404)

    def test_attachment_content_and_traversal_guard(self):
        status, text = self.call("GET", "/api/attachments/A-1001-1", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("T-1001 Requirements", text)
        self.assertEqual(self.call("GET", "/api/attachments/nope")[0], 404)
        conn = self.db_conn()
        conn.execute("INSERT INTO task_attachments (id, task_id, kind, filename, path, size_bytes, sha256) VALUES ('A-evil', 'T-1001', 'reference', 'x', '../../../Windows/win.ini', 1, 'x')")
        conn.close()
        self.assertEqual(self.call("GET", "/api/attachments/A-evil")[0], 404)

    def test_static_ui_and_unknown_routes(self):
        status, html = self.call("GET", "/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("Operator Console", html)
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)
        req = urllib.request.Request(self.base + "/api/tasks", method="POST", data=b"not json", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_meta_and_contributors(self):
        _, m = self.call("GET", "/api/meta")
        self.assertEqual(len(m["areas"]), 6)
        self.assertEqual(m["stuck_threshold_seconds"]["critical"], 25)
        _, c = self.call("GET", "/api/contributors")
        c03 = next(x for x in c["contributors"] if x["id"] == "C-03")
        self.assertEqual((c03["max_concurrent_tasks"], c03["available_hours"], c03["active_tasks"]), (1, 40, 0))
        self.assertEqual(c03["expertise"][0]["level"], 4)


class TestCreateAndIntervene(ApiCase):
    def test_create_requires_requirements_and_acceptance_criteria(self):
        s, r = self.new_task(attachments=[{"kind": "requirements", "filename": "r.md", "content": "x"}])
        self.assertEqual(s, 400)
        self.assertIn("acceptance criteria", r["error"])
        self.assertEqual(self.new_task(priority="urgent")[0], 400)
        self.assertEqual(self.new_task(area_id="astrology")[0], 400)
        self.assertEqual(self.new_task(min_level=0)[0], 400)
        self.assertEqual(self.new_task(estimated_effort_hours=-3)[0], 400)

    def test_uploads_are_stored_beside_the_database_in_use(self):
        s, r = self.new_task(title="Upload location")
        self.assertTrue((self.tmp / "uploads" / r["id"] / "requirements.md").is_file())          # beside the test database
        self.assertFalse((ROOT / "var" / "uploads" / r["id"]).exists())                            # the real runtime folder is untouched

    def test_create_draft_then_submit(self):
        s, r = self.new_task(title="Draft one")
        self.assertEqual((s, r["status"]), (200, "draft"))
        _, d = self.call("GET", f"/api/tasks/{r['id']}")
        self.assertEqual(len(d["attachments"]), 2)
        _, text = self.call("GET", f"/api/attachments/{d['attachments'][0]['id']}", raw=True)
        self.assertTrue(text.startswith("#") or text.startswith("-"))
        self.assertEqual(self.call("POST", f"/api/tasks/{r['id']}/submit", {})[0], 200)
        self.assertEqual(self.call("GET", f"/api/tasks/{r['id']}")[1]["task"]["status"], "submitted")
        s, err = self.call("POST", f"/api/tasks/{r['id']}/submit", {})
        self.assertEqual(s, 400)
        self.assertIn("only drafts", err["error"])

    def test_create_with_submit_flag_and_operator_is_recorded(self):
        s, r = self.new_task(title="Submit on create", submit=True, sim_routing="hang")
        self.assertEqual(r["status"], "submitted")
        _, d = self.call("GET", f"/api/tasks/{r['id']}")
        created = next(e for e in d["events"] if e["event_type"] == "task_created")
        self.assertEqual(created["actor"], "operator:Dana")
        self.assertNotIn('"hang"', json.dumps(d))                                    # the demo control stays hidden
        self.assertNotIn('"sim"', json.dumps(d))

    def test_assign_warns_but_proceeds_and_reassign_cancels_the_old_assignment(self):
        s, r = self.call("POST", "/api/tasks/T-1005/assign", {"contributor_id": "C-11"})
        self.assertEqual(s, 200)
        self.assertIn("not eligible by area/level/availability", r["warnings"])
        s, r = self.call("POST", "/api/tasks/T-1005/assign", {"contributor_id": "C-09"})
        _, d = self.call("GET", "/api/tasks/T-1005")
        self.assertEqual([a["status"] for a in d["assignments"]], ["cancelled", "offered"])
        self.assertEqual(self.call("POST", "/api/tasks/T-1005/assign", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/tasks/T-1005/assign", {"contributor_id": "C-99"})[0], 400)

    def test_cancel(self):
        _, r = self.new_task(title="to cancel", submit=True)
        self.assertEqual(self.call("POST", f"/api/tasks/{r['id']}/cancel", {"reason": "duplicate"})[0], 200)
        self.assertEqual(self.call("GET", f"/api/tasks/{r['id']}")[1]["task"]["status"], "cancelled")
        self.assertEqual(self.call("POST", f"/api/tasks/{r['id']}/cancel", {})[0], 400)

    def test_availability_change_is_logged(self):
        self.assertEqual(self.call("POST", "/api/contributors/C-06/availability", {"availability": "away", "away_reason": "sick"})[0], 200)
        self.assertEqual(self.call("POST", "/api/contributors/C-06/availability", {"availability": "asleep"})[0], 400)
        self.assertEqual(self.call("POST", "/api/contributors/C-99/availability", {"availability": "away"})[0], 404)
        self.call("POST", "/api/contributors/C-06/availability", {"availability": "available"})
        conn = self.db_conn()
        n = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'contributor_availability_changed'").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(n, 2)


class TestQuestionsAndAlerts(ApiCase):
    def raise_no_match(self):
        conn = self.db_conn()
        cfg = config.load(conn)
        with transaction(conn):
            tasks.move(conn, "T-1009", "submitted", "x", now=clock.now())
            tasks.move(conn, "T-1009", "routing", "x", now=clock.now())
            tasks.move(conn, "T-1009", "unmatched", "x", now=clock.now())
            alerts.raise_alert(conn, cfg, trigger="no_match", task=tasks.get(conn, "T-1009"), now=clock.now(), message="No contributor matches T-1009",
                               dedupe_key="no_match:T-1009:api", context={"task": "T-1009 (x)", "task_id": "T-1009", "area": "data_analysis", "min_level": 2})
        conn.close()

    def test_inbox_answer_and_ack_flow(self):
        self.raise_no_match()
        _, inbox = self.call("GET", "/api/inbox")
        q = next(x for x in inbox["questions"] if x["task_id"] == "T-1009")
        al = next(a for a in inbox["alerts"] if a["task_id"] == "T-1009")
        self.assertEqual((q["type"], q["safe_default"], al["severity"]), ("no_match", "hold_for_availability", "low"))
        s, err = self.call("POST", f"/api/questions/{q['id']}/answer", {"option": "bake_a_cake"})
        self.assertEqual(s, 400)
        self.assertIn("not an option", err["error"])
        s, err = self.call("POST", f"/api/questions/{q['id']}/answer", {"option": "lower_min_level_with_expert_review", "params": {"min_level": 5}})
        self.assertEqual(s, 400)                                                      # rejected: nothing changed
        self.assertEqual(self.call("GET", "/api/tasks/T-1009")[1]["task"]["min_level"], 2)
        self.assertEqual(self.call("POST", f"/api/alerts/{al['id']}/ack", {})[0], 200)
        s, r = self.call("POST", f"/api/questions/{q['id']}/answer", {"option": "hold_for_availability", "note": "wait for C-05"})
        self.assertEqual((s, r["resolved"]), (200, True))
        self.assertEqual(self.call("POST", f"/api/questions/{q['id']}/answer", {"option": "hold_for_availability"})[0], 400)   # already answered
        _, d = self.call("GET", "/api/tasks/T-1009")
        self.assertEqual(d["task"]["hold_reason"], "availability")
        self.assertIn("operator:Dana", [e["actor"] for e in d["events"] if e["event_type"] == "question_answered"])


class TestContributorWorkflow(ApiCase):
    def test_manual_contributor_must_give_an_estimate_to_accept_then_works(self):
        self.assertEqual(self.call("POST", "/api/contributors/C-05/manual", {"manual": True})[0], 200)
        self.call("POST", "/api/tasks/T-1003/assign", {"contributor_id": "C-05"})
        _, inbox = self.call("GET", "/api/contributors/C-05/inbox")
        offer = next(o for o in inbox["offers"] if o["task_id"] == "T-1003")
        s, err = self.call("POST", f"/api/assignments/{offer['id']}/accept", {})
        self.assertEqual(s, 400)                                                     # capture at accept: no estimate, no acceptance
        self.assertIn("hours_total", err["error"])
        self.assertEqual(self.call("POST", f"/api/assignments/{offer['id']}/accept", {"hours_total": 20, "confidence": "maybe"})[0], 400)
        s, _ = self.call("POST", f"/api/assignments/{offer['id']}/accept", {"hours_total": 20, "confidence": "high", "basis": "spec_review"})
        self.assertEqual(s, 200)
        _, d = self.call("GET", "/api/tasks/T-1003")
        a = d["assignments"][-1]
        self.assertEqual((d["task"]["status"], a["plan_health"], a["revisions"][0]["hours_total"], a["revisions"][0]["submitted_by"]), ("accepted", "complete", 20, "contributor:C-05"))
        self.assertEqual(self.call("POST", f"/api/assignments/{a['id']}/start", {})[0], 200)
        _, board = self.call("GET", "/api/board")
        row = next(t for t in board["tasks"] if t["id"] == "T-1003")
        self.assertEqual((row["status"], row["confirmed_hours"]["total"], row["assignee"]), ("in_progress", 20, "Aisha Bello"))
        self.assertEqual(self.call("POST", f"/api/assignments/{a['id']}/complete", {"note": "done"})[0], 200)
        self.assertEqual(self.call("GET", "/api/tasks/T-1003")[1]["task"]["status"], "completed")

    def test_start_is_refused_without_an_estimate(self):
        conn = self.db_conn()
        cfg = config.load(conn)
        with transaction(conn):
            aid = assignments.offer(conn, "T-1004", "C-08", "operator", "operator:x", clock.now())
            assignments.accept(conn, cfg, aid, "contributor:C-08", clock.now())
        conn.close()
        s, err = self.call("POST", f"/api/assignments/{aid}/start", {})
        self.assertEqual(s, 400)
        self.assertIn("estimate", err["error"])
        s, _ = self.call("POST", f"/api/assignments/{aid}/estimate", {"hours_total": 25, "confidence": "medium", "basis": "spec_review"})
        self.assertEqual(s, 200)
        self.assertEqual(self.call("POST", f"/api/assignments/{aid}/start", {})[0], 200)

    def test_decline_and_status_reply_validation(self):
        self.call("POST", "/api/contributors/C-02/manual", {"manual": True})
        self.call("POST", "/api/tasks/T-1007/assign", {"contributor_id": "C-02"})
        _, inbox = self.call("GET", "/api/contributors/C-02/inbox")
        offer = next(o for o in inbox["offers"] if o["task_id"] == "T-1007")
        self.assertEqual(self.call("POST", f"/api/assignments/{offer['id']}/decline", {"reason": "on leave next week"})[0], 200)
        _, d = self.call("GET", "/api/tasks/T-1007")
        self.assertEqual((d["task"]["status"], d["assignments"][-1]["end_reason"]), ("declined", "on leave next week"))
        self.assertEqual(self.call("POST", "/api/status-checks/9999/reply", {"status": "on_track"})[0], 404)

    def test_status_reply_updates_remaining_hours_and_raises_at_risk(self):
        conn = self.db_conn()
        cfg = config.load(conn)
        with transaction(conn):
            aid = assignments.offer(conn, "T-1008", "C-07", "operator", "operator:x", clock.now())
            from app import tracking
            tracking.accept_with_estimate(conn, cfg, aid, "contributor:C-07", clock.now(), hours_total=16, confidence="high")
            cid = conn.execute("INSERT INTO status_checks (assignment_id, sent_at, due_at) VALUES (?, ?, ?)", (aid, clock.iso(clock.now()), clock.iso(clock.ahead(1)))).lastrowid
        conn.close()
        self.assertEqual(self.call("POST", f"/api/status-checks/{cid}/reply", {})[0], 400)
        self.assertEqual(self.call("POST", f"/api/status-checks/{cid}/reply", {"status": "fine"})[0], 400)
        s, _ = self.call("POST", f"/api/status-checks/{cid}/reply", {"status": "at_risk", "progress_percent": 30, "hours_remaining": 14, "note": "vendor data late"})
        self.assertEqual(s, 200)
        _, d = self.call("GET", "/api/tasks/T-1008")
        self.assertEqual(d["assignments"][-1]["revisions"][-1]["source"], "check_in")
        self.assertTrue(any(a["trigger"] == "status_at_risk" and a["status"] == "open" for a in d["alerts"]))
        self.assertEqual(self.call("POST", f"/api/status-checks/{cid}/reply", {"status": "on_track"})[0], 400)     # already answered


class TestReportEndpoint(ApiCase):
    def test_report_generates_and_renders(self):
        s, r = self.call("GET", "/api/report")
        self.assertEqual(s, 200)
        self.assertIn("estimate_quality", r["report"])
        self.assertIn("DAILY REPORT", r["text"])
        self.assertEqual(self.call("GET", "/api/report?date=yesterday")[0], 400)


if __name__ == "__main__":
    unittest.main()
