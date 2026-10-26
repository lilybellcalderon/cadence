"""
Daily report (DESIGN decision 1 and section 9.5): the end-of-day log of every record that needed attention, plus the
estimate data-quality metrics. Built only from the event log and the alert/estimate tables, so it can be regenerated
for any past date. Dates are UTC.
"""
import json
import statistics
from datetime import timedelta

from . import clock

ATTENTION_TRIGGERS = ("stuck_request", "no_match", "capacity_conflict", "priority_contention", "request_declined", "assignment_failed",
                      "estimate_overdue", "estimate_stale", "estimate_variance", "estimate_debt", "status_no_response",
                      "status_at_risk", "status_blocked", "due_date_at_risk")


def generate(conn, cfg, date: str, now=None, persist: bool = True) -> dict:
    now = now or clock.now()
    lo, hi = date + "T00:00:00Z", date + "T99"
    factor = cfg["contributor_engagement"]["status_check"]["demo_compression_factor"]

    def q(sql, *args):
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    flow = {"created": 0, "submitted": 0, "accepted": 0, "started": 0, "completed": 0, "cancelled": 0}
    for e in q("SELECT event_type, payload FROM events WHERE ts >= ? AND ts < ?", lo, hi):
        p = json.loads(e["payload"])
        if e["event_type"] == "task_created":
            flow["created"] += 1
        elif e["event_type"] == "task_status_changed" and p.get("to") in ("submitted", "accepted", "completed", "cancelled"):
            flow[p["to"]] += 1
        elif e["event_type"] == "work_started":
            flow["started"] += 1

    raised = q("SELECT a.id, a.task_id, a.trigger, a.agent, a.severity, a.status, a.message, a.created_at, a.acknowledged_by, a.resolved_at, "
               "(SELECT COUNT(*) FROM outbox o WHERE o.alert_id = a.id) AS outbox_messages FROM alerts a WHERE a.created_at >= ? AND a.created_at < ? "
               "AND a.severity <> 'info' ORDER BY a.id", lo, hi)
    needing_attention = {}
    for a in raised:
        if a["trigger"] in ATTENTION_TRIGGERS:
            needing_attention.setdefault(a["trigger"], []).append(a)

    late_returns = q("SELECT task_id, payload, ts FROM events WHERE event_type = 'late_return_superseded' AND ts >= ? AND ts < ?", lo, hi)
    operator_actions = q("SELECT ts, task_id, event_type, actor, payload FROM events WHERE actor LIKE 'operator:%' AND ts >= ? AND ts < ? "
                         "AND event_type NOT IN ('alert_acknowledged') ORDER BY id", lo, hi)
    acks = q("SELECT COUNT(*) AS n FROM events WHERE event_type = 'alert_acknowledged' AND ts >= ? AND ts < ?", lo, hi)[0]["n"]
    questions = {
        "asked": q("SELECT COUNT(*) n FROM operator_questions WHERE asked_at >= ? AND asked_at < ?", lo, hi)[0]["n"],
        "answered_by_operator": q("SELECT COUNT(*) n FROM operator_questions WHERE answered_at >= ? AND answered_at < ? AND answered_by LIKE 'operator:%'", lo, hi)[0]["n"],
        "answered_by_safe_default": q("SELECT COUNT(*) n FROM operator_questions WHERE answered_at >= ? AND answered_at < ? AND answered_by LIKE '%(safe_default)'", lo, hi)[0]["n"],
        "safe_default_applied_without_closing": q("SELECT COUNT(*) n FROM events WHERE event_type = 'question_safe_default_applied' AND ts >= ? AND ts < ?", lo, hi)[0]["n"],
        "still_open": q("SELECT id, task_id, type, asked_at, answer_due_at, safe_default_applied_count FROM operator_questions WHERE status = 'open' ORDER BY id"),
    }

    # ---- estimate data quality (DESIGN 9.5)
    accepted = q("SELECT a.id, a.task_id, a.decided_at, a.estimate_due_at, t.priority, "
                 "(SELECT MIN(submitted_at) FROM estimate_revisions e WHERE e.assignment_id = a.id AND e.source <> 'operator_on_behalf') AS first_estimate "
                 "FROM assignments a JOIN tasks t ON t.id = a.task_id WHERE a.decided_at >= ? AND a.decided_at < ? AND a.estimate_due_at IS NOT NULL", lo, hi)
    in_sla = [a for a in accepted if a["first_estimate"] and a["first_estimate"] <= a["estimate_due_at"]]
    by_priority: dict[str, list[float]] = {}
    for a in accepted:
        if a["first_estimate"]:
            minutes = (clock.parse(a["first_estimate"]) - clock.parse(a["decided_at"])).total_seconds() / 60 * factor
            by_priority.setdefault(a["priority"], []).append(minutes)
    now_bad = q("SELECT a.id, a.task_id, a.contributor_id, a.plan_health FROM assignments a WHERE a.status IN ('accepted', 'in_progress') "
                "AND a.plan_health IN ('overdue_estimate', 'stale') ORDER BY a.id")
    debt_limit = cfg["estimates"]["estimate_debt"]["max_overdue_estimates_per_contributor"]
    debtors = q("SELECT contributor_id, overdue_estimates FROM v_contributor_load WHERE overdue_estimates > ?", debt_limit)
    conflicts = q("SELECT task_id, payload FROM events WHERE event_type = 'capacity_conflict' AND ts >= ? AND ts < ?", lo, hi)
    on_unconfirmed = [c["task_id"] for c in conflicts if json.loads(c["payload"]).get("unconfirmed_assignments", 0) > 0]
    quality = {
        "estimate_coverage_pct": round(100 * len(in_sla) / len(accepted), 1) if accepted else None,
        "assignments_accepted": len(accepted),
        "estimates_within_sla": len(in_sla),
        "median_minutes_to_estimate_by_priority": {p: round(statistics.median(v), 1) for p, v in sorted(by_priority.items())},
        "currently_overdue_or_stale": now_bad,
        "contributors_with_estimate_debt": debtors,
        "capacity_decisions_on_unconfirmed_estimates": on_unconfirmed,
        "note": "Times are simulated minutes (demo compression factor %d)." % factor,
    }

    touched = q("SELECT DISTINCT task_id FROM events WHERE task_id IS NOT NULL AND ts >= ? AND ts < ? AND event_type <> 'task_created'", lo, hi)
    tasks = []
    for r in touched:
        t = conn.execute(
            "SELECT t.id, t.title, t.priority, t.status, t.created_at, c.name AS assignee, a.plan_health FROM tasks t "
            "LEFT JOIN assignments a ON a.task_id = t.id AND a.status IN ('offered', 'accepted', 'in_progress', 'completed') "
            "LEFT JOIN contributors c ON c.id = a.contributor_id WHERE t.id = ? ORDER BY a.id DESC LIMIT 1", (r["task_id"],)).fetchone()
        if t:
            d = dict(t)
            d["age_minutes"] = int((now - clock.parse(d["created_at"])).total_seconds() // 60)
            tasks.append(d)
    tasks.sort(key=lambda d: d["id"])

    content = {
        "date": date,
        "generated_at": clock.iso(now),
        "flow": flow,
        "needing_attention": needing_attention,
        "late_returns_ignored": [{"task_id": r["task_id"], "ts": r["ts"], **json.loads(r["payload"])} for r in late_returns],
        "operator_interventions": [{"ts": r["ts"], "task_id": r["task_id"], "event": r["event_type"], "actor": r["actor"], **json.loads(r["payload"])} for r in operator_actions],
        "alerts_acknowledged": acks,
        "questions": questions,
        "estimate_quality": quality,
        "tasks_touched": tasks,
    }
    if persist:
        conn.execute("INSERT INTO daily_reports (report_date, generated_at, content) VALUES (?, ?, ?) "
                     "ON CONFLICT(report_date) DO UPDATE SET generated_at = excluded.generated_at, content = excluded.content",
                     (date, clock.iso(now), json.dumps(content, ensure_ascii=False)))
    return content


def ensure_previous_day(conn, cfg, now) -> bool:
    """Called by the worker: when the UTC date rolls over, write the report for the day that just ended (once)."""
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if conn.execute("SELECT 1 FROM daily_reports WHERE report_date = ?", (yesterday,)).fetchone():
        return False
    if conn.execute("SELECT 1 FROM events WHERE ts >= ? AND ts < ? LIMIT 1", (yesterday + "T00:00:00Z", yesterday + "T99")).fetchone() is None:
        return False
    generate(conn, cfg, yesterday, now)
    return True


def render_text(report: dict) -> str:
    """A plain-text version for the console / email body."""
    q, f = report["estimate_quality"], report["flow"]
    lines = [f"DAILY REPORT {report['date']}  (generated {report['generated_at']})", "",
             "ESTIMATE DATA QUALITY",
             f"  coverage within SLA: {q['estimate_coverage_pct']}%  ({q['estimates_within_sla']}/{q['assignments_accepted']} accepted assignments)",
             f"  median minutes to estimate: {q['median_minutes_to_estimate_by_priority'] or 'n/a'}",
             f"  overdue or stale now: {[a['task_id'] for a in q['currently_overdue_or_stale']] or 'none'}",
             f"  contributors with estimate debt: {[d['contributor_id'] for d in q['contributors_with_estimate_debt']] or 'none'}",
             f"  capacity decisions on unconfirmed estimates: {q['capacity_decisions_on_unconfirmed_estimates'] or 'none'}", "",
             f"FLOW  created {f['created']}, submitted {f['submitted']}, accepted {f['accepted']}, started {f['started']}, completed {f['completed']}, cancelled {f['cancelled']}", "",
             "NEEDING ATTENTION"]
    if not report["needing_attention"]:
        lines.append("  nothing raised")
    for trigger, items in report["needing_attention"].items():
        for a in items:
            lines.append(f"  [{a['severity']:8}] {trigger:20} {a['task_id']}  {a['status']:12} {a['message']}")
    lines += ["", f"OPERATOR INTERVENTIONS: {len(report['operator_interventions'])}   LATE RESULTS IGNORED: {len(report['late_returns_ignored'])}   "
              f"QUESTIONS asked {report['questions']['asked']}, answered by operator {report['questions']['answered_by_operator']}, "
              f"open {len(report['questions']['still_open'])}"]
    return "\n".join(lines)
