# Cadence — Design

Scope of this step: the three seed datasets and how they relate. The workflow state machine, routing-engine simulation, API and UI are the next steps and are only sketched here where they constrain the data.

## 1. The three datasets

```
expertise_areas  1 ──< contributors.expertise[]  >── 1  (area, level 1-4)
        1
        └──< tasks.area  (+ tasks.min_level)
```

| Dataset | File | Rows | Role |
|---|---|---|---|
| Expertise areas | `data/expertise_areas.json` | 6 | Shared vocabulary. Rich descriptions, scope boundaries, sub-skills, per-level rubric, keywords. Both tasks and contributors point at it. |
| Contributors | `data/contributors.json` | 11 | People, their expertise (area + level 1-4 + sub-skills), declared availability, `max_concurrent_tasks` and `available_hours`. |
| Requests (tasks) | `data/tasks.json` + `data/attachments/` | 10 tasks, 22 files | The request repository: area, min level, priority, status, estimated effort, user story, description, attachments (requirements, acceptance criteria, sample data). |

Areas: finance, healthcare, data_analysis, operations, legal_compliance, technical_writing.
Levels: 1 Foundational, 2 Practitioner, 3 Advanced, 4 Expert (each area has its own rubric for what a level means).

### Design choices worth noting
- **Level on the task, not just the area.** `tasks.min_level` lets a request say "needs an Expert" and is what makes "no match" a real outcome rather than a random one.
- **Workload is derived, not stored.** `contributors.json` holds only the limits (`max_concurrent_tasks`, `available_hours`); `tasks.json` holds `estimated_effort_hours`. Current load is computed from runtime assignments so it can never disagree with them.
- **Availability is declared state** (`available | away | offline`). C-11 is `away`, so the router must skip someone who otherwise qualifies (visible on T-1006).
- **Attachments are real files** referenced by path and `kind` (`requirements`, `acceptance_criteria`, `sample_data`, `reference`). Every task has at least requirements + acceptance criteria; the validator enforces that.
- **Simulation is quarantined in `tasks[].sim`.** It scripts engine and contributor behaviour for demos and tests. Tasks submitted by the operator omit it and get the default engine (5-20s latency, 20% never return).
- **Time since created** comes from `created_minutes_ago`, converted to an absolute timestamp at load, so the column is meaningful on every fresh start.
- All seed tasks start as `draft`. The workflow states are deliberately left to the next step.
- **No personal data.** All people, patients and records are fictional (`example.test` emails; the intake CSV is synthetic).

## 2. Eligibility and capacity (what the engine is handed)

**Eligibility (hard rule).** A contributor is eligible when both hold:
1. has `task.area` at level >= `task.min_level`
2. `availability == "available"`

The engine receives the task plus the eligible list and either picks one or reports no match. `python validate_seed.py` prints the eligible set per task.

**Capacity (soft rule, decision 3).** Workload is derived from the contributor's active assignments: the count, and the sum of each assignment's best available hours (check-in remaining, then the contributor's confirmed estimate, then the requester's `estimated_effort_hours` x 1.25 labelled unconfirmed; see section 9), compared with `max_concurrent_tasks` and `available_hours` (hours free for assigned work over a 10-working-day planning window). Holding the proposed task must stay within both limits.
- Prefer eligible contributors who fit. Among those, the engine's pick stands.
- If every eligible contributor would exceed a limit, the pick is **not** blocked from the record: a `capacity_conflict` event is logged (contributor, current load, task estimate, which limit, by how much) and the task goes to `queued` with a resource-usage warning for the operator, who can wait, reassign, raise the contributor's hours, or split the task.
- Example in the seed data: C-03 is the only eligible contributor for T-1010 (30h). Alone it fits; together with T-1002 (12h) it is `2 tasks > max 1` and `42h > 40h available`.

## 3. Scenario coverage (use cases -> seed data)

