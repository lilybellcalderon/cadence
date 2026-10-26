"""
Validates the seed datasets and prints, per task, which contributors the routing
engine would consider eligible. Run:  python validate_seed.py

Eligibility (the baseline rule the simulated engine will use):
  contributor has the task's area at level >= task.min_level
  AND availability == "available"
Capacity is a SOFT constraint (not part of eligibility): if every eligible contributor would exceed
max_concurrent_tasks or available_hours, the engine still picks, a capacity conflict is logged for the
operator, and the task is queued with a resource-usage warning.
"""
import json, sys
from pathlib import Path

DATA = Path(__file__).parent / "data"
load = lambda n: json.loads((DATA / n).read_text(encoding="utf-8"))

areas_doc, contribs_doc, tasks_doc = load("expertise_areas.json"), load("contributors.json"), load("tasks.json")
areas = {a["id"]: a for a in areas_doc["areas"]}
contribs = {c["id"]: c for c in contribs_doc["contributors"]}
tasks = tasks_doc["tasks"]
errors = []

def err(msg): errors.append(msg)

# ---- referential integrity -------------------------------------------------
for a in areas.values():
    for adj in a["adjacent_areas"]:
        if adj not in areas: err(f"area {a['id']}: unknown adjacent area {adj}")
    if set(a["level_rubric"]) != {"1", "2", "3", "4"}: err(f"area {a['id']}: level_rubric must cover levels 1-4")

for c in contribs.values():
    if c["availability"] not in ("available", "away", "offline"): err(f"{c['id']}: bad availability")
    for e in c["expertise"]:
        if e["area"] not in areas: err(f"{c['id']}: unknown area {e['area']}")
        elif not 1 <= e["level"] <= 4: err(f"{c['id']}: bad level {e['level']}")
        else:
            valid = {s["id"] for s in areas[e["area"]]["sub_skills"]}
            for s in e.get("sub_skills", []):
                if s not in valid: err(f"{c['id']}: sub_skill {s} not in area {e['area']}")

for c in contribs.values():
    if not isinstance(c.get("available_hours"), (int, float)) or c["available_hours"] <= 0: err(f"{c['id']}: available_hours must be > 0")

seen = set()
for t in tasks:
    if not isinstance(t.get("estimated_effort_hours"), (int, float)) or t["estimated_effort_hours"] <= 0:
        err(f"{t['id']}: estimated_effort_hours must be > 0")
    if t["id"] in seen: err(f"duplicate task id {t['id']}")
    seen.add(t["id"])
    if t["area"] not in areas: err(f"{t['id']}: unknown area {t['area']}")
    if t["priority"] not in tasks_doc["_meta"]["priority_values"]: err(f"{t['id']}: bad priority")
    kinds = {a["kind"] for a in t["attachments"]}
    for need in ("requirements", "acceptance_criteria"):
        if need not in kinds: err(f"{t['id']}: missing {need} attachment")
    for a in t["attachments"]:
        if a["kind"] not in tasks_doc["_meta"]["attachment_kinds"]: err(f"{t['id']}: bad attachment kind {a['kind']}")
        if not (DATA / a["path"]).is_file(): err(f"{t['id']}: attachment file missing {a['path']}")

# ---- eligibility + scenario checks ----------------------------------------
def eligible(t):
    out = []
    for c in contribs.values():
        lvl = next((e["level"] for e in c["expertise"] if e["area"] == t["area"]), 0)
        if lvl >= t["min_level"] and c["availability"] == "available":
            out.append((c["id"], lvl))
    return sorted(out, key=lambda x: -x[1])

print(f"{'task':7} {'area':17} {'min':3} {'prio':8} {'scenario':30} eligible (id:level)")
for t in tasks:
    el = eligible(t)
    print(f"{t['id']:7} {t['area']:17} {t['min_level']:<3} {t['priority']:8} {t['sim']['scenario']:30} "
          + (", ".join(f"{i}:{l}" for i, l in el) or "NONE"))
    sim = t["sim"]
    if sim["scenario"] == "no_match" and el: err(f"{t['id']}: no_match scenario but eligible exist {el}")
    if sim["scenario"] != "no_match" and not el: err(f"{t['id']}: no eligible contributor")
    fp = sim.get("forced_pick")
    if fp and fp not in [i for i, _ in el]: err(f"{t['id']}: forced_pick {fp} is not eligible")

# ---- capacity-conflict preview ---------------------------------------------
def conflicts(contributor_id, assigned_ids):
    """Reasons the contributor would be over capacity holding these tasks (empty = fits)."""
    c, held = contribs[contributor_id], [t for t in tasks if t["id"] in assigned_ids]
    hrs = sum(t["estimated_effort_hours"] for t in held)
    out = []
    if len(held) > c["max_concurrent_tasks"]: out.append(f"{len(held)} tasks > max {c['max_concurrent_tasks']}")
    if hrs > c["available_hours"]: out.append(f"{hrs}h > {c['available_hours']}h available")
    return out

print("\nCapacity-conflict preview (each task alone, and the T-1002 + T-1010 contention pair on C-03):")
for t in tasks:
    el = eligible(t)
    solo = {i: conflicts(i, {t["id"]}) for i, _ in el}
    if el and all(solo.values()): print(f"  {t['id']}: every eligible contributor conflicts even when idle: {solo}")
pair = conflicts("C-03", {"T-1002", "T-1010"})
print(f"  C-03 holding T-1002 + T-1010 -> {pair or 'fits'}")
if not pair: err("expected a capacity conflict for C-03 holding T-1002 + T-1010")
for tid in ("T-1002", "T-1010"):
    if conflicts("C-03", {tid}): err(f"C-03 should fit {tid} on its own")

