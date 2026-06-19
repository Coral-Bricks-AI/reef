---
id: compute_note_diff_across_years
when: Footnote / disclosure-line delta across two FYs on the same issuer — "how did [note] change from FY-A to FY-B" / "compare the [note] between [year_a] and [year_b]" / "provisional amounts disclosed in FY-A compared to finalized amounts in FY-B" / "reconciliation of [note] from [year_a] to [year_b]". Notes: Acquisitions, Restructuring, Goodwill, Intangible Assets, Purchase Price Allocation, Inventory.
applies_to: [sector_analyst]
source_lines: 36, 1080-1117
---

**Dedicated tool: `compute_note_diff_across_years`. Call ONCE.**

```
compute_note_diff_across_years(
    ticker=<TICKER>,
    note_keyword=<keyword>,
    fy_a=<earlier>,
    fy_b=<later>,
)
```

Tool finds the note in both 10-Ks via `extract_filing_tables`,
line-aligns rows by label, and returns delta + percentage-change
columns. Quote `answer_summary_block` verbatim.

The `note_keyword` should be 1-3 words that uniquely identify the
table in 10-K narrative (e.g. `"Purchase price allocation"`,
`"Restructuring liability"`, `"Identifiable intangible assets"`).
If the tool returns an error indicating the note wasn't found,
retry with an alternative keyword variant (e.g. `"PPA"`,
`"Acquisitions"`, `"Goodwill"`) or fall back to a manual
`extract_filing_tables` call per FY.

## When the same question ALSO asks for per-asset impairment breakouts

(e.g. *"itemize the Q4 FY-X impairments by asset name, amount, and
asset type, identify which impaired assets originated from the
[target] portfolio"*), make TWO additional tool calls AFTER the
note_diff call:

1. `extract_filing_tables(ref=<FY-B 10-K>, table_keyword="impairment",
   item="8", limit=10)` — pulls every impairment-related table in
   the financials section. Per-asset impairment tables are
   typically in the Goodwill and Other Intangible Assets note.
2. `get_full_text(ref=<FY-B 10-K>, max_chars=12000)` filtered by
   section keyword `"intangible"` — captures the narrative naming
   each asset alongside its impairment dollar value.

Enumerate every per-asset impairment in your answer with the asset
name verbatim (`<asset_name> — $<value>M (<asset_type>)`) — do NOT
collapse them into a generic line like *"$X million in finite-lived
brand impairments"* or *"various intangible asset impairments"*
because the rubric grades each asset individually.

After enumerating, identify which assets originated from the
named-target portfolio (use the target's FY-prior 10-K filings as
the reference list of their assets) and compute the
target-portfolio impairment share as `(sum of target-asset
impairments) / (total impairments) × 100%`.
