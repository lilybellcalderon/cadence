"""
Alerts, operator questions and the simulated outbox (DESIGN section 8).

Convention: functions here never open a transaction; callers (agent ticks, operator actions) do.
Severity = clamp(priority_rank + trigger.adjust, floor, critical); an action-needed trigger is never below 'low'.
"""
import json
from datetime import timedelta

from . import clock, events

# Which task states an alert is relevant to. When the task leaves them the alert resolves itself.
RELEVANT_STATES = {
    "stuck_request": {"routing"},
    "no_match": {"unmatched"},
    "capacity_conflict": {"queued"},
    "priority_contention": {"queued"},
    "request_declined": {"declined"},
    "assignment_failed": {"failed"},
}


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _order(cfg) -> list[str]:
    ranks = cfg["alerts"]["severity_rank"]
    return sorted(ranks, key=ranks.get)


def severity_for(cfg: dict, priority: str, trigger: str) -> str:
    al = cfg["alerts"]
    trg = al["triggers"][trigger]
    rank = al["priority_rank"][priority] + trg["adjust"]
    order = _order(cfg)
    floor = 1 if trg["needs_action"] else 0
    return order[max(floor, min(len(order) - 1, rank))]


def _deliver(conn, cfg, alert_id, severity, task_id, message, now):
    """Write the simulated external message for severities whose delivery includes the outbox."""
    delivery = cfg["alerts"]["delivery_by_severity"][severity]
    if "outbox" in delivery["channels"]:
        conn.execute("INSERT INTO outbox (alert_id, channel, subject, body, created_at) VALUES (?, 'outbox', ?, ?, ?)",
                     (alert_id, f"[{severity.upper()}] {task_id or 'system'}: {message}", message, clock.iso(now)))
    return delivery


def _next_re_alert(delivery, now):
    if delivery["re_alert_after_minutes"] and delivery["max_re_alerts"] > 0:
        return clock.iso(now + timedelta(minutes=delivery["re_alert_after_minutes"]))
    return None


def raise_alert(conn, cfg, *, trigger, task, message, dedupe_key, now, assignment_id=None, context=None):
    """Raise an alert (and its operator question, if the trigger needs a decision).
    Returns (alert_id, created). An unresolved alert with the same dedupe_key is returned unchanged."""
    existing = conn.execute("SELECT id FROM alerts WHERE dedupe_key = ? AND status <> 'resolved'", (dedupe_key,)).fetchone()
    if existing:
        return existing["id"], False
    trg = cfg["alerts"]["triggers"][trigger]
    severity = severity_for(cfg, task["priority"], trigger)
    delivery = cfg["alerts"]["delivery_by_severity"][severity]
    log_only = severity == "info"
    cur = conn.execute(
        "INSERT INTO alerts (task_id, assignment_id, trigger, agent, severity, message, status, dedupe_key, created_at, "
        "resolved_at, next_re_alert_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task["id"], assignment_id, trigger, trg["agent"], severity, message, "resolved" if log_only else "open",
         dedupe_key, clock.iso(now), clock.iso(now) if log_only else None, None if log_only else _next_re_alert(delivery, now)))
    alert_id = cur.lastrowid
    _deliver(conn, cfg, alert_id, severity, task["id"], message, now)
    events.log(conn, "alert_raised", trg["agent"], task_id=task["id"], assignment_id=assignment_id, now=now,
               payload={"alert_id": alert_id, "trigger": trigger, "severity": severity, "message": message})
    if trg["needs_action"] and trg["question"]:
        _ask(conn, cfg, trg["question"], alert_id, task, context or {}, now)
    return alert_id, True


def _ask(conn, cfg, qtype, alert_id, task, context, now):
    spec = cfg["operator_questions"]["types"][qtype]
    timeout = cfg["operator_questions"]["answer_timeout_minutes_by_priority"][task["priority"]]
    prompt = spec["prompt"].format_map(_Safe(context))
    cur = conn.execute(
        "INSERT INTO operator_questions (alert_id, task_id, type, prompt, context, options, safe_default, asked_at, answer_due_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alert_id, task["id"], qtype, prompt, json.dumps(context, ensure_ascii=False), json.dumps(spec["options"]),
         spec["safe_default"], clock.iso(now), clock.iso(now + timedelta(minutes=timeout))))
    events.log(conn, "question_asked", "RoutingAgent" if qtype in ROUTING_QUESTIONS else "TrackingAgent",
               task_id=task["id"], now=now,
               payload={"question_id": cur.lastrowid, "type": qtype, "options": spec["options"], "safe_default": spec["safe_default"]})
    return cur.lastrowid


ROUTING_QUESTIONS = {"stuck_request", "no_match", "capacity_conflict", "priority_contention", "recovery"}


