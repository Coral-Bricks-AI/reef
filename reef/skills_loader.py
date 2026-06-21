# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``reef.skills_loader`` -- generic folder-skill loader.

The framework half of the SKILL contract:

    SKILL = SKILL.md (markdown instructions the planner reads)
          + Python bindings (callables the runtime dispatches)

This module owns the *loader*: given a directory containing flat
``<slug>.md`` files and/or folder-shaped ``<slug>/SKILL.md`` (+ optional
``impl.py``) trees, it parses each into a :class:`Skill` and -- when an
``impl.py`` is present -- imports it so the ``@skill_fn`` decorator
registers the callables for :mod:`reef.skill_tools` to
dispatch on.

Domain-agnostic. Each consumer passes ``skills_dir`` and
``module_prefix`` at the loader call; the framework owns no
default registry. See ``examples/cocktails`` for the minimal
binding.

Two on-disk shapes are supported:

- **Flat** ``<slug>.md`` -- prose-only routing playbook. No Python.
- **Folder** ``<slug>/SKILL.md`` -- prose + optional ``impl.py`` whose
  ``@skill_fn``-decorated callables register at load time.

A given slug may not appear in both shapes; a duplicate id raises
``ValueError`` so a half-finished migration fails loudly.

The frontmatter parser is deliberately tiny (no PyYAML dependency);
skill files use a fixed flat shape and any deviation fails fast.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from reef import skill_fn as _skill_fn


# Tokens we don't want polluting the keyword-overlap heuristic.
_STOPWORDS = frozenset(
    """a an the of for to in on and or is are be by with from as at that this
    query asks about issuer company x y n it its their when whether one more
    not no into over per each every may must should via etc""".split()
)


@dataclass(frozen=True)
class Skill:
    """One loadable specialist routing playbook.

    - ``id`` -- stable slug, what the dispatcher lists when loading the
      skill into a specialist round.
    - ``when`` -- the one-line trigger shown in the index; also the
      corpus the keyword heuristic matches a query against.
    - ``applies_to`` -- specialists the skill is designed for
      (informational; not enforced by the loader).
    - ``body`` -- the verbatim playbook text injected when the skill
      is loaded.
    - ``source_lines`` -- provenance back into the source corpus (audit
      only).
    - ``has_impl`` -- folder-shaped skill ships an ``impl.py`` with
      ``@skill_fn``-decorated callables that ``invoke_skill_fn`` will
      dispatch to. Flat ``<slug>.md`` skills leave this ``False``.
    """

    id: str
    when: str
    applies_to: tuple[str, ...]
    body: str
    source_lines: str
    has_impl: bool = False


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

_FM_LIST_RE = re.compile(r"^\[(.*)\]$")


def _parse_frontmatter(
    text: str, *, path: Path, has_impl: bool = False,
) -> Skill:
    if not text.startswith("---\n"):
        raise ValueError(f"{path.name}: missing frontmatter opener")
    _, fm, body = text.split("---\n", 2)
    meta: dict[str, str] = {}
    for line in fm.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"{path.name}: bad frontmatter line {line!r}")
        key, _, val = line.partition(":")
        meta[key.strip()] = val.strip()

    missing = {"id", "when", "applies_to"} - meta.keys()
    if missing:
        raise ValueError(f"{path.name}: frontmatter missing {sorted(missing)}")

    applies_raw = meta["applies_to"]
    m = _FM_LIST_RE.match(applies_raw)
    applies = (
        tuple(x.strip() for x in m.group(1).split(",") if x.strip())
        if m
        else (applies_raw,)
    )

    return Skill(
        id=meta["id"],
        when=meta["when"],
        applies_to=applies,
        body=body.strip("\n"),
        source_lines=meta.get("source_lines", ""),
        has_impl=has_impl,
    )


