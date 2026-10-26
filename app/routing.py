"""
RoutingAgent (DESIGN sections 2, 7, 8).

Trigger states: submitted, queued, unmatched, declined, failed (plus 'routing' for pending-request upkeep).
Each tick step is idempotent and runs in its own transaction, so a crash or restart between steps is harmless:
requests, deadlines and outcomes are persisted, never held in memory.

Rules it enforces:
  * a stuck request is FLAGGED (alert + question), never failed automatically
  * a result that arrives after the operator intervened is logged and ignored (late_return_superseded)
  * capacity is soft: over-capacity -> queued + capacity_conflict alert/question, not a silent drop
  * the agent asks, the operator decides: a question's safe default is never a reassignment or cancellation
"""
import json
import random
from dataclasses import dataclass

from . import alerts, assignments, capacity, clock, config, events, simulator, tasks
from .db import transaction
from .tasks import PRIORITY_RANK

ACTOR = "RoutingAgent"
# Defaults that do nothing (the question stays open and re-alerts). Anything else is carried out when applied.
PASSIVE_DEFAULTS = {"keep_waiting", "hold_for_availability", "wait_for_capacity"}


@dataclass
class Outcome:
    resolved: bool          # False: the answer was recorded as a follow-up but the question stays open
    detail: str = ""


class AnswerRejected(Exception):
    """The chosen option cannot be carried out right now; the question stays open so the operator can choose again."""


@dataclass
class Ctx:
    conn: object
    cfg: dict
    now: object
    actor: str
    rng: random.Random


def _prio_key(t):
    return (-PRIORITY_RANK[t["priority"]], t["due_at"] or "9999", t["created_at"])


def _snapshot(ranked):
    return [{"id": e["id"], "level": e["level"], "fits": e["fits"], "reasons": e["reasons"], "utilization": e["utilization"]} for e in ranked]


# ------------------------------------------------------------------ routing requests
def create_request(ctx: Ctx, task) -> int:
    conn = ctx.conn
    attempt = conn.execute("SELECT COALESCE(MAX(attempt), 0) + 1 FROM routing_requests WHERE task_id = ?", (task["id"],)).fetchone()[0]
    ranked = capacity.eligible(conn, ctx.cfg, task)
    returns_at, pick = simulator.plan_engine(task, ranked, ctx.cfg, ctx.rng, ctx.now)
    cur = conn.execute("INSERT INTO routing_requests (task_id, attempt, started_at, eligible, sim_returns_at, sim_pick) VALUES (?, ?, ?, ?, ?, ?)",
                       (task["id"], attempt, clock.iso(ctx.now), json.dumps(_snapshot(ranked)), returns_at, pick))
    if task["status"] != "routing":
        tasks.move(conn, task["id"], "routing", ctx.actor, now=ctx.now, reason=f"routing request #{cur.lastrowid}")
    events.log(conn, "routing_requested", ctx.actor, task_id=task["id"], now=ctx.now,
               payload={"request_id": cur.lastrowid, "attempt": attempt, "eligible": [e["id"] for e in ranked]})
    return cur.lastrowid


def supersede_pending(ctx: Ctx, task_id: str, reason: str):
    """Close the task's pending request without an answer. A result that arrives later is logged and ignored."""
    req = ctx.conn.execute("SELECT id FROM routing_requests WHERE task_id = ? AND status = 'pending'", (task_id,)).fetchone()
    if req:
        ctx.conn.execute("UPDATE routing_requests SET status = 'superseded', finished_at = ? WHERE id = ?", (clock.iso(ctx.now), req["id"]))
        events.log(ctx.conn, "routing_superseded", ctx.actor, task_id=task_id, now=ctx.now, payload={"request_id": req["id"], "reason": reason})
        alerts.resolve_for_task(ctx.conn, task_id, "stuck_request", "request superseded", ctx.now)
    return req["id"] if req else None


