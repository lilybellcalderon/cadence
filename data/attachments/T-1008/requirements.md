# T-1008 Requirements: Post-incident review, carrier outage

## Incident summary
A carrier API outage on the 14th, combined with a missing fallback rule, held 1,400 orders for up to 36 hours.

## Inputs
- Monitoring alerts and the incident channel transcript.
- Carrier status page history and the carrier's own incident report.
- Order-hold report (orders, hold start and release times).
- Interviews with the on-call engineer, the shift supervisor and the carrier account manager.

## Required content
1. Verified timeline (detection, escalation, mitigation, recovery) with timestamps and sources.
2. Impact: orders delayed, delay distribution, customer contacts, cost estimate.
3. Root cause and contributing factors (blameless; process and system causes, not individuals).
4. What went well / what went badly.
5. Corrective actions, each with an owner, a due date and a way to verify completion.

## Deliverable
Post-incident review document following the company template, plus a short customer-facing summary.
