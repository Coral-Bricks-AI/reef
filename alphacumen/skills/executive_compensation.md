---
id: executive_compensation
when: Director / executive compensation, board nominees / elections, or proxy-voting question — anything sourced from a DEF 14A proxy filing.
applies_to: [sector_analyst]
source_lines: 1564-1571
---

**Search DEF 14A, not 10-K.** Director pay, executive pay, board
nominees, and proxy-voting questions live in the DEF 14A — this data
is almost never in the 10-K. Use `form_type: "DEF 14A"` in
`bm25_sec` whether or not the GP named the form type.

**Compensation totals — `extract_filing_tables` with
`table_keyword="Director Compensation"` (or `"Summary Compensation"`
for NEOs), then `run_python` to sum.** The compensation table has
per-person dollar amounts — do NOT estimate from the compensation
structure description.
