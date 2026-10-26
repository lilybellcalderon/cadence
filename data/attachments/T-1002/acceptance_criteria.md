# T-1002 Acceptance Criteria

- [ ] AC1: The audit covers 100% of the month's intake records, and the summary states the exact record count audited.
- [ ] AC2: Every record failing at least one rule appears in the exception list exactly once per failed rule, with the rule identified.
- [ ] AC3: Duplicate `patient_ref` values are flagged and none are removed.
- [ ] AC4: The exception list masks `date_of_birth` to year and contains no free-text clinical notes.
- [ ] AC5: The summary identifies whether failures cluster by site, date range or form version, with supporting counts.
- [ ] AC6: The report is delivered at least 2 hours before the submission deadline so the director can review it.
