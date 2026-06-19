# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.skill_tools`` -- model-facing tools for skill dispatch.

A SKILL in this harness is ``SKILL.md`` (markdown instructions the
planner reads) + Python bindings registered via ``@skill_fn`` (see
:mod:`harness.skill_fn`). The three tools in this module are
the model's interface to that machinery:

- :data:`INVOKE_SKILL_FN` -- dispatch to a ``@skill_fn``-decorated
  callable by ``(skill_id, fn)``, passing model-supplied args.
- :data:`LOAD_SKILLS` -- pull one or more **folder-shaped** skill
  bodies (``SKILL.md`` + ``impl.py``) into the specialist's thread.
- :data:`LOAD_PLANNER_SKILLS` -- pull one or more **flat** skill
  bodies (planner-side ``*.md`` routing playbooks) into the planner's
  thread.

The two ``LOAD_*`` tools are kept as separate model-facing names even
though they share machinery: the planner's skill registry (routing
playbooks) and the sector specialist's skill registry (retrieval
recipes + their Python impl) are intentionally distinct so each side
sees only what's relevant to its turn.

All three tools currently call into ``alphacumen.skills`` (flat loader) and
``alphacumen.skills`` (folder loader). Those modules sit one layer
below the harness in the current package shape -- they are
parameterized loaders that happen to point at the alphacumen-side
skill directories. Future cleanup will move the loaders themselves
into :mod:`harness.skills_loader` and route the alphacumen
directories in via a registry arg.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from harness.tool import Tool


# ---------------------------------------------------------------------------
# invoke_skill_fn
# ---------------------------------------------------------------------------

def _do_invoke_skill_fn(
    skill_id: str,
    fn: str,
    args: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Dispatch to a registered ``@skill_fn`` callable.

    Returns a ``{"error": ...}`` envelope (rather than raising) when
    the lookup fails or the args fail a basic shape check, so the
    ReAct loop can show the model the error and let it self-correct.
    Validation is intentionally light: required-field presence only.
    Per-field type / pattern validation would need a JSON-Schema
    library and the underlying callables already do their own
    type-coercion.
    """
    # Late import: the registry contents depend on the alphacumen-side
    # impl modules having been imported, which only happens after the
    # folder loader runs. Import here so framework load doesn't pull
    # the alphacumen skill folder in.
    from harness import skill_fn as _skill_fn  # noqa: PLC0415

    if not isinstance(skill_id, str) or not skill_id:
        return {"error": "skill_id must be a non-empty string"}
    if not isinstance(fn, str) or not fn:
        return {"error": "fn must be a non-empty string"}
    args = dict(args or {})

    entry = _skill_fn.get(skill_id, fn)
    if entry is None:
        return {
            "error": (
                f"no skill_fn registered for skill_id={skill_id!r} "
                f"fn={fn!r}"
            )
        }

    required = list(entry.parameters.get("required") or [])
    missing = [k for k in required if k not in args]
    if missing:
        return {
            "error": (
                f"missing required args for {skill_id}.{fn}: {missing}"
            )
        }

    try:
        return entry.fn(**args)
    except TypeError as exc:
        return {
            "error": (
                f"{skill_id}.{fn} arg mismatch: {exc!s}"
            )
        }


INVOKE_SKILL_FN = Tool(
    name="invoke_skill_fn",
    description=(
        "Dispatch to a per-skill Python callable. The model picks "
        "(skill_id, fn) from the currently loaded skill block -- each "
        "loaded folder-shaped skill renders the callables it ships, "
        "with name + description + an args JSON Schema. Pass the "
        "required + optional args under `args` as a JSON object. "
        "Returns the callable's result envelope verbatim (or "
        "{\"error\": ...} when the lookup or shape check fails)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": (
                    "Slug of the loaded skill that owns the callable, "
                    "e.g. 'debt_refi_impact'."
                ),
            },
            "fn": {
                "type": "string",
                "description": (
                    "Name of the registered callable, e.g. "
                    "'compute_debt_refi_impact'."
                ),
            },
            "args": {
                "type": "object",
                "description": (
                    "Arguments object for the callable. Shape is "
                    "documented in the loaded-skill block's `args "
                    "schema` JSON Schema -- match it field-for-field."
                ),
            },
        },
        "required": ["skill_id", "fn", "args"],
    },
    fn=_do_invoke_skill_fn,
)


# ---------------------------------------------------------------------------
# load_skills -- specialist-side (folder skills)
# ---------------------------------------------------------------------------

def _do_load_skills(skill_ids: Sequence[str]) -> Any:
    """Return the rendered ``=== LOADED SKILLS ===`` block for ``skill_ids``.

    Unknown ids are dropped silently (mirrors the loader's
    ``validate_ids``); the rendered block surfaces what was loaded so
    the model can see which of its requested ids landed.
    """
    from alphacumen.skill_registry import render_loaded  # noqa: PLC0415

    if not skill_ids:
        return {"error": "skill_ids must be a non-empty list"}
    block = render_loaded(list(skill_ids))
    if not block:
        return {
            "error": (
                f"no known skills in {list(skill_ids)!r} -- check the "
                "skill index in your seed for valid ids."
            )
        }
    return block


LOAD_SKILLS = Tool(
    name="load_skills",
    description=(
        "Pull one or more skill playbook bodies into the thread so you "
        "can read the recipe before composing retrieval. Pass a list "
        "of skill ids from the index in your seed. Returns the "
        "rendered `=== LOADED SKILLS ===` block; follow each loaded "
        "playbook verbatim. For folder-shaped (callable) skills the "
        "loaded block includes the `invoke_skill_fn` dispatch schema "
        "for the callable -- follow that to execute."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill ids to load, drawn from the seed's skill "
                    "index. Multi-skill loads are additive."
                ),
            },
        },
        "required": ["skill_ids"],
    },
    fn=_do_load_skills,
)


# ---------------------------------------------------------------------------
# load_planner_skills -- planner-side (flat skills)
# ---------------------------------------------------------------------------

def _do_load_planner_skills(skill_ids: Sequence[str]) -> Any:
    from alphacumen.skills import render_loaded  # noqa: PLC0415

    if not skill_ids:
        return {"error": "skill_ids must be a non-empty list"}
    block = render_loaded(list(skill_ids))
    if not block:
        return {
            "error": (
                f"no known planner skills in {list(skill_ids)!r} -- "
                "check your skill index for valid ids."
            )
        }
    return block


LOAD_PLANNER_SKILLS = Tool(
    name="load_skills",
    description=(
        "Pull one or more planner skill playbook bodies into the "
        "thread so you can consult dispatch / routing rules before "
        "deciding the next round. Pass a list of skill ids from the "
        "index in your seed. Returns the rendered `=== LOADED "
        "SKILLS ===` block."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Planner skill ids to load. Multi-skill loads "
                    "are additive."
                ),
            },
        },
        "required": ["skill_ids"],
    },
    fn=_do_load_planner_skills,
)


__all__ = [
    "INVOKE_SKILL_FN",
    "LOAD_PLANNER_SKILLS",
    "LOAD_SKILLS",
]
