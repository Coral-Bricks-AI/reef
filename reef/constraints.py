# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``reef.constraints`` -- first-class harness constraints.

LLMs are notoriously bad at time. They confidently quote a price that's a
day past the asof, paraphrase a filing into the wrong fiscal year, or
synthesize a "fact" pieced together from sources on both sides of a
cutoff. Bigger models don't fix it -- the failure mode is structural, not
capacity-bound.

The fix is to make time (and the other invariants a domain run depends
on) **first-class harness inputs**, not prose buried in a system prompt.
A :class:`HarnessConstraints` value is the public contract for one run:

    HarnessConstraints(
        asof=date(2024, 9, 30),                  # NEVER read past this
        tool_budget=50,                          # per-specialist cap
        max_rounds=8,                            # synthesizer rounds
        allowed_indices=("sec_2024q3",),         # corpus slice
        token_budget=200_000,                    # cost ceiling
    )

The enforcement venue is decoupled (see :mod:`reef.enforcement`).
A local :class:`~reef.enforcement.LocalEnforcer` runs the same
contract a multi-tenant sandbox runtime would -- the constraints object
is the seam that lets you swap one for the other without changing the
harness, the skills, or the tools.

This generalizes past finance. Anywhere a domain has a "freeze date"
(legal: statute-effective; clinical: enrollment cutoff; regulatory:
snapshot; evals: contamination prevention), :class:`HarnessConstraints`
is where you declare it and the enforcer is where you stop the LLM from
violating it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Optional, Union


AsofLike = Union[date, str, None]
"""Accepted shapes for ``asof``: a ``datetime.date``, an ISO-8601 string
(``YYYY-MM-DD`` or full datetime), or ``None`` for an unconstrained run."""


def _coerce_asof(value: AsofLike) -> Optional[date]:
    """Normalize ``asof`` to a ``date`` (or ``None``).

    Accepts a ``date`` directly, or an ISO-8601 string. Full datetimes are
    truncated to their date component -- the asof contract is day-level
    granularity; sub-day cutoffs aren't a concept any of the downstream
    finance tools honor.
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Accept "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SS" etc.
        try:
            return date.fromisoformat(s[:10])
        except ValueError as exc:
            raise ValueError(
                f"asof must be ISO-8601 (YYYY-MM-DD), got {value!r}"
            ) from exc
    raise TypeError(
        f"asof must be date | str | None, got {type(value).__name__}"
    )


@dataclass(frozen=True)
class HarnessConstraints:
    """The contract one harness run honors.

    All fields are optional with sensible defaults; pass only what the
    domain actually constrains. The runtime threads this object through
    the planner, the specialists, every tool dispatch, and the
    synthesizer -- it's the single source of truth a
    :class:`~reef.enforcement.ConstraintEnforcer` checks against.

    Fields:

    - ``asof`` -- temporal cutoff. Tools that read time-bearing data
      MUST NOT return rows dated after this. ``None`` means unconstrained
      (live mode). Stored as ``datetime.date``; pass an ISO-8601 string
      and it'll be coerced.
    - ``tool_budget`` -- max tool calls per specialist before the
      enforcer forces a final-answer round. Cost ceiling + drift guard.
    - ``max_rounds`` -- max planner/postprocessor loop iterations before
      a forced terminal synthesis. Protects against non-convergence.
    - ``allowed_indices`` -- whitelist of retrieval indices the run may
      touch. Empty tuple = no restriction (every index a specialist
      binds is allowed). Use to enforce per-tenant corpus scoping.
    - ``token_budget`` -- optional aggregate token cap (input + output)
      across the run. ``None`` = no cap. Honored by the enforcer; tools
      that emit token-counted traces consult this.
    - ``run_id`` -- optional opaque correlation id, propagated to
      observability hooks. Not enforced.
    """

    asof: Optional[date] = field(default=None)
    tool_budget: int = 50
    max_rounds: int = 8
    allowed_indices: tuple[str, ...] = ()
    token_budget: Optional[int] = None
    run_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "asof", _coerce_asof(self.asof))
        if self.tool_budget <= 0:
            raise ValueError(
                f"tool_budget must be positive, got {self.tool_budget}"
            )
        if self.max_rounds <= 0:
            raise ValueError(
                f"max_rounds must be positive, got {self.max_rounds}"
            )
        if self.token_budget is not None and self.token_budget <= 0:
            raise ValueError(
                f"token_budget must be positive or None, got "
                f"{self.token_budget}"
            )
        if not isinstance(self.allowed_indices, tuple):
            object.__setattr__(
                self, "allowed_indices", tuple(self.allowed_indices)
            )

    @property
    def asof_iso(self) -> Optional[str]:
        """``asof`` rendered as ``YYYY-MM-DD`` or ``None``.

        Convenience for legacy call sites that thread asof as a string.
        """
        return self.asof.isoformat() if self.asof else None

    def is_index_allowed(self, index: str) -> bool:
        """``True`` if ``index`` is on the allowlist (or list is empty)."""
        return not self.allowed_indices or index in self.allowed_indices

    @classmethod
    def from_legacy_kwargs(
        cls,
        *,
        asof: AsofLike = None,
        tool_budget: Optional[int] = None,
        max_rounds: Optional[int] = None,
        allowed_indices: Optional[tuple[str, ...]] = None,
        token_budget: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> "HarnessConstraints":
        """Build from the swarm.run_swarm legacy kwargs shape.

        Use this in transitional code that still accepts ``asof`` /
        ``tool_budget`` / ``max_rounds`` as loose kwargs but wants a
        single ``HarnessConstraints`` value downstream. ``None`` values
        fall back to dataclass defaults.
        """
        kw: dict = {}
        if asof is not None:
            kw["asof"] = asof
        if tool_budget is not None:
            kw["tool_budget"] = tool_budget
        if max_rounds is not None:
            kw["max_rounds"] = max_rounds
        if allowed_indices is not None:
            kw["allowed_indices"] = tuple(allowed_indices)
        if token_budget is not None:
            kw["token_budget"] = token_budget
        if run_id is not None:
            kw["run_id"] = run_id
        return cls(**kw)

    def evolve(self, **changes) -> "HarnessConstraints":
        """Return a copy with ``changes`` applied (``dataclasses.replace`` proxy)."""
        return replace(self, **changes)


__all__ = ["AsofLike", "HarnessConstraints"]
