"""
TrackingAgent (DESIGN section 9). It owns the quality of the estimate and status data the RoutingAgent plans with.

Trigger states: accepted, in_progress. Each sweep, for every active assignment it:
  1. makes sure an estimate has been requested, chases it on the priority ladder, and raises estimate_overdue
  2. recomputes plan_health (awaiting_estimate | overdue_estimate | stale | at_risk | complete)
  3. sends status check-ins on the priority cadence and notices missed ones
  4. raises estimate_debt when a contributor has too many overdue estimates (RoutingAgent then avoids them)
Everything is idempotent (dedupe keys, unique (assignment, sequence)) and time is read from stored timestamps, so a restart
changes nothing. Estimate SLAs, cadences and the stale window are simulated minutes compressed by the demo factor;
due dates and operator timers are real time.
"""
import json
import random
from datetime import timedelta

from . import alerts, assignments, capacity, clock, config, events, routing, tasks
from .db import transaction
from .routing import AnswerRejected, Ctx, Outcome

ACTOR = "TrackingAgent"
TRACKING_QUESTIONS = {"estimate_missing", "estimate_variance", "contributor_silent", "at_risk"}
ACTIVE = ("accepted", "in_progress")
# Safe defaults that must leave the question open and change nothing (accepting a risk or asking for clarification
# is the operator's call, so an unanswered question never closes itself that way).
PASSIVE_DEFAULTS = {"accept_risk", "request_clarification"}


# ------------------------------------------------------------------ helpers
def _real(cfg, sim_minutes):
    return config.sim_minutes(cfg, sim_minutes)


def _sim_minutes_between(cfg, start, end) -> float:
    return (end - start).total_seconds() / 60 * cfg["contributor_engagement"]["status_check"]["demo_compression_factor"]


def _row(conn, assignment_id):
    return conn.execute(
        "SELECT a.*, t.priority, t.title, t.due_at, t.estimated_effort_hours AS requester_hours, c.name AS contributor_name "
        "FROM assignments a JOIN tasks t ON t.id = a.task_id JOIN contributors c ON c.id = a.contributor_id WHERE a.id = ?",
        (assignment_id,)).fetchone()


def latest_revision(conn, assignment_id):
    return conn.execute("SELECT * FROM estimate_revisions WHERE assignment_id = ? ORDER BY revision DESC LIMIT 1", (assignment_id,)).fetchone()


def _latest_reply(conn, assignment_id):
    return conn.execute("SELECT * FROM status_checks WHERE assignment_id = ? AND replied_at IS NOT NULL ORDER BY id DESC LIMIT 1", (assignment_id,)).fetchone()


def _label(a) -> str:
    return f"{a['task_id']} ({a['title']})"


def _risk_accepted(conn, key) -> bool:
    """The operator answered accept_risk for this exact situation; do not nag again until the data changes."""
    return conn.execute("SELECT 1 FROM events e WHERE e.event_type = 'risk_accepted' AND json_extract(e.payload, '$.dedupe_key') = ?", (key,)).fetchone() is not None


def due_risk(cfg, a, rev, now):
    """(at_risk, detail): remaining hours exceed the working hours left before the due date."""
    if a["due_at"] is None or rev is None:
        return False, ""
    per_day = cfg["estimates"]["validation_rules"]["working_hours_per_day"]
    hours_left = (clock.parse(a["due_at"]) - now).total_seconds() / 3600
    working = max(0.0, hours_left) * per_day / 24
    if rev["hours_remaining"] > working:
        return True, f"{rev['hours_remaining']:g}h remaining but only {working:.1f} working hours before the due date"
    return False, ""


