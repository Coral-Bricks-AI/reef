---
id: dedicated_specialist_dispatch
when: Query matches a known pattern (multi-issuer ratios, M&A deal terms, DCF, LBO, payout-ratio peers, monthly seasonality, KPI sub-period trend, FCF margin trend, multi-issuer capex guidance, restructuring flow-through, etc.) where the right specialist owns a dedicated playbook end-to-end.
applies_to: [planner]
---

**Your job is to pick the right specialist and describe intent.** Do
NOT name tools, argument shapes, or skill ids inside the dispatch
instruction. Each specialist owns its own router:

- `sector_analyst` carries the full sector skill index in its seed
  and Hard rule 0 to scan the question against it. Once it sees a
  plain-English intent ("compute leverage ratios for <TICKER>
  FY<YYYY> asof <YYYY-MM-DD>"), it identifies the matching skill
  and calls its tool.
- `stock_analyst` likewise routes by intent for price-return,
  N-day-reaction, and options questions.

## Dispatch table

| Trigger | Specialist | Intent template |
|---|---|---|
| Compare leverage / debt ratios / EV/EBITDA / EBITDA margin / inventory efficiency (DIO) / price-to-book across ≥2 US-listed tickers | one `sector_analyst` **per ticker**, parallel in round 1 | "<TICKER>: compute <ratio list> for FY<YYYY>, asof <YYYY-MM-DD>." |
| EPS guidance (constant-currency growth %) for consumer-staples / foreign issuer — implied $ range and/or beat / miss | `sector_analyst` | "Resolve <TICKER>'s FY<YYYY> EPS guidance to a dollar range; compare to reported actuals if asked." |
| Net-income impact if <issuer>'s debt were refinanced at Y% higher / lower | `sector_analyst` | "What would <TICKER>'s FY<YYYY> net income be if rates moved by <bps> bps?" |
| Dividend payout ratio of a consumer-staples issuer vs peers | `sector_analyst` | "How does <TICKER>'s FY<YYYY> payout ratio compare to its Consumer Staples peers?" |
| Single-issuer competitive position change since <year> | `sector_analyst` | "How has <TICKER>'s competitive position changed from FY<Y1> to FY<Y2>?" |
| Single non-financial KPI trajectory over ≥3 FYs (ARPU / MAU / paid memberships / room-nights / deliveries / units shipped / etc.) | `sector_analyst` | "Trace <TICKER>'s <KPI name> from FY<Y1> to FY<Y2>." |
| Multi-year FCF margin trend on one issuer | `sector_analyst` | "Show <TICKER>'s FCF margin from FY<Y1> to FY<Y2>." |
| Compare forward-FY capex guidance across ≥2 tickers | `sector_analyst` | "Rank <T1>, <T2>, … by FY<YYYY> capex guidance." |
| Deal terms (share-exchange ratio / per-share offer / equity value / enterprise value) of an announced acquisition | `sector_analyst` | "What are the deal terms for the <ACQUIRER>–<TARGET> acquisition?" |
| Full roster of operating / performance metrics for one issuer's FY | `sector_analyst` | "List every operating KPI <TICKER> reported for FY<YYYY>." |
| Marketplace YoY revenue split between take-rate change and GMV growth | `sector_analyst` | "Attribute <TICKER>'s FY<YYYY> revenue growth between take-rate and GMV." |
| Footnote / note line change between two FYs on one issuer (PPA, restructuring liability roll-forward, goodwill delta, etc.) | `sector_analyst` | "How did <TICKER>'s <note name> change from FY<Y1> to FY<Y2>?" |
| DCF projection with explicit assumption set on one US-listed issuer | `sector_analyst` | "Run a <horizon>-year DCF on <TICKER> with these assumptions: <state them>." |
| Simplified LBO / take-private model on one US-listed target | `sector_analyst` | "Build a simplified LBO for <TICKER> with these assumptions: <state them>." |
| "Monthly [Month] seasonality" question for a foreign-private issuer publishing monthly 6-K revenue | `sector_analyst` | "Apply the monthly-seasonality forecast for <TICKER> targeting <Month> <Year>." |
| Restructuring plan flow-through analysis on one issuer | `sector_analyst` | "Trace <TICKER>'s <Plan Name> through FY<YYYY> financials." |
| Multi-ticker price-return / N-day reaction (with optional rank-correlation against revenue or another fundamental) | `stock_analyst` (price leg) + `sector_analyst` (fundamental leg, if rank-correlation) | "Compute <window> price returns for <T1>, <T2>, …. <Plus FY revenue growth if ranking>." |
| Ecosystem / landscape / partners / startups around <X> | `vc_analyst` (primary) + `risk_analyst` (tail risks) | "Map the ecosystem around <X>: key startups / partners / customers / competitors, relationship type, recent material developments." |

Use the specialist column AS WRITTEN — if it says "one per ticker," fan
out per ticker; if it says "stock_analyst + sector_analyst," dispatch
both in parallel. The intent template is the shape your instruction
should take; fill in the entity / period / metric placeholders and
keep the wording natural-language. The specialist takes it from there.

## Synthesis discipline — peer-comparison questions

For any "Compare <metrics> across ≥2 tickers" / "peer-set leverage" /
"which company is most leveraged / cheapest / most efficient" trigger
where the synthesizer receives N parallel per-ticker results, the
final answer MUST include a **per-metric peer average** row alongside
the per-ticker values, computed as the unweighted arithmetic mean of
the N ticker values for each metric. This applies even when the
question doesn't explicitly ask for averages — rubric atoms grading
multi-issuer comparisons commonly cover both individual values AND
the cross-ticker mean, and the synth should make the comparison
basis explicit. If one ticker's metric is unavailable (missing data
for that issuer), report the peer average over the available subset
and explicitly state which ticker is excluded so the consumer can
recompute if they have the missing input. Generic across any
peer-comparison question shape; no per-question hardcoding.
