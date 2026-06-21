# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen.skills`` -- skill registry, index rendering, loading.

A *skill* is one markdown file under ``alphacumen/planner_skills/skills/`` with
YAML-ish frontmatter (``id`` / ``when`` / ``applies_to`` / ``source_lines``)
above a verbatim body. Skills hold the conditional, query-pattern-specific
routing playbooks that used to live inline in the 1,177-line orchestrator
prompt; the planner loads them on demand instead of carrying all of them every
round.

This module is the read side of that contract:

- :func:`load_skills` -- parse every ``*.md`` into a :class:`Skill`, cached.
- :func:`render_index` -- the ``{skill_index}`` block for the planner seed
  (one ``- `id` — when`` line per skill, id-sorted for prefix-cache
  stability).
- :func:`render_loaded` -- the ``=== LOADED SKILLS ===`` block injected into
  the planner's user message for a set of requested ids.
- :func:`validate_ids` -- filter a model-supplied ``load_skills`` list down to
  known ids (mirrors how the swarm filters ``invoke_next`` against the
  roster).
- :func:`suggest_ids` -- a cheap keyword-overlap heuristic for optionally
  pre-seeding round-1 skills (used by the test harness; the planner can also
  just read the index and choose).

The frontmatter parser is deliberately tiny (no PyYAML dependency) because the
generator (:mod:`alphacumen.planner._build_prompts`) writes a fixed, flat shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

from harness.skill_tools import make_load_skills_tool
from harness.tool import Tool

_SKILLS_DIR = Path(__file__).resolve().parent / "planner_skills"

# Tokens we don't want polluting the keyword-overlap heuristic.
_STOPWORDS = frozenset(
    """a an the of for to in on and or is are be by with from as at that this
    query asks about issuer company x y n it its their when whether one more
    not no into over per each every may must should via etc""".split()
)


@dataclass(frozen=True)
class Skill:
    """One loadable routing playbook.

    - ``id`` -- stable slug, what the planner lists in ``load_skills``.
    - ``when`` -- the one-line trigger shown in the index; also the corpus
      the keyword heuristic matches a query against.
    - ``applies_to`` -- specialists the skill tends to route to (informational
      for the planner; not enforced).
    - ``body`` -- the verbatim playbook text injected when loaded.
    - ``source_lines`` -- provenance back into the source prompt (audit only).
    """

    id: str
    when: str
    applies_to: tuple[str, ...]
    body: str
    source_lines: str


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

_FM_LIST_RE = re.compile(r"^\[(.*)\]$")


def _parse_frontmatter(text: str, *, path: Path) -> Skill:
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
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_skills() -> dict[str, Skill]:
    """Return ``{id: Skill}`` for every skill file, parsed + cached.

    Cached for the process lifetime -- skill files are immutable per deploy.
    Call :func:`load_skills.cache_clear` after regenerating in a dev loop.
    """
    if not _SKILLS_DIR.is_dir():
        raise FileNotFoundError(
            f"skills dir not found: {_SKILLS_DIR} -- run "
            "`python -m alphacumen.planner._build_prompts` first"
        )
    out: dict[str, Skill] = {}
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        skill = _parse_frontmatter(path.read_text(encoding="utf-8"), path=path)
        if skill.id in out:
            raise ValueError(f"duplicate skill id {skill.id!r} ({path.name})")
        if skill.id != path.stem:
            raise ValueError(
                f"skill id {skill.id!r} != filename {path.stem!r} ({path.name})"
            )
        out[skill.id] = skill
    if not out:
        raise FileNotFoundError(f"no skill files in {_SKILLS_DIR}")
    return out


def render_index(skills: dict[str, Skill] | None = None) -> str:
    """Render the ``{skill_index}`` block: one ``- `id` — when`` per skill.

    Id-sorted so the rendered seed (and thus the cacheable system-prompt
    prefix) is byte-stable across runs.
    """
    skills = skills or load_skills()
    return "\n".join(
        f"- `{sid}` — {skills[sid].when}" for sid in sorted(skills)
    )


def validate_ids(
    requested: Iterable[object],
    *,
    skills: dict[str, Skill] | None = None,
) -> list[str]:
    """Filter a model-supplied ``load_skills`` list to known ids, de-duped.

    Non-strings and unknown ids are dropped (order preserved). Mirrors the
    defensive filtering the swarm applies to ``invoke_next.persona_key``.
    """
    skills = skills or load_skills()
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


def render_loaded(
    ids: Sequence[str],
    *,
    skills: dict[str, Skill] | None = None,
) -> str:
    """Render the ``=== LOADED SKILLS ===`` block for the planner user message.

    Empty string when no ids -- the caller omits the block entirely so a
    no-skills round's user message stays clean. Ids are emitted id-sorted so
    repeated rounds with the same loaded set produce identical text.
    """
    valid = validate_ids(ids, skills=skills)
    if not valid:
        return ""
    skills = skills or load_skills()
    parts = ["=== LOADED SKILLS ===", ""]
    for sid in sorted(valid):
        parts.append(f"<!-- skill: {sid} -->")
        parts.append(skills[sid].body)
        parts.append("")
    return "\n".join(parts).rstrip("\n")


# ---------------------------------------------------------------------------
# Optional round-1 pre-seed heuristic
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def suggest_ids(
    query: str,
    *,
    top_k: int = 6,
    min_overlap: int = 2,
    skills: dict[str, Skill] | None = None,
) -> list[str]:
    """Cheap keyword-overlap suggestion of skills to pre-seed for round 1.

    Scores each skill by the size of the token overlap between the query and
    the skill's ``when`` trigger (+ its id). This is a non-LLM convenience for
    the test harness / an optional pre-seed step -- the planner itself can
    just read the index and decide. Returns at most ``top_k`` ids with an
    overlap of at least ``min_overlap``, best-first.
    """
    skills = skills or load_skills()
    q = _tokens(query)
    scored: list[tuple[int, str]] = []
    for sid, skill in skills.items():
        corpus = _tokens(skill.when) | _tokens(sid.replace("_", " "))
        score = len(q & corpus)
        if score >= min_overlap:
            scored.append((score, sid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [sid for _, sid in scored[:top_k]]


LOAD_PLANNER_SKILLS: Tool = make_load_skills_tool(
    lambda ids: render_loaded(list(ids)),
    description=(
        "Pull one or more planner skill playbook bodies into the "
        "thread so you can consult dispatch / routing rules before "
        "deciding the next round. Pass a list of skill ids from the "
        "index in your seed. Returns the rendered `=== LOADED "
        "SKILLS ===` block."
    ),
)
"""``load_skills`` tool bound to AlphaCumen's flat planner-skill registry.

Re-exported as :data:`alphacumen.tools.LOAD_PLANNER_SKILLS`.
"""


__all__ = [
    "LOAD_PLANNER_SKILLS",
    "Skill",
    "load_skills",
    "render_index",
    "render_loaded",
    "validate_ids",
    "suggest_ids",
]
