# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end tests for :func:`alphacumen.swarm.run`.

The swarm is now the multi-round IA orchestrator (parity with the
prod ``run_investment_analyst_swarm`` shape). These tests run the
full pipeline -- GP synthesizer + parallel specialists across N
rounds -- end-to-end with the only external surface, the platform
LLM proxy ``coralbricks.sandbox.llm.chat``, replaced by a
deterministic FakeLLM. Tools are exercised in a separate test where
the LLM emits a ``tool_calls`` response and the swarm dispatches
through ``alphacumen.tools`` to the kernel verb (which is also patched).

The orchestration shape we verify:

- Round 1 GP returns ``invoke_next`` with focused per-specialist
  instructions.
- Round 2 (or N) GP returns ``converged: true`` with a structured
  ``final_answer`` and the swarm ships it as ``result["answer"]``.
- ``common_thread_summary`` carries one row per message (user, GP
  decisions, specialist results) in chronological order.
- Token totals roll up across every LLM call (specialists +
  every GP round).
- Pipeline = "investment_analyst" only; anything else is rejected
  before the first LLM call.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from harness import react as cb_runtime
from alphacumen import swarm as cb_swarm
from alphacumen.index_map import GDELT_EVENTS_INDEX
from alphacumen.roster import INVESTMENT_ANALYST_ROSTER


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _wrap_chat_response(
    *,
    content: str = "",
    tool_calls=None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "model": "fake-model",
        "response": {
            "choices": [{"message": msg}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    }


def _specialist_payload(*, key: str) -> dict[str, Any]:
    return {
        "answer_summary": f"{key} sees opportunity in AAPL",
        "specialist": key,
        "key_evidence": [f"{key} datapoint A", f"{key} datapoint B"],
        "confidence": "medium",
    }


def _gp_orchestrate_payload(invoke_keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        "reasoning": "Need price action and filings.",
        "pruning_notes": None,
        "converged": False,
        "invoke_next": [
            {"persona_key": k, "instruction": f"Look at {k}-related signals for AAPL"}
            for k in invoke_keys
        ],
        "final_answer": None,
    }


def _gp_converge_payload() -> dict[str, Any]:
    return {
        "reasoning": "Specialists agree, ready to converge.",
        "pruning_notes": None,
        "converged": True,
        "invoke_next": [],
        "final_answer": {
            "answerable": True,
            "answer_summary": "## AAPL\n**Buy** -- measured opportunity, monitor antitrust.",
            "confidence": "medium",
            "key_events": [],
            "metrics_evidence": [],
        },
    }


# Identify the GP system prompt by a marker that appears only in the
# orchestrator template -- specialists never share this phrasing.
_GP_MARKER = "General Partner"


def _is_gp_call(kwargs: dict[str, Any]) -> bool:
    for m in kwargs.get("messages") or []:
        if m.get("role") == "system" and _GP_MARKER in (m.get("content") or ""):
            return True
    return False


def _is_specialist_call(kwargs: dict[str, Any], persona_key: str) -> bool:
    """Match a specialist by the user-message instruction prefix.

    The swarm passes the GP's per-task instruction as the user
    message verbatim. Our scripted GP emits instructions of the form
    ``"Look at <key>-related signals for AAPL"``, so we route on
    that substring -- robust to which specialist persona system
    prompt happens to win the system slot.
    """
    if _is_gp_call(kwargs):
        return False
    for m in kwargs.get("messages") or []:
        if m.get("role") == "user" and persona_key in (m.get("content") or ""):
            return True
    return False


# --------------------------------------------------------------------------
# Happy-path end-to-end: round 1 invokes -> round 2 converges
# --------------------------------------------------------------------------


def test_swarm_run_two_round_happy_path() -> None:
    invoked = INVESTMENT_ANALYST_ROSTER  # GP invokes the full roster

    gp_calls = {"n": 0}

    def fake_chat(**kwargs: Any) -> dict[str, Any]:
        if _is_gp_call(kwargs):
            gp_calls["n"] += 1
            if gp_calls["n"] == 1:
                return _wrap_chat_response(
                    content=json.dumps(_gp_orchestrate_payload(invoked)),
                    prompt_tokens=200, completion_tokens=120,
                )
            return _wrap_chat_response(
                content=json.dumps(_gp_converge_payload()),
                prompt_tokens=300, completion_tokens=180,
            )
        # Otherwise it's a specialist; route by the per-task user msg.
        for k in invoked:
            if _is_specialist_call(kwargs, k):
                # news_quant_analyst's SpecialistConfig sets
                # ``min_tool_calls_before_final=1`` -- the runtime
                # ReAct loop refuses to accept a no-tool-call
                # final answer. To stay on the happy path here we
                # emit a tool_call on the first turn (the runtime
                # will dispatch it; with no kernel attached it
                # falls into the "unknown tool" branch which still
                # increments ``traj.token_usage["tool_calls"]`` to
                # satisfy the gate) and the structured payload on
                # the second turn (recognized by a "tool" role
                # message in ``messages``).
                if k == "news_quant_analyst":
                    msgs = kwargs.get("messages") or []
                    has_tool_result = any(
                        m.get("role") == "tool" for m in msgs
                    )
                    if not has_tool_result:
                        return _wrap_chat_response(
                            content="",
                            tool_calls=[{
                                "id": "call_nq_1",
                                "type": "function",
                                "function": {
                                    "name": "bm25_scraped_articles",
                                    "arguments": json.dumps({
                                        "query": "AAPL test",
                                        "k": 5,
                                    }),
                                },
                            }],
                        )
                return _wrap_chat_response(
                    content=json.dumps(_specialist_payload(key=k)),
                )
        raise AssertionError(
            f"unrouted chat: messages={kwargs.get('messages')!r}"
        )

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=fake_chat):
        result = cb_swarm.run("Should I buy AAPL?", model="fake-model")

    assert result["success"] is True
    assert result["error"] is None
    assert result["pipeline"] == "investment_analyst"
    assert result["model"] == "fake-model"
    assert "synthesizer_model" not in result
    assert list(result["roster"]) == list(INVESTMENT_ANALYST_ROSTER)
    assert result["rounds"] == 2

    # ``answer`` carries the structured final_answer dict (prod parity).
    assert result["answer"] is not None
    assert isinstance(result["answer"], dict)
    assert "AAPL" in result["answer"]["answer_summary"]
    assert result["answer_summary"] == result["answer"]["answer_summary"]
    assert result["final_answer"] == result["answer"]

    # All four specialists fired exactly once, in round 1.
    outputs = result["specialist_outputs"]
    assert {o["key"] for o in outputs} == set(INVESTMENT_ANALYST_ROSTER)
    assert all(o["round"] == 1 for o in outputs)
    assert all(o["error"] is None for o in outputs)
    assert all(o["payload"] is not None for o in outputs)

    # Top-level convenience fields are populated in lock-step with
    # the canonical (``key``, ``round``) ones so a UI / downstream
    # consumer can render an invocation row without walking
    # ``trajectory.*``. Regression guard for the prior bug where
    # these were silently absent and consumers saw ``None``s.
    for o in outputs:
        assert o["persona_key"] == o["key"]
        assert o["round_index"] == o["round"]
        assert o["success"] is True
        assert isinstance(o["tool_calls"], int) and o["tool_calls"] >= 0
        assert o["tool_calls"] == o["token_usage"]["tool_calls"]
        assert set(o["token_usage"]) == {
            "input_tokens", "output_tokens", "cached_tokens", "tool_calls",
        }
        assert isinstance(o["latency_ms"], int) and o["latency_ms"] >= 0

    # Synthesizer fired twice; both rounds are recorded.
    sr = result["synthesizer_rounds"]
    assert [r["round"] for r in sr] == [1, 2]
    assert [r["round_index"] for r in sr] == [1, 2]
    assert sr[0]["converged"] is False
    assert sr[1]["converged"] is True
    assert all(r["error"] is None for r in sr)

    # Common thread shape: user + (synth + N specialists) + synth,
    # where N = len(INVESTMENT_ANALYST_ROSTER). Currently N=5
    # (stock, sector, vc, risk, news_quant).
    rows = result["common_thread_summary"]
    n_spec = len(INVESTMENT_ANALYST_ROSTER)
    assert rows[0]["agent"] == "user"
    assert rows[1]["agent"] == "synthesizer"
    assert {rows[i]["agent"] for i in range(2, 2 + n_spec)} == set(INVESTMENT_ANALYST_ROSTER)
    assert rows[2 + n_spec]["agent"] == "synthesizer"
    assert result["common_thread_length"] == 3 + n_spec

    # Token totals. Each "narrative" specialist makes 1 LLM call
    # (10 prompt + 5 completion). news_quant_analyst makes 2 (one
    # tool-call turn + one synthesis turn) to satisfy the
    # must-retrieve gate. Plus GP rounds 1 (200/120) + 2 (300/180).
    n_narrative = n_spec - 1  # all except news_quant_analyst
    n_quant_extra = 1  # news_quant_analyst's extra LLM turn
    assert result["token_usage"]["input_tokens"] == (
        (n_narrative * 10) + ((1 + n_quant_extra) * 10) + 200 + 300
    )
    assert result["token_usage"]["output_tokens"] == (
        (n_narrative * 5) + ((1 + n_quant_extra) * 5) + 120 + 180
    )

    assert result["elapsed_ms"] >= 0


# --------------------------------------------------------------------------
# Convergence on round 1 (GP needs nothing)
# --------------------------------------------------------------------------


def test_swarm_run_converges_in_first_round_without_invoking_specialists() -> None:
    def fake_chat(**kwargs: Any) -> dict[str, Any]:
        if _is_gp_call(kwargs):
            return _wrap_chat_response(content=json.dumps(_gp_converge_payload()))
        raise AssertionError(
            "Specialists should not have been invoked on a same-round converge"
        )

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=fake_chat):
        result = cb_swarm.run("trivial query", model="fake-model")

    assert result["success"] is True
    assert result["rounds"] == 1
    assert result["specialist_outputs"] == []
    assert result["answer"]["answer_summary"].startswith("## AAPL")


