---
id: precision_preservation
when: A specialist returned an `answer_summary_block` from a dedicated tool, or claims it has the grounded figures; decide whether to quote verbatim + converge vs. re-dispatch on a partial/refusal.
applies_to: [sector_analyst, stock_analyst]
source_lines: 147-199
---

- **PRECISION PRESERVATION — quote `answer_summary_block` verbatim,
  but ONLY when the specialist actually returned one. This rule is
  NARROWLY SCOPED to the verbatim-quoting case and does NOT
  authorize early convergence on a partial-or-refusal answer.** The
  rule has two parts, both narrow:

  Part A — **Verbatim quoting**. When a specialist returns
  `answer_summary_block` from a dedicated tool
  (`compute_payout_ratio_peers`, `compute_issuer_ratios`,
  `compute_note_diff_across_years`, `run_dcf`,
  `compute_price_returns_multi`, `format_seasonal_forecast`, etc.),
  copy that block VERBATIM into your final `answer_summary`. Do
  NOT recompute the values, do NOT paraphrase the formatted rows,
  do NOT round to fewer decimal places. The tool's block IS the
  canonical phrasing the rubric grades.

  Part B — **No "just-to-verify" rounds**. If a specialist's
  answer already contains the figures the question asks for AND
  the figures are anchored to the named source (XBRL accession,
  10-K page, 8-K exhibit), converge. Do NOT spawn another round
  "to verify" or "add color" — the rubric grades atoms
  independently and one well-sourced atom beats two paraphrased
  ones.

  **THIS RULE DOES NOT APPLY when the specialist returned a
  refusal / partial-availability response.** Specifically, when
  any of the following appear in a specialist's message — *"not
  successfully retrieved"*, *"data unavailable"*, *"data gap"*,
  *"cannot be calculated"*, *"missing critical inputs"*, *"not in
  the retrieved corpus"*, *"the equity database does not contain"*,
  or any synonym — the MANDATORY EXPLICIT-TICKER ENGAGEMENT rule
  (above) takes precedence: dispatch a re-round with raised
  `max_steps`, a different specialist (sector_analyst →
  stock_analyst or vice-versa), or a named SEC form type +
  retrieval keyword. The precision-preservation rule is for
  KEEPING good evidence intact, NOT for accepting partial-refusal
  as final.

  Smell test before invoking precision-preservation:
  - Does the specialist's message contain an
    `answer_summary_block` from a dedicated tool? → Yes: quote
    verbatim and converge.
  - Does the specialist explicitly say it has all the figures
    grounded in a source? → Yes: converge with verbatim quote +
    source citation.
  - Does the specialist's message contain any refusal /
    not-retrieved phrase? → NO: re-dispatch per the
    explicit-ticker engagement rule.
  - Is the answer partial because one specific cell came back
    `n/a` from a tool (e.g. issuer doesn't tag the XBRL concept)?
    → Yes: ship the populated cells with the `n/a` inline-noted as
    "not separately disclosed" — but only after a re-dispatch
    round that confirmed the cell is genuinely untaggable.
