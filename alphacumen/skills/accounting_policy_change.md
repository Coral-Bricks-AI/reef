---
id: accounting_policy_change
when: Accounting-policy-change disclosure question — useful life change, depreciation method change, revenue recognition change, segment realignment — that explicitly asks "what does [the change] suggest about [some business driver]" or "support your conclusion with the disclosed management rationale".
applies_to: [sector_analyst]
source_lines: 1244-1277
---

**Quote management's stated rationale verbatim AND state the
implication for the underlying business driver named in the
question.** The rubric for these questions grades BOTH:

## 1. Verbatim rationale

Quote the exact management language from the relevant 10-K / 10-Q
filing's MD&A — do NOT paraphrase ("based on better hardware" loses
the atom even when the underlying claim is right). Pull via
`extract_filing_tables` on the relevant 10-K + `get_full_text`
filtered by the relevant accounting-policy section (typically Note 1
Significant Accounting Policies or the Property, Plant & Equipment
note).


## 2. Implication framing

Explicitly state the implication for the business driver the
question NAMES. Read the question text to identify the business
driver — do NOT pre-bake a generic business-driver framing. If the
question asks *"what does this suggest about [X]"*, your answer
must explicitly state whether the disclosed change implies
acceleration / deceleration / shift / stability in [X]. Use
direction-of-change verbs ("accelerate", "decelerate", "expand",
"compress") rather than neutral descriptions.

## For Q-share-of-FY impact questions

Extract the Q disclosed impact ($/share or $-amount) from the Q
10-Q, management's full-year guidance for the impact from the same
filing, compute `Q_impact / FY_guidance × 100%`. State the % and
pair it with the direction-of-change framing the rubric expects
(acceleration when share is high, deceleration when share is low —
interpret in context of the named business driver).
