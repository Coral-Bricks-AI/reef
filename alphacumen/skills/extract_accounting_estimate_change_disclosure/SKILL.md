---
id: extract_accounting_estimate_change_disclosure
when: Question asks about a change in accounting estimate (useful-life revision, amortization-period change, discount-rate update, allowance methodology shift, segment reclassification, depreciation-method change, etc.) and the rubric grades a verbatim management rationale ("the change is due to / based on / reflects / is attributed to ..."). Common trigger phrasings: "the rationale management gave for", "the disclosed reason for", "what does management attribute the change in X to", "explain why management changed X".
applies_to: [sector_analyst]
source_lines: 0
---

**Dedicated tool: `extract_accounting_estimate_change_disclosure(ticker, period_end_iso, change_topic)`. Call ONCE per (ticker, period, topic).**

Pulls the verbatim Notes-to-Financial-Statements passage from a specific 10-Q / 10-K where the issuer discusses a change in accounting estimate. The rationale for an estimate change typically lives in the *Notes* (10-Q Item 1 "Summary of Significant Accounting Policies" or 10-K Item 8 Note 1), NOT in Item 7 MD&A — and the rubric grades against the *exact* sentence the issuer uses to justify the change. Returns the verbatim 2-4 sentence passage anchored on the change-topic keyword + the surrounding rationale.

## Why this exists

The chunked SEC index BM25-ranks against keyword density, which routinely surfaces Item 7 MD&A "Critical Accounting Estimates" boilerplate or unrelated paragraphs over the specific Note 1 passage where the estimate change is announced + justified. Rubric atoms grading a *verbatim* management rationale (the issuer's exact "the change is attributed to / due to / based on <specific drivers> ..." sentence) fail when the model paraphrases the disclosure into its own words. A dedicated walker over the Notes-to-Financial-Statements text surfaces the exact 1-2 sentences with the rationale, which can then be quoted verbatim.

## Workflow

1. Identify the change topic and the period in which the change was first disclosed (e.g. "useful life" + Q1 of the fiscal year the new estimate took effect).
2. Call `extract_accounting_estimate_change_disclosure(ticker=<TICKER>, period_end_iso=<YYYY-MM-DD>, change_topic=<short noun phrase>)`. The period_end_iso is the quarter-end (or fiscal-year-end) date — the walker resolves it to the matching 10-Q / 10-K via `_find_filing_ref_for_asof`.
3. The skill pulls the filing's full text from EDGAR, finds the change_topic anchor, captures the surrounding 3-5 sentences (including the rationale sentence the rubric grades).
4. Quote `answer_summary_block` verbatim in the final answer's discussion of the estimate change. Include the issuer's specific rationale phrasing as a blockquote.

## When this skill applies

- Useful-life revisions (depreciation periods extended / shortened for specific asset classes)
- Amortization-period changes for intangibles
- Discount-rate or loss-rate methodology updates
- Reserves / allowances re-estimation with a stated rationale
- Segment reclassification announcements with stated rationale
- Any "change in accounting estimate" rubric atom whose answer hinges on quoting the management-disclosed rationale

## Common failure modes (this skill prevents)

- ❌ Quoting Item 7 MD&A "Critical Accounting Estimates" boilerplate ("management's estimates may differ from actual results") instead of the specific Note 1 change disclosure.
- ❌ Paraphrasing the rationale ("management believed conditions had improved") when the rubric expects the exact filing phrasing ("due to <specific drivers named by management>").
- ❌ Returning a stale prior-period disclosure (e.g. last year's useful-life change) when the question asks about a current-period change.

Generic across any US-listed issuer disclosing an accounting-estimate change in a quarterly or annual filing's Notes to Financial Statements. No ticker names, no rubric values, no row-keyed conditions baked in.