# ------------------------------------------------------------------ operator interventions (shared by actions + handlers)
def intervene_assign(ctx: Ctx, task_id: str, contributor_id: str, reason=None) -> list[str]:
    """Operator picks the assignee, whatever state routing is in. Returns warnings (not blocking)."""
    conn = ctx.conn
    task = tasks.get(conn, task_id)
    if task["status"] in tasks.TERMINAL:
        raise ValueError(f"{task_id} is {task['status']}")
    c = conn.execute("SELECT * FROM contributors WHERE id = ?", (contributor_id,)).fetchone()
    if c is None:
        raise ValueError(f"unknown contributor {contributor_id}")
    warnings = []
    if conn.execute("SELECT 1 FROM v_eligible_contributors WHERE task_id = ? AND contributor_id = ?", (task_id, contributor_id)).fetchone() is None:
        warnings.append("not eligible by area/level/availability")
    chk = capacity.check(conn, ctx.cfg, contributor_id, task)
    if not chk["fits"]:
        warnings.append("capacity: " + "; ".join(chk["reasons"]))
    supersede_pending(ctx, task_id, "operator_intervention")
    assignments.cancel_active(conn, task_id, "operator reassigned", ctx.actor, ctx.now)
    assignments.offer(conn, task_id, contributor_id, "operator", ctx.actor, ctx.now, note=reason, warnings=warnings)
    return warnings


def intervene_cancel(ctx: Ctx, task_id: str, reason=None) -> None:
    supersede_pending(ctx, task_id, "task_cancelled")
    assignments.cancel_active(ctx.conn, task_id, reason or "task cancelled", ctx.actor, ctx.now)
    tasks.move(ctx.conn, task_id, "cancelled", ctx.actor, now=ctx.now, reason=reason)


def split_task(ctx: Ctx, task, children: list[dict]) -> list[str]:
    if not children or len(children) < 2:
        raise AnswerRejected("split_task needs params {'children': [{title, estimated_effort_hours}, ...]} with at least two children")
    ids = []
    for n, ch in enumerate(children, 1):
        cid = f"{task['id']}-{n}"
        ctx.conn.execute(
            "INSERT INTO tasks (id, title, area_id, min_level, priority, status, requester_name, requester_team, story_as_a, story_i_want, "
            "story_so_that, description, estimated_effort_hours, tags, created_at, due_at, updated_at, parent_task_id) "
            "VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, ch["title"], task["area_id"], task["min_level"], task["priority"], task["requester_name"], task["requester_team"],
             task["story_as_a"], task["story_i_want"], task["story_so_that"], ch.get("description", task["description"]),
             ch["estimated_effort_hours"], task["tags"], clock.iso(ctx.now), task["due_at"], clock.iso(ctx.now), task["id"]))
        events.log(ctx.conn, "task_created", ctx.actor, task_id=cid, now=ctx.now, payload={"source": "split", "parent": task["id"]})
        ids.append(cid)
    intervene_cancel(ctx, task["id"], f"split into {', '.join(ids)}")
    return ids


# ------------------------------------------------------------------ answer handlers (one per question type)
def _need(params, key, why):
    if params.get(key) in (None, ""):
        raise AnswerRejected(f"'{why}' needs params['{key}']")
    return params[key]


def h_stuck_request(ctx, q, option, params):
    task = tasks.get(ctx.conn, q["task_id"])
    if option == "keep_waiting":
        return Outcome(True, "still waiting; the agent will ask again if the request is still pending")
    if option == "resubmit_routing":
        supersede_pending(ctx, task["id"], "operator_resubmit")
        create_request(ctx, task)
        return Outcome(True, "previous request superseded; new request sent")
    if option == "assign_manually":
        return Outcome(True, "assigned manually; warnings: " + ("; ".join(intervene_assign(ctx, task["id"], _need(params, "contributor_id", option), params.get("note"))) or "none"))
    if option == "cancel_task":
        intervene_cancel(ctx, task["id"], params.get("note"))
        return Outcome(True, "task cancelled")