def acknowledge(conn, alert_id: int, actor: str, now=None) -> None:
    cur = conn.execute("UPDATE alerts SET status = 'acknowledged', acknowledged_at = ?, acknowledged_by = ?, next_re_alert_at = NULL "
                       "WHERE id = ? AND status = 'open'", (clock.iso(now), actor, alert_id))
    if cur.rowcount:
        a = conn.execute("SELECT task_id FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        events.log(conn, "alert_acknowledged", actor, task_id=a["task_id"], payload={"alert_id": alert_id}, now=now)


def resolve(conn, alert_id: int, reason: str, now=None) -> None:
    """Resolve an alert and withdraw any question still open on it."""
    cur = conn.execute("UPDATE alerts SET status = 'resolved', resolved_at = ?, next_re_alert_at = NULL "
                       "WHERE id = ? AND status <> 'resolved'", (clock.iso(now), alert_id))
    if cur.rowcount:
        conn.execute("UPDATE operator_questions SET status = 'withdrawn' WHERE alert_id = ? AND status = 'open'", (alert_id,))
        a = conn.execute("SELECT task_id FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        events.log(conn, "alert_resolved", "system", task_id=a["task_id"], payload={"alert_id": alert_id, "reason": reason}, now=now)


def resolve_for_task(conn, task_id: str, trigger: str, reason: str, now=None) -> None:
    for r in conn.execute("SELECT id FROM alerts WHERE task_id = ? AND trigger = ? AND status <> 'resolved'", (task_id, trigger)).fetchall():
        resolve(conn, r["id"], reason, now)


def resolve_stale(conn, task_id: str, new_status: str, now=None) -> None:
    """Called on every task status change: an alert whose situation no longer applies resolves itself."""
    for r in conn.execute("SELECT id, trigger FROM alerts WHERE task_id = ? AND status <> 'resolved'", (task_id,)).fetchall():
        relevant = RELEVANT_STATES.get(r["trigger"])
        if relevant is not None and new_status not in relevant:
            resolve(conn, r["id"], f"task moved to {new_status}", now)


def record_answer(conn, question_id: int, option: str, actor: str, note, now=None) -> None:
    q = conn.execute("SELECT task_id, alert_id FROM operator_questions WHERE id = ?", (question_id,)).fetchone()
    conn.execute("UPDATE operator_questions SET status = 'answered', answered_at = ?, answered_by = ?, answer = ?, note = ? WHERE id = ?",
                 (clock.iso(now), actor, option, note, question_id))
    events.log(conn, "question_answered", actor, task_id=q["task_id"], now=now,
               payload={"question_id": question_id, "answer": option, "note": note})
    if q["alert_id"]:
        resolve(conn, q["alert_id"], f"question answered: {option}", now)


def process_re_alerts(conn, cfg, now) -> int:
    """Re-alert unacknowledged alerts on the severity's schedule; the last re-alert is raised one level."""
    order, n = _order(cfg), 0
    for a in conn.execute("SELECT * FROM alerts WHERE status = 'open' AND next_re_alert_at IS NOT NULL AND next_re_alert_at <= ?",
                          (clock.iso(now),)).fetchall():
        delivery = cfg["alerts"]["delivery_by_severity"][a["severity"]]
        count = a["re_alert_count"] + 1
        severity = a["severity"]
        if count >= delivery["max_re_alerts"]:
            severity = order[min(len(order) - 1, order.index(severity) + 1)]       # final re-alert escalates one level
            next_at = None
        else:
            next_at = _next_re_alert(delivery, now)
        conn.execute("UPDATE alerts SET re_alert_count = ?, severity = ?, next_re_alert_at = ? WHERE id = ?",
                     (count, severity, next_at, a["id"]))
        _deliver(conn, cfg, a["id"], severity, a["task_id"], f"REMINDER {count}: {a['message']}", now)
        events.log(conn, "alert_re_alerted", "system", task_id=a["task_id"], now=now,
                   payload={"alert_id": a["id"], "count": count, "severity": severity})
        n += 1
    return n


def open_questions_due(conn, now):
    return conn.execute("SELECT q.*, t.priority FROM operator_questions q JOIN tasks t ON t.id = q.task_id "
                        "WHERE q.status = 'open' AND q.answer_due_at <= ?", (clock.iso(now),)).fetchall()


def mark_safe_default(conn, cfg, q, now) -> None:
    """Record that the safe default was applied and push the deadline out; the question stays open unless the caller answers it."""
    priority = conn.execute("SELECT priority FROM tasks WHERE id = ?", (q["task_id"],)).fetchone()[0]
    timeout = cfg["operator_questions"]["answer_timeout_minutes_by_priority"][priority]
    conn.execute("UPDATE operator_questions SET safe_default_applied_count = safe_default_applied_count + 1, last_safe_default_at = ?, "
                 "answer_due_at = ? WHERE id = ?", (clock.iso(now), clock.iso(now + timedelta(minutes=timeout)), q["id"]))
    events.log(conn, "question_safe_default_applied", "RoutingAgent" if q["type"] in ROUTING_QUESTIONS else "TrackingAgent",
               task_id=q["task_id"], now=now, payload={"question_id": q["id"], "safe_default": q["safe_default"]})
