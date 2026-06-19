---
id: extract_ma_deal_terms
when: M&A transaction-pricing question — deal terms (share-exchange ratio, per-share price, equity value, enterprise value, or any combination) of a completed or in-progress acquisition of a US-listed target.
applies_to: [sector_analyst]
source_lines: 989-999
---

**Dedicated tool: `extract_ma_deal_terms`. Call ONCE.**

```
extract_ma_deal_terms(target_ticker=<TARGET>)
```

Tool finds the merger 8-K (Item 1.01 / 8.01 for the target ticker)
and regex-extracts share ratio, price per share, equity value, and
enterprise value. Quote `answer_summary_block` verbatim.

When `extracted` is empty, `get_full_text` on each entry in
`filings_found` (most recent first) and quote the deal-terms
paragraph manually.
