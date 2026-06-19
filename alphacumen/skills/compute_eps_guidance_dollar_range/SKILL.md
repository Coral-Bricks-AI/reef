---
id: compute_eps_guidance_dollar_range
when: Question asks about EPS guidance / EPS beat / EPS miss for an issuer whose FY guidance is a constant-currency growth percent (consumer staples — GIS, KO, PEP, KMB, CL, CHD — and most foreign large-caps).
applies_to: [sector_analyst]
source_lines: 28, 365-440
---

**Dedicated tool: `compute_eps_guidance_dollar_range`.** The canonical
answer is the **absolute dollar range** = `prior_year_EPS × (1 +
growth_bound)`, NOT the percent string the issuer published.

**Source filing for each FY:** the Q4 FY-N earnings 8-K (Item 2.02
+ ex99 exhibit's "Current Outlook" / "Fiscal [YYYY] Outlook" block)
contains BOTH the next-year growth-percentage guidance AND the
prior-year actual EPS. Use the ORIGINAL guidance from the Q4 8-K
— NOT any mid-year revision (Sep/Dec 8-Ks).

## Workflow (per FY in the comparison window)

1. `bm25_sec` for the issuer's Q4 8-K with a ±7-day `filed_at`
   window around the canonical Q4-earnings release date.
2. **`get_full_text` on each Q4 8-K body** — do NOT use
   `extract_filing_tables` for these. Consumer-staples 8-Ks describe
   the EPS outlook in NARRATIVE prose ("we expect adjusted diluted
   EPS to grow X% to Y% in constant currency"), not in a structured
   table. `extract_filing_tables` returns empty results and burns
   tool steps. Read the body, find the "Current Outlook" / "Fiscal
   [YYYY] Outlook" paragraph, extract the growth-percent low/high
   AND the prior-year actual adjusted diluted EPS (also in the same
   body, in the per-share results summary).
3. Call `compute_eps_guidance_dollar_range(ticker, fy,
   prior_year_actual_eps, growth_low_pct, growth_high_pct)` to get
   the canonical `$X.XX - $Y.YY` string in `answer.range_phrasing`
   and the atom-shaped string in `answer.atom_phrasing`. Quote those
   verbatim.
4. For beat/miss verdicts: call the tool again for the same FY
   passing `fy_actual_eps=<actual>` (extracted from the NEXT Q4
   8-K, after the fiscal year closes). The tool returns a
   `verdict_phrasing` (e.g. "TICKER beat Adjusted Diluted EPS
   guidance midpoint in YYYY") you can adapt to the issuer's full
   name.

`answer_summary` MUST report the DOLLAR range (not the percent
string). For multi-year beat counts, include per-year verdicts AND
the total count ("beat N times in the past M years").

**Beat counts are ANNUAL, not quarterly.** For "how many times has
X beaten EPS guidance in the past N years" questions, the unit is
FISCAL YEARS, not quarters. Each FY is one observation: the FY
actual vs the FY guidance issued at the start of that year. Do NOT
enumerate quarterly beats (Q1/Q2/Q3/Q4 vs analyst consensus) —
those answer a different question and dilute the annual answer.
Phrase the final count canonically: "beat <N> times in the past
<M> years".

**Quote ONLY the ORIGINAL Q4-issued guidance in `answer_summary`.**
Do NOT also mention mid-year revised guidance (Sep/Dec 8-Ks) even
as a side note — the canonical answer is the originally-issued
range only. A "December 2024 revision: $X.XX-$Y.YY" bullet in your
answer introduces facts the canonical answer doesn't carry, and
reads as a contradiction of the original-only framing.

## Common failure modes

- ❌ Reporting the percent string as the guidance instead of the
  derived dollar range.
- ❌ Quoting a mid-year revised outlook instead of the original
  Q4-issued one.
- ❌ Using the wrong prior-year EPS as the multiplier (each year's
  guidance uses the IMMEDIATELY preceding year's actual).
- ❌ Approximating the dollar range mentally — off-by-one-cent
  errors are visible to the reader and weaken the answer.
- ❌ Calling `extract_filing_tables` to find the outlook — the
  outlook is in narrative text, not a structured table. Use
  `get_full_text` and read the prose.
- ❌ Burning steps on multiple table-extraction attempts before
  calling `compute_eps_guidance_dollar_range` — once you've
  extracted the % growth + prior-year EPS from one `get_full_text`,
  call the tool immediately.
