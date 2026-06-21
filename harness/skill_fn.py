# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.skill_fn`` -- Semantic-Kernel-style per-skill callables.

A **SKILL** is the unit of reusable domain competence in this harness:

    SKILL = SKILL.md (markdown instructions the planner reads)
          + Python bindings (callables the runtime dispatches)

This module owns the *Python bindings* half. A skill author writes their
function next to its ``SKILL.md`` under ``<package>/skills/<slug>/impl.py``
and decorates it with :func:`skill_fn`, attaching an explicit JSON Schema
that the model sees as the dispatch contract. The loader (the folder
``skills_loader``) imports each ``impl.py`` so the decorated functions
register themselves into :data:`_REGISTRY` keyed by ``(skill_id, fn_name)``.
At runtime the ``invoke_skill_fn`` tool looks the binding up and calls it
with the model-supplied args.

This module is domain-agnostic. The decorator does not know about
any specific corpus or vertical -- those live in the consumer package.

Why an explicit ``parameters`` dict on the decorator rather than
introspecting type hints: a tool's per-field ``description`` prose
(e.g. "300 = 3 percentage points") is what the model actually keys off
when it picks args. Type-hint introspection would lose that prose. The
decorator is the bridge that keeps it where the model sees it -- rendered
into the loaded-skill block alongside the markdown playbook.

The registry is process-scoped. Skill files are immutable per deploy so
re-registration on the same key is treated as a programmer error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional


@dataclass(frozen=True)
class SkillFn:
    """One dispatchable skill callable.

    - ``skill_id`` -- the slug of the owning skill folder.
    - ``name`` -- the function name the model passes to invoke_skill_fn.
    - ``description`` -- prose the model sees in the loaded-skill block;
      mirrors a tool's ``description`` in shape.
    - ``parameters`` -- JSON Schema (OpenAI-tool-shape) for the args.
      Used to render the schema into the loaded block AND to validate
      the model's args at dispatch time.
    - ``fn`` -- the actual Python callable invoked with ``**args``.
    """

    skill_id: str
    name: str
    description: str
    parameters: Mapping[str, Any]
    fn: Callable[..., Any]


_REGISTRY: dict[tuple[str, str], SkillFn] = {}


def skill_fn(
    *,
    skill_id: str,
    name: Optional[str] = None,
    description: str,
    parameters: Mapping[str, Any],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn`` as the ``(skill_id, name)`` skill callable.

    ``name`` defaults to the decorated function's ``__name__``. The
    decorator returns the original callable unchanged so the impl
    module can still call its own functions directly (e.g. when one
    skill fn composes another).

    Raises ``ValueError`` on duplicate registration -- in-process
    re-decoration of the same key would silently shadow the prior
    entry, which is almost always a bug (typo in skill_id, or the
    same impl.py imported twice under different module paths).
    """
    def _decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn_name = name or fn.__name__
        key = (skill_id, fn_name)
        if key in _REGISTRY:
            raise ValueError(
                f"skill_fn {fn_name!r} already registered for skill "
                f"{skill_id!r}"
            )
        _REGISTRY[key] = SkillFn(
            skill_id=skill_id,
            name=fn_name,
            description=description,
            parameters=parameters,
            fn=fn,
        )
        return fn

    return _decorate


def get(skill_id: str, fn_name: str) -> Optional[SkillFn]:
    """Return the registered ``SkillFn`` or ``None`` if absent."""
    return _REGISTRY.get((skill_id, fn_name))


def fns_for(skill_id: str) -> tuple[SkillFn, ...]:
    """Return all callables registered for ``skill_id``, name-sorted."""
    return tuple(
        sorted(
            (v for (sid, _), v in _REGISTRY.items() if sid == skill_id),
            key=lambda f: f.name,
        )
    )


def clear() -> None:
    """Drop the registry (test-only)."""
    _REGISTRY.clear()


__all__ = ["SkillFn", "skill_fn", "get", "fns_for", "clear"]
