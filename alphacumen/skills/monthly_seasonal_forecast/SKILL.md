---
id: monthly_seasonal_forecast
when: Question names a calendar month ("normal March seasonality", "September seasonality") for a foreign private issuer publishing monthly revenue as 6-K filings — the MONTH word is the trigger, not "Q[N]".
applies_to: [sector_analyst]
source_lines: 26, 266-328
---

**Dedicated tool: `format_seasonal_forecast` (preceded by
`fetch_foreign_monthly_revenue`).**

**Quarter resolution rule (critical):** the target month named in
the question IS the month being forecast (the one whose monthly 6-K
hasn't been filed yet). The target quarter is the quarter that
contains the target month, regardless of which quarter the question
literally names. Pattern:

- *"normal [Month] seasonality, will [TICKER] beat Q[N+1] guidance?"*
  — if [Month] is the last month of Q[N], the target quarter is
  Q[N], NOT Q[N+1] (e.g. March → Q1; September → Q3). The
  "Q[N+1] guidance" phrasing in the question is a documented
  misnomer.

If the GP's dispatch instruction frames this as a quarterly
Q[N]→Q[N+1] comparison or asks for sequential quarterly growth
rates, **IGNORE that framing** — the correct math is MONTHLY
(prior_month → target_month growth applied to prior_month actual,
summed with YTD, vs the issuer's Q[N] USD guidance).

## Workflow

1. **First call: `fetch_foreign_monthly_revenue(ticker=<TICKER>,
   fy_start_month="<Y-3>-01", fy_end_month="<Y>-<TargetMonth>")`.**
   Most FPI monthly-revenue 6-Ks are NOT in the local
   sec_filings_chunked BM25 corpus (date-range backfill is
   intentionally narrow). This tool bypasses the local index by
   querying sec-api.io's filings endpoint directly + EDGAR direct
   fetch + regex-extracting every month's revenue value. Returns a
   chronologically ordered list plus pre-computed month-over-month
   growth rates ready to drop into `format_seasonal_forecast`.

2. **If the prior-quarter earnings 6-K (with USD guidance + FX
   rate) is also out-of-corpus**, locate it via `bm25_sec` first
   (the earnings 6-K filed two months after quarter-end usually IS
   indexed); else call `fetch_foreign_monthly_revenue` again or
   query sec-api directly. The press release names the issuer's
   own outlook FX rate in the same paragraph as the USD range —
   use THAT for `fx_local_per_usd`, NOT spot.

3. Call:
   ```
   format_seasonal_forecast(
       ticker, target_year, target_quarter,
       prior_month_name, target_month_name,
       guidance_usd_low, guidance_usd_high,
       fx_local_per_usd,
       history_growth_rates_pct=[...],
       history_years=[...],
       prior_month_actual_local,
       ytd_cumulative_local,
       local_currency_symbol="NT$",
   )
   ```
   The tool runs the seasonal arithmetic (avg growth ×
   prior_month_actual + YTD compared to guidance midpoint) and
   returns 10 canonical atom strings in `answer.formatted_atoms`.

## Fallback when `fetch_foreign_monthly_revenue` returns sparse data

Some FPIs report monthly revenue via 6-K but use non-standard
phrasings the regex doesn't catch. Then fan out `bm25_sec` calls
in parallel for whichever months are missing:
`ticker:<TICKER> form_type:6-K filed_at_gte:<Y>-<MM>-08
filed_at_lte:<Y>-<MM>-12`. `get_full_text` on each top hit (EDGAR
fallback handles stub-body older 6-Ks automatically). Quote the
issuer's percentage VERBATIM — "decrease" = signed-negative growth
rate.

Quote the tool's `answer_summary_block` (already pre-joined with
`- ` markers) directly into your `answer_summary`. Do not
paraphrase — the canonical answer expects the tool's exact phrasing.
