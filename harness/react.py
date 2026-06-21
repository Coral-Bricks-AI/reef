# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.react`` -- framework-free ReAct loop over a direct LLM client.

A small, transparent, no-deps ReAct loop that talks the OpenAI
tool-calling shape :func:`harness.llm.chat` already speaks. The whole
run is one function (:func:`run_react`) and one transcript shape
(:class:`Trajectory`); there is no inheritance hierarchy and no
graph compilation step. The model authors a tool call, we dispatch
via :attr:`harness.tool.Tool.fn`, append the result back to the
messages, repeat until the model emits an assistant message without
``tool_calls``.

The rationale for not depending on LangChain / LangGraph: an agent
loop is forty lines of control flow plus retry, watchdog, and
trajectory recording. Coupling to a framework version locks the
ground-truth eval set to that version's tool-calling adapter and
wraps the direct provider client in yet another ChatModel
indirection. Owning the loop ourselves keeps the wire shape
inspectable and the model upgrade path one provider import away.

Why this shape
--------------

1. **Wire-shape match.** ``llm.chat`` returns the raw provider
   response normalized to ``{"model", "response": {"choices": [...
   {"message": {...}}], "usage": {...}}}``. The OpenAI tool-calling
   contract is the only shape the proxy needs to understand and is
   the lowest common denominator across DeepInfra, Cerebras, OpenAI,
   Bedrock, and self-hosted SGLang. We talk it directly so a model
   swap is a one-line change.

2. **Trajectory is structured data, not log lines.** Every step of
   every specialist is a :class:`Step` with the inputs and outputs
   the runtime saw. The swarm aggregates these into the run result
   so the Console / IA UI can render a real timeline. The legacy
   trajectory store reached into a process-global dict; this one is
   per-call and the swarm hands the trajectory back as part of the
   pipeline result dict.

3. **Cancellation = gateway terminate.** We do not poll a Python
   ``cancel_event`` between LLM calls anymore. The platform's
   contract for stopping a run is ``POST /runs/{id}/terminate``,
   which kills the sandbox subprocess; that's the hard guarantee.
   Cooperative in-process cancel only saved at most one in-flight
   round-trip and added kwargs / branches to every layer here.

4. **Transient retries are at the chat layer, not the agent.**
   :func:`chat_with_retry` wraps ``llm.chat`` with bounded retry on
   the two transient classes we see in production: provider 429 /
   queue-full responses, and DeepInfra CUDA kernel hiccups. The
   ReAct loop and the synthesizer both call through this helper, so
   the policy lives in one place.

5. **Tool errors are first-class messages, not exceptions.** When
   a tool dispatch raises, we serialize the exception and feed it
   back as a ``role: tool`` message so the model can self-correct.
   This is what created reasonable recovery behaviour in the legacy
   code (where every ``@tool`` wrapper had its own try/except);
   we centralise it here so personas don't have to.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from harness import llm as cb_llm
from harness import _langfuse
from harness.context import current_constraints, current_enforcer
from harness.enforcement import ConstraintViolation
from harness.tool import Tool, lookup_tool

logger = logging.getLogger(__name__)


# Cap on the JSON-serialized length of a single tool result fed back
# into the conversation. The IA tools already clip their internal
# strings, but a degenerate ``query_graph`` row dump can still cross
# this; we clip again here so a runaway tool can't blow the model's
# context window in one shot.
_TOOL_RESULT_MAX_CHARS = 32_000


# Default per-call wall budget for the LLM. The provider stack has
# its own timeouts, but we set ours below the gateway's hard cap so
# a slow model surfaces here as a clean :class:`RuntimeError` rather
# than the gateway tearing the whole UDS down.
_LLM_CALL_TIMEOUT_S = 180.0

# Hard cap on consecutive must-retrieve coercion turns -- see the
# ``min_tool_calls_before_final`` parameter on :func:`run_react`. We
# don't want a model that simply refuses to retrieve to burn the
# entire ``max_steps`` budget on coercion no-ops; once we've nudged
# this many times the next no-tool message is accepted as final
# and the swarm-level enforcement (``swarm._enforce_vc_quant_extract``
# and friends) decides whether to discard the resulting payload.
_MAX_MUST_RETRIEVE_COERCIONS = 2

# The coercion message itself. Phrased as a system reminder rather
# than a user follow-up because providers (esp. Cerebras+Qwen) treat
# system messages as higher-priority than tool-result messages, and
# we want this nudge to override the model's prior decision to
# emit the structured envelope.
_MUST_RETRIEVE_COERCION = (
    "STOP. You attempted to emit a final answer without making any "
    "tool calls. This persona's contract requires at least "
    "{min_n} tool retrieval(s) before any final answer (see your "
    "system prompt's must-retrieve rule). The information needed "
    "to answer this question is not in your training data and "
    "must be retrieved fresh. Issue a `bm25_scraped_articles` or "
    "`vector_scraped_articles` call NOW with a query targeting the "
    "exact figure / KPI / company / period the GP asked about. Do "
    "NOT emit any final JSON envelope until you have at least one "
    "tool result back. If your training tells you the answer, "
    "ignore it -- training data goes stale, and the GP routed this "
    "to you specifically because the figure may have moved or never "
    "lived in the SEC body. Retrieve, then answer."
)


# Soft warning fired one round BEFORE the hard "tools removed" cliff.
# Without this nudge the model goes from "full tool freedom" to
# "no tools + synthesize NOW" in a single turn, and Cerebras+Qwen
# tends to emit one more dispatch on the cliff turn anyway (often in
# the XML envelope flavour, see _parse_xml_tool_call_body) which the
# last-round shim then drops. Telling the model on the penultimate
# round that it is the LAST opportunity to dispatch lets a
# well-behaved model voluntarily skip its final retrieval and start
# packaging findings; for the ones that don't, we still have the
# hard last-round contract as a backstop. Observed in practice on
# Vals AI row 7 sector_analyst (request df248c96): 7 native tool
# dispatches across rounds 1-7, then a stranded XML dispatch on
# round 8 that produced an empty payload. Only fires when
# ``max_steps >= 3`` so single- or double-step specialists (none
# in the current roster, but the runtime is callable from tests with
# small budgets) don't get a warning that overlaps the last-round
# message.
_BUDGET_PENULTIMATE = (
    "Budget check: this is your LAST tool-call round. After this "
    "turn tools are removed and the runtime accepts ONLY a JSON "
    "answer envelope -- planning prose ('let me check X', 'I need "
    "to verify Y') will be rejected as malformed. Whatever you "
    "fetch now is the last data you will see. If retrieval is "
    "incomplete, package what you have with an explicit caveat in "
    "answer_summary; do NOT save synthesis for the final turn."
)


# Substrings we treat as "the provider is overloaded, please wait".
# Lowercased before compare.
_TRANSIENT_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "429",
    "rate limit",
    "rate_limit",
    "model busy",
    "queue_exceeded",
    "try again soon",
    "too_many_requests",
    "retry later",
)


# Substrings we treat as "DeepInfra CUDA hiccup, retry once". Case
# preserved -- these come back in the provider error payload as-is.
_TRANSIENT_GPU_MARKERS: tuple[str, ...] = (
    "RMSNorm",
    "cudaSuccess",
    "cuda error",
    "CUDA error",
)


# Substrings we treat as "the provider's tail latency just chewed the
# call". Lowercased. Triggered by either our soft RPC timeout firing or
# an upstream transport-level deadline. See the LLM-call watchdog
# constants below for the soft/hard thresholds.
_TRANSIENT_TIMEOUT_MARKERS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "deadline exceeded",
    "read timeout",
    "request timed out",
)


# LLM-call watchdog policy. Two production incidents (run d05abe72 and
# run 7ad86e1c) showed Cerebras tail-latency spiking to ~60s on
# otherwise-trivial calls (47 output tokens, no generation issue --
# pure provider-side queueing / cold-start). Each stall blocked the
# critical-path specialist for the full duration. The watchdog:
#   - logs a WARN when any successful call exceeds SOFT_S so the spikes
#     are visible without aborting in-flight work;
#   - clamps the per-call timeout to HARD_S so a single stalled call
#     can't dominate the run wallclock;
#   - on a timeout-classified transient, the retry below swaps to the
#     model in _LLM_FALLBACK_MODEL_MAP (when one is registered) so we
#     escape the failing provider instead of re-stalling against it.
_LLM_WATCHDOG_SOFT_S: float = 10.0
_LLM_WATCHDOG_HARD_S: float = 25.0

