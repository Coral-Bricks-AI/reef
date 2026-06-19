---
id: vote_results_8k_edgar
when: Question names a specific 8-K item (most often Item 5.07 vote-results), or `bm25_sec` returned adjacent-but-wrong items (e.g. Item 2.02 earnings 8-K when you needed Item 5.07) for an explicitly-anchored ticker + form_type + date lookup.
applies_to: [sector_analyst]
source_lines: 150-185
---

**Fall back to `find_sec_filing_edgar` BEFORE refusing.**

## The trap to avoid

`sec_filings_chunked` has ingestion gaps. The canonical case: 8-Ks
filed by proxy/transfer agents under a non-issuer CIK — Item 5.07
vote-results 8-Ks are routinely missing from the chunked index even
though the filing exists on EDGAR. A `bm25_sec` query with the right
ticker, form_type, and date range will return clearly-adjacent items
(Item 2.02 earnings 8-K, Item 9.01 exhibits) but never the targeted
filing. Refusing on "filing not retrievable" surfaces the index gap
as a data gap — wrong call.

## Workflow

1. Trigger when **both** hold:
   - The question names a specific filing (form_type + filing
     window + optional 8-K item code).
   - `bm25_sec` with the right ticker, form_type, and date range
     returned hits that are clearly adjacent but not the targeted
     item.
2. Call `find_sec_filing_edgar` directly:

   ```
   find_sec_filing_edgar(
       ticker="FL",
       form_type="8-K",
       item_section="5.07",
       filed_at_gte="2022-05-01",
       filed_at_lte="2022-06-30",
       k=3,
   )
   ```

   The tool reaches EDGAR directly, resolves the ticker via
   `company_tickers.json` (with an EDGAR atom fallback for delisted
   issuers), filters by `items_desc` substring, fetches each
   filing's primary HTML doc, and returns BS4-stripped text per hit.
3. If you need a specific table (vote tabulation, executive
   compensation, etc.), follow up with `extract_filing_tables` on
   the returned accession.

## Common failure modes

- ❌ Refusing with "filing not retrievable" after one `bm25_sec`
  miss. Your job is to exhaust the available retrieval surface, not
  to surface index gaps as data gaps.
- ❌ Treating the Item 2.02 earnings 8-K as a substitute when Item
  5.07 was asked for. Adjacent ≠ correct.
- ❌ Reissuing the same `bm25_sec` query 3+ times hoping the chunked
  index changes. It won't.