# --------------------------------------------------------------------------
# Empty invoke_next without converging -> swarm forces exit
# --------------------------------------------------------------------------


def test_swarm_run_force_exits_on_empty_invoke_next_without_converge() -> None:
    """GP returned no tasks but said ``converged=false``; swarm must stop."""
    def fake_chat(**kwargs: Any) -> dict[str, Any]:
        return _wrap_chat_response(
            content=json.dumps({
                "reasoning": "I'm confused",
                "pruning_notes": None,
                "converged": False,
                "invoke_next": [],
                "final_answer": None,
            })
        )

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=fake_chat):
        result = cb_swarm.run("query", model="fake-model")

    # No invoke_next + no converge = swarm forces convergence and hands
    # off to the postprocessor. With no specialist output to synthesize,
    # the postprocessor produces an empty answer and the deterministic
    # fallback kicks in -- the run lands as success=False with a
    # well-shaped placeholder ``answer`` rather than crashing.
    assert result["success"] is False
    assert result["rounds"] == 1
    assert result["specialist_outputs"] == []
    assert isinstance(result["answer"], dict)
    assert result["answer"]["answerable"] is False
    assert result["answer"]["answer_summary"] == ""
    assert result["error"] == "postprocessor_empty_summary"


# --------------------------------------------------------------------------
# Partial-failure tolerance: one specialist returns garbage, swarm still ships
# --------------------------------------------------------------------------


