# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for :mod:`harness.react`.

The goal here is to exercise every branch of :func:`run_react`
(natural exit, multi-round tool dispatch, parallel tool_calls, tool
errors, unknown tool, malformed arguments, max_steps exhaustion,
provider exception, no-choices response) plus
:func:`chat_with_retry` (transient retries on rate-limit + CUDA,
hard-fail on anything else) without touching the platform LLM
proxy. We do that by patching ``coralbricks.sandbox.llm.chat`` with
a deterministic scripted fake that pulls the next response off a
list. This is the same patch surface :mod:`harness.synthesizer (legacy, removed)` uses,
so the synthesizer tests benefit from the same harness.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import patch

import pytest

from harness import react as cb_runtime
from harness.react import (
    Step,
    Trajectory,
    chat_with_retry,
    extract_json_payload,
    run_react,
)
from alphacumen.tools import Tool


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _wrap_assistant(content: str = "", tool_calls=None) -> dict[str, Any]:
    """Build the choices[0].message envelope ``run_react`` expects."""
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return msg


def _wrap_chat_response(
    *,
    content: str = "",
    tool_calls=None,
    prompt_tokens: int = 5,
    completion_tokens: int = 7,
    total_tokens: int = 12,
    cached_tokens: int = 0,
) -> dict[str, Any]:
    """Match the wire shape returned by ``coralbricks.sandbox.llm.chat``.

    Includes the OpenAI-shape ``prompt_tokens_details.cached_tokens``
    nested block so the runtime's cached-token accounting is exercised
    by the same fixture every test goes through.
    """
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    if cached_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return {
        "model": "test-model",
        "response": {
            "choices": [{"message": _wrap_assistant(content, tool_calls)}],
            "usage": usage,
        },
    }


def _tool_call(
    *, call_id: str, name: str, arguments: Any,
) -> dict[str, Any]:
    """Mirror the OpenAI tool_call shape (arguments is a JSON string)."""
    raw_args = (
        arguments
        if isinstance(arguments, str)
        else json.dumps(arguments)
    )
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": raw_args},
    }


