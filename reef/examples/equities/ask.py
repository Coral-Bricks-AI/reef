#!/usr/bin/env python3
# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Skinny end-to-end example: one specialist, two skills, BM25 + 1y return math.

Usage::

    export LLM_API_KEY=sk-...
    python reef/examples/equities/ask.py "How has NVDA performed over the last year?"

The framework hello-world. No planner, no synthesizer, no
SpecialistConfig -- just :func:`reef.react.run_react` wired to a
persona prompt and two skill-dispatch tools.

``make_load_skill_tool`` is the factory the framework ships for the
``load_skill`` Tool; we close it over this example's ``SKILLS`` dict.
``INVOKE_SKILL_FN`` is reused straight from the framework (the
``@skill_fn`` decorator registers into a process-global registry that
this example's ``impl.py`` modules populate when ``load_skills(...)``
imports them).

The data in ``data/companies.json`` is **mock and illustrative** — ~20
well-known tickers with fabricated point-in-time prices. Do not mistake
this for live market data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from reef.react import run_react
from reef.skills_loader import load_skills, render_index, render_loaded
from reef.skill_tools import INVOKE_SKILL_FN, make_load_skill_tool

HERE = Path(__file__).resolve().parent

# Load this example's skills and import their impl.py modules. The import
# pass registers the @skill_fn-decorated callables with the global registry
# that reef.skill_tools.INVOKE_SKILL_FN dispatches against.
SKILLS = load_skills(
    HERE / "skills",
    module_prefix="reef.examples.equities._skills",
)


LOAD_SKILL = make_load_skill_tool(
    lambda ids: render_loaded(list(ids), skills=SKILLS),
)


# Render the skill index once at module load and stitch it into the persona.
_INDEX = render_index(SKILLS)
_PROMPT = (HERE / "analyst.md").read_text(encoding="utf-8").replace(
    "{skill_index}", _INDEX
)


def ask(question: str, model: str = "openai/gpt-4o-mini") -> str | None:
    """Run the equity analyst on one question; return its final natural-language answer."""
    traj = run_react(
        model=model,
        system_prompt=_PROMPT,
        user_message=question,
        tools=[LOAD_SKILL, INVOKE_SKILL_FN],
        max_steps=6,
        log_label="equities.analyst",
    )
    if traj.final_message is None:
        return None
    return traj.final_message.get("content") or ""


if __name__ == "__main__":
    if "LLM_API_KEY" not in os.environ and "OPENAI_API_KEY" not in os.environ:
        print(
            "Set your LLM API key before running this example:\n"
            "    export LLM_API_KEY=sk-...\n",
            file=sys.stderr,
        )
        sys.exit(2)
    q = " ".join(sys.argv[1:]) or "How has NVDA performed over the last year?"
    print(f"Q: {q}\n")
    answer = ask(q)
    print(f"A: {answer}")
