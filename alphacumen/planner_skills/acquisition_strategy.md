---
id: acquisition_strategy
when: Query asks about an issuer's acquisitions over N years / acquisition strategy / M&A activity since [date]: search multiple 8-K items + 10-K notes; include both motivation and the deals.
applies_to: [sector_analyst]
source_lines: 646-683 724-751
---

- **Acquisition strategy / M&A activity over N years — search
  multiple SEC disclosure surfaces, NOT just Item 2.01.** When
  the question asks about an issuer's "acquisitions over the past
  N years" / "acquisition strategy" / "M&A activity since [date]",
  the canonical disclosure set spans several 8-K items AND the
  10-K Notes — never just Item 2.01 (Completion of Acquisition).
  Smaller deals (sub-materiality threshold for Item 2.01) are
  routinely disclosed via:
  - **Item 8.01 (Other Events)** — common for "announced" deals
    that haven't closed
  - **Item 7.01 (Reg FD Disclosure)** — used for the deal-
    announcement press release
  - **Item 1.01 (Entry into Material Definitive Agreement)** —
    when the deal is large enough but not yet closed
  - **Item 5.02 (Departures of Officers)** — when the acquired
    company's founders join the acquirer's leadership
  - **10-K Acquisitions / Business Combinations Note** (typically
    Note 4 or Note 5) — issuer enumerates EVERY material
    acquisition that closed during the FY, with deal value and
    contingent consideration. THIS is the canonical aggregate
    list.
  - **10-K MD&A "Recent Developments" section** — narrative
    summary of acquisitions completed during the period.

  Dispatch `sector_analyst` with:
  *"Apply Hard rule for acquisition-strategy questions. Pull the
  issuer's FY 10-K via `bm25_sec(form_type:'10-K', ticker:<TICKER>,
  event_date_gte:<period_start>, k:3)` AND query
  `extract_filing_tables(ref=<10-K ref>, item='8',
  table_keyword='Business Combinations')` (try also
  `table_keyword='Acquisitions'` / `'Recent Developments'`)
  to surface the Acquisitions Note. ALSO fan
  `bm25_sec(form_type:'8-K', ticker:<TICKER>,
  filed_at_gte:<period_start>)` and read multiple Item types
  (1.01, 2.01, 5.02, 7.01, 8.01). Do NOT filter to Item 2.01
  alone — smaller deals skip 2.01. Enumerate EVERY acquisition
  the issuer disclosed during the window, with deal value and
  close date."*

- **Acquisition strategy = motivation + deals.** When the question
  asks about a company's acquisition strategy, the answer must
  include BOTH the strategic motivation (what problem or revenue
  decline drove the acquisitions) AND the specific deals (names,
  amounts, dates). A list of acquisitions without the business
  context that explains WHY the company pursued them is incomplete.
  Instruct the specialist to look for revenue headwinds, segment
  declines, or strategic pivots discussed in MD&A alongside the
  acquisition footnotes. For the final answer:
  1. Use the EXACT dollar amounts from the filing — do NOT round
     to the nearest hundred million. Quote the exact figure the
     issuer disclosed (a deal recorded as $399M differs from
     $400M).
  2. List EVERY acquisition in the period — search the notes to
     financial statements in BOTH the 10-K AND quarterly 10-Qs,
     since smaller deals are often disclosed only in the quarter
     they closed, not the annual filing.
  3. When characterizing revenue trends as strategic context,
     state the overall structural direction without a granular
     year-by-year revenue table. A table showing partial recovery
     in later years contradicts a "declined" characterization and
     reads as internal inconsistency. The strategic context is
     the multi-year trend that motivated the pivot, not the
     granular recovery trajectory.
  4. Keep the answer concise — focus on deals + motivation +
     alignment. Do NOT include quarterly breakdowns, segment-mix
     percentages, or management quotes unless they directly
     answer the question.
