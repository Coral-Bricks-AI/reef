---
id: compute_revenue_decomposition_take_rate
when: Marketplace revenue-growth decomposition — attributing a marketplace issuer's YoY revenue growth between unit-economics (take rate = revenue / gross-bookings) and volume (gross bookings / GMV). Phrasings like "portion", "share", or "contribution" came from each driver.
applies_to: [sector_analyst]
source_lines: 1014-1058
---

**Dedicated tool: `compute_revenue_decomposition_take_rate`. Call
ONCE, then enumerate per-segment growth.**

Tool pulls Revenue + Gross Bookings/GMV for FY and FY-1, computes
per-FY take rate, and attributes growth using the canonical
take_rate × volume identity. Quote `answer_summary_block` verbatim.

When the volume concept isn't found, the issuer likely uses an
extension tag (e.g. `<issuer>:GrossBookings`); pass it explicitly via
`volume_concept`.

## Per-segment growth enumeration

Two-sided marketplace issuers typically report gross bookings / GMV
broken out by operating segment (whichever segments the issuer
publishes). The canonical answer enumerates each segment's YoY
growth rate, not just the aggregate.

`compute_revenue_decomposition_take_rate` now auto-fills the
`per_segment_growth` field with parsed values from the most-recent
10-K Item 7 MD&A's canonical sentence template (`"[Segment] Gross
Bookings (grew|declined|increased|decreased|rose|fell) [by] X%"`).
When the tool returns a populated `per_segment_growth`, quote each
segment's growth-with-sign from `answer_summary_block` verbatim —
DO NOT paraphrase a signed-negative growth as "Volume recovery" or
qualitative narrative; quote the literal value (e.g. `[Segment]:
-X%` or `[Segment] declined X%`).

If the tool returns empty `per_segment_growth`, fall back to one of:

1. `get_xbrl_facts(ref=<10-K ref>, concept_pattern="GrossBookings",
   periods=["<FY>", "<FY-1>"])` filtered to the issuer's
   segment-axis dimension — XBRL records segment-scoped values with
   a `dimensions` / `segments` field that the main tool skips (it
   returns consolidated only).
2. OR `extract_filing_tables(ref=<10-K ref>, item="7",
   table_keyword="Segment results")` and read the per-segment
   bookings column directly.

Add per-segment lines to the `answer_summary`:
`- Mobility: +X%`, `- Delivery: +Y%`, `- Freight: +Z%` (or whatever
segment names the issuer reports, with the signed percentage from
the tool / filing). The aggregate take-rate + volume narrative is
necessary but not sufficient — the canonical answer for marketplace
decomposition questions includes the per-segment breakdown.