def compute_health(conn, cfg, a, now) -> tuple[str, str]:
    """(plan_health, reason) for one active assignment."""
    rev = latest_revision(conn, a["id"])
    if rev is None:
        if a["estimate_due_at"] and now < clock.parse(a["estimate_due_at"]):
            return "awaiting_estimate", "estimate requested"
        return "overdue_estimate", "no confirmed estimate past the SLA"
    reply = _latest_reply(conn, a["id"])
    if reply and reply["status"] in ("at_risk", "blocked"):
        return "at_risk", f"contributor reported {reply['status']}"
    risky, detail = due_risk(cfg, a, rev, now)
    if risky:
        return "at_risk", detail
    freshest = max(rev["submitted_at"], reply["replied_at"] if reply else "")
    stale_after = _real(cfg, cfg["estimates"]["stale_after_minutes_by_priority"][a["priority"]])
    if now - clock.parse(freshest) > stale_after:
        return "stale", f"no estimate or status update since {freshest}"
    return "complete", "estimate confirmed and fresh"


def refresh_health(conn, cfg, assignment_id, now) -> str:
    a = _row(conn, assignment_id)
    health, reason = compute_health(conn, cfg, a, now)
    if health != a["plan_health"]:
        conn.execute("UPDATE assignments SET plan_health = ?, plan_health_updated_at = ? WHERE id = ?", (health, clock.iso(now), assignment_id))
        events.log(conn, "plan_health_changed", ACTOR, task_id=a["task_id"], assignment_id=assignment_id, now=now,
                   payload={"from": a["plan_health"], "to": health, "reason": reason})
    return health


# ------------------------------------------------------------------ data entry points (contributor, operator, simulator)
def submit_estimate(conn, cfg, assignment_id, *, hours_total, hours_remaining=None, confidence="medium", basis="rough_guess",
                    actor, now, source="contributor", note=None) -> int:
    """Record a confirmed estimate (or revision). Answers outstanding requests, checks it against the requester's number
    and the previous revision, and refreshes plan health. Caller owns the transaction."""
    a = _row(conn, assignment_id)
    if a is None or a["status"] not in ACTIVE:
        raise ValueError(f"assignment {assignment_id} is not active")
    if hours_total is None or hours_total <= 0:
        raise ValueError("hours_total must be > 0")
    remaining = hours_total if hours_remaining is None else hours_remaining
    prev = latest_revision(conn, assignment_id)
    rev = assignments.add_revision(conn, assignment_id, hours_total, remaining, confidence, basis, source, actor, now, note)
    conn.execute("UPDATE estimate_requests SET answered_at = ? WHERE assignment_id = ? AND answered_at IS NULL", (clock.iso(now), assignment_id))
    events.log(conn, "estimate_submitted", actor, task_id=a["task_id"], assignment_id=assignment_id, now=now,
               payload={"revision": rev, "hours_total": hours_total, "hours_remaining": remaining, "confidence": confidence,
                        "basis": basis, "source": source, "note": note})
    _check_variance(conn, cfg, a, rev, hours_total, remaining, prev, now, source)
    refresh_health(conn, cfg, assignment_id, now)
    return rev


def accept_with_estimate(conn, cfg, assignment_id, actor, now, *, hours_total=None, confidence=None, basis=None) -> None:
    """Capture at accept (DESIGN 9.2): accepting and confirming the estimate are one atomic step. Without hours_total the
    assignment is accepted as awaiting_estimate and chased."""
    assignments.accept(conn, cfg, assignment_id, actor, now)
    if hours_total:
        submit_estimate(conn, cfg, assignment_id, hours_total=hours_total, confidence=confidence or "medium", basis=basis or "rough_guess",
                        actor=actor, now=now, source="contributor")


