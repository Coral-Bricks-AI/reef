# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``reef.context`` -- per-run constraints/enforcer propagation.

Threading :class:`HarnessConstraints` + :class:`ConstraintEnforcer`
explicitly through every signature in a large tool surface would be a
days-long refactor. This module is the principled alternative: a pair
of :class:`contextvars.ContextVar` slots scoped to one run, set by
the runner entrypoint and read at the tool-dispatch site.

Why ContextVar and not thread-local: ContextVar is the modern Python
primitive for this exact pattern. It plays correctly with asyncio
(coroutines inherit the snapshot, not the live binding), it nests
cleanly (a nested ``with harness_context(...)`` restores the outer
binding on exit), and it doesn't leak across thread-pool worker reuses.

Usage at the boundary (the swarm's run() entrypoint)::

    with harness_context(constraints, enforcer):
        # Anywhere below this -- runtime.run_react, tool dispatch,
        # skill fn calls -- can call current_enforcer() / current_constraints()
        # and get the run's bindings.
        return _run_inner(...)

The harness layer treats absence (``current_constraints() is None``) as
"unconstrained run" -- back-compat for existing callers that don't pass
the new params. Production code paths should always set both.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from reef.constraints import HarnessConstraints
from reef.enforcement import ConstraintEnforcer, LocalEnforcer


_CURRENT_CONSTRAINTS: ContextVar[Optional[HarnessConstraints]] = ContextVar(
    "harness_constraints", default=None,
)
_CURRENT_ENFORCER: ContextVar[Optional[ConstraintEnforcer]] = ContextVar(
    "harness_enforcer", default=None,
)


def current_constraints() -> Optional[HarnessConstraints]:
    """Return the current run's constraints, or ``None`` if unset."""
    return _CURRENT_CONSTRAINTS.get()


def current_enforcer() -> Optional[ConstraintEnforcer]:
    """Return the current run's enforcer, or ``None`` if unset."""
    return _CURRENT_ENFORCER.get()


@contextmanager
def harness_context(
    constraints: Optional[HarnessConstraints],
    enforcer: Optional[ConstraintEnforcer] = None,
) -> Iterator[tuple[Optional[HarnessConstraints], Optional[ConstraintEnforcer]]]:
    """Bind ``constraints`` + ``enforcer`` for the duration of this block.

    When ``constraints`` is not ``None`` and ``enforcer`` is ``None``, a
    fresh :class:`LocalEnforcer` is created -- the friendly default for
    callers who just want the contract enforced and don't care about
    swapping the venue. The enforcer's ``on_run_start`` is called on
    entry and ``on_run_end`` on exit so accounting state resets cleanly
    between runs.

    Nested binds restore the outer values on exit (ContextVar token
    semantics), so it's safe to use this inside a parent block that
    already set a different context (e.g. a test fixture overriding
    enforcement strictness).
    """
    if constraints is not None and enforcer is None:
        enforcer = LocalEnforcer()

    c_token = _CURRENT_CONSTRAINTS.set(constraints)
    e_token = _CURRENT_ENFORCER.set(enforcer)
    if enforcer is not None and constraints is not None:
        enforcer.on_run_start(constraints)
    try:
        yield (constraints, enforcer)
    finally:
        if enforcer is not None and constraints is not None:
            try:
                enforcer.on_run_end(constraints)
            except Exception:
                pass
        _CURRENT_ENFORCER.reset(e_token)
        _CURRENT_CONSTRAINTS.reset(c_token)


def begin_run(
    constraints: Optional[HarnessConstraints],
    enforcer: Optional[ConstraintEnforcer] = None,
) -> tuple[object, object, Optional[ConstraintEnforcer]]:
    """Imperative form of :func:`harness_context` for non-``with`` callers.

    Sets the constraints + enforcer contextvars and calls
    ``enforcer.on_run_start``. Returns the pair of reset tokens plus
    the (possibly defaulted) enforcer, to be handed back to
    :func:`end_run` in a paired ``finally`` block.

    Use only when you can't easily wrap the run body in a ``with``
    block (e.g. existing try/finally scaffolding that would require
    re-indenting hundreds of lines). New code should prefer
    :func:`harness_context`.
    """
    if constraints is not None and enforcer is None:
        enforcer = LocalEnforcer()
    c_token = _CURRENT_CONSTRAINTS.set(constraints)
    e_token = _CURRENT_ENFORCER.set(enforcer)
    if constraints is not None and enforcer is not None:
        enforcer.on_run_start(constraints)
    return c_token, e_token, enforcer


def end_run(
    c_token: object,
    e_token: object,
    enforcer: Optional[ConstraintEnforcer],
    constraints: Optional[HarnessConstraints],
) -> None:
    """Counterpart to :func:`begin_run`. Idempotent on the tokens.

    Calls ``enforcer.on_run_end`` (swallowing errors -- a misbehaving
    enforcer must not corrupt the run's normal teardown) and restores
    the previous contextvar bindings.
    """
    if enforcer is not None and constraints is not None:
        try:
            enforcer.on_run_end(constraints)
        except Exception:
            pass
    _CURRENT_ENFORCER.reset(e_token)  # type: ignore[arg-type]
    _CURRENT_CONSTRAINTS.reset(c_token)  # type: ignore[arg-type]


__all__ = [
    "begin_run",
    "current_constraints",
    "current_enforcer",
    "end_run",
    "harness_context",
]
