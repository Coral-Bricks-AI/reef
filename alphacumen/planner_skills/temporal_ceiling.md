---
id: temporal_ceiling
when: You resolved a fiscal year to a calendar window and must pass an upper filing bound (filed_at_lte) / anchor on the period the filing covers to avoid the most-recent-filing default.
applies_to: [sector_analyst]
source_lines: 707-723
---

- **Temporal ceiling: pass explicit filed_at_lte.** When you
  resolve a fiscal year to a calendar window, include BOTH bounds
  in the instruction — not just the start. Example: *"Pull
  [ISSUER]'s FY[Y] 10-K (fiscal year ending [FYE date], filed by
  [filing deadline]). Use filed_at_lte=[filing deadline] to
  exclude FY[Y+1] filings."* Without the upper bound, the
  specialist defaults to the most recent filing (FY[Y+1]) because
  BM25 recency bias surfaces it first. **But a periodic report is
  filed AFTER the period it covers** (a 10-Q ~30–45 days after
  quarter-end, a 10-K ~45–90 days after FYE), so the ceiling must
  be the *filing* window, never the period-end itself —
  `filed_at_lte=<quarter-end>` excludes the very 10-Q that reports
  that quarter and yields the prior quarter's instead. Better:
  tell the specialist to anchor on the period the filing covers,
  not when it was filed. Especially for "as of Q[N] YYYY" / "as
  of <date>" snapshot questions (registered/listed securities,
  shares outstanding, board composition, debt outstanding).