def _check_variance(conn, cfg, a, rev, hours_total, remaining, prev, now, source) -> None:
    rules = cfg["estimates"]["validation_rules"]
    task = tasks.get(conn, a["task_id"])
    reasons = []
    delta = (hours_total - a["requester_hours"]) / a["requester_hours"] * 100
    if prev is None and source != "operator_on_behalf" and abs(delta) > rules["variance_flag_pct"]:
        reasons.append(f"total {hours_total:g}h vs requester {a['requester_hours']:g}h ({delta:+.0f}%)")
    if prev is not None and prev["hours_remaining"] > 0 and remaining > prev["hours_remaining"] * (1 + rules["remaining_increase_flag_pct"] / 100):
        grow = (remaining - prev["hours_remaining"]) / prev["hours_remaining"] * 100
        reasons.append(f"remaining grew {prev['hours_remaining']:g}h -> {remaining:g}h ({grow:+.0f}%)")
    if reasons:
        alerts.raise_alert(
            conn, cfg, trigger="estimate_variance", task=task, now=now, assignment_id=a["id"],
            message=f"{a['task_id']}: {a['contributor_name']}'s estimate looks off: {'; '.join(reasons)}",
            dedupe_key=f"estimate_variance:{a['id']}:{rev}",
            context={"contributor": a["contributor_name"], "contributor_id": a["contributor_id"], "assignment_id": a["id"],
                     "task": _label(a), "task_id": a["task_id"], "contributor_hours": hours_total, "requester_hours": a["requester_hours"],
                     "delta_pct": f"{delta:+.0f}", "detail": "; ".join(reasons)})


def submit_status(conn, cfg, check_id, *, status, progress_percent=None, hours_remaining=None, note=None, actor, now) -> None:
    """A contributor answers a check-in. hours_remaining becomes the newest estimate revision, so planning sees it."""
    chk = conn.execute("SELECT * FROM status_checks WHERE id = ?", (check_id,)).fetchone()
    if chk is None or chk["replied_at"] is not None:
        raise ValueError(f"status check {check_id} is not open")
    if status not in ("on_track", "at_risk", "blocked"):
        raise ValueError("status must be on_track, at_risk or blocked")
    a = _row(conn, chk["assignment_id"])
    if a["status"] not in ACTIVE:
        raise ValueError("assignment is no longer active")
    conn.execute("UPDATE status_checks SET replied_at = ?, status = ?, progress_percent = ?, hours_remaining = ?, note = ? WHERE id = ?",
                 (clock.iso(now), status, progress_percent, hours_remaining, note, check_id))
    events.log(conn, "status_reply", actor, task_id=a["task_id"], assignment_id=a["id"], now=now,
               payload={"check_id": check_id, "status": status, "progress_percent": progress_percent, "hours_remaining": hours_remaining, "note": note})
    if hours_remaining is not None:
        prev = latest_revision(conn, a["id"])
        if prev is None:
            submit_estimate(conn, cfg, a["id"], hours_total=hours_remaining, hours_remaining=hours_remaining, confidence="medium",
                            basis="rough_guess", actor=actor, now=now, source="check_in", note="estimate given with a status check-in")
        else:
            rev = assignments.add_revision(conn, a["id"], max(prev["hours_total"], hours_remaining), hours_remaining, prev["confidence"],
                                           prev["basis"], "check_in", actor, now, note)
            _check_variance(conn, cfg, a, rev, prev["hours_total"], hours_remaining, prev, now, "check_in")
    task = tasks.get(conn, a["task_id"])
    if status in ("at_risk", "blocked"):
        trigger = "status_at_risk" if status == "at_risk" else "status_blocked"
        detail = note or (f"{hours_remaining:g}h remaining" if hours_remaining is not None else "no detail given")
        alerts.raise_alert(conn, cfg, trigger=trigger, task=task, now=now, assignment_id=a["id"],
                           message=f"{a['task_id']} is {status.replace('_', ' ')} per {a['contributor_name']}: {detail}",
                           dedupe_key=f"{trigger}:{a['id']}:{check_id}",
                           context={"task": _label(a), "task_id": a["task_id"], "state": status.replace("_", " "), "detail": detail,
                                    "contributor": a["contributor_name"], "contributor_id": a["contributor_id"], "assignment_id": a["id"]})
    refresh_health(conn, cfg, a["id"], now)


# ------------------------------------------------------------------ question handlers
def _assignment_for(ctx, q):
    context = json.loads(q["context"])
    aid = context.get("assignment_id")
    a = _row(ctx.conn, aid) if aid else None
    if a is None or a["status"] not in ACTIVE:
        raise AnswerRejected("that assignment is no longer active")
    return a, context


