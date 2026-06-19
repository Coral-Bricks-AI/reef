---
id: regulatory_risk
when: Query asks about regulatory / compliance / legal risks from a 10-K, esp. data-handling SaaS (HCM/payroll, fintech, health IT): enumerate named statutes + infosec + third-party + tax + product subsections.
applies_to: [sector_analyst]
source_lines: 798-854
---

- **Regulatory risk questions → list every specific law by name,
  AND extend coverage to information-security + third-party + tax
  + product subsections.** When the question asks about regulatory,
  compliance, or legal risks from a 10-K — especially for
  data-handling SaaS issuers (HCM/payroll/benefits like PCTY/PAYC/
  PCTY/WORK, fintech like SQ/AFRM, or healthcare IT like VEEV/CERN)
  — the canonical answer enumerates 7-10 distinct items covering at least:

  1. Specific named statutes — quote whichever the issuer's own
     10-K names (sector- and jurisdiction-specific:
     healthcare-data laws, state biometric / consumer-privacy
     acts, federal trade / financial regulations, state
     breach-notification laws, payment-card data standards, etc.).
     List every one the filing actually cites.
  2. Foreign-data-privacy exposure — quote the jurisdictions and
     regulations the issuer names for international clients.
  3. Changing tax / benefit / employment law → product modification
     burden.
  4. **Money transmitter / money services business licensing
     risk** (always present for issuers handling client funds).
  5. **Third-party / supply-chain / vendor risk** — breaches at
     subprocessors, cloud-infra failures, supply-chain attacks.
     This subsection is in EVERY data-handling SaaS 10-K and is
     materially relevant to "regulatory" framing because the
     issuer's compliance posture extends to its vendor chain.
     Include it.
  6. **Cybersecurity incident → customer-trust / retention risk** —
     the same 10-K's "Risks Related to Information Security"
     subsection typically has a paragraph along the lines of *"a
     security incident could result in loss of customer trust and
     hinder our ability to retain or attract clients"*. Include
     it as a distinct bullet.
  7. **Regulatory scrutiny of cybersecurity is increasing** —
     usually a 1-2 sentence statement near the top of the security
     subsection.

  These seven categories (1–7) are the canonical bullet set
  for HCM/SaaS regulatory risk questions. High-level summaries
  ("evolving regulatory environment") under-answer because they
  don't name the statutes; section-bounded summaries that only
  cover the "Legal & Regulatory Matters" subheading miss
  categories 5, 6, 7, which live in adjacent risk-factor
  subsections.

  Set `max_steps: 14` so the specialist has room to read
  multiple risk-factor chunks. Instruction template: *"Read
  Item 1A of [TICKER]'s most recent 10-K. Enumerate EVERY major
  risk-factor subsection, not just the legal/regulatory one.
  Mandatory subsections to cover (each as a distinct bullet):
  (a) Data Privacy & Specific Statutes — name every statute the
  issuer's 10-K actually cites (sector- and jurisdiction-
  specific), (b) Foreign-data-privacy laws, (c)
  Changing tax/benefit/privacy laws → product modifications, (d)
  Money transmitter licensing, (e) Third-party / supply-chain
  vendor breaches, (f) Security incidents → loss of customer
  trust / retention, (g) Regulatory scrutiny of cybersecurity
  increasing. Quote the issuer's exact language for each."*