# ---- engine config: alert policy + operator questions ----------------------
cfg = load("engine_config.json")
al, qs = cfg["alerts"], cfg["operator_questions"]["types"]
sev_by_rank = {v: k for k, v in al["severity_rank"].items()}
prios = set(tasks_doc["_meta"]["priority_values"])
for key in ("stuck_threshold_seconds_by_priority",):
    if set(cfg["routing"][key]) != prios: err(f"routing.{key} must cover {sorted(prios)}")
for key in ("answer_timeout_minutes_by_priority",):
    if set(cfg["operator_questions"][key]) != prios: err(f"operator_questions.{key} must cover {sorted(prios)}")
if set(cfg["contributor_engagement"]["status_check"]["cadence_minutes_by_priority"]) != prios: err("status_check cadence must cover all priorities")
if set(al["delivery_by_severity"]) != set(al["severity_rank"]): err("delivery_by_severity must cover every severity")
for name, trg in al["triggers"].items():
    q = trg["question"]
    if trg["needs_action"] != (q is not None): err(f"trigger {name}: needs_action and question disagree")
    if q is not None and q not in qs: err(f"trigger {name}: unknown question type {q}")
for name, q in qs.items():
    if q["safe_default"] not in q["options"]: err(f"question {name}: safe_default not among options")
    if q["safe_default"] in ("reassign_task", "cancel_task", "assign_anyway", "assign_manually"):
        err(f"question {name}: safe_default must not be a state-changing action")
replies = {"on_track", "at_risk", "blocked", "no_response"}
for t in tasks:
    r = t["sim"].get("status_check", {}).get("reply")
    if r and r not in replies: err(f"{t['id']}: bad status_check reply {r}")

def severity(priority, trigger):
    rank = al["priority_rank"][priority] + al["triggers"][trigger]["adjust"]
    floor = 1 if al["triggers"][trigger]["needs_action"] else 0
    return sev_by_rank[max(floor, min(4, rank))]

print("\nAlert severity by task priority (trigger -> severity):")
shown = ["stuck_request", "no_match", "capacity_conflict", "assignment_failed", "status_blocked", "late_return_superseded"]
print(f"  {'priority':9}" + "".join(f"{s:>24}" for s in shown))
for p in ("critical", "high", "medium", "low"):
    print(f"  {p:9}" + "".join(f"{severity(p, s):>24}" for s in shown))

# ---- TrackingAgent: estimate policy ----------------------------------------
est = cfg["estimates"]
for key in ("sla_minutes_by_priority", "stale_after_minutes_by_priority"):
    if set(est[key]) != prios: err(f"estimates.{key} must cover {sorted(prios)}")
for name, trg in al["triggers"].items():
    if trg.get("agent") not in cfg["agents"]: err(f"trigger {name}: unknown agent {trg.get('agent')}")
for pr, sla in est["sla_minutes_by_priority"].items():
    if not sla["estimate_due"] > 0 or not sla["reminder_every"] > 0: err(f"sla {pr}: values must be > 0")
    if sla["estimate_due"] > cfg["contributor_engagement"]["status_check"]["cadence_minutes_by_priority"][pr]:
        err(f"sla {pr}: estimate_due must not exceed the status-check cadence")
modes = {"with_accept", "after_reminder", "never"}
buf, vpct = est["model"]["unconfirmed_buffer"], est["validation_rules"]["variance_flag_pct"]
print("\nTrackingAgent estimate SLA (simulated minutes from acceptance; at demo compression 60, 1 sim minute = 1 real second):")
print(f"  {'priority':9}{'estimate due':>14}{'reminder every':>16}{'operator alert at':>19}")
for pr in ("critical", "high", "medium", "low"):
    s_ = est["sla_minutes_by_priority"][pr]
    print(f"  {pr:9}{s_['estimate_due']:>14}{s_['reminder_every']:>16}{s_['estimate_due'] + s_['reminder_every'] * s_['max_reminders']:>19}")
print("\nScripted estimate outcomes:")
for t in tasks:
    e = t["sim"].get("estimate")
    if not e: continue
    if e["mode"] not in modes: err(f"{t['id']}: bad estimate mode {e['mode']}")
    if e["mode"] != "never":
        if e.get("confidence") not in ("high", "medium", "low") or not e.get("hours_total"): err(f"{t['id']}: estimate needs hours_total and confidence")
        delta = (e["hours_total"] - t["estimated_effort_hours"]) / t["estimated_effort_hours"] * 100
        flag = "  -> estimate_variance alert" if abs(delta) > vpct else ""
        print(f"  {t['id']} {e['mode']:15} requester {t['estimated_effort_hours']:>3}h vs contributor {e['hours_total']:>3}h ({delta:+.0f}%, confidence {e['confidence']}){flag}")
    else:
        print(f"  {t['id']} never -> overdue_estimate -> reminders -> estimate_overdue alert + operator question")
c03 = contribs["C-03"]["available_hours"]
unconf = (12 + 30) * buf
print(f"\nCapacity basis demo (C-03, {c03}h available): T-1002 + T-1010 = 42h confirmed, "
      f"{unconf:.1f}h if both are only requester estimates (x{buf}).")

print()
away = [c["id"] for c in contribs.values() if c["availability"] != "available"]
print(f"{len(areas)} areas | {len(contribs)} contributors ({len(away)} not available: {away}) | {len(tasks)} tasks")
if errors:
    print("\nFAILED:"); [print("  -", e) for e in errors]; sys.exit(1)
print("OK: all references resolve, attachments exist, scenarios consistent with data.")