| Use case | Task | What makes it happen |
|---|---|---|
| Routine assignment | T-1001, T-1006 | `routing: normal`, contributor accepts |
| Unresolved routing, high priority | T-1002 (critical) | `routing: hang`: request never returns |
| Intervene while pending, request returns later | T-1003 | `routing: late_return` after 120s |
| Declined assignment -> recovery | T-1004 | engine picks C-04, who declines |
| Failed assignment -> recovery | T-1007 | engine picks C-02, assignment fails before work starts |
| No match found | T-1005 | `min_level: 4` in legal_compliance, no one qualifies (data-driven, no scripting) |
| Capacity conflict -> logged and queued | T-1010 (with T-1002) | only C-03 qualifies; together the tasks exceed C-03's 1-task limit and 40h |
| Default engine behaviour (20% hang) | T-1008, T-1009 | `routing: random` |

## 4. How this maps onto the Symphony template

| Symphony concept | This system |
|---|---|
| Tracker (ADO / Jira) | The request repository (`tasks`), stored in SQLite |
| `WORKFLOW.md` front matter + trigger states | Engine config: latency range, hang rate, timeouts, concurrency |
| Poll loop claims items in trigger states | Each agent claims work in its own trigger states (below) |
| Agent routed by type + state | **RoutingAgent** (`submitted`, `queued`, `unmatched`, `declined`, `failed`) and **TrackingAgent** (`accepted`, `in_progress`, plus a periodic sweep). Same pattern as Symphony's PlanningAgent / CodingAgent split: each agent owns one stage and a defined hand-off. |
| HITL gate | The operator: intervene, reassign, recover, and answer agent questions |
| `symphony_runs` (agent, start, end, success, error) | `routing_requests` + an append-only `events` table (the history the operator inspects) |
| Per-run log file | Event payloads (request id, eligible set, result, who acted) |

## 5. Runtime entities (not seeded, defined next)

- `routing_requests`: one row per attempt (request id, task, started, status `pending | returned | superseded | abandoned`, result). A task can have several over its life.
- `assignments`: task, contributor, status, offered/decided timestamps, reason on decline/fail.
- `events`: append-only history of everything above plus operator actions.
- `alerts`: severity, trigger, task, acknowledged by/at, re-alert count; `outbox` holds the simulated external deliveries.
- `operator_questions`: type, context snapshot, options, answer, answered by/at, safe default applied or not.
- `status_checks`: one row per contributor check-in (sent, due, reply, progress %, hours remaining, status).
- `estimate_revisions`: append-only estimate history per assignment (hours total/remaining, confidence, basis, who, when, revision). Never overwritten, so the operator can see how an estimate moved.
- `estimate_requests`: each request/reminder the TrackingAgent sent for an assignment (sent, due, reminder count, answered at).
- `assignments.plan_health`: derived (`complete | awaiting_estimate | overdue_estimate | stale | at_risk`), recomputed by the TrackingAgent every sweep.

Key constraint these impose on the data model: a late response must be matched to its **request id**, not just its task, so that after an operator intervenes the stale result is recorded in history but never silently overrides the operator's decision.

## 6. Decisions (resolved)

| # | Decision | Outcome |
|---|---|---|
| 1 | Stuck requests | After a threshold (default 30s, configurable; normal max is 20s) a pending request gets a **"possibly stuck" warning flag**. It is never failed automatically, because the operator can't know whether it is slow or lost. A **daily report** (end of day) lists every record that was flagged, intervened on, queued, declined or failed, generated from the event log. |
| 2 | Late return after intervention | The stale result is **logged and ignored**. It is matched by routing-request id, recorded in history as `superseded`, and never overrides the operator's decision. |
| 3 | Capacity | **Soft constraint** using current assignments and work estimates (section 2). Conflicts are logged as events and the task is **queued with a resource-usage warning**. |
| 4 | Stack | Same as Symphony for fast turnaround: **Python + SQLite** (FastAPI service, background worker for routing, single-page UI). |
| 5 | Workflow states | **Confirmed**, with `queued` linked to the `priority_contention` question and the check-in loop on `accepted` / `in_progress` (section 7). |
| 6 | Outbox | **Confirmed:** simulated outbox for critical/high alerts now; a real Teams/email notifier can replace it later. |
| 7 | Estimate data is a first-class concern | **Added:** a second agent (TrackingAgent) owns estimate and status quality; estimates are captured at acceptance and chased to completion (section 9). |

## 7. Workflow states (confirmed)

Stuck is a **derived flag** (time pending > threshold), not a state, so a late return needs no special transition.

