---
id: inventory_turnover
when: Query asks for inventory turnover / inventory efficiency.
applies_to: [sector_analyst]
source_lines: 437-446
---

- **Inventory turnover → denominator = ENDING inventory, not
  (Beg + End) / 2.** The model defaults to the textbook
  "average inventory" formula even when the specialist prompt
  says otherwise. Force the convention in the `invoke_next`
  instruction itself: *"Compute inventory turnover as COGS /
  ENDING inventory (period-end balance sheet value). Do NOT
  average beginning and ending inventory. Use `run_python` with
  `avg_inventory = ending_inventory` so the calculation is
  auditable. Report `Avg. Inventory: $<ending_value>` and
  `Inventory Turnover = <COGS/ending>` in the answer."*
