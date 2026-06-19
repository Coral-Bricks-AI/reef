---
id: beat_or_miss_routing
when: Query asks did X beat/miss guidance, or compares actuals vs guidance, or asks across multiple quarters (Q1-Q4 / last N quarters).
applies_to: [sector_analyst]
source_lines: 421-436
---

- **Multi-quarter beat-or-miss → route to sector_analyst.** When
  the question asks "in each of the last N quarters" / "for Q1-Q4
  of FY[year]" / "across all four quarters," dispatch
  sector_analyst with ticker + FY + the metric named explicitly,
  and raise `max_steps` to 12 (one quarter ≈ two tool calls).
  For non-Dec FY-ends (GIS June, ORCL May), state the calendar
  window in plain English so the specialist anchors on the right
  months.
- **Beat-or-miss questions → route to sector_analyst.** When the
  user asks "did X beat or miss guidance" / "compare actuals to
  guidance," dispatch sector_analyst with the calendar windows for
  both filings (prior-quarter guidance + period results) stated in
  plain English, and ask explicitly for **every guided metric**
  (revenue, EBITDA, SBC, CapEx, weighted shares, etc.) — not just
  headlines — in a per-metric table. Raise `max_steps` to 12 so
  the specialist has the budget to read both filings in full.