# Per-model watchdog override. 25s is the right cap for fast models
# (gpt-oss-120b, GLM-4.7), but the Qwen-235B/397B family genuinely
# needs more — measured across three providers (Cerebras qwen-3-235b,
# Together Qwen3-235B FP8, DeepInfra Qwen 3.5 397B-A17B) on the
# 7-query preset sweep: EVERY call timed out at exactly 25024 ms and
# zero substantive answers came back. The model isn't broken; it's
# structurally slow for cb-ia's tool-result-heavy turns, regardless
# of provider. 60s lets it complete reliably. Keys are matched as
# lower-cased prefixes.
_LLM_WATCHDOG_HARD_S_OVERRIDES: tuple[tuple[str, float], ...] = (
    ("qwen/", 60.0),
    ("together/qwen/", 60.0),
    ("cerebras/qwen-", 60.0),
    # Lilac-proxied Kimi K2.6 is the official Kimi route. Kimi is a
    # reasoning model that emits ~25k chars of internal reasoning on
    # real eval queries (measured on a CRWD/PANW CAGR prompt with
    # max_tokens=16k: completion_tokens=8143, wall=66.7s). DeepInfra
    # direct ``moonshotai/`` was retired because the same query took
    # 2:09-2:48 -- busts any reasonable watchdog. 120s fits Lilac's
    # tail comfortably and leaves headroom for multi-tool synthesizer
    # turns. Covers any future Lilac-proxied reasoning model under
    # the same prefix.
    ("lilac/", 120.0),
    # OpenRouter-proxied Kimi K2.6 fallback. Same Moonshot Kimi
    # reasoning model as the Lilac route, similar per-call latency
    # tail (60-90s observed on equivalent prompts) plus a small extra
    # margin for the cross-proxy routing overhead. Same 120s budget
    # as Lilac; covers any other OpenRouter-routed reasoning model
    # we wire in later under the same prefix.
    ("openrouter/", 120.0),
    # Self-hosted gpt-oss-* on AWS via the ``aws/`` prefix (vllm/sglang
    # on a p5.48xlarge today). gpt-oss is a reasoning model: it emits a
    # CoT block before the visible content, and our cb-ia turns are
    # tool-result-heavy (tens of KB of context per turn). 25s is too
    # tight; 90s gives the model room without the 120s Kimi-class tail
    # since locally-hosted means no proxy overhead.
    ("aws/", 90.0),
)


# Per-model floor on ``max_completion_tokens``. Some reasoning models
# (Kimi K2.6 via Lilac) emit thousands of tokens of internal reasoning
# BEFORE the visible content, and the model honors ``finish=length``
# silently -- the API call "succeeds" with finish_reason=length and an
# empty (or truncated) content string. cb-ia's default caps (specialist
# 4096-8192, synthesizer/postprocessor 6144) are sized for non-reasoning
# models and let Kimi eat the entire budget on reasoning alone, leaving
# no room for the JSON answer the planner expects -- the postprocessor
# then sees empty content and json_parse_failure fires.
#
# Empirical floor: a real cb-ia synthesis turn needs ~6-10k tokens of
# Kimi reasoning + 2-4k tokens of JSON content. 16000 covers both with
# margin. The cap is a CEILING -- only billed for what the model
# actually emits -- so raising it is safe for non-reasoning callers
# that happen to land on this prefix later.
_LLM_MAX_COMPLETION_TOKENS_FLOOR_OVERRIDES: tuple[tuple[str, int], ...] = (
    ("lilac/", 16000),
    # OpenRouter-proxied Kimi K2.6 emits the same reasoning-tokens
    # pattern as the Lilac route -- same floor needed so the
    # postprocessor doesn't see empty content on a finish=length
    # truncation.
    ("openrouter/", 16000),
    # Self-hosted gpt-oss-* on AWS (``aws/`` prefix). gpt-oss emits a
    # reasoning CoT before the visible content; same finish=length
    # truncation failure mode as Kimi if the budget is sized for non-
    # reasoning models. Same 16k floor so the postprocessor has room.
    ("aws/", 16000),
)


def _watchdog_hard_s_for(model: str) -> float:
    """Return the per-model hard timeout (defaults to _LLM_WATCHDOG_HARD_S)."""
    if not isinstance(model, str):
        return _LLM_WATCHDOG_HARD_S
    lc = model.lower()
    for prefix, hard_s in _LLM_WATCHDOG_HARD_S_OVERRIDES:
        if lc.startswith(prefix):
            return hard_s
    return _LLM_WATCHDOG_HARD_S


def _max_completion_tokens_floor_for(model: str) -> Optional[int]:
    """Return the per-model floor for ``max_completion_tokens`` (or None)."""
    if not isinstance(model, str):
        return None
    lc = model.lower()
    for prefix, floor in _LLM_MAX_COMPLETION_TOKENS_FLOOR_OVERRIDES:
        if lc.startswith(prefix):
            return floor
    return None


# Max attempts for the timeout transient class specifically. Lower
# than `max_attempts` because the empirical signal is: when both the
# primary AND the registered fallback time out at HARD_S, retrying the
# fallback a third time almost never succeeds — it's the same model on
# the same input that just failed, often because the trajectory is
# large enough that 25s genuinely isn't enough for input ingestion.
# Better to bail at 2 attempts (50s ceiling) and let the specialist
# return success=False than burn another 25s on a guaranteed failure.
# Rate-limit / GPU transients keep the full `max_attempts` budget;
# those failure modes are more likely to clear on retry.
_LLM_MAX_TIMEOUT_ATTEMPTS: int = 2

# Map: primary model -> fallback model used when the primary times out
# during a retry. Keys are matched by lower-cased prefix. Values must
# be in the pipeline manifest's `models` allowlist. Order matters: the
# first matching prefix wins, so more-specific prefixes go FIRST.
#
# Why gpt-oss-120b is the universal fallback. Measured across the
# 7-query preset sweep (2026-05-22→25): gpt-oss-120b on Cerebras was
# the only model in the allowlist that produced substantive answers on
# all 7 queries with zero watchdog timeouts (avg 170s). Every Qwen
# variant we tested — Cerebras qwen-3-235b, Together Qwen3-235B FP8,
# DeepInfra Qwen 3.5 397B-A17B — failed all 7 queries with 100%
# timeout rate. So when ANY primary times out, we swap to the one
# reliable model we have rather than to another slow Qwen.
_LLM_FALLBACK_MODEL_MAP: tuple[tuple[str, str], ...] = (
    # DeepInfra-served Qwen (matches "Qwen/..." literal model ids).
    ("qwen/", "cerebras/gpt-oss-120b"),
    # Together-served Qwen (any together/qwen/* slug).
    ("together/qwen/", "cerebras/gpt-oss-120b"),
    # Cerebras Qwen specifically — falls to the same gpt-oss-120b
    # (also on Cerebras, different model fleet so a Qwen-fleet
    # incident doesn't take this down too).
    ("cerebras/qwen-", "cerebras/gpt-oss-120b"),
    # Lilac-proxied Kimi K2.6 (lilac/moonshotai/kimi-k2.6) falls to
    # OpenRouter-proxied Kimi K2.6. Same Moonshot Kimi weights via a
    # different proxy network -- a Lilac incident shouldn't drop the
    # swarm to a different model family. OpenRouter's Kimi route has
    # measured per-call latency comparable to Lilac's (60-90s on real
    # eval prompts) so the existing 120s watchdog still covers the
    # fallback attempt. If OpenRouter also times out (or fails the
    # 2-attempt timeout budget), the openrouter/moonshotai/ entry
    # below cascades to Cerebras gpt-oss-120b as a final last-resort
    # recovery path. DeepInfra-direct Kimi was retired -- 2:09-2:48
    # per call busts the 120s watchdog.
    ("lilac/moonshotai/", "openrouter/moonshotai/kimi-k2.6"),
    # OpenRouter-proxied Kimi K2.6 last-resort fallback. Cascaded to
    # Cerebras gpt-oss-120b for the rare case where both Lilac AND
    # OpenRouter are unhealthy simultaneously (different model family
    # but reliably substantive output -- the only model in the
    # allowlist with zero watchdog timeouts on the 2026-05 sweep).
    ("openrouter/moonshotai/", "cerebras/gpt-oss-120b"),
    # Cerebras gpt-oss-120b -> Lilac Kimi K2.6. gpt-oss-120b shows up
    # as a one-model-per-run choice via the ``cerebras-gpt-oss`` profile,
    # so it needs a *non*-Cerebras fallback for the rare case where
    # the Cerebras fleet itself has an incident. Lilac kimi is slow
    # for the postprocessor's 50k-token thread (median ~3min) but
    # has been the de-facto recovery path before this change and is
    # known to produce substantive (if late) synthesis output. The
    # Qwen catch-all below would otherwise inherit -- which has 100%
    # timeout rate per the 2026-05 sweep, i.e. no recovery at all.
    ("cerebras/gpt-oss-120b", "lilac/moonshotai/kimi-k2.6"),
    # Other Cerebras models (zai-glm, deepseek if added later) keep
    # the legacy DeepInfra-Qwen fallback. Last-resort catch-all so a
    # Cerebras-side incident still has SOME fallback path; not
    # validated against gpt-oss as the user can change the default
    # model without recompiling.
    ("cerebras/", "Qwen/Qwen3.5-397B-A17B"),
)


