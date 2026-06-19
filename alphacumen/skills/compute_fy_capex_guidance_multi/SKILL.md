---
id: compute_fy_capex_guidance_multi
when: Question compares forward-FY capital-spending plans across two or more US-listed issuers (ranking, "highest / lowest spender", absolute-value comparison).
applies_to: [sector_analyst]
source_lines: 976-987
---

**Dedicated tool: `compute_fy_capex_guidance_multi`. Call ONCE.**

```
compute_fy_capex_guidance_multi(tickers=[<T1>,<T2>,…], fy=<YYYY>)
```

Tool fans across all named tickers, finds each issuer's Q4 FY-1
earnings 8-K (where FY guidance is issued), extracts the capex
dollar figure via regex, and returns a ranked table. Quote
`answer_summary_block` verbatim.

When a per-ticker entry returns `source_text` instead of
`fy_capex_label`, the regex missed — `get_full_text` on its
`source_ref` and quote the capex paragraph directly.
