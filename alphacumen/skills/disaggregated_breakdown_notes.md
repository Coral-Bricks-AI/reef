---
id: disaggregated_breakdown_notes
when: "Which X had the biggest/smallest Y" — revenue / cost / asset breakdown by geography, segment, product line, or customer.
applies_to: [sector_analyst]
source_lines: 104-115
---

**Query the Notes to Financial Statements first, not MD&A.**

## The trap to avoid

MD&A (`part1item2`) discusses revenue/cost/asset mix in narrative
form, typically rolled up one level for readability ("U.S. vs
International," "Products vs Services"). The disaggregated tables
the question actually needs — with the issuer's most granular
categories intact — are filed in the Notes to Financial Statements,
not the MD&A. Pulling only from MD&A silently loses the sub-rows you
need to rank.

## Workflow

1. `bm25_sec` with `item="part1item1"` (10-Q) or the equivalent
   statements-and-notes item in a 10-K. Query text: the metric's own
   vocabulary — `"disaggregation of revenue"`, `"geographic
   information"`, `"segment information"`. Keep it 2-3 nouns; do not
   pad with ticker + synonyms (that drops the table chunk in BM25
   rank).
2. `extract_filing_tables(ref=..., table_keyword="Disaggregation of
   Revenue"` / `"Geographic Information"` / `"Segment
   Information")`.
3. Apply Hard rule 6.5 — return the table's own row labels and
   numbers; the synthesizer decides which row answers the question.

## Common failure modes

- ❌ Reading MD&A's rolled-up "U.S. vs International" line and
  presenting it as the geography breakdown. The Notes carry the
  per-country split.
- ❌ Skipping the table extraction and quoting numbers off a
  narrative paragraph that mentions them. Narrative paraphrases drop
  rows.
- ❌ Concluding "not disclosed" after one `part1item2` query. The
  disclosure exists; it's in `part1item1`.