def _import_impl_module(
    skill_id: str, impl_path: Path, module_prefix: str,
) -> None:
    """Import ``impl.py`` for a folder-shaped skill.

    Uses ``importlib.util.spec_from_file_location`` so the file is
    loadable whether the package was installed via ``pip install -e .``
    or as a wheel under ``site-packages``. The module is registered
    into ``sys.modules`` under ``<module_prefix>.<skill_id>._impl`` so
    the impl module's own intra-package imports resolve.

    Skipped silently if the same synthetic name is already imported --
    re-running with a cleared loader cache would otherwise hit
    ``@skill_fn``'s duplicate-key guard. Cache invalidation is the
    loader's contract, not importlib's.
    """
    mod_name = f"{module_prefix}.{skill_id}._impl"
    if mod_name in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(mod_name, str(impl_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build import spec for {impl_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)


# ---------------------------------------------------------------------------
# Registry -- cached per (skills_dir, module_prefix)
# ---------------------------------------------------------------------------

_LOADER_CACHE: dict[tuple[str, str], dict[str, Skill]] = {}


def load_skills(
    skills_dir: Path,
    *,
    module_prefix: str = "reef._skills_impl",
) -> dict[str, Skill]:
    """Return ``{id: Skill}`` for every skill file under ``skills_dir``.

    Walks the directory in two passes -- flat ``*.md`` first, then
    folder-shaped ``<slug>/SKILL.md`` -- and imports each folder's
    ``impl.py`` if present.

    Parameters
    ----------
    skills_dir : Path
        Directory containing the skill files. Each consumer passes
        its own directory.
    module_prefix : str
        Synthetic-module-name prefix used when importing each
        folder's ``impl.py``. Defaults to a framework-namespaced
        prefix; consumers with multiple registries should override
        so the registries coexist without collisions.

    Cached for the process lifetime, keyed by
    ``(resolve(skills_dir), module_prefix)``. Call :func:`clear_cache`
    after regenerating in a dev loop.
    """
    key = (str(skills_dir.resolve()), module_prefix)
    cached = _LOADER_CACHE.get(key)
    if cached is not None:
        return cached

    if not skills_dir.is_dir():
        raise FileNotFoundError(f"skills dir not found: {skills_dir}")

    out: dict[str, Skill] = {}

    # Flat .md skills first.
    for path in sorted(skills_dir.glob("*.md")):
        skill = _parse_frontmatter(
            path.read_text(encoding="utf-8"), path=path,
        )
        if skill.id in out:
            raise ValueError(
                f"duplicate skill id {skill.id!r} ({path.name})"
            )
        if skill.id != path.stem:
            raise ValueError(
                f"skill id {skill.id!r} != filename {path.stem!r} "
                f"({path.name})"
            )
        out[skill.id] = skill

    # Folder-shaped skills (SKILL.md + optional impl.py).
    for folder in sorted(skills_dir.iterdir()):
        if not folder.is_dir() or folder.name.startswith("_"):
            continue
        skill_md = folder / "SKILL.md"
        if not skill_md.is_file():
            continue
        impl_py = folder / "impl.py"
        skill = _parse_frontmatter(
            skill_md.read_text(encoding="utf-8"),
            path=skill_md,
            has_impl=impl_py.is_file(),
        )
        if skill.id in out:
            raise ValueError(
                f"duplicate skill id {skill.id!r} "
                f"(folder {folder.name} collides with prior entry)"
            )
        if skill.id != folder.name:
            raise ValueError(
                f"skill id {skill.id!r} != folder name {folder.name!r}"
            )
        if impl_py.is_file():
            _import_impl_module(skill.id, impl_py, module_prefix)
        out[skill.id] = skill

    if not out:
        raise FileNotFoundError(f"no skill files in {skills_dir}")

    _LOADER_CACHE[key] = out
    return out


def clear_cache() -> None:
    """Drop the loader cache (test/dev only)."""
    _LOADER_CACHE.clear()


# ---------------------------------------------------------------------------
# Rendering helpers (operate on a pre-loaded skills dict; no path arg)
# ---------------------------------------------------------------------------

def render_index(skills: dict[str, Skill]) -> str:
    """Render the ``{skill_index}`` block: one ``- `id` — when`` per skill.

    Id-sorted so the rendered seed (and thus the cacheable system-prompt
    prefix) is byte-stable across runs.
    """
    return "\n".join(
        f"- `{sid}` — {skills[sid].when}" for sid in sorted(skills)
    )


def validate_ids(
    requested: Iterable[object],
    *,
    skills: dict[str, Skill],
) -> list[str]:
    """Filter a caller-supplied skill-id list to known ids, de-duped.

    Non-strings and unknown ids are dropped (order preserved).
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in requested or []:
        if not isinstance(item, str):
            continue
        sid = item.strip()
        if sid in skills and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _render_skill_fn_block(skill_id: str) -> str:
    """Render a fenced block listing every callable registered for a skill.

    Walked by :func:`render_loaded` when a folder-shaped skill has
    ``@skill_fn``-registered callables. The block carries the
    ``invoke_skill_fn`` dispatch form + each callable's JSON Schema
    so the model can build a well-typed payload.
    """
    fns = _skill_fn.fns_for(skill_id)
    if not fns:
        return ""
    parts = ["", "### Callables (dispatch via `invoke_skill_fn`)", ""]
    for fn in fns:
        parts.append(
            f"- `invoke_skill_fn(skill_id={skill_id!r}, "
            f"fn={fn.name!r}, args={{...}})`"
        )
        parts.append(f"  - {fn.description}")
        parts.append("  - args schema:")
        parts.append("    ```json")
        for line in json.dumps(fn.parameters, indent=2).splitlines():
            parts.append(f"    {line}")
        parts.append("    ```")
    return "\n".join(parts)


def render_loaded(
    ids: Sequence[str],
    *,
    skills: dict[str, Skill],
) -> str:
    """Render the ``=== LOADED SKILLS ===`` block for ``ids``.

    Empty string when no valid ids -- the caller omits the block
    entirely so a no-skills round's user message stays clean. Ids are
    emitted id-sorted for deterministic prompt prefixes.

    For folder-shaped skills (``has_impl=True``), each registered
    callable's JSON Schema is appended so the model can build a
    well-typed ``invoke_skill_fn`` payload.
    """
    valid = validate_ids(ids, skills=skills)
    if not valid:
        return ""
    parts = ["=== LOADED SKILLS ===", ""]
    for sid in sorted(valid):
        parts.append(f"<!-- skill: {sid} -->")
        parts.append(skills[sid].body)
        if skills[sid].has_impl:
            fn_block = _render_skill_fn_block(sid)
            if fn_block:
                parts.append(fn_block)
        parts.append("")
    return "\n".join(parts).rstrip("\n")


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def suggest_ids(
    query: str,
    *,
    skills: dict[str, Skill],
    top_k: int = 6,
    min_overlap: int = 2,
) -> list[str]:
    """Cheap keyword-overlap suggestion of skills for a query.

    Scores each skill by the size of the token overlap between the
    query and the skill's ``when`` trigger (+ its id). Returns at most
    ``top_k`` ids with an overlap of at least ``min_overlap``,
    best-first. Useful for test harnesses and an optional pre-seed
    step on the dispatch side; the planner can also just read the
    index and choose explicitly.
    """
    q = _tokens(query)
    scored: list[tuple[int, str]] = []
    for sid, skill in skills.items():
        corpus = _tokens(skill.when) | _tokens(sid.replace("_", " "))
        score = len(q & corpus)
        if score >= min_overlap:
            scored.append((score, sid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [sid for _, sid in scored[:top_k]]


__all__ = [
    "Skill",
    "clear_cache",
    "load_skills",
    "render_index",
    "render_loaded",
    "suggest_ids",
    "validate_ids",
]
