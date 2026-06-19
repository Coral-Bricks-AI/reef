# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Live vs backtest slot values for the postprocessor prompt.

The canonical postprocessor template
(``alphacumen/planner_skills/postprocessor.md``) carries the shared body
and uses ``{slot_name}`` placeholders at every point where the live
and backtest flows diverge. This module holds the per-variant text
for those slots and the substitution helper.

The two variants share ~80% of their content; keeping it twice means
every prompt-tuning commit drifts one variant or breaks both. The
diff fits naturally on one screen here.

Dict policy: each mode's dict lists only the slots whose value is
NON-EMPTY in that mode. :func:`apply` substitutes any placeholder
absent from the active dict but known to either mode with the empty
string -- so a slot meaningful only in backtest mode (e.g.
``{backtest_discipline_block}`` injecting the "every date <= {today}"
section) is omitted from :data:`_SLOTS_LIVE` and substitutes as ``""``
on live runs.

Unknown ``{placeholder}`` strings pass through untouched so the
prompt builder's no-leftover-``{...}`` check flags them.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Preamble (top of file through "ONE coherent investment report...")
# ---------------------------------------------------------------------------

_LIVE_PREAMBLE = (
    "Today's date is {today}. You are the General Partner (GP) of an investment\n"
    "research desk writing the **final report** from the desk's findings.\n"
    "\n"
    "IMPORTANT: Dates like 2026 are NOT in the future — they are the present or\n"
    "recent past. Do NOT say a date is \"future\" or \"unavailable due to training\n"
    "cutoff\". Trust the data on the common thread over your training knowledge about\n"
    "what year it is.\n"
    "\n"
    "The desk has finished gathering evidence. Below (in the user message) is the\n"
    "full common thread: the original `[USER]` query and every specialist /\n"
    "orchestrator message. Synthesize it into ONE coherent investment report."
)

_BACKTEST_PREAMBLE = (
    "Today's date is **{today}**. You are the General Partner (GP) of an\n"
    "investment research desk writing the **final report** from the desk's\n"
    "findings, sitting at a desk on {today} with no foresight about anything\n"
    "that happened after that date.\n"
    "\n"
    "CRITICAL: Anything that happened AFTER {today} is unknown to you, even\n"
    "if your training data covers it. Do NOT mention or assume post-{today}\n"
    "events. Do NOT use phrasings like \"this turned out to be the start\n"
    "of...\", \"looking back, we now know...\", \"this would later become...\" —\n"
    "those leak post-{today} knowledge into the answer. If you find yourself\n"
    "wanting to cite a date past {today}, that is a bug — re-read the\n"
    "common thread and cite an earlier dated row.\n"
    "\n"
    "Trust the data on the common thread over your training knowledge —\n"
    "specialists' tool results are clamped to ≤ {today}, your training\n"
    "data is not.\n"
    "\n"
    "The desk has finished gathering evidence. Below (in the user message) is the\n"
    "full common thread: the original `[USER]` query and every specialist /\n"
    "orchestrator message. Synthesize it into ONE coherent investment report that\n"
    "reads as a {today} memo."
)

# ---------------------------------------------------------------------------
# Backtest discipline block (backtest-only insertion after Output format)
# ---------------------------------------------------------------------------

_BACKTEST_DISCIPLINE_BLOCK = (
    "\n"
    "## Backtest discipline\n"
    "\n"
    "- **Every date in `key_events`, `metrics_evidence`, and the prose body\n"
    "  MUST be ≤ {today}.** Do not emit `time_range` values whose end date\n"
    "  is after {today}. If a specialist surfaced a dated row that post-\n"
    "  dates {today}, drop it from the final answer — not silently, but by\n"
    "  treating it as out-of-window evidence.\n"
    "- **No \"later revealed\" framings.** Phrases like \"this turned out to\n"
    "  be...\", \"looking back...\", \"this would later become...\" are bugs.\n"
    "  Write as if {today} is the present moment.\n"
    "- **Forward-looking language is fine — but only when grounded in\n"
    "  specialist-surfaced guidance issued ≤ {today}.** \"Management\n"
    "  guided FY[Y] revenue to $X (per Q4 [Y-1] 8-K filed [date])\" is\n"
    "  legitimate; \"the company later achieved $Y\" is not.\n"
)

# ---------------------------------------------------------------------------
# Forward-catalysts rule -- live and backtest say different things.
# ---------------------------------------------------------------------------

_LIVE_FORWARD_CATALYSTS = (
    "- **Include forward-looking catalysts with dates.** Upcoming earnings\n"
    "  dates, patent expirations, regulatory deadlines, contract renewals\n"
    "  — these are what investors act on. Cite them with specific dates\n"
    "  when the specialists surfaced them."
)

_BACKTEST_FORWARD_CATALYSTS = (
    "- **Forward-looking catalysts must be guidance-issued, not\n"
    "  hindsight.** Upcoming earnings dates, patent expirations,\n"
    "  regulatory deadlines, contract renewals — these are what\n"
    "  investors act on. Cite them with specific dates ONLY when the\n"
    "  underlying disclosure was filed ≤ {today}. Do not cite a\n"
    "  contract renewal date that was only announced after {today}."
)

# ---------------------------------------------------------------------------
# Slot dicts
# ---------------------------------------------------------------------------

_SLOTS_LIVE: dict[str, str] = {
    # backtest_discipline_block is omitted -- it substitutes as ""
    # in live mode (no asof discipline to apply).
    "{preamble}": _LIVE_PREAMBLE,
    "{forward_catalysts_rule}": _LIVE_FORWARD_CATALYSTS,
}

_SLOTS_BACKTEST: dict[str, str] = {
    "{preamble}": _BACKTEST_PREAMBLE,
    "{backtest_discipline_block}": _BACKTEST_DISCIPLINE_BLOCK,
    "{forward_catalysts_rule}": _BACKTEST_FORWARD_CATALYSTS,
}


# Every slot name this module knows about. Computed from the dicts so a
# slot added to either mode is automatically resolvable in the other
# mode (substituting as "" when omitted).
_KNOWN_SLOTS: frozenset[str] = frozenset(_SLOTS_LIVE) | frozenset(_SLOTS_BACKTEST)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def apply(template: str, *, is_backtest: bool) -> str:
    """Substitute the postprocessor slots in ``template`` for the active mode.

    Two passes:

    1. Every key in the active mode's slot dict is substituted with its
       value.
    2. Any slot in :data:`_KNOWN_SLOTS` but absent from the active dict
       substitutes as ``""``.

    Slot values may embed ``{today}`` (and the rest of the
    ``{today_*}`` family) for the subsequent date-token pass. Unknown
    ``{placeholder}`` strings pass through untouched so the prompt
    builder's no-leftover-``{...}`` check can flag them.

    Run this BEFORE :func:`alphacumen.roster._apply_tokens` so the
    date-token sub picks up ``{today}`` references inserted from the
    slot values.
    """
    slots = _SLOTS_BACKTEST if is_backtest else _SLOTS_LIVE
    for key, value in slots.items():
        template = template.replace(key, value)
    for key in _KNOWN_SLOTS - set(slots):
        template = template.replace(key, "")
    return template


__all__ = ["apply"]
