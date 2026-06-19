---
id: extract_restated_adj_ebitda_continuing_ops
when: Multi-year Adjusted EBITDA reconstruction question where the window spans 4+ years and includes any FY old enough that the issuer's latest 10-K's 2-3 year MD&A recon doesn't reach back to it. Load this skill ALONGSIDE `extract_ebitda_reconciliation_multi_year` for any 5-year recon question; the main skill handles the recent years, this sub-skill provides the restated continuing-operations value for the oldest year(s) when the issuer has had a divestiture / spin-off / discontinued-ops reclassification in the intervening years.
applies_to: [sector_analyst]
source_lines: 0
---

**Dedicated tool: `extract_restated_adj_ebitda_continuing_ops(ticker, fy_target)`. Call ONCE per target FY.**

Walks the issuer's 10-K stack backward from the latest 10-K to find the most recent filing whose multi-year recon table includes `fy_target` as a column AND emits an "Adjusted EBITDA from continuing operations" (or close equivalent) row. Returns that restated value. Generic across any US-listed issuer with a multi-year EBITDA reconciliation convention.

## Why this exists

When an issuer reclassifies prior-year results for a discontinued-ops event (segment spin-off, divestiture, business held-for-sale), the prior years' Adj EBITDA gets split into "continuing operations" + "discontinued operations". The original FY-N 10-K reports the COMBINED total (as-originally-filed). A LATER 10-K (typically FY-N+1 or FY-N+2) restates FY-N in its 5-year recon table, showing the continuing-operations portion alone -- which is materially lower.

Rubrics for multi-year EBITDA questions reference the CONTINUING-OPERATIONS RESTATED value, not the as-originally-filed total. The `extract_ebitda_reconciliation_multi_year` skill picks up the latest 10-K's recon -- when that recon doesn't span back to fy_target (5-year window doesn't reach old enough years), it falls back to fy_target's own 10-K which gives the as-originally-filed combined total. This sub-skill bridges that gap.

## Workflow

1. Identify the target FY whose value seems "wrong" in the broader recon (e.g. the multi-year table's oldest year differs from the rubric by 5-10%, or shows a value materially higher than later years' "from continuing operations" line).
2. Call `extract_restated_adj_ebitda_continuing_ops(ticker=<TICKER>, fy_target=<YYYY>)`.
3. The skill walks 10-Ks backward (fy_target + 1, fy_target + 2, fy_target + 3, fy_target + 4) until it finds a 10-K whose recon table includes fy_target as a column with a "from continuing operations" row.
4. Quote `answer_summary_block` verbatim. Use the returned value in place of the as-originally-filed figure.

## When this skill applies

- Multi-year Adjusted EBITDA reconciliation questions whose window covers a year predating a divestiture / discontinued-ops event
- Adjacent: any rubric atom that compares historical continuing-operations EBITDA against the issuer's restated-for-comparability series

## Common failure modes (this skill prevents)

- ❌ Quoting the FY-N 10-K's as-originally-filed Adj EBITDA when the rubric grades against the FY-N+2 10-K's restated continuing-operations value (the rubric convention for multi-year trajectories where a divestiture happened mid-window).
- ❌ Assuming the LATEST 10-K's recon always covers the target FY -- many issuers' MD&A non-GAAP recon is a 2-3-year layout, not a 5-7-year Selected-Financial-Data layout, so the latest 10-K misses the earlier years entirely.

Generic across any US-listed issuer that publishes "Adjusted EBITDA from continuing operations" + "Adjusted EBITDA from discontinued operations" rows in their 5-year recon. No ticker-specific or rubric-keyed value baked in.