def _fallback_model_for(model: str) -> Optional[str]:
    """Return the registered fallback for ``model`` (None if unmapped)."""
    if not isinstance(model, str):
        return None
    lc = model.lower()
    for prefix, fallback in _LLM_FALLBACK_MODEL_MAP:
        if lc.startswith(prefix):
            return fallback
    return None


def _classify_transient(exc: BaseException) -> Optional[str]:
    """Return ``"rate_limit"`` / ``"gpu"`` / ``"timeout"`` / ``None``."""
    text_lower = str(exc).lower()
    for marker in _TRANSIENT_RATE_LIMIT_MARKERS:
        if marker in text_lower:
            return "rate_limit"
    text = str(exc)
    for marker in _TRANSIENT_GPU_MARKERS:
        if marker in text:
            return "gpu"
    for marker in _TRANSIENT_TIMEOUT_MARKERS:
        if marker in text_lower:
            return "timeout"
    return None


def chat_with_retry(
    *,
    max_attempts: int = 3,
    **chat_kwargs: Any,
) -> dict[str, Any]:
    """Call :func:`coralbricks.sandbox.llm.chat` with bounded retry.

    Retries only the two transient classes the production swarm
    actually sees -- provider 429 / queue-full and DeepInfra CUDA
    kernel hiccups. Anything else (auth, malformed request, model
    not found, real server errors) propagates immediately so the
    caller can surface it cleanly. Exhausting ``max_attempts`` re-
    raises the last exception.

    The backoff is intentionally simple: 5s then 10s for rate
    limits, 2s for GPU errors. We don't jitter because the retry
    budget is small and the gateway is already serializing the
    sandbox -> provider hop.
    """
    last_exc: Optional[BaseException] = None
    trace = _langfuse.get_active()

    # LLM-call watchdog: clamp per-call timeout to the per-model HARD_S
    # unless the caller explicitly asked for something tighter (we never
    # relax it past the watchdog limit -- the point is to keep stalls
    # bounded). Qwen-family models get a 60s ceiling via
    # _LLM_WATCHDOG_HARD_S_OVERRIDES; everyone else stays at 25s.
    primary_model = chat_kwargs.get("model", "")
    hard_s = _watchdog_hard_s_for(primary_model)
    caller_timeout = chat_kwargs.get("timeout_s")
    if caller_timeout is None or caller_timeout > hard_s:
        chat_kwargs["timeout_s"] = hard_s

    # max_completion_tokens floor for reasoning-model prefixes
    # (Kimi K2.6 via Lilac). The model emits ~6-10k tokens of internal
    # reasoning BEFORE the visible content; if max_completion_tokens
    # is below the floor, the model hits finish=length silently with
    # empty content, the JSON parser fails, and the planner falls back
    # to a deterministic concat with no key_events/entities/metrics.
    # We only raise, never lower -- if the caller asked for MORE
    # budget than the floor, respect it.
    # Only RAISE the budget keys the caller actually passed. Adding the
    # other key when it was unset (the prior behavior) silently injects
    # ``max_tokens`` alongside ``max_completion_tokens`` (or vice versa),
    # and the Cerebras fallback rejects requests that set both with::
    #
    #   400 Setting "max_tokens" and "max_completion_tokens" at the
    #   same time is not supported.
    #
    # That fails every postprocessor call whenever the kimi watchdog
    # swaps the model to cerebras mid-loop, burning the full retry
    # budget on guaranteed 400s.
    tok_floor = _max_completion_tokens_floor_for(primary_model)
    if tok_floor is not None:
        for key in ("max_completion_tokens", "max_tokens"):
            cur = chat_kwargs.get(key)
            if isinstance(cur, int) and cur < tok_floor:
                chat_kwargs[key] = tok_floor

    # Track timeout transients separately from the general attempt
    # counter. We cap at _LLM_MAX_TIMEOUT_ATTEMPTS because after a
    # primary+fallback both timing out at 25s each, retrying again is
    # almost certainly going to time out a third time -- saves the
    # final 25s of wasted budget.
    timeout_attempts_used = 0

    for attempt in range(max_attempts):
        t0 = time.perf_counter()
        try:
            env = cb_llm.chat(**chat_kwargs)
        except Exception as exc:  # noqa: BLE001 -- provider raises a zoo
            last_exc = exc
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            # Emit a failed generation observation even on the attempt
            # that will be retried; each attempt is a real provider
            # round-trip and the Langfuse UI should show them all so
            # rate-limit / GPU-hiccup patterns are visible.
            if trace is not None:
                trace.record_chat(
                    model=chat_kwargs.get("model", "<unknown>"),
                    messages=chat_kwargs.get("messages"),
                    response=None,
                    latency_ms=elapsed_ms,
                    error=exc,
                )
            kind = _classify_transient(exc)
            if kind is None or attempt == max_attempts - 1:
                raise
            # Timeout-specific budget cap: bail after 2 timeouts even
            # if max_attempts has more room left. Empirically the 3rd
            # attempt also times out (Production run d4864e83:
            # cerebras 25s → DeepInfra 25s → DeepInfra-retry 25s, all
            # fail). Better to let the specialist surface
            # success=False at 50s than burn another 25s.
            if kind == "timeout":
                timeout_attempts_used += 1
                if timeout_attempts_used >= _LLM_MAX_TIMEOUT_ATTEMPTS:
                    logger.warning(
                        "llm.chat watchdog: %d consecutive timeouts on "
                        "this call (attempt %d/%d, last model=%s) — "
                        "bailing instead of further retry; caller will "
                        "see RpcConnectError. Saves the final 25s of "
                        "wasted budget on a pattern that empirically "
                        "doesn't recover.",
                        timeout_attempts_used, attempt + 1, max_attempts,
                        chat_kwargs.get("model", "<unknown>"),
                    )
                    raise
            # Backoff: 0s for timeout (no point waiting if the provider
            # is hot-spotting); 2s for GPU; 5/10s ramp for rate-limit.
            if kind == "timeout":
                wait_s = 0
            elif kind == "rate_limit":
                wait_s = (attempt + 1) * 5
            else:
                wait_s = 2
            # On a timeout, swap to the registered fallback model (if
            # any) for the next attempt — same primary is likely to
            # stall again if the provider is having an incident. The
            # swap is one-shot per call site; we don't swap a second
            # time.
            primary_model = chat_kwargs.get("model", "")
            if kind == "timeout":
                fallback = _fallback_model_for(primary_model)
                if fallback and fallback != primary_model:
                    logger.warning(
                        "llm.chat watchdog timeout on %s after %d ms "
                        "(attempt %d/%d) — swapping to fallback %s",
                        primary_model, elapsed_ms,
                        attempt + 1, max_attempts, fallback,
                    )
                    chat_kwargs["model"] = fallback
                else:
                    logger.warning(
                        "llm.chat watchdog timeout on %s after %d ms "
                        "(attempt %d/%d) — retrying same model (no "
                        "fallback registered)",
                        primary_model, elapsed_ms,
                        attempt + 1, max_attempts,
                    )
            else:
                logger.warning(
                    "llm.chat transient %s error (attempt %d/%d), "
                    "waiting %ds: %s",
                    kind, attempt + 1, max_attempts, wait_s, exc,
                )
            if wait_s > 0:
                time.sleep(wait_s)
            continue
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        # Soft-warning observability: any successful call above SOFT_S
        # is logged so tail-latency spikes are visible even when they
        # didn't trip the hard timeout. Use the model the call ACTUALLY
        # ran against (may differ from the original primary if a prior
        # attempt swapped to the fallback).
        if elapsed_ms >= int(_LLM_WATCHDOG_SOFT_S * 1000):
            actual_model = chat_kwargs.get("model", "<unknown>")
            logger.warning(
                "llm.chat slow call: model=%s latency_ms=%d (soft "
                "threshold=%.1fs, hard=%.1fs); call succeeded",
                actual_model,
                elapsed_ms,
                _LLM_WATCHDOG_SOFT_S,
                _watchdog_hard_s_for(actual_model),
            )
        if trace is not None:
            trace.record_chat(
                model=chat_kwargs.get("model", "<unknown>"),
                messages=chat_kwargs.get("messages"),
                response=env.get("response") if isinstance(env, Mapping) else None,
                latency_ms=elapsed_ms,
            )
        return env
    # Unreachable: the loop either returns or raises above.
    raise last_exc  # type: ignore[misc]  # pragma: no cover