```
draft -> submitted -> routing -> offered -> accepted -> in_progress -> completed
                        |          |  \
                        |          |   +-> declined --\
                        |          +-----> failed ----+--> (operator recovers) -> submitted | offered(other) | cancelled
                        +-> unmatched ----------------/
                        +-> queued (capacity conflict, warning) -> offered (when capacity frees or operator releases)
any non-terminal state --operator intervene--> offered(chosen contributor) | cancelled
```

- `routing` has the request id; operator intervention marks it `superseded`.
- `accepted` ends routing for that task (one active assignee at most). It also **hands the assignment to the TrackingAgent**: `accepted -> in_progress` requires a contributor-confirmed estimate (the estimate gate, section 9).
- `queued` has two entry reasons: capacity conflict, and estimate debt. When two queued tasks want the same contributor, a `priority_contention` question is raised instead of silently picking one.
- `plan_health` is a flag on the assignment, not a state, so a stale estimate never changes where the task is on the board.
- Restart safety: on startup, requests still `pending` are kept as pending (their simulated timer is re-armed from the original start time, or flagged stuck if already past the threshold), so a refresh or restart neither loses nor double-submits work.

## 8. Operator alerts, questions and contributor check-ins

All of this is policy-as-data in `data/engine_config.json`; `validate_seed.py` checks it is consistent.

### 8.1 Alerts by priority
The log stays the system of record; an alert is a second, actionable signal raised from the same events.

`severity = clamp(priority_rank + trigger.adjust, floor, critical)`, where the floor is `low` for any trigger that needs a decision, so an action item is never log-only.

| Task priority | stuck request | no match | capacity conflict | assignment failed | blocked | late return (ignored) |
|---|---|---|---|---|---|---|
| critical | critical | critical | high | critical | critical | info |
| high | high | high | normal | critical | high | info |
| medium | normal | normal | low | high | normal | info |
| low | low | low | low | normal | low | info |

Delivery and escalation by severity:

| Severity | Channels | Re-alert if unacknowledged |
|---|---|---|
| critical | pinned in-app banner + outbox message | every 5 min, up to 3 times, last one raised a level |
| high | pinned in-app banner + outbox message | every 15 min, up to 2 times |
| normal | in-app inbox | after 60 min, once |
| low | hourly in-app digest | none |
| info | log and daily report only | none |

The **outbox** is a simulated email/chat channel (a table shown in the UI), so the demo needs no real service and a real notifier can replace it later. Stuck thresholds now also scale with priority (critical 25s, high 30s, medium 45s, low 60s), still flag-only.

### 8.2 The routing agent asks, the operator decides
When a situation needs judgment, the agent raises an **operator question** attached to an alert. Each question states what the agent knows and offers explicit options. Nine types are defined: `stuck_request`, `no_match`, `capacity_conflict`, `priority_contention` (two queued tasks competing for one contributor), `recovery` (decline or failure), `estimate_missing`, `estimate_variance`, `contributor_silent`, `at_risk`. The first five come from the RoutingAgent, the last four from the TrackingAgent (every trigger in the config names its agent).

Rules:
- No question is ever auto-answered with a state-changing action. On timeout (5 min critical ... 4 h low) the agent applies only a non-destructive `safe_default` (e.g. keep waiting, send reminder), re-alerts, and leaves the question open. The validator rejects any `safe_default` that reassigns, cancels or force-assigns.
- Questions and answers are events (who, when, option, note) and survive a restart.
- The answer drives the next state transition (e.g. `no_match` -> `lower_min_level_with_expert_review` re-submits with the new level and records why).

### 8.3 Engaging contributors on assigned work (owned by the TrackingAgent)
For tasks in `accepted` or `in_progress` the TrackingAgent sends status check-ins on a priority-based cadence (critical 4h, high 8h, medium 24h, low 48h of simulated time; a demo compression factor of 60 makes them visible in minutes. Routing latency is not compressed).

- The contributor replies with progress %, hours remaining, status (`on_track | at_risk | blocked`) and a note.
- **The reply feeds planning:** `hours_remaining` replaces the original estimate in that contributor's load, so capacity and conflict checks stay honest.
- `at_risk` / `blocked` raise an alert and an `at_risk` question. Two missed check-ins raise `status_no_response` and a `contributor_silent` question.
- A simulated contributor inbox lets a demo user answer; seed tasks can script the reply with `tasks[].sim.status_check` (T-1001 on track, T-1003 at risk, T-1006 silent, T-1008 blocked).

