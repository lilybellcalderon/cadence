"""Task state machine (DESIGN section 7). Every status change goes through move()."""
from . import alerts, clock, events

TRANSITIONS: dict[str, set[str]] = {
    "draft":       {"submitted", "offered", "cancelled"},
    "submitted":   {"routing", "offered", "cancelled"},
    "routing":     {"offered", "unmatched", "queued", "cancelled"},
    "queued":      {"offered", "submitted", "cancelled"},
    "unmatched":   {"submitted", "offered", "cancelled"},
    "offered":     {"accepted", "declined", "failed", "offered", "cancelled"},
    "accepted":    {"in_progress", "declined", "failed", "offered", "cancelled"},
    "in_progress": {"completed", "failed", "offered", "cancelled"},
    "declined":    {"submitted", "offered", "cancelled"},
    "failed":      {"submitted", "offered", "cancelled"},
    "completed":   set(),
    "cancelled":   set(),
}
TERMINAL = {"completed", "cancelled"}
PRIORITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


class InvalidTransition(Exception):
    pass


def get(conn, task_id: str):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown task {task_id}")
    return row


def label(task) -> str:
    return f"{task['id']} ({task['title']})"


def move(conn, task_id: str, to: str, actor: str, *, reason: str | None = None, now=None, **extra) -> None:
    task = get(conn, task_id)
    frm = task["status"]
    if to not in TRANSITIONS[frm]:
        raise InvalidTransition(f"{task_id}: {frm} -> {to} is not allowed")
    conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (to, clock.iso(now), task_id))
    events.log(conn, "task_status_changed", actor, task_id=task_id, now=now,
               payload={"from": frm, "to": to, "reason": reason, **extra})
    alerts.resolve_stale(conn, task_id, to, now)
