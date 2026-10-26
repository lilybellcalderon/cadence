# T-1002 Requirements: Patient intake form audit

## Context
A system release on the 3rd may have disabled enforcement of mandatory fields on the outpatient intake form. A regulatory data submission is due imminently. All data in this exercise is synthetic; treat it with the same care as real PHI.

## Scope
All intake records created last month (about 3,100). Sample attached: `intake_sample.csv`.

## Mandatory fields
`patient_ref`, `date_of_birth`, `sex`, `arrival_date`, `referral_source`, `primary_complaint_code`, `consent_recorded`.

## Plausibility rules
- `date_of_birth` is not in the future and implies an age under 120 on `arrival_date`.
- `arrival_date` falls within the audit month.
- `primary_complaint_code` is a valid code from the supplied code list.
- `consent_recorded` must be `Y` for records to be submitted.
- `patient_ref` is unique across the month; duplicates are flagged, not deleted.

## Deliverable
1. Summary: record count, count failing each rule, percent clean.
2. Record-level exception list: `patient_ref`, rule failed, field value (masked as needed), suggested action.
3. Short note on whether the pattern points to a systemic cause (e.g. one site, one date range, one form version).

## Handling rules
Do not copy full records into chat or email. Work only in the approved workspace. Mask `date_of_birth` to year in the exception list.
