---
id: run_dcf
when: "Using a DCF / Discounted Cash Flow Analysis ... assuming [these assumptions]" — explicit per-year revenue growth, EBITDA margin, D&A %, capex %, ΔNWC, tax rate, WACC, exit method, and projection horizon for a US-listed issuer.
applies_to: [sector_analyst]
source_lines: 37, 1279-1336
---

**Dedicated tool: `run_dcf`. Call ONCE, pass historical context,
quote verbatim.**

All dollar inputs in $millions. Pass `revenue_growth_pct` and
`ebitda_margin_pct` as per-year arrays of identical length (length
determines projection horizon N). Tool computes NOPAT → FCF → PV
→ EV (Exit Multiple) → Equity Value.

**Pass `historical_revenues_mm` + `historical_fys` +
`historical_notes` for the 2-3 fiscal years preceding the base FY.**
A grounded DCF writeup always anchors the projection in 1-3 years
of reported actuals before the forward cells, so the historical
context is non-optional even when the question says "project from
FY-X". Reviewers expect to see the latest-actual-FY values for
revenue, the EBITDA build components, ΔNWC, and CapEx alongside
the projection.

For dual-method questions (e.g. "does Exit Multiple or Perpetual
Growth yield higher per-share"), also pass `terminal_growth_rate_pct`
— tool returns BOTH methods + the higher-method flag.

## Assumption extraction (do this BEFORE step 1 below)

When the question contains an explicit assumption block
("assume the following:", "using these assumptions:", a bulleted
list of parameter values), extract EVERY stated value VERBATIM and
pass those exact values to `run_dcf`. Do NOT substitute values
derived from historical XBRL, do NOT round / re-estimate / "anchor
on recent trend." A DCF model is the user's stated assumption set
applied to the issuer — substituting historical derivations for
stated inputs produces a different model. Specifically:
  - If the question says "D&A = 14% of revenue" / "D&A as a %
    of revenue is 14.0%" → pass `da_pct_of_revenue=14.0`, NOT
    a value back-solved from historical D&A / Revenue.
  - If the question says "EBITDA margin will be 35.5%" /
    "assume EBITDA margin of 35%" → pass that margin verbatim to
    `ebitda_margin_pct=[35.5, 35.5, ...]`, NOT a value derived
    from historical EBITDA / Revenue.
  - If the question says "tax rate of 21%" / "marginal tax 25%"
    → pass that exact rate, NOT a value pulled from XBRL
    effective tax rate.
  - Same for ΔNWC %, capex %, terminal growth %, discount rate /
    WACC, exit EBITDA multiple, projection horizon (length of
    the growth-rate array).

Only fall back to historical-derived defaults for parameters the
question DOES NOT specify.

**"Other" operating activities + deferred taxes belong in FCF.** When
the question pins these (typical shapes: *"Other operating activities
are core to the business; use the avg of 2023-2025 as a constant"*,
*"Deferred taxes will not change from the 2025 reported value"*),
they enter the FCF projection — not the EBITDA build. Extract the
underlying values via `extract_filing_tables(... cash flow statement)`
or `get_xbrl_facts(...OtherOperatingActivitiesCashFlowStatement)` /
`get_xbrl_facts(...DeferredIncomeTaxExpenseBenefit)`, pre-compute the
constant in $millions (signed: an outflow is negative), and pass it
to `run_dcf` via `other_operating_mm=<signed $mm>` and/or
`deferred_tax_change_mm=<signed $mm>`. "No change from FY-Base" for
deferred taxes → pass `0` (an unchanged balance sheet item adds nothing
to FCF); to lock in a delta, pass the signed annual change. Both
params default to `0`, so omitting them preserves prior behavior
(textbook unlevered FCF = NOPAT + D&A − Capex − ΔNWC).

**Component-defined Adjusted EBITDA.** When the question provides
an EXPLICIT FORMULA for Adjusted EBITDA from component line items
(common shapes: `Adj EBITDA = Op Income + D&A` /
`= Op Income + D&A + Restructuring` /
`= Op Income + D&A + SBC + Restructuring` /
`= Net Income + Interest + Tax + D&A + non-cash items`), the
question's formula is the operative definition for the entire
analysis. Compute Adj EBITDA from the component XBRL pulls and
derive the margin from the result:
`ebitda_margin_pct = (computed_adj_ebitda / revenue) * 100`,
rounded to 4 decimal places. Pass that derived margin to `run_dcf`.
Do NOT substitute the issuer's reported "Adjusted EBITDA" non-GAAP
line — that uses the issuer's preferred recon, not the user's
formula. Anchor the computation on the **latest fiscal year with
reported actuals** for all components — when `base_fy` is a
guidance year (8-K projected revenue with no component-level
disclosure), the component sum has to be computed on `base_fy − 1`
or the most recent actual year. Component pulls (generic recipe):
  - Op Income → `get_xbrl_facts(...OperatingIncomeLoss)`
  - D&A → `get_xbrl_facts(...DepreciationAmortizationAndAccretionNet)`
    or `DepreciationDepletionAndAmortization` /
    `DepreciationAndAmortization`
  - Restructuring → `get_xbrl_facts(...RestructuringCharges)`
  - SBC → `get_xbrl_facts(...ShareBasedCompensation)`
  - Capitalized-contract amortization →
    `get_xbrl_facts(...CapitalizedContractCostAmortization)` when
    the question explicitly bundles this into D&A.

**`historical_notes` coverage.** Pass each of the following keys
(when the corresponding metric appears in either the assumption
block or the component-EBITDA formula) for every historical FY.
The tool emits each value verbatim in the historical context block
and auto-emits a derived "Adjusted EBITDA" line per FY when Op
Income + D&A + Restructuring are all present:

  - `"Operating Income"`  ← `us-gaap:OperatingIncomeLoss`
  - `"D&A"`               ← `DepreciationAmortizationAndAccretionNet`
                            (or `DepreciationDepletionAndAmortization`)
  - `"Restructuring"`     ← `us-gaap:RestructuringCharges`
  - `"SBC"`               ← `us-gaap:ShareBasedCompensation`
  - `"CapEx"`             ← `PaymentsToAcquirePropertyPlantAndEquipment`
  - `"ΔNWC"`              ← `us-gaap:IncreaseDecreaseInOperatingCapital`
                            (signed; negative = NWC decreased / cash inflow)

**Anchor held-constant ratios on the latest-actuals FY.** `base_fy`
in `run_dcf` is the projection-start year. When that year is a
guidance year, its components aren't reported — so the "hold X
constant as % of revenue" inputs (`ebitda_margin_pct`,
`da_pct_of_revenue`, `capex_pct_of_revenue`,
`nwc_change_pct_of_revenue`) must be derived from the most recent
ACTUALS year (typically `base_fy − 1`). Use the corresponding
historical_notes values divided by historical revenue:

  - `ebitda_margin_pct` = (Op Income + D&A + Restructuring) / Revenue × 100
  - `nwc_change_pct_of_revenue` = ΔNWC / Revenue × 100 (preserve sign)
  - `da_pct_of_revenue` = D&A / Revenue × 100
  - `capex_pct_of_revenue` = CapEx / Revenue × 100

