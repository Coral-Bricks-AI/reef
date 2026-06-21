# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Integration tests for :mod:`reef` constraints + enforcement.

Pins the contract that the rest of alphacumen + alphacumen depends on:

- ``HarnessConstraints`` round-trips ISO strings to dates.
- ``LocalEnforcer`` injects / clamps / validates ``asof`` against
  ``@time_bounded`` declarations.
- ``harness_context`` propagates bindings to the runtime layer's
  ``current_constraints()`` / ``current_enforcer()`` lookups.
- ``begin_run`` / ``end_run`` pair (used by ``alphacumen.swarm.run`` to
  avoid re-indenting the entire orchestrator body) round-trips
  cleanly.
- The constraint refactor is back-compat: callers that don't pass
  ``constraints`` see no enforcement (the existing thread-local asof
  ceiling in ``alphacumen.tools`` is the legacy path).
"""

from __future__ import annotations

from datetime import date

import pytest

from reef import (
    AsofViolation,
    AuditingEnforcer,
    BudgetExceeded,
    HarnessConstraints,
    LocalEnforcer,
    NullEnforcer,
    TimeBound,
    current_constraints,
    current_enforcer,
    enforced_run,
    get_time_bound,
    harness_context,
    time_bounded,
)
from reef.context import begin_run, end_run


# ---------------------------------------------------------------------------
# HarnessConstraints
# ---------------------------------------------------------------------------

class TestHarnessConstraints:

    def test_iso_string_coerces_to_date(self) -> None:
        c = HarnessConstraints(asof="2024-09-30")
        assert c.asof == date(2024, 9, 30)
        assert c.asof_iso == "2024-09-30"

    def test_full_iso_datetime_truncates_to_date(self) -> None:
        c = HarnessConstraints(asof="2024-09-30T15:30:00")
        assert c.asof == date(2024, 9, 30)

    def test_date_passthrough(self) -> None:
        d = date(2024, 6, 15)
        c = HarnessConstraints(asof=d)
        assert c.asof is d

    def test_none_asof_is_unconstrained(self) -> None:
        c = HarnessConstraints()
        assert c.asof is None
        assert c.asof_iso is None

    def test_validation_rejects_zero_tool_budget(self) -> None:
        with pytest.raises(ValueError, match="tool_budget"):
            HarnessConstraints(tool_budget=0)

    def test_validation_rejects_bad_asof(self) -> None:
        with pytest.raises(ValueError, match="ISO"):
            HarnessConstraints(asof="not-a-date")

    def test_index_allowlist_empty_means_unconstrained(self) -> None:
        c = HarnessConstraints()
        assert c.is_index_allowed("anything")

    def test_index_allowlist_restricts(self) -> None:
        c = HarnessConstraints(allowed_indices=("sec", "edgar"))
        assert c.is_index_allowed("sec")
        assert not c.is_index_allowed("reddit")

    def test_from_legacy_kwargs(self) -> None:
        c = HarnessConstraints.from_legacy_kwargs(
            asof="2024-06-15", max_rounds=5, tool_budget=20,
        )
        assert c.asof == date(2024, 6, 15)
        assert c.max_rounds == 5
        assert c.tool_budget == 20

    def test_evolve_returns_new_instance(self) -> None:
        c1 = HarnessConstraints(asof="2024-06-15", tool_budget=10)
        c2 = c1.evolve(tool_budget=50)
        assert c2.tool_budget == 50
        assert c1.tool_budget == 10  # unchanged


# ---------------------------------------------------------------------------
# @time_bounded + LocalEnforcer
# ---------------------------------------------------------------------------

@time_bounded(asof_arg="as_of_iso", filter_field="filing_date", mode="inject")
def _inject_fn(ticker: str, as_of_iso: str | None = None) -> dict:
    return {
        "ticker": ticker,
        "rows": [
            {"filing_date": "2024-06-30", "doc": "A"},
            {"filing_date": "2024-12-15", "doc": "B"},
            {"filing_date": "2024-09-01", "doc": "C"},
        ],
    }


@time_bounded(asof_arg="as_of_iso", mode="clamp")
def _clamp_fn(as_of_iso: str | None = None) -> dict:
    return {}


@time_bounded(asof_arg="as_of_iso", mode="validate")
def _validate_fn(as_of_iso: str | None = None) -> dict:
    return {}


class TestTimeBoundedDecorator:

    def test_decorator_stamps_metadata(self) -> None:
        bound = get_time_bound(_inject_fn)
        assert bound is not None
        assert bound.asof_arg == "as_of_iso"
        assert bound.filter_field == "filing_date"
        assert bound.mode == "inject"

    def test_decorator_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValueError):
            TimeBound(asof_arg="x", mode="bogus")


class TestLocalEnforcerAsof:

    def test_inject_mode_overwrites_model_arg(self) -> None:
        enf = LocalEnforcer()
        c = HarnessConstraints(asof="2024-09-30", tool_budget=10)
        args = enf.before_tool_call(
            "x", _inject_fn,
            {"ticker": "AAPL", "as_of_iso": "2025-01-15"},
            c,
        )
        assert args["as_of_iso"] == "2024-09-30"

    def test_clamp_mode_uses_min(self) -> None:
        enf = LocalEnforcer()
        c = HarnessConstraints(asof="2024-09-30", tool_budget=10)
        # Model picks a date past asof -> clamped to asof
        a1 = enf.before_tool_call(
            "x", _clamp_fn, {"as_of_iso": "2025-01-15"}, c,
        )
        assert a1["as_of_iso"] == "2024-09-30"
        # Model picks a date before asof -> kept
        a2 = enf.before_tool_call(
            "x", _clamp_fn, {"as_of_iso": "2024-03-15"}, c,
        )
        assert a2["as_of_iso"] == "2024-03-15"

    def test_validate_mode_rejects_future_date(self) -> None:
        enf = LocalEnforcer()
        c = HarnessConstraints(asof="2024-09-30", tool_budget=10)
        with pytest.raises(AsofViolation):
            enf.before_tool_call(
                "x", _validate_fn, {"as_of_iso": "2025-01-15"}, c,
            )

    def test_post_filter_drops_post_asof_rows(self) -> None:
        enf = LocalEnforcer()
        c = HarnessConstraints(asof="2024-09-30", tool_budget=10)
        args = enf.before_tool_call("x", _inject_fn, {"ticker": "AAPL"}, c)
        raw = _inject_fn(**args)
        filtered = enf.after_tool_call("x", raw, c)
        dates = {r["filing_date"] for r in filtered["rows"]}
        assert "2024-12-15" not in dates
        assert dates == {"2024-06-30", "2024-09-01"}

    def test_no_filter_when_asof_unset(self) -> None:
        enf = LocalEnforcer()
        c = HarnessConstraints()
        args = enf.before_tool_call("x", _inject_fn, {"ticker": "AAPL"}, c)
        raw = _inject_fn(**args)
        filtered = enf.after_tool_call("x", raw, c)
        assert len(filtered["rows"]) == 3  # nothing dropped


class TestLocalEnforcerBudget:

    def test_tool_budget_exhausts(self) -> None:
        enf = LocalEnforcer()
        c = HarnessConstraints(tool_budget=2)
        with enforced_run(c, enf):
            enf.before_tool_call("x", _clamp_fn, {}, c)
            enf.before_tool_call("x", _clamp_fn, {}, c)
            with pytest.raises(BudgetExceeded):
                enf.before_tool_call("x", _clamp_fn, {}, c)

    def test_budget_resets_per_run(self) -> None:
        enf = LocalEnforcer()
        c = HarnessConstraints(tool_budget=1)
        with enforced_run(c, enf):
            enf.before_tool_call("x", _clamp_fn, {}, c)
        # Re-enter
        with enforced_run(c, enf):
            enf.before_tool_call("x", _clamp_fn, {}, c)  # should not raise


# ---------------------------------------------------------------------------
# Context-var propagation
# ---------------------------------------------------------------------------

class TestHarnessContext:

    def test_empty_outside_block(self) -> None:
        assert current_constraints() is None
        assert current_enforcer() is None

    def test_with_block_sets_and_restores(self) -> None:
        c = HarnessConstraints(asof="2024-09-30")
        e = LocalEnforcer()
        with harness_context(c, e):
            assert current_constraints() is c
            assert current_enforcer() is e
        assert current_constraints() is None
        assert current_enforcer() is None

    def test_default_local_enforcer(self) -> None:
        c = HarnessConstraints(asof="2024-09-30")
        with harness_context(c):
            assert isinstance(current_enforcer(), LocalEnforcer)

    def test_begin_run_end_run_pair(self) -> None:
        c = HarnessConstraints(asof="2024-09-30")
        e = LocalEnforcer()
        tokens = begin_run(c, e)
        assert current_constraints() is c
        assert current_enforcer() is e
        end_run(*tokens, c)
        assert current_constraints() is None
        assert current_enforcer() is None

    def test_begin_run_defaults_enforcer(self) -> None:
        c = HarnessConstraints(asof="2024-09-30")
        tokens = begin_run(c, None)
        assert isinstance(current_enforcer(), LocalEnforcer)
        end_run(*tokens, c)

    def test_nested_blocks_restore(self) -> None:
        c1 = HarnessConstraints(asof="2024-01-01", tool_budget=5)
        c2 = HarnessConstraints(asof="2024-12-31", tool_budget=10)
        with harness_context(c1):
            assert current_constraints() is c1
            with harness_context(c2):
                assert current_constraints() is c2
            assert current_constraints() is c1


# ---------------------------------------------------------------------------
# Auditing
# ---------------------------------------------------------------------------

class TestAuditingEnforcer:

    def test_records_each_call(self) -> None:
        audit = AuditingEnforcer()
        c = HarnessConstraints(asof="2024-09-30", tool_budget=5)
        with enforced_run(c, audit):
            audit.before_tool_call("a", _clamp_fn, {}, c)
            audit.before_tool_call("b", _clamp_fn, {}, c)
        assert len(audit.events) == 2
        assert audit.events[0].tool_name == "a"
        assert audit.events[0].asof_injected == "2024-09-30"
        assert audit.events[1].tool_name == "b"

    def test_records_violations(self) -> None:
        audit = AuditingEnforcer()
        c = HarnessConstraints(asof="2024-09-30", tool_budget=5)
        with pytest.raises(AsofViolation):
            audit.before_tool_call(
                "x", _validate_fn, {"as_of_iso": "2025-01-15"}, c,
            )
        violations = [e for e in audit.events if e.kind == "violation"]
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Real finance tool: _do_find_sec_filing_edgar carries the decorator
# ---------------------------------------------------------------------------

class TestRealFinanceToolDecorated:

    def test_find_sec_filing_edgar_has_time_bound(self) -> None:
        from alphacumen.tools import _do_find_sec_filing_edgar
        bound = get_time_bound(_do_find_sec_filing_edgar)
        assert bound is not None
        assert bound.asof_arg == "filed_at_lte"
        assert bound.mode == "clamp"

    def test_find_sec_filing_clamps_via_enforcer(self) -> None:
        from alphacumen.tools import _do_find_sec_filing_edgar
        enf = LocalEnforcer()
        c = HarnessConstraints(asof="2024-09-30", tool_budget=5)
        args = enf.before_tool_call(
            "find_sec_filing_edgar",
            _do_find_sec_filing_edgar,
            {"ticker": "AAPL", "filed_at_lte": "2025-01-15"},
            c,
        )
        assert args["filed_at_lte"] == "2024-09-30"


# ---------------------------------------------------------------------------
# Back-compat: existing callers (no constraints) see no enforcement
# ---------------------------------------------------------------------------

class TestBackCompat:

    def test_null_enforcer_passes_args_through(self) -> None:
        enf = NullEnforcer()
        c = HarnessConstraints(asof="2024-09-30", tool_budget=2)
        args = enf.before_tool_call(
            "x", _inject_fn, {"as_of_iso": "2025-01-15"}, c,
        )
        assert args["as_of_iso"] == "2025-01-15"  # untouched

    def test_runtime_skips_enforcement_when_context_empty(self) -> None:
        # The reef.react layer reads current_constraints() /
        # current_enforcer() at dispatch. With no context bound,
        # both return None and the runtime falls through to the
        # legacy dispatch path.
        assert current_constraints() is None
        assert current_enforcer() is None


# ---------------------------------------------------------------------------
# End-to-end: harness_context -> run_react -> enforcer.before_tool_call
# Mocks the LLM proxy so we never reach the gateway, but exercises the
# full constraint plumbing through the actual production code path.
# ---------------------------------------------------------------------------

class TestEndToEndConstraintPropagationViaRunReact:
    """The big assertion: a model-supplied future date arrives at the
    real tool function clamped back to constraints.asof, by virtue of
    the production run_react dispatch hot path picking the enforcer
    up out of contextvars and applying @time_bounded.

    We mock the LLM (no gateway) and stub the @time_bounded tool's fn
    (no SEC EDGAR network) but keep run_react itself, the enforcer
    itself, and the contextvar plumbing fully production.
    """

    def test_filed_at_lte_clamped_through_full_dispatch_chain(self) -> None:
        import json
        from unittest.mock import patch

        from reef import react as cb_runtime
        from reef.react import run_react
        from alphacumen.tools import FIND_SEC_FILING_EDGAR, Tool

        fn_seen: list[str | None] = []

        def _stub(*_a, **kwargs):
            fn_seen.append(kwargs.get("filed_at_lte"))
            return {"ticker": kwargs.get("ticker"), "hits": []}

        # Carry the @time_bounded metadata across the stub so the
        # enforcer's get_time_bound() lookup still resolves.
        _stub.__time_bound__ = get_time_bound(FIND_SEC_FILING_EDGAR.fn)

        stubbed = Tool(
            name=FIND_SEC_FILING_EDGAR.name,
            description=FIND_SEC_FILING_EDGAR.description,
            parameters=FIND_SEC_FILING_EDGAR.parameters,
            fn=_stub,
            bound_indices=FIND_SEC_FILING_EDGAR.bound_indices,
        )

        turn = [0]

        def fake_chat(**_kw):
            turn[0] += 1
            if turn[0] == 1:
                return {
                    "model": "fake-model",
                    "response": {
                        "choices": [{"message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "find_sec_filing_edgar",
                                    "arguments": json.dumps({
                                        "ticker": "AAPL",
                                        "form_type": "8-K",
                                        "filed_at_lte": "2025-01-15",
                                    }),
                                },
                            }],
                        }}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                  "total_tokens": 15},
                    },
                }
            # Turn 2: emit a non-tool final message so run_react exits.
            return {
                "model": "fake-model",
                "response": {
                    "choices": [{"message": {
                        "role": "assistant", "content": "done",
                    }}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                              "total_tokens": 6},
                },
            }

        constraints = HarnessConstraints(asof="2024-09-30", tool_budget=5)
        enforcer = LocalEnforcer()

        with harness_context(constraints, enforcer):
            with patch.object(cb_runtime.cb_llm, "chat", side_effect=fake_chat):
                traj = run_react(
                    model="fake-model",
                    system_prompt="Use the tool to find filings.",
                    user_message="Find AAPL 8-Ks.",
                    tools=[stubbed],
                    max_steps=3,
                    log_label="e2e-test",
                )

        assert fn_seen, "tool fn was never invoked through run_react"
        # The model passed 2025-01-15 (past asof); the enforcer in the
        # production dispatch path looked up @time_bounded(mode="clamp")
        # on the tool's fn and clamped to constraints.asof.
        assert fn_seen[0] == "2024-09-30", (
            f"clamp did not engage end-to-end: expected '2024-09-30', "
            f"got {fn_seen[0]!r}"
        )
        assert traj.error is None

