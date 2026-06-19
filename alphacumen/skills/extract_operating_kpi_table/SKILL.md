---
id: extract_operating_kpi_table
when: Full-roster operating-KPI enumeration — issuer's *complete* set of operating / performance metrics for a FY (not headline, not a specific KPI — the canonical full roster). Common phrasings: "list", "all", "every" KPI paired with its FY value.
applies_to: [sector_analyst]
source_lines: 1001-1012
---

**Dedicated tool: `extract_operating_kpi_table`. Call ONCE.**

```
extract_operating_kpi_table(ticker=<TICKER>, fy=<YYYY>)
```

Tool finds the canonical Operating-Statistics / Key-Operating-Metrics
/ Selected-Operating-Data table in the 10-K MD&A (tries seven keyword
variants) and dumps every row as a canonical bullet list. Quote
`answer_summary_block` verbatim — **do not trim to headline
metrics**; the canonical answer enumerates every disclosed KPI.
