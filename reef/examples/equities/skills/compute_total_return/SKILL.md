---
id: compute_total_return
when: Compute trailing 1-year price return for a ticker — accounts for price change from one year ago to today.
applies_to: [equity_analyst]
---

**Dedicated tool: `compute_total_return`. Call AFTER `search_companies` has returned the ticker.**

```
compute_total_return(
    ticker=<TICKER from search_companies results>,
)
```

Computes trailing 1-year price return:

    pct_return = (price_now - price_1y_ago) / price_1y_ago

This is **price return** only — it does not include dividends. The mock corpus
is point-in-time and illustrative; treat the numbers as fictional, not as live
market data.

Returns:
- `ticker` — echoes the input (uppercased)
- `pct_return_1y` — rounded to 1 decimal place
- `price_now` / `price_1y_ago` — anchoring numbers
- `answer_summary_block` — grader-ready text; **quote this verbatim** in
  your reply rather than reformatting

If the ticker is unknown, the function returns an `error` envelope —
surface that to the user.
