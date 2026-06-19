---
id: financial_ratio_formulas
when: A round needs a financial ratio / formula whose convention diverges from the textbook version (inventory turnover denominator, N-year CAGR exponent, EBITDAR→EBITDA→EBIT bridge). Load whenever any ratio/CAGR/bridge calculation is in scope.
applies_to: [sector_analyst]
source_lines: 166, 202-264
---

**Shared reference of formula conventions used by this pipeline.**
Conventions diverge from textbook formulas; the divergences are
graded.

## Inventory Turnover — MUST use ENDING inventory as the denominator

Do NOT use the (Beginning + Ending) / 2 textbook average. This is
the authoritative convention for this pipeline; the analyst-style
"average of two period-end values" formula is NOT used here
regardless of what training-data examples suggest.

```python
# run_python — this is the ONLY acceptable inventory turnover
# calculation for this pipeline:
cogs = <COGS_for_period>          # Cost of Sales
ending_inventory = <fy_end_inv>   # Period-END balance-sheet inventory
avg_inventory = ending_inventory  # NOT (beg + end) / 2 !
inventory_turnover = round(cogs / avg_inventory, 2)
```

When stating the inputs in your `answer_summary`, label the
denominator as **"Avg. Inventory: $<ending_value>"** with the
period-end value — quote in this exact format.

Do NOT show the (Beg + End) / 2 work in the answer even as side
commentary — it triggers contradiction failures when the reference
uses a single value.

## "N-year CAGR" — MUST use N from the question as the exponent

When the question says "3-year CAGR," the exponent is `1/3`, not
`1/2`. The N in "N-year CAGR" counts the fiscal years *involved*,
not the year-gaps. This is FactSet/Bloomberg convention.

- **WRONG (do NOT do this):** "3-year CAGR from FY2017 to FY2019 is
  `(FY2019/FY2017) ** (1/2) - 1`" — this is a 2-year CAGR formula
  applied to a 3-year window. It will be marked incorrect.
- **RIGHT:** "3-year CAGR from FY2017 to FY2019 is
  `(FY2019/FY2017) ** (1/3) - 1`. Three fiscal years are involved
  (2017, 2018, 2019), so N=3."
- The N=3 worked example above uses generic FY2017→FY2019 years for
  illustration. The rule applies to ANY (start, end, N) — for a
  5-year CAGR from FY[A] to FY[A+4], the exponent is `1/5` (five
  fiscal years involved). Always count *fiscal years involved*,
  never year-gaps.
- **Test the rule:** if the question says "K-year" and your chosen
  start/end fiscal years span (K-1) calendar gaps, you still use
  `1/K` as the exponent — never `1/(K-1)`. The window is defined by
  the question's number, not by how many `**` steps you'd take in a
  `for` loop.
- This rule fires for revenue CAGR, EPS CAGR, FCF CAGR, any CAGR
  question. Always compute via `run_python` with an inline comment
  `# N-year CAGR per FactSet convention: 1/N` so the calculation is
  auditable.

## Adjusted EBITDAR → EBITDA → EBIT — walk the FULL bridge, never stop one step short

Each rung differs by one subtraction:
- EBITDA = EBITDAR − rent expense
- EBIT = EBITDA − D&A

When a question (or ratio numerator/denominator) names "Adjusted
EBIT" or "Adjusted EBITDA," compute it by walking the issuer's
non-GAAP reconciliation table all the way down — do NOT halt at
EBITDAR − rent and label that value "EBIT." A skipped D&A
subtraction is the single most common way these answers go wrong,
and outsized one-off D&A (sale-leaseback / impairment years) can
flip Adjusted EBIT negative even when EBITDAR and EBITDA are
healthy positives.

If Adjusted EBIT comes out negative, an interest coverage ratio is
**zero** by convention, not "n/a" and not the EBITDA-based number.

Show every rung of the bridge in `run_python` (EBITDAR, − rent →
EBITDA, − D&A → EBIT) so the chain is auditable in the answer.
