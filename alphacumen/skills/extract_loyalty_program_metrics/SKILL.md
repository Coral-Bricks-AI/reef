---
id: extract_loyalty_program_metrics
when: Question asks what share of room nights / check-ins / stays / bookings an issuer's loyalty program members account for, OR the funding mechanism for a hospitality / hotel / travel issuer's loyalty program (Bonvoy, Rewards, World of Hyatt, IHG One, etc.). Multi-issuer loyalty-disclosure comparisons ("which company is better positioned to translate loyalty strength into durable economic advantage") fan out to this skill once per ticker.
applies_to: [sector_analyst]
source_lines: 0
---

**Dedicated tool: `extract_loyalty_program_metrics(ticker, fy)`. Call ONCE per ticker.**

Pulls the issuer's loyalty-program member-share metrics (US / global / regional percentages of room nights / check-ins / stays) from the latest FY 10-K body. Returns each percentage with its geography label + metric label + the verbatim surrounding sentence. Generic across US-listed hospitality issuers that disclose loyalty-program penetration in Item 1 Business or MD&A.

## Why this exists

Hospitality issuers disclose loyalty-program member share inconsistently. Common shapes (synthetic examples):

- "approximately X percent of our U.S. hotel room nights and approximately Y percent of our global hotel room nights were booked by Loyalty Program members" (room-nights layout)
- "Our members accounted for over X% of check-ins at our hotels globally and over Y% in the United States" (check-ins layout)
- "approximately Z% of room revenue ... <Brand> members" (room-revenue layout)

BM25 on terms like "loyalty room nights percent" returns retrieval-noise hits (loyalty-revenue notes, points-liability footnotes, fair-value disclosures) far more often than the canonical penetration paragraph. Without a dedicated walker, the model either misses the disclosure entirely or quotes a non-canonical sentence.

## Workflow

1. Call this skill ONCE for each ticker the question names (e.g. `extract_loyalty_program_metrics(ticker=<TickerA>, fy=<FY>)` then `extract_loyalty_program_metrics(ticker=<TickerB>, fy=<FY>)` for a two-issuer comparison).
2. Quote `answer_summary_block` verbatim. Each row gives `geography | metric | percentage | source sentence` -- directly answers "what share of room nights / check-ins are attributable to loyalty members".
3. For "funding mechanism" sub-questions, supplement with `get_full_text(ref=<10-K>)` filtered by "Loyalty Program" or the brand name (Bonvoy / Rewards / Hyatt / IHG One).

## When this skill applies

- Multi-issuer hospitality-loyalty comparisons (any pair of US-listed hotel issuers)
- Single-issuer loyalty-disclosure questions ("what percentage of <Issuer> room nights come from <Loyalty Program> members")
- Adjacent: "which company is better positioned to translate loyalty strength into durable economic advantage" -- the conclusion depends on having BOTH issuers' percentages, not just one

## Common failure modes (this skill prevents)

- ❌ Asymmetric retrieval -- model pulls full loyalty disclosure for one ticker but misses it for another, then concludes the second has "no disclosure" purely from the model's own retrieval gap.
- ❌ Quoting a non-canonical sentence (points-liability fair value, deferred-revenue footnote) that the rubric doesn't grade against.

Generic across hospitality issuers; no ticker-specific or rubric-keyed percentage values baked in.