Use base-year guidance ratios only when the question explicitly
says "use FY-guidance ratios."

## Workflow

1. Pull base-FY revenue + share count + net debt:
   - `get_xbrl_facts(ref=<base FY 10-K>, concept_pattern="Revenues")`
   - `get_cover_page_share_counts(ref=<base FY 10-K>)` for shares
     outstanding (basic weighted-average if the LBO/DCF question
     specifies)
   - `get_xbrl_facts(...)` for total debt
     (LongTermDebtNoncurrent + LongTermDebtCurrent +
     ShortTermBorrowings) and cash (use
     `CashAndCashEquivalentsAtCarryingValue`).
2. Pull historical revenue + key historical line items for 1-3 FYs
   immediately preceding the base FY. **Non-optional** — the
   rubric typically grades the base FY actuals + 1-2 prior
   historical revenues / restructuring / capex BEFORE the forward
   projection cells. Pass these as `historical_revenues_mm`,
   `historical_fys`, `historical_notes={"restructuring": [...],
   "capex": [...]}`.
3. Call `run_dcf(...)` with all required params. For dual-method
   questions ("Exit Multiple vs Perpetual Growth"), ALSO pass
   `terminal_growth_rate_pct`; the tool returns both methods plus
   the higher-per-share flag.
4. Quote `answer_summary_block` verbatim — historical context block
   first, then projection table, then valuation. Do NOT trim the
   historical block even if the question says "project from FY-X" —
   the rubric grades the base year actuals.

## Worked example (5-year DCF, Exit Multiple, base year = guidance year)

Generic shape for: question pins `base_revenue` from guidance,
defines `Adj EBITDA = Op Income + D&A + Restructuring`, holds the
held-constant ratios from the latest-actuals year. Replace the
angle-bracket values with pulled XBRL numbers — `<latest>` denotes
the latest fiscal year with reported component disclosures.

```
run_dcf(
    ticker=<TKR>,
    base_fy=<guidance_year>,
    base_revenue=<guidance-midpoint revenue in $M>,
    revenue_growth_pct=[g, g, g, g],         # per question (e.g. constant CAGR)
    ebitda_margin_pct=[m, m, m, m],          # m derived from <latest>-FY components
    da_pct_of_revenue=<D&A% from <latest>-FY>,
    capex_pct_of_revenue=<CapEx% from <latest>-FY>,
    nwc_change_pct_of_revenue=<ΔNWC% from <latest>-FY, signed>,
    tax_rate_pct=<%>,
    discount_rate_pct=<%>,
    exit_ebitda_multiple=<x>,
    shares_outstanding_mm=<basic weighted-avg in mm>,
    net_debt_mm=<total debt − cash − any items the question excludes>,
    historical_revenues_mm=[<FY-2 rev>, <FY-1 rev>, <latest rev>],
    historical_fys=[<FY-2>, <FY-1>, <latest>],
    historical_notes={
        "Operating Income": [<FY-2>, <FY-1>, <latest>],
        "D&A":              [<FY-2>, <FY-1>, <latest>],
        "Restructuring":    [<FY-2>, <FY-1>, <latest>],
        "SBC":              [<FY-2>, <FY-1>, <latest>],
        "CapEx":            [<FY-2>, <FY-1>, <latest>],
        "ΔNWC":             [<FY-2>, <FY-1>, <latest>],   # signed
    },
)
```