def _send_reminder(ctx, a, kind="estimate") -> None:
    cfg, conn = ctx.cfg, ctx.conn
    if kind == "estimate" and latest_revision(conn, a["id"]) is None:
        seq = conn.execute("SELECT COALESCE(MAX(sequence), -1) + 1 FROM estimate_requests WHERE assignment_id = ?", (a["id"],)).fetchone()[0]
        every = cfg["estimates"]["sla_minutes_by_priority"][a["priority"]]["reminder_every"]
        conn.execute("INSERT INTO estimate_requests (assignment_id, kind, sequence, sent_at, due_at) VALUES (?, 'reminder', ?, ?, ?)",
                     (a["id"], seq, clock.iso(ctx.now), clock.iso(ctx.now + _real(cfg, every))))
    events.log(conn, "reminder_sent", ctx.actor, task_id=a["task_id"], assignment_id=a["id"], now=ctx.now,
               payload={"kind": kind, "contributor_id": a["contributor_id"]})


def h_estimate_missing(ctx, q, option, params):
    a, _ = _assignment_for(ctx, q)
    conn, cfg = ctx.conn, ctx.cfg
    if option == "send_reminder":
        _send_reminder(ctx, a)
        return Outcome(False, "reminder sent; the question stays open until the estimate arrives")
    if option == "contact_contributor":
        events.log(conn, "operator_contacting_contributor", ctx.actor, task_id=a["task_id"], assignment_id=a["id"], now=ctx.now,
                   payload={"contributor_id": a["contributor_id"], "note": params.get("note")})
        return Outcome(False, "noted; the question stays open until the estimate arrives")
    if option == "enter_estimate_on_their_behalf":
        hours = routing._need(params, "hours_total", option)
        submit_estimate(conn, cfg, a["id"], hours_total=float(hours), confidence=params.get("confidence", "low"),
                        basis=params.get("basis", "rough_guess"), actor=ctx.actor, now=ctx.now, source="operator_on_behalf",
                        note=params.get("note") or "entered by the operator because the contributor did not respond")
        return Outcome(True, f"estimate of {hours}h entered on {a['contributor_name']}'s behalf (flagged as such)")
    if option == "use_requester_estimate_provisionally":
        submit_estimate(conn, cfg, a["id"], hours_total=a["requester_hours"], confidence="low", basis="rough_guess", actor=ctx.actor,
                        now=ctx.now, source="operator_on_behalf", note="provisional: requester's estimate, not confirmed by the contributor")
        return Outcome(True, f"requester's {a['requester_hours']:g}h used provisionally (low confidence)")
    if option == "reassign_task":
        warnings = routing.intervene_assign(ctx, a["task_id"], routing._need(params, "contributor_id", option), params.get("note"))
        return Outcome(True, "reassigned; warnings: " + ("; ".join(warnings) or "none"))


def h_estimate_variance(ctx, q, option, params):
    a, context = _assignment_for(ctx, q)
    conn = ctx.conn
    if option == "accept_contributor_estimate":
        events.log(conn, "estimate_accepted", ctx.actor, task_id=a["task_id"], assignment_id=a["id"], now=ctx.now, payload={"note": params.get("note")})
        return Outcome(True, "contributor's estimate accepted as the plan")
    if option in ("request_clarification", "ask_requester_to_rescope", "request_second_opinion"):
        events.log(conn, option, ctx.actor, task_id=a["task_id"], assignment_id=a["id"], now=ctx.now, payload={"note": params.get("note")})
        return Outcome(False, f"{option.replace('_', ' ')} recorded; the question stays open until you decide")
    if option == "split_task":
        return Outcome(True, "split into " + ", ".join(routing.split_task(ctx, tasks.get(conn, a["task_id"]), params.get("children"))))


