---
id: run_lbo
when: "Create a simplified LBO model for the illustrative take-private of [TICKER] using the following assumptions" — explicit premium / leverage / hold / exit multiple / interest rate / revenue-growth / margin-expansion assumptions, asks for sponsor equity / exit EV / exit equity / IRR / MOIC.
applies_to: [sector_analyst]
source_lines: 38
---

**Dedicated tool: `run_lbo`. Call ONCE.**

**PASS `asof_date=<YYYY-MM-DD>` to let the tool fetch the recent
close from `get_equity_bars` internally — DO NOT pass a
round-number placeholder like `recent_close=200.0` in lieu of the
actual close.**

## Workflow

1. Pull from filings:
   - basic weighted-average shares outstanding from the FY 10-K
     (`get_cover_page_share_counts` or `get_xbrl_facts` with
     `WeightedAverageNumberOfSharesOutstandingBasic`)
   - base FY revenue + EBITDA components (Operating Income + D&A)
     via `get_xbrl_facts`

   All dollar inputs in $millions.

2. Call:
   ```
   run_lbo(
       target_ticker=<TICKER>,
       asof_date=<YYYY-MM-DD>,
       offer_premium_pct=<%>,
       shares_outstanding_mm=<mm>,
       base_revenue_mm=<$mm>,
       base_ebitda_margin_pct=<%>,
       revenue_growth_pct_per_year=<%>,
       ebitda_margin_expansion_bps_per_year=<bps>,
       da_pct_of_revenue=<%>,
       capex_pct_of_revenue=<%>,
       nwc_change_pct_of_revenue=<%>,
       tax_rate_pct=<%>,
       leverage_multiple_x=<x>,
       debt_interest_rate_pct=<%>,
       exit_ebitda_multiple=<x>,
       hold_years=<N>,
       transaction_expenses_mm=<$mm>,
       cash_balance_min_mm=<$mm>,
       cash_interest_rate_pct=<%>,
   )
   ```

Tool handles sources/uses, year-by-year projection with avg-balance
interest expense + income via fixed-point iteration (matches the
canonical LBO convention), FCF debt-paydown logic, exit EV / equity
/ IRR / MOIC.

Quote `answer_summary_block` verbatim.

**DO NOT do the LBO math by hand** — the avg-balance interest
expense is iterative and manual math reliably under- or over-counts
the sponsor equity by the transaction-expense line.
