---
id: revenue_disaggregation_customers
when: Query asks "% of customers / customer mix / customer concentration" by channel (direct / channel partners / reseller): answer is the revenue-disaggregation table (Item 8).
applies_to: [sector_analyst]
source_lines: 543-603
---

- **Revenue disaggregation IS the standard answer to "% of
  customers" / "customer mix" questions — do NOT hedge with
  "metric not found".** SEC issuers disclose customer/channel mix
  through **revenue disaggregation** in the Note 3 / Note X
  Revenue tables; they almost never publish a separate
  customer-count breakdown. When the question is phrased as
  "what % of customers were derived from channel partners?" /
  "what % of customers are direct?" / "customer concentration mix"
  and the specialist has surfaced a Revenue Disaggregation table
  with the channel percentage, that IS the answer. State the
  percentage directly without prefacing it with "the specific
  metric was not found" or "this is a related but distinct
  metric." A footnote like "disclosed as % of revenue" is fine,
  but lead with the number. The headline framing IS the answer;
  if you open with "not found", a reader treats the answer as a
  refusal even when the number appears later. Pattern: a
  revenue disaggregation row like "Channel partners | $<X> |
  <Y>%" answers the "% from channel partners" question — quote
  "**<Y>%** (per the FY revenue disaggregation table)" — NOT
  "the specific customer-count percentage was not disclosed".
  This rule overrides any specialist instinct to refuse:
  even if your training data says the SEC doesn't disclose
  customer-count percentages directly, the issuer's revenue
  disaggregation IS the disclosure and IS the canonical answer.
  Force a `bm25_sec` query for `<TICKER> revenue
  disaggregation channel partners` (substitute "direct sales",
  "indirect", "reseller" for the channel term as appropriate)
  before answering "not found".

  **Mandatory tool sequence for this question pattern.** BM25
  ranks the disaggregation table chunk LOW because the
  surrounding narrative carries higher keyword density on
  generic revenue / customer terms while the table itself is
  numeric (`$337,394 20% Direct customers 1,332,232 80%`-style
  whitespace-dense content). `get_full_text` on a non-table
  chunk reads as "metric not found" even though the table
  exists in a different chunk. The reliable retrieval path is:

  1. `bm25_sec(form_type:"10-K", ticker:<TICKER>, query:"revenue
     contracts by type customer channel partners")` to locate
     the FY 10-K accession ref.
  2. **`extract_filing_tables(ref=<10-K accession ref>,
     table_keyword="contracts by type of customer", item="8")`** —
     the Revenue Note with the disaggregation table lives in
     **Item 8 (Financial Statements)**, NOT Item 7 (MD&A). The
     tool's default `item="7"` will return zero tables for this
     question pattern. Always pass `item="8"`. If the keyword
     variant doesn't match, retry with
     `table_keyword="revenue disaggregation"`,
     `table_keyword="channel partners"`, or
     `table_keyword="customer concentration"`, each with
     `item="8"`.
  3. Quote the percentage row from the returned table verbatim.

  Specialists routinely skip step 2 (or call it with the default
  `item="7"` and get zero hits) and then refuse after step 1
  surfaces only narrative chunks; the canonical answer lives in
  the Item 8 table that step 2 surfaces. The dispatch instruction
  MUST mention `extract_filing_tables` by name AND the
  `item="8"` argument for any "% of customers/revenue from
  [channel]" question.
