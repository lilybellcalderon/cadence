"""
Operator actions: the HITL gate. Everything the operator can do goes through here so each action is validated,
atomic (a rejected answer rolls back whatever the handler wrote) and logged with the operator as actor.
"""
import json
import random
from datetime import timedelta

from . import alerts, clock, config, events, routing, tasks, tracking
from .db import transaction


class ActionError(Exception):
    """The action cannot be carried out; nothing was changed."""


# Question type -> handler (both agents' types).
HANDLERS = {**routing.HANDLERS, **tracking.HANDLERS}


def _actor(name: str) -> str:
    return name if name.startswith("operator:") else f"operator:{name}"


def _ctx(conn, actor, now, rng) -> routing.Ctx:
    return routing.Ctx(conn, config.load(conn), now or clock.now(), _actor(actor), rng or random.Random())


def submit_task(conn, task_id: str, actor: str = "operator", now=None, rng=None) -> None:
    ctx = _ctx(conn, actor, now, rng)
    with transaction(conn):
        task = tasks.get(conn, task_id)
        if task["status"] != "draft":
            raise ActionError(f"{task_id} is {task['status']}; only drafts can be submitted")
        tasks.move(conn, task_id, "submitted", ctx.actor, now=ctx.now, reason="submitted for assignment")


def assign_task(conn, task_id: str, contributor_id: str, actor: str = "operator", reason=None, now=None, rng=None) -> list[str]:
    """Intervene: pick the assignee now. Works in any non-terminal state, including while routing is pending
    (the pending request is superseded; if it answers later the result is logged and ignored)."""
    ctx = _ctx(conn, actor, now, rng)
    with transaction(conn):
        try:
            return routing.intervene_assign(ctx, task_id, contributor_id, reason)
        except (ValueError, tasks.InvalidTransition) as e:
            raise ActionError(str(e)) from e


def cancel_task(conn, task_id: str, actor: str = "operator", reason=None, now=None, rng=None) -> None:
    ctx = _ctx(conn, actor, now, rng)
    with transaction(conn):
        try:
            routing.intervene_cancel(ctx, task_id, reason)
        except tasks.InvalidTransition as e:
            raise ActionError(str(e)) from e


def acknowledge_alert(conn, alert_id: int, actor: str = "operator", now=None) -> None:
    with transaction(conn):
        alerts.acknowledge(conn, alert_id, _actor(actor), now)


def answer_question(conn, question_id: int, option: str, actor: str = "operator", note=None, params=None, now=None, rng=None) -> routing.Outcome:
    """Answer an agent's question. The answer drives the next transition. If the option cannot be carried out right
    now, ActionError is raised, nothing is changed and the question stays open."""
    ctx = _ctx(conn, actor, now, rng)
    with transaction(conn):
        q = conn.execute("SELECT q.*, t.priority FROM operator_questions q JOIN tasks t ON t.id = q.task_id WHERE q.id = ?", (question_id,)).fetchone()
        if q is None:
            raise ActionError(f"unknown question {question_id}")
        if q["status"] != "open":
            raise ActionError(f"question {question_id} is already {q['status']}")
        if option not in json.loads(q["options"]):
            raise ActionError(f"'{option}' is not an option; choose one of {json.loads(q['options'])}")
        handler = HANDLERS.get(q["type"])
        if handler is None:
            raise ActionError(f"no handler registered for question type {q['type']}")
        try:
            outcome = handler(ctx, q, option, dict(params or {}, note=note) if note else (params or {}))
        except routing.AnswerRejected as e:
            raise ActionError(str(e)) from e
        if outcome is None:
            raise ActionError(f"option '{option}' is not supported for {q['type']}")
        if outcome.resolved:
            alerts.record_answer(conn, question_id, option, ctx.actor, note or outcome.detail, ctx.now)
        else:   # recorded, but the situation is not resolved: keep the question open and give the operator more time
            timeout = ctx.cfg["operator_questions"]["answer_timeout_minutes_by_priority"][q["priority"]]
            conn.execute("UPDATE operator_questions SET answer_due_at = ? WHERE id = ?", (clock.iso(ctx.now + timedelta(minutes=timeout)), question_id))
            events.log(conn, "question_followup", ctx.actor, task_id=q["task_id"], now=ctx.now,
                       payload={"question_id": question_id, "option": option, "detail": outcome.detail, "note": note})
        return outcome
