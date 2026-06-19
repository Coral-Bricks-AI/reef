# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen.skill_registry`` -- AlphaCumen's folder-skill registry.

Thin wrapper over the framework loader at
:mod:`harness.skills_loader` that defaults to AlphaCumen's own
``skills/`` directory and a finance-namespaced module prefix.

The public surface (``load_skills``, ``render_index``, ``render_loaded``,
``validate_ids``, ``suggest_ids``) matches the legacy ``alphacumen.skills``
API so the back-compat shim at the old path is a one-line re-export.

A non-finance harness that builds on the same SKILL contract uses
:mod:`harness.skills_loader` directly, passing its own
``skills_dir`` + ``module_prefix``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from harness import skills_loader as _loader
from harness.skills_loader import Skill


_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_MODULE_PREFIX = "alphacumen.skills._loaded"


def load_skills() -> dict[str, Skill]:
    """Return ``{id: Skill}`` for every AlphaCumen folder-skill, cached."""
    return _loader.load_skills(_SKILLS_DIR, module_prefix=_MODULE_PREFIX)


def render_index(skills: dict[str, Skill] | None = None) -> str:
    """Render the ``{skill_index}`` block. Defaults to all loaded skills."""
    return _loader.render_index(skills or load_skills())


def validate_ids(
    requested: Iterable[object],
    *,
    skills: dict[str, Skill] | None = None,
) -> list[str]:
    """Filter ``requested`` to known skill ids (preserving order)."""
    return _loader.validate_ids(requested, skills=skills or load_skills())


def render_loaded(
    ids: Sequence[str],
    *,
    skills: dict[str, Skill] | None = None,
) -> str:
    """Render the ``=== LOADED SKILLS ===`` block for ``ids``."""
    return _loader.render_loaded(ids, skills=skills or load_skills())


def suggest_ids(
    query: str,
    *,
    top_k: int = 6,
    min_overlap: int = 2,
    skills: dict[str, Skill] | None = None,
) -> list[str]:
    """Cheap keyword-overlap suggestion of skills for ``query``."""
    return _loader.suggest_ids(
        query,
        skills=skills or load_skills(),
        top_k=top_k,
        min_overlap=min_overlap,
    )


__all__ = [
    "Skill",
    "load_skills",
    "render_index",
    "render_loaded",
    "suggest_ids",
    "validate_ids",
]
