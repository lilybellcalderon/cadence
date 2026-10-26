"""The engine policy in force, read from the snapshot the seed loader stored (so decisions match what was loaded)."""
import json
import sqlite3
from datetime import timedelta

_cache: dict[str, dict] = {}


def load(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key = 'config_sha256'").fetchone()
    if row is None:
        raise RuntimeError("no engine config in the database; run `python -m app.seed` first")
    sha = row[0]
    if sha not in _cache:
        _cache[sha] = json.loads(conn.execute("SELECT config FROM config_snapshots WHERE sha256 = ?", (sha,)).fetchone()[0])
    return _cache[sha]


def sim_minutes(cfg: dict, minutes: float) -> timedelta:
    """Simulated minutes -> real time. Estimate SLAs and check-in cadences are compressed in the demo
    (factor 60: one simulated hour per real minute). Routing latency and operator timers are not."""
    factor = cfg["contributor_engagement"]["status_check"]["demo_compression_factor"]
    return timedelta(seconds=minutes * 60 / factor)
