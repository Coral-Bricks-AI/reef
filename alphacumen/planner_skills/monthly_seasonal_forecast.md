---
id: monthly_seasonal_forecast
when: Query contains a calendar-MONTH seasonality word for an issuer publishing monthly revenue as 6-K filings (TSM/UMC/ASE): a monthly seasonal-forecast question, not a quarterly comparison.
applies_to: [sector_analyst]
source_lines: 869-891
---

- **Monthly-revenue seasonal forecasting (foreign 6-K issuers).**
  When the question contains a CALENDAR-MONTH word ("March
  seasonality", "September seasonality", etc.) for an issuer
  that publishes monthly revenue as 6-K filings (TSM, UMC,
  ASE), the question is a **monthly seasonal-forecast** question
  — NOT a quarterly Q[N]→Q[N+1] comparison, even if the
  question literally names "Q[N+1] guidance". The target month
  in the question is the one being forecast; the target quarter
  is whichever quarter CONTAINS that month.

  Route to `sector_analyst` with **`max_steps: 16`** (need 7-8
  bm25_sec calls + 7-8 get_full_text calls + 1 format tool
  call). Instruction template: *"This is a MONTHLY seasonal
  forecast question. Load the `monthly_seasonal_forecast` sector
  skill (dispatched via `invoke_skill_fn`). Pull these 8 6-Ks in parallel: the
  [prior_month] revenue 6-K for [Y-3], [Y-2], [Y-1], [Y]; the
  [target_month] revenue 6-K for [Y-3], [Y-2], [Y-1]; the Q[N-1]
  earnings 6-K with the Q[N] USD guidance. Extract growth
  percentages from each Mar 6-K (verbatim 'increase/decrease of
  X percent from [prior_month]') and call format_seasonal_forecast
  with all extracted values. DO NOT interpret this as a quarterly
  Q[N]→Q[N+1] comparison even if the question says 'Q[N+1]
  guidance'."*