@dataclass(frozen=True)
class Step:
    """One unit of progress inside a :func:`run_react` call.

    A step is either an LLM call (``kind="llm"``) or a tool
    dispatch (``kind="tool"``). The trajectory is the chronological
    sequence of these steps; the synthesizer + the UI both walk it
    to render what the agent did.
    """

    kind: str
    name: str
    started_at_ms: int
    elapsed_ms: int
    arguments: Mapping[str, Any] = field(default_factory=dict)
    result_preview: str = ""
    has_error: bool = False
    error_message: Optional[str] = None


@dataclass
class Trajectory:
    """Mutable per-run trajectory; passed by reference to keep the
    runtime side-effect-free outside this object.

    ``final_message`` is set when the loop terminates with an
    assistant message that has no ``tool_calls``; ``token_usage`` is
    accumulated across every LLM call. Field names
    (``input_tokens`` / ``output_tokens`` / ``cached_tokens`` /
    ``tool_calls``) mirror the OpenAI usage shape so downstream
    accounting consumers don't have to special-case this loop.
    """

    steps: list[Step] = field(default_factory=list)
    final_message: Optional[dict[str, Any]] = None
    token_usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "tool_calls": 0,
        }
    )
    rounds: int = 0
    error: Optional[str] = None
    # Full tool payloads captured for tools in ``run_react``'s
    # ``capture_tools`` whitelist. Step.result_preview is clipped to
    # 300 chars, too narrow to reconstruct OHLC bars / macro series
    # / other structured output that downstream assembly (e.g. the
    # swarm's post-synth equity_chart shaper) needs. Keyed by tool
    # name, value is the list of full result payloads in the order
    # the tool was dispatched within the specialist.
    captured_tool_results: dict[str, list[Any]] = field(default_factory=dict)
    # Verbatim chat-completion messages list at loop exit (system +
    # user + interleaved assistant / tool turns). Exposed for the
    # swarm-side JSON-envelope coercion path: when the last-round
    # assistant content fails to parse as the structured envelope,
    # the swarm replays this list with one trailing system directive
    # to coerce a JSON-only re-emit (see swarm._coerce_json_envelope).
    # Stored by reference -- the runtime does not mutate it after
    # returning, so callers see the final state.
    final_messages: Optional[list[dict[str, Any]]] = None


def _truncate(s: str, n: int = _TOOL_RESULT_MAX_CHARS) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n... [truncated at {n} chars]"


