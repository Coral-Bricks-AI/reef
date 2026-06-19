---
id: multi_issuer_serial
when: Multi-issuer ratio / metric comparison (P/B, D/E, D/Cap, EV/EBITDA, EBITDA margin, DIO, FCCR, …) across ≥2 US-listed tickers — especially when balance-sheet as-of dates differ per ticker, when one or more issuers is a foreign private issuer / partnership / multi-segment filer, or when the per-ticker primitives need to be retrieved with their own ticker-specific guards.
applies_to: [planner]
---

**MANDATORY: one `invoke_next` entry per ticker.** If the question
names N tickers, your `invoke_next` array MUST contain N entries
— one `sector_analyst` per ticker. A single batched entry that
lists multiple tickers in one instruction is a routing failure
(the specialist will pick one as-of for the whole batch and the
per-ticker as-of dates the question specifies will be lost).

**Per-ticker instruction shape:** name exactly ONE ticker, the
ratios, the FY, and the as-of date for THAT ticker:

```
invoke_next: [
  {"persona_key": "sector_analyst",
   "instruction": "<TICKER_1>: compute <ratios> for FY<YYYY>, asof <DATE_1>.",
   "max_steps": 4},
  {"persona_key": "sector_analyst",
   "instruction": "<TICKER_2>: compute <ratios> for FY<YYYY>, asof <DATE_2>.",
   "max_steps": 4},
  …  (one entry per ticker, all parallel in round 1)
]
```

Each per-ticker dispatch carries its own as-of date. The
specialist passes that as-of into `compute_issuer_ratios`, which
routes to the right filing (10-K / 20-F / 10-Q) for that period.
Mixed as-of dates (e.g. AER at 12/31, FTAI at 9/30) are exactly
why the decomposition is required — a batched call loses the
per-ticker dates.

**Converge in round 2.** Once all per-ticker posts are on the
thread, converge — the postprocessor composes the per-ratio
ranking + peer averages from each per-ticker post (see its
multi-ticker-ratio synthesis rule). Do not dispatch another
"summary" specialist; the comparison composition is the
postprocessor's job.
