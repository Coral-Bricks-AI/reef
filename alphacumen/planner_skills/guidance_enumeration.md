---
id: guidance_enumeration
when: A beat-or-miss answer must enumerate EVERY guided metric (revenue, EPS, EBITDA, margins, capex, SBC, weighted shares, tax rate), not just the headline lines.
applies_to: [sector_analyst]
source_lines: 685-698
---

- **Beat-or-Miss enumeration: include every guided metric,
  not just headline metrics.** Earnings 8-K guidance covers a
  full roster: revenue, EPS, EBITDA, gross / operating margin,
  capex, stock-based compensation, weighted shares outstanding,
  effective tax rate. Each guided line item is a distinct
  beat-or-miss data point. The canonical answer to "how did
  [issuer]'s [period] results compare to guidance" enumerates
  EVERY guided metric the issuer provided, not just the
  3-5 headline lines (revenue / EBITDA / EPS). When the specialist
  uses `format_guidance_comparison`, build the input metric list
  to include the operating-level lines (SBC, capex, weighted
  shares, tax rate) alongside headlines. Skipping the operating-
  level metrics produces a partial answer that reads as
  "didn't enumerate the full guidance comparison".
