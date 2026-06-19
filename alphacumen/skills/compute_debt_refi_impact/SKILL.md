---
id: compute_debt_refi_impact
when: Question asks "impact to net income if all debt were refinanced at X% higher" / "Y bp higher rates" / "interest expense sensitivity" — debt-refinancing net-income sensitivity for a single issuer.
applies_to: [sector_analyst]
source_lines: 24, 40-46, 166-201
---

rate_delta_bps mapping: "3% higher" / "3 percentage points" /
"300 basis points" all = 300; "1 percentage point" = 100; "200
bp" = 200. Pass `tax_rate=0.20` (the default — do NOT pass a
different value unless the question states one).

If the dedicated callable returns an error, then and only then
fall back to the manual recipe documented in Hard rule 5.

**Hard rule 5: standard financial ratio formulas.**
- **Debt-refinancing impact (net-income sensitivity) — ALWAYS
  dispatch `compute_debt_refi_impact` via `invoke_skill_fn`
  first; do NOT roll your own bm25_sec + get_xbrl_facts +
  run_python recipe.** When the GP asks about "impact to net
  income if all debt were refinanced at X% higher" /
  "interest expense sensitivity to a Y bp rate increase," your
  FIRST tool call must dispatch
  `compute_debt_refi_impact(ticker=<X>, fy=<YYYY>,
  rate_delta_bps=<bps>)` through `invoke_skill_fn` (see the
  callables block at the end of this skill for the exact
  shape). The callable returns the canonical phrasing in
  `answer.phrasing` (template: *"$<X> Billion Negative Impact
  to Net Income, or a <Y>% decrease"*) — quote that verbatim
  in your `answer_summary`. Use the default `tax_rate=0.20`
  unless the question states otherwise; the pipeline uses 20%,
  NOT the 21% US federal statutory rate.

  Why the callable and not the manual path: the model
  habitually picks the wrong inputs when assembling this
  manually. The callable hardcodes three conventions:
  1. **Long-Term Debt non-current** as the base — NOT Total Debt
     (which includes the current portion). The short-term portion
     is rolled at market rates and is already "at current rates."
  2. **20% effective tax shield** regardless of loss position,
     carry-forwards, or NOLs.
  3. **% decrease vs. `abs(net_income)`**, not % of revenue or %
     change in interest expense.

  Manual fallback (`run_python`) is permitted ONLY when the
  callable returns an error (non-Dec FY-end, missing FY 10-K,
  or XBRL extraction failure):

  ```python
  pre_tax_impact   = lt_debt * (rate_delta_bps / 10000)
  after_tax_impact = pre_tax_impact * (1 - 0.20)
  pct_impact       = after_tax_impact / abs(net_income)
  ```
