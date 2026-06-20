# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Langfuse observability primitive for the harness.

Owned by the framework so any domain instance (cocktails, alphacumen, …) can
emit run-level traces without re-implementing the wiring. Callers that want a
domain-specific attribution (e.g. trace name ``alphacumen.investment_analyst``
instead of the default ``harness.run``) call :func:`configure` once at import
time before the first :class:`RunTrace` is constructed.

Envelope
--------

* One :class:`RunTrace` per top-level run. Root span keyed to the
  deterministic OTEL trace id derived from ``request_id`` so any external
  Console's "Open in Langfuse" deep-link (`?search=<request_id>`) still
  resolves without persisting a second UUID.
* :meth:`RunTrace.record_chat` emits one ``generation`` observation per
  LLM call with model, prompt messages, output preview, usage, latency.
* :meth:`RunTrace.record_tool` emits one ``tool`` observation per
  dispatched tool call with params, result preview, latency.

Env inputs
----------

* ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` -- credentials.
  Both present = tracing on. Hosted-runtime injection of these only
  happens when the manifest's ``egress`` allowlist declared a Langfuse
  host, so their presence is already a strong opt-in signal -- no
  separate kill-switch flag needed. Local runs stay hermetic by default.
* ``LANGFUSE_HOST`` (or ``LANGFUSE_BASE_URL``) -- defaults to
  ``https://us.cloud.langfuse.com``.
* ``LANGFUSE_TRACING_ENVIRONMENT`` -- ``[a-z0-9-_]+`` only.
* ``CORAL_REQUEST_ID`` -- the run's request id. The hosted runtime exports
  it alongside ``$CORAL_GATEWAY_SOCKET``. When unset (ad-hoc local
  invocation), :func:`resolve_request_id` synthesises a fresh UUID and
  logs a warning so the trace is still well-formed.

Failure handling
----------------

Langfuse errors never fail a run. Every hook is wrapped in try/except,
logged at ``debug`` level, and the pipeline continues. A missing
``langfuse`` wheel (ImportError) pins the client to ``False`` so the
rest of the run short-circuits on first access.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import re
import threading
import urllib.parse
import uuid
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


_DEFAULT_LANGFUSE_HOST = "https://us.cloud.langfuse.com"
_TRACING_ENV_PATTERN = re.compile(r"^(?!langfuse)[a-z0-9-_]+$")

_client_lock = threading.Lock()
_client: Any = None


# Domain-attribution defaults. Override via :func:`configure` at import time
# before any :class:`RunTrace` is built; the source tag flows into every
# observation's metadata and the root span name.
_DEFAULT_SOURCE = "harness"
_DEFAULT_PIPELINE = "run"
_source = _DEFAULT_SOURCE
_pipeline_default = _DEFAULT_PIPELINE


def configure(
    *,
    source: Optional[str] = None,
    pipeline_default: Optional[str] = None,
) -> None:
    """Set module-wide attribution defaults.

    ``source`` lands in every observation's ``metadata["source"]`` and is
    the prefix of the root span name (``<source>.<pipeline>``).

    ``pipeline_default`` is the fallback :class:`RunTrace` ``pipeline``
    argument when callers don't pass one (e.g. ``alphacumen`` defaults to
    ``investment_analyst``; a non-finance harness can leave it at ``run``).

    Call this once at the consuming package's import time, before any
    :class:`RunTrace` is constructed. Idempotent; the last call wins.
    """
    global _source, _pipeline_default
    if source is not None:
        _source = source
    if pipeline_default is not None:
        _pipeline_default = pipeline_default


def is_enabled() -> bool:
    """True iff the harness should emit Langfuse traces for this run.

    Gated solely on credential presence: hosted-runtime injection of
    ``LANGFUSE_*`` is itself the opt-in signal, so keys-in-env is enough.
    Local runs without those keys stay hermetic.
    """
    pk = (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    sk = (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()
    return bool(pk and sk)


def _host() -> str:
    h = (
        os.environ.get("LANGFUSE_HOST")
        or os.environ.get("LANGFUSE_BASE_URL")
        or ""
    ).strip()
    return h or _DEFAULT_LANGFUSE_HOST


def otel_trace_id(external_id: str) -> str:
    """Map an arbitrary correlation id to OTEL's 32-lowercase-hex shape."""
    raw = (external_id or "").strip()
    compact = raw.lower().replace("-", "")
    if len(compact) == 32 and re.fullmatch(r"[0-9a-f]{32}", compact):
        return compact
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def trace_url(otel_trace_id: str, client: Any | None = None) -> str:
    """Build a project-scoped Langfuse deep-link for a given OTEL trace id."""
    c = client if client is not None else get_client()
    if c is not None:
        try:
            url = c.get_trace_url(trace_id=otel_trace_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Langfuse get_trace_url failed: %s", exc)
            url = None
        if url:
            return url
    base = _host().rstrip("/")
    return f"{base}/?pending_trace={urllib.parse.quote(otel_trace_id, safe='')}"


def get_client() -> Any | None:
    """Return the singleton Langfuse client, or ``None`` when disabled.

    Lazy-initialises on first call. A failed init pins the client to
    ``False`` so subsequent calls short-circuit.
    """
    global _client
    if not is_enabled():
        return None
    if _client is False:
        return None
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client if _client is not False else None
        try:
            from langfuse import Langfuse  # type: ignore[import-not-found]
        except ImportError as exc:
            logger.warning(
                "Langfuse enabled but the 'langfuse' package isn't "
                "installed (%s); %s telemetry off for this run.",
                exc, _source,
            )
            _client = False
            return None
        try:
            _client = Langfuse(
                host=_host(),
                public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip(),
                secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Langfuse init failed (%s); %s telemetry off.",
                exc, _source,
            )
            _client = False
            return None
        env = (os.environ.get("LANGFUSE_TRACING_ENVIRONMENT") or "").strip()
        if env and not _TRACING_ENV_PATTERN.match(env):
            logger.warning(
                "LANGFUSE_TRACING_ENVIRONMENT=%r violates Langfuse's "
                "[a-z0-9-_]+ rule; events will be dropped server-side.",
                env,
            )
        logger.info(
            "%s Langfuse client up host=%s tracing_env=%s",
            _source, _host(), env or "(unset)",
        )
        return _client


def flush() -> None:
    """Best-effort flush of the singleton client."""
    c = _client
    if c is None or c is False:
        return
    try:
        c.flush()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Langfuse flush failed: %s", exc)


atexit.register(flush)


def _string_metadata(meta: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in meta.items():
        if v is None:
            out[str(k)[:200]] = ""
            continue
        out[str(k)[:200]] = str(v)[:200]
    return out


def _truncate_for_preview(value: Any, *, max_chars: int = 4000) -> Any:
    """Pass-through. Prior char caps destroyed structural parseability of
    the logged messages array. The Langfuse SDK serialises native Python
    structures directly. ``max_chars`` kept for ABI compat; it is ignored.
    """
    return value


def _chat_output_preview(response: Optional[Mapping[str, Any]]) -> Any:
    if not response:
        return None
    try:
        choices = response.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            names = []
            for tc in tool_calls:
                fn = (tc or {}).get("function") or {}
                nm = fn.get("name")
                if isinstance(nm, str):
                    names.append(nm)
            return {
                "kind": "tool_calls",
                "n": len(tool_calls),
                "names": names[:32],
                "text_preview": _truncate_for_preview(content, max_chars=400)
                if isinstance(content, str)
                else None,
            }
        return _truncate_for_preview(content)
    except Exception:  # noqa: BLE001
        return None


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _extract_usage(response: Optional[Mapping[str, Any]]) -> dict[str, int]:
    if not response:
        return {}
    usage = response.get("usage") if isinstance(response, Mapping) else None
    if not isinstance(usage, Mapping):
        return {}
    out: dict[str, int] = {}
    for src, dst in (
        ("prompt_tokens", "input"),
        ("completion_tokens", "output"),
        ("total_tokens", "total"),
    ):
        v = _coerce_int(usage.get(src))
        if v is not None:
            out[dst] = v
    return out


class RunTrace:
    """Per-run Langfuse root span + child observation factory.

    One instance per top-level run (e.g. one ``swarm.run`` call). Methods
    are no-ops when Langfuse is disabled or the SDK failed to initialise --
    callers don't have to branch on :func:`is_enabled`.

    ``pipeline`` is the run's pipeline name; it falls into the root span
    name (``<source>.<pipeline>``) and observation metadata. Defaults to
    whatever :func:`configure` set (or ``"run"`` out of the box).
    """

    def __init__(
        self,
        *,
        request_id: str,
        pipeline: Optional[str] = None,
        query: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self._request_id = request_id
        self._pipeline = pipeline or _pipeline_default
        self._source = _source
        self._query = query
        self._model = model
        self._otel_trace_id = otel_trace_id(request_id)
        self._lock = threading.Lock()
        self._client: Any | None = None
        self._root: Any | None = None
        self._started = False
        self._ended = False

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def trace_id(self) -> str:
        return self._otel_trace_id

    @property
    def trace_url(self) -> str:
        return trace_url(self._otel_trace_id, self._client)

    def start(self) -> None:
        """Open the root span. Idempotent and exception-safe."""
        if self._started:
            return
        self._started = True
        client = get_client()
        if client is None:
            return
        self._client = client
        meta = _string_metadata(
            {
                "request_id": self._request_id,
                "pipeline": self._pipeline,
                "model": self._model or "",
                "source": self._source,
            }
        )
        try:
            self._root = client.start_observation(
                trace_context={"trace_id": self._otel_trace_id},
                name=f"{self._source}.{self._pipeline}",
                as_type="span",
                input={"query": self._query} if self._query is not None else None,
                metadata=meta,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Langfuse root span start failed for request_id=%s: %s",
                self._request_id, exc,
            )
            self._root = None
            return
        logger.info(
            "%s langfuse trace opened request_id=%s trace_id=%s host=%s url=%s",
            self._source, self._request_id, self._otel_trace_id, _host(), self.trace_url,
        )

    def end(
        self,
        *,
        output: Optional[Any] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        """Close the root span and flush. Idempotent and exception-safe."""
        if self._ended:
            return
        self._ended = True
        root = self._root
        if root is None:
            flush()
            return
        try:
            update_kwargs: dict[str, Any] = {}
            if output is not None:
                update_kwargs["output"] = _truncate_for_preview(output)
            if error is not None:
                update_kwargs["level"] = "ERROR"
                update_kwargs["status_message"] = str(error)[:500]
            if update_kwargs:
                root.update(**update_kwargs)
            root.end()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Langfuse root span end failed for request_id=%s: %s",
                self._request_id, exc,
            )
        finally:
            flush()

    def record_chat(
        self,
        *,
        model: str,
        messages: Any,
        response: Optional[Mapping[str, Any]],
        latency_ms: int,
        error: Optional[BaseException] = None,
    ) -> None:
        """Emit a ``generation`` observation under the run's root span."""
        if not self._started:
            return
        client = self._client or get_client()
        if client is None:
            return
        usage_details = _extract_usage(response)
        meta = _string_metadata(
            {
                "request_id": self._request_id,
                "pipeline": self._pipeline,
                "model": model,
                "latency_ms": latency_ms,
                "source": "harness.react",
            }
        )
        try:
            with self._lock:
                start_kwargs: dict[str, Any] = {
                    "name": "llm.chat",
                    "as_type": "generation",
                    "model": model,
                    "input": _truncate_for_preview(messages, max_chars=20000),
                    "metadata": meta,
                }
                if self._root is not None:
                    obs = self._root.start_observation(**start_kwargs)
                else:
                    start_kwargs["trace_context"] = {"trace_id": self._otel_trace_id}
                    obs = client.start_observation(**start_kwargs)
                update_kwargs: dict[str, Any] = {
                    "output": _chat_output_preview(response),
                }
                if usage_details:
                    update_kwargs["usage_details"] = usage_details
                if error is not None:
                    update_kwargs["level"] = "ERROR"
                    update_kwargs["status_message"] = str(error)[:500]
                obs.update(**update_kwargs)
                obs.end()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Langfuse chat generation failed for request_id=%s: %s",
                self._request_id, exc,
            )

    def record_tool(
        self,
        *,
        name: str,
        params: Mapping[str, Any],
        result: Any,
        latency_ms: int,
        error: Optional[BaseException] = None,
    ) -> None:
        """Emit a ``tool`` observation under the run's root span."""
        if not self._started:
            return
        client = self._client or get_client()
        if client is None:
            return
        meta = _string_metadata(
            {
                "request_id": self._request_id,
                "pipeline": self._pipeline,
                "tool": name,
                "latency_ms": latency_ms,
                "source": "harness.react",
            }
        )
        try:
            with self._lock:
                start_kwargs: dict[str, Any] = {
                    "name": name,
                    "as_type": "tool",
                    "input": _truncate_for_preview(dict(params), max_chars=8000),
                    "metadata": meta,
                }
                if self._root is not None:
                    obs = self._root.start_observation(**start_kwargs)
                else:
                    start_kwargs["trace_context"] = {"trace_id": self._otel_trace_id}
                    obs = client.start_observation(**start_kwargs)
                update_kwargs: dict[str, Any] = {
                    "output": _truncate_for_preview(result, max_chars=4000),
                }
                if error is not None:
                    update_kwargs["level"] = "ERROR"
                    update_kwargs["status_message"] = str(error)[:500]
                obs.update(**update_kwargs)
                obs.end()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Langfuse tool observation failed for request_id=%s tool=%s: %s",
                self._request_id, name, exc,
            )


_active_trace: Optional[RunTrace] = None
_active_trace_lock = threading.Lock()


def set_active(trace: Optional[RunTrace]) -> None:
    """Set (or clear) the process-wide active trace."""
    global _active_trace
    with _active_trace_lock:
        _active_trace = trace


def get_active() -> Optional[RunTrace]:
    """Return the active :class:`RunTrace`, or ``None`` when disabled."""
    return _active_trace


def resolve_request_id() -> str:
    """Pick the request id to key the root trace to.

    Reads ``$CORAL_REQUEST_ID`` (the hosted runtime exports it alongside
    ``$CORAL_GATEWAY_SOCKET``). Falls back to a synthesized UUID for
    ad-hoc local invocations so the trace is still well-formed; in that
    case the Console's request-id-based deep-link won't resolve.
    """
    rid = (os.environ.get("CORAL_REQUEST_ID") or "").strip()
    if rid:
        return rid
    synth = str(uuid.uuid4())
    logger.info(
        "%s: $CORAL_REQUEST_ID unset; synthesizing request_id=%s for "
        "Langfuse trace (Console deep-link will not resolve)",
        _source, synth,
    )
    return synth


__all__ = [
    "RunTrace",
    "configure",
    "flush",
    "get_active",
    "get_client",
    "is_enabled",
    "otel_trace_id",
    "resolve_request_id",
    "set_active",
    "trace_url",
]