def test_swarm_run_tolerates_one_specialist_emitting_non_json() -> None:
    invoked = INVESTMENT_ANALYST_ROSTER
    bad_specialist = invoked[0]
    gp_calls = {"n": 0}

    def fake_chat(**kwargs: Any) -> dict[str, Any]:
        if _is_gp_call(kwargs):
            gp_calls["n"] += 1
            if gp_calls["n"] == 1:
                return _wrap_chat_response(
                    content=json.dumps(_gp_orchestrate_payload(invoked))
                )
            return _wrap_chat_response(
                content=json.dumps(_gp_converge_payload())
            )
        if _is_specialist_call(kwargs, bad_specialist):
            return _wrap_chat_response(content="oops, no JSON here")
        for k in invoked:
            if _is_specialist_call(kwargs, k):
                return _wrap_chat_response(
                    content=json.dumps(_specialist_payload(key=k)),
                )
        raise AssertionError("unrouted chat")

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=fake_chat):
        result = cb_swarm.run("Should I buy AAPL?", model="fake-model")

    assert result["success"] is True
    bad_out = next(o for o in result["specialist_outputs"]
                   if o["key"] == bad_specialist)
    assert bad_out["payload"] is None
    assert bad_out["error"] is None  # specialist completed, just no JSON
    assert "oops" in bad_out["thread_text"]
    good = [o for o in result["specialist_outputs"] if o["key"] != bad_specialist]
    assert all(o["payload"] is not None for o in good)


