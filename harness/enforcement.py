# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.enforcement`` -- run-time enforcement of harness constraints.

The harness threads :class:`~harness.constraints.HarnessConstraints`
through every layer, but the *enforcement venue* is decoupled: an
implementation of :class:`ConstraintEnforcer` is what actually rejects an
asof-violating tool call, decrements the per-call tool budget, or filters
post-cutoff rows out of a tool result.

Three impls ship in the harness:

- :class:`NullEnforcer` -- no-op. Use in tests and dev where you've
  already verified the contract some other way and just want the
  pipeline to run unconstrained.
- :class:`LocalEnforcer` -- the reference single-process enforcer.
  Self-contained, deterministic, and the one used by the open-source
  reproduce-it harness. Counts tool calls per specialist, validates
  date-bearing args against asof, and post-filters tool results when a
  result schema declares a date field.
- :class:`AuditingEnforcer` -- :class:`LocalEnforcer` plus an in-memory
  event log. Useful for tests asserting "this run made 3 EDGAR calls
  with asof injected" and for the conformance suite.

A production multi-tenant runtime (the closed Coral sandbox) ships its
own enforcer that implements the same :class:`ConstraintEnforcer`
protocol -- the harness, skills, and tools don't know which one is
plugged in. The conformance test suite (see ``tests/test_enforcer_contract.py``)
is the contract; anything that passes it is a valid enforcer.

Two error classes the harness layer catches and feeds back to the model
as tool errors:

- :class:`AsofViolation` -- a tool call would read past asof.
- :class:`BudgetExceeded` -- the run is out of tool calls.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)

from harness.constraints import HarnessConstraints, _coerce_asof


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ConstraintViolation(Exception):
    """Base class for enforcer-raised violations.

    The runtime catches these at the tool-dispatch boundary and renders
    the message as a tool-call error so the model can see *why* its
    request was rejected and route around it. Subclasses MUST carry
    enough prose for the model to diagnose -- e.g. "asof=2024-09-30
    rejects filing_date=2024-12-15".
    """


class AsofViolation(ConstraintViolation):
    """A tool call (or its result) would read data past ``asof``."""


class BudgetExceeded(ConstraintViolation):
    """Run is out of tool calls or tokens; no further dispatches allowed."""


class IndexNotAllowed(ConstraintViolation):
    """Tool tried to read an index outside the allowlist."""


# ---------------------------------------------------------------------------
# Tool-side declaration (consumed by enforcers)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TimeBound:
    """Declares the temporal contract of one tool.

    A tool author attaches this via :func:`@time_bounded` (see
    :mod:`harness.decorators`, Pass 7). The enforcer reads it off
    the tool's ``fn`` attribute to know:

    - ``asof_arg`` -- the parameter name to inject ``constraints.asof``
      into (or validate if the model already passed one).
    - ``filter_field`` -- the result field whose date the enforcer
      compares to asof when post-filtering rows (``None`` to disable
      post-filtering and trust the tool).
    - ``mode`` -- ``"inject"`` (overwrite the model's value), ``"clamp"``
      (use min of model value and asof), or ``"validate"`` (raise if
      model value > asof).
    """

    asof_arg: str
    filter_field: Optional[str] = None
    mode: str = "inject"

    def __post_init__(self) -> None:
        if self.mode not in ("inject", "clamp", "validate"):
            raise ValueError(
                f"TimeBound.mode must be inject|clamp|validate, "
                f"got {self.mode!r}"
            )


