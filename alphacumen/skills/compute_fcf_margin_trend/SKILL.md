---
id: compute_fcf_margin_trend
when: Multi-year Free Cash Flow margin trend question — FCF margin across multiple FYs derived from cash-flow-statement primitives (CFO and Capex) plus revenue.
applies_to: [sector_analyst]
source_lines: 947-974
---

**Dedicated tool: `compute_fcf_margin_trend`. Call ONCE.**

Tool pulls CFO (continuing-ops preferred), Capex, Revenue per FY
via XBRL, computes FCF = CFO − abs(Capex) and margin = FCF / Revenue,
returns the canonical FY table + trend narrative. Quote
`answer_summary_block` verbatim — do NOT recompute the math in
`run_python`.

When the tool returns an error about missing CFO/Capex concepts,
fall back to `extract_filing_tables(ref=..., table_keyword="Cash
Flow")` on the FY 10-K and read the multi-year comparative columns
directly.

## Plausibility check — discontinued-operations contamination

When the tool's per-FY FCF margin reads as implausibly high for the
issuer's business type (>50% for a typical operating company, >100%
for any non-financial issuer), the CFO concept the tool picked
includes discontinued-operations cash flows from a unit the issuer
was winding down. Mortgage origination, broker-dealer, and
lease-finance subsidiaries are common culprits. In that case:

(a) Re-call the tool with the issuer ticker but check the returned
`cfo_concept` — if it's
`NetCashProvidedByUsedInOperatingActivities` (consolidated) rather
than `…ContinuingOperations`, the consolidated number includes the
wind-down.

(b) Fall back to MD&A's non-GAAP FCF reconciliation table OR
`extract_filing_tables(ref=..., table_keyword="Free Cash Flow")`
for the issuer's published continuing-ops figure, and use THAT
instead of recomputing.