## 9. Estimate and status data quality: the TrackingAgent (design priority)

**Problem.** The current process has no reliable estimates or status updates on assigned work, yet queueing and assignment decisions depend on exactly that data. A routing decision is only as good as the load figure behind it.

**Principle.** Capture the estimate at the moment of assignment, make its quality visible, and have an agent chase gaps instead of relying on people to remember. RoutingAgent consumes the data; TrackingAgent owns it.

### 9.1 Where an estimate comes from
1. **Estimate revisions** (append-only): the contributor's own estimate (hours total/remaining, confidence, basis) and every later check-in, each stored as a revision with its source. **The latest revision wins**, so the most recent honest number is always what planning sees.
2. `requester_provisional` x 1.25: the requester's guess from submission, used only while no revision exists, **buffered and labelled "unconfirmed"**.

Capacity and conflict checks use the view `v_assignment_load`, which applies exactly this rule. Any decision that rested on source 3 says so in the conflict warning and operator question, and counts in the daily report ("capacity decisions made on unconfirmed estimates").

### 9.2 Capture at accept, chase if missing
- **Preferred path:** the accept action in the contributor view requires hours total and confidence, so the estimate arrives with acceptance and the assignment goes straight to `in_progress` eligibility.
- **Fallback:** operator manual assignments and simulated assignments start as `awaiting_estimate`. The TrackingAgent sends an estimate request at once and follows this ladder (simulated minutes, compressed in the demo):

| Priority | Estimate due | Reminder every | Operator alert + question at |
|---|---|---|---|
| critical | 15 | 10 | 35 (after 2 reminders) |
| high | 30 | 20 | 70 |
| medium | 120 | 60 | 240 |
| low | 480 | 240 | 960 |

The operator alert is `estimate_overdue` (severity scaled by task priority as in 8.1) with an `estimate_missing` question. Options include entering the estimate on the contributor's behalf or provisionally using the requester's number. The planning basis in use is stated in the question.

### 9.3 Keeping data fresh and sane
Each sweep the TrackingAgent re-checks every active assignment:
- **Stale:** no estimate revision or status within 1.5x the check-in cadence (critical 6h ... low 72h) -> `estimate_stale`.
- **Variance:** contributor total differs from the requester's by more than 50%, or `hours_remaining` grows more than 25% between revisions -> `estimate_variance` and an `estimate_variance` question (the task may be mis-scoped).
- **Due-date risk:** `hours_remaining` exceeds working hours left before due -> `due_date_at_risk`.
- **Silent:** two missed check-ins -> `status_no_response`.

### 9.4 Estimate debt: the data feeds back into routing
If a contributor has more than one assignment in `overdue_estimate`, the RoutingAgent treats that contributor as **capacity-unknown**: it prefers other eligible contributors, and if they are the only option it queues the task with an "estimate debt" warning instead of assigning. This makes missing estimates cost something real and gives the operator a concrete reason to follow up.

### 9.5 Measuring it
The daily report leads with data-quality metrics: estimate coverage within SLA, median time to estimate by priority, assignments currently overdue or stale, contributors with estimate debt, and decisions made on unconfirmed estimates. The target is the first one trending toward 100%.

### 9.6 Seed scenarios for this agent
| Task | Scripted behaviour | What it shows |
|---|---|---|
| T-1001 | estimate provided with accept (14h vs 16h) | Happy path, plan complete immediately |
| T-1003 | estimate arrives after one reminder (26h) | Chase ladder works; high-priority timing |
| T-1006 | never provides an estimate and never answers check-ins | `overdue_estimate` -> `estimate_overdue` alert + question, then `status_no_response` |
| T-1008 | estimate 28h vs 16h requester, low confidence | `estimate_variance` alert (+75%) |
| T-1010 + T-1002 on C-03 | 42h confirmed vs 52.5h if unconfirmed | Same pair is a conflict either way, but the question shows which basis was used |

## 10. Build status