class _ScriptedChat:
    """Pop responses off a list, recording the kwargs for each call.

    Lets a test assert both how many LLM calls happened *and* what
    tools schema / messages were on the wire when each call fired
    -- the runtime is supposed to drop ``tools`` on the last
    allowed round, and we want a regression test for that.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(
                "scripted chat exhausted; runtime made more calls than expected"
            )
        return self.responses.pop(0)


_NOOP_TOOL_PARAMS = {"type": "object", "properties": {}, "additionalProperties": False}


def _echo_tool() -> Tool:
    """Tool that echoes its kwargs and tracks invocation count."""
    state: dict[str, Any] = {"calls": []}

    def fn(**kwargs: Any) -> dict[str, Any]:
        state["calls"].append(kwargs)
        return {"echo": kwargs, "n": len(state["calls"])}

    t = Tool(
        name="echo",
        description="Echo arguments back",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": True,
        },
        fn=fn,
    )
    # Stash the state so tests can read it without rebuilding the closure.
    t.__dict__["_state"] = state  # frozen dataclass; bypass via __dict__
    return t


def _exploding_tool(exc: Exception) -> Tool:
    def fn(**kwargs: Any) -> dict[str, Any]:
        raise exc

    return Tool(
        name="boom",
        description="Always raises",
        parameters=_NOOP_TOOL_PARAMS,
        fn=fn,
    )


# --------------------------------------------------------------------------
# Trajectory data model
# --------------------------------------------------------------------------


def test_trajectory_default_token_usage_is_zero_in_prod_naming() -> None:
    traj = Trajectory()
    assert traj.token_usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "tool_calls": 0,
    }
    assert traj.steps == []
    assert traj.final_message is None
    assert traj.rounds == 0
    assert traj.error is None


def test_step_is_frozen() -> None:
    s = Step(
        kind="llm",
        name="llm.chat",
        started_at_ms=0,
        elapsed_ms=1,
    )
    with pytest.raises(Exception):
        s.kind = "tool"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Natural exit: model returns content, no tool_calls
# --------------------------------------------------------------------------


def test_run_react_exits_on_first_no_tool_response() -> None:
    chat = _ScriptedChat([_wrap_chat_response(content="hello world")])
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="test-model",
            system_prompt="be helpful",
            user_message="hi",
            tools=(),
            max_steps=4,
        )

    assert traj.error is None
    assert traj.rounds == 1
    assert traj.final_message is not None
    assert traj.final_message["content"] == "hello world"
    assert len(traj.steps) == 1
    assert traj.steps[0].kind == "llm"
    assert traj.steps[0].has_error is False
    # 5 prompt -> input_tokens, 7 completion -> output_tokens.
    assert traj.token_usage["input_tokens"] == 5
    assert traj.token_usage["output_tokens"] == 7
    assert traj.token_usage["cached_tokens"] == 0
    assert traj.token_usage["tool_calls"] == 0


def test_run_react_passes_system_and_user_messages() -> None:
    chat = _ScriptedChat([_wrap_chat_response(content="ok")])
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        run_react(
            model="m1",
            system_prompt="SYS",
            user_message="USER",
            tools=(),
            max_steps=2,
        )

    msgs = chat.calls[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1] == {"role": "user", "content": "USER"}


def test_run_react_picks_up_cached_tokens_from_nested_details() -> None:
    chat = _ScriptedChat(
        [_wrap_chat_response(content="ok", cached_tokens=42)]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(),
            max_steps=2,
        )
    assert traj.token_usage["cached_tokens"] == 42


# --------------------------------------------------------------------------
# Tool dispatch
# --------------------------------------------------------------------------


def test_run_react_dispatches_tool_then_finalizes() -> None:
    tool = _echo_tool()
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(
                        call_id="c1",
                        name="echo",
                        arguments={"value": "hi"},
                    ),
                ],
            ),
            _wrap_chat_response(content="done"),
        ]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=4,
        )

    assert traj.error is None
    assert traj.rounds == 2
    assert traj.final_message["content"] == "done"

    state = tool.__dict__["_state"]
    assert state["calls"] == [{"value": "hi"}]

    kinds = [s.kind for s in traj.steps]
    assert kinds == ["llm", "tool", "llm"]
    tool_step = traj.steps[1]
    assert tool_step.name == "echo"
    assert tool_step.has_error is False

    # Token totals are summed across both LLM calls; tool_calls is 1.
    assert traj.token_usage["input_tokens"] == 10
    assert traj.token_usage["output_tokens"] == 14
    assert traj.token_usage["tool_calls"] == 1


def test_run_react_dispatches_parallel_tool_calls_in_one_round() -> None:
    tool = _echo_tool()
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(
                        call_id="c1",
                        name="echo",
                        arguments={"value": "a"},
                    ),
                    _tool_call(
                        call_id="c2",
                        name="echo",
                        arguments={"value": "b"},
                    ),
                ],
            ),
            _wrap_chat_response(content="done"),
        ]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=4,
        )

    state = tool.__dict__["_state"]
    assert state["calls"] == [{"value": "a"}, {"value": "b"}]
    # Two tool steps in the same round still count as one llm round.
    assert traj.rounds == 2
    tool_steps = [s for s in traj.steps if s.kind == "tool"]
    assert len(tool_steps) == 2
    assert traj.token_usage["tool_calls"] == 2

    # The follow-up LLM call should have seen both tool messages.
    follow_up_msgs = chat.calls[1]["messages"]
    tool_msgs = [m for m in follow_up_msgs if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]


def test_run_react_records_unknown_tool_as_first_class_message() -> None:
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(
                        call_id="c1",
                        name="ghost",
                        arguments={"x": 1},
                    ),
                ],
            ),
            _wrap_chat_response(content="recovered"),
        ]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(),
            max_steps=4,
        )

    assert traj.error is None
    assert traj.final_message["content"] == "recovered"
    tool_step = next(s for s in traj.steps if s.kind == "tool")
    assert tool_step.has_error is True
    assert tool_step.error_message == "unknown tool"

    follow_up_msgs = chat.calls[1]["messages"]
    tool_msg = next(m for m in follow_up_msgs if m.get("role") == "tool")
    assert "unknown tool" in tool_msg["content"]
    assert "ghost" in tool_msg["content"]


def test_run_react_recovers_from_tool_exception() -> None:
    tool = _exploding_tool(ValueError("kaboom"))
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(
                        call_id="c1",
                        name="boom",
                        arguments={},
                    ),
                ],
            ),
            _wrap_chat_response(content="recovered"),
        ]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=4,
        )

    assert traj.error is None
    tool_step = next(s for s in traj.steps if s.kind == "tool")
    assert tool_step.has_error is True
    assert "ValueError" in (tool_step.error_message or "")

    follow_up_msgs = chat.calls[1]["messages"]
    tool_msg = next(m for m in follow_up_msgs if m.get("role") == "tool")
    assert "ValueError" in tool_msg["content"]
    assert "kaboom" in tool_msg["content"]


def test_run_react_handles_malformed_tool_arguments() -> None:
    tool = _echo_tool()
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(
                        call_id="c1",
                        name="echo",
                        arguments="not json {{",  # invalid JSON string
                    ),
                ],
            ),
            _wrap_chat_response(content="recovered"),
        ]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=4,
        )

    assert traj.error is None
    # The exploding parse should not have invoked the underlying fn.
    state = tool.__dict__["_state"]
    assert state["calls"] == []
    tool_step = next(s for s in traj.steps if s.kind == "tool")
    assert tool_step.has_error is True
    assert tool_step.error_message and "could not parse" in tool_step.error_message


# --------------------------------------------------------------------------
# Max steps + provider failures
# --------------------------------------------------------------------------


def test_run_react_drops_tools_on_last_round() -> None:
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(
                        call_id="c1",
                        name="echo",
                        arguments={"value": "x"},
                    ),
                ],
            ),
            _wrap_chat_response(content="forced final"),
        ]
    )
    tool = _echo_tool()
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=2,
        )

    assert traj.error is None
    assert traj.rounds == 2
    # First call should include tool schemas; the second (last) round
    # must not, so the model is forced to answer.
    assert "tools" in chat.calls[0]
    assert "tools" not in chat.calls[1]


class _SnapshotChat(_ScriptedChat):
    """``_ScriptedChat`` that deep-copies ``messages`` on every call.

    The base helper records the live ``messages`` list reference, so
    every recorded call ends up pointing at the same final mutated
    list -- fine for asserting "schemas absent on the last round"
    (a kwarg, not a list mutation) but not fine for asserting which
    system messages were on the wire at a specific round, since the
    runtime appends as the conversation grows. Snapshot-on-record
    makes per-round message assertions reliable.
    """

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        if "messages" in kwargs:
            kwargs = {**kwargs, "messages": copy.deepcopy(kwargs["messages"])}
        return super().__call__(**kwargs)


def _system_message_contents(call_kwargs: dict[str, Any]) -> list[str]:
    """Pull the system-message bodies out of a recorded chat-kwargs.

    The runtime injects scoped system messages (penultimate-round
    warning, last-round synthesize-now, must-retrieve coercion) by
    appending them to the running ``messages`` list. Pair this with
    :class:`_SnapshotChat` so the slice reflects what the model
    actually saw at that round.
    """
    msgs = call_kwargs.get("messages") or []
    return [
        str(m.get("content") or "")
        for m in msgs
        if (m.get("role") == "system")
    ]


def test_run_react_injects_penultimate_budget_warning_when_max_steps_at_least_3() -> None:
    """The runtime should fire a soft "1 round left" warning on the
    second-to-last round so the model has a chance to start
    synthesizing before tools disappear. Regression for the
    Cerebras+Qwen "stranded XML dispatch on the last round"
    failure mode (Vals AI row 7 sector_analyst, request df248c96):
    the model went 7 native dispatches deep, then on round 8 (the
    cliff) emitted one more tool call in XML format which the
    last-round shim discarded. A penultimate-round warning gives
    well-behaved models the opportunity to skip that final
    dispatch."""
    chat = _SnapshotChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(call_id="c1", name="echo", arguments={"value": "a"}),
                ],
            ),
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(call_id="c2", name="echo", arguments={"value": "b"}),
                ],
            ),
            # Round 3 (penultimate): the model still issues a dispatch
            # so the loop runs to the last round and we can assert on
            # round 4's recorded messages.
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(call_id="c3", name="echo", arguments={"value": "c"}),
                ],
            ),
            _wrap_chat_response(content="forced final"),
        ]
    )
    tool = _echo_tool()
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="seed-system",
            user_message="u",
            tools=(tool,),
            max_steps=4,
        )

    assert traj.error is None
    # Round 1 (index 0) and round 2 (index 1): no budget warnings yet.
    for early in (0, 1):
        sys_msgs = _system_message_contents(chat.calls[early])
        assert not any(
            "Budget check" in s or "you have used all your tool calls" in s.lower()
            for s in sys_msgs
        ), f"unexpected budget hint on round {early + 1}: {sys_msgs!r}"

    # Round 3 (index 2) is the penultimate round. The warning should
    # be present, but the hard "synthesize NOW" message must NOT be
    # (it belongs to the next round).
    pen_sys = _system_message_contents(chat.calls[2])
    assert any("Budget check" in s for s in pen_sys), (
        f"expected penultimate budget warning on round 3, got: {pen_sys!r}"
    )
    assert not any(
        "do not call any more tools" in s.lower() for s in pen_sys
    ), f"last-round message must not appear on penultimate round: {pen_sys!r}"
    # Tools are still on the wire for the penultimate round -- the
    # whole point of the warning is to give a tools-allowed turn for
    # the model to finish gracefully.
    assert "tools" in chat.calls[2]

    # Round 4 (index 3) is the last round: hard message + no tools.
    last_sys = _system_message_contents(chat.calls[3])
    assert any(
        "do not call any more tools" in s.lower() for s in last_sys
    ), f"expected last-round message on round 4, got: {last_sys!r}"
    assert "tools" not in chat.calls[3]


def test_run_react_skips_penultimate_warning_when_max_steps_below_3() -> None:
    """At ``max_steps=2`` the penultimate round IS the first round,
    which would mean the model gets a "1 round left" warning before
    it has done anything -- noise. The runtime gates the penultimate
    hint on ``max_steps >= 3`` so tiny budgets only see the existing
    last-round contract."""
    chat = _SnapshotChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(call_id="c1", name="echo", arguments={"value": "x"}),
                ],
            ),
            _wrap_chat_response(content="forced final"),
        ]
    )
    tool = _echo_tool()
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="seed-system",
            user_message="u",
            tools=(tool,),
            max_steps=2,
        )

    assert traj.error is None
    # Round 1 (index 0) is BOTH the first and the penultimate round
    # in a 2-step budget. The warning must NOT fire here -- the gate
    # is ``max_steps >= 3``.
    first_sys = _system_message_contents(chat.calls[0])
    assert not any("Budget check" in s for s in first_sys), (
        f"penultimate hint must be skipped at max_steps=2, got: {first_sys!r}"
    )
    # The last-round message still fires on round 2.
    last_sys = _system_message_contents(chat.calls[1])
    assert any("do not call any more tools" in s.lower() for s in last_sys)


def test_run_react_penultimate_warning_fires_at_max_steps_exactly_3() -> None:
    """Boundary check on the ``max_steps >= 3`` gate."""
    chat = _SnapshotChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(call_id="c1", name="echo", arguments={"value": "x"}),
                ],
            ),
            # Penultimate round still dispatches so the loop reaches
            # the last round and we can inspect both messages.
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(call_id="c2", name="echo", arguments={"value": "y"}),
                ],
            ),
            _wrap_chat_response(content="forced final"),
        ]
    )
    tool = _echo_tool()
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="seed-system",
            user_message="u",
            tools=(tool,),
            max_steps=3,
        )

    assert traj.error is None
    # Round 1: clean.
    first_sys = _system_message_contents(chat.calls[0])
    assert not any("Budget check" in s for s in first_sys)
    # Round 2 (penultimate): warning fires.
    pen_sys = _system_message_contents(chat.calls[1])
    assert any("Budget check" in s for s in pen_sys)
    # Round 3 (last): hard message.
    last_sys = _system_message_contents(chat.calls[2])
    assert any("do not call any more tools" in s.lower() for s in last_sys)


def test_run_react_strips_inline_tool_call_envelopes_on_last_round() -> None:
    """Defends against the Cerebras+Qwen failure mode where the model
    emits ``<tool_call>...</tool_call>`` text on the synthesis round.

    Regression for the risk_analyst silent-failure bug: with
    ``max_steps=2`` the runtime drops ``tools=schemas`` from round 2's
    LLM call so the provider can't structurally emit a tool call -- but
    Qwen returns inline text envelopes anyway. Without the
    last-round shim guard the runtime would extract those envelopes,
    dispatch them, exhaust the for-loop, and return ``react loop hit
    max_steps=N without a final answer``. The fix: on the last round,
    treat envelopes as no-ops and use the cleaned prose as the final
    answer.
    """
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(
                        call_id="c1",
                        name="echo",
                        arguments={"value": "x"},
                    ),
                ],
            ),
            _wrap_chat_response(
                content=(
                    "Final synthesis here.\n"
                    "<tool_call>\n"
                    '{"name": "echo", "arguments": {"value": "ghost"}}\n'
                    "</tool_call>"
                ),
            ),
        ]
    )
    tool = _echo_tool()
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=2,
        )

    assert traj.error is None, traj.error
    assert traj.final_message is not None
    final_content = str(traj.final_message.get("content") or "")
    assert "Final synthesis here." in final_content
    assert "<tool_call>" not in final_content, (
        "envelope syntax leaked through to the user-visible answer"
    )
    assert "ghost" not in final_content, (
        "stripped envelope arguments leaked into the final message"
    )
    tool_step_names = [
        s.name for s in traj.steps if s.kind == "tool"
    ]
    assert tool_step_names == ["echo"], (
        f"expected exactly one (round 1) tool dispatch, got {tool_step_names!r} "
        "-- the round-2 inline envelope must NOT have been dispatched"
    )


def test_run_react_records_max_steps_exhaustion() -> None:
    tool = _echo_tool()
    # Two rounds where the model keeps calling the tool. With
    # max_steps=2, the 2nd round drops tools, so the model can't call
    # again -- but here we force it to anyway by scripting another
    # tool_calls response. The runtime should bail out with an error.
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(
                        call_id="c1",
                        name="echo",
                        arguments={"value": "x"},
                    ),
                ],
            ),
            _wrap_chat_response(
                tool_calls=[
                    _tool_call(
                        call_id="c2",
                        name="echo",
                        arguments={"value": "y"},
                    ),
                ],
            ),
        ]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=2,
        )

    assert traj.final_message is None
    assert traj.error is not None
    assert "max_steps=2" in traj.error
    assert traj.rounds == 2


def test_run_react_captures_provider_exception() -> None:
    def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("provider down")

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=boom):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(),
            max_steps=4,
        )

    assert traj.final_message is None
    assert traj.error is not None
    assert "RuntimeError" in traj.error
    assert "provider down" in traj.error
    assert len(traj.steps) == 1
    assert traj.steps[0].has_error is True


def test_run_react_handles_no_choices_in_response() -> None:
    chat = _ScriptedChat(
        [{"model": "m", "response": {"choices": [], "usage": {}}}]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(),
            max_steps=4,
        )

    assert traj.error is not None
    assert "no choices" in traj.error


# --------------------------------------------------------------------------
# chat_with_retry
# --------------------------------------------------------------------------


def test_chat_with_retry_returns_first_success_immediately() -> None:
    chat = _ScriptedChat([_wrap_chat_response(content="ok")])
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        env = chat_with_retry(model="m", messages=[])
    assert env["response"]["choices"][0]["message"]["content"] == "ok"
    assert len(chat.calls) == 1


def test_chat_with_retry_recovers_from_rate_limit_error(monkeypatch) -> None:
    """Two 429s, then success: should sleep + retry rather than raise."""
    sleeps: list[float] = []
    monkeypatch.setattr(cb_runtime.time, "sleep", lambda s: sleeps.append(s))

    attempts: dict[str, int] = {"n": 0}

    def flaky(**kwargs: Any) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("HTTP 429 too_many_requests")
        return _wrap_chat_response(content="finally")

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=flaky):
        env = chat_with_retry(model="m", messages=[])

    assert env["response"]["choices"][0]["message"]["content"] == "finally"
    assert attempts["n"] == 3
    assert sleeps == [5, 10]  # 5s, then 10s backoff for the two retries.


def test_chat_with_retry_recovers_from_cuda_error(monkeypatch) -> None:
    """One CUDA hiccup, then success."""
    sleeps: list[float] = []
    monkeypatch.setattr(cb_runtime.time, "sleep", lambda s: sleeps.append(s))

    attempts: dict[str, int] = {"n": 0}

    def flaky(**kwargs: Any) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("RMSNorm: cuda error invalid argument")
        return _wrap_chat_response(content="ok")

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=flaky):
        env = chat_with_retry(model="m", messages=[])

    assert env["response"]["choices"][0]["message"]["content"] == "ok"
    assert attempts["n"] == 2
    assert sleeps == [2]  # 2s backoff for CUDA.


def test_chat_with_retry_does_not_retry_non_transient_errors(
    monkeypatch,
) -> None:
    """Auth errors and the like must propagate immediately (no sleep)."""
    sleeps: list[float] = []
    monkeypatch.setattr(cb_runtime.time, "sleep", lambda s: sleeps.append(s))

    attempts: dict[str, int] = {"n": 0}

    def hard_fail(**kwargs: Any) -> dict[str, Any]:
        attempts["n"] += 1
        raise ValueError("invalid api key")

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=hard_fail):
        with pytest.raises(ValueError, match="invalid api key"):
            chat_with_retry(model="m", messages=[])

    assert attempts["n"] == 1
    assert sleeps == []


def test_chat_with_retry_gives_up_after_max_attempts(monkeypatch) -> None:
    monkeypatch.setattr(cb_runtime.time, "sleep", lambda _s: None)
    attempts: dict[str, int] = {"n": 0}

    def always_429(**kwargs: Any) -> dict[str, Any]:
        attempts["n"] += 1
        raise RuntimeError("HTTP 429 try again soon")

    with patch.object(cb_runtime.cb_llm, "chat", side_effect=always_429):
        with pytest.raises(RuntimeError, match="429"):
            chat_with_retry(model="m", messages=[], max_attempts=3)
    assert attempts["n"] == 3


# --------------------------------------------------------------------------
# extract_json_payload
# --------------------------------------------------------------------------


def test_extract_json_payload_handles_fenced_block() -> None:
    text = "Here is the answer:\n```json\n{\"a\": 1, \"b\": [2, 3]}\n```"
    out = extract_json_payload(text)
    assert out == {"a": 1, "b": [2, 3]}


def test_extract_json_payload_handles_bare_object() -> None:
    text = '{"x": "y"}'
    assert extract_json_payload(text) == {"x": "y"}


def test_extract_json_payload_handles_object_with_prose_around() -> None:
    text = "before {\"k\": 1} after"
    assert extract_json_payload(text) == {"k": 1}


def test_extract_json_payload_returns_none_for_empty_or_invalid() -> None:
    assert extract_json_payload("") is None
    assert extract_json_payload("just words, no json") is None


# --------------------------------------------------------------------------
# Inline <tool_call> envelope shim (Qwen / Hermes fallback)
# --------------------------------------------------------------------------


def test_extract_text_tool_calls_returns_empty_when_marker_absent() -> None:
    tcs, cleaned = cb_runtime._extract_text_tool_calls("just prose, no envelope")
    assert tcs == []
    assert cleaned == "just prose, no envelope"


def test_extract_text_tool_calls_parses_single_envelope() -> None:
    content = (
        'I will look this up.\n<tool_call>\n'
        '{"name": "echo", "arguments": {"value": "hi"}}\n'
        '</tool_call>'
    )
    tcs, cleaned = cb_runtime._extract_text_tool_calls(content)
    assert len(tcs) == 1
    assert tcs[0]["type"] == "function"
    assert tcs[0]["function"]["name"] == "echo"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"value": "hi"}
    # Envelope stripped from the cleaned content.
    assert "<tool_call>" not in cleaned
    assert cleaned.startswith("I will look this up.")


def test_extract_text_tool_calls_parses_multiple_envelopes() -> None:
    content = (
        '<tool_call>\n{"name": "a", "arguments": {"x": 1}}\n</tool_call>'
        ' middle '
        '<tool_call>\n{"name": "b", "arguments": {"y": 2}}\n</tool_call>'
    )
    tcs, _ = cb_runtime._extract_text_tool_calls(content)
    assert [t["function"]["name"] for t in tcs] == ["a", "b"]
    assert {t["id"] for t in tcs} == {"call_text_0", "call_text_1"}


def test_extract_text_tool_calls_skips_malformed_json() -> None:
    content = (
        '<tool_call>{"name": "good", "arguments": {"k": 1}}</tool_call>'
        '<tool_call>{"name": "bad", "arguments": {not json</tool_call>'
    )
    tcs, _ = cb_runtime._extract_text_tool_calls(content)
    assert [t["function"]["name"] for t in tcs] == ["good"]


def test_extract_text_tool_calls_handles_missing_arguments() -> None:
    content = '<tool_call>{"name": "noop"}</tool_call>'
    tcs, _ = cb_runtime._extract_text_tool_calls(content)
    assert len(tcs) == 1
    assert json.loads(tcs[0]["function"]["arguments"]) == {}


def test_extract_text_tool_calls_parses_qwen_xml_envelope() -> None:
    """Qwen sometimes emits the second envelope flavour where the
    body is XML, not JSON. Captured verbatim from the Vals AI row 7
    sector_analyst trajectory (Coral request df248c96, cb-ia@0.0.151)
    where this format was the round-8 message that silently failed
    the specialist."""
    content = (
        "<tool_call>\n"
        "<function=get_full_text>\n"
        "<parameter=ref>\n"
        "sec:0001193125-23-233499:2.01\n"
        "</parameter>\n"
        "<parameter=max_chars>\n"
        "16000\n"
        "</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    tcs, cleaned = cb_runtime._extract_text_tool_calls(content)
    assert len(tcs) == 1
    assert tcs[0]["type"] == "function"
    assert tcs[0]["function"]["name"] == "get_full_text"
    args = json.loads(tcs[0]["function"]["arguments"])
    # Numeric values round-trip through json.loads, string IDs stay strings.
    assert args == {
        "ref": "sec:0001193125-23-233499:2.01",
        "max_chars": 16000,
    }
    assert "<tool_call>" not in cleaned
    assert "<function=" not in cleaned


def test_extract_text_tool_calls_parses_qwen_xml_with_nested_json_param() -> None:
    """XML parameter values that themselves contain JSON (filters,
    arrays) must round-trip as parsed objects, matching the way the
    native tool_calls dispatcher would receive them."""
    content = (
        "<tool_call>\n"
        "<function=bm25_sec>\n"
        "<parameter=query>TKO IMG On Location PBR</parameter>\n"
        "<parameter=k>5</parameter>\n"
        '<parameter=filters>{"ticker": "TKO", "form_type": ["8-K"]}'
        "</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    tcs, _ = cb_runtime._extract_text_tool_calls(content)
    assert len(tcs) == 1
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args["query"] == "TKO IMG On Location PBR"
    assert args["k"] == 5
    assert args["filters"] == {"ticker": "TKO", "form_type": ["8-K"]}


def test_extract_text_tool_calls_mixes_json_and_xml_envelopes() -> None:
    """A single assistant message can contain both envelope flavours
    interleaved (Qwen sometimes drifts mid-message). Both must be
    parsed in document order so the dispatch loop preserves the
    model's intended call sequence."""
    content = (
        '<tool_call>{"name": "echo", "arguments": {"x": 1}}</tool_call>'
        " middle prose "
        "<tool_call>\n"
        "<function=bm25_sec>\n"
        "<parameter=query>tko</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    tcs, _ = cb_runtime._extract_text_tool_calls(content)
    assert [t["function"]["name"] for t in tcs] == ["echo", "bm25_sec"]
    assert json.loads(tcs[0]["function"]["arguments"]) == {"x": 1}
    assert json.loads(tcs[1]["function"]["arguments"]) == {"query": "tko"}


def test_extract_text_tool_calls_xml_envelope_requires_function_block() -> None:
    """A ``<tool_call>`` whose body has no ``<function=...>`` block
    is malformed -- skip it rather than fabricating a call. This
    is the safety net that keeps stray ``<parameter>`` tags from
    triggering bogus dispatches."""
    content = (
        "<tool_call><parameter=x>1</parameter></tool_call>"
    )
    tcs, _ = cb_runtime._extract_text_tool_calls(content)
    assert tcs == []


def test_parse_xml_tool_call_body_returns_none_for_non_xml() -> None:
    """Unit-level guard so callers can rely on ``None`` to mean
    "fall through to the next parser" rather than have to
    distinguish empty-args from no-match."""
    assert cb_runtime._parse_xml_tool_call_body("just prose") is None
    assert cb_runtime._parse_xml_tool_call_body("") is None
    # An empty function name is also a no-match.
    assert cb_runtime._parse_xml_tool_call_body(
        "<function=>noop</function>"
    ) is None


def test_run_react_dispatches_text_envelope_tool_calls() -> None:
    """End-to-end: a Qwen-style response with `<tool_call>` text envelope
    must dispatch the tool exactly like an OpenAI-shape ``tool_calls``
    response would. This is the regression test for the prod NVIDIA 8-K
    bug (3 of 4 specialists silently no-op'd on cerebras+qwen)."""
    tool = _echo_tool()
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                content=(
                    "Looking that up.\n"
                    "<tool_call>\n"
                    '{"name": "echo", "arguments": {"value": "qwen"}}\n'
                    "</tool_call>"
                ),
            ),
            _wrap_chat_response(content="done"),
        ]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=4,
        )

    assert traj.error is None
    assert traj.rounds == 2
    assert traj.final_message["content"] == "done"
    # The tool actually ran with the parsed args.
    state = tool.__dict__["_state"]
    assert state["calls"] == [{"value": "qwen"}]
    # And the trajectory has the same llm/tool/llm shape as the
    # native-format dispatch test, so downstream consumers (Console,
    # synthesizer common-thread) see one canonical layout regardless
    # of provider.
    assert [s.kind for s in traj.steps] == ["llm", "tool", "llm"]
    assert traj.steps[1].name == "echo"
    assert traj.steps[1].has_error is False