def h_no_match(ctx, q, option, params):
    conn, task = ctx.conn, tasks.get(ctx.conn, q["task_id"])
    if option == "lower_min_level_with_expert_review":
        new = int(_need(params, "min_level", option))
        if not 1 <= new < task["min_level"]:
            raise AnswerRejected(f"min_level must be between 1 and {task['min_level'] - 1}")
        tags = json.loads(task["tags"])
        if "expert_review_required" not in tags:
            tags.append("expert_review_required")
        conn.execute("UPDATE tasks SET min_level = ?, tags = ?, hold_reason = NULL WHERE id = ?", (new, json.dumps(tags), task["id"]))
        events.log(conn, "task_requirements_changed", ctx.actor, task_id=task["id"], now=ctx.now,
                   payload={"min_level": {"from": task["min_level"], "to": new}, "note": "expert review required before sign-off"})
        tasks.move(conn, task["id"], "submitted", ctx.actor, now=ctx.now, reason="min level lowered")
        return Outcome(True, f"min level {task['min_level']} -> {new}; tagged expert_review_required; re-submitted")
    if option == "widen_to_adjacent_area":
        area = _need(params, "area_id", option)
        if not conn.execute("SELECT 1 FROM area_adjacency WHERE area_id = ? AND adjacent_area_id = ?", (task["area_id"], area)).fetchone():
            raise AnswerRejected(f"{area} is not adjacent to {task['area_id']}")
        conn.execute("UPDATE tasks SET area_id = ?, hold_reason = NULL WHERE id = ?", (area, task["id"]))
        events.log(conn, "task_requirements_changed", ctx.actor, task_id=task["id"], now=ctx.now, payload={"area": {"from": task["area_id"], "to": area}})
        tasks.move(conn, task["id"], "submitted", ctx.actor, now=ctx.now, reason="area widened")
        return Outcome(True, f"area {task['area_id']} -> {area}; re-submitted")
    if option == "split_task":
        return Outcome(True, "split into " + ", ".join(split_task(ctx, task, params.get("children"))))
    if option == "hold_for_availability":
        conn.execute("UPDATE tasks SET hold_reason = 'availability' WHERE id = ?", (task["id"],))
        return Outcome(True, "holding; the agent re-routes automatically when an eligible contributor appears")
    if option == "cancel_task":
        intervene_cancel(ctx, task["id"], params.get("note"))
        return Outcome(True, "task cancelled")


def h_capacity_conflict(ctx, q, option, params):
    conn, task = ctx.conn, tasks.get(ctx.conn, q["task_id"])
    context = json.loads(q["context"])
    cid = context["contributor_id"]
    if option == "wait_for_capacity":
        conn.execute("UPDATE tasks SET hold_reason = 'capacity' WHERE id = ?", (task["id"],))
        return Outcome(True, "queued; will be offered automatically when capacity frees")
    if option == "assign_anyway":
        events.log(conn, "capacity_override", ctx.actor, task_id=task["id"], now=ctx.now, payload={"contributor_id": cid, "reasons": context.get("reasons_list", [])})
        intervene_assign(ctx, task["id"], cid, "capacity override by operator")
        return Outcome(True, f"offered to {cid} despite: {context.get('reasons')}")
    if option == "next_eligible_contributor":
        others = [e for e in capacity.eligible(conn, ctx.cfg, task, exclude=(cid,)) if e["fits"]]
        if not others:
            raise AnswerRejected("no other eligible contributor fits; choose another option")
        intervene_assign(ctx, task["id"], others[0]["id"], "next eligible contributor chosen by operator")
        return Outcome(True, f"offered to {others[0]['id']}")
    if option == "raise_contributor_hours":
        hours, slots = params.get("available_hours"), params.get("max_concurrent_tasks")
        if hours is None and slots is None:
            raise AnswerRejected("'raise_contributor_hours' needs params available_hours and/or max_concurrent_tasks")
        c = conn.execute("SELECT * FROM contributors WHERE id = ?", (cid,)).fetchone()
        new_hours, new_slots = float(hours or c["available_hours"]), int(slots or c["max_concurrent_tasks"])
        if new_hours < c["available_hours"] or new_slots < c["max_concurrent_tasks"]:
            raise AnswerRejected("limits can only be raised here")
        conn.execute("UPDATE contributors SET available_hours = ?, max_concurrent_tasks = ? WHERE id = ?", (new_hours, new_slots, cid))
        events.log(conn, "contributor_limits_changed", ctx.actor, now=ctx.now,
                   payload={"contributor_id": cid, "available_hours": [c["available_hours"], new_hours], "max_concurrent_tasks": [c["max_concurrent_tasks"], new_slots]})
        return Outcome(True, f"{cid} limits raised to {new_hours:g}h / {new_slots} tasks; the queue re-checks next tick")
    if option == "request_estimates_first":
        events.log(conn, "estimates_requested", ctx.actor, task_id=task["id"], now=ctx.now, payload={"contributor_id": cid, "reason": "confirm load before capacity decision"})
        return Outcome(False, f"asked {cid}'s assignments to be confirmed; question stays open until estimates are in")
    if option == "split_task":
        return Outcome(True, "split into " + ", ".join(split_task(ctx, task, params.get("children"))))


