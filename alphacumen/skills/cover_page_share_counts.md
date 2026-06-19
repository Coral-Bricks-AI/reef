---
id: cover_page_share_counts
when: Shares outstanding by class — "Class A vs Class B," "Class H shares," per-class share count, or any question pinned to `dei:EntityCommonStockSharesOutstanding`.
applies_to: [sector_analyst]
source_lines: 231-244
---

**Call `get_cover_page_share_counts(ref)` — do NOT use
`get_xbrl_facts` or `extract_filing_tables`.**

## The trap to avoid

Cover-page disclosures are stripped from `sec_filings_chunked`
during ingestion (everything before the first "Item X." header is
removed). So `bm25_sec` snippets and `get_full_text` chunks will
never carry the cover-page share-count table.

The consolidated stockholders' equity table is NOT a substitute.
Cover-page share counts and equity-rollforward share counts can
legitimately disagree — Airbnb's 9,200,000 Class H shares are
outstanding on the cover page but net to zero in the equity
rollforward because the consolidated Host Endowment Fund holds them.
The cover-page number is the canonical answer to "shares
outstanding."

`get_xbrl_facts` and `extract_filing_tables` lose the
axis-member dimension and force you to reassemble per-class numbers
by hand — wrong tool.

## Workflow

1. `bm25_sec` for the issuer's most-recent 10-K or 10-Q (no special
   query — any chunk's `ref` will do; the tool reaches the
   cover-page disclosure independently).
2. `get_cover_page_share_counts(ref)` — returns the SEC-mandated
   `dei:EntityCommonStockSharesOutstanding` disclosure grouped by
   share class.
3. Quote the returned `shares` value verbatim per class.

## Common failure modes

- ❌ Quoting numbers from the consolidated stockholders' equity
  table. They can disagree with the cover page.
- ❌ Dividing total shares by class proportions. Per-class numbers
  are filed directly; no derivation needed.
- ❌ Refusing on "cover-page data not in index." That's Hard rule 8
  — the dedicated tool exists for exactly this case.
