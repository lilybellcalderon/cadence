"""Eligibility (hard rule) and capacity (soft rule) for a task, per DESIGN section 2."""


def hours_for(task, cfg) -> float:
    """What a not-yet-assigned task counts for: the requester's provisional estimate x the unconfirmed buffer."""
    return round(task["estimated_effort_hours"] * cfg["estimates"]["model"]["unconfirmed_buffer"], 2)


def check(conn, cfg, contributor_id: str, task) -> dict:
    """Would this contributor stay within limits if given the task? Capacity never makes anyone ineligible;
    the result is used to prefer contributors who fit and to explain a conflict to the operator."""
    c = conn.execute("SELECT * FROM v_contributor_load WHERE contributor_id = ?", (contributor_id,)).fetchone()
    need = hours_for(task, cfg)
    reasons = []
    if c["active_tasks"] + 1 > c["max_concurrent_tasks"]:
        reasons.append(f"{c['active_tasks'] + 1} tasks > max {c['max_concurrent_tasks']}")
    if c["load_hours"] + need > c["available_hours"]:
        reasons.append(f"{c['load_hours'] + need:g}h > {c['available_hours']:g}h available")
    debt_max = cfg["estimates"]["estimate_debt"]["max_overdue_estimates_per_contributor"]
    if c["overdue_estimates"] > debt_max:
        reasons.append(f"estimate debt: {c['overdue_estimates']} overdue estimates (max {debt_max})")
    buffer = cfg["estimates"]["model"]["unconfirmed_buffer"]
    return {
        "contributor_id": contributor_id,
        "fits": not reasons,
        "reasons": reasons,
        "active_tasks": c["active_tasks"],
        "unconfirmed_assignments": c["unconfirmed_assignments"],
        "load_hours": c["load_hours"],
        "task_hours": need,
        "available_hours": c["available_hours"],
        "utilization": round((c["load_hours"] + need) / c["available_hours"], 3),
        "basis": (f"current load {c['load_hours']:g}h across {c['active_tasks']} task(s) "
                  f"({c['unconfirmed_assignments']} on unconfirmed estimates) + this task {need:g}h "
                  f"(requester estimate x{buffer:g}, unconfirmed)"),
    }


def eligible(conn, cfg, task, exclude=()) -> list[dict]:
    """Eligible contributors ranked best-first: those who fit, then lowest utilisation, then lowest level
    (do not spend experts on work a practitioner can do), then id. Excludes the task's routing exclusions."""
    rows = conn.execute(
        "SELECT e.contributor_id, e.level, c.name FROM v_eligible_contributors e JOIN contributors c ON c.id = e.contributor_id "
        "WHERE e.task_id = ? AND e.contributor_id NOT IN (SELECT contributor_id FROM routing_exclusions WHERE task_id = ?)",
        (task["id"], task["id"])).fetchall()
    out = []
    for r in rows:
        if r["contributor_id"] in exclude:
            continue
        chk = check(conn, cfg, r["contributor_id"], task)
        out.append({"id": r["contributor_id"], "name": r["name"], "level": r["level"], **chk})
    out.sort(key=lambda e: (not e["fits"], e["utilization"], e["level"], e["id"]))
    return out
