# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``reef.agents.headless_claude`` -- drive the ``claude`` CLI as a reef agent.

Wraps ``claude -p ... --output-format stream-json`` as a Python class
so orchestration code (Polyp's Architect / Analyzer / Auto-suggester,
plus any other harness that shells out to Claude Code) stops reinventing
the same shape in bash: subprocess + timeout, stream-json JSONL log,
post-hoc parse for the assistant text tail used in retry context, and
optional Langfuse tracing.

Output Trajectory
-----------------

The parser maps Claude Code's stream-json into the canonical
:class:`reef.react.Trajectory` so callers see the same shape they'd see
from :func:`reef.react.run_react`:

- each ``type=assistant`` event with text content becomes a
  ``Step(kind="llm", name="claude.message", result_preview=<text>)``
- each ``type=assistant`` event with a ``tool_use`` block becomes a
  ``Step(kind="tool", name=<tool>, arguments=<input>)``; the matching
  ``type=user`` ``tool_result`` populates ``result_preview`` and
  ``has_error``
- the terminal ``type=result`` event populates
  :attr:`Trajectory.token_usage` and (on success) ``final_message``

Retry pattern
-------------

The driver itself runs one attempt and returns the trajectory + tail.
The multi-attempt retry-with-previous-tail loop stays in the caller --
each harness has its own success criterion (branch pushed,
queue row reached a terminal status, stdout matched a regex). Use
:meth:`ClaudeResult.tail` to grab the last N characters of assistant
text for the next attempt's prompt.

Example
-------

    from reef.agents.headless_claude import HeadlessClaude

    driver = HeadlessClaude(
        model="claude-sonnet-4-6",
        max_turns=150,
        timeout_s=1800,
        permission_mode="acceptEdits",
        skip_permissions=True,
    )
    result = driver.invoke(prompt=prompt, log_path="/path/to/log.jsonl")
    if result.timed_out:
        ...
    elif result.exit_code != 0:
        prev_tail = result.tail(3500)
        ...  # retry with prev_tail injected in the next prompt
    else:
        ...  # success
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from reef import _langfuse
from reef.react import Step, Trajectory, accumulate_usage

logger = logging.getLogger(__name__)

# Exit codes the GNU coreutils ``timeout`` command uses; matched on
# the subprocess return code so callers can distinguish wall-cap
# kills from in-band failures.
TIMEOUT_EXIT_CODE = 124


@dataclass
class ClaudeResult:
    """Outcome of one :meth:`HeadlessClaude.invoke` call.

    ``trajectory`` is the canonical reef :class:`Trajectory` parsed
    from the stream-json log; ``assistant_text`` is the concatenated
    text of every ``type=assistant`` text block in order (handy for
    grepping for sentinel strings, building retry context, or posting
    a Slack tail).
    """

    exit_code: int
    elapsed_s: float
    log_path: Path
    trajectory: Trajectory
    assistant_text: str = ""
    timed_out: bool = False
    raw_events: list[dict[str, Any]] = field(default_factory=list)

    def tail(self, n_chars: int = 3500) -> str:
        """Return the last ``n_chars`` of concatenated assistant text.

        Empty string when the session produced no assistant prose
        (common when ``claude`` died at startup before emitting anything).
        Used by callers to inject previous-attempt context into the next
        attempt's prompt.
        """
        if not self.assistant_text:
            return ""
        return self.assistant_text[-n_chars:]


