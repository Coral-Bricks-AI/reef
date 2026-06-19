---
id: compute_issuer_ratios
when: Single-ticker ratio pull (P/B, D/E, D/Cap, EV/EBITDA, EBITDA margin, EBITDAR margin, DIO, FCCR) for ONE issuer at a specific fiscal year and balance-sheet as-of.
applies_to: [sector_analyst]
---

**Dedicated tool: `compute_issuer_ratios`. Call ONCE per dispatch.**

```
compute_issuer_ratios(
    ticker=<T>,
    ratios=[…],
    fy=<YYYY>,
    asof_market_price=<YYYY-MM-DD or null>,
    bs_asof_date=<YYYY-MM-DD or null>,
)
```

Pass `bs_asof_date` when the question pins the balance sheet to a
non-FY-end date (e.g. "balance sheet as of September 30, 2025" in
a question about FY2025). Income-statement items still come from
the FY 10-K to preserve annual semantics.

Supported ratios: `p_to_b`, `d_to_e`, `d_to_tc`, `ev_ebitda`,
`ebitda_margin`, `ebitdar_margin`, `dio`, `fccr`.

Returns this issuer's primitives (equity, debt, cash, market cap,
close price, issuer-disclosed book value per share when published)
plus each requested ratio. Quote the `answer_summary_block`
verbatim — the planner's postprocessor will compose the
cross-ticker ranking and peer averages from each per-ticker post.

**Do not compare against other tickers in your answer.** Your
dispatch covers exactly one ticker; the planner has dispatched
sibling specialists for the others. Keep your post scoped to
your assigned ticker so the comparison composition stays clean.
