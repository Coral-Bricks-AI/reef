---
id: bank_tbvps_recovery
when: Bank/financial-issuer question framed as "liquidation value per share" / "shareholder recovery" / "what could each shareholder get" / per-share book or tangible-book value.
applies_to: [sector_analyst]
source_lines: 40-46
---

**Answer with the issuer's published TBVPS (tangible book value per
share), verbatim — never reconstruct it.**

## The trap to avoid

These questions read like a divide-stockholders'-equity-by-shares
exercise. They are not. TBVPS is a canonical published KPI for banks
and the issuer's own definition diverges from naive
equity-over-shares: regulatory intangibles, goodwill, preferred
stock, and weighted-average vs period-end shares all change the
denominator and numerator in issuer-specific ways. By analyst
convention, "what each shareholder could recover in liquidation" is
answered with the issuer's TBVPS — not a derived figure.

## Workflow

1. `bm25_sec(ticker=<X>, form_type=["10-K", "10-Q"], query="tangible
   book value per share", k>=5)` — the issuer's earnings-release 8-K
   (Item 2.02) and investor-presentation 8-K (Item 7.01) also carry
   TBVPS prominently; widen to `form_type: "8-K"` if the periodic
   filings don't surface it.
2. `get_full_text` on the top hit and quote the published per-share
   figure exactly. The number lives in MD&A, the Selected Financial
   Data table, or a Non-GAAP Reconciliation table.
3. If both shapes return zero hits, fall through to book value per
   share with the caveat noted; do NOT compute either by hand.

## Common failure modes

- ❌ Dividing total stockholders' equity by shares outstanding and
  presenting that as the answer. Wrong denominator (preferred stock,
  weighted-average shares) and wrong numerator (no intangibles
  adjustment).
- ❌ Refusing on "liquidation value not disclosed." The question is
  asking about TBVPS by analyst convention — the disclosure exists.
- ❌ Answering with BVPS when TBVPS is published. TBVPS is the
  bank-analyst standard.