# --------------------------------------------------------------------------
# Re-invocation across rounds
# --------------------------------------------------------------------------


def test_swarm_run_re_invokes_specialist_in_second_round() -> None:
    """GP invokes ``stock_analyst`` in round 1, then again in round 2."""
    gp_calls = {"n": 0}
    spec_call_count = {"n": 0}

    def fake_chat(**kwargs: Any) -> dict[str, Any]:
        if _is_gp_call(kwargs):
            gp_calls["n"] += 1
            if gp_calls["n"] == 1:
                return _wrap_chat_response(
                    content=json.dumps(
                        _gp_orchestrate_payload(("stock_analyst",))
                    )
                )
            if gp_calls["n"] == 2:
                # Round 2: re-invoke same specialist with refined ask.
                return _wrap_chat_response(
                    content=json.dumps({
                        "reasoning": "need more depth on technicals",
                        "pruning_notes": "ignore the macro angle",
                        "converged": False,
                        "invoke_next": [
                            {
                                "persona_key": "stock_analyst",
                                "instruction": "Look at stock_analyst price chart depth",
                            },
                        ],
                        "final_answer": None,
                    })
                )
            return _wrap_chat_response(content=json.dumps(_gp_converge_payload()))
        if _is_specialist_call(kwargs, "stock_analyst"):
            spec_call_count["n"] += 1
            return _wrap_chat_response(
                content=json.dumps(_specialist_payload(key="stock_analyst"))
            )
        raise AssertionError("unrouted chat")

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=fake_chat):
        result = cb_swarm.run(
            "AAPL deep dive", model="fake-model", roster=("stock_analyst",),
            max_rounds=3,
        )

    assert result["success"] is True
    assert result["rounds"] == 3
    assert spec_call_count["n"] == 2  # invoked once per round
    rounds = sorted(o["round"] for o in result["specialist_outputs"])
    assert rounds == [1, 2]
    # Pruning note should land on the round-2 GP record.
    sr2 = result["synthesizer_rounds"][1]
    assert sr2["pruning_notes"] == "ignore the macro angle"


# --------------------------------------------------------------------------
# Tool round-trip: model emits a tool_call, swarm dispatches via kernel verb
# --------------------------------------------------------------------------


def test_swarm_run_round_trips_a_tool_call_through_kernel_verb() -> None:
    """When a specialist emits a ``tool_call``, it must dispatch through
    ``alphacumen.tools`` -> ``coralbricks.sandbox.tools`` and the second
    LLM turn must see the tool result.

    Single-specialist roster keeps the script tight (one tool_call
    -> one final-answer turn -> GP converges).
    """
    captured_bm25_kwargs: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        captured_bm25_kwargs.update(kwargs)
        return {
            "index": kwargs["index"],
            "hits": [
                {
                    "id": "doc1",
                    "score": 1.5,
                    "source": {"title": "Apple beats earnings"},
                }
            ],
        }

    gp_calls = {"n": 0}
    spec_call_count = {"n": 0}

    def fake_chat(**kwargs: Any) -> dict[str, Any]:
        if _is_gp_call(kwargs):
            gp_calls["n"] += 1
            if gp_calls["n"] == 1:
                return _wrap_chat_response(
                    content=json.dumps(
                        _gp_orchestrate_payload(("risk_analyst",))
                    )
                )
            return _wrap_chat_response(content=json.dumps(_gp_converge_payload()))
        # Specialist
        spec_call_count["n"] += 1
        if spec_call_count["n"] == 1:
            return _wrap_chat_response(
                tool_calls=[
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {
                            "name": "bm25_gdelt",
                            "arguments": json.dumps(
                                {"query": "apple earnings", "k": 3}
                            ),
                        },
                    },
                ]
            )
        return _wrap_chat_response(
            content=json.dumps(_specialist_payload(key="risk_analyst"))
        )

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=fake_chat), \
         patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        result = cb_swarm.run(
            "Should I buy AAPL right now?",
            model="fake-model",
            roster=("risk_analyst",),
        )

    assert result["success"] is True
    assert captured_bm25_kwargs["index"] == GDELT_EVENTS_INDEX
    assert captured_bm25_kwargs["query"] == "apple earnings"
    assert captured_bm25_kwargs["k"] == 3

    spec_out = result["specialist_outputs"][0]
    assert spec_out["payload"] == _specialist_payload(key="risk_analyst")
    traj = spec_out["trajectory"]
    kinds = [s["kind"] for s in traj["steps"]]
    assert kinds == ["llm", "tool", "llm"]
    tool_step = traj["steps"][1]
    assert tool_step["name"] == "bm25_gdelt"
    assert tool_step["has_error"] is False


