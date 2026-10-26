# T-1007 Requirements: Vendor invoice vs. AP ledger reconciliation

## Scope
All vendor invoices and vendor statements dated July-September, for the top 40 vendors by spend (about 85% of spend).

## Inputs
- AP ledger extract.
- Vendor statements (PDF/CSV) provided by AP.
- Chart of accounts and the approved adjustment-entry template.

## Method
1. Match invoices to ledger entries on vendor, invoice number and amount.
2. Classify each unmatched item as **timing**, **duplicate**, **pricing difference** or **unknown**.
3. For each non-timing item, propose an adjusting entry using the template.

## Deliverable
Reconciliation workbook (summary, matched, unmatched-with-classification, proposed entries) and a one-page summary of totals by class.