def get_time_bound(fn: Callable[..., Any]) -> Optional[TimeBound]:
    """Return the ``TimeBound`` attached by ``@time_bounded``, if any."""
    return getattr(fn, "__time_bound__", None)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ConstraintEnforcer(Protocol):
    """The enforcer contract. See :class:`LocalEnforcer` for a reference impl.

    Lifecycle (one enforcer instance per harness run):

    1. ``on_run_start(c)`` once before any tool calls.
    2. ``on_round_start(i, c)`` at the top of each planner/synthesizer round.
    3. ``before_tool_call(tool, fn, args, c)`` before each dispatch --
       MAY mutate ``args`` (e.g. inject asof) and return the new dict;
       MAY raise :class:`ConstraintViolation`.
    4. ``after_tool_call(tool, result, c)`` after each dispatch --
       MAY transform ``result`` (e.g. filter post-asof rows) and return it.
    5. ``on_round_end(i, c)`` at end of round.
    6. ``on_run_end(c)`` once.
    """

    def on_run_start(self, c: HarnessConstraints) -> None: ...

    def on_run_end(self, c: HarnessConstraints) -> None: ...

    def on_round_start(self, round_idx: int, c: HarnessConstraints) -> None: ...

    def on_round_end(self, round_idx: int, c: HarnessConstraints) -> None: ...

    def before_tool_call(
        self,
        tool_name: str,
        fn: Callable[..., Any],
        args: Mapping[str, Any],
        c: HarnessConstraints,
    ) -> dict[str, Any]: ...

    def after_tool_call(
        self,
        tool_name: str,
        result: Any,
        c: HarnessConstraints,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Null
# ---------------------------------------------------------------------------

class NullEnforcer:
    """No-op enforcer. The harness runs unconstrained.

    Test-only. Production code should always use :class:`LocalEnforcer`
    (or a stricter substitute) -- a harness without enforcement loses
    the asof contract the rest of the system depends on.
    """

    def on_run_start(self, c: HarnessConstraints) -> None: pass
    def on_run_end(self, c: HarnessConstraints) -> None: pass
    def on_round_start(self, round_idx: int, c: HarnessConstraints) -> None: pass
    def on_round_end(self, round_idx: int, c: HarnessConstraints) -> None: pass

    def before_tool_call(
        self, tool_name, fn, args, c,
    ) -> dict[str, Any]:
        return dict(args)

    def after_tool_call(self, tool_name, result, c) -> Any:
        return result


# ---------------------------------------------------------------------------
# Local reference impl
# ---------------------------------------------------------------------------

# Convention-based fallback when a tool doesn't carry @time_bounded.
# These arg names are the common spellings of "asof" across tool
# surfaces; tools with one of them get asof injected/validated even
# without a decorator. New tools should prefer the explicit decorator.
_ASOF_ARG_ALIASES = frozenset({
    "asof", "as_of", "asof_iso", "as_of_iso", "asof_date", "as_of_date",
    "cutoff", "cutoff_date",
})


def _try_parse_date(value: Any) -> Optional[date]:
    """Best-effort coerce an arg value to ``date``; ``None`` on failure."""
    try:
        return _coerce_asof(value)
    except (TypeError, ValueError):
        return None


@dataclass
class LocalEnforcer:
    """Single-process reference enforcer.

    Tracks tool-call count and token spend across the run, validates
    asof against tool args (declarative via ``@time_bounded`` or fallback
    by arg-name convention), and optionally post-filters tool results.

    Construct one per run; the enforcer is **stateful** -- it carries
    the per-call budget pointer between dispatches.

    Parameters
    ----------
    strict_post_filter:
        When ``True`` (default), if a tool result is a dict containing
        a list under any key matching a known date field, rows with a
        date > asof are dropped. Set ``False`` if you trust every tool
        to honor asof on the input side.
    on_violation:
        Hook called with each :class:`ConstraintViolation` raised. Use
        for observability. The violation is still raised; this is
        notification-only.
    """

    strict_post_filter: bool = True
    on_violation: Optional[Callable[[ConstraintViolation], None]] = None

    _calls_used: int = field(default=0, init=False)
    _tokens_used: int = field(default=0, init=False)
    _round_idx: int = field(default=0, init=False)
    # Bound metadata for the most recent before_tool_call, so
    # after_tool_call can look it up without changing the Protocol
    # signature. Single-threaded harness => safe.
    _last_bound: Optional[TimeBound] = field(default=None, init=False)

    # ---- lifecycle ----------------------------------------------------

    def on_run_start(self, c: HarnessConstraints) -> None:
        self._calls_used = 0
        self._tokens_used = 0
        self._round_idx = 0

    def on_run_end(self, c: HarnessConstraints) -> None:
        pass

    def on_round_start(self, round_idx: int, c: HarnessConstraints) -> None:
        self._round_idx = round_idx

    def on_round_end(self, round_idx: int, c: HarnessConstraints) -> None:
        pass

    # ---- accounting ---------------------------------------------------

    @property
    def calls_used(self) -> int:
        return self._calls_used

    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    def record_tokens(self, n: int) -> None:
        """Bump the token counter; raise if past ``token_budget`` (caller passes ``c``)."""
        self._tokens_used += max(0, int(n))

    # ---- tool dispatch hooks -----------------------------------------

    def before_tool_call(
        self,
        tool_name: str,
        fn: Callable[..., Any],
        args: Mapping[str, Any],
        c: HarnessConstraints,
    ) -> dict[str, Any]:
        if self._calls_used >= c.tool_budget:
            self._raise(BudgetExceeded(
                f"tool_budget={c.tool_budget} exhausted; refusing call to "
                f"{tool_name!r}. Submit final answer with what you have."
            ))
        if c.token_budget is not None and self._tokens_used >= c.token_budget:
            self._raise(BudgetExceeded(
                f"token_budget={c.token_budget} exhausted; refusing call to "
                f"{tool_name!r}."
            ))

        new_args = dict(args)
        self._last_bound = get_time_bound(fn)
        self._apply_asof_to_args(tool_name, fn, new_args, c)
        self._calls_used += 1
        return new_args

    def after_tool_call(
        self,
        tool_name: str,
        result: Any,
        c: HarnessConstraints,
    ) -> Any:
        if c.asof is None or not self.strict_post_filter:
            return result
        bound = self._resolve_bound(tool_name, result)
        if bound is None or bound.filter_field is None:
            return result
        return _filter_rows_by_date(result, bound.filter_field, c.asof)

    # ---- helpers ------------------------------------------------------

    def _apply_asof_to_args(
        self,
        tool_name: str,
        fn: Callable[..., Any],
        args: dict[str, Any],
        c: HarnessConstraints,
    ) -> None:
        if c.asof is None:
            return
        bound = get_time_bound(fn)
        if bound is not None:
            self._apply_declared(tool_name, args, bound, c.asof)
            return
        # Fallback: arg-name convention scan.
        for k in list(args.keys()):
            if k in _ASOF_ARG_ALIASES:
                self._apply_declared(
                    tool_name,
                    args,
                    TimeBound(asof_arg=k, mode="validate"),
                    c.asof,
                )

    def _apply_declared(
        self,
        tool_name: str,
        args: dict[str, Any],
        bound: TimeBound,
        asof: date,
    ) -> None:
        arg = bound.asof_arg
        current = args.get(arg)
        if bound.mode == "inject" or current is None:
            args[arg] = asof.isoformat()
            return
        parsed = _try_parse_date(current)
        if parsed is None:
            # Model passed something we can't compare to -- safest is to
            # overwrite with the asof. Equivalent to inject.
            args[arg] = asof.isoformat()
            return
        if bound.mode == "clamp":
            args[arg] = min(parsed, asof).isoformat()
            return
        # validate
        if parsed > asof:
            self._raise(AsofViolation(
                f"{tool_name}: arg {arg!r}={current!r} is past "
                f"asof={asof.isoformat()}. Use asof or an earlier date."
            ))

    def _resolve_bound(self, tool_name: str, result: Any) -> Optional[TimeBound]:
        # ``_last_bound`` was captured at before_tool_call from
        # ``fn.__time_bound__`` (set by ``@time_bounded``). Cleared
        # after consumption so post-filter only runs for the dispatch
        # that immediately preceded.
        bound = self._last_bound
        self._last_bound = None
        return bound

    def _raise(self, exc: ConstraintViolation) -> None:
        if self.on_violation is not None:
            try:
                self.on_violation(exc)
            except Exception:
                pass
        raise exc


# ---------------------------------------------------------------------------
# Auditing variant -- LocalEnforcer + an event log
# ---------------------------------------------------------------------------

@dataclass
class EnforcementEvent:
    kind: str                              # "call" | "violation" | "round"
    tool_name: Optional[str] = None
    args: Optional[Mapping[str, Any]] = None
    asof_injected: Optional[str] = None
    detail: Optional[str] = None


class AuditingEnforcer(LocalEnforcer):
    """:class:`LocalEnforcer` + an in-memory event log.

    Use in tests asserting the enforcer did what you expected, and in
    the conformance suite. The log is process-local and unbounded --
    don't use in long-running production runs.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.events: list[EnforcementEvent] = []

    def before_tool_call(self, tool_name, fn, args, c):
        new_args = super().before_tool_call(tool_name, fn, args, c)
        self.events.append(EnforcementEvent(
            kind="call",
            tool_name=tool_name,
            args=dict(new_args),
            asof_injected=c.asof_iso,
        ))
        return new_args

    def _raise(self, exc):
        self.events.append(EnforcementEvent(
            kind="violation",
            detail=str(exc),
        ))
        super()._raise(exc)


# ---------------------------------------------------------------------------
# Post-filter helper
# ---------------------------------------------------------------------------

def _filter_rows_by_date(
    result: Any, filter_field: str, asof: date,
) -> Any:
    """Drop rows from ``result`` whose ``filter_field`` is > ``asof``.

    Works on:

    - ``list[dict]`` directly (filters in place into a new list).
    - ``dict`` containing exactly one list value (filters that list).
    - Anything else: returns unchanged.

    Tools whose return shape doesn't fit one of these should set
    ``filter_field=None`` on their ``@time_bounded`` and do their own
    asof-handling. Post-filter is a safety net, not a contract.
    """
    def _keep(row: Any) -> bool:
        if not isinstance(row, Mapping):
            return True
        v = row.get(filter_field)
        d = _try_parse_date(v)
        return d is None or d <= asof

    if isinstance(result, list):
        return [r for r in result if _keep(r)]
    if isinstance(result, Mapping):
        out = dict(result)
        list_keys = [k for k, v in out.items() if isinstance(v, list)]
        if len(list_keys) == 1:
            k = list_keys[0]
            out[k] = [r for r in out[k] if _keep(r)]
        return out
    return result


# ---------------------------------------------------------------------------
# Convenience: scoped enforcer context
# ---------------------------------------------------------------------------

@contextmanager
def enforced_run(
    constraints: HarnessConstraints,
    enforcer: Optional[ConstraintEnforcer] = None,
) -> Iterator[ConstraintEnforcer]:
    """Context manager that calls ``on_run_start`` / ``on_run_end``.

    Default enforcer is :class:`LocalEnforcer`. The harness's
    ``run_swarm`` should wrap its main body in this so accounting state
    is properly reset between runs.
    """
    enf: ConstraintEnforcer = enforcer or LocalEnforcer()
    enf.on_run_start(constraints)
    try:
        yield enf
    finally:
        enf.on_run_end(constraints)


__all__ = [
    "ConstraintViolation",
    "AsofViolation",
    "BudgetExceeded",
    "IndexNotAllowed",
    "TimeBound",
    "get_time_bound",
    "ConstraintEnforcer",
    "NullEnforcer",
    "LocalEnforcer",
    "AuditingEnforcer",
    "EnforcementEvent",
    "enforced_run",
]
