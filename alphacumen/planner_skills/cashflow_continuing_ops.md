---
id: cashflow_continuing_ops
when: Query asks for a multi-year CFO / FCF / FCF-margin trend (use continuing operations, not raw consolidated CFO - Capex).
applies_to: [sector_analyst]
source_lines: 515-522
---

- **Cash-flow trend questions use continuing operations.** For any
  multi-year trend in CFO / FCF / FCF margin, lead with the
  continuing-operations series, not raw consolidated `CFO − Capex`.
  Raw CFO is distorted by discontinued-ops wind-downs, divestiture
  proceeds, and one-time settlements; the canonical trend uses the
  ongoing-business view. Instruct `sector_analyst` to pull the
  ex-discontinued-ops figure from the cash-flow-statement
  segmentation or the non-GAAP FCF reconciliation in MD&A.
