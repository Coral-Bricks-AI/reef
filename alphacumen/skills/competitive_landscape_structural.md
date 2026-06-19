---
id: competitive_landscape_structural
when: Structural competitive-landscape question — "[issuer]'s competitive moat" / "where does [issuer] stand vs competitors" / "who competes with [issuer]". Cross-sectional landscape description, NO multi-year trajectory dimension.
applies_to: [sector_analyst]
source_lines: 33, 778-847
---

**10-K Item 1 "Competition" section ONLY; cap at 4 rounds.**

Trajectory-flavored variants ("how has [X]'s competitive position
**changed/evolved/shifted since** <year>", "evolution of [X]'s
strategy", "[X]'s positioning **over time**") are NOT this skill —
load `competitive_trajectory` instead, which adds an XBRL-facts
pull on top of the Competition narrative because the user expects
specific financial numbers in the answer.

## The trap to avoid

The default instinct is to read multiple 10-Ks / 10-Qs and 8-Ks
looking for "competitive position changes". For a 2-3 year span
this typically becomes 8-12 `get_full_text` calls, hits `max_steps`,
and produces zero final content because the model can't distill a
narrative from raw boilerplate at scale. Production runs that fell
into this trap on competitive-position questions have burned the
entire step budget on 10-K / 10-Q reads and emitted no final answer
— wasted round.

## Where this signal actually lives

10-K Item 1 ("Business") has a standalone **"Competition"**
sub-section (typically 2-4 paragraphs near the end of Item 1). That
sub-section IS the issuer's own framing of its competitive position
— who the competitors are, how the issuer differentiates, what
moat/strategy it claims. Year-over-year changes to that text ARE
the competitive-position changes. Reading 8-Ks / 10-Qs / press
releases adds little: those filings carry quarterly results, not
competitive-landscape narrative. The supplementary signal (market
share shifts, new entrant competitive threats, customer flight)
lives in `bm25_scraped_articles` / `bm25_gdelt`, which the
`vc_analyst` and `risk_analyst` specialists own — let them handle
that side.

## Workflow (cap at 4 rounds)

1. **Single `bm25_sec` call** with `form_type:"10-K"` +
   `event_date_gte:<period_start>` + `k:5` (the most recent 1-3
   10-Ks covering the asked period). The 10-K's Item 1 "Competition"
   sub-section IS the issuer's own competitive-position statement.
2. **One `get_full_text`** on the highest-ranking hit. If the
   period covers multiple FYs, a second `get_full_text` on a
   different-FY 10-K to capture year-over-year deltas. Cap at 2
   total full-text calls.
3. **Quote the issuer's Competition sub-section verbatim**, plus
   any year-over-year deltas you observe across the FYs you pulled.
   Frame the answer as "the issuer's own competitive framing was X
   in FY[Y-2] and shifted to Y by FY[Y]".
4. **Acknowledge supplementary signal lives elsewhere.** If the
   user wants industry market-share shifts, GDELT competitive
   events, or analyst takes on the issuer's strategy, those come
   from `vc_analyst` / `risk_analyst` — say so in the final answer
   rather than trying to fetch them from SEC.

## Common failure modes

- ❌ Reading 8-Ks / 10-Qs hoping to find "competitive position
  changes" in quarterly earnings. They don't carry that narrative —
  they carry quarterly results. Pure budget burn.
- ❌ Trying to derive competitive-position changes from
  year-over-year revenue/margin deltas in the financial statements.
  That's a different question (financial-performance trend); the
  canonical answer for *competitive position* uses the issuer's
  Item 1 "Competition" framing.
- ❌ Going to 8+ rounds. If 4 rounds (1 bm25 + 2 get_full_text + 1
  final answer) hasn't converged, the next 8 won't either — the
  missing signal isn't in SEC filings.
