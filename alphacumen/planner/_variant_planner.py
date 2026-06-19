# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Live vs backtest slot values for the planner seed prompt.

The canonical planner template (``alphacumen/planner_skills/planner_seed.md``)
carries the shared body and uses ``{slot_name}`` placeholders at every
point where the live and backtest flows diverge. This module holds the
per-variant text for those slots and the substitution helper.

Roster and skill set are the SAME in live and backtest -- only the
asof-anchored slot text differs. The ``{roster_brief}`` and
``{skill_index}`` placeholders are substituted by the caller
(:func:`alphacumen.swarm._planner_system_prompt`) AFTER this module runs,
identically in both modes.

Why slots-in-Python instead of two .md files: the two variants share
~80% of their content; keeping it twice means every prompt-tuning
commit drifts one variant or breaks both. The diff fits naturally on
one screen here, which is the right granularity for review.

Dict policy: each mode's dict lists only the slots whose value is
NON-EMPTY in that mode. :func:`apply` substitutes any placeholder
absent from the active dict but known to either mode with the empty
string -- so a slot that's only meaningful in backtest mode (e.g.
``{first_turn_tail}`` appending an asof anchor) appears in
:data:`_SLOTS_BACKTEST` and is omitted from :data:`_SLOTS_LIVE`. The
live dict then reads as "things non-trivially different from the
bare template".

Unknown ``{placeholder}`` strings (typos, renamed slots not
propagated) pass through untouched so the no-leftover-``{...}`` check
in the prompt builder flags them.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Preamble (top of file through "your job is to:")
# ---------------------------------------------------------------------------

_LIVE_PREAMBLE = (
    "Today's date is {today}. You are the General Partner (GP) orchestrating an\n"
    "investment research desk.\n"
    "\n"
    "IMPORTANT: Dates like 2026 are NOT in the future — they are the present\n"
    "or recent past. Do NOT say a date is \"future\" or \"unavailable due to\n"
    "training cutoff\". Trust data returned by specialists over your training\n"
    "knowledge about what year it is.\n"
    "\n"
    "You manage a team of specialist analysts. Your job is to:"
)

_BACKTEST_PREAMBLE = (
    "Today's date is **{today}**. Reason as a General Partner sitting at a\n"
    "desk on {today}, with no foresight about anything that happened after\n"
    "that date. The platform has constrained every retrieval call your\n"
    "specialists issue to ``time <= {today}``; any data they return is what\n"
    "was knowable on {today}.\n"
    "\n"
    "You are the General Partner (GP) orchestrating an investment research\n"
    "desk.\n"
    "\n"
    "CRITICAL: Anything that happened AFTER {today} is unknown to you,\n"
    "even if your training data covers it. Do NOT mention or assume\n"
    "post-{today} events. Do NOT use phrasings like \"this turned out to be\n"
    "the start of...\", \"looking back, we now know...\", \"this would later\n"
    "become...\" — those leak post-{today} knowledge into the answer. If\n"
    "you find yourself wanting to cite a date past {today}, that is a\n"
    "bug — re-read the specialists' findings and cite an earlier dated\n"
    "row.\n"
    "\n"
    "Trust data returned by specialists over your training knowledge —\n"
    "specialists' tool results are clamped to ≤ {today}, your training\n"
    "data is not.\n"
    "\n"
    "You manage a team of specialist analysts. Your job is to:"
)

# ---------------------------------------------------------------------------
# Framing-mismatch rule -- diverges at the trailing paragraph.
# ---------------------------------------------------------------------------

_LIVE_FRAMING_MISMATCH_TAIL = (
    " Generic re-instructions\n"
    "  (\"search for other transactions\") fail for the same reason the\n"
    "  original generic instruction failed. Before re-dispatching,\n"
    "  also scan the prior round's specialists for retrieved article\n"
    "  URLs / titles that obviously cover the right event but came\n"
    "  from the wrong bucket (e.g. `vc_analyst` was forbidden from\n"
    "  quoting figures); if any look promising, name those URLs in\n"
    "  the follow-up so the figure extractor can `get_full_text` on\n"
    "  them directly. Bias the follow-up window toward the most\n"
    "  recent date range not yet covered. Only converge once you have\n"
    "  either (a) found a transaction whose mechanics match the\n"
    "  user's framing, or (b) exhaustively confirmed no such\n"
    "  transaction exists in the searchable window — in which case\n"
    "  the final answer must explicitly note the framing mismatch and\n"
    "  the windows searched, not silently substitute a different\n"
    "  deal. Triggers: your own draft reasoning contains phrases like\n"
    "  \"the user's framing is incorrect\", \"actually the consideration\n"
    "  flowed the other way\", \"this was not an acquisition but a\n"
    "  merger\", or notes that a specialist (typically\n"
    "  `news_quant_analyst`) \"focused on the [wrong] transaction\" or\n"
    "  \"anchored on $[wrong figure]\"."
)