def test_run_react_step_preview_falls_back_to_tool_names_when_content_empty() -> None:
    """Tool-only turns (model emitted only `<tool_call>` envelopes that
    the shim stripped) must surface the dispatched tool names in the
    LLM step's ``result_preview``, not show as an empty row in the
    trajectory UI."""
    tool = _echo_tool()
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                content=(
                    "<tool_call>"
                    '{"name": "echo", "arguments": {"value": "a"}}'
                    "</tool_call>"
                    "<tool_call>"
                    '{"name": "echo", "arguments": {"value": "b"}}'
                    "</tool_call>"
                ),
            ),
            _wrap_chat_response(content="done"),
        ]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=4,
        )

    llm_steps = [s for s in traj.steps if s.kind == "llm"]
    # First LLM turn was tool-only; preview should call out the tools.
    assert llm_steps[0].result_preview == "\u2192 echo, echo"
    # Second LLM turn had real prose; preview is the prose verbatim.
    assert llm_steps[1].result_preview == "done"


def test_run_react_text_envelope_does_not_clobber_native_tool_calls() -> None:
    """If the provider emits BOTH ``tool_calls`` and ``<tool_call>`` text
    (rare, but possible during model migrations), the native list wins
    and the text shim is a no-op. Belt-and-braces: we must never run a
    tool twice."""
    tool = _echo_tool()
    chat = _ScriptedChat(
        [
            _wrap_chat_response(
                content=(
                    "<tool_call>"
                    '{"name": "echo", "arguments": {"value": "TEXT"}}'
                    "</tool_call>"
                ),
                tool_calls=[
                    _tool_call(
                        call_id="c1",
                        name="echo",
                        arguments={"value": "NATIVE"},
                    ),
                ],
            ),
            _wrap_chat_response(content="done"),
        ]
    )
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=4,
        )

    state = tool.__dict__["_state"]
    # Only the native-shape call ran.
    assert state["calls"] == [{"value": "NATIVE"}]