def h_contributor_silent(ctx, q, option, params):
    a, _ = _assignment_for(ctx, q)
    conn = ctx.conn
    if option == "send_reminder":
        _send_reminder(ctx, a, kind="status")
        return Outcome(False, "status reminder sent; the question stays open")
    if option == "escalate_to_manager":
        conn.execute("INSERT INTO outbox (alert_id, channel, recipient, subject, body, created_at) VALUES (?, 'outbox', 'manager', ?, ?, ?)",
                     (q["alert_id"], f"Escalation: {a['contributor_name']} not responding on {a['task_id']}",
                      f"{a['contributor_name']} has missed status check-ins on {_label(a)}.", clock.iso(ctx.now)))
        events.log(conn, "escalated_to_manager", ctx.actor, task_id=a["task_id"], assignment_id=a["id"], now=ctx.now, payload={"contributor_id": a["contributor_id"]})
        return Outcome(True, "manager notified")
    if option == "keep_waiting":
        return Outcome(True, "waiting; the agent will raise it again if check-ins keep being missed")
    if option == "reassign_task":
        warnings = routing.intervene_assign(ctx, a["task_id"], routing._need(params, "contributor_id", option), params.get("note"))
        return Outcome(True, "reassigned; warnings: " + ("; ".join(warnings) or "none"))


def h_at_risk(ctx, q, option, params):
    a, context = _assignment_for(ctx, q)
    conn = ctx.conn
    if option == "accept_risk":
        events.log(conn, "risk_accepted", ctx.actor, task_id=a["task_id"], assignment_id=a["id"], now=ctx.now,
                   payload={"dedupe_key": context.get("risk_key"), "note": params.get("note")})
        return Outcome(True, "risk accepted; will not be raised again until the numbers change")
    if option == "extend_due_date":
        hours = float(routing._need(params, "extend_hours", option))
        task = tasks.get(conn, a["task_id"])
        base = clock.parse(task["due_at"]) if task["due_at"] else ctx.now
        new_due = clock.iso(base + timedelta(hours=hours))
        conn.execute("UPDATE tasks SET due_at = ? WHERE id = ?", (new_due, task["id"]))
        events.log(conn, "due_date_changed", ctx.actor, task_id=task["id"], now=ctx.now, payload={"from": task["due_at"], "to": new_due, "note": params.get("note")})
        return Outcome(True, f"due date moved to {new_due}")
    if option == "add_second_contributor_for_help":
        events.log(conn, "helper_requested", ctx.actor, task_id=a["task_id"], assignment_id=a["id"], now=ctx.now,
                   payload={"note": params.get("note"), "rule": "the task keeps a single active assignee; the helper contributes without owning it"})
        return Outcome(True, "help requested; the task keeps one accountable assignee")
    if option == "contact_contributor":
        events.log(conn, "operator_contacting_contributor", ctx.actor, task_id=a["task_id"], assignment_id=a["id"], now=ctx.now, payload={"note": params.get("note")})
        return Outcome(False, "noted; the question stays open")
    if option == "reassign_task":
        warnings = routing.intervene_assign(ctx, a["task_id"], routing._need(params, "contributor_id", option), params.get("note"))
        return Outcome(True, "reassigned; warnings: " + ("; ".join(warnings) or "none"))


HANDLERS = {
    "estimate_missing": h_estimate_missing,
    "estimate_variance": h_estimate_variance,
    "contributor_silent": h_contributor_silent,
    "at_risk": h_at_risk,
}


