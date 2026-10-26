"""
Background worker: advances the whole system one step per tick. Run:  python -m app.worker [--db PATH] [--interval 1] [--once]

All state lives in SQLite, so the process can be killed and restarted at any point. On start it records what it found
(pending routing requests, requests already due) and carries on; nothing is re-rolled and nothing is submitted twice.
"""
import argparse
import random
import sys
import threading

from . import alerts, clock, config, reports
from .db import DEFAULT_DB, connect, init_schema, transaction
from .contributor_sim import ContributorSimulator
from .routing import RoutingAgent
from .tracking import TRACKING_QUESTIONS, TrackingAgent


def tick_all(conn, routing: RoutingAgent, contributors: ContributorSimulator, tracking: TrackingAgent, now=None) -> dict:
    now = now or clock.now()
    cfg = config.load(conn)
    summary = {"contributors_responded": contributors.tick(cfg, now), "routing": routing.tick(now), "tracking": tracking.tick(now)}
    with transaction(conn):
        summary["re_alerts"] = alerts.process_re_alerts(conn, cfg, now)
    asked = 0
    for q in alerts.open_questions_due(conn, now):
        (routing if q["type"] in alerts.ROUTING_QUESTIONS else tracking).apply_safe_default(q, now)
        asked += 1
    summary["safe_defaults_applied"] = asked
    with transaction(conn):
        summary["daily_report_written"] = int(reports.ensure_previous_day(conn, cfg, now))
    return summary


def run_loop(db_path, stop, interval=1.0, seed=None, once=False, quiet=False) -> None:
    """Advance the system once per interval until `stop` is set. Used by the CLI and by the server's background thread."""
    conn = connect(db_path)
    init_schema(conn)
    routing = RoutingAgent(conn, random.Random(seed))
    tracking = TrackingAgent(conn, random.Random(seed))
    contributors = ContributorSimulator(conn)
    if not quiet:
        print("agents started:", routing.recover(), tracking.recover(), flush=True)
    else:
        routing.recover(), tracking.recover()
    try:
        while not stop.is_set():
            try:
                summary = tick_all(conn, routing, contributors, tracking)
                busy = {k: v for k, v in {**summary.pop("routing"), **summary.pop("tracking"), **summary}.items() if v}
                if busy and not quiet:
                    print(clock.iso(), busy, flush=True)
            except Exception as e:      # a failed tick must not stop the agents; the next tick starts from stored state
                print(f"tick failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            if once:
                break
            stop.wait(interval)
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the Cadence background worker.")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between ticks")
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for repeatable engine latency/hangs")
    args = ap.parse_args(argv)
    stop = threading.Event()
    try:
        run_loop(args.db, stop, args.interval, args.seed, once=args.once)
    except KeyboardInterrupt:
        print("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
