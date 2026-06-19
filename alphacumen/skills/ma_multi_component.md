---
id: ma_multi_component
when: M&A question asking for multiple labeled sub-components (i / ii / iii / iv) — premium paid, pro forma segment mix impact, synergies as % of target revenue AND % of target SG&A, strategic / financial acquisition criteria.
applies_to: [sector_analyst]
source_lines: 1206-1242
---

**Fetch BOTH the announcement 8-K Exhibit 99 AND the target's
most-recent annual 10-K, then compute each component separately.**

## Workflow

1. **Announcement 8-K + Ex 99.1 deck.** Find the 8-K via
   `find_quarterly_earnings_8ks(target_ticker, fy)` or
   `bm25_sec(query="<target> acquisition <acquirer>",
   form_type:8-K)`. Pull Exhibit 99.1 via
   `extract_filing_tables(ref=<8-K acc:99.1>, limit=10)` — this is
   where the **pro forma segment mix %** and the **announced
   run-rate synergies $** are disclosed.

   Quote the exact pro forma percentage and synergies dollar amount
   verbatim — do NOT paraphrase to *"significantly expands
   exposure"* or *"approximately X%"*. The rubric grades the
   literal percentage/dollar figure.

   **Slide-deck fallback.** Acquisition-announcement Ex 99.1 is
   often a PowerPoint-style HTML deck whose slides render as nested
   `<div>` / image elements that `extract_filing_tables` cannot
   parse (returns `tables_in_item: 0` for that ref). When the call
   comes back empty, pivot to a text-mode retrieval chain:
     - `get_full_text(ref=<8-K acc:99.1>, max_chars=40000)` — pulls
       the deck body via the existing EDGAR direct-fetch fallback
       when the indexed body is a short stub. Slide titles and
       speaker-note text usually contain the literal percentage.
     - `bm25_scraped_articles(query="<acquirer> <target> pro forma
       segment mix percent non-oil-and-gas")` — press coverage of
       the announcement deck quotes the exact pro forma percentage
       verbatim from management's investor presentation. Same
       phrase for any segment-mix question; the underlying
       sub-industry words don't matter.
     - `vector_scraped_articles(query="<acquirer> acquires <target>
       segment revenue mix pro forma")` — ANN retrieval picks up
       paraphrased coverage of the same number that BM25 misses.
   Scan the returned text for an explicit `XX%` mention adjacent
   to a segment / business-line / end-market label. Quote it
   verbatim. Generic across any M&A deck question; the rubric
   grades the literal percentage regardless of retrieval source
   path.

2. **Target's most-recent 10-K** for FY-prior denominators. Pull
   `get_xbrl_facts(ref=<target FY 10-K>, concept_pattern="Revenues")`
   for the revenue base AND
   `concept_pattern="SellingGeneralAndAdministrativeExpense"` for the
   SG&A base. If `SellingGeneralAndAdministrativeExpense` is
   absent, try
   `concept_pattern="SellingGeneralAndAdministrativeExpenses"`,
   `"GeneralAndAdministrativeExpense"`, then fall back to
   `extract_filing_tables(ref=<target FY 10-K>,
   table_keyword="Selling, general", item="8")` for the
   operating-expense breakdown.

3. **Compute each ratio.** Synergies as % of revenue =
   `announced_synergies / target_FY_revenue × 100%`. Synergies as %
   of SG&A = `announced_synergies / target_FY_SGandA × 100%`. State
   BOTH ratios with the underlying $ inputs — do NOT report "SG&A
   data unavailable" without first running the
   `extract_filing_tables` fallback.

4. **Strategic / financial acquisition criteria summary.** Pull
   the acquirer's most recent 10-K MD&A "strategy" section AND the
   announcement 8-K's deal-rationale slide for the explicit
   strategic-criteria framing — typically a bullet list naming
   end-markets, R&D synergies, regulatory tailwinds, etc.

   **Cover the standard M&A rationale dimensions.** When summarizing
   the acquirer's strategic + financial rationale, surface whatever
   themes the announcement materials actually disclose along the
   standard finance-textbook M&A-criteria axes: end-market /
   customer exposure, revenue-stream characteristics, synergy
   levers, financial impact (return on investment, free cash flow,
   margin trajectory), and strategic / technology fit. Quote the
   issuer's own framing for each axis verbatim where the materials
   make it explicit; do NOT omit an axis the deck addressed just
   because it was qualitative rather than quantified.
