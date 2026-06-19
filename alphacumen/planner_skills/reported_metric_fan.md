---
id: reported_metric_fan
when: Query asks for a specific reported figure for a named company (capex, revenue, EPS, headcount, market share, deal value, KPI) that may live in a transcript/press-wire as well as a filing.
applies_to: [sector_analyst, news_quant_analyst]
source_lines: 447-475
---

- **Reported-metric questions → fan SEC + news in parallel.**
  When the question asks for a specific reported figure for a
  named company (capex, revenue, EPS, headcount, market share, deal
  value, KPI), invoke `sector_analyst` (primary, for the SEC-filed
  value) AND `news_quant_analyst` (parallel fallback, for
  analyst-cited / press-wire / earnings-transcript figures) on the
  same round. Some issuers publish the number only in transcripts
  or via implicit framing (run-rate proxies, percentage-growth
  references) that an 8-K extractor reads as "not disclosed".

  IMPORTANT: route the figure-extraction half to
  `news_quant_analyst`, **NOT** to `vc_analyst`. `vc_analyst`'s
  narrative persona makes it prone to hallucinate figures from
  training memory: confident but fabricated Reuters/Bloomberg
  citations with the wrong number are a recurring failure mode.
  `news_quant_analyst` has the same news/scraped-articles tools
  but a narrow figure-extraction-only prompt and a runtime
  must-retrieve gate that physically prevents short-circuit
  answers. If the question also has a competitive-landscape /
  positioning angle worth covering, you MAY ALSO invoke
  `vc_analyst` in the same round — just keep its instruction
  scoped to "name the relevant competitors and frame the
  market dynamic", not "find the dollar figure".

  Synthesis precedence: prefer the SEC-filed figure when available;
  cross-check against news for confirmation or divergence; fall
  back to news only when SEC reports "not disclosed". Never
  average across sources — pick a source-of-truth and cite the
  other as cross-check.
