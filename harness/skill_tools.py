# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.skill_tools`` -- model-facing tools for skill dispatch.

A SKILL in this harness is ``SKILL.md`` (markdown instructions the
model reads) + Python bindings registered via ``@skill_fn`` (see
:mod:`harness.skill_fn`). The tools in this module are the model's
interface to that machinery:

- :data:`INVOKE_SKILL_FN` -- dispatch to a ``@skill_fn``-decorated
  callable by ``(skill_id, fn)``, passing model-supplied args.
- :func:`make_load_skill_tool` -- factory that returns a
  ``load_skill`` :class:`~harness.tool.Tool` bound to a caller-supplied
  loader (a function from a list of skill ids to the rendered playbook
  block). Each harness instance constructs its own ``load_skill`` tool
  this way -- no global registry, no hidden coupling between instances.

The factory pattern keeps the framework domain-agnostic: harness
ships the dispatch shape (parameters, error envelopes, description
text) and the consumer plugs in the registry. The cocktails example
in ``examples/cocktails`` shows the minimal binding; a multi-specialist
instance (e.g. a planner + specialists pair) can create one tool per
side by passing different loader functions.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence

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
    # Late import: the registry contents depend on the consumer's
    # impl modules having been imported, which only happens after the
    # folder loader runs. Import here so framework load doesn't pull
    # any specific skill folder in.
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
                    "Slug of the loaded skill that owns the callable."
                ),
            },
            "fn": {
                "type": "string",
                "description": (
                    "Name of the registered callable."
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
# load_skill -- factory
# ---------------------------------------------------------------------------

LoadFn = Callable[[Sequence[str]], str]
"""A function from a list of skill ids to a rendered playbook block.

Typically a thin wrapper around :func:`harness.skills_loader.render_loaded`
that closes over the caller's ``SKILLS`` dict. Returns ``""`` (empty
string) when none of the ids resolve, so the factory can surface a
helpful error to the model.
"""


def make_load_skill_tool(
    load_fn: LoadFn,
    *,
    name: str = "load_skill",
    description: Optional[str] = None,
) -> Tool:
    """Build a ``load_skill`` :class:`Tool` bound to ``load_fn``.

    ``load_fn`` is a function from a list of skill ids to a rendered
    block of skill bodies (the ``=== LOADED SKILLS ===`` block the
    model reads). Most callers wire this to
    :func:`harness.skills_loader.render_loaded` closed over their own
    ``SKILLS`` dict::

        from harness.skills_loader import load_skills, render_loaded
        from harness.skill_tools import make_load_skill_tool

        SKILLS = load_skills("./skills")
        LOAD_SKILL = make_load_skill_tool(
            lambda ids: render_loaded(list(ids), skills=SKILLS)
        )

    ``name`` and ``description`` are overridable so a multi-side
    harness (planner + specialist) can ship two distinct tool names
    pointing at distinct registries.
    """

    def _do_load(skill_ids: Sequence[str]) -> Any:
        if not skill_ids:
            return {"error": "skill_ids must be a non-empty list"}
        block = load_fn(list(skill_ids))
        if not block:
            return {
                "error": (
                    f"no known skills in {list(skill_ids)!r} -- check "
                    "the skill index in your system prompt for valid ids."
                )
            }
        return block

    return Tool(
        name=name,
        description=description or (
            "Pull one or more skill playbook bodies into the thread so "
            "you can read the recipe before composing the dispatch. "
            "Pass a list of skill ids from the index in your system "
            "prompt. Returns the rendered `=== LOADED SKILLS ===` "
            "block; follow each loaded playbook verbatim. For "
            "folder-shaped (callable) skills the loaded block includes "
            "the `invoke_skill_fn` dispatch schema for the callable -- "
            "follow that to execute."
        ),
        parameters={
            "type": "object",
            "properties": {
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Skill ids to load, drawn from the index in "
                        "your system prompt. Multi-skill loads are "
                        "additive."
                    ),
                },
            },
            "required": ["skill_ids"],
        },
        fn=_do_load,
    )


__all__ = [
    "INVOKE_SKILL_FN",
    "LoadFn",
    "make_load_skill_tool",
]
