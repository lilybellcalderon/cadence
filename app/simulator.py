"""
The simulated routing engine (a black box that may be slow or never answer). Contributors are in contributor_sim.py.
`sim_*` columns on routing requests are the simulation's private state and are never shown to the operator.
"""
import json
import random
from datetime import timedelta

from . import clock


def _sim(task) -> dict:
    return json.loads(task["sim"]) if task["sim"] else {}


def plan_engine(task, ranked_eligible: list[dict], cfg: dict, rng: random.Random, now):
    """Decide, once, when the engine will answer and what it will pick. Stored on the request so a restart
    re-arms the same behaviour. Returns (returns_at_iso | None for 'never returns', pick_contributor_id | None)."""
    sim = _sim(task)
    lo, hi = cfg["routing"]["normal_latency_seconds"]
    mode = sim.get("routing", "random")
    if mode == "hang" or (mode == "random" and rng.random() < cfg["routing"]["hang_probability"]):
        returns_at = None
    elif mode == "late_return":
        returns_at = clock.iso(now + timedelta(seconds=sim.get("late_return_seconds", 90)))
    else:
        returns_at = clock.iso(now + timedelta(seconds=rng.uniform(lo, hi)))
    ids = [e["id"] for e in ranked_eligible]
    forced = sim.get("forced_pick")
    pick = forced if forced in ids else (ids[0] if ids else None)
    return returns_at, pick