# --------------------------------------------------------------------------
# Must-retrieve gate (min_tool_calls_before_final)
# --------------------------------------------------------------------------


def test_run_react_must_retrieve_gate_disabled_by_default() -> None:
    """With ``min_tool_calls_before_final=0`` (default) the loop accepts
    a no-tool first response immediately -- regression guard that the
    gate does not fire on personas that don't opt in."""
    chat = _ScriptedChat([_wrap_chat_response(content="done in one")])
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(),
            max_steps=4,
            # Default: min_tool_calls_before_final=0
        )
    assert traj.rounds == 1
    assert traj.final_message is not None
    assert traj.final_message["content"] == "done in one"
    assert traj.token_usage["tool_calls"] == 0


def test_run_react_must_retrieve_gate_coerces_when_model_skips_tools() -> None:
    """With the gate set to 1, a model that emits a final answer on
    turn 1 with zero tool calls gets a coercion system message
    appended and the loop continues. Once it actually issues a tool
    call (and the result comes back) the next no-tool message is
    accepted as final."""
    tool = _echo_tool()
    chat = _ScriptedChat([
        # Turn 1: model tries to short-circuit with a final answer.
        _wrap_chat_response(content="i know the answer"),
        # Turn 2 (after coercion): model issues a tool call.
        _wrap_chat_response(
            tool_calls=[
                _tool_call(
                    call_id="c1",
                    name="echo",
                    arguments={"value": "retrieved"},
                ),
            ],
        ),
        # Turn 3: model issues final answer with tool result in hand.
        _wrap_chat_response(content="grounded answer"),
    ])
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(tool,),
            max_steps=6,
            min_tool_calls_before_final=1,
        )
    assert traj.error is None
    assert traj.final_message is not None
    assert traj.final_message["content"] == "grounded answer"
    assert traj.token_usage["tool_calls"] == 1
    # The coercion appended a system message between turns 1 and 2.
    sys_msgs = [m for m in chat.calls[1]["messages"] if m.get("role") == "system"]
    assert any("STOP" in (m.get("content") or "") for m in sys_msgs)
    assert any(
        "tool retrieval" in (m.get("content") or "").lower() for m in sys_msgs
    )


