---
id: compute_competitive_trajectory
when: Competitive question with explicit multi-year trajectory dimension — "how has [X]'s competitive position changed since [year]" / "evolution of [X]'s strategy" / "[X]'s positioning over time". User expects SPECIFIC FINANCIAL NUMBERS alongside the Item 1 narrative.
applies_to: [sector_analyst]
source_lines: 34, 849-906
---

**Dedicated tool: `compute_competitive_trajectory`. Call ONCE; do
not hand-assemble the recipe.**

These look like structural competitive-landscape questions (see
`competitive_landscape_structural`) but the canonical answer needs
**specific financial numbers** alongside the Item 1 narrative;
the structural recipe ("Competition section only") under-answers
them.

## Workflow (cap at 2 rounds = 1 trajectory call + 1 final)

1. **One `compute_competitive_trajectory` call** with:
   ```
   compute_competitive_trajectory(
       ticker=<TICKER>,
       fy_start=<earliest FY in the question>,
       fy_end=<most recent reported FY>,
   )
   ```
   The tool internally chains `bm25_sec(form_type:"10-K")` →
   `get_full_text` on the most-recent 10-K (extracts the Item 1
   Competition sub-section) → `get_xbrl_facts` across the FY window
   for the four canonical income-statement concepts (`Revenues`,
   `GrossProfit`, `OperatingIncomeLoss`, `NetIncomeLoss`). Returns a
   pre-composed `answer_summary_block` (Competition quote + FY-by-FY
   markdown table).

2. **Quote `answer_summary_block` verbatim** in the final answer,
   then add 1-2 sentences connecting the numerical trajectory to
   the narrative ("Revenue grew X% from FY[a] to FY[b] while gross
   margin compressed Y bps, consistent with the FY[b] 10-K's shift
   from 'beneficial competition' framing to 'highly competitive'
   language").

**Do NOT hand-assemble the bm25_sec → get_full_text →
get_xbrl_facts recipe.** Earlier versions of this rule prescribed
the recipe inline; the tool now enforces it deterministically in
~25-40s wall time (vs. 3 sequential specialist rounds), picks the
correct XBRL concepts the first time, and filters segment-scoped
facts out of the table automatically.

**When the tool's `error` field is set** (no 10-K hits — typically
foreign filers on 20-F, or non-calendar FY issuers whose
`event_date_gte` window missed the actual filing), fall back to
`bm25_sec(form_type:["10-K","20-F"], ticker:<TICKER>)` and the
manual recipe. For 20-F filers specifically, XBRL tagging is
optional — use `extract_filing_tables(ref=..., table_keyword=
"Consolidated Statements of Operations")` on one of the hits and
read the multi-year comparative column directly.

## Common failure modes

- ❌ Calling `bm25_sec` + `get_full_text` + `get_xbrl_facts`
  separately when `compute_competitive_trajectory` would have done
  it in one call. Burns 3 rounds for the same output.
- ❌ Treating the tool's `answer_summary_block` as raw data and
  paraphrasing the table. Quote it verbatim — paraphrasing drops
  digits and the canonical answer expects exact numbers.
- ❌ Stopping at the tool's Competition narrative without including
  the FY table in the final answer. The canonical answer needs both
  the narrative AND the multi-year financial trajectory; the
  narrative alone reads as "no specific financials".
