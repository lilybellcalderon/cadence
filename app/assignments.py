"""Assignment lifecycle. The partial unique index on assignments is the final guard for 'one active assignee'."""
from . import clock, config, events, tasks

ACTIVE = ("offered", "accepted", "in_progress")


def offer(conn, task_id: str, contributor_id: str, source: str, actor: str, now, *, routing_request_id=None, note=None, warnings=None) -> int:
    cur = conn.execute("INSERT INTO assignments (task_id, contributor_id, routing_request_id, source, offered_at) VALUES (?, ?, ?, ?, ?)",
                       (task_id, contributor_id, routing_request_id, source, clock.iso(now)))
    aid = cur.lastrowid
    conn.execute("UPDATE tasks SET hold_reason = NULL WHERE id = ?", (task_id,))
    tasks.move(conn, task_id, "offered", actor, now=now, reason=note, contributor_id=contributor_id)
    events.log(conn, "assignment_offered", actor, task_id=task_id, assignment_id=aid, now=now,
               payload={"contributor_id": contributor_id, "source": source, "note": note, "warnings": warnings or []})
    return aid


def accept(conn, cfg, assignment_id: int, actor: str, now) -> None:
    """Accept an offer. The assignment starts as 'awaiting_estimate' with its SLA deadline set; use
    tracking.accept_with_estimate to accept and confirm the estimate in one step (DESIGN 9.2)."""
    a = _get(conn, assignment_id, "offered")
    t = tasks.get(conn, a["task_id"])
    sla = cfg["estimates"]["sla_minutes_by_priority"][t["priority"]]
    due = clock.iso(now + config.sim_minutes(cfg, sla["estimate_due"]))
    conn.execute("UPDATE assignments SET status = 'accepted', decided_at = ?, estimate_due_at = ?, plan_health = 'awaiting_estimate', "
                 "plan_health_updated_at = ? WHERE id = ?", (clock.iso(now), due, clock.iso(now), assignment_id))
    tasks.move(conn, a["task_id"], "accepted", actor, now=now)
    events.log(conn, "assignment_accepted", actor, task_id=a["task_id"], assignment_id=assignment_id, now=now,
               payload={"estimate_due_at": due})


class EstimateRequired(Exception):
    """The estimate gate (DESIGN 9.2): work cannot start until the contributor has confirmed an estimate."""


def start(conn, assignment_id: int, actor: str, now) -> None:
    a = _get(conn, assignment_id, "accepted")
    if conn.execute("SELECT 1 FROM estimate_revisions WHERE assignment_id = ?", (assignment_id,)).fetchone() is None:
        raise EstimateRequired(f"assignment {assignment_id}: provide an estimate before starting work")
    conn.execute("UPDATE assignments SET status = 'in_progress', started_at = ? WHERE id = ?", (clock.iso(now), assignment_id))
    tasks.move(conn, a["task_id"], "in_progress", actor, now=now)
    events.log(conn, "work_started", actor, task_id=a["task_id"], assignment_id=assignment_id, now=now, payload={})


def complete(conn, assignment_id: int, actor: str, now, note=None) -> None:
    a = _get(conn, assignment_id, "in_progress")
    conn.execute("UPDATE assignments SET status = 'completed', completed_at = ? WHERE id = ?", (clock.iso(now), assignment_id))
    tasks.move(conn, a["task_id"], "completed", actor, now=now, reason=note)
    events.log(conn, "work_completed", actor, task_id=a["task_id"], assignment_id=assignment_id, now=now, payload={"note": note})


def add_revision(conn, assignment_id, hours_total, hours_remaining, confidence, basis, source, submitted_by, now, note=None) -> int:
    rev = conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM estimate_revisions WHERE assignment_id = ?", (assignment_id,)).fetchone()[0]
    conn.execute("INSERT INTO estimate_revisions (assignment_id, revision, hours_total, hours_remaining, confidence, basis, source, "
                 "submitted_by, submitted_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (assignment_id, rev, hours_total, hours_remaining, confidence, basis, source, submitted_by, clock.iso(now), note))
    return rev


def _end(conn, assignment_id: int, new_status: str, reason: str, actor: str, now, expect=("offered", "accepted")):
    a = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    if a["status"] not in expect:
        raise ValueError(f"assignment {assignment_id} is {a['status']}, expected one of {expect}")
    conn.execute("UPDATE assignments SET status = ?, decided_at = COALESCE(decided_at, ?), end_reason = ? WHERE id = ?",
                 (new_status, clock.iso(now), reason, assignment_id))
    return a


def decline(conn, assignment_id: int, reason: str, actor: str, now) -> None:
    a = _end(conn, assignment_id, "declined", reason, actor, now)
    tasks.move(conn, a["task_id"], "declined", actor, now=now, reason=reason, contributor_id=a["contributor_id"])
    events.log(conn, "assignment_declined", actor, task_id=a["task_id"], assignment_id=assignment_id, now=now,
               payload={"contributor_id": a["contributor_id"], "reason": reason})


def fail(conn, assignment_id: int, reason: str, actor: str, now) -> None:
    a = _end(conn, assignment_id, "failed", reason, actor, now, expect=("offered", "accepted", "in_progress"))
    tasks.move(conn, a["task_id"], "failed", actor, now=now, reason=reason, contributor_id=a["contributor_id"])
    events.log(conn, "assignment_failed", actor, task_id=a["task_id"], assignment_id=assignment_id, now=now,
               payload={"contributor_id": a["contributor_id"], "reason": reason})


def cancel_active(conn, task_id: str, reason: str, actor: str, now) -> list[int]:
    """End every live assignment on the task (operator reassign/cancel). Does not move the task; the caller does."""
    ids = []
    for a in conn.execute(f"SELECT id, contributor_id FROM assignments WHERE task_id = ? AND status IN {ACTIVE}", (task_id,)).fetchall():
        conn.execute("UPDATE assignments SET status = 'cancelled', decided_at = COALESCE(decided_at, ?), end_reason = ? WHERE id = ?",
                     (clock.iso(now), reason, a["id"]))
        events.log(conn, "assignment_cancelled", actor, task_id=task_id, assignment_id=a["id"], now=now,
                   payload={"contributor_id": a["contributor_id"], "reason": reason})
        ids.append(a["id"])
    return ids


def _get(conn, assignment_id, expect):
    a = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    if a is None or a["status"] != expect:
        raise ValueError(f"assignment {assignment_id} is not {expect}")
    return a
