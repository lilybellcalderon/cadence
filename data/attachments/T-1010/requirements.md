# T-1010 Requirements: Local diagnosis list to ICD-10-CM

## Scope
240 locally-defined diagnosis labels used in the outpatient system. Map each to the closest ICD-10-CM code.

## Method
1. For each label, propose one primary ICD-10-CM code and, where needed, alternates.
2. Rate confidence: **High** (exact), **Medium** (close, minor loss of specificity), **Low** (ambiguous or needs clinical review).
3. Document any label that has no valid code or maps to more than one.

## Deliverable
Crosswalk spreadsheet: `local_label`, `primary_code`, `description`, `confidence`, `notes`, `alternates`. Plus a summary count by confidence level and a list of all Low-confidence items for clinical review.

## Handling
No patient-level data is required for this task.