def _serialize_tool_result(payload: Any) -> str:
    """Render a tool's return value into a string the model can read.

    String payloads (e.g. ``load_skills`` rendering the ``=== LOADED
    SKILLS ===`` block) are passed through verbatim — json.dumps on a
    string would wrap it in quotes and escape every newline, so the
    model sees one wall of ``\\n`` literals instead of paragraphs.
    Non-string payloads are JSON-serialized so the model can match on
    keys in subsequent reasoning; ``repr`` is the last-resort fallback
    for non-serializable objects (which the IA tools never produce).
    Always clipped to :data:`_TOOL_RESULT_MAX_CHARS`.
    """
    if isinstance(payload, str):
        return _truncate(payload)
    try:
        text = json.dumps(payload, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(payload)
    return _truncate(text)


# Keys the IA kernel verbs use to wrap result lists. When a tool
# returns a payload whose list-under-these-keys is empty, we treat
# it as an empty result for WARNING-level logging so "I got nothing
# back" is visible in the run log without having to open the
# trajectory JSON.
_EMPTY_RESULT_LIST_KEYS: tuple[str, ...] = (
    "results", "rows", "items", "hits", "documents",
    "records", "data", "matches", "events", "entities",
)


def _result_looks_empty(payload: Any) -> bool:
    """Heuristic: does this tool payload carry no actual data?

    Catches the common shapes the IA tools emit on a miss (empty
    list, empty dict, ``{"results": []}``). Not exhaustive -- a
    bespoke tool with a novel shape can still fall through, in which
    case the log just won't fire and behaviour is unchanged.
    """
    if payload is None:
        return True
    if isinstance(payload, (str, bytes)) and not payload:
        return True
    if isinstance(payload, (list, tuple, set)) and not payload:
        return True
    if isinstance(payload, Mapping):
        if not payload:
            return True
        for key in _EMPTY_RESULT_LIST_KEYS:
            val = payload.get(key)
            if isinstance(val, (list, tuple)) and not val:
                return True
    return False


def _args_preview(args: Mapping[str, Any]) -> str:
    """Compact single-line args rendering for log lines."""
    try:
        return json.dumps(args, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(args)


def accumulate_usage(
    target: dict[str, int],
    usage: Optional[Mapping[str, Any]],
) -> None:
    """Sum one provider ``usage`` block into running totals.

    Translates the OpenAI shape that ``llm.chat`` returns
    (``prompt_tokens`` / ``completion_tokens`` / nested
    ``prompt_tokens_details.cached_tokens``) into the prod-IA naming
    (``input_tokens`` / ``output_tokens`` / ``cached_tokens``) so the
    trajectory speaks one vocabulary regardless of provider.

    Used by the ReAct loop, the synthesizer, and the planner/post-
    processor -- canonicalized here so callers don't each carry a copy.
    """
    if not usage:
        return
    inp = usage.get("input_tokens")
    if inp is None:
        inp = usage.get("prompt_tokens")
    if isinstance(inp, int):
        target["input_tokens"] = target.get("input_tokens", 0) + inp

    out = usage.get("output_tokens")
    if out is None:
        out = usage.get("completion_tokens")
    if isinstance(out, int):
        target["output_tokens"] = target.get("output_tokens", 0) + out

    cached = usage.get("cached_tokens")
    if cached is None:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, Mapping):
            cached = details.get("cached_tokens")
    if isinstance(cached, int):
        target["cached_tokens"] = target.get("cached_tokens", 0) + cached


def run_react(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    tools: Sequence[Tool],
    max_steps: int = 6,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    extra_chat_kwargs: Optional[Mapping[str, Any]] = None,
    log_label: str = "react",
    capture_tools: Sequence[str] = (),
    min_tool_calls_before_final: int = 0,
) -> Trajectory:
    """Drive one specialist (or any tool-calling agent) through a ReAct loop.

    The loop terminates when:

    - the model returns an assistant message with no ``tool_calls``
      (the natural exit path -- this is the answer);
    - we hit ``max_steps`` (the model gets one final no-tool prompt
      to force a non-tool answer);
    - any LLM call raises after exhausting the transient-retry
      budget (returned trajectory has ``error`` set).

    A "step" in ``max_steps`` counts each LLM call. Tool dispatches
    are not counted -- the budget is on model thinking, not on
    function calls (one model turn can call multiple tools in
    parallel via the ``tool_calls`` array).

    ``min_tool_calls_before_final`` is the must-retrieve gate. When
    set to ``N >= 1`` the loop refuses to accept the model's first
    no-tool-call message as the final answer if fewer than ``N``
    tool dispatches have happened so far. Instead a coercion ``user``
    message is appended ("you produced no tool calls; issue a
    retrieval now before answering") and the loop continues. We cap
    coercions at :data:`_MAX_MUST_RETRIEVE_COERCIONS` so a model
    that simply refuses to retrieve doesn't burn the entire step
    budget on coercion turns -- once the cap is hit the next no-tool
    message is accepted as final (with the trajectory's
    ``token_usage["tool_calls"]`` left at whatever the model actually
    produced, so the swarm-level enforcement can still discard the
    payload as ungrounded).

    Personas like ``news_quant_analyst`` whose entire job is
    figure-extraction-from-news set this to 1 so the model cannot
    short-circuit a quant-extract dispatch with a confident
    LLM-memory answer (the documented Vals AI row 5 failure on
    cb-ia <= 0.0.149: vc_analyst's quant-extract dispatch returned
    tool_calls=0 + 4220 chars of fabricated Reuters/Bloomberg
    citations).

    The returned :class:`Trajectory` is the only side-effect surface;
    the messages list is rebuilt locally each call. Callers that
    want to inspect the verbatim transcript can read it from the
    final message in :attr:`Trajectory.final_message`.
    """
    schemas = [t.to_openai_schema() for t in tools]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    traj = Trajectory()
    traj.final_messages = messages
    extra_kwargs = dict(extra_chat_kwargs or {})

    # Identical-call signatures seen so far. When the model emits the
    # same (tool_name, args) twice, the second response is wrapped
    # with a system notice telling it to change args or finalise --
    # prevents the "bm25_sec x5 with identical args hoping for a
    # different answer" loop we hit in practice on missing-ticker-
    # filter queries.
    prior_call_sigs: set[tuple[str, str]] = set()

    capture_set: set[str] = set(capture_tools) if capture_tools else set()

    # Track how many must-retrieve coercions we've issued so far so
    # we don't loop forever on a model that refuses to retrieve. See
    # the ``min_tool_calls_before_final`` parameter docstring.
    must_retrieve_coercions = 0

    logger.info(
        "run_react start label=%s model=%s max_steps=%d tools=%s "
        "min_tool_calls_before_final=%d user_message=%r",
        log_label, model, max_steps, [t.name for t in tools],
        min_tool_calls_before_final, user_message or "",
    )

    for round_idx in range(max_steps):
        traj.rounds = round_idx + 1
        # Force a non-tool answer on the very last allowed round so
        # we don't waste the budget calling tools we'll never get
        # to act on. The model still sees the full schemas earlier.
        is_last_round = round_idx == max_steps - 1
        # Penultimate-round soft warning. Gated on ``max_steps >= 3``
        # so we don't double-message at the cliff when budgets are
        # tiny (no roster persona uses <6, but tests sometimes do).
        is_penultimate_round = (
            not is_last_round
            and round_idx == max_steps - 2
            and max_steps >= 3
        )
        if is_penultimate_round:
            messages.append({
                "role": "system",
                "content": _BUDGET_PENULTIMATE,
            })
        if is_last_round:
            messages.append({
                "role": "system",
                "content": (
                    "You have used all your tool calls. Do NOT call any "
                    "more tools. Synthesize your findings into the JSON "
                    "answer_summary now using the data you already have."
                ),
            })
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "timeout_s": _LLM_CALL_TIMEOUT_S,
        }
        if not is_last_round:
            kwargs["tools"] = schemas
        kwargs.update(extra_kwargs)

        t0 = time.perf_counter()
        started_at_ms = int(time.time() * 1000)
        try:
            env = chat_with_retry(**kwargs)
        except Exception as exc:  # noqa: BLE001 -- provider raises a zoo
            elapsed = int((time.perf_counter() - t0) * 1000)
            traj.steps.append(
                Step(
                    kind="llm",
                    name="llm.chat",
                    started_at_ms=started_at_ms,
                    elapsed_ms=elapsed,
                    arguments={"model": model, "round": round_idx + 1},
                    has_error=True,
                    error_message=str(exc)[:500],
                )
            )
            traj.error = f"llm.chat raised: {type(exc).__name__}: {exc!s}"
            logger.error(
                "run_react label=%s round=%d llm.chat raised after retries "
                "latency_ms=%d: %s",
                log_label, round_idx + 1, elapsed, traj.error,
            )
            return traj

        elapsed = int((time.perf_counter() - t0) * 1000)
        response = env.get("response") or {}
        choices = response.get("choices") or []
        usage = response.get("usage")
        accumulate_usage(traj.token_usage, usage)
        if not choices:
            traj.steps.append(
                Step(
                    kind="llm",
                    name="llm.chat",
                    started_at_ms=started_at_ms,
                    elapsed_ms=elapsed,
                    arguments={"model": model, "round": round_idx + 1},
                    has_error=True,
                    error_message="provider returned no choices",
                )
            )
            traj.error = "llm.chat returned no choices"
            return traj

        choice_msg = (choices[0] or {}).get("message") or {}
        tool_calls = choice_msg.get("tool_calls") or []

        # Fallback for providers (Cerebras+Qwen, some Hermes builds)
        # that emit tool calls as inline ``<tool_call>{...}</tool_call>``
        # text instead of populating ``tool_calls``. Without this shim
        # the loop treats the message as a final answer and the
        # specialist silently no-ops -- see the docstring on
        # :func:`_extract_text_tool_calls`.
        #
        # CRITICAL: skip the shim on the last allowed round. We
        # already dropped ``tools=schemas`` from the kwargs above to
        # force a tool-free synthesis turn, but Qwen+Cerebras
        # routinely emits ``<tool_call>`` text anyway -- if the shim
        # extracts and we dispatch it, the for-loop exits after the
        # tool round with no chance to synthesize, and the specialist
        # returns ``react loop hit max_steps=N without a final
        # answer``. Strip the envelopes from the content so the user
        # never sees the raw ``<tool_call>...`` syntax, but DO NOT
        # dispatch -- treat whatever prose remains as the final
        # answer (empty is OK; the GP will see a thread saying
        # "no final summary; tools called: ...").
        if not tool_calls:
            text_tcs, cleaned_content = _extract_text_tool_calls(
                str(choice_msg.get("content") or "")
            )
            # JSON-payload tool_calls fallback (Qwen+seeded-structured
            # output emits ``{..., "tool_calls": [...]}`` in CONTENT
            # instead of the native API tool_use channel).
            if not text_tcs:
                json_tcs, _ = _extract_json_payload_tool_calls(
                    str(choice_msg.get("content") or "")
                )
                if json_tcs:
                    text_tcs = json_tcs
                    cleaned_content = str(choice_msg.get("content") or "")
            if text_tcs and not is_last_round:
                tool_calls = text_tcs
                # Mutate a copy so we don't surprise the caller's
                # logging by rewriting the provider's literal payload.
                choice_msg = dict(choice_msg)
                choice_msg["content"] = cleaned_content
            elif text_tcs and is_last_round:
                # Drop the would-be tool calls; keep cleaned prose.
                logger.info(
                    "run_react label=%s round=%d/%d last-round shim "
                    "stripped %d inline <tool_call> envelopes; "
                    "treating cleaned content as final answer",
                    log_label, round_idx + 1, max_steps, len(text_tcs),
                )
                choice_msg = dict(choice_msg)
                choice_msg["content"] = cleaned_content

        # Build the LLM-step preview. Prefer the model's prose
        # (post text-envelope strip), but if the turn was tool-only
        # (common with Cerebras+Qwen, where the entire content is one
        # or more `<tool_call>` envelopes that the shim removed),
        # fall back to listing the dispatched tool names. Without
        # this fallback the trajectory UI showed empty rows for
        # every tool-only turn, which made it look like the
        # specialist had done nothing.
        content_for_preview = str(choice_msg.get("content") or "").strip()
        preview = content_for_preview[:400]
        if not preview and tool_calls:
            tool_names = [
                ((tc.get("function") or {}).get("name") or "?")
                for tc in tool_calls
            ]
            preview = "\u2192 " + ", ".join(tool_names)

        traj.steps.append(
            Step(
                kind="llm",
                name="llm.chat",
                started_at_ms=started_at_ms,
                elapsed_ms=elapsed,
                arguments={
                    "model": model,
                    "round": round_idx + 1,
                    "tool_call_count": len(tool_calls),
                },
                result_preview=_truncate(preview),
            )
        )

        logger.info(
            "run_react label=%s round=%d/%d llm.chat ok latency_ms=%d "
            "tool_calls=%d tokens_in=%d tokens_out=%d content=%r",
            log_label, round_idx + 1, max_steps, elapsed, len(tool_calls),
            int((usage or {}).get("prompt_tokens") or
                (usage or {}).get("input_tokens") or 0),
            int((usage or {}).get("completion_tokens") or
                (usage or {}).get("output_tokens") or 0),
            content_for_preview,
        )

        # Append the assistant message verbatim before dispatching
        # tools so subsequent turns see the same conversation the
        # provider built its tool_calls against. Some providers
        # reject a follow-up turn whose previous assistant message
        # is missing a recorded `tool_calls` list.
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": choice_msg.get("content") or "",
        }
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            # Must-retrieve gate. If the persona contract requires N
            # retrievals before any final answer and the model is
            # trying to short-circuit, inject a coercion message and
            # continue the loop instead of terminating. Capped at
            # _MAX_MUST_RETRIEVE_COERCIONS so a stubborn model can
            # still terminate eventually -- the swarm-level
            # enforcement will discard the resulting payload.
            current_tool_calls = int(traj.token_usage.get("tool_calls", 0) or 0)
            if (
                min_tool_calls_before_final > 0
                and current_tool_calls < min_tool_calls_before_final
                and must_retrieve_coercions < _MAX_MUST_RETRIEVE_COERCIONS
                and not is_last_round
            ):
                must_retrieve_coercions += 1
                coercion = _MUST_RETRIEVE_COERCION.format(
                    min_n=min_tool_calls_before_final
                )
                messages.append({"role": "system", "content": coercion})
                logger.warning(
                    "run_react label=%s round=%d must-retrieve coercion "
                    "fired (%d/%d): model emitted final answer with "
                    "tool_calls=%d < required=%d; injecting coercion "
                    "and continuing loop",
                    log_label, round_idx + 1,
                    must_retrieve_coercions, _MAX_MUST_RETRIEVE_COERCIONS,
                    current_tool_calls, min_tool_calls_before_final,
                )
                # Skip the dispatch block (no tool_calls) and let the
                # for-loop iterate to issue the next LLM call.
                continue

            traj.final_message = choice_msg
            logger.info(
                "run_react label=%s done rounds=%d final_content_chars=%d "
                "must_retrieve_coercions=%d",
                log_label, traj.rounds,
                len(str(choice_msg.get("content") or "")),
                must_retrieve_coercions,
            )
            return traj

        # Dispatch every requested tool in order -- providers may
        # return multiple tool_calls in one turn (parallel tool
        # calling). We do them sequentially rather than parallel
        # because the kernel is the rate-limit point and we want
        # back-pressure to surface, not be hidden in concurrency.
        for tc in tool_calls:
            tc_id = tc.get("id") or "call_unknown"
            fn = (tc.get("function") or {})
            tool_name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (TypeError, ValueError) as exc:
                args = {}
                args_err = f"could not parse tool_call arguments: {exc}"
            else:
                args_err = None

            t_tool0 = time.perf_counter()
            tool_started_ms = int(time.time() * 1000)

            if args_err:
                tool_payload = {"error": args_err, "raw": raw_args}
                tool_result_str = _serialize_tool_result(tool_payload)
                traj.steps.append(
                    Step(
                        kind="tool",
                        name=tool_name,
                        started_at_ms=tool_started_ms,
                        elapsed_ms=int((time.perf_counter() - t_tool0) * 1000),
                        arguments={"raw": raw_args[:200]},
                        result_preview=_truncate(tool_result_str, 300),
                        has_error=True,
                        error_message=args_err,
                    )
                )
                logger.warning(
                    "run_react label=%s round=%d tool=%s arg_parse_error: %s "
                    "raw=%r",
                    log_label, round_idx + 1, tool_name or "?",
                    args_err, raw_args,
                )
                traj.token_usage["tool_calls"] = (
                    traj.token_usage.get("tool_calls", 0) + 1
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": tool_name,
                        "content": tool_result_str,
                    }
                )
                continue

            try:
                tool = lookup_tool(tool_name, tools)
            except KeyError:
                err_payload = {
                    "error": f"unknown tool {tool_name!r}",
                    "available": [t.name for t in tools],
                }
                tool_result_str = _serialize_tool_result(err_payload)
                traj.steps.append(
                    Step(
                        kind="tool",
                        name=tool_name,
                        started_at_ms=tool_started_ms,
                        elapsed_ms=int((time.perf_counter() - t_tool0) * 1000),
                        arguments=args,
                        result_preview=_truncate(tool_result_str, 300),
                        has_error=True,
                        error_message="unknown tool",
                    )
                )
                logger.warning(
                    "run_react label=%s round=%d tool=%s unknown_tool; "
                    "available=%s",
                    log_label, round_idx + 1, tool_name,
                    [t.name for t in tools],
                )
                traj.token_usage["tool_calls"] = (
                    traj.token_usage.get("tool_calls", 0) + 1
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": tool_name,
                        "content": tool_result_str,
                    }
                )
                continue

            # Constraint enforcement (Pass 6): before_tool_call may mutate
            # ``args`` (inject/clamp asof) or raise ConstraintViolation.
            # Both constraints AND enforcer must be set in the current
            # harness_context() for enforcement to engage; missing either
            # means "unconstrained run" (back-compat for legacy callers).
            _constraints = current_constraints()
            _enforcer = current_enforcer()
            _enforce = _constraints is not None and _enforcer is not None
            _enforce_violation: Optional[ConstraintViolation] = None
            if _enforce:
                try:
                    args = dict(_enforcer.before_tool_call(
                        tool_name, tool.fn, args, _constraints,
                    ))
                except ConstraintViolation as exc:
                    _enforce_violation = exc

            logger.info(
                "run_react label=%s round=%d tool=%s dispatch args=%s",
                log_label, round_idx + 1, tool_name, _args_preview(args),
            )

            trace = _langfuse.get_active()
            tool_exc: Optional[BaseException] = None
            if _enforce_violation is not None:
                # Enforcer rejected the dispatch; surface as a tool-error
                # message the model can read and route around. No call.
                has_err = True
                err_msg = (
                    f"{type(_enforce_violation).__name__}: "
                    f"{_enforce_violation!s}"
                )
                result_payload = {
                    "error": err_msg,
                    "tool": tool_name,
                    "arguments": args,
                    "constraint_violation": True,
                }
                tool_exc = _enforce_violation
            else:
                try:
                    result_payload = tool.fn(**args)
                    has_err = False
                    err_msg = None
                    if _enforce:
                        result_payload = _enforcer.after_tool_call(
                            tool_name, result_payload, _constraints,
                        )
                except Exception as exc:  # noqa: BLE001 -- tools can raise zoo
                    has_err = True
                    err_msg = f"{type(exc).__name__}: {exc!s}"
                    result_payload = {
                        "error": err_msg,
                        "tool": tool_name,
                        "arguments": args,
                    }
                    tool_exc = exc

            tool_elapsed = int((time.perf_counter() - t_tool0) * 1000)
            if trace is not None:
                trace.record_tool(
                    name=tool_name,
                    params=args,
                    result=result_payload,
                    latency_ms=tool_elapsed,
                    error=tool_exc,
                )
            # Capture full payloads for whitelisted tools so the caller
            # can reconstruct structured output downstream -- Step's
            # ``result_preview`` is clipped to 300 chars which is too
            # narrow for OHLC bar lists and similar. Only capture on
            # success; an error envelope is already in Step.error_message.
            if not has_err and tool_name in capture_set:
                traj.captured_tool_results.setdefault(tool_name, []).append(
                    result_payload
                )
            tool_result_str = _serialize_tool_result(result_payload)
            is_empty = (not has_err) and _result_looks_empty(result_payload)
            if has_err:
                logger.warning(
                    "run_react label=%s round=%d tool=%s raised "
                    "latency_ms=%d args=%s error=%s",
                    log_label, round_idx + 1, tool_name, tool_elapsed,
                    _args_preview(args), err_msg,
                )
            elif is_empty:
                logger.warning(
                    "run_react label=%s round=%d tool=%s returned empty "
                    "latency_ms=%d args=%s result=%s",
                    log_label, round_idx + 1, tool_name, tool_elapsed,
                    _args_preview(args), tool_result_str,
                )
            else:
                logger.info(
                    "run_react label=%s round=%d tool=%s ok latency_ms=%d "
                    "result_bytes=%d result=%r",
                    log_label, round_idx + 1, tool_name, tool_elapsed,
                    len(tool_result_str), tool_result_str,
                )
            # Loop-escape hint: flag identical (tool, args) repeats.
            try:
                arg_key = json.dumps(args, sort_keys=True, default=str)
            except (TypeError, ValueError):
                arg_key = repr(args)
            call_sig = (tool_name, arg_key)
            if call_sig in prior_call_sigs:
                tool_result_str = (
                    "[SYSTEM NOTE: you just made this exact tool call "
                    "and got the same result. Change your arguments "
                    "or produce a final answer now -- do not repeat "
                    "this call.]\n\n"
                    + tool_result_str
                )
                logger.warning(
                    "run_react label=%s round=%d tool=%s identical_repeat "
                    "args=%s",
                    log_label, round_idx + 1, tool_name,
                    _args_preview(args),
                )
            prior_call_sigs.add(call_sig)

            traj.steps.append(
                Step(
                    kind="tool",
                    name=tool_name,
                    started_at_ms=tool_started_ms,
                    elapsed_ms=tool_elapsed,
                    arguments=args,
                    result_preview=_truncate(tool_result_str, 300),
                    has_error=has_err,
                    error_message=err_msg,
                )
            )
            traj.token_usage["tool_calls"] = (
                traj.token_usage.get("tool_calls", 0) + 1
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tool_name,
                    "content": tool_result_str,
                }
            )

    # Loop fell off the end without a no-tool answer. Set an error
    # but still return the trajectory -- callers can inspect the
    # last assistant message for a partial answer.
    traj.error = (
        f"react loop hit max_steps={max_steps} without a final answer"
    )
    logger.warning(
        "run_react label=%s hit max_steps=%d without final answer "
        "(tool_calls=%d)",
        log_label, max_steps, traj.token_usage.get("tool_calls", 0),
    )
    return traj


