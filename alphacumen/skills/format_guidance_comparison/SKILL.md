---
id: format_guidance_comparison
when: Question asks "how did [issuer] [period] results compare to [prior quarter / Q[N]] guidance" — multi-metric beat-or-miss with operating-level lines (SBC, CapEx, Weighted Shares, IFP, GEP) alongside headlines.
applies_to: [sector_analyst]
source_lines: 27, 116-155, 560-609, 1428-1447
---

**Dedicated tool: `format_guidance_comparison`.**

Each metric is phrased as `<Metric>: <actual> <unit>, <verdict>`
where the verdict phrasing is canonical (e.g. "above high end of
guidance range", "high end of guidance range", "above the expected
$X", "right on target").

## Hard rules that apply specifically to this skill

**Guidance-vs-actuals → extract EVERY guided metric, not just
headlines.** Issuers typically guide on 5-8 metrics (revenue,
EBITDA, IFP/GEP, SBC, CapEx, weighted shares, etc.); each one is a
separate data point. Read the FULL Item 2.02 body (not the snippet),
enumerate every guided line, and emit one answer line per metric in
the form *"<Metric>: $<actual>, <above/within/below> <guidance value
or range>"*. Defaulting to headlines drops operating-level lines
and forfeits those atoms. Insurance issuers typically guide on 7+
metrics; SaaS 5-6; fintech 4-5.

**"Prior quarter's full-year guidance" = the Q3 8-K (date-anchored),
not the FY-Q4 8-K.** For an FY result question that references
"prior quarter's guidance," the source is the Q3 8-K filed ~3
months before FY-end (late Oct/early Nov for Dec FYE; late March
for June FYE / GIS). Without an explicit `filed_at_gte`/`filed_at_lte`
bracketing that prior-quarter month, the search surfaces the most
recent 8-K (FY Q4 actuals) and misses the guidance entirely. The
Shareholder Letter exhibit (Item 2.02) is where insurance / SaaS
issuers publish the full guidance table including the
operating-level lines (SBC, CapEx, share count) that the headline
summary drops.

**"At midpoint" → midpoint from $-ranges, NOT from margin
endpoints.** When guidance is published as a *margin range*
alongside its underlying *dollar ranges*, the canonical midpoint
margin = `(dollar_lo + dollar_hi)/2 ÷ (denom_lo + denom_hi)/2`,
NOT `avg(margin_lo, margin_hi)`. The two diverge whenever the
dollar ranges are asymmetric. For the actual side, also use raw
dollars — the issuer's published margin is rounded to 1 decimal and
loses BPS resolution.

```python
guidance_mid_margin = ((ebitda_low + ebitda_high) / 2) / \
                      ((gb_low     + gb_high)     / 2)
actual_margin = actual_ebitda / actual_gb
beat_bps = round((actual_margin - guidance_mid_margin) * 10000, 1)
```

BPS multiplier: `*10000` when starting from decimal margins
(0.02xx); `*100` when starting from percentage-point values already
in the 0-100 scale.

## Workflow

1. Find the prior-quarter 8-K (e.g. Q3 [Y] 8-K for FY [Y] guidance
   question — see synthesizer's beat-or-miss rule for `filed_at`
   windows). `get_full_text` on its Item 2.02 — the exhibits
   walker auto-handles the iXBRL cover; the press release in ex99.X
   has a "Current Outlook" / "Guidance" section listing EVERY
   guided metric.
2. Find the period 8-K (e.g. Q4 [Y] for FY [Y] actuals).
   `get_full_text` for the actuals.
3. Build a list of per-metric dicts, ONE PER GUIDED METRIC (do NOT
   filter to headlines — operating-level metrics are part of the
   canonical answer too):
   ```python
   metrics = [
       {"metric_name": "In Force Premium (IFP)", "actual": <fy_actual>,
        "guidance_low": <prior_low>, "guidance_high": <prior_high>,
        "unit": "Million"},
       # ... one per guided metric ...
       {"metric_name": "Stock-based Compensation", "actual": <fy_actual>,
        "guidance_target": <prior_target>, "unit": "Million"},
       # ... etc for CapEx, Weighted Common Shares, etc.
   ]
   ```
   Use `guidance_target` (single point) when the issuer gave one
   number; use `guidance_low` + `guidance_high` when they gave a
   range.
4. Call `format_guidance_comparison(metrics=metrics)`. Quote
   `answer.answer_summary_block` directly in `answer_summary`.

The tool decides the verdict phrasing ("above high end of guidance
range" vs "above the expected $X" vs "right on target" etc.) based
on whether you passed range or target. Pass `is_loss=True` for
EBITDA Loss metrics so the formatter wraps the actual as `$(X)
Million`.

Insurance issuers typically have 7+ guided metrics in the
shareholder letter; SaaS 5-6; fintech 4-5.
Coverage matters — missing a metric is a per-atom miss.

## Guidance / Outlook tables in 8-K shareholder letters

This rule applies in full to "Guidance" / "Outlook" tables in 8-K
shareholder letters. When the question is "FY result vs guidance"
(beat-or-miss), the 8-K's Guidance section is a *table* where every
row is a guided metric — even when individual lines look like
operating items (Stock-based compensation expense, Capital
expenditures, Weighted common shares). Insurance issuers typically
guide on 7+ metrics; SaaS issuers on 5-6; fintech on 4-5. Do NOT
filter to "headline" metrics like Revenue and EBITDA and drop the
rest as "operating-level". Each guided line is a separate data
point — a 3-metric answer to a 7-metric guidance table is a
4-data-point miss before review even starts. The "Current Outlook"
/ "Guidance" section of an insurance- or SaaS-issuer 8-K typically
has rows for SBC, CapEx, and Weighted Common Shares formatted as
`<Metric> <Q-actual> <FY-guidance>` — list EVERY row from the
section header through to the next section break in your
`answer_summary`.