# ------------------------------------------------------------------ the agent
class TrackingAgent:
    name = ACTOR

    def __init__(self, conn, rng: random.Random | None = None):
        self.conn = conn
        self.rng = rng or random.Random()

    def ctx(self, cfg, now, actor=ACTOR) -> Ctx:
        return Ctx(self.conn, cfg, now, actor, self.rng)

    def recover(self, now=None) -> dict:
        now = now or clock.now()
        active = self.conn.execute("SELECT COUNT(*) FROM assignments WHERE status IN ('accepted', 'in_progress')").fetchone()[0]
        with transaction(self.conn):
            events.log(self.conn, "agent_started", ACTOR, now=now, payload={"active_assignments": active})
        return {"active_assignments": active}

    def tick(self, now=None) -> dict:
        now = now or clock.now()
        cfg = config.load(self.conn)
        out = {"estimate_requests": 0, "reminders": 0, "overdue_alerts": 0, "status_checks": 0, "missed_checks": 0, "silent_alerts": 0,
               "stale_alerts": 0, "risk_alerts": 0}
        for row in self.conn.execute("SELECT id FROM assignments WHERE status IN ('accepted', 'in_progress') ORDER BY id").fetchall():
            with transaction(self.conn):
                a = _row(self.conn, row["id"])
                if a["status"] not in ACTIVE:
                    continue
                self._chase_estimate(cfg, a, now, out)
                self._status_checks(cfg, a, now, out)
                self._health_alerts(cfg, a, now, out)
        with transaction(self.conn):
            out["debt_alerts"] = self._estimate_debt(cfg, now)
            self._reconcile(cfg, now)
        return {k: v for k, v in out.items()}

    # 1. estimate request + chase ladder ------------------------------------
    def _chase_estimate(self, cfg, a, now, out) -> None:
        if latest_revision(self.conn, a["id"]) is not None:
            return
        sla = cfg["estimates"]["sla_minutes_by_priority"][a["priority"]]
        reqs = self.conn.execute("SELECT * FROM estimate_requests WHERE assignment_id = ? ORDER BY sequence", (a["id"],)).fetchall()
        if not reqs:
            self.conn.execute("INSERT INTO estimate_requests (assignment_id, kind, sequence, sent_at, due_at) VALUES (?, 'request', 0, ?, ?)",
                              (a["id"], a["decided_at"], a["estimate_due_at"]))
            events.log(self.conn, "estimate_requested", ACTOR, task_id=a["task_id"], assignment_id=a["id"], now=now,
                       payload={"due_at": a["estimate_due_at"], "contributor_id": a["contributor_id"]})
            out["estimate_requests"] += 1
            return
        last = reqs[-1]
        if last["answered_at"] or now < clock.parse(last["due_at"]):
            return
        if last["sequence"] < sla["max_reminders"]:
            seq = last["sequence"] + 1
            self.conn.execute("INSERT INTO estimate_requests (assignment_id, kind, sequence, sent_at, due_at) VALUES (?, 'reminder', ?, ?, ?)",
                              (a["id"], seq, clock.iso(now), clock.iso(now + _real(cfg, sla["reminder_every"]))))
            events.log(self.conn, "estimate_reminder_sent", ACTOR, task_id=a["task_id"], assignment_id=a["id"], now=now,
                       payload={"sequence": seq, "contributor_id": a["contributor_id"]})
            out["reminders"] += 1
            return
        # every reminder went unanswered: this is now the operator's decision
        task = tasks.get(self.conn, a["task_id"])
        overdue = _sim_minutes_between(cfg, clock.parse(a["estimate_due_at"]), now)
        _, created = alerts.raise_alert(
            self.conn, cfg, trigger="estimate_overdue", task=task, now=now, assignment_id=a["id"],
            message=f"{a['task_id']}: no confirmed estimate from {a['contributor_name']} ({int(overdue)} min past the SLA, {last['sequence']} reminders sent)",
            dedupe_key=f"estimate_overdue:{a['id']}",
            context=self._missing_context(cfg, a, "no confirmed estimate", int(overdue), last["sequence"]))
        out["overdue_alerts"] += created

    def _missing_context(self, cfg, a, issue, minutes_overdue, reminders) -> dict:
        buf = cfg["estimates"]["model"]["unconfirmed_buffer"]
        return {"contributor": a["contributor_name"], "contributor_id": a["contributor_id"], "assignment_id": a["id"], "task": _label(a),
                "task_id": a["task_id"], "issue": issue, "minutes_overdue": minutes_overdue, "reminders": reminders,
                "fallback_hours": round(a["requester_hours"] * buf, 2), "fallback_source": f"requester estimate x{buf:g} (unconfirmed)"}

    # 2. status check-ins ---------------------------------------------------
    def _status_checks(self, cfg, a, now, out) -> None:
        se = cfg["contributor_engagement"]["status_check"]
        cadence = _real(cfg, se["cadence_minutes_by_priority"][a["priority"]])
        window = _real(cfg, se["reply_window_minutes"])
        last = self.conn.execute("SELECT * FROM status_checks WHERE assignment_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
        if last is not None and last["replied_at"] is None and last["status"] is None:
            if now >= clock.parse(last["due_at"]):                               # no reply in the window: a missed check-in
                self.conn.execute("UPDATE status_checks SET status = 'no_response' WHERE id = ?", (last["id"],))
                events.log(self.conn, "status_check_missed", ACTOR, task_id=a["task_id"], assignment_id=a["id"], now=now, payload={"check_id": last["id"]})
                out["missed_checks"] += 1
                self._missed_alert(cfg, a, now, out)
            return
        base = clock.parse(a["decided_at"]) if last is None else clock.parse(last["sent_at"])
        if now >= base + cadence:
            cur = self.conn.execute("INSERT INTO status_checks (assignment_id, sent_at, due_at) VALUES (?, ?, ?)",
                                    (a["id"], clock.iso(now), clock.iso(now + window)))
            events.log(self.conn, "status_check_requested", ACTOR, task_id=a["task_id"], assignment_id=a["id"], now=now, payload={"check_id": cur.lastrowid})
            out["status_checks"] += 1

    def _missed_alert(self, cfg, a, now, out) -> None:
        need = cfg["contributor_engagement"]["status_check"]["missed_checkins_before_alert"]
        rows = self.conn.execute("SELECT status FROM status_checks WHERE assignment_id = ? ORDER BY id DESC", (a["id"],)).fetchall()
        missed = 0
        for r in rows:
            if r["status"] != "no_response":
                break
            missed += 1
        if missed >= need:
            task = tasks.get(self.conn, a["task_id"])
            _, created = alerts.raise_alert(
                self.conn, cfg, trigger="status_no_response", task=task, now=now, assignment_id=a["id"],
                message=f"{a['contributor_name']} missed {missed} status check-ins on {a['task_id']}",
                dedupe_key=f"status_no_response:{a['id']}:{missed}",
                context={"contributor": a["contributor_name"], "contributor_id": a["contributor_id"], "assignment_id": a["id"],
                         "missed": missed, "task": _label(a), "task_id": a["task_id"]})
            out["silent_alerts"] += created

    # 3. plan health + stale / due-date alerts ------------------------------
    def _health_alerts(self, cfg, a, now, out) -> None:
        health = refresh_health(self.conn, cfg, a["id"], now)
        rev = latest_revision(self.conn, a["id"])
        task = tasks.get(self.conn, a["task_id"])
        if health == "stale" and rev is not None:
            reply = _latest_reply(self.conn, a["id"])
            freshest = max(rev["submitted_at"], reply["replied_at"] if reply else "")
            _, created = alerts.raise_alert(
                self.conn, cfg, trigger="estimate_stale", task=task, now=now, assignment_id=a["id"],
                message=f"{a['task_id']}: estimate/status from {a['contributor_name']} is stale (last update {freshest})",
                dedupe_key=f"estimate_stale:{a['id']}:{freshest}",
                context=self._missing_context(cfg, a, "estimate or status is stale", int(_sim_minutes_between(cfg, clock.parse(freshest), now)), 0))
            out["stale_alerts"] += created
        risky, detail = due_risk(cfg, a, rev, now)
        if risky:
            key = f"due_date_at_risk:{a['id']}:{rev['id']}"
            if not _risk_accepted(self.conn, key):
                _, created = alerts.raise_alert(
                    self.conn, cfg, trigger="due_date_at_risk", task=task, now=now, assignment_id=a["id"],
                    message=f"{a['task_id']} may miss its due date: {detail}", dedupe_key=key,
                    context={"task": _label(a), "task_id": a["task_id"], "state": "at risk of missing its due date", "detail": detail,
                             "contributor_id": a["contributor_id"], "contributor": a["contributor_name"], "assignment_id": a["id"], "risk_key": key})
                out["risk_alerts"] += created

    # 4. estimate debt ------------------------------------------------------
    def _estimate_debt(self, cfg, now) -> int:
        limit = cfg["estimates"]["estimate_debt"]["max_overdue_estimates_per_contributor"]
        n = 0
        for c in self.conn.execute("SELECT contributor_id, overdue_estimates FROM v_contributor_load WHERE overdue_estimates > ?", (limit,)).fetchall():
            worst = self.conn.execute(
                "SELECT a.id FROM assignments a JOIN tasks t ON t.id = a.task_id WHERE a.contributor_id = ? AND a.plan_health = 'overdue_estimate' "
                "AND a.status IN ('accepted', 'in_progress') ORDER BY CASE t.priority WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC, a.id LIMIT 1",
                (c["contributor_id"],)).fetchone()
            a = _row(self.conn, worst["id"])
            ctx = self._missing_context(cfg, a, f"estimate debt: {c['overdue_estimates']} overdue estimates", 0, 0)
            ctx["minutes_overdue"] = int(_sim_minutes_between(cfg, clock.parse(a["estimate_due_at"]), now))
            _, created = alerts.raise_alert(
                self.conn, cfg, trigger="estimate_debt", task=tasks.get(self.conn, a["task_id"]), now=now, assignment_id=a["id"],
                message=f"{a['contributor_name']} has {c['overdue_estimates']} assignments without a confirmed estimate; new work will queue instead of being assigned",
                dedupe_key=f"estimate_debt:{c['contributor_id']}", context=ctx)
            n += created
        return n

    # 5. resolve alerts whose situation has cleared --------------------------
    def _reconcile(self, cfg, now) -> None:
        limit = cfg["estimates"]["estimate_debt"]["max_overdue_estimates_per_contributor"]
        for al in self.conn.execute("SELECT * FROM alerts WHERE agent = 'TrackingAgent' AND status <> 'resolved'").fetchall():
            reason = None
            a = _row(self.conn, al["assignment_id"]) if al["assignment_id"] else None
            if a is not None and a["status"] not in ACTIVE:
                reason = "assignment is no longer active"
            elif al["trigger"] in ("estimate_overdue", "estimate_stale") and a is not None:
                want = "overdue_estimate" if al["trigger"] == "estimate_overdue" else "stale"
                if a["plan_health"] != want:
                    reason = f"plan health is now {a['plan_health']}"
            elif al["trigger"] == "estimate_debt":
                cid = al["dedupe_key"].split(":", 1)[1]
                if self.conn.execute("SELECT overdue_estimates FROM v_contributor_load WHERE contributor_id = ?", (cid,)).fetchone()[0] <= limit:
                    reason = "estimate debt cleared"
            elif al["trigger"] in ("status_at_risk", "status_blocked") and a is not None:
                reply = _latest_reply(self.conn, a["id"])
                if reply and reply["status"] == "on_track":
                    reason = "contributor reports on track again"
            elif al["trigger"] == "status_no_response" and a is not None:
                last = self.conn.execute("SELECT status FROM status_checks WHERE assignment_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
                if last and last["status"] in ("on_track", "at_risk", "blocked"):
                    reason = "contributor replied"
            elif al["trigger"] == "due_date_at_risk" and a is not None:
                if not due_risk(cfg, a, latest_revision(self.conn, a["id"]), now)[0]:
                    reason = "no longer at risk"
            if reason:
                alerts.resolve(self.conn, al["id"], reason, now)

    # -- operator questions ------------------------------------------------
    def apply_safe_default(self, q, now) -> None:
        cfg = config.load(self.conn)
        with transaction(self.conn):
            alerts.mark_safe_default(self.conn, cfg, q, now)
            default = q["safe_default"]
            if default in PASSIVE_DEFAULTS:
                return
            c = self.ctx(cfg, now, actor="TrackingAgent(safe_default)")
            try:
                outcome = HANDLERS[q["type"]](c, q, default, {})
            except AnswerRejected as e:
                events.log(self.conn, "safe_default_not_applicable", ACTOR, task_id=q["task_id"], now=now, payload={"question_id": q["id"], "why": str(e)})
                return
            if outcome and outcome.resolved:
                alerts.record_answer(self.conn, q["id"], default, c.actor, f"applied after timeout: {outcome.detail}", now)