# --------------------------------------------------------------------------
# JSON-from-content helper
# --------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL,
)


# ----------------------------------------------------------------------------
# In-content tool-call envelope (Qwen / Hermes / Llama-3.1 fallback)
# ----------------------------------------------------------------------------
#
# Several open-weight models that the platform routes to (Qwen 3
# family on DeepInfra / Together, plus the historical Cerebras Qwen
# 235B that was the swarm's default before 2026-05-27 deprecation)
# do *not* populate the OpenAI ``tool_calls`` array even
# when ``tools=[...]`` is on the request. They instead emit the tool
# call as inline text using the Hermes / Qwen envelope::
#
#     <tool_call>
#     {"name": "<tool>", "arguments": {<args>}}
#     </tool_call>
#
# A message can contain one or more such envelopes, optionally
# interleaved with prose. If we trust ``choice_msg["tool_calls"]``
# alone we drop those calls on the floor and the specialist returns
# what looks like a final answer that's actually unexecuted intent
# (observed in prod: NVIDIA 8-K query, run 41f0a37c, where 3 of 4
# specialists emitted text-envelope calls and the synth converged on
# stale data). The shim below detects the envelope, parses the
# embedded JSON, and synthesises an OpenAI-shape tool_call list so
# the dispatch loop downstream is provider-agnostic.
_TOOL_CALL_ENVELOPE_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL,
)

