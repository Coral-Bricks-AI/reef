---
id: extract_item1_human_capital_headcount
when: Question asks for year-over-year (or multi-year) headcount / employee-count change at a US-listed issuer. Trigger phrases include "headcount reduction", "year-over-year headcount", "employee reduction", "workforce reduction", "change in employees from FY-A to FY-B", or any question that grades a % change in the employee base across fiscal years.
applies_to: [sector_analyst]
source_lines: 0
---

**Dedicated tool: `extract_item1_human_capital_headcount(ticker, fy_start, fy_end)`. Call ONCE per ticker.**

Walks each fiscal year's 10-K Item 1 Business and reads the "Human Capital" / "Employees" subsection prose for the verbatim headcount disclosure (the canonical "we employed approximately X employees as of <date>" sentence). Returns per-FY headcount + YoY %.

## Why this exists

Headcount is reported in Item 1 prose ("Human Capital" or, in older filings, "Employees"), not in XBRL. The `dei:EntityNumberOfEmployees` field is supposed to capture it but a meaningful share of US issuers (including several large-cap technology / semiconductor names) don't tag it at all. Without a dedicated walker, the model either pulls a stale prior-year figure from the chunked index, quotes the wrong sub-period mid-year update, or aborts the atom entirely with "headcount not disclosed". Rubric atoms grading a multi-FY % delta need the canonical end-of-FY figure from EACH year's 10-K — which means walking each FY's Item 1 deterministically.

## Workflow

1. Identify the FY range the question grades (typically the latest two FYs for a "year-over-year" question, or a multi-FY series for a "trend" question).
2. Call `extract_item1_human_capital_headcount(ticker=<TICKER>, fy_start=<FY-A>, fy_end=<FY-B>)`.
3. The skill walks each FY's 10-K backward from `_find_10k_ref_for_fy`, pulls Item 1 via the sec-api Extractor with an EDGAR direct-fetch fallback, finds the Human Capital subsection, regex-extracts the headcount + as-of date, and computes per-FY YoY %.
4. Quote `answer_summary_block` verbatim. The block contains each FY's headcount sentence (verbatim from the filing) and the computed % delta.

## When this skill applies

- Single-issuer multi-FY headcount-trend questions
- Restructuring / layoff impact analysis where the rubric grades the % workforce change as one atom
- Adjacent: questions about a specific "Human Capital" metric beyond raw count (gender mix, regional distribution) — the same Item 1 subsection prose carries these numbers

## Common failure modes (this skill prevents)

- ❌ Surfacing a quarterly headline ("we reduced headcount by 15,000 in Q3") instead of the FY-end disclosure the rubric grades.
- ❌ Pulling the prior-year comparable figure quoted inside the current-year 10-K ("compared to N a year ago") instead of the current-year headcount — getting the direction right but the base year wrong.
- ❌ Returning "headcount not disclosed" because XBRL `dei:EntityNumberOfEmployees` is empty for issuers that don't tag the field (despite the prose disclosure being present in every Item 1).

Generic across any US-listed issuer that reports an Item 1 "Human Capital" or "Employees" subsection with the standard "we employed approximately X employees" disclosure pattern. No ticker-specific values baked in.
