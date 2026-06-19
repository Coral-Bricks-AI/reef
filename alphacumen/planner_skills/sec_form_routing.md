---
id: sec_form_routing
when: Query needs a specific SEC form beyond 10-K/10-Q/8-K: board nominations / exec pay (DEF 14A), preferred/offering terms (424B), shelf (S-3), IPO (S-1), registered/listed securities (10-K 12(b) cover table), or beat/miss BPS math.
applies_to: [sector_analyst]
source_lines: 259-297
---

- **Route to the right SEC form type.** The SEC index contains
  multiple form types beyond 10-K/10-Q/8-K. When instructing
  `sector_analyst`, name the form type that has the data:
  - **Board nominations, director compensation, executive pay** →
    `DEF 14A` (Proxy Statement). NOT in 10-K.
  - **Preferred stock offering terms (price, conversion, dividends,
    voting rights, liquidation preference)** → `424B2` or `424B5`
    (Prospectus Supplement). The 8-K announces the offering; the
    424B has the detailed terms. Try both form types.
  - **Registration statements, shelf offerings** → `S-3`.
  - **IPO prospectus** → `S-1`.
  - **Which securities are registered / listed to trade on a
    national securities exchange under <issuer>'s name** (common
    stock AND debt notes, preferred, depositary shares, warrants,
    units) → the **cover-page "Securities registered pursuant to
    Section 12(b)" table of the 10-K / 10-Q** — NOT S-3 / 424B / 8-K.
    Those announce *new offerings*; the 12(b) table is the standing
    list of what's on an exchange. Instruct `sector_analyst` to read
    the cover-page registered-securities table of the relevant 10-K
    or 10-Q (for "as of Q[N] YYYY" / "as of <date>", the 10-Q whose
    reporting period covers that date — filed ~30–45 days after the
    quarter closes).
  State the form type in the instruction, e.g.: *"Search BBSI's DEF
  14A proxy statement (filed ~April 2024) for the list of director
  nominees."* Without this hint, the specialist defaults to 10-K/8-K
  and misses the data entirely.
  **Prospectus supplements (424B2) may not have a ticker filter.**
  Prospectus supplements are sometimes filed by the underwriter, not
  the issuer, so the ticker field may be empty. If filtering by
  ticker + form_type "424B2" returns 0 results, instruct the
  specialist to search by company name keyword instead of ticker
  filter.