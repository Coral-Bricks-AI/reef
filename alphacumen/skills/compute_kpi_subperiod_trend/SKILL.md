---
id: compute_kpi_subperiod_trend
when: A single named non-financial / operational KPI tracked across a multi-year window (≥3 FYs) where the canonical answer expects per-year values PLUS sub-period growth-rate framing. Covers ARPU/ARM/ARPPU, MAU/DAU, paid memberships, subscribers, deliveries, room-nights, miles flown, etc.
applies_to: [sector_analyst]
source_lines: 908-945
---

**Dedicated tool: `compute_kpi_subperiod_trend`. Call ONCE.**

KPIs in this category include — but are not limited to — average
revenue per user/member/customer (ARPU / ARM / ARPPU), monthly /
daily active users (MAU / DAU), paid memberships, subscriber counts,
deliveries, units shipped, packages sorted, room-nights booked,
miles flown, occupancy rates, average ticket size, or any
issuer-specific operating metric tracked in MD&A / 10-K disclosures.
Per-segment / regional cuts are NOT this skill — those are the
issuer's secondary breakdowns; the canonical answer is the
consolidated annual series.

Tool pulls per-FY XBRL values + canonical overall CAGR + two
sub-period CAGRs in one call (1 bm25_sec + 1-N get_xbrl_facts
internally). Quote `answer_summary_block` verbatim.

## Why this skill (not manual extract_filing_tables)

The manual workflow — `bm25_sec` + `extract_filing_tables` +
per-year value quotes — reliably surfaces the per-FY series
but stops there. Trend questions ("how has X changed /
trended / evolved over Y-Z") consistently require at least
one sub-period CAGR beyond the overall CAGR: rubrics grade
the **trend decomposition** (peak-to-plateau, growth-then-
flat, accel-then-decel) as a separate atom from the per-year
values themselves. The dedicated tool returns both layers in
one call; the manual recipe returns only the values. Skipping
this skill for a "how has X changed / trended" question
loses the sub-period attribution atom by default — regardless
of how many per-year values the manual recipe captures.
Per-year values are necessary but not sufficient.

## After the tool returns: quote management's stated cause

When the per-FY series shows a visible inflection (plateau,
decline, acceleration) and the two sub-period CAGRs differ
materially (≥2× ratio or sign flip), trend rubrics typically
ALSO grade an attribution atom — "the inflection in FY[YYYY]
was driven by [reason]." The tool produces the numeric
decomposition; the attribution itself comes from prose.

Workflow after `compute_kpi_subperiod_trend` returns:

1. Examine `early_subperiod_cagr_pct` vs `late_subperiod_cagr_pct`
   in the returned envelope. If the sign flips or magnitudes
   differ ≥2×, treat the `inflection_fy` as a meaningful
   regime change worth attributing.
2. Call `get_full_text` (or `extract_filing_tables` with the
   relevant section keyword) on the 10-K MD&A from the
   `xbrl_refs_used` filings — issuers routinely explain
   inflections in the "Results of Operations" / "Revenue"
   / "Key Metrics" sub-sections (e.g. "this decline was
   driven by [product mix shift / FX / promotional pricing /
   ad-supported plan launch / regional expansion]").
3. Add a 1-line "Management cited [stated cause]" sentence
   alongside the tool's table. Do NOT speculate cause from
   training prior — quote what the MD&A says, or omit if the
   filing does not discuss it.

Failing this step loses the attribution atom even when the
sub-period CAGRs are perfectly correct.

## Picking `kpi_concept`

Pick `kpi_concept` from the natural-language metric name
(case-insensitive substring on the us-gaap concept):
- ARPU / ARM / "average revenue per X" → `"AverageRevenuePer"`
- MAU / DAU → `"MonthlyActive"` / `"DailyActive"`
- paid memberships / subscribers → `"PaidMemberships"` /
  `"Subscribers"`
- delivery count → `"Deliveries"` / `"VehicleDeliveries"`
- room-nights / passenger metrics → `"RoomNights"` /
  `"RevenuePassengerMiles"`

If the first call returns no facts, retry with an alternate
substring (XBRL concept names vary across filers) before falling
back to bm25_sec on the KPI metric definition.

**Do NOT route this class of question to `news_quant_analyst` or
`vc_analyst`.** Those analysts search scraped articles and GDELT
news, which carry per-quarter or per-region cuts in news coverage;
the canonical answer is the consolidated annual series from the
10-K, which only XBRL on the 10-K provides cleanly.