# --------------------------------------------------------------------------
# Bad inputs
# --------------------------------------------------------------------------


def test_swarm_run_returns_error_for_unknown_specialist_key() -> None:
    def fake_chat(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("LLM should not be called for an invalid roster")

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=fake_chat):
        result = cb_swarm.run(
            "anything",
            model="fake-model",
            roster=("not_a_real_specialist",),
        )

    assert result["success"] is False
    assert result["error"] is not None
    assert "unknown specialist" in result["error"]
    assert result["specialist_outputs"] == []


def test_swarm_run_rejects_unsupported_pipeline() -> None:
    def fake_chat(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("LLM should not be called for a bad pipeline")

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=fake_chat):
        result = cb_swarm.run(
            "anything", model="fake-model", pipeline="not_a_real_pipeline",
        )

    assert result["success"] is False
    assert "unsupported pipeline" in result["error"]
    assert result["pipeline"] == "not_a_real_pipeline"
    assert result["specialist_outputs"] == []


# --------------------------------------------------------------------------
# Tool-name fallback in common-thread preview
# --------------------------------------------------------------------------


def test_specialist_thread_text_falls_back_to_tool_names_when_empty() -> None:
    """When a specialist's final message is empty (e.g. provider emitted
    only ``<tool_call>`` envelopes that the runtime stripped), the
    common-thread preview must surface the dispatched tool names so the
    GP and the IA UI can see what the specialist actually did."""
    from harness.react import Step, Trajectory

    traj = Trajectory()
    traj.steps.append(
        Step(
            kind="llm", name="llm.chat",
            started_at_ms=0, elapsed_ms=10,
        )
    )
    traj.steps.append(
        Step(
            kind="tool", name="query_graph",
            started_at_ms=0, elapsed_ms=20,
        )
    )
    traj.steps.append(
        Step(
            kind="tool", name="get_full_text",
            started_at_ms=0, elapsed_ms=30,
            has_error=True, error_message="boom",
        )
    )

    out = cb_swarm._specialist_thread_text(None, "", trajectory=traj)
    assert out == "[no final summary; tools called: query_graph, get_full_text(error)]"


def test_specialist_thread_text_returns_empty_when_no_tools_and_no_text() -> None:
    """Pure no-op (no payload, no raw text, no tool steps) -> empty string,
    same as before -- we don't fabricate a row."""
    from harness.react import Trajectory

    out = cb_swarm._specialist_thread_text(None, "", trajectory=Trajectory())
    assert out == ""


def test_specialist_thread_text_prefers_payload_summary_over_tool_fallback() -> None:
    """If the specialist DID emit a parseable payload, use its
    ``answer_summary`` as before -- the tool-name fallback only kicks
    in when both payload and raw are empty."""
    from harness.react import Step, Trajectory

    traj = Trajectory()
    traj.steps.append(
        Step(
            kind="tool", name="query_graph",
            started_at_ms=0, elapsed_ms=10,
        )
    )
    payload = {"answer_summary": "## Real summary from the model"}
    out = cb_swarm._specialist_thread_text(payload, "", trajectory=traj)
    assert out == "## Real summary from the model"


