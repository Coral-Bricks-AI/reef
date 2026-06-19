---
id: extract_ebitda_reconciliation_multi_year
when: Multi-year Adjusted EBITDA reconciliation question — reconstruct the GAAP-to-Adjusted-EBITDA bridge for each FY-A through FY-B + identify per-category add-backs (integration / transaction / restructuring or other named categories) + compute per-year totals + express as % of Adjusted EBITDA.
applies_to: [sector_analyst]
source_lines: 1132-1145
---

**Dedicated tool: `extract_ebitda_reconciliation_multi_year`. Call
ONCE.**

```
extract_ebitda_reconciliation_multi_year(
    ticker,
    fy_start,
    fy_end,
    add_back_categories,
)
```

Tool pulls each FY's MD&A non-GAAP reconciliation table via
`extract_filing_tables(table_keyword="Adjusted EBITDA")`, parses
standard add-back sub-categories, computes totals + %.

Pass `add_back_categories=["integration","transaction",
"restructuring"]` (default), or override when the issuer uses
different category labels.

Quote `answer_summary_block` verbatim.
