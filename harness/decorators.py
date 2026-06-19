# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.decorators`` -- declarative tool-side constraint contracts.

A tool author declares *what their tool reads from time* by decorating
the implementation; the enforcer reads the declaration and stops the
LLM from violating it. This is the open-source primitive the blog
points at: a one-line decoration that says

    "this tool reads time-bearing data; the model's ``as_of_iso`` arg
    must equal ``constraints.asof``, and rows in the result with a
    ``filing_date`` after asof get dropped."

The decorator does no enforcement itself. It only **declares** -- it
stamps a :class:`~harness.enforcement.TimeBound` onto the function
as ``fn.__time_bound__``. The runtime enforcer
(:class:`~harness.enforcement.LocalEnforcer`) is what reads the
stamp and acts on it. Decoupling declaration from enforcement is what
lets the closed Coral sandbox plug in its own enforcer for prod runs --
same decorators, different enforcement venue.

Usage::

    @time_bounded(asof_arg="as_of_iso", filter_field="filing_date")
    def get_recent_filings(ticker: str, as_of_iso: str | None = None):
        ...

Three modes (see :class:`TimeBound.mode`):

- ``inject`` (default): the enforcer overwrites whatever the model
  passed for ``asof_arg`` with ``constraints.asof.isoformat()``. Use
  when the tool's asof arg is a pure cutoff parameter -- the model
  doesn't get to choose.
- ``clamp``: the enforcer uses ``min(model_value, constraints.asof)``.
  Use when the model legitimately picks an as-of (e.g. quoting an
  intra-period balance sheet) but must not pick one past the run's
  cutoff.
- ``validate``: the enforcer raises :class:`AsofViolation` if the model
  passed a value past asof. Use for tools where overwriting silently
  would be more confusing than refusing.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, TypeVar

from harness.enforcement import TimeBound


F = TypeVar("F", bound=Callable[..., Any])


def time_bounded(
    *,
    asof_arg: str,
    filter_field: Optional[str] = None,
    mode: str = "inject",
) -> Callable[[F], F]:
    """Declare a tool's temporal contract.

    Parameters
    ----------
    asof_arg:
        The function parameter (and the JSON-schema field the model
        sees) that carries the asof cutoff. Must exist in the tool's
        signature. The enforcer manipulates this arg per ``mode``.
    filter_field:
        Optional key in the tool's return dict whose date the enforcer
        compares to asof when post-filtering rows. Set to ``None`` if
        the tool guarantees row-level asof on its own.
    mode:
        ``"inject"`` | ``"clamp"`` | ``"validate"``. See module docstring.

    The decorator returns the original function unchanged; it only
    attaches metadata. Composes with ``@skill_fn`` and the existing
    ``Tool`` wrapper -- order doesn't matter, attach both.
    """
    bound = TimeBound(
        asof_arg=asof_arg,
        filter_field=filter_field,
        mode=mode,
    )

    def _decorate(fn: F) -> F:
        # Note: we attach to the underlying callable so the enforcer
        # finds it via ``fn.__time_bound__`` regardless of whether the
        # caller looks up the raw function or a wrapped Tool.
        try:
            fn.__time_bound__ = bound  # type: ignore[attr-defined]
        except (AttributeError, TypeError) as exc:
            raise TypeError(
                f"@time_bounded cannot stamp {fn!r}; the target must "
                f"accept attribute assignment. Wrap with functools.wraps "
                f"or apply to the raw function."
            ) from exc
        return fn

    return _decorate


__all__ = ["time_bounded"]
