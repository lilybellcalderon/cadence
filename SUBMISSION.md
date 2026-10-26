Repository: https://github.com/lilybellcalderon/cadence
Walkthrough video: <TODO: Loom link, 10 minutes max>

Agent setup:
Claude Code (Claude Sonnet 5.5) in the Claude desktop app, working inside my workspace, using the same pattern as my Symphony agentic-SDLC template: one agent per stage, a defined hand-off between stages, and me as the human gate between them. The work went in this order, with me deciding at each gate before the next stage started:
1. Seed datasets first (expertise areas, contributors, request repository with attachments), with a validator script (`validate_seed.py`) that acts as an executable spec and checks references, attachments and that each demo scenario matches the data.
2. A design doc (`DESIGN.md`) holding the decisions I made and the state machine, updated after every gate.
3. SQLite schema that enforces the key invariants itself, plus an idempotent seed loader.
4. The RoutingAgent, built test-first against a simulated clock so 5-20s latency, hangs, restarts and timeouts are repeatable.
5. The TrackingAgent, which I added after deciding that missing estimate and status data was the real bottleneck.
6. A report generator, then the operator API and single-page UI, checked by driving the real app in the in-app browser (submit, stuck request, intervention, answering a question, creating a task, hand-operated contributor, restart).
The runtime design mirrors the way I built it: a RoutingAgent and a TrackingAgent, each owning one stage, with the operator as the human-in-the-loop gate. Stack matches Symphony for speed: Python 3.14 standard library, SQLite, unittest (FastAPI was dropped so nothing has to be installed). No subagents or parallel sessions were used. 118 automated tests; review, tests and hands-on use of the running app caught real defects (listed in DESIGN.md section 10).

Session map:
- Initial planning or decomposition: <session name/ID from the app's session list>, first two turns. Read my Symphony template, then produced the three datasets and `DESIGN.md` (entities, eligibility rule, scenario coverage, Symphony mapping).
- Most important correction or change of direction: same session, the turn where I said missing work estimates and status updates are the main bottleneck and must drive the design. That reshaped the design: estimates captured at acceptance, a second agent (TrackingAgent) to chase them, estimate precedence and an "estimate debt" rule that feeds back into routing. Earlier in the session my decision that capacity is a soft constraint (log the conflict, queue with a warning) replaced the original hard-exclusion rule.
- Final testing and verification: same session, the final two turns (RoutingAgent, then TrackingAgent + report + UI). 118 automated tests on a simulated clock (routine, stuck, intervention with late return, decline, failure, no match, capacity conflict, contention, restart recovery, estimate chase ladder, variance, stale, due-date risk, estimate debt, report, HTTP API), `validate_seed.py`, and a live run of the real app in the browser, including a server restart with work in flight. Review, tests and hands-on use found real defects (estimate at accept bypassing the variance check and an import cycle, both caught on self-review; an unplaceable seed task, found by a test; infeasible seed dates; lost clicks under UI polling, found by using the app); they are listed in `DESIGN.md` section 10.

Intentional omissions:
- Authentication and multiple operators: the console binds to localhost and the operator name is just recorded on each action. Out of scope for the exercise.
- Real notifications: critical and high alerts go to a simulated outbox table (visible in the UI) instead of email or chat, so the demo needs no external service. A real notifier can replace it.
- Automated UI tests: the UI was verified by hand in a browser and through the API tests that back every action it calls. Browser test automation would add a dependency for little extra assurance here.
- Production concerns: a single worker process, no schema migrations (a version check refuses a mismatched database), no deployment packaging.
- A sophisticated matching algorithm: routing ranks eligible contributors by fit, then utilisation, then lowest sufficient level. The exercise says this is not the focus.
- Split tasks do not copy the parent's attachments; the child records its parent id and the operator can open the parent.