# Qwen also emits a *second*, non-JSON envelope flavour where the
# tool call body is XML-ish::
#
#     <tool_call>
#     <function=get_full_text>
#     <parameter=ref>
#     sec:0001193125-23-233499:2.01
#     </parameter>
#     <parameter=max_chars>
#     16000
#     </parameter>
#     </function>
#     </tool_call>
#
# This is the format Cerebras+Qwen-3-235b sometimes "switches into"
# late in a long conversation -- often on the LAST allowed round, when
# the model is meant to be synthesising a final answer but instead
# emits one more textual tool dispatch. The JSON-only parser above
# rejects this body (``json.loads("<function=...>")`` raises) and the
# call falls through as raw text, leaving the specialist with an
# unparseable "final answer" of literal XML (observed: Vals AI row 7
# sector_analyst, request df248c96, 7 native tool_calls then an
# 8th-round XML envelope -> success=False with empty payload).
#
# The parser below recovers the call without hard-coding the JSON
# vs. XML choice into the caller -- it tries JSON first (the native
# Qwen-instruct shape) and falls back to XML when that fails.
_FUNCTION_BLOCK_RE = re.compile(
    r"<function\s*=\s*([^>\s]+)\s*>(.*?)</function>", re.DOTALL,
)
_PARAMETER_BLOCK_RE = re.compile(
    r"<parameter\s*=\s*([^>\s]+)\s*>(.*?)</parameter>", re.DOTALL,
)


def _parse_xml_tool_call_body(body: str) -> Optional[dict[str, Any]]:
    """Parse a Qwen XML tool-call body into ``{"name", "arguments"}``.

    Returns ``None`` when the body doesn't look like the XML flavour
    so the caller can fall through to other parsers / skip the
    envelope. Argument values are JSON-decoded when possible (so
    numeric / boolean / list / dict args round-trip correctly) and
    kept as the raw stripped string otherwise (so plain identifiers
    like ``sec:0001193125-23-233499:2.01`` survive). This mirrors
    how Qwen itself rehydrates the call when it sees it echoed back.
    """
    fn_match = _FUNCTION_BLOCK_RE.search(body)
    if not fn_match:
        return None
    name = fn_match.group(1).strip()
    if not name:
        return None
    inner = fn_match.group(2)
    args: dict[str, Any] = {}
    for pm in _PARAMETER_BLOCK_RE.finditer(inner):
        key = pm.group(1).strip()
        if not key:
            continue
        raw_val = pm.group(2).strip()
        # Try JSON first so numbers / bools / lists / nested objects
        # round-trip into the dispatched arg dict the way the model
        # intended. Strings without quotes are the common case for
        # Qwen XML envelopes -- fall back to the raw stripped text.
        try:
            args[key] = json.loads(raw_val)
        except (TypeError, ValueError):
            args[key] = raw_val
    return {"name": name, "arguments": args}


def _extract_json_payload_tool_calls(
    content: str,
) -> tuple[list[dict[str, Any]], str]:
    """Extract ``tool_calls`` from a structured JSON payload in content.

    Recent prompt iterations push the model to emit a single JSON
    object with persona fields (`answerable`, `answer_summary`,
    `reasoning`, …) plus a `tool_calls` array. Qwen complies by
    emitting the JSON in CONTENT instead of using the API tool_use
    channel, so the loop sees ``choice_msg["tool_calls"] = []`` and
    declares the turn a final answer. The trajectory log then shows
    ``tool_calls=0`` for every round and the specialist exits with
    a "plan" instead of executing it -- silent dispatch loss.

    Recover by parsing the JSON payload from content; if it has a
    top-level `tool_calls` array of `{name, arguments}` items, lift
    those into the OpenAI-shape tool-call list the dispatch loop
    expects. Return ``([], content)`` unchanged when the payload
    isn't JSON, has no `tool_calls` field, or none of the items
    parse as callable.

    Content stays UNMODIFIED when this fallback fires -- the
    persona-field parser still needs to extract `answer_summary` /
    `reasoning` from the same payload after the tools execute.
    """
    if not content or "tool_calls" not in content:
        return [], content
    payload = extract_json_payload(content)
    if not isinstance(payload, dict):
        return [], content
    raw_calls = payload.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        return [], content
    out: list[dict[str, Any]] = []
    for idx, tc in enumerate(raw_calls):
        if not isinstance(tc, dict):
            continue
        # Two shapes: OpenAI-native ({"function": {"name", "arguments"}})
        # or the bare-name shape the prompt examples show
        # ({"name": "...", "arguments": {...}}).
        name = None
        args: Any = None
        fn = tc.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            args = fn.get("arguments")
        if not isinstance(name, str) or not name:
            name = tc.get("name")
            args = tc.get("arguments") if args is None else args
        if not isinstance(name, str) or not name:
            continue
        if args is None:
            args = tc.get("args")
        if args is None:
            args = {}
        if isinstance(args, str):
            try:
                json.loads(args)
                args_json = args
            except (TypeError, ValueError):
                args_json = json.dumps({"_raw": args})
        else:
            try:
                args_json = json.dumps(args, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                args_json = "{}"
        out.append(
            {
                "id": f"call_json_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": args_json},
            }
        )
    return out, content


def _extract_text_tool_calls(
    content: str,
) -> tuple[list[dict[str, Any]], str]:
    """Parse ``<tool_call>{...}</tool_call>`` envelopes out of message content.

    Returns ``(tool_calls, cleaned_content)``:

    * ``tool_calls`` is a list of dicts in the OpenAI shape
      (``{"id", "type": "function", "function": {"name", "arguments"}}``)
      with ``arguments`` JSON-stringified, ready to be appended to the
      assistant message and dispatched by the existing loop.
    * ``cleaned_content`` is ``content`` with the parsed envelopes
      removed (and surrounding whitespace collapsed) so the trajectory
      preview shows the model's prose, not a wall of tool-call JSON.

    Best-effort: malformed envelopes (truncated JSON, missing
    ``name``) are skipped silently rather than blowing up the
    specialist -- the model still got *some* of its calls dispatched
    and can recover on the next turn from any ones we couldn't parse.
    Returns ``([], content)`` unchanged when the marker isn't present
    so the hot path on native-tool-calls providers stays a single
    substring check.
    """
    if not content or "<tool_call>" not in content:
        return [], content

    out: list[dict[str, Any]] = []
    for idx, m in enumerate(_TOOL_CALL_ENVELOPE_RE.finditer(content)):
        body = m.group(1).strip()
        # Strip a stray ```json fence the model sometimes wraps the
        # envelope body in.
        if body.startswith("```"):
            body = body.lstrip("`")
            if body.lower().startswith("json"):
                body = body[4:]
            body = body.strip().rstrip("`").strip()
        obj: Optional[dict[str, Any]]
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            obj = parsed
        else:
            # Fall back to the Qwen XML envelope flavour (see
            # _parse_xml_tool_call_body docstring). This is the
            # round-7 sector_analyst failure mode on Vals AI row 7.
            obj = _parse_xml_tool_call_body(body)
            if obj is None:
                continue
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = obj.get("arguments")
        if args is None:
            args = {}
        if isinstance(args, str):
            # Already-stringified args are valid OpenAI shape; pass
            # them through but make sure they parse so the dispatch
            # loop doesn't have to second-guess.
            try:
                json.loads(args)
                args_json = args
            except (TypeError, ValueError):
                args_json = json.dumps({"_raw": args})
        else:
            try:
                args_json = json.dumps(args, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                args_json = "{}"
        out.append(
            {
                "id": f"call_text_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": args_json},
            }
        )

    cleaned = _TOOL_CALL_ENVELOPE_RE.sub("", content).strip()
    return out, cleaned