def h_priority_contention(ctx, q, option, params):
    conn, context = ctx.conn, json.loads(q["context"])
    rows = [tasks.get(conn, tid) for tid in context["task_ids"]]
    rows = [t for t in rows if t["status"] == "queued"]
    if option == "pick_explicitly":
        first = _need(params, "task_id", option)
        if first not in [t["id"] for t in rows]:
            raise AnswerRejected(f"{first} is not one of the queued contenders")
        rows.sort(key=lambda t: t["id"] != first)
    elif option == "by_due_date":
        rows.sort(key=lambda t: (t["due_at"] or "9999", -PRIORITY_RANK[t["priority"]]))
    elif option == "by_priority":
        rows.sort(key=_prio_key)
    else:
        raise AnswerRejected(f"unsupported option {option}")
    for t in rows:
        if capacity.check(conn, ctx.cfg, context["contributor_id"], t)["fits"]:
            assignments.offer(conn, t["id"], context["contributor_id"], "operator" if ctx.actor.startswith("operator") else "routing",
                              ctx.actor, ctx.now, note=f"won contention ({option})")
            return Outcome(True, f"{t['id']} offered to {context['contributor_id']}; the others stay queued")
    return Outcome(True, "capacity changed; none fit now, tasks stay queued")


def h_recovery(ctx, q, option, params):
    conn, task, context = ctx.conn, tasks.get(ctx.conn, q["task_id"]), json.loads(q["context"])
    if option == "reroute_excluding_contributor":
        conn.execute("INSERT OR IGNORE INTO routing_exclusions (task_id, contributor_id, reason, created_at) VALUES (?, ?, ?, ?)",
                     (task["id"], context["contributor_id"], f"{context['outcome']}: {context['reason']}", clock.iso(ctx.now)))
        tasks.move(conn, task["id"], "submitted", ctx.actor, now=ctx.now, reason=f"re-route excluding {context['contributor_id']}")
        return Outcome(True, f"re-routing without {context['contributor_id']}")
    if option == "offer_to_next_eligible":
        prior = {r[0] for r in conn.execute("SELECT contributor_id FROM assignments WHERE task_id = ? AND status IN ('declined', 'failed')", (task["id"],))}
        ranked = [e for e in capacity.eligible(conn, ctx.cfg, task, exclude=prior) if e["fits"]]
        if not ranked:
            raise AnswerRejected("no other eligible contributor fits; re-route so the engine can queue it, or assign manually")
        aid = assignments.offer(conn, task["id"], ranked[0]["id"], "operator", ctx.actor, ctx.now, note="next eligible after " + context["outcome"])
        return Outcome(True, f"offered to {ranked[0]['id']} (assignment {aid})")
    if option == "assign_manually":
        return Outcome(True, "assigned manually; warnings: " + ("; ".join(intervene_assign(ctx, task["id"], _need(params, "contributor_id", option), params.get("note"))) or "none"))
    if option == "contact_contributor":
        events.log(conn, "operator_contacting_contributor", ctx.actor, task_id=task["id"], now=ctx.now, payload={"contributor_id": context["contributor_id"], "note": params.get("note")})
        return Outcome(False, "noted; question stays open until you choose how to proceed")
    if option == "cancel_task":
        intervene_cancel(ctx, task["id"], params.get("note"))
        return Outcome(True, "task cancelled")


