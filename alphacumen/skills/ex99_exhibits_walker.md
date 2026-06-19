---
id: ex99_exhibits_walker
when: You need substantive 8-K content past the cover sheet (full press release, multi-page shareholder letter, guidance breakdowns, segment reconciliations) — content lives in ex99*.htm exhibits, not in the 8-K cover.
applies_to: [sector_analyst]
source_lines: 330-363
---

**8-K earnings exhibits live in `ex99*.htm`, not the 8-K cover sheet
— use `extract_filing_tables` to reach them.**

Many issuers put substantive earnings content — press release, KPI
tables, quarterly guidance breakdowns, segment reconciliations —
inside Exhibit 99.1 of the 8-K, not Item 2.02 of the cover. Two
consequences:

1. `bm25_sec` snippets and `get_full_text` on an 8-K typically return
   the COVER sheet only ("furnished as Exhibit 99.1"), or the first
   ~30 KB of the exhibit when the chunker happens to merge them.
   Anything past ~30 KB of the press release (later pages of the
   shareholder letter — full-year guidance breakdowns, multi-quarter
   guidance tables, longer reconciliations) is NOT in the index.

2. `extract_filing_tables(ref=<8-K ref>, table_keyword=<keyword>,
   item="2-2")` automatically walks the filing directory and pulls
   every `ex99*.htm` exhibit when sec-api returns a cover-stub. So
   for **any** "X published Q[N] guidance for end-to-end volume /
   payment volume / GMV / revenue / margin / EBITDA" question, the
   correct first move is `extract_filing_tables` on the 8-K
   announcing that quarter's earnings, with `table_keyword` set to
   the metric name (e.g. `"End-to-End Payment Volume"`,
   `"Q3 2024"`, `"Quarterly Guidance"`).

   Pattern: shareholder-letter exhibits frequently include a
   "Breaking Down Our [YYYY] Guidance by Quarter" or "Quarterly
   Outlook" table several pages into `ex99*.htm`. The chunker
   typically truncates at the first 30 KB, so any guidance table
   past that cutoff is NOT in indexed body chunks but IS reachable
   via the exhibits walker.

When the result includes `"source": "edgar_exhibits"` you're
reading the directory-walked exhibits and can trust the figures
(same SEC filing, just bypassed the chunker's 30 KB truncation).
