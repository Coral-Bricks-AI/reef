---
id: multi_quarter_beat_miss
when: Question asks "beat or miss guidance in each of the last N quarters" / "Q1-Q4 of FY[year] beat/miss" / "across all four quarters" — multi-quarter sequence for one ticker.
applies_to: [sector_analyst]
source_lines: 25, 72-114
---

**Dedicated tool: `find_quarterly_earnings_8ks`. Call FIRST.**

```
find_quarterly_earnings_8ks(ticker=<X>, fy=<YYYY>)
```

Returns 5 paired 8-Ks (Q4 FY-1 + Q1-Q4 FY) with explicit
guidance/actuals refs per quarter. Do NOT issue your own `bm25_sec`
calls to rediscover them — the tool runs five date-bounded queries
internally and returns the canonical pairs.

After it returns, call `get_full_text` on each ref and use
`run_python` to compute beat/miss per quarter.

## Non-GAAP gross profit specifically — extract the DIRECT $ value

NEVER derive from `revenue × margin`. Every semiconductor / hardware
non-GAAP earnings 8-K reports the actual non-GAAP gross profit as
an explicit dollar line in the "Reconciliation of GAAP to non-GAAP
Financial Measures" table (usually labeled "Non-GAAP gross profit"
with the $ amount right next to it). Use:

```
extract_filing_tables(ref=<actuals_8k>, table_keyword="Non-GAAP gross profit", item="2-2")
```

The directly-reported dollar value is the canonical disclosed value.
Computing `revenue × rounded_margin` is typically off by $5-15M from
the reconciled value because the actual non-GAAP margin has 1-2 more
decimal places of precision than what's quoted in the earnings press
release (e.g. the release says "approximately 52%" while the
reconciliation table carries 52.13% or similar). The canonical
answer is the exact reconciled dollar value, so a 0.3-0.5pp
beat-percentage error fails the atom even when the underlying logic
is right.

## Output format for "QX - $XXX million (X.X% BEAT/MISS)"

When the question's response template is `"QX - $XXX million (X.X %
BEAT or MISS)"`, the `$XXX million` slot holds the **ACTUAL value of
the metric** (the reconciled non-GAAP dollar amount from the actuals
8-K), NOT the dollar amount of the beat (i.e. NOT `actual −
guidance`). The percentage in parentheses is the beat/miss
percentage `(actual − guidance_midpoint) / guidance_midpoint × 100`.

Get this format right — readers (human or automated) can fail to
match the answer to the question even when the underlying math is
correct.
