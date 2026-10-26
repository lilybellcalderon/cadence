# Cadence

*Routing you can trust to keep time.*

Assigns tasks to contributors by area of expertise. An automatic routing engine (simulated: 5-20s latency, 20% of requests never return) tries to match; an operator monitors, intervenes and recovers. A second agent makes sure the estimate and status data that routing depends on actually exists.

Python 3.12+ standard library only (SQLite, `http.server`, `unittest`). Nothing to install.

## Run it

```bash
python -m app.seed --reset         # fresh demo data in var/cadence.db (6 areas, 11 contributors, 10 tasks, 22 attachments)
python run_server.py --seed 1      # operator console at http://127.0.0.1:8000 (agents run in the same process)
python -m unittest discover -s tests   # 118 tests, simulated clock
python validate_seed.py            # dataset consistency report
```

`--seed N` makes the engine's latency and hangs repeatable. Stop and restart the server at any point: all state is in SQLite.

## What is where

| | |
|---|---|
| `data/` | Seed datasets: `expertise_areas.json`, `contributors.json`, `tasks.json` (+ `attachments/`), `engine_config.json` (all policy: thresholds, SLAs, alert and question rules) |
| `app/routing.py` | **RoutingAgent**: claims submitted tasks, calls the simulated engine, flags stuck requests, queues on capacity, asks the operator, recovers from decline/failure |
| `app/tracking.py` | **TrackingAgent**: estimate requests and chase ladder, plan health, check-ins, variance/stale/due-date checks, estimate debt |
| `app/actions.py` | The operator's gate: submit, assign (intervene), cancel, acknowledge, answer a question |
| `app/alerts.py` | Alerts by priority, simulated outbox, operator questions with safe defaults |
| `app/schema.sql` | SQLite schema; the database itself enforces one active assignee per task, one pending request per task, append-only history |
| `app/server.py`, `app/static/index.html` | JSON API and the single-page console |
| `app/reports.py` | End-of-day report |
| `DESIGN.md` | Decisions, state machine, how it maps to the Symphony template |
| `DEMO.md` | Walkthrough script covering the four use cases |

## The decisions that matter (details in DESIGN.md)

1. **A pending request is never failed automatically.** The operator cannot know slow from lost, so after a priority-based threshold it is *flagged* (alert + question) and the operator decides.
2. **Intervening supersedes the pending request.** If the engine answers later, that result is recorded and ignored; it never overrides the operator.
3. **Capacity is soft.** A conflict is logged and the task is queued with a warning that shows exactly what the load is based on; the operator can wait, raise limits, assign anyway, or split.
4. **Estimates are captured at accept and chased if missing.** Work cannot start without a confirmed estimate. A contributor with several overdue estimates is treated as capacity-unknown, so missing data has a real cost.
5. **The agent asks, the operator decides.** Unanswered questions apply only a declared safe default, never a reassignment or cancellation.
6. **Restart-safe.** Deadlines and simulated outcomes are stored, so a restart neither re-rolls the 20% hang nor double-submits work.
