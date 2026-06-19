---
id: implied_fy_runrate
when: An issuer gave no explicit full-year guidance but offered a Q4 actual + run-rate-proxy framing for the next year.
applies_to: [sector_analyst]
source_lines: 497-514
---

- **Derive implied FY when explicit guidance is missing but a Q4
  actual + run-rate framing is available.** A common disclosure
  pattern: an issuer doesn't publish an explicit full-year capex /
  revenue / spend guidance in its Q4 8-K, but management offers a
  framing that anchors the next year to the Q4 actual ("we view
  Q[N] [Y-1] as a reasonable run-rate proxy for [Y]", "Q4 capex
  is the right base for the year ahead", etc.). In that case the
  GP should compute and lead with `implied FY [Y] = Q4 [Y-1]
  actual × 4` and cite both inputs (the Q4 dollar figure from the
  filing AND the management framing language from the press release
  or transcript). Make the derivation explicit in the answer body
  (e.g. *"[ISSUER]: no explicit FY [Y] capex disclosed; implied
  [Q4 actual × 4], per management's run-rate proxy framing on the
  Q4 earnings call"*). This is NOT averaging across sources —
  it's a single-source derivation that uses the issuer's own
  framing to project the filed number forward.
