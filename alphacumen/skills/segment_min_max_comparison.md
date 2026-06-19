---
id: segment_min_max_comparison
when: "Which segment has the highest/lowest X" / "rank the segments by Y" — min/max or ordering across an issuer's reportable segments. Also applies to row-by-row min/max over 8-K Guidance / Outlook tables.
applies_to: [sector_analyst]
source_lines: 74-91
---

**Pull the structured segment-results table; take the literal
min/max over EVERY row.**

## The trap to avoid

Multi-column segment-results tables are exactly the structure that
truncates badly in BM25 snippets — the rightmost columns get
dropped, and the snippet's segment list looks complete but isn't.
Enumerating from a snippet, from priors, or by re-categorizing rows
("operating vs non-operating," "core vs reconciling") silently
filters before the comparison and produces the wrong winner.

The issuer's published table IS the universe of reportable
segments. A question asking for the lowest/highest is asking about
that universe.

## Workflow

1. `extract_filing_tables(ref=..., table_keyword="Segment results"`
   or `"Business segments")` on the relevant 10-K/10-Q. Call this
   BEFORE composing any answer.
2. Every column header in the structured table is a reportable
   segment, including:
   - "Corporate" / "Treasury" / "Other" reconciling columns
   - Columns whose net revenue is negative (shown as `$(N)`)
3. If your final enumeration has fewer rows than the table has
   segment columns (excluding the "Total" column), you have missed
   a segment — re-read the table.
4. Take the literal min/max over EVERY row. Do NOT filter to "core"
   or "operating" segments first.

## 8-K Guidance / Outlook table extension

This rule applies in full to "Guidance" / "Outlook" tables in 8-K
shareholder letters: every row is a guided metric, even when
individual lines look like operating items (SBC, CapEx,
weighted-average shares). Take the min/max across every row of the
guidance table.

## Common failure modes

- ❌ Enumerating segments from a BM25 snippet. Rightmost columns are
  dropped.
- ❌ Excluding "Corporate" or "Other" as not a "real" segment. The
  issuer's column IS the universe.
- ❌ Treating a negative-revenue segment as a data error. `$(N)` is a
  reportable value; include it in the comparison.
- ❌ Re-categorizing rows in an 8-K guidance table before taking
  min/max. The whole table is the comparison set.
