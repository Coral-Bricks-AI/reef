---
id: registered_securities_listing
when: "What securities does X have registered on a national exchange" / "list all securities trading under issuer Y" — common stock, debt notes, preferred, depositary shares, warrants, units listed under the issuer's name.
applies_to: [sector_analyst]
source_lines: 245-255
---

**Call `get_registered_securities(ref)` against a 10-K or 10-Q —
NOT S-3 / 424B2 / 424B5 / 8-K.**

## The trap to avoid

The cover-page "Securities registered pursuant to Section 12(b)"
table is the standing list of every security registered under the
issuer's name on a national exchange. It is the answer to "what's
registered."

S-3, 424B2, 424B5, and 8-K filings announce *new offerings* — they
describe individual transactions, not the standing registered-list.
If the GP routed you to S-3/424B for a "what's registered" question,
ignore that routing and go to the 10-K/10-Q cover-page table
instead.

Like all cover-page disclosures, this table is stripped from
`sec_filings_chunked` (Hard rule 8) and only accessible via the
dedicated tool.

## Workflow

1. `bm25_sec` for the issuer's most-recent 10-K or 10-Q. Any chunk's
   `ref` works — the tool reaches the cover-page table
   independently.
2. `get_registered_securities(ref)` — returns one row per registered
   security: title, trading symbol, exchange.
3. Enumerate every returned row (Hard rule 6 — completeness).

## Common failure modes

- ❌ Pulling the 424B2 prospectus for a "what's registered" question
  and listing only the bond series it covers. That filing describes
  one offering, not the universe.
- ❌ Filtering out debt notes / depositary shares / warrants because
  they're "not equity." The Section 12(b) table is the universe of
  registered securities — every row counts.
- ❌ Refusing on "cover-page data not in index." The dedicated tool
  exists.
