---
id: marketplace_revenue_decomposition
when: Query asks a two-sided-platform issuer (Revenue = Take Rate x Gross Bookings / GMV) to decompose revenue / enumerate per-segment growth (prefer constant-currency).
applies_to: [sector_analyst]
source_lines: 622-645
---

- **Marketplace revenue decomposition — ALWAYS enumerate the
  per-segment growth rates the issuer reports, and prefer
  constant-currency over GAAP-reported when both are disclosed.**
  When an issuer whose business model intermediates supply and
  demand (any two-sided platform whose top-line is Revenue = Take
  Rate × Gross Bookings / GMV) publishes segment-level gross-
  bookings or GMV with YoY growth by segment, the
  canonical answer to a revenue-decomposition question is the
  decomposition AND the per-segment growth rates **on a constant-
  currency basis**. Constant-currency strips FX noise from cross-
  period comparisons and is the canonical basis for marketplace
  growth comparisons; the GAAP-reported (reported-currency) figure
  carries FX impact that distorts the underlying volume signal.
  When the 10-K MD&A discloses BOTH presentations (e.g. a sentence
  template like *"[Segment] Gross Bookings grew X% year-over-year,
  on a constant currency basis"* alongside a table showing GAAP-
  reported segment results), QUOTE the constant-currency value.
  After calling `compute_revenue_decomposition_take_rate`, ALSO
  instruct the specialist to pull segment-level gross-bookings YoY
  growth
  from the same 10-K's MD&A or via `get_xbrl_facts` filtered to
  the issuer's segment-axis dimension. Quote each segment's
  growth rate in the `answer_summary` — not just the aggregate
  number.
