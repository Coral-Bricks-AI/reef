---
id: compute_restructuring_plan_flowthrough
when: Restructuring-plan-flow-through question — how a specific Restructuring Plan flowed through FY-X financial statements, difference between reported restructuring charges and plan-attributable charges, liability roll-forward, YoY headcount change.
applies_to: [sector_analyst]
source_lines: 1147-1204
---

**Tool option (try first, fall back to manual workflow if
incomplete):** `compute_restructuring_plan_flowthrough(ticker,
plan_name, fy)` — pulls FY 10-K iXBRL facts + Q-prior 10-Q iXBRL
facts + headcount in one call. The numeric fields
(`reported_total`, `plan_severance`, `other_severance`,
`litigation_total`, `impairment_total`, `ending_accrued`,
`plan_charges_incurred`, `plan_cash_payments`, `cumulative_q_prior`,
`expected_plan_cost`) come from sec-api's XBRL-to-JSON converter
filtered by `us-gaap:RestructuringPlanAxis` — they ARE the
filed-precision SEC values.

**ONE-CALL DISCIPLINE.** Call this tool ONCE per ticker / plan /
fy combination, then **immediately summarize and stop**. Do NOT
loop trying to fill missing fields with other tools. In particular:

- `headcount_fy` / `headcount_fy_prior` are null when the issuer
  doesn't tag `dei:EntityNumberOfEmployees` in iXBRL (Intel is one
  such issuer). When null, you may make ONE
  `get_full_text(ref=<FY 10-K>, max_chars=80000)` call per FY 10-K
  to search the Human Capital paragraph, but if that doesn't surface
  an explicit "approximately N employees" line in the first read,
  **report headcount as not directly disclosed in the available
  text and stop searching**. Burning 10+ rounds chasing
  approximations is wasted budget — the specialist's react loop
  has a hard step cap, and exhausting it without writing a final
  summary means the synthesizer never sees the dollar figures
  you already extracted.

- The dollar-figure null sentinels (`reported_total = null`,
  `plan_severance = null`) only matter when the iXBRL extraction
  came up empty for an issuer whose 10-K Note has no iXBRL tagging
  on the restructuring components — rare for active-plan filings.
  In that case fall through to the manual workflow below. Don't
  re-call the tool with different args trying to fix it.

**QUOTE-VERBATIM DISCIPLINE.** Copy `answer_summary_block`
**verbatim** into your final response — every per-category
dollar figure, the roll-forward, the cumulative-to-date, the
derived differences, and the narrative paragraph. The block is
formatted so the synthesizer can drop it straight into the
user-facing answer. Do NOT default to "data gap" / "not
retrievable in available research window" framings — those
phrases are appropriate only when the tool returned no non-null
numeric fields at all.

## Manual workflow (fallback only — skip when the tool returned numbers)

1. **FY 10-K Note on Restructuring.** Locate via
   `extract_filing_tables(ref=<FY 10-K>, table_keyword="restructuring",
   item="8", limit=10)`. The note typically tables (a) total
   Restructuring and Other Charges by category, (b) per-plan
   severance + benefit charges, (c) the liability roll-forward
   (beginning balance + charges + payments + ending balance), (d)
   any non-plan severance / litigation / asset impairment
   sub-totals. Quote each dollar figure verbatim — investors
   reading the answer need per-line precision (e.g. "litigation
   charges $X million", "asset impairment $Y million", "ending
   accrued $Z million").

2. **Q-prior 10-Q cumulative plan cost.** Find the Q3 (or whichever
   quarter the question references) 10-Q via `bm25_sec` filtered to
   form_type=10-Q and the relevant fiscal period. The cumulative
   plan cost as of that quarter is in the same Restructuring note
   (typically labeled "Cumulative <plan name> charges through
   <date>"). Quote verbatim.

3. **YoY headcount.** Pull the employee-count rows from both FY
   10-Ks (cover page or Item 1 Description-of-Business "Human
   Capital" sub-section) via `get_full_text(ref=<FY 10-K>,
   max_chars=80000)`. Compute
   `(FY_headcount - FY-1_headcount) / FY-1_headcount × 100%`.

4. **Compute the differences explicitly.** Show:
   - `reported_restructuring_total - plan_attributable_severance` =
     residual covering litigation + asset impairment + other-plan
     severance
   - `reported_restructuring_total - Q-prior_cumulative_plan_cost` =
     incremental charges booked after Q-prior cutoff
   Show both math lines with the underlying $ inputs.

5. **Narrative atoms.** State explicitly that the plan flows
   through (a) the **Restructuring and Other Charges line** on the
   income statement, (b) the **accrued compensation / current
   liabilities line** on the balance sheet (via the liability
   roll-forward), and (c) cash payments that reduce the accrued
   liability. Also state that the reported FY charge base exceeds
   both the plan-attributable severance and the Q-prior cumulative
   plan cost because it ALSO includes broader severance,
   litigation, and asset impairment charges.
