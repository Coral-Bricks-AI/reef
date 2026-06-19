---
id: legal_proceedings_enumeration
when: Question asks about "material legal battles" / "ongoing litigation" / "legal proceedings" / "what is X being sued for" / "regulatory investigations" for an issuer. Answer is a list of named matter categories the issuer's own counsel deems material.
applies_to: [sector_analyst]
source_lines: 191-203
---

**Anchor on the issuer's most-recent 10-K, NOT GDELT/news.**

## The trap to avoid

The default instinct is to reach for GDELT or news first because
"litigation" reads like a news-shaped question. It is not. News and
GDELT only surface *settlement events* — putative class actions, open
regulatory investigations, antitrust matters, and U&C / PBM-type
matter categories never reach the news index until they resolve. A
GDELT-first answer systematically undercounts the issuer's active
legal exposure and misses entire matter categories the 10-K
enumerates by name.

Item 3 (Legal Proceedings) plus the "Commitments and Contingencies"
note in the financial statements lists every matter the issuer's own
counsel deems material. That list IS the enumeration the question is
asking for.

## Workflow

1. **Single `bm25_sec` call** narrowed to the issuer's most-recent
   10-K: `bm25_sec(ticker=<X>, form_type="10-K", query="legal
   proceedings commitments contingencies", k>=5)`. The query text
   targets both Item 3 and the Commitments note in one pass.
2. `get_full_text` on the top hits to enumerate every named matter
   category — each typically opens with a short caption naming the
   matter type (antitrust, securities class action, ERISA, patent
   infringement, FCPA investigation, etc.) followed by status. Cap to
   ≤3 calls; the 10-K's named-matter list is finite.
3. Compose `answer_summary` as a bullet list where each disclosed
   matter category is its own bullet, using the issuer's own caption
   wording — apply Hard rule 6 (enumeration completeness).
4. **Only NOW** reach for `bm25_gdelt` / news — to quantify
   settlement amounts on a matter already named in the 10-K. Never to
   discover the matter list.

## Common failure modes

- ❌ Leading with `bm25_gdelt` and returning only the settled or
  newsworthy matters. The active matters never made the news yet,
  and they are exactly what the question asks about.
- ❌ Collapsing distinct disclosed matter categories (e.g. antitrust,
  PBM pricing, ERISA, securities class action) into one "legal
  risks" or "various litigation" bucket. Each named category is its
  own bullet.
- ❌ Refusing on "no recent news coverage" without first reading the
  10-K. The 10-K is the canonical source even when the matter has
  zero news coverage.
- ❌ Stopping at Item 3 without checking the Commitments and
  Contingencies note. The note frequently carries matters Item 3
  cross-references rather than restates.
