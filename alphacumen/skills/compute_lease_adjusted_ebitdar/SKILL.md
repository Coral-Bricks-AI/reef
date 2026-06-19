---
id: compute_lease_adjusted_ebitdar
when: Multi-year EBITDAR (EBITDA + Rent) question — compute Adjusted EBITDAR across a multi-year window by adding rent / operating-lease obligations to Adjusted EBITDA, typically with YoY growth + multi-year CAGR. Common: casino-tenant operators (CZR/PENN/MGM/BYD) where the operator pays master-lease rent to REIT lessors (GLPI/VICI).
applies_to: [sector_analyst]
source_lines: 1119-1130
---

**Dedicated tool: `compute_lease_adjusted_ebitdar`. Call ONCE.**

```
compute_lease_adjusted_ebitdar(ticker, fy_start, fy_end)
```

Tool pulls reported Adj EBITDA via XBRL + operating-lease cost per
FY + computes per-year YoY growth + end-to-end CAGR. Quote
`answer_summary_block` verbatim.

Do NOT compute EBITDAR manually year-by-year — the tool handles the
multi-year extraction and the math in a single call.
