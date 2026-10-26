"""
Operator API + UI. Run:  python -m app.server [--db PATH] [--port 8000] [--seed N] [--no-worker]

Standard library only (http.server). One process serves the single-page UI, the JSON API and, unless --no-worker, runs the
agents in a background thread. Binds to 127.0.0.1: there is no authentication (the operator name is just recorded on actions).
Every request opens its own SQLite connection; all writes go through the same service functions the tests exercise.
"""
import argparse
import hashlib
import json
import mimetypes
import random
import re
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import actions, alerts, assignments, capacity, clock, config, contributor_sim, events, reports, tasks, tracking
from .db import DATA_DIR, DEFAULT_DB, ROOT, connect, init_schema, transaction

STATIC = Path(__file__).with_name("static")
VAR_DIR = ROOT / "var"
ROUTES: list[tuple[str, re.Pattern, callable]] = []


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def _var_dir(conn) -> Path:
    """Runtime files (uploads) live next to the database in use, so a test or alternative database never touches the real ones."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    return Path(path).parent if path else VAR_DIR


def _rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _json(value, default=None):
    return json.loads(value) if value else default


def _need(body, *keys):
    missing = [k for k in keys if body.get(k) in (None, "")]
    if missing:
        raise HttpError(400, f"missing: {', '.join(missing)}")


def _num(body, key, minimum=0.0, maximum=1000.0):
    try:
        v = float(body[key])
    except (KeyError, TypeError, ValueError):
        raise HttpError(400, f"{key} must be a number")
    if not minimum < v <= maximum:
        raise HttpError(400, f"{key} must be greater than {minimum:g} and at most {maximum:g}")
    return v


# ------------------------------------------------------------------ reads
def board_rows(conn, cfg, now):
    stuck_after = cfg["routing"]["stuck_threshold_seconds_by_priority"]
    out = []
    for t in _rows(conn, "SELECT * FROM tasks ORDER BY created_at"):
        a = conn.execute(
            "SELECT a.*, c.name AS contributor_name FROM assignments a JOIN contributors c ON c.id = a.contributor_id WHERE a.task_id = ? "
            "ORDER BY (a.status IN ('offered', 'accepted', 'in_progress')) DESC, a.id DESC LIMIT 1", (t["id"],)).fetchone()
        req = conn.execute("SELECT id, started_at, status FROM routing_requests WHERE task_id = ? AND status = 'pending'", (t["id"],)).fetchone()
        rev = tracking.latest_revision(conn, a["id"]) if a else None
        flags = []
        stuck = None
        if req:
            elapsed = int((now - clock.parse(req["started_at"])).total_seconds())
            stuck = {"request_id": req["id"], "started_at": req["started_at"], "elapsed_seconds": elapsed, "threshold_seconds": stuck_after[t["priority"]], "flagged": elapsed >= stuck_after[t["priority"]]}
            if stuck["flagged"]:
                flags.append("possibly stuck")
        if t["status"] == "queued":
            flags.append("queued: capacity")
        if t["hold_reason"]:
            flags.append(f"holding: {t['hold_reason']}")
        if a and a["status"] in ("accepted", "in_progress") and a["plan_health"] not in ("complete", "awaiting_estimate"):
            flags.append(a["plan_health"].replace("_", " "))
        counts = conn.execute("SELECT (SELECT COUNT(*) FROM alerts WHERE task_id = ? AND status <> 'resolved' AND severity <> 'info') AS alerts, "
                              "(SELECT COUNT(*) FROM operator_questions WHERE task_id = ? AND status = 'open') AS questions", (t["id"], t["id"])).fetchone()
        worst = conn.execute("SELECT severity FROM alerts WHERE task_id = ? AND status <> 'resolved' ORDER BY CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
                             "WHEN 'normal' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC LIMIT 1", (t["id"],)).fetchone()
        out.append({
            "id": t["id"], "title": t["title"], "area_id": t["area_id"], "min_level": t["min_level"], "priority": t["priority"], "status": t["status"],
            "created_at": t["created_at"], "age_minutes": int((now - clock.parse(t["created_at"])).total_seconds() // 60), "due_at": t["due_at"],
            "requester_hours": t["estimated_effort_hours"], "parent_task_id": t["parent_task_id"],
            "assignee": a["contributor_name"] if a else None, "assignee_id": a["contributor_id"] if a else None,
            "assignment_status": a["status"] if a else None, "assignment_id": a["id"] if a else None,
            "plan_health": a["plan_health"] if a and a["status"] in ("accepted", "in_progress") else None,
            "confirmed_hours": {"total": rev["hours_total"], "remaining": rev["hours_remaining"], "confidence": rev["confidence"], "source": rev["source"]} if rev else None,
            "routing": stuck, "flags": flags, "open_alerts": counts["alerts"], "open_questions": counts["questions"],
            "worst_alert": worst["severity"] if worst else None,
        })
    return out


def task_detail(conn, cfg, task_id, now):
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if t is None:
        raise HttpError(404, f"unknown task {task_id}")
    task = dict(t)
    task["tags"] = _json(task["tags"], [])
    task.pop("sim", None)                                                  # simulation scripting is never shown to the operator
    area = conn.execute("SELECT id, name, summary FROM expertise_areas WHERE id = ?", (t["area_id"],)).fetchone()
    assigns = _rows(conn, "SELECT a.*, c.name AS contributor_name FROM assignments a JOIN contributors c ON c.id = a.contributor_id WHERE a.task_id = ? ORDER BY a.id", task_id)
    for a in assigns:
        a["revisions"] = _rows(conn, "SELECT * FROM estimate_revisions WHERE assignment_id = ? ORDER BY revision", a["id"])
        a["estimate_requests"] = _rows(conn, "SELECT * FROM estimate_requests WHERE assignment_id = ? ORDER BY sequence", a["id"])
        a["status_checks"] = _rows(conn, "SELECT * FROM status_checks WHERE assignment_id = ? ORDER BY id", a["id"])
    requests = _rows(conn, "SELECT id, attempt, started_at, status, eligible, stuck_flagged_at, stuck_alerts, late_returned_at, finished_at, result "
                           "FROM routing_requests WHERE task_id = ? ORDER BY id", task_id)       # sim_* columns deliberately omitted
    stuck_after = cfg["routing"]["stuck_threshold_seconds_by_priority"][t["priority"]]
    for r in requests:
        r["eligible"], r["result"] = _json(r["eligible"], []), _json(r["result"])
        if r["status"] == "pending":
            r["elapsed_seconds"] = int((now - clock.parse(r["started_at"])).total_seconds())
            r["possibly_stuck"] = r["elapsed_seconds"] >= stuck_after
    qs = _rows(conn, "SELECT * FROM operator_questions WHERE task_id = ? ORDER BY id", task_id)
    for q in qs:
        q["options"], q["context"] = _json(q["options"], []), _json(q["context"], {})
    evs = _rows(conn, "SELECT * FROM events WHERE task_id = ? ORDER BY id", task_id)
    for e in evs:
        e["payload"] = _json(e["payload"], {})
    contributors = []
    for c in _rows(conn, "SELECT c.id, c.name, c.title, c.availability FROM contributors c ORDER BY c.name"):
        el = conn.execute("SELECT level FROM v_eligible_contributors WHERE task_id = ? AND contributor_id = ?", (task_id, c["id"])).fetchone()
        chk = capacity.check(conn, cfg, c["id"], t)
        c.update({"eligible": el is not None, "level": el["level"] if el else None, "fits": chk["fits"], "reasons": chk["reasons"], "basis": chk["basis"]})
        contributors.append(c)
    return {"task": task, "area": dict(area) if area else None, "attachments": _rows(conn, "SELECT id, kind, filename, description, size_bytes FROM task_attachments WHERE task_id = ?", task_id),
            "assignments": assigns, "routing_requests": requests, "alerts": _rows(conn, "SELECT * FROM alerts WHERE task_id = ? ORDER BY id", task_id),
            "questions": qs, "events": evs, "contributors": contributors}


def contributors_view(conn, cfg):
    manual = contributor_sim.manual_ids(conn)
    out = []
    for c in _rows(conn, "SELECT c.*, l.active_tasks, l.load_hours, l.unconfirmed_assignments, l.overdue_estimates FROM contributors c "
                         "JOIN v_contributor_load l ON l.contributor_id = c.id ORDER BY c.name"):
        c["expertise"] = _rows(conn, "SELECT e.area_id, a.name AS area_name, e.level FROM contributor_expertise e JOIN expertise_areas a ON a.id = e.area_id "
                                     "WHERE e.contributor_id = ? ORDER BY e.level DESC", c["id"])
        c["manual"] = c["id"] in manual
        c["assignments"] = _rows(conn, "SELECT a.id, a.task_id, a.status, a.plan_health, t.title FROM assignments a JOIN tasks t ON t.id = a.task_id "
                                       "WHERE a.contributor_id = ? AND a.status IN ('offered', 'accepted', 'in_progress') ORDER BY a.id", c["id"])
        out.append(c)
    return out


def contributor_inbox(conn, contributor_id):
    if conn.execute("SELECT 1 FROM contributors WHERE id = ?", (contributor_id,)).fetchone() is None:
        raise HttpError(404, f"unknown contributor {contributor_id}")
    base = "a.contributor_id = ? "
    return {
        "offers": _rows(conn, "SELECT a.id, a.task_id, a.offered_at, t.title, t.priority, t.estimated_effort_hours AS requester_hours, t.due_at, t.story_i_want "
                              "FROM assignments a JOIN tasks t ON t.id = a.task_id WHERE " + base + "AND a.status = 'offered' ORDER BY a.id", contributor_id),
        "estimate_requests": _rows(conn, "SELECT er.id, er.assignment_id, er.kind, er.sequence, er.sent_at, er.due_at, a.task_id, t.title, t.priority FROM estimate_requests er "
                                         "JOIN assignments a ON a.id = er.assignment_id JOIN tasks t ON t.id = a.task_id WHERE " + base +
                                         "AND er.answered_at IS NULL AND a.status IN ('accepted', 'in_progress') ORDER BY er.id", contributor_id),
        "status_checks": _rows(conn, "SELECT s.id, s.assignment_id, s.sent_at, s.due_at, a.task_id, t.title FROM status_checks s JOIN assignments a ON a.id = s.assignment_id "
                                     "JOIN tasks t ON t.id = a.task_id WHERE " + base + "AND s.replied_at IS NULL AND s.status IS NULL AND a.status IN ('accepted', 'in_progress') ORDER BY s.id", contributor_id),
        "active": _rows(conn, "SELECT a.id, a.task_id, a.status, a.plan_health, a.estimate_due_at, t.title, t.priority, t.due_at, "
                              "(SELECT hours_remaining FROM estimate_revisions e WHERE e.assignment_id = a.id ORDER BY revision DESC LIMIT 1) AS hours_remaining "
                              "FROM assignments a JOIN tasks t ON t.id = a.task_id WHERE " + base + "AND a.status IN ('accepted', 'in_progress') ORDER BY a.id", contributor_id),
    }


@route("GET", "/api/meta")
def meta(conn, cfg, now, body, q):
    return {"now": clock.iso(now),
            "areas": _rows(conn, "SELECT id, name, summary FROM expertise_areas ORDER BY name"),
            "levels": _rows(conn, "SELECT level, name, meaning FROM expertise_levels ORDER BY level"),
            "priorities": ["critical", "high", "medium", "low"],
            "adjacency": _rows(conn, "SELECT area_id, adjacent_area_id FROM area_adjacency"),
            "stuck_threshold_seconds": cfg["routing"]["stuck_threshold_seconds_by_priority"],
            "demo_compression_factor": cfg["contributor_engagement"]["status_check"]["demo_compression_factor"],
            "estimate_sla_minutes": cfg["estimates"]["sla_minutes_by_priority"],
            "severity_delivery": cfg["alerts"]["delivery_by_severity"]}


@route("GET", "/api/board")
def board(conn, cfg, now, body, q):
    return {"now": clock.iso(now), "tasks": board_rows(conn, cfg, now),
            "counts": {"open_alerts": conn.execute("SELECT COUNT(*) FROM alerts WHERE status = 'open' AND severity <> 'info'").fetchone()[0],
                       "open_questions": conn.execute("SELECT COUNT(*) FROM operator_questions WHERE status = 'open'").fetchone()[0]}}


@route("GET", r"/api/tasks/(?P<tid>[\w-]+)")
def get_task(conn, cfg, now, body, q, tid):
    return task_detail(conn, cfg, tid, now)


@route("GET", "/api/inbox")
def inbox(conn, cfg, now, body, q):
    alerts_ = _rows(conn, "SELECT a.*, t.title FROM alerts a LEFT JOIN tasks t ON t.id = a.task_id WHERE a.status <> 'resolved' AND a.severity <> 'info' "
                          "ORDER BY CASE a.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'normal' THEN 2 ELSE 1 END DESC, a.id DESC")
    for a in alerts_:
        a["outbox"] = conn.execute("SELECT COUNT(*) FROM outbox WHERE alert_id = ?", (a["id"],)).fetchone()[0]
    qs = _rows(conn, "SELECT q.*, t.title, t.priority, t.area_id FROM operator_questions q JOIN tasks t ON t.id = q.task_id WHERE q.status = 'open' "
                     "ORDER BY CASE t.priority WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC, q.id")
    for x in qs:
        x["options"], x["context"] = _json(x["options"], []), _json(x["context"], {})
    return {"alerts": alerts_, "questions": qs, "outbox": _rows(conn, "SELECT * FROM outbox ORDER BY id DESC LIMIT 30"),
            "recently_resolved": _rows(conn, "SELECT a.id, a.task_id, a.trigger, a.severity, a.message, a.resolved_at FROM alerts a WHERE a.status = 'resolved' "
                                             "AND a.severity <> 'info' ORDER BY a.resolved_at DESC LIMIT 8")}


@route("GET", "/api/contributors")
def list_contributors(conn, cfg, now, body, q):
    return {"contributors": contributors_view(conn, cfg)}


@route("GET", r"/api/contributors/(?P<cid>[\w-]+)/inbox")
def get_inbox(conn, cfg, now, body, q, cid):
    return contributor_inbox(conn, cid)


@route("GET", "/api/report")
def get_report(conn, cfg, now, body, q):
    date = (q.get("date") or [now.strftime("%Y-%m-%d")])[0]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HttpError(400, "date must be YYYY-MM-DD")
    with transaction(conn):
        report = reports.generate(conn, cfg, date, now)
    return {"report": report, "text": reports.render_text(report)}


@route("GET", r"/api/attachments/(?P<aid>[\w-]+)")
def get_attachment(conn, cfg, now, body, q, aid):
    a = conn.execute("SELECT * FROM task_attachments WHERE id = ?", (aid,)).fetchone()
    if a is None:
        raise HttpError(404, "unknown attachment")
    base = (_var_dir(conn) if a["path"].startswith("uploads/") else DATA_DIR).resolve()
    full = (base / a["path"]).resolve()
    if not full.is_relative_to(base) or not full.is_file():
        raise HttpError(404, "attachment file not found")
    return ("raw", full.read_text(encoding="utf-8", errors="replace"), a["filename"])


# ------------------------------------------------------------------ writes
def _actor(handler_headers, body):
    return (body.get("actor") or handler_headers.get("X-Operator") or "operator").strip()[:40] or "operator"


@route("POST", "/api/tasks")
def create_task(conn, cfg, now, body, q):
    for k in ("title", "area_id", "priority", "description"):
        _need(body, k)
    story = body.get("user_story") or {}
    _need(story, "as_a", "i_want", "so_that")
    if not conn.execute("SELECT 1 FROM expertise_areas WHERE id = ?", (body["area_id"],)).fetchone():
        raise HttpError(400, "unknown area_id")
    if body["priority"] not in ("critical", "high", "medium", "low"):
        raise HttpError(400, "priority must be critical, high, medium or low")
    level = int(_num(body, "min_level", 0, 4))
    if level < 1 or level != float(body["min_level"]):
        raise HttpError(400, "min_level must be a whole number from 1 to 4")
    hours = _num(body, "estimated_effort_hours", 0, 500)
    atts = {a.get("kind"): a for a in body.get("attachments", []) if a.get("content", "").strip()}
    for need in ("requirements", "acceptance_criteria"):
        if need not in atts:
            raise HttpError(400, f"a {need.replace('_', ' ')} attachment with content is required")
    sim_mode = body.get("sim_routing")
    sim = None
    if sim_mode in ("normal", "hang", "late_return"):
        sim = {"routing": sim_mode, "contributor_response": "accept"}
        if sim_mode == "late_return":
            sim["late_return_seconds"] = 90
    actor = _actor(body.get("_headers", {}), body)
    with transaction(conn):
        n = max([int(m.group(1)) for (i,) in conn.execute("SELECT id FROM tasks").fetchall() if (m := re.fullmatch(r"T-(\d+)", i))] + [1000]) + 1
        tid = f"T-{n}"
        due = clock.iso(clock.ahead(float(body["due_in_hours"]), now)) if body.get("due_in_hours") else None
        conn.execute(
            "INSERT INTO tasks (id, title, area_id, min_level, priority, status, requester_name, requester_team, story_as_a, story_i_want, story_so_that, "
            "description, estimated_effort_hours, tags, sim, created_at, due_at, updated_at) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, body["title"].strip()[:200], body["area_id"], level, body["priority"], (body.get("requester") or {}).get("name") or actor,
             (body.get("requester") or {}).get("team") or "Operations", story["as_a"], story["i_want"], story["so_that"], body["description"], hours,
             json.dumps(body.get("tags", [])), json.dumps(sim) if sim else None, clock.iso(now), due, clock.iso(now)))
        events.log(conn, "task_created", f"operator:{actor}", task_id=tid, now=now, payload={"source": "operator", "priority": body["priority"], "area": body["area_id"]})
        folder = _var_dir(conn) / "uploads" / tid
        folder.mkdir(parents=True, exist_ok=True)
        for i, a in enumerate(body["attachments"], 1):
            if not a.get("content", "").strip():
                continue
            fname = re.sub(r"[^A-Za-z0-9._-]", "_", a.get("filename") or f"{a['kind']}.md")[:80]
            (folder / fname).write_text(a["content"], encoding="utf-8")
            raw = a["content"].encode("utf-8")
            conn.execute("INSERT INTO task_attachments (id, task_id, kind, filename, path, description, size_bytes, sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (f"A-{n}-{i}", tid, a["kind"], fname, f"uploads/{tid}/{fname}", a.get("description"), len(raw), hashlib.sha256(raw).hexdigest()))
        if body.get("submit"):
            tasks.move(conn, tid, "submitted", f"operator:{actor}", now=now, reason="submitted on creation")
    return {"id": tid, "status": "submitted" if body.get("submit") else "draft"}


def _op(conn, fn, *args, **kw):
    try:
        return fn(conn, *args, **kw)
    except actions.ActionError as e:
        raise HttpError(400, str(e)) from e
    except KeyError as e:
        raise HttpError(404, str(e)) from e


@route("POST", r"/api/tasks/(?P<tid>[\w-]+)/submit")
def submit(conn, cfg, now, body, q, tid):
    _op(conn, actions.submit_task, tid, _actor(body.get("_headers", {}), body), now=now)
    return {"ok": True}


@route("POST", r"/api/tasks/(?P<tid>[\w-]+)/assign")
def assign(conn, cfg, now, body, q, tid):
    _need(body, "contributor_id")
    warnings = _op(conn, actions.assign_task, tid, body["contributor_id"], _actor(body.get("_headers", {}), body), reason=body.get("reason"), now=now)
    return {"ok": True, "warnings": warnings}


@route("POST", r"/api/tasks/(?P<tid>[\w-]+)/cancel")
def cancel(conn, cfg, now, body, q, tid):
    _op(conn, actions.cancel_task, tid, _actor(body.get("_headers", {}), body), reason=body.get("reason"), now=now)
    return {"ok": True}


@route("POST", r"/api/alerts/(?P<aid>\d+)/ack")
def ack(conn, cfg, now, body, q, aid):
    actions.acknowledge_alert(conn, int(aid), _actor(body.get("_headers", {}), body), now=now)
    return {"ok": True}


@route("POST", r"/api/questions/(?P<qid>\d+)/answer")
def answer(conn, cfg, now, body, q, qid):
    _need(body, "option")
    out = _op(conn, actions.answer_question, int(qid), body["option"], _actor(body.get("_headers", {}), body),
              note=body.get("note"), params=body.get("params") or {}, now=now)
    return {"ok": True, "resolved": out.resolved, "detail": out.detail}


@route("POST", r"/api/contributors/(?P<cid>[\w-]+)/manual")
def set_manual(conn, cfg, now, body, q, cid):
    inbox_ = contributor_inbox(conn, cid)       # 404 if unknown
    with transaction(conn):
        contributor_sim.set_manual(conn, cid, bool(body.get("manual")))
        events.log(conn, "contributor_mode_changed", f"operator:{_actor(body.get('_headers', {}), body)}", now=now, payload={"contributor_id": cid, "manual": bool(body.get("manual"))})
    return {"ok": True}


@route("POST", r"/api/contributors/(?P<cid>[\w-]+)/availability")
def set_availability(conn, cfg, now, body, q, cid):
    if body.get("availability") not in ("available", "away", "offline"):
        raise HttpError(400, "availability must be available, away or offline")
    with transaction(conn):
        cur = conn.execute("UPDATE contributors SET availability = ?, away_reason = ? WHERE id = ?", (body["availability"], body.get("away_reason"), cid))
        if not cur.rowcount:
            raise HttpError(404, f"unknown contributor {cid}")
        events.log(conn, "contributor_availability_changed", f"operator:{_actor(body.get('_headers', {}), body)}", now=now,
                   payload={"contributor_id": cid, "availability": body["availability"], "reason": body.get("away_reason")})
    return {"ok": True}


def _assignment(conn, aid):
    a = conn.execute("SELECT * FROM assignments WHERE id = ?", (aid,)).fetchone()
    if a is None:
        raise HttpError(404, f"unknown assignment {aid}")
    return a


def _contributor_call(conn, cfg, now, aid, fn):
    a = _assignment(conn, aid)
    actor = f"contributor:{a['contributor_id']}"
    try:
        with transaction(conn):
            fn(a, actor)
    except (ValueError, assignments.EstimateRequired, tasks.InvalidTransition) as e:
        raise HttpError(400, str(e)) from e
    return {"ok": True}


@route("POST", r"/api/assignments/(?P<aid>\d+)/accept")
def accept(conn, cfg, now, body, q, aid):
    _need(body, "hours_total", "confidence")            # capture at accept: no estimate, no acceptance (DESIGN 9.2)
    hours = _num(body, "hours_total", 0, 500)
    if body["confidence"] not in ("high", "medium", "low"):
        raise HttpError(400, "confidence must be high, medium or low")
    return _contributor_call(conn, cfg, now, int(aid), lambda a, actor: tracking.accept_with_estimate(
        conn, cfg, a["id"], actor, now, hours_total=hours, confidence=body["confidence"], basis=body.get("basis") or "rough_guess"))


@route("POST", r"/api/assignments/(?P<aid>\d+)/decline")
def decline(conn, cfg, now, body, q, aid):
    return _contributor_call(conn, cfg, now, int(aid), lambda a, actor: assignments.decline(conn, a["id"], body.get("reason") or "declined by contributor", actor, now))


@route("POST", r"/api/assignments/(?P<aid>\d+)/estimate")
def estimate(conn, cfg, now, body, q, aid):
    hours = _num(body, "hours_total", 0, 500)
    remaining = body.get("hours_remaining")
    if remaining not in (None, ""):
        remaining = _num(body, "hours_remaining", -1, 500)
    else:
        remaining = None
    return _contributor_call(conn, cfg, now, int(aid), lambda a, actor: tracking.submit_estimate(
        conn, cfg, a["id"], hours_total=hours, hours_remaining=remaining, confidence=body.get("confidence") or "medium",
        basis=body.get("basis") or "rough_guess", actor=actor, now=now, note=body.get("note")))


@route("POST", r"/api/assignments/(?P<aid>\d+)/start")
def start(conn, cfg, now, body, q, aid):
    return _contributor_call(conn, cfg, now, int(aid), lambda a, actor: assignments.start(conn, a["id"], actor, now))


@route("POST", r"/api/assignments/(?P<aid>\d+)/complete")
def complete(conn, cfg, now, body, q, aid):
    return _contributor_call(conn, cfg, now, int(aid), lambda a, actor: assignments.complete(conn, a["id"], actor, now, body.get("note")))


@route("POST", r"/api/status-checks/(?P<cid>\d+)/reply")
def reply(conn, cfg, now, body, q, cid):
    chk = conn.execute("SELECT assignment_id FROM status_checks WHERE id = ?", (int(cid),)).fetchone()
    if chk is None:
        raise HttpError(404, "unknown status check")
    _need(body, "status")
    remaining = body.get("hours_remaining")
    return _contributor_call(conn, cfg, now, chk["assignment_id"], lambda a, actor: tracking.submit_status(
        conn, cfg, int(cid), status=body["status"], progress_percent=body.get("progress_percent"),
        hours_remaining=float(remaining) if remaining not in (None, "") else None, note=body.get("note"), actor=actor, now=now))


@route("POST", "/api/demo/submit-drafts")
def submit_drafts(conn, cfg, now, body, q):
    ids = body.get("task_ids") or [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status = 'draft' ORDER BY id")]
    done = []
    for tid in ids:
        try:
            actions.submit_task(conn, tid, _actor(body.get("_headers", {}), body), now=now)
            done.append(tid)
        except actions.ActionError:
            pass
    return {"submitted": done}


# ------------------------------------------------------------------ HTTP plumbing
class Handler(BaseHTTPRequestHandler):
    server_version = "Cadence/1.0"
    db_path = DEFAULT_DB

    def log_message(self, fmt, *args):      # keep the console for agent activity
        pass

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else (payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.removeprefix("/static/")
        full = (STATIC / name).resolve()
        if not full.is_relative_to(STATIC.resolve()) or not full.is_file():
            return self._send(404, {"error": "not found"})
        self._send(200, full.read_bytes(), (mimetypes.guess_type(full.name)[0] or "application/octet-stream") + "; charset=utf-8")

    def _dispatch(self, method):
        url = urlparse(self.path)
        if method == "GET" and not url.path.startswith("/api/"):
            return self._static(url.path)
        for m, pattern, fn in ROUTES:
            match = pattern.match(url.path)
            if m == method and match:
                break
        else:
            return self._send(404, {"error": f"no route for {method} {url.path}"})
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "body must be JSON"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "body must be a JSON object"})
            body["_headers"] = {"X-Operator": self.headers.get("X-Operator")}
        conn = connect(self.db_path)
        try:
            cfg, now = config.load(conn), clock.now()
            result = fn(conn, cfg, now, body, parse_qs(url.query), **match.groupdict())
            if isinstance(result, tuple) and result[0] == "raw":
                return self._send(200, result[1], "text/plain; charset=utf-8")
            self._send(200, result)
        except HttpError as e:
            self._send(e.status, {"error": str(e)})
        except Exception as e:                         # a bug must not look like success, but must not kill the server either
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            conn.close()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def make_server(db_path, host="127.0.0.1", port=8000) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"db_path": db_path})
    ThreadingHTTPServer.allow_reuse_address = True
    return ThreadingHTTPServer((host, port), handler)


def start_worker(db_path, seed=None, interval=1.0) -> threading.Event:
    from .worker import run_loop
    stop = threading.Event()
    threading.Thread(target=run_loop, args=(db_path, stop, interval, seed), daemon=True, name="agents").start()
    return stop


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Cadence operator console (API + UI + agents).")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for repeatable engine latency/hangs")
    ap.add_argument("--no-worker", action="store_true", help="serve only; run `python -m app.worker` separately")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    init_schema(conn)
    if conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0:
        print("database is empty; run `python -m app.seed` first", file=sys.stderr)
        return 1
    conn.close()
    if not args.no_worker:
        start_worker(args.db, args.seed)
    server = make_server(args.db, port=args.port)
    print(f"Operator console: http://127.0.0.1:{args.port}   (agents {'off' if args.no_worker else 'running'}; Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
