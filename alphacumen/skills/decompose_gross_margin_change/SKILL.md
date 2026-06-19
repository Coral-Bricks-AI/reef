---
id: decompose_gross_margin_change
when: Gross-margin change decomposition question — Δ gross margin between two periods, attribution to revenue vs cost-of-sales, identification of dominant COGS line item driving the change, normalized / adjusted gross margin removing the dominant change driver.
applies_to: [sector_analyst]
source_lines: 0
---

**Dedicated tool: `decompose_gross_margin_change(ticker, period_a, period_b, period_kind)`.** Pulls Revenue + COGS for both periods, computes GM% per period and Δ GM in percentage points, attributes the change to a revenue-side effect vs a cost-side effect, surfaces COGS sub-components when the issuer discloses them, identifies the largest sub-component contributor, and produces a normalized GM (period A re-stated with period-B level of that sub-component).

`period_kind` accepts `"annual"` (default; uses 11-13 month XBRL spans / 10-K data) or `"quarterly"` (uses 2-4 month XBRL spans / 10-Q + 8-K data). Pass `"quarterly"` when the question references 8-K data, when the dates are interim quarter-ends rather than fiscal-year-ends, or when the issuer reports a sub-FY period (e.g. Feb 28 for a calendar-FY issuer). The tool also auto-detects quarterly periods from the date-suffix heuristic — dates that don't match common fiscal-year-end conventions (12-31, 01-31, 02-01, 02-02, 06-30, 09-30, 03-31) are treated as quarterly even when `period_kind="annual"` is passed.

## Workflow

1. Call `decompose_gross_margin_change(ticker=<TICKER>, period_a=<earlier YYYY-MM-DD>, period_b=<later YYYY-MM-DD>, period_kind="annual"|"quarterly")` as your first tool call. Period kind defaults to annual.

2. Quote the tool's `answer_summary_block` verbatim — it includes the reported margin change in percentage points, the revenue-vs-cost attribution, the dominant sub-component contribution, and the normalized margin change after stripping the dominant driver.

3. **Forward-direction commentary** is a separate atom. Use the tool's dominant-driver name (e.g. a specific COGS line) and reason qualitatively about whether it is likely to grow / shrink in the subsequent fiscal year given the issuer's MD&A and capex disclosures. Do NOT pre-commit to a direction without a documented rationale; quote the relevant MD&A sentence if one exists.

4. If the tool returns `dominant_subcomponent: null` (issuer doesn't break out COGS components in the income-statement footnote), fall back to:
   - `extract_filing_tables(ref=<period-B 10-K>, table_keyword="cost of revenue", item="8")`
   - `extract_filing_tables(ref=<period-B 10-K>, table_keyword="cost of sales", item="8")`
   - `get_full_text(ref=<period-B 10-K>, max_chars=20000)` + manual scan for COGS-component dollar amounts adjacent to category labels.

Generic across any issuer with two comparable periods; no eval-keyed line items or magnitudes baked in.
