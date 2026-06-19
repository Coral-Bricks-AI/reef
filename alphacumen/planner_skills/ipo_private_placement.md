---
id: ipo_private_placement
when: Query is about an IPO (S-1 registration / prospectus) or a private fundraise / Reg D offering (Form D).
applies_to: [sector_analyst]
source_lines: 892-899
---

- **IPO and private-placement disclosures.** For IPO questions,
  point `sector_analyst` at the registration statement / prospectus
  (S-1) for business description, risk factors, financials, and
  use of proceeds. For private fundraises or Reg D offerings,
  point it at Form D for amount raised, investor count, and
  exemption type. These forms must be named in the instruction —
  a generic "find the IPO filing" defaults to 8-K/10-K and misses
  them.
