---
id: news_priming_narrative_qualitative
when: Query asks about narrative / qualitative disclosure framed in investor vocabulary (tailwind, headwind, pace of, acceleration, category creation, durable advantage, positioning, narrative, moat, demand driver) — words 10-K / 10-Q text does not use lexically.
applies_to: [planner]
---

- **MANDATORY two-round dispatch when this skill loads.** Skipping
  vc_analyst and going straight to sector_analyst is the antipattern
  this skill exists to prevent. Investor vocabulary
  (tailwind / headwind / acceleration / etc.) does NOT lexically
  match SEC filing text, so sector_analyst alone will under-retrieve
  issuer-specific product names, feature codenames, and IR-side
  framings that the rubric expects (e.g. branded AI capabilities,
  new platform module names, demand-driver attribution phrases).

- **Round 1 (always, NEVER skip): `dispatch_vc_analyst`.** Goal is
  vocabulary translation, NOT analysis. Instruct vc_analyst to
  return a structured term list pulled from news / IR coverage:
  issuer-specific product / feature names, management-quoted phrases,
  metric labels, and the noun phrases the issuer's own IR / earnings
  decks use to translate the investor framing.

- **Round 2: `dispatch_sector_analyst`** with SEC-shaped queries
  built FROM round 1's term list — feed each surfaced noun phrase
  as its own short `bm25_sec` seed, not the question's investor
  vocabulary. One short query per term beats one long query per
  framing.

- **Synthesis discipline:** round 1's vocabulary is query-seed
  only. The final answer must quote filing-grounded language from
  the round 2 retrieval, never the news-side abstraction. If a
  product / capability name appeared in round 1 but did NOT verify
  in round 2 SEC retrieval, drop it from the synthesis (don't let
  unverified news framings leak into the answer).
