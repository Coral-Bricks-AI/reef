---
id: forward_looking_anchor
when: Query uses forward-tense verbs relative to the period asked about (plans to / will spend / guides / guided / expects / forecasts / targets): canonical answer is the guidance-issue filing, not the retrospective actual.
applies_to: [sector_analyst]
source_lines: 384-420
---

- **Forward-looking language → as-of-issue anchor (critical).**
  When the user's question is phrased in the *future tense relative
  to the period being asked about* — verbs like "plans to", "will
  spend", "guides", "guided", "expects", "forecasts", "projects",
  "anticipates", "targets" — the canonical answer lives in the
  disclosure made **at the START of that period**, NOT in
  retrospective actuals filed at the end. Map the period to its
  guidance-issue window, not its results window:
  - **"What did X plan to spend / guide / forecast for FY YYYY?"**
    → Q4 FY [YYYY-1] earnings 8-K (filed Jan–Feb of YYYY for
    calendar-year filers; one fiscal quarter later for off-cycle
    issuers). The guidance lives in *Item 2.02 results of operations*
    + the press release exhibit, often in a "Financial Outlook" or
    "Current Outlook" section. The 10-K filed at the end of FY YYYY
    contains *actuals*, not the original guidance.
  - **"What did X plan to spend / guide for Q[N] YYYY?"** → Q[N-1]
    YYYY earnings 8-K (or Q4 [YYYY-1] for Q1 questions).
  - **"What is X expected to earn next quarter?"** (asked today)
    → most recent quarterly earnings 8-K, where management gave
    next-quarter guidance.
  - The naive mistake (which retrieval-recency bias amplifies) is
    to map "FY YYYY guidance" to the FY YYYY 10-K because both
    contain "FY YYYY". Resist that — name the issue-date filing
    explicitly in the instruction so the specialist doesn't default
    to the most recent disclosure.
  - **Even when `{today}` is well after the period closes**, a
    "plans to" / "guided" question is asking what was *planned at
    the time*, not what was eventually spent. Reporting actuals
    against a guidance question is a contradiction failure under
    a literal-read of the answer, not partial credit — the actuals number is
    a different concept than the guidance number, even if numerically
    close.
  - Restate the issue-date filing in plain English in the
    instruction so the specialist doesn't default to the most
    recent disclosure: e.g. *"Pull X's FY YYYY capex guidance —
    that was disclosed on the Q4 [YYYY-1] earnings 8-K (Item 2.02);
    quote the guidance figure and any framing language verbatim."*
