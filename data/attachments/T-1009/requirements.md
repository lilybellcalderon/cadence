# T-1009 Requirements: Definition of "active customer"

## Current state
- **Sales:** any customer who logged in during the last 90 days.
- **Finance:** any customer with a paid invoice in the last 12 months.
- **Product:** any customer with at least one order in the last 6 months.

## Required work
1. Quantify the overlap and differences between the three definitions (Venn-style counts for the last full quarter).
2. Propose one definition with: numerator, denominator, time window, grain (account vs. user), and exclusions (trials, internal, churned-then-reactivated).
3. Document the edge cases and how each is treated.
4. Describe how existing reports would change under the proposed definition.

## Deliverable
Metric definition sheet (one page) and a short supporting analysis.
