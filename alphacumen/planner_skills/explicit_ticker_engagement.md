---
id: explicit_ticker_engagement
when: Query names one or more explicit US-listed tickers (NYSE:/NASDAQ:/ticker XXX/(XXXX)). Load on round 1 as a precondition — sets the dispatch shape and forbids refusal framings in the final answer, regardless of what specialists return.
applies_to: [sector_analyst, stock_analyst]
source_lines: 90-145
---

- **MANDATORY EXPLICIT-TICKER RESEARCH ENGAGEMENT — never emit empty
  answer when the question names a ticker.** When the user's question
  contains one or more explicit US-listed tickers (e.g. `NYSE:XXX`,
  `NASDAQ:XXX`, `ticker XXX`, or any company name immediately followed
  / preceded by a symbol like `(XXXX)` or `XXX:`), the top-level
  `final_answer` MUST be `answerable: true` with a non-empty
  `answer_summary` that surfaces every numeric, textual, or qualitative
  fact a specialist returned, even if the answer is partial. The
  named ticker is a real SEC filer — its 10-K / 10-Q / 8-K / DEF 14A
  / 424B / 6-K / S-1 / S-3 exists by definition; the data CAN be
  surfaced. Refusal framings as the headline of the answer are
  FORBIDDEN, including (non-exhaustive): *"unable to locate"*, *"data
  unavailable"*, *"no verifiable figures"*, *"insufficient data"*,
  *"no dated evidence"*, *"no SEC filings found"*, *"data gap"*,
  *"cannot be calculated"*, *"cannot be fulfilled at this time"*,
  *"desk was unable"*, *"scraped news corpus contains no"*, *"the
  request cannot be answered"*, or any synonym. Such phrases as the
  primary framing are a synthesis-side defect, not a real refusal.
  Operationally:
  1. If specialists came back thin, **force one re-dispatch round**
     before any refusal is even considered. The re-dispatch MUST:
     (a) route **ONLY to `sector_analyst`** (NEVER `news_quant_analyst`
     or `vc_analyst` for SEC-data questions — if your previous round
     mentioned "scraped news corpus" or routed to news_quant for an
     explicit-ticker SEC question, that was a routing defect; correct
     it now), (b) raise `max_steps` to at least 12 (14 for multi-ticker
     queries), (c) name the explicit SEC form type the data lives in
     (10-K item, 8-K item, 10-Q item, DEF 14A, 424B5, 6-K), and (d)
     for multi-issuer questions, instruct the specialist to fan
     across **every** named ticker at breadth, not deep-dive a subset.
  2. After the re-dispatch round, even if some rubric points remain
     ungrounded, ship `answerable: true` with the data you DID
     gather as the headline, then add a single inline note flagging
     specific gaps (e.g. *"FY2024 10-K not in retrieved corpus; figures
     above are FY2023 actuals"*). Gap disclosure as a footnote is
     fine; gap disclosure as the entire answer is the defect this
     rule prohibits.
  3. This rule has higher precedence than the round-limit hint and
     the converged-without-payload sentinel. If you would otherwise
     converge with `answerable: false` on round N because the
     specialist returned thin, instead converge with
     `answerable: true` + every datum the specialist surfaced + a
     gap note — OR force one more dispatch round if you have not yet
     used the multi-step / form-type / sector_analyst-only escalation
     above.

  Examples of explicit-ticker patterns that trigger this rule:
  *"For NASDAQ:CRWD, in what ways does management…"*; *"NYSE:SUI made
  an announcement…"*; *"Compare the inventory efficiency of NYSE:HD
  and NYSE:LOW…"*; *"For NASDAQ:VSEC, using the midpoint…"*.

  The pre-existing "Specialist policy-refusals do NOT propagate"
  rule (below) is the speculation-screen variant of this principle;
  the explicit-ticker SEC variant above is its mandatory counterpart
  for the research-on-named-filer case.
