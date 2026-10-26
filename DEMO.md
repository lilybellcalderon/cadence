# Walkthrough script (about 10 minutes)

Setup: `python -m app.seed --reset`, then `python run_server.py --seed 1`, open http://127.0.0.1:8000. Everything below happens on the wall clock; the seeded tasks script each use case, so the order is repeatable.

## 0. Orientation (1 min) - Board
All 10 tasks are drafts. Columns: **age** (time since created), **state**, **assignee**, **plan** (is the estimate confirmed?), **estimate requested -> confirmed**, **due**. Open any task: user story, attachments (requirements, acceptance criteria, sample data), history.

## 1. Routine assignment (1 min)
Click **Submit all drafts**. Within ~20s T-1001 is routed, offered, accepted *with the contributor's estimate* (14h vs the requester's 16h) and goes **in progress**. Open it -> History shows each step and who did it.

## 2. Unresolved routing (2 min)
T-1002 (critical) never gets an answer. At 25s a **critical banner** appears and an entry lands in the simulated **outbox**. Open **Alerts & Questions**: the agent asks what to do and shows the options; the starred one is the only thing it will do on its own (keep waiting). Point out: it is *not* failed and not reassigned, because slow and lost look identical.

## 3. Human intervention while pending, then the late result (2 min)
T-1003 is high priority and the engine answers only after 120s. Once its banner appears (~30s), open the task -> **Intervene** -> assign someone. The pending request becomes *superseded*, the banner clears. At ~120s open **History**: "answered late ... **Ignored**". The operator's decision stood.

## 4. Recovery (2 min)
- **T-1004** declined by Tomasz and **T-1007** failed for Marcus: each raised a recovery question. Answer T-1004 *reroute excluding contributor* (note the history of the first attempt is kept) and T-1007 *offer to next eligible*.
- **T-1005** found **no match** (needs level 4, nobody has it). Answer *lower min level with expert review* -> level 3: it is tagged `expert_review_required`, the previous level is logged, and it is assigned.

## 5. Capacity conflict and queueing (1 min)
Restart the demo (`--reset`). Before submitting, open T-1002 -> Intervene -> assign **Dr. Elena Vasquez** (the only level-4 healthcare contributor, one task at a time). Then submit **T-1010**: it can only go to her, so it is **queued** with a warning: *2 tasks > max 1; 49.5h > 40h available*, and the question states the load basis ("requester estimate x1.25, unconfirmed"). Answer *raise contributor hours* (60h, 2 tasks) and it is offered.

## 6. Estimate quality (1 min)
- **T-1008**: contributor says 28h, requester said 16h: **estimate variance** alert (+75%).
- **T-1003** (assigned in step 3) is accepted without an estimate: watch **plan** go *awaiting -> overdue* and a reminder, then the estimate arrives (~35s).
- **T-1006** never gives one: it stays **accepted**, cannot start, and the ladder ends in an alert (low priority, ~16 min; to see it live, set `demo_compression_factor` to 240 in `data/engine_config.json` and re-run `python -m app.seed`).
- **Contributors** tab: tick *I respond by hand* for someone, assign them a task, press *Work as ...*, and try **Accept** with no hours: it is refused. That is capture-at-accept.

## 7. Daily report (30s)
**Daily report** -> Generate: estimate coverage within SLA, every record that needed attention (with its severity and whether it is resolved), operator interventions, late results ignored, questions answered or still open.

## 8. Restart (30s)
Stop the server (Ctrl+C), start it again: the board is unchanged, pending requests continue from their stored deadlines, nothing is re-submitted.

Also worth a mention: **New task** creates a task with requirements and acceptance-criteria attachments and an optional engine control (normal / never returns / returns after 90s) for demonstrating behaviour on demand.