class HeadlessClaude:
    """Reusable driver config for ``claude -p`` invocations.

    One instance per (model, max_turns, timeout, permission policy);
    call :meth:`invoke` per attempt. Stateless across calls -- the
    subprocess is spawned fresh every time.
    """

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-6",
        max_turns: int = 150,
        timeout_s: int = 1800,
        kill_after_s: int = 60,
        permission_mode: Optional[str] = "acceptEdits",
        skip_permissions: bool = True,
        verbose: bool = True,
        binary: str = "claude",
        timeout_binary: Optional[str] = None,
        extra_args: Sequence[str] = (),
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str | Path] = None,
        langfuse_pipeline: str = "headless-claude",
        oauth_token_path: Optional[str | Path] = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.timeout_s = timeout_s
        self.kill_after_s = kill_after_s
        self.permission_mode = permission_mode
        self.skip_permissions = skip_permissions
        self.verbose = verbose
        self.binary = binary
        # ``timeout`` is GNU coreutils; macOS ships ``gtimeout`` from
        # brew's coreutils. Pick the right one rather than failing
        # opaquely when only one is present.
        if timeout_binary is None:
            self.timeout_binary = (
                shutil.which("timeout") or shutil.which("gtimeout") or "timeout"
            )
        else:
            self.timeout_binary = timeout_binary
        self.extra_args = list(extra_args)
        self.env_overrides = dict(env) if env else {}
        self.cwd = Path(cwd) if cwd is not None else None
        self.langfuse_pipeline = langfuse_pipeline
        self.oauth_token_path = (
            Path(oauth_token_path).expanduser() if oauth_token_path else None
        )

    def _build_argv(self, prompt_arg_source: str) -> list[str]:
        """Argv for the subprocess. ``prompt_arg_source`` is the marker
        that callers see in logs; the actual prompt is passed via
        stdin to keep argv length bounded."""
        argv: list[str] = []
        if self.timeout_s and self.timeout_s > 0:
            argv += [self.timeout_binary, f"--kill-after={self.kill_after_s}",
                     str(self.timeout_s)]
        argv += [self.binary, "-p", prompt_arg_source]
        argv += ["--model", self.model]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        if self.skip_permissions:
            argv += ["--dangerously-skip-permissions"]
        argv += ["--max-turns", str(self.max_turns)]
        argv += ["--output-format", "stream-json"]
        if self.verbose:
            argv += ["--verbose"]
        argv += list(self.extra_args)
        return argv

    def _resolved_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Polyp's bash flow reads ~/.oat at every launch; do the same
        # here so callers don't have to handle token rotation outside.
        if self.oauth_token_path and self.oauth_token_path.exists():
            try:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = (
                    self.oauth_token_path.read_text().strip()
                )
            except OSError as exc:
                logger.warning(
                    "headless_claude: oauth token read from %s failed: %s",
                    self.oauth_token_path, exc,
                )
        env.update(self.env_overrides)
        return env

    def invoke(
        self,
        *,
        prompt: str,
        log_path: str | Path,
        attempt_label: Optional[str] = None,
    ) -> ClaudeResult:
        """Run one ``claude -p`` session, stream stdout/stderr to
        ``log_path``, parse, return the result.

        ``log_path`` is opened in append mode so callers running
        multi-attempt loops can keep one canonical log per run. The
        driver writes ``===== ATTEMPT <label> START =====`` and
        ``===== ATTEMPT <label> END (rc=...) =====`` sentinels around
        each call when ``attempt_label`` is set; the parser uses these
        to scope tail extraction to a single attempt.
        """
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        argv = self._build_argv(prompt_arg_source="(prompt via stdin)")
        # claude -p reads the prompt from argv (positional after -p),
        # not stdin. Re-emit argv with the literal prompt now that the
        # subprocess.Popen call needs it; the logged form above keeps
        # the placeholder so logs aren't bloated with the full prompt.
        argv_with_prompt = list(argv)
        # find the placeholder slot and replace it
        for i, a in enumerate(argv_with_prompt):
            if a == "(prompt via stdin)":
                argv_with_prompt[i] = prompt
                break

        env = self._resolved_env()
        cwd = str(self.cwd) if self.cwd is not None else None

        start_label = (
            f"===== ATTEMPT {attempt_label} START ====="
            if attempt_label is not None
            else None
        )
        end_template = (
            "===== ATTEMPT {label} END (rc={rc}, {dur}s) ====="
            if attempt_label is not None
            else None
        )

        t0 = time.time()
        with open(log_path, "a", buffering=1) as log_f:
            if start_label:
                log_f.write(start_label + "\n")
            log_f.write(
                f"[headless_claude] cmd: {shlex.join(argv)}\n"
            )
            try:
                proc = subprocess.Popen(
                    argv_with_prompt,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    env=env,
                    cwd=cwd,
                )
            except FileNotFoundError as exc:
                log_f.write(f"[headless_claude] launch failed: {exc}\n")
                if end_template:
                    log_f.write(end_template.format(label=attempt_label,
                                                   rc=127, dur=0) + "\n")
                empty_traj = Trajectory()
                empty_traj.error = f"claude binary not found: {exc}"
                return ClaudeResult(
                    exit_code=127,
                    elapsed_s=0.0,
                    log_path=log_path,
                    trajectory=empty_traj,
                    timed_out=False,
                )

            rc = proc.wait()
            elapsed = time.time() - t0
            timed_out = rc == TIMEOUT_EXIT_CODE
            if end_template:
                log_f.write(end_template.format(
                    label=attempt_label, rc=rc, dur=int(elapsed),
                ) + "\n")

        events = _read_log_events(log_path, attempt_label=attempt_label)
        traj, assistant_text = build_trajectory(events)
        if timed_out:
            traj.error = (
                f"claude -p timed out after {self.timeout_s}s (rc=124)"
            )
        elif rc != 0 and not traj.error:
            traj.error = f"claude -p exited rc={rc}"

        result = ClaudeResult(
            exit_code=rc,
            elapsed_s=elapsed,
            log_path=log_path,
            trajectory=traj,
            assistant_text=assistant_text,
            timed_out=timed_out,
            raw_events=events,
        )
        self._emit_trace(result, prompt)
        return result

    def _emit_trace(self, result: ClaudeResult, prompt: str) -> None:
        """Record one Langfuse generation per invocation if a trace is active."""
        trace = _langfuse.get_active()
        if trace is None:
            return
        try:
            usage_response = {
                "usage": {
                    "prompt_tokens": result.trajectory.token_usage.get("input_tokens", 0),
                    "completion_tokens": result.trajectory.token_usage.get("output_tokens", 0),
                },
                "choices": [{
                    "message": {
                        "content": result.assistant_text[:4000],
                    }
                }],
            }
            trace.record_chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt[:8000]}],
                response=usage_response,
                latency_ms=int(result.elapsed_s * 1000),
                error=RuntimeError(result.trajectory.error)
                if result.trajectory.error else None,
            )
        except Exception as exc:  # noqa: BLE001 -- tracing must never fail the run
            logger.debug("headless_claude trace emit failed: %s", exc)


