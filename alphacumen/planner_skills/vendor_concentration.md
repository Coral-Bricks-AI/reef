---
id: vendor_concentration
when: Query asks about vendor / supplier / single-vendor concentration risk (10-K Item 1A Risk Factors).
applies_to: [sector_analyst]
source_lines: 604-621
---

- **Vendor / supplier concentration risk → 10-K Risk Factors,
  named single-vendor passage.** When the question asks about an
  issuer's "vendor concentration", "supplier concentration", or
  "key vendor risk", the canonical SEC disclosure is a Risk
  Factor paragraph naming the single vendor or small vendor set
  the issuer depends on. The phrasing in the filing is usually
  *"our [merchant processing | manufacturing | inventory supply |
  payments processing | cloud services] is facilitated by one
  vendor"* / *"we rely on a single supplier for X"*. Dispatch
  `sector_analyst` with:
  *"Apply Hard rule for vendor-concentration risk. Call
  `bm25_sec(form_type:'10-K', ticker:<TICKER>, query:'single
  vendor concentration risk factor processing supplier')` then
  `get_full_text` on the top hit, scan Item 1A Risk Factors for
  a paragraph naming the single vendor. Quote that paragraph
  VERBATIM — the named vendor + the operational scope (e.g.
  'North America merchant processing') IS the answer; do NOT
  paraphrase or generalise to 'multiple vendors'."*