def _try_repair_truncated_json(cand: str) -> Optional[dict[str, Any]]:
    """Best-effort repair of a JSON object that was cut off mid-emission.

    Specialists hit by the ``max_tokens`` cap leave us with strings
    like ``{"answerable": true, "answer_summary": "## Header\\n\\nSome
    content that just stops mid-`` -- a balanced parser rejects this
    even though most of the structured fields are intact.

    Strategy:

    1. Walk the string and track the JSON token state machine
       (in-string, escape, brace/bracket depth).
    2. If we end inside a string, close it (``"``) before closing
       any open structures.
    3. Close open ``[`` / ``{`` in reverse order of opening.
    4. Strip a trailing dangling comma + whitespace before any close
       (``json`` rejects ``{"a": 1,}``).
    5. Try ``json.loads`` on the patched string; return the dict on
       success, ``None`` on failure.

    This is intentionally tolerant: we'd rather salvage a payload
    where ``answer_summary`` is mid-sentence than drop the whole
    specialist output. The synthesizer can still treat short or
    suspicious payloads with caution.
    """
    if not cand or not cand.lstrip().startswith("{"):
        return None
    depth_stack: list[str] = []
    in_string = False
    escape = False
    for ch in cand:
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth_stack.append("}")
        elif ch == "[":
            depth_stack.append("]")
        elif ch in "}]":
            if depth_stack and depth_stack[-1] == ch:
                depth_stack.pop()
    patched = cand
    if in_string:
        patched += '"'
    # Strip dangling commas that would otherwise need to be closed.
    # We do this *after* injecting the closing quote so a string
    # containing a literal comma at the end isn't damaged.
    patched_stripped = patched.rstrip()
    while patched_stripped.endswith(","):
        patched_stripped = patched_stripped[:-1].rstrip()
    patched = patched_stripped
    while depth_stack:
        patched += depth_stack.pop()
    try:
        obj = json.loads(patched)
    except (TypeError, ValueError):
        return None
    if isinstance(obj, dict):
        return obj
    return None


def extract_json_payload(text: str) -> Optional[dict[str, Any]]:
    """Best-effort parse of the structured JSON personas are asked to emit.

    A persona prompt asks the model to reply with one JSON object.
    Most providers comply, but a few wrap it in a ```json fence
    anyway; we handle both cases.

    When the strict pass fails (most commonly because the model hit
    its ``max_tokens`` cap mid-emission, leaving an unbalanced
    object), :func:`_try_repair_truncated_json` is called as a
    last-resort fallback that closes any open strings / brackets /
    braces. The recovered dict is marked with ``__repaired_json: True``
    so downstream callers can degrade trust accordingly (e.g. the
    swarm uses this to lower ``confidence`` when the specialist
    didn't get to finish writing).

    Returns ``None`` if no JSON object can be found even after
    repair -- the caller decides whether that's a soft failure or
    fatal.
    """
    if not text:
        return None
    candidates: list[str] = []
    fenced = _JSON_FENCE_RE.findall(text)
    candidates.extend(fenced)

    # Also try the first balanced {...} segment in the raw text in
    # case the model dropped the fence. Greedy match is fine because
    # ``json.loads`` will reject anything malformed.
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    else:
        first = stripped.find("{")
        last = stripped.rfind("}")
        if first != -1 and last > first:
            candidates.append(stripped[first : last + 1])

    # Strict pass first.
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj

    # Truncation-repair pass. Try on the largest candidate (the whole
    # stripped text from the first ``{`` onward) since fenced blocks
    # are typically complete; repair is for the unfenced, cut-off case.
    repair_target: Optional[str] = None
    if stripped.startswith("{"):
        repair_target = stripped
    else:
        first = stripped.find("{")
        if first != -1:
            repair_target = stripped[first:]
    if repair_target:
        repaired = _try_repair_truncated_json(repair_target)
        if repaired is not None:
            repaired["__repaired_json"] = True
            return repaired
    return None


# ---------------------------------------------------------------------------
# Shared helpers for the orchestrator (synthesizer / planner / postprocessor)
# ---------------------------------------------------------------------------


def format_common_thread(thread: Sequence[Mapping[str, Any]]) -> str:
    """Render a common thread into ``[USER]: ... / [<name>]: ...`` format.

    Used by the synthesizer, the planner, and the post-processor to build
    the user-message body the GP model reads each round.
    """
    parts: list[str] = []
    for msg in thread:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "user":
            parts.append(f"[USER]: {content}")
            continue
        name = msg.get("name") or role or "agent"
        parts.append(f"[{name}]: {content}")
    return "\n\n".join(parts)


@dataclass(frozen=True)
class JsonLlmResult:
    """Outcome of :func:`json_llm_call` -- a single-shot JSON-mode LLM call
    with bounded retry.

    ``parsed`` is the extracted dict on success, ``None`` on exhaustion.
    Callers build their typed decision/result from the parsed dict + the
    accounting fields.
    """

    parsed: Optional[dict[str, Any]]
    raw: str
    token_usage: dict[str, int]
    latency_ms: int
    attempts: int
    error: Optional[str]


_JSON_RETRY_REMINDER = (
    "\n\n### REMINDER ###\n"
    "Reply with a single JSON object only. First char `{`, last `}`. "
    "No markdown fences."
)


def json_llm_call(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_s: float = 180.0,
    max_retries: int = 3,
    log_label: str = "json_llm",
) -> JsonLlmResult:
    """Call the LLM expecting a JSON object back, with bounded retry.

    On each retry the temperature drops to 0 and a "single JSON object"
    reminder is appended. Returns the first successfully-parsed dict, or
    ``parsed=None`` with an ``error`` marker if all attempts fail.

    This is the shared loop behind ``plan_round`` and ``postprocess`` --
    each builds its typed result from the returned :class:`JsonLlmResult`.
    """
    token_usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "tool_calls": 0,
    }
    last_raw = ""
    total_latency_ms = 0

    for attempt in range(max_retries):
        attempt_temp = temperature if attempt == 0 else 0.0
        suffix = _JSON_RETRY_REMINDER if attempt > 0 else ""
        msg = user_message + suffix

        t0 = time.perf_counter()
        try:
            env = chat_with_retry(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": msg},
                ],
                temperature=attempt_temp,
                max_completion_tokens=max_tokens,
                timeout_s=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.perf_counter() - t0) * 1000)
            total_latency_ms += elapsed
            logger.warning(
                "%s llm.chat raised on attempt %d/%d: %s",
                log_label, attempt + 1, max_retries, exc,
            )
            if attempt == max_retries - 1:
                return JsonLlmResult(
                    parsed=None,
                    raw=last_raw,
                    token_usage=token_usage,
                    latency_ms=total_latency_ms,
                    attempts=attempt + 1,
                    error="llm_chat_error",
                )
            continue

        elapsed = int((time.perf_counter() - t0) * 1000)
        total_latency_ms += elapsed
        response = env.get("response") or {}
        accumulate_usage(token_usage, response.get("usage"))
        choices = response.get("choices") or []
        message = (choices[0] or {}).get("message") if choices else {}
        raw = (message or {}).get("content") or ""
        last_raw = raw

        parsed = extract_json_payload(raw)
        if parsed is None:
            logger.warning(
                "%s attempt %d/%d returned non-JSON (first 200: %r)",
                log_label, attempt + 1, max_retries, raw[:200],
            )
            continue

        return JsonLlmResult(
            parsed=parsed,
            raw=raw,
            token_usage=token_usage,
            latency_ms=total_latency_ms,
            attempts=attempt + 1,
            error=None,
        )

    return JsonLlmResult(
        parsed=None,
        raw=last_raw,
        token_usage=token_usage,
        latency_ms=total_latency_ms,
        attempts=max_retries,
        error="json_parse_failure",
    )


__all__ = [
    "JsonLlmResult",
    "Step",
    "Trajectory",
    "accumulate_usage",
    "chat_with_retry",
    "extract_json_payload",
    "format_common_thread",
    "json_llm_call",
    "run_react",
    "_extract_text_tool_calls",
]