# ----------------------------------------------------------------------------
# Stream-json parsing
# ----------------------------------------------------------------------------


def _read_log_events(
    log_path: Path,
    *,
    attempt_label: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read the stream-json log and return one dict per parseable event.

    Scopes to ``===== ATTEMPT <label> START/END =====`` sentinels when
    ``attempt_label`` is set; useful when one log file accumulates
    multiple attempts.
    """
    start_marker = (
        f"===== ATTEMPT {attempt_label} START ====="
        if attempt_label is not None else None
    )
    end_marker = (
        f"===== ATTEMPT {attempt_label} END "
        if attempt_label is not None else None
    )
    events: list[dict[str, Any]] = []
    in_scope = start_marker is None  # no scoping requested -> include all
    try:
        with open(log_path, "r") as f:
            for line in f:
                if start_marker is not None and line.startswith(start_marker):
                    in_scope = True
                    continue
                if end_marker is not None and line.startswith(end_marker):
                    in_scope = False
                    continue
                if not in_scope:
                    continue
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def parse_stream_json_log(
    log_path: str | Path,
    *,
    attempt_label: Optional[str] = None,
) -> tuple[Trajectory, str]:
    """Public helper: parse a stream-json log into a Trajectory + assistant text.

    Useful when you have an existing log on disk (e.g. an archived
    Architect run) and want the reef trajectory shape for analysis.
    """
    events = _read_log_events(Path(log_path), attempt_label=attempt_label)
    return build_trajectory(events)


def build_trajectory(
    events: Iterable[Mapping[str, Any]],
) -> tuple[Trajectory, str]:
    """Walk parsed stream-json events into a :class:`Trajectory` + assistant
    text concat.

    Returns the trajectory and the concatenated assistant text so callers
    that just want the tail don't have to walk the steps again.
    """
    traj = Trajectory()
    assistant_text_parts: list[str] = []
    last_assistant_msg: Optional[dict[str, Any]] = None
    pending_tool_steps: dict[str, int] = {}  # tool_use_id -> index in traj.steps

    for ev in events:
        ev_type = ev.get("type")

        if ev_type == "assistant":
            msg = ev.get("message") or {}
            content_blocks = msg.get("content") or []
            text_chunks: list[str] = []
            tool_uses: list[dict[str, Any]] = []
            for block in content_blocks:
                btype = block.get("type") if isinstance(block, Mapping) else None
                if btype == "text":
                    text = block.get("text") or ""
                    if text:
                        text_chunks.append(text)
                elif btype == "tool_use":
                    tool_uses.append(block)

            combined_text = "".join(text_chunks)
            if combined_text:
                assistant_text_parts.append(combined_text)

            # One Step per assistant turn (the LLM's prose / tool-dispatch
            # decision). Mirrors reef.react's per-round Step shape.
            traj.steps.append(Step(
                kind="llm",
                name="claude.message",
                started_at_ms=0,
                elapsed_ms=0,
                arguments={"tool_call_count": len(tool_uses)},
                result_preview=combined_text[:400],
            ))

            # One Step per tool_use; result_preview gets filled when we
            # see the matching tool_result in a subsequent user event.
            for tu in tool_uses:
                tu_id = tu.get("id") or ""
                tu_name = tu.get("name") or "<unknown>"
                tu_input = tu.get("input") or {}
                traj.steps.append(Step(
                    kind="tool",
                    name=tu_name,
                    started_at_ms=0,
                    elapsed_ms=0,
                    arguments=tu_input if isinstance(tu_input, Mapping) else {},
                    result_preview="",
                ))
                if tu_id:
                    pending_tool_steps[tu_id] = len(traj.steps) - 1
                traj.token_usage["tool_calls"] = (
                    traj.token_usage.get("tool_calls", 0) + 1
                )

            last_assistant_msg = msg
            traj.rounds += 1
            # accumulate usage if the assistant event carries it
            usage = msg.get("usage")
            if isinstance(usage, Mapping):
                accumulate_usage(traj.token_usage, usage)

        elif ev_type == "user":
            # tool_result blocks carry the dispatched tool's output;
            # back-fill the matching pending tool Step.
            msg = ev.get("message") or {}
            content_blocks = msg.get("content") or []
            for block in content_blocks:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") != "tool_result":
                    continue
                tu_id = block.get("tool_use_id") or ""
                idx = pending_tool_steps.pop(tu_id, None)
                if idx is None:
                    continue
                payload = block.get("content")
                payload_text = _tool_result_to_text(payload)
                is_err = bool(block.get("is_error"))
                # Step is frozen; replace in place with an updated copy.
                old = traj.steps[idx]
                traj.steps[idx] = Step(
                    kind=old.kind,
                    name=old.name,
                    started_at_ms=old.started_at_ms,
                    elapsed_ms=old.elapsed_ms,
                    arguments=old.arguments,
                    result_preview=payload_text[:400],
                    has_error=is_err,
                    error_message=payload_text[:500] if is_err else None,
                )

        elif ev_type == "result":
            # Terminal event: carries the total usage and the final
            # assistant text. claude emits this once at the end of a
            # session.
            usage = ev.get("usage")
            if isinstance(usage, Mapping):
                accumulate_usage(traj.token_usage, usage)
            result_text = ev.get("result")
            if isinstance(result_text, str) and result_text:
                # The result text is the final assistant response;
                # use it as final_message so reef-shaped consumers
                # see the canonical exit message.
                traj.final_message = {
                    "role": "assistant",
                    "content": result_text,
                }

        elif ev_type == "system":
            # init / status events -- ignored for trajectory purposes
            continue

    if traj.final_message is None and last_assistant_msg is not None:
        # No terminal `result` event (subprocess was killed mid-stream).
        # Fall back to the last assistant message we saw so callers can
        # still inspect what the session was working on.
        text_blocks = [
            b.get("text", "") for b in (last_assistant_msg.get("content") or [])
            if isinstance(b, Mapping) and b.get("type") == "text"
        ]
        if text_blocks:
            traj.final_message = {
                "role": "assistant",
                "content": "".join(text_blocks),
            }

    return traj, "".join(assistant_text_parts)


def _tool_result_to_text(payload: Any) -> str:
    """Best-effort flatten of a tool_result content into a string."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts: list[str] = []
        for block in payload:
            if isinstance(block, Mapping):
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                else:
                    try:
                        parts.append(json.dumps(block, default=str))
                    except (TypeError, ValueError):
                        parts.append(repr(block))
            else:
                parts.append(str(block))
        return "".join(parts)
    try:
        return json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return repr(payload)


__all__ = [
    "ClaudeResult",
    "HeadlessClaude",
    "TIMEOUT_EXIT_CODE",
    "build_trajectory",
    "parse_stream_json_log",
]
