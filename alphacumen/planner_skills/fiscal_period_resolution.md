---
id: fiscal_period_resolution
when: Query references a bare "FY YYYY" or "Q[N] FY[X]" that could plausibly mean either a year-end snapshot or a same-quarter comparison; pick the right shape before instructing a specialist.
applies_to: [sector_analyst]
source_lines: 331-382
---

- **Name the comparison shape, not the calendar bounds.** Fix the
  *kind* of period — year-end snapshot vs same-quarter YoY — and
  state it explicitly in the instruction (e.g. *"comparing Q3
  FY2025 vs Q3 FY2024"*, *"year-end snapshot for FY2024"*). The
  specialist verifies the issuer's FYE from the 10-K and resolves
  calendar bounds; the *kind* is not recoverable downstream once
  dispatched.

  **Resolution for a bare "FY YYYY":** check the rest of the
  question for companions.
  - Paired with a specific quarter of another FY → resolve the
    bare FY to that same **Q[N]**, pull the 10-Q. Defaulting to
    year-end is the dominant period-resolution failure.
  - Paired with another bare FY only → both year-end.
  - Standalone → year-end.

  Self-check: if your restated shape names a 10-K or year-end
  snapshot on either side of a question containing "Q[N]", you
  misapplied the rule — re-resolve.
