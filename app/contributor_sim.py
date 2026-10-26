"""
Simulated contributors. They answer offers, estimate requests and status check-ins a few seconds after being asked,
as scripted by each task's `sim` block (accept, with the requester's number as the estimate, if nothing is scripted).

A contributor can be switched to MANUAL (settings.manual_contributors): the simulator then leaves them alone so a person
can answer from the contributor view, which is how the demo shows accept-with-estimate by hand.
"""
import json
from datetime import timedelta

from . import assignments, clock, tasks, tracking
from .db import transaction
from .simulator import _sim

RESPONSE_DELAY_SECONDS = 3
_ACTIVE = "('accepted', 'in_progress')"


def manual_ids(conn) -> set[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = 'manual_contributors'").fetchone()
    return set(json.loads(row[0])) if row else set()


def set_manual(conn, contributor_id: str, manual: bool) -> None:
    ids = manual_ids(conn)
    (ids.add if manual else ids.discard)(contributor_id)
    conn.execute("INSERT INTO settings (key, value) VALUES ('manual_contributors', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (json.dumps(sorted(ids)),))


def scripted_response(conn, task, assignment) -> str:
    """accept | decline | fail. The script targets the forced_pick contributor if there is one, otherwise the task's
    first assignment, so a recovery re-offer to someone else is not scripted to fail again."""
    sim = _sim(task)
    response = sim.get("contributor_response", "accept")
    forced = sim.get("forced_pick")
    if forced:
        applies = assignment["contributor_id"] == forced
    else:
        first = conn.execute("SELECT MIN(id) FROM assignments WHERE task_id = ?", (task["id"],)).fetchone()[0]
        applies = assignment["id"] == first
    return response if applies else "accept"


class ContributorSimulator:
    def __init__(self, conn):
        self.conn = conn

    def tick(self, cfg: dict, now) -> int:
        cutoff = clock.iso(now - timedelta(seconds=RESPONSE_DELAY_SECONDS))
        manual = manual_ids(self.conn)
        return (self._offers(cfg, now, cutoff, manual) + self._estimates(cfg, now, cutoff, manual)
                + self._start_work(now, cutoff, manual) + self._status_checks(cfg, now, cutoff, manual))

    def _offers(self, cfg, now, cutoff, manual) -> int:
        n = 0
        for a in self.conn.execute("SELECT id FROM assignments WHERE status = 'offered' AND offered_at <= ? ORDER BY id", (cutoff,)).fetchall():
            with transaction(self.conn):
                a = self.conn.execute("SELECT * FROM assignments WHERE id = ?", (a["id"],)).fetchone()
                if a["status"] != "offered" or a["contributor_id"] in manual:
                    continue
                task = tasks.get(self.conn, a["task_id"])
                actor = f"contributor:{a['contributor_id']}"
                response = scripted_response(self.conn, task, a)
                if response == "decline":
                    assignments.decline(self.conn, a["id"], "Declined: not enough capacity this week", actor, now)
                elif response == "fail":
                    assignments.fail(self.conn, a["id"], "Assignment failed: contributor became unavailable", actor, now)
                else:
                    est = _sim(task).get("estimate") or {"mode": "with_accept"}
                    if est["mode"] == "with_accept":
                        tracking.accept_with_estimate(self.conn, cfg, a["id"], actor, now, hours_total=est.get("hours_total", task["estimated_effort_hours"]),
                                                      confidence=est.get("confidence", "medium"), basis=est.get("basis", "rough_guess"))
                    else:   # after_reminder / never: accepted with no estimate; the TrackingAgent chases it
                        tracking.accept_with_estimate(self.conn, cfg, a["id"], actor, now)
            n += 1
        return n

    def _estimates(self, cfg, now, cutoff, manual) -> int:
        n = 0
        rows = self.conn.execute(
            f"SELECT er.id, er.assignment_id, er.sequence FROM estimate_requests er JOIN assignments a ON a.id = er.assignment_id "
            f"WHERE er.answered_at IS NULL AND a.status IN {_ACTIVE} AND er.sent_at <= ? ORDER BY er.id", (cutoff,)).fetchall()
        for r in rows:
            with transaction(self.conn):
                a = self.conn.execute("SELECT * FROM assignments WHERE id = ?", (r["assignment_id"],)).fetchone()
                if a["contributor_id"] in manual or tracking.latest_revision(self.conn, a["id"]) is not None:
                    continue
                task = tasks.get(self.conn, a["task_id"])
                est = _sim(task).get("estimate") or {"mode": "with_accept"}
                if est["mode"] == "never" or (est["mode"] == "after_reminder" and r["sequence"] < est.get("after_reminders", 1)):
                    continue
                tracking.submit_estimate(self.conn, cfg, a["id"], hours_total=est.get("hours_total", task["estimated_effort_hours"]),
                                         confidence=est.get("confidence", "medium"), basis=est.get("basis", "rough_guess"),
                                         actor=f"contributor:{a['contributor_id']}", now=now)
            n += 1
        return n

    def _start_work(self, now, cutoff, manual) -> int:
        n = 0
        rows = self.conn.execute("SELECT id FROM assignments WHERE status = 'accepted' AND started_at IS NULL AND decided_at <= ? "
                                 "AND EXISTS (SELECT 1 FROM estimate_revisions e WHERE e.assignment_id = assignments.id) ORDER BY id", (cutoff,)).fetchall()
        for r in rows:
            with transaction(self.conn):
                a = self.conn.execute("SELECT * FROM assignments WHERE id = ?", (r["id"],)).fetchone()
                if a["status"] == "accepted" and a["contributor_id"] not in manual:
                    assignments.start(self.conn, a["id"], f"contributor:{a['contributor_id']}", now)
                    n += 1
        return n

    def _status_checks(self, cfg, now, cutoff, manual) -> int:
        n = 0
        rows = self.conn.execute(
            f"SELECT s.id, s.assignment_id FROM status_checks s JOIN assignments a ON a.id = s.assignment_id "
            f"WHERE s.replied_at IS NULL AND s.status IS NULL AND a.status IN {_ACTIVE} AND s.sent_at <= ? ORDER BY s.id", (cutoff,)).fetchall()
        for r in rows:
            with transaction(self.conn):
                a = self.conn.execute("SELECT * FROM assignments WHERE id = ?", (r["assignment_id"],)).fetchone()
                if a["contributor_id"] in manual:
                    continue
                script = _sim(tasks.get(self.conn, a["task_id"])).get("status_check") or {}
                if script.get("reply") == "no_response":
                    continue
                rev = tracking.latest_revision(self.conn, a["id"])
                default_remaining = round(rev["hours_remaining"] * 0.6, 1) if rev else None
                tracking.submit_status(self.conn, cfg, r["id"], status=script.get("reply", "on_track"),
                                       progress_percent=script.get("progress_percent", 50),
                                       hours_remaining=script.get("hours_remaining", default_remaining),
                                       note=script.get("note"), actor=f"contributor:{a['contributor_id']}", now=now)
            n += 1
        return n
