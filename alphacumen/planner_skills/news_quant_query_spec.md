---
id: news_quant_query_spec
when: You are about to dispatch `news_quant_analyst`: build the instruction as a query spec with metric-noun + disambiguating entities + calendar window anchor tokens.
applies_to: [news_quant_analyst]
source_lines: 476-496
---

- **`news_quant_analyst` instructions are query specs, not
  briefs.** The specialist's first BM25 query lifts noun phrases
  straight from your instruction. Generic in, generic out — the
  hits will be about whichever event for the named entity
  dominates the corpus, not necessarily the one the user asked
  about. Every dispatch MUST contain three anchor tokens:
  1. The **metric noun** in news-outlet language ("deal value",
     "total consideration", "capex guidance"), not filler ("the
     figure", "the amount").
  2. **Disambiguating entities** — for M&A, the acquired
     assets / divisions / subsidiaries, not just parent tickers.
     For earnings, the issuer + fiscal period. When the named
     parties have multiple events of similar shape, the asset
     list / sub-entity / event-id is what makes the query land
     on the right one.
  3. An **explicit calendar window** (announcement + close
     months for M&A; issue-date filing window for guidance).
  Self-check before dispatching: would these noun phrases pulled
  into BM25 retrieve THIS event, or could they collide with a
  different event for the same parties? If the latter, add the
  disambiguating tokens.
