---
id: foreign_private_issuer
when: Ticker is a foreign private issuer: name the foreign form (6-K instead of 8-K, 20-F instead of 10-K/10-Q).
applies_to: [sector_analyst]
source_lines: 861-868
---

- **Foreign private issuers — name the right form (critical).**
  Non-US companies file **6-K** instead of 8-K and **20-F**
  instead of 10-K / 10-Q. When the user asks about earnings or
  annual disclosures for a foreign-private-issuer ticker, your
  instruction must name the foreign form explicitly (e.g. *"pull
  [TICKER]'s most recent 6-K covering Q[N] [Y] results"*).
  Asking for an 8-K or 10-Q on a foreign issuer will return
  nothing.
