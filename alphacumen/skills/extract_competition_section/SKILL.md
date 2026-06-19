---
id: extract_competition_section
when: Question asks who the issuer competes with, how the issuer characterizes its competitive landscape, or which forms of entertainment / substitutes the issuer identifies as competitors. Multi-issuer competitive-disclosure comparisons ("which company provides more detailed disclosure on their competitive landscape") fan out to this skill once per ticker.
applies_to: [sector_analyst]
source_lines: 0
---

**Dedicated tool: `extract_competition_section(ticker, fy)`. Call ONCE per ticker.**

Pulls the verbatim "Competition" subsection from the issuer's Item 1 Business in the latest FY 10-K (or 20-F for foreign filers). Returns the prose text + the original 10-K ref. Generic across any US-listed or FPI issuer that follows the standard SEC Item 1 layout.

## Why this exists

The "Competition" subsection lives in Item 1 Business prose, between the Products / Services description and the next subsection (typically Research and Development, Sales and Distribution, Manufacturing, or Government Regulation). Item-level chunking in the `sec_filings_chunked` index sometimes splits this subsection across chunk boundaries; BM25 on the term "Competition" routinely surfaces Item 1A Risk Factors snippets or supplemental references instead of the canonical Item 1 subsection. Without a dedicated walker, the model quotes a one-sentence excerpt from the Risk Factors instead of the full Competition disclosure -- losing every atom that grades named competitors or specific competitor-language phrasing.

## Workflow

1. Call this skill ONCE for each ticker the question names (e.g. `extract_competition_section(ticker=<TickerA>, fy=<FY>)` then `extract_competition_section(ticker=<TickerB>, fy=<FY>)` for a two-issuer comparison).
2. Quote `answer_summary_block` verbatim in the final answer's per-issuer section. The block contains the full Item 1 Competition prose with the original 10-K ref attached.
3. The answer's narrative atoms grade against the issuer's actual language; quoting the section verbatim preserves it.

## When this skill applies

- Multi-issuer competitive-landscape comparisons ("Who do A and B see as competitors?", "Which company provides more detailed disclosure?")
- Single-issuer competitive-disclosure questions where the rubric grades the issuer's specific language
- Adjacent: questions about substitute products / non-direct competitors (the Competition subsection often names them — "other forms of entertainment such as movies, television, social media")

## Common failure modes (this skill prevents)

- ❌ Quoting Item 1A Risk Factors instead of Item 1 Competition. Risk Factors restates the existence of competition; Item 1 names it.
- ❌ BM25 returning a one-sentence snippet from a generic "intensely competitive" line, missing the full subsection prose with named competitors.
- ❌ Asymmetric extraction in multi-issuer comparisons -- model pulls the full prose for one ticker but a thin snippet for the other, then concludes the second has "less detailed disclosure" purely from the model's own retrieval gap.

Generic across any issuer with a standard SEC Item 1 layout; no rubric-keyed competitor names baked in.