_BACKTEST_FRAMING_MISMATCH_TAIL = (
    " Bias the follow-up\n"
    "  window toward the most recent date range not yet covered, but\n"
    "  always ≤ {today}. Only converge once you have either (a) found\n"
    "  a transaction whose mechanics match the user's framing, or (b)\n"
    "  exhaustively confirmed no such transaction exists in the\n"
    "  searchable window — in which case the final answer must\n"
    "  explicitly note the framing mismatch and the windows searched,\n"
    "  not silently substitute a different deal."
)

# ---------------------------------------------------------------------------
# Slot dicts
# ---------------------------------------------------------------------------

_SLOTS_LIVE: dict[str, str] = {
    # Slots NOT listed below substitute as "" (their backtest-only
    # appendages do not apply in live mode): orchestrate_tail,
    # fiscal_resolution_tail, first_turn_tail.
    "{preamble}": _LIVE_PREAMBLE,
    "{converge_clause}": "synthesize them into a final answer.",
    "{instruction_task_example}": "Specific research task.",
    "{intent_period_phrase}": "the calendar period",
    "{intent_good_example_tail}": "filed late February 2025)",
    "{framing_mismatch_tail}": _LIVE_FRAMING_MISMATCH_TAIL,
    "{filing_queries_period}": "the calendar period",
}

_SLOTS_BACKTEST: dict[str, str] = {
    "{preamble}": _BACKTEST_PREAMBLE,
    "{orchestrate_tail}": (
        " Frame instructions in terms of what\n"
        "   was knowable on {today}; do not ask \"what is the current X\" — ask\n"
        "   \"what was X as of {today}\"."
    ),
    "{converge_clause}": (
        "hand off to the synthesis step, which will\n"
        "   produce a coherent {today} memo — no foresight, no \"later\n"
        "   revealed\", no post-{today} dates anywhere in the final answer."
    ),
    "{instruction_task_example}": "Specific research task framed as of {today}.",
    "{fiscal_resolution_tail}": (
        "\n  Every resolved window must end on or before {today}."
    ),
    "{first_turn_tail}": (
        " Anchor the instruction in {today}: \"as of\n"
        "  {today}, what is X\" rather than \"what is the current X\"."
    ),
    "{intent_period_phrase}": "the calendar period bounded by {today}",
    "{intent_good_example_tail}": "filed late February 2025, ≤ {today})",
    "{framing_mismatch_tail}": _BACKTEST_FRAMING_MISMATCH_TAIL,
    "{filing_queries_period}": "the calendar period (ending ≤ {today})",
}


# Every slot name this module knows about. Computed from the dicts so a
# slot added to either mode is automatically resolvable in the other
# mode (substituting as "" when omitted).
_KNOWN_SLOTS: frozenset[str] = frozenset(_SLOTS_LIVE) | frozenset(_SLOTS_BACKTEST)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def apply(template: str, *, is_backtest: bool) -> str:
    """Substitute the planner-seed slots in ``template`` for the active mode.

    Two passes:

    1. Every key in the active mode's slot dict is substituted with its
       value.
    2. Any slot in :data:`_KNOWN_SLOTS` but absent from the active dict
       substitutes as ``""`` -- the "this slot does not apply in this
       mode" default.

    Slot values may embed ``{today}`` (and the rest of the
    ``{today_*}`` family) for the subsequent date-token pass. Unknown
    ``{placeholder}`` strings -- typos, renamed slots not propagated --
    pass through untouched so the prompt builder's no-leftover-``{...}``
    check can flag them.

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
