"""Append-only history. Every state change, decision and operator action goes through here."""
import json

from . import clock


def log(conn, event_type: str, actor: str, *, task_id=None, assignment_id=None, payload=None, now=None) -> int:
    cur = conn.execute(
        "INSERT INTO events (ts, task_id, assignment_id, event_type, actor, payload) VALUES (?, ?, ?, ?, ?, ?)",
        (clock.iso(now), task_id, assignment_id, event_type, actor, json.dumps(payload or {}, ensure_ascii=False)))
    return cur.lastrowid
