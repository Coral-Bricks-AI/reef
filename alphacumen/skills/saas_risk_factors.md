---
id: saas_risk_factors
when: Question asks about "regulatory risks" / "risk factors" / "Item 1A" / "compliance risks" for a data-handling SaaS issuer (HCM/payroll, fintech, cloud-collab, healthcare-IT, commerce).
applies_to: [sector_analyst]
source_lines: 29, 442-511
---

**SaaS 10-K Risk-Factors enumeration — read multiple Item 1A
subsections.**

Item 1A spans multiple subsections; data-handling SaaS issuers
typically disclose risk along multiple axes (regulatory, security,
third-party, tax/benefit-law-change, licensing). Enumerate each as
a distinct bullet.

## Workflow

1. `bm25_sec` for the issuer's most recent 10-K with `form_type:
   "10-K"`, then NARROW to risk-factor chunks with `body_mode:
   "snippet"` and follow-up queries:
   - `"risk factors data privacy regulation"` (legal/regulatory)
   - `"risk factors security incident breach trust"` (info-security)
   - `"risk factors third-party vendor supplier subprocessor"` (vendor)

   Each query targets a different Item 1A subsection. Call
   `get_full_text` on the top hit from EACH query so you cover all
   relevant subsections.

2. Enumerate the issuer's disclosed risks across these axes (use
   the issuer's own wording — do NOT substitute generic phrasing):

   1. **Global data privacy evolution** — the trajectory the
      issuer cites for cross-jurisdictional privacy regulation.
   2. **Specific US laws cited** — name every statute the issuer's
      10-K explicitly cites (sector- and jurisdiction-specific:
      healthcare-data laws, state biometric / consumer-privacy
      acts, federal trade / financial regulations, state
      breach-notification laws). Do NOT collapse them into "various
      federal and state laws".
   3. **Foreign data-privacy laws** — name the jurisdictions /
      frameworks the issuer cites (EU, UK, APAC, etc.) for
      international clients.
   4. **Changing tax / benefit / privacy laws** that require
      product modifications, increase costs, delay new offerings,
      or reduce demand. Distinct from atom 2 (current vs changing).
   5. **Money transmitter / money-services-business licensing** —
      issuers handling client funds disclose this.
   6. **Third-party / supplier / subprocessor breach risk** —
      typically in the "Risks Related to Operations" or "Risks
      Related to Information Security" subsection. Distinct from
      regulatory risk even when the question framing is
      "regulatory".
   7. **Security incidents impacting customer trust / retention**
      — same Information Security subsection. Frames the
      customer-relationship consequence of a breach.
   8. **Regulatory scrutiny of cybersecurity increasing** —
      usually a 1-2 sentence statement near the top of the
      security subsection. Distinct from legal/regulatory because
      it lives in the security area.

3. Write `answer_summary` as a bullet list where each enumerated
   axis is its own bullet — do not collapse third-party +
   security-incident + cyber-scrutiny into one "cybersecurity"
   bullet.

## Common failure modes (DO NOT do these)

- ❌ Stopping after the "Legal & Regulatory Matters" subsection
  (misses several axes that live elsewhere in Item 1A).
- ❌ Collapsing third-party + security-incident + cyber-scrutiny
  into one "cybersecurity" bullet.
- ❌ Substituting generic phrasing for the issuer's actual
  disclosed wording.
