# T-1003 Requirements: 90-day retention by signup cohort

## Context
Retention for April and May 2026 signups appears lower than prior months. An onboarding change shipped on 14 April. Growth needs to know whether the change caused the drop.

## Definitions (confirm before starting)
- **Cohort:** users grouped by calendar month of first successful signup.
- **Retained at day N:** user performed at least one qualifying action (login + one core action) on day N +/- 3.
- Exclude internal and test accounts.

## Required analysis
1. Retention curves (day 7, 30, 60, 90) for Jan-Jun cohorts, with cohort sizes.
2. Verify the retention metric definition and event tracking did not change in the period (check the event-schema change log).
3. Compare April-May cohorts with and without exposure to the new onboarding flow (flag in `onboarding_variant`).
4. Rank plausible drivers (onboarding change, channel mix shift, seasonality, tracking change) with evidence and a confidence level for each.

## Deliverable
Notebook or query pack with results, and a one-page findings memo including caveats.