Everything in this document is built and tested (118 tests, simulated clock; plus manual verification in the browser). Stack as decided: Python and SQLite, standard library only. FastAPI was dropped in favour of `http.server` so nothing has to be installed; the API is plain JSON over HTTP and the UI is one static page.

| Area | Where | Notes |
|---|---|---|
| Datasets and loader | `data/`, `app/seed.py` | Idempotent; re-running never touches runtime state; policy snapshot hashed in `config_snapshots` |
| Schema | `app/schema.sql` | 22 tables, 3 views; one active assignee per task, one pending request per task, append-only history, one unresolved alert per dedupe key |
| RoutingAgent | `app/routing.py` | Claims, engine call, stuck flagging, supersede + late-return ignore, capacity queue, contention, recovery, no-match handling; every operator answer drives a real transition |
| TrackingAgent | `app/tracking.py` | Capture at accept, chase ladder, plan health, check-ins, variance/stale/due-date alerts, estimate debt, reconcile on clear |
| Simulation | `app/simulator.py`, `app/contributor_sim.py` | Engine (latency, 20% hang, scripted modes) and contributors (accept/decline/fail, estimate behaviour, check-in replies); any contributor can be switched to manual |
| Alerts and questions | `app/alerts.py` | Severity by priority, simulated outbox, re-alerts with escalation, nine question types with declared safe defaults |
| Operator actions | `app/actions.py` | Submit, assign, cancel, acknowledge, answer; atomic, validated, attributed |
| Reports | `app/reports.py` | Estimate data-quality metrics, records needing attention, interventions, questions; written automatically when the UTC date rolls over |
| API and UI | `app/server.py`, `app/static/index.html` | Board, alerts and questions, contributors (with a hand-operated contributor workspace), daily report, new task, per-task drawer with history |
| Worker | `app/worker.py` | One loop for both agents; runs inside the server or standalone |

### Defects found in review, testing and hands-on use, and how they were resolved
- **Estimates given at acceptance bypassed the variance check** (T-1008's +75% would never have fired). Accepting and estimating are now one path (`tracking.accept_with_estimate`).
- **An import cycle** (routing -> simulator -> tracking -> routing) was removed by moving the contributor simulator to its own module.
- **A 50h unconfirmed requirement could never be placed** (T-1005 at 40h x 1.25 exceeded every contributor's hours); the seed was corrected and the behaviour kept, because it is a useful signal.
- **The seed contradicted its own due dates** (a 12h critical task due in 8h); due dates were made feasible, and a due-date-risk check now catches that class of problem for real tasks.
- **Lost clicks in the UI**: the board text changed every poll, so the DOM was rebuilt under the cursor. Counters now tick client-side and rendering pauses briefly after any mouse press. The urgent banner is capped so the page does not jump.
- **A safe-default lookup assumed a joined column**; it now reads the task priority itself.

### Known limits (deliberate)
- Single worker process; SQLite WAL and `BEGIN IMMEDIATE` make a second process safe but it is untested.
- No authentication: the console binds to 127.0.0.1 and the operator name is only recorded on actions.
- Real notifications are simulated by the outbox table; attachments are text files and are shown as text.
- Due dates are real time and are not compressed by the demo factor; SLA and check-in cadences are.
- The UI has no automated tests; it was verified by hand in a browser and through the API tests that back it.

## Files

```
Cadence/
  README.md, DEMO.md, DESIGN.md, SUBMISSION.md
  run_server.py   start the console from any folder
  validate_seed.py
  app/        schema.sql, db.py, seed.py, clock.py, config.py, events.py, tasks.py (state machine), alerts.py,
              capacity.py, simulator.py, contributor_sim.py, assignments.py, routing.py (RoutingAgent), tracking.py (TrackingAgent),
              actions.py (operator), reports.py, server.py, static/index.html, worker.py
  tests/      test_schema_and_seed.py, test_routing.py, test_tracking.py, test_reports.py, test_api.py
  var/        cadence.db (runtime, recreated by --reset)
  data/
    expertise_areas.json
    contributors.json
    tasks.json
    engine_config.json   (both agents: stuck thresholds, estimate SLAs and chase ladder, alert policy, operator questions, check-in cadence)
    attachments/T-1001 ... T-1010/  (requirements.md, acceptance_criteria.md, + 2 sample CSVs)
```
