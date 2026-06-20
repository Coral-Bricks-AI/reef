#!/usr/bin/env python3
# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Skinny end-to-end example: one specialist, two skills, BM25 + ABV math.

Usage::

    export OPENAI_API_KEY=sk-...
    python harness/examples/cocktails/ask.py "What's in a Negroni and how strong is it?"

The framework hello-world. No planner, no synthesizer, no SpecialistConfig --
just :func:`harness.react.run_react` wired to a persona prompt and two
skill-dispatch tools that close over this example's skills dict.

The framework's ``harness.skill_tools.LOAD_SKILLS`` hard-codes a registry from
``alphacumen.skill_registry``. The skinny example owns its own ``load_skills``
tool that resolves against its local registry; ``invoke_skill_fn`` is reused
straight from the framework (the ``@skill_fn`` decorator registers into a
process-global registry that this example's ``impl.py`` modules populate
when ``load_skills(...)`` imports them).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

from harness.react import run_react
from harness.skills_loader import load_skills, render_index, render_loaded
from harness.skill_tools import INVOKE_SKILL_FN
from harness.tool import Tool

HERE = Path(__file__).resolve().parent

# Load this example's skills and import their impl.py modules. The import
# pass registers the @skill_fn-decorated callables with the global registry
# that harness.skill_tools.INVOKE_SKILL_FN dispatches against.
SKILLS = load_skills(
    HERE / "skills",
    module_prefix="harness.examples.cocktails._skills",
)


def _do_load_skills(skill_ids: Sequence[str]):
    """Local load_skills that resolves against this example's SKILLS dict."""
    if not skill_ids:
        return {"error": "skill_ids must be a non-empty list"}
    block = render_loaded(list(skill_ids), skills=SKILLS)
    if not block:
        return {
            "error": (
                f"no known skills in {list(skill_ids)!r} -- "
                f"valid ids: {sorted(SKILLS)}"
            )
        }
    return block


LOAD_SKILLS_LOCAL = Tool(
    name="load_skills",
    description=(
        "Pull one or more skill playbook bodies into the thread. For folder-"
        "shaped (callable) skills the loaded block includes the "
        "`invoke_skill_fn` dispatch schema -- follow that to execute."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Skill ids from the index in the system prompt.",
            },
        },
        "required": ["skill_ids"],
    },
    fn=_do_load_skills,
)


# Render the skill index once at module load and stitch it into the persona.
_INDEX = render_index(SKILLS)
_PROMPT = (HERE / "bartender.md").read_text(encoding="utf-8").replace(
    "{skill_index}", _INDEX
)


def ask(question: str, model: str = "openai/gpt-4o-mini") -> str | None:
    """Run the bartender on one question; return its final natural-language answer."""
    traj = run_react(
        model=model,
        system_prompt=_PROMPT,
        user_message=question,
        tools=[LOAD_SKILLS_LOCAL, INVOKE_SKILL_FN],
        max_steps=6,
        log_label="cocktails.bartender",
    )
    if traj.final_message is None:
        return None
    return traj.final_message.get("content") or ""


if __name__ == "__main__":
    if "OPENAI_API_KEY" not in os.environ:
        print(
            "Set OPENAI_API_KEY before running this example:\n"
            "    export OPENAI_API_KEY=sk-...\n",
            file=sys.stderr,
        )
        sys.exit(2)
    q = " ".join(sys.argv[1:]) or "What's in a Negroni and how strong is it?"
    print(f"Q: {q}\n")
    answer = ask(q)
    print(f"A: {answer}")
