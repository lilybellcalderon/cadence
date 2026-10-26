"""
Loads data/*.json into SQLite.  Run:  python -m app.seed [--db PATH] [--reset]

Idempotent by design, because the app is expected to restart while work is active:
  * reference data (levels, areas, sub-skills, contributor profiles) is upserted - edits to the JSON take effect
  * tasks and attachments are INSERT-IF-MISSING - re-running never overwrites status, estimates or history
  * --reset deletes the database file first (the only way to get back to a pristine demo)
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from . import clock
from .db import DATA_DIR, DEFAULT_DB, connect, init_schema, reset_files, transaction


class SeedError(Exception):
    pass


def _load(data_dir: Path, name: str) -> dict:
    return json.loads((data_dir / name).read_text(encoding="utf-8"))


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _event(conn, ts, event_type, actor, payload, task_id=None):
    conn.execute("INSERT INTO events (ts, task_id, event_type, actor, payload) VALUES (?, ?, ?, ?, ?)",
                 (ts, task_id, event_type, actor, _j(payload)))


def _seed_config(conn, cfg: dict, ts: str) -> bool:
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    cur = conn.execute("INSERT OR IGNORE INTO config_snapshots (sha256, loaded_at, config) VALUES (?, ?, ?)",
                       (digest, ts, canonical))
    conn.execute("INSERT INTO settings (key, value) VALUES ('unconfirmed_buffer', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (str(cfg["estimates"]["model"]["unconfirmed_buffer"]),))
    conn.execute("INSERT INTO settings (key, value) VALUES ('config_sha256', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (digest,))
    return cur.rowcount == 1


def _seed_reference(conn, areas_doc: dict) -> None:
    for lv in areas_doc["levels"]:
        conn.execute("INSERT INTO expertise_levels (level, name, meaning) VALUES (?, ?, ?) "
                     "ON CONFLICT(level) DO UPDATE SET name = excluded.name, meaning = excluded.meaning",
                     (lv["level"], lv["name"], lv["meaning"]))
    for a in areas_doc["areas"]:
        conn.execute(
            "INSERT INTO expertise_areas (id, name, summary, description, in_scope, out_of_scope, level_rubric, "
            "typical_deliverables, example_tasks, keywords) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, summary = excluded.summary, "
            "description = excluded.description, in_scope = excluded.in_scope, out_of_scope = excluded.out_of_scope, "
            "level_rubric = excluded.level_rubric, typical_deliverables = excluded.typical_deliverables, "
            "example_tasks = excluded.example_tasks, keywords = excluded.keywords",
            (a["id"], a["name"], a["summary"], a["description"], _j(a["in_scope"]), _j(a["out_of_scope"]),
             _j(a["level_rubric"]), _j(a["typical_deliverables"]), _j(a["example_tasks"]), _j(a["keywords"])))
        for s in a["sub_skills"]:
            conn.execute("INSERT INTO area_sub_skills (area_id, id, name, description) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(area_id, id) DO UPDATE SET name = excluded.name, description = excluded.description",
                         (a["id"], s["id"], s["name"], s["description"]))
    # adjacency references areas on both sides, so it goes in after every area exists
    for a in areas_doc["areas"]:
        for adj in a["adjacent_areas"]:
            conn.execute("INSERT OR IGNORE INTO area_adjacency (area_id, adjacent_area_id) VALUES (?, ?)", (a["id"], adj))


def _seed_contributors(conn, doc: dict) -> None:
    for c in doc["contributors"]:
        conn.execute(
            "INSERT INTO contributors (id, name, title, email, timezone, availability, away_reason, "
            "max_concurrent_tasks, available_hours, years_experience, bio) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, title = excluded.title, email = excluded.email, "
            "timezone = excluded.timezone, availability = excluded.availability, away_reason = excluded.away_reason, "
            "max_concurrent_tasks = excluded.max_concurrent_tasks, available_hours = excluded.available_hours, "
            "years_experience = excluded.years_experience, bio = excluded.bio",
            (c["id"], c["name"], c["title"], c["email"], c["timezone"], c["availability"], c.get("away_reason"),
             c["max_concurrent_tasks"], c["available_hours"], c.get("years_experience"), c.get("bio")))
        # expertise is wholly owned by the JSON: replace it (skills first, they reference expertise)
        conn.execute("DELETE FROM contributor_skills WHERE contributor_id = ?", (c["id"],))
        conn.execute("DELETE FROM contributor_expertise WHERE contributor_id = ?", (c["id"],))
        for e in c["expertise"]:
            conn.execute("INSERT INTO contributor_expertise (contributor_id, area_id, level) VALUES (?, ?, ?)",
                         (c["id"], e["area"], e["level"]))
            for s in e.get("sub_skills", []):
                conn.execute("INSERT INTO contributor_skills (contributor_id, area_id, sub_skill_id) VALUES (?, ?, ?)",
                             (c["id"], e["area"], s))


def _seed_tasks(conn, doc: dict, data_dir: Path, now) -> tuple[int, int]:
    ts_now = clock.iso(now)
    created = attached = 0
    for t in doc["tasks"]:
        created_at = clock.ago(t["created_minutes_ago"], now)
        story = t["user_story"]
        cur = conn.execute(
            "INSERT OR IGNORE INTO tasks (id, title, area_id, min_level, priority, status, requester_name, "
            "requester_team, story_as_a, story_i_want, story_so_that, description, estimated_effort_hours, tags, "
            "sim, created_at, due_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t["id"], t["title"], t["area"], t["min_level"], t["priority"], t["status"], t["requester"]["name"],
             t["requester"]["team"], story["as_a"], story["i_want"], story["so_that"], t["description"],
             t["estimated_effort_hours"], _j(t.get("tags", [])), _j(t["sim"]) if t.get("sim") else None,
             clock.iso(created_at), clock.iso(clock.ahead(t["due_in_hours"], now)) if t.get("due_in_hours") else None,
             ts_now))
        if cur.rowcount == 1:
            created += 1
            _event(conn, clock.iso(created_at), "task_created", "seed",
                   {"priority": t["priority"], "area": t["area"], "min_level": t["min_level"], "source": "seed"}, t["id"])
        for a in t["attachments"]:
            f = data_dir / a["path"]
            if not f.is_file():
                raise SeedError(f"{t['id']}: attachment file missing: {a['path']}")
            raw = f.read_bytes()
            cur = conn.execute(
                "INSERT OR IGNORE INTO task_attachments (id, task_id, kind, filename, path, description, size_bytes, sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (a["id"], t["id"], a["kind"], a["filename"], a["path"], a.get("description"), len(raw),
                 hashlib.sha256(raw).hexdigest()))
            attached += cur.rowcount
    return created, attached


def seed(conn: sqlite3.Connection, data_dir: Path = DATA_DIR) -> dict:
    """Load every dataset in one transaction; returns row counts. Raises SeedError and rolls back on bad data."""
    areas, contribs, tasks, cfg = (_load(data_dir, n) for n in
                                   ("expertise_areas.json", "contributors.json", "tasks.json", "engine_config.json"))
    now = clock.now()
    try:
        with transaction(conn):
            new_config = _seed_config(conn, cfg, clock.iso(now))
            _seed_reference(conn, areas)
            _seed_contributors(conn, contribs)
            new_tasks, new_attachments = _seed_tasks(conn, tasks, data_dir, now)
            result = {
                "areas": conn.execute("SELECT COUNT(*) FROM expertise_areas").fetchone()[0],
                "contributors": conn.execute("SELECT COUNT(*) FROM contributors").fetchone()[0],
                "tasks_total": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "tasks_new": new_tasks,
                "attachments_new": new_attachments,
                "config_snapshot_new": new_config,
            }
            _event(conn, clock.iso(now), "seed_loaded", "seed", result)
    except sqlite3.IntegrityError as e:
        raise SeedError(f"seed data violates the schema: {e}") from e
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Load seed datasets into the Cadence database.")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (default: var/cadence.db)")
    ap.add_argument("--reset", action="store_true", help="delete the database first (discards all runtime state)")
    args = ap.parse_args(argv)
    if args.reset:
        reset_files(args.db)
    conn = connect(args.db)
    init_schema(conn)
    try:
        counts = seed(conn)
    except SeedError as e:
        print(f"SEED FAILED: {e}", file=sys.stderr)
        return 1
    print(f"Seeded {args.db}")
    for k, v in counts.items():
        print(f"  {k:22} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