def test_run_react_must_retrieve_gate_caps_coercions() -> None:
    """A stubborn model that refuses to retrieve across multiple
    coercion turns must eventually be allowed to terminate so we
    don't burn the entire step budget on coercion no-ops. The
    cap is :data:`_MAX_MUST_RETRIEVE_COERCIONS`; after that, the
    next no-tool message is accepted as final and the
    swarm-level enforcement gets the chance to discard the payload."""
    cap = cb_runtime._MAX_MUST_RETRIEVE_COERCIONS
    # cap+2 responses: cap+1 short-circuit attempts (coerced cap times,
    # then the (cap+1)th attempt is accepted because the budget is
    # exhausted), plus one extra to make sure the test doesn't miss-
    # count.
    responses = [
        _wrap_chat_response(content=f"refusing #{i}")
        for i in range(cap + 2)
    ]
    chat = _ScriptedChat(responses)
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(),
            max_steps=10,
            min_tool_calls_before_final=1,
        )
    assert traj.error is None
    assert traj.final_message is not None
    # The cap+1th response is the one that's accepted.
    assert traj.final_message["content"] == f"refusing #{cap}"
    # We made exactly cap+1 LLM calls (cap coerced + 1 accepted).
    assert traj.rounds == cap + 1
    assert traj.token_usage["tool_calls"] == 0


def test_run_react_must_retrieve_gate_skips_on_last_round() -> None:
    """On the very last allowed round the loop has already removed
    ``tools`` from the kwargs to force a synthesis turn -- coercing
    here would just produce another no-tool message we'd have to
    accept anyway. The gate must skip on the last round so we
    don't waste a step on a doomed coercion."""
    chat = _ScriptedChat([
        # max_steps=2 -> round 0 is normal, round 1 is the
        # last (no-tools) round. We send a no-tool answer on
        # turn 1; the gate would normally coerce, but we have
        # only one more round and that's the last one, so the
        # implementation should still coerce on round 0 (it does)
        # then on round 1 the gate skips (is_last_round).
        _wrap_chat_response(content="round 0 answer"),
        _wrap_chat_response(content="round 1 answer"),
    ])
    with patch.object(cb_runtime.cb_llm, "chat", side_effect=chat):
        traj = run_react(
            model="m",
            system_prompt="s",
            user_message="u",
            tools=(),
            max_steps=2,
            min_tool_calls_before_final=1,
        )
    # Round 0: coerced. Round 1: is_last_round, gate skips,
    # accept the answer.
    assert traj.rounds == 2
    assert traj.final_message is not None
    assert traj.final_message["content"] == "round 1 answer"