HANDLERS = {
    "stuck_request": h_stuck_request,
    "no_match": h_no_match,
    "capacity_conflict": h_capacity_conflict,
    "priority_contention": h_priority_contention,
    "recovery": h_recovery,
}


# ------------------------------------------------------------------ the agent
class RoutingAgent:
    name = ACTOR

    def __init__(self, conn, rng: random.Random | None = None):
        self.conn = conn
        self.rng = rng or random.Random()

    def ctx(self, cfg, now, actor=ACTOR) -> Ctx:
        return Ctx(self.conn, cfg, now, actor, self.rng)

    def recover(self, now=None) -> dict:
        """Run once at startup. Nothing needs re-arming (deadlines are stored); this just records what was found."""
        now = now or clock.now()
        pending = self.conn.execute("SELECT COUNT(*) FROM routing_requests WHERE status = 'pending'").fetchone()[0]
        overdue = self.conn.execute("SELECT COUNT(*) FROM routing_requests WHERE status = 'pending' AND sim_returns_at IS NOT NULL AND sim_returns_at <= ?",
                                    (clock.iso(now),)).fetchone()[0]
        with transaction(self.conn):
            events.log(self.conn, "agent_started", ACTOR, now=now, payload={"pending_requests": pending, "already_due": overdue})
        return {"pending_requests": pending, "already_due": overdue}

    def tick(self, now=None) -> dict:
        now = now or clock.now()
        cfg = config.load(self.conn)
        c = self.ctx(cfg, now)
        return {
            "repaired": self._repair(c),
            "claimed": self._claim_submitted(c),
            "late_returns": self._late_returns(c),
            "returned": self._process_returns(c),
            "stuck_flagged": self._flag_stuck(c),
            "recovery_asked": self._ask_recovery(c),
            "dequeued": self._dequeue(c),
            "unmatched_retried": self._retry_unmatched(c),
        }

    # -- steps -------------------------------------------------------------
    def _each(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def _repair(self, c) -> int:
        """A task in 'routing' with no pending request (should not happen; each step is atomic) gets a fresh one."""
        n = 0
        for t in self._each("SELECT id FROM tasks WHERE status = 'routing' AND id NOT IN (SELECT task_id FROM routing_requests WHERE status = 'pending')"):
            with transaction(self.conn):
                create_request(c, tasks.get(self.conn, t["id"]))
                events.log(self.conn, "routing_repaired", ACTOR, task_id=t["id"], now=c.now, payload={})
            n += 1
        return n

    def _claim_submitted(self, c) -> int:
        rows = sorted(self._each("SELECT * FROM tasks WHERE status = 'submitted'"), key=_prio_key)
        for t in rows:
            with transaction(self.conn):
                fresh = tasks.get(self.conn, t["id"])
                if fresh["status"] == "submitted":
                    create_request(c, fresh)
        return len(rows)

    def _late_returns(self, c) -> int:
        """Decision 2: a superseded request that answers later is recorded and ignored; it never overrides the operator."""
        n = 0
        for r in self._each("SELECT * FROM routing_requests WHERE status IN ('superseded', 'abandoned') AND late_returned_at IS NULL "
                            "AND sim_returns_at IS NOT NULL AND sim_returns_at <= ?", (clock.iso(c.now),)):
            with transaction(self.conn):
                t = tasks.get(self.conn, r["task_id"])
                self.conn.execute("UPDATE routing_requests SET late_returned_at = ?, result = ? WHERE id = ?",
                                  (clock.iso(c.now), json.dumps({"outcome": "ignored_late_return", "pick": r["sim_pick"]}), r["id"]))
                events.log(self.conn, "late_return_superseded", ACTOR, task_id=t["id"], now=c.now,
                           payload={"request_id": r["id"], "late_pick": r["sim_pick"], "current_status": t["status"], "action": "ignored"})
                alerts.raise_alert(self.conn, c.cfg, trigger="late_return_superseded", task=t, now=c.now,
                                   message=f"Routing request #{r['id']} for {t['id']} returned after the operator intervened; result ignored",
                                   dedupe_key=f"late_return:{r['id']}")
            n += 1
        return n

    def _process_returns(self, c) -> int:
        due = self._each("SELECT * FROM routing_requests WHERE status = 'pending' AND sim_returns_at IS NOT NULL AND sim_returns_at <= ? ORDER BY sim_returns_at",
                         (clock.iso(c.now),))
        for r in due:
            with transaction(self.conn):
                r = self.conn.execute("SELECT * FROM routing_requests WHERE id = ?", (r["id"],)).fetchone()
                if r["status"] == "pending":
                    self._handle_return(c, r)
        return len(due)

    def _handle_return(self, c, r) -> None:
        conn, task = self.conn, tasks.get(self.conn, r["task_id"])
        if task["status"] != "routing":
            conn.execute("UPDATE routing_requests SET status = 'superseded', finished_at = ? WHERE id = ?", (clock.iso(c.now), r["id"]))
            return
        ranked = capacity.eligible(conn, c.cfg, task)
        by_id = {e["id"]: e for e in ranked}
        forced = simulator._sim(task).get("forced_pick")
        pick, adjusted = r["sim_pick"], None
        if pick is not None and pick not in by_id:                       # availability/level changed while the engine was thinking
            adjusted, pick = f"{pick} no longer eligible", (ranked[0]["id"] if ranked else None)
        elif pick is not None and not by_id[pick]["fits"] and pick != forced:
            fitting = [e for e in ranked if e["fits"]]
            if fitting:
                adjusted, pick = f"{pick} no longer fits", fitting[0]["id"]
        if adjusted:
            events.log(conn, "pick_adjusted", ACTOR, task_id=task["id"], now=c.now, payload={"request_id": r["id"], "reason": adjusted, "new_pick": pick})

        def close(outcome, **extra):
            conn.execute("UPDATE routing_requests SET status = 'returned', finished_at = ?, result = ? WHERE id = ?",
                         (clock.iso(c.now), json.dumps({"outcome": outcome, "pick": pick, **extra}), r["id"]))
            events.log(conn, "routing_returned", ACTOR, task_id=task["id"], now=c.now, payload={"request_id": r["id"], "outcome": outcome, "pick": pick, **extra})

        if pick is None:
            close("no_match")
            tasks.move(conn, task["id"], "unmatched", ACTOR, now=c.now, reason="engine found no eligible contributor")
            alerts.raise_alert(conn, c.cfg, trigger="no_match", task=task, now=c.now,
                               message=f"No contributor matches {task['id']} (area {task['area_id']}, min level {task['min_level']})",
                               dedupe_key=f"no_match:{task['id']}:{r['id']}",
                               context={"task": tasks.label(task), "task_id": task["id"], "area": task["area_id"], "min_level": task["min_level"]})
        elif by_id[pick]["fits"]:
            close("offered")
            assignments.offer(conn, task["id"], pick, "routing", ACTOR, c.now, routing_request_id=r["id"])
        else:
            chosen = by_id[pick]
            close("queued", reasons=chosen["reasons"])
            tasks.move(conn, task["id"], "queued", ACTOR, now=c.now, reason="capacity conflict", contributor_id=pick)
            self._raise_capacity_conflict(c, task, chosen)

    def _raise_capacity_conflict(self, c, task, chosen) -> None:
        reasons = "; ".join(chosen["reasons"])
        _, created = alerts.raise_alert(
            self.conn, c.cfg, trigger="capacity_conflict", task=task, now=c.now,
            message=f"{task['id']} queued: {chosen['name']} would be over capacity ({reasons})",
            dedupe_key=f"capacity_conflict:{task['id']}:{chosen['id']}",
            context={"contributor": f"{chosen['name']} ({chosen['id']})", "contributor_id": chosen["id"], "task": tasks.label(task),
                     "task_id": task["id"], "reasons": reasons, "reasons_list": chosen["reasons"], "estimate_basis": chosen["basis"]})
        if created:
            events.log(self.conn, "capacity_conflict", ACTOR, task_id=task["id"], now=c.now,
                       payload={"contributor_id": chosen["id"], "reasons": chosen["reasons"], "basis": chosen["basis"],
                               "unconfirmed_assignments": chosen["unconfirmed_assignments"]})

    def _flag_stuck(self, c) -> int:
        n = 0
        rows = self._each("SELECT r.*, t.priority FROM routing_requests r JOIN tasks t ON t.id = r.task_id WHERE r.status = 'pending'")
        for r in rows:
            elapsed = (c.now - clock.parse(r["started_at"])).total_seconds()
            if elapsed < c.cfg["routing"]["stuck_threshold_seconds_by_priority"][r["priority"]]:
                continue
            if r["stuck_alerts"]:       # asked before: ask again only if that alert was resolved (e.g. 'keep waiting') and a timeout has passed
                open_alert = self.conn.execute("SELECT 1 FROM alerts WHERE task_id = ? AND trigger = 'stuck_request' AND status <> 'resolved'", (r["task_id"],)).fetchone()
                wait = c.cfg["operator_questions"]["answer_timeout_minutes_by_priority"][r["priority"]] * 60
                if open_alert or (c.now - clock.parse(r["stuck_flagged_at"])).total_seconds() < wait:
                    continue
            with transaction(self.conn):
                task = tasks.get(self.conn, r["task_id"])
                k = r["stuck_alerts"] + 1
                alerts.raise_alert(self.conn, c.cfg, trigger="stuck_request", task=task, now=c.now,
                                   message=f"Routing request #{r['id']} for {task['id']} pending {int(elapsed)}s with no response (possibly stuck)",
                                   dedupe_key=f"stuck_request:{task['id']}:{r['id']}:{k}",
                                   context={"task": tasks.label(task), "task_id": task["id"], "elapsed": int(elapsed), "request_id": r["id"]})
                self.conn.execute("UPDATE routing_requests SET stuck_alerts = ?, stuck_flagged_at = ? WHERE id = ?", (k, clock.iso(c.now), r["id"]))
            n += 1
        return n

    def _ask_recovery(self, c) -> int:
        n = 0
        for t in self._each("SELECT * FROM tasks WHERE status IN ('declined', 'failed')"):
            a = self.conn.execute("SELECT a.*, c.name FROM assignments a JOIN contributors c ON c.id = a.contributor_id "
                                  "WHERE a.task_id = ? AND a.status IN ('declined', 'failed') ORDER BY a.id DESC LIMIT 1", (t["id"],)).fetchone()
            if a is None:
                continue
            with transaction(self.conn):
                outcome = a["status"]
                _, created = alerts.raise_alert(
                    self.conn, c.cfg, trigger="request_declined" if outcome == "declined" else "assignment_failed", task=t, now=c.now,
                    assignment_id=a["id"], dedupe_key=f"recovery:{t['id']}:{a['id']}",
                    message=f"{t['id']} {outcome} by {a['name']}: {a['end_reason']}",
                    context={"task": tasks.label(t), "task_id": t["id"], "outcome": outcome, "contributor": f"{a['name']} ({a['contributor_id']})",
                             "contributor_id": a["contributor_id"], "reason": a["end_reason"]})
                n += created
        return n

    def _fitting(self, c, task):
        return [e for e in capacity.eligible(self.conn, c.cfg, task) if e["fits"]]

    def _dequeue(self, c) -> int:
        """Offer queued tasks as capacity frees. When two queued tasks would each fit the same contributor but
        not together, ask the operator which goes first (priority_contention) instead of silently choosing."""
        queued = sorted(self._each("SELECT * FROM tasks WHERE status = 'queued'"), key=_prio_key)
        if not queued:
            return 0
        with transaction(self.conn):
            by_best: dict[str, list] = {}
            for t in queued:
                fit = self._fitting(c, t)
                if fit:
                    by_best.setdefault(fit[0]["id"], []).append(t)
            for cid, group in by_best.items():
                if len(group) > 1 and not self._fit_together(c, cid, group):
                    alerts.raise_alert(
                        self.conn, c.cfg, trigger="priority_contention", task=group[0], now=c.now,
                        dedupe_key=f"priority_contention:{cid}:{'+'.join(sorted(t['id'] for t in group))}",
                        message=f"{', '.join(t['id'] for t in group)} are competing for {cid}'s remaining capacity",
                        context={"tasks": ", ".join(tasks.label(t) for t in group), "task_ids": [t["id"] for t in group],
                                 "contributor": cid, "contributor_id": cid,
                                 "hours": round(self.conn.execute("SELECT available_hours - load_hours FROM v_contributor_load WHERE contributor_id = ?", (cid,)).fetchone()[0], 2)})
        n = 0
        for t in queued:
            with transaction(self.conn):
                fresh = tasks.get(self.conn, t["id"])
                if fresh["status"] != "queued" or self._in_open_contention(fresh["id"]):
                    continue
                fit = self._fitting(c, fresh)
                if fit:
                    assignments.offer(self.conn, fresh["id"], fit[0]["id"], "routing", ACTOR, c.now, note="capacity freed")
                    n += 1
                elif fresh["hold_reason"] is None:      # still no fit and nobody chose to wait: make sure the operator has been told
                    ranked = capacity.eligible(self.conn, c.cfg, fresh)
                    if ranked:
                        self._raise_capacity_conflict(c, fresh, ranked[0])
        return n

    def _fit_together(self, c, cid, group) -> bool:
        load = self.conn.execute("SELECT * FROM v_contributor_load WHERE contributor_id = ?", (cid,)).fetchone()
        hours = load["load_hours"] + sum(capacity.hours_for(t, c.cfg) for t in group)
        return hours <= load["available_hours"] and load["active_tasks"] + len(group) <= load["max_concurrent_tasks"]

    def _in_open_contention(self, task_id) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM operator_questions q, json_each(q.context, '$.task_ids') j WHERE q.status = 'open' AND q.type = 'priority_contention' AND j.value = ?",
            (task_id,)).fetchone() is not None

    def _retry_unmatched(self, c) -> int:
        n = 0
        for t in self._each("SELECT * FROM tasks WHERE status = 'unmatched' AND hold_reason = 'availability'"):
            if capacity.eligible(self.conn, c.cfg, t):
                with transaction(self.conn):
                    self.conn.execute("UPDATE tasks SET hold_reason = NULL WHERE id = ?", (t["id"],))
                    events.log(self.conn, "eligibility_restored", ACTOR, task_id=t["id"], now=c.now, payload={})
                    tasks.move(self.conn, t["id"], "submitted", ACTOR, now=c.now, reason="eligible contributor available again")
                n += 1
        return n

    # -- operator questions ------------------------------------------------
    def apply_safe_default(self, q, now) -> None:
        """A question went unanswered past its deadline. Re-alert and apply only the declared safe default."""
        cfg = config.load(self.conn)
        with transaction(self.conn):
            alerts.mark_safe_default(self.conn, cfg, q, now)
            default = q["safe_default"]
            if default in PASSIVE_DEFAULTS:
                return
            c = self.ctx(cfg, now, actor="RoutingAgent(safe_default)")
            try:
                outcome = HANDLERS[q["type"]](c, q, default, {})
            except AnswerRejected as e:
                events.log(self.conn, "safe_default_not_applicable", ACTOR, task_id=q["task_id"], now=now, payload={"question_id": q["id"], "why": str(e)})
                return
            if outcome.resolved:
                alerts.record_answer(self.conn, q["id"], default, c.actor, f"applied after timeout: {outcome.detail}", now)
