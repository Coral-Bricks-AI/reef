# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.llm`` -- direct-provider LLM client.

Drop-in replacement for ``coralbricks.sandbox.llm`` that talks to
providers directly via their SDKs. Same envelope shape as the
sandbox proxy, so ``react.py``'s watchdog/fallback/retry machinery
keeps working unchanged.

Provider dispatch is by model prefix. The model id looks like
``<provider>/<model>`` and the prefix routes the call:

- ``openai/...`` -> OpenAI direct via ``OPENAI_API_KEY`` (or ``LLM_API_KEY`` as a generic fallback)
- ``anthropic/...`` -> Anthropic direct via ``ANTHROPIC_API_KEY`` (or ``LLM_API_KEY``)
- ``aws/...`` -> Bedrock via boto3 + standard AWS creds
- ``lilac/...`` -> OpenAI-compatible proxy at ``LILAC_BASE_URL``
  with ``LILAC_API_KEY``
- ``together/...`` -> Together AI (OpenAI-shape) at
  ``TOGETHER_BASE_URL`` / ``TOGETHER_API_KEY``
- ``openrouter/...`` -> OpenRouter (OpenAI-shape) at
  ``OPENROUTER_BASE_URL`` / ``OPENROUTER_API_KEY``
- ``cerebras/...`` -> Cerebras Cloud (OpenAI-shape) at
  ``CEREBRAS_BASE_URL`` / ``CEREBRAS_API_KEY``
- ``deepinfra/...``, ``qwen/...`` -> DeepInfra (OpenAI-shape) at
  ``DEEPINFRA_BASE_URL`` / ``DEEPINFRA_API_KEY``
- bare ``<vendor>/<model>`` (e.g. ``moonshotai/kimi-k2.6``) ->
  default OpenAI-shape proxy using ``LLM_BASE_URL`` /
  ``LLM_API_KEY`` (escape hatch for new providers)

The envelope returned matches the gateway proxy's shape::

    {"model": "<model>",
     "response": {"id": ..., "choices": [...], "usage": {...}}}

so callers (the ReAct loop, any planner / synthesizer above it)
see one stable wire shape regardless of provider. Translation happens here
when a provider returns a different native shape (e.g. Anthropic's
``content`` blocks vs OpenAI's ``message``).

Why this lives next to ``react.py`` and not in a separate package
-------------------------------------------------------------------
The harness's whole resilience story (watchdog, per-model timeout
overrides, provider fallback) sits one layer above the chat call.
Keeping ``llm.chat`` in the same module hierarchy makes that
resilience easy to reason about: one function call, one retry
boundary, no cross-package indirection. Pluggability is via the
model-prefix routing -- a new provider is one new branch + one
new pair of env vars.
"""

from __future__ import annotations

import os
import time
from typing import Any, Mapping, Optional, Sequence


# Per-provider env-var conventions. Add a new entry to add a new
# OpenAI-shape proxy.
_OPENAI_SHAPE_PROVIDERS: dict[str, tuple[str, str, str]] = {
    # prefix -> (base_url_env, api_key_env, default_base_url)
    "openai": ("OPENAI_BASE_URL", "OPENAI_API_KEY", "https://api.openai.com/v1"),
    "lilac": ("LILAC_BASE_URL", "LILAC_API_KEY", "https://console.getlilac.com/v1"),
    "together": ("TOGETHER_BASE_URL", "TOGETHER_API_KEY", "https://api.together.xyz/v1"),
    "openrouter": (
        "OPENROUTER_BASE_URL",
        "OPENROUTER_API_KEY",
        "https://openrouter.ai/api/v1",
    ),
    "cerebras": (
        "CEREBRAS_BASE_URL",
        "CEREBRAS_API_KEY",
        "https://api.cerebras.ai/v1",
    ),
    "deepinfra": (
        "DEEPINFRA_BASE_URL",
        "DEEPINFRA_API_KEY",
        "https://api.deepinfra.com/v1/openai",
    ),
    # Qwen models were historically served by DeepInfra; the ``qwen/``
    # prefix routes there too unless ``QWEN_BASE_URL`` is set.
    "qwen": (
        "QWEN_BASE_URL",
        "QWEN_API_KEY",
        "https://api.deepinfra.com/v1/openai",
    ),
}


def _split_model(model: str) -> tuple[str, str]:
    """Return (provider_prefix, model_tail) for a slash-prefixed id.

    ``"openai/gpt-4o"`` -> ``("openai", "gpt-4o")``
    ``"lilac/moonshotai/kimi-k2.6"`` -> ``("lilac", "moonshotai/kimi-k2.6")``
    ``"gpt-4o"`` (no prefix) -> ``("", "gpt-4o")`` -- caller decides
    what to do (defaults to OpenAI).
    """
    if "/" not in model:
        return "", model
    prefix, _, tail = model.partition("/")
    return prefix.lower(), tail


def _openai_shape_chat(
    *,
    base_url: str,
    api_key: str,
    model_for_provider: str,
    params: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """Issue a chat.completions call to an OpenAI-compatible endpoint.

    Uses the ``openai`` Python SDK with a per-call client (clients
    are cheap to construct and we don't want a global one because
    different calls in a single run may target different providers).
    """
    # Lazy import so the package can be inspected without the SDK
    # installed, but real calls require it.
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "openai SDK required for OpenAI-shape providers. "
            "Install with: pip install 'openai>=1.0'"
        ) from exc

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s)
    sdk_kwargs = dict(params)
    sdk_kwargs["model"] = model_for_provider
    completion = client.chat.completions.create(**sdk_kwargs)
    # The SDK returns a Pydantic model; coerce to dict in the gateway
    # envelope shape.
    if hasattr(completion, "model_dump"):
        response_dict = completion.model_dump()
    else:  # pragma: no cover -- legacy SDK
        response_dict = dict(completion)
    return {"model": model_for_provider, "response": response_dict}


def _anthropic_chat(
    *,
    model_for_provider: str,
    params: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """Issue an Anthropic Messages call and translate to OpenAI shape.

    Anthropic's ``messages.create`` returns ``content`` blocks; we
    translate to the OpenAI ``{choices: [{message: {role, content,
    tool_calls}}]}`` shape so the ReAct loop reads the same fields
    regardless of provider.
    """
    try:
        from anthropic import Anthropic  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "anthropic SDK required for anthropic/ models. "
            "Install with: pip install 'anthropic>=0.30'"
        ) from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "set LLM_API_KEY (or ANTHROPIC_API_KEY) — required for anthropic/ models."
        )

    client = Anthropic(api_key=api_key, timeout=timeout_s)

    # Anthropic separates system from messages.
    messages = list(params.get("messages") or [])
    system_parts: list[str] = []
    filtered_messages: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str):
                system_parts.append(content)
            continue
        filtered_messages.append(dict(msg))

    anthropic_kwargs: dict[str, Any] = {
        "model": model_for_provider,
        "messages": filtered_messages,
        "max_tokens": params.get("max_tokens") or params.get(
            "max_completion_tokens"
        ) or 4096,
    }
    if system_parts:
        anthropic_kwargs["system"] = "\n\n".join(system_parts)
    if params.get("temperature") is not None:
        anthropic_kwargs["temperature"] = params["temperature"]
    if params.get("top_p") is not None:
        anthropic_kwargs["top_p"] = params["top_p"]
    if params.get("stop") is not None:
        anthropic_kwargs["stop_sequences"] = (
            params["stop"] if isinstance(params["stop"], list) else [params["stop"]]
        )
    # Anthropic tool calling -- pass-through; SDK accepts a similar
    # tools-list shape.
    if params.get("tools") is not None:
        anthropic_kwargs["tools"] = params["tools"]

    msg = client.messages.create(**anthropic_kwargs)

    # Translate Anthropic content blocks to OpenAI shape.
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in msg.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": _json_dumps(block.input),
                },
            })
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "model": model_for_provider,
        "response": {
            "id": msg.id,
            "choices": [{
                "index": 0,
                "finish_reason": msg.stop_reason or "stop",
                "message": message,
            }],
            "usage": {
                "prompt_tokens": msg.usage.input_tokens,
                "completion_tokens": msg.usage.output_tokens,
                "total_tokens": msg.usage.input_tokens + msg.usage.output_tokens,
            },
        },
    }


def _bedrock_chat(
    *,
    model_for_provider: str,
    params: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """Issue a Bedrock Converse call and translate to OpenAI shape.

    Uses boto3 + the Bedrock Converse API, which has a unified shape
    across model families on Bedrock (Claude, Llama, Mistral, etc.).
    Standard AWS creds resolution (env / instance profile / SSO).
    """
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "boto3 required for aws/ (Bedrock) models. "
            "Install with: pip install 'boto3>=1.34'"
        ) from exc

    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=__import__("botocore.client", fromlist=["Config"]).Config(
            read_timeout=int(timeout_s),
            connect_timeout=10,
        ),
    )

    # Translate OpenAI messages -> Bedrock Converse messages.
    messages = list(params.get("messages") or [])
    system_blocks: list[dict[str, Any]] = []
    converse_messages: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str):
                system_blocks.append({"text": content})
            continue
        converse_role = "user" if role == "user" else "assistant"
        if isinstance(content, str):
            converse_messages.append({
                "role": converse_role,
                "content": [{"text": content}],
            })

    inference_config: dict[str, Any] = {}
    if params.get("max_tokens") is not None:
        inference_config["maxTokens"] = params["max_tokens"]
    elif params.get("max_completion_tokens") is not None:
        inference_config["maxTokens"] = params["max_completion_tokens"]
    if params.get("temperature") is not None:
        inference_config["temperature"] = params["temperature"]
    if params.get("top_p") is not None:
        inference_config["topP"] = params["top_p"]

    converse_kwargs: dict[str, Any] = {
        "modelId": model_for_provider,
        "messages": converse_messages,
    }
    if system_blocks:
        converse_kwargs["system"] = system_blocks
    if inference_config:
        converse_kwargs["inferenceConfig"] = inference_config

    resp = client.converse(**converse_kwargs)

    output_msg = resp.get("output", {}).get("message", {})
    output_text = "".join(
        b.get("text", "") for b in output_msg.get("content", [])
    )
    usage = resp.get("usage", {})

    return {
        "model": model_for_provider,
        "response": {
            "id": resp.get("ResponseMetadata", {}).get("RequestId", ""),
            "choices": [{
                "index": 0,
                "finish_reason": resp.get("stopReason", "stop"),
                "message": {
                    "role": "assistant",
                    "content": output_text,
                },
            }],
            "usage": {
                "prompt_tokens": usage.get("inputTokens", 0),
                "completion_tokens": usage.get("outputTokens", 0),
                "total_tokens": usage.get("totalTokens", 0),
            },
        },
    }


def _json_dumps(obj: Any) -> str:
    """Serialize tool args to JSON string for the OpenAI envelope."""
    import json
    return json.dumps(obj, default=str, ensure_ascii=False)


def chat(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Optional[Sequence[Mapping[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: Optional[int] = None,
    max_completion_tokens: Optional[int] = None,
    n: Optional[int] = None,
    seed: Optional[int] = None,
    stop: Optional[Any] = None,
    response_format: Optional[Mapping[str, Any]] = None,
    parallel_tool_calls: Optional[bool] = None,
    user: Optional[str] = None,
    socket_path: Optional[str] = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Run a chat completion against the provider implied by ``model``.

    Drop-in replacement for ``coralbricks.sandbox.llm.chat`` --
    accepts the same kwargs, returns the same envelope shape::

        {"model": "<model>",
         "response": {"id": ..., "choices": [...], "usage": {...}}}

    Provider dispatch is by the slash-prefix of ``model``:

    - ``openai/...``, ``anthropic/...``, ``aws/...`` -- native SDKs
    - ``lilac/...``, ``together/...``, ``openrouter/...``,
      ``cerebras/...``, ``deepinfra/...``, ``qwen/...`` --
      OpenAI-compatible proxies
    - bare ``<model>`` (no prefix) -- routes through ``LLM_API_KEY``
      (or ``OPENAI_API_KEY``) against the OpenAI endpoint

    The ``socket_path`` kwarg is accepted for signature compatibility
    with the sandbox proxy and is ignored.
    """
    del socket_path  # unused; here for sandbox-proxy signature parity

    # Bundle the non-None knobs into a params dict the providers can
    # filter against.
    params: dict[str, Any] = {"messages": list(messages)}
    for key, value in (
        ("tools", list(tools) if tools is not None else None),
        ("tool_choice", tool_choice),
        ("temperature", temperature),
        ("top_p", top_p),
        ("max_tokens", max_tokens),
        ("max_completion_tokens", max_completion_tokens),
        ("n", n),
        ("seed", seed),
        ("stop", stop),
        ("response_format", dict(response_format) if response_format is not None else None),
        ("parallel_tool_calls", parallel_tool_calls),
        ("user", user),
    ):
        if value is not None:
            params[key] = value

    prefix, tail = _split_model(model)

    # Native-SDK providers.
    if prefix == "anthropic":
        return _anthropic_chat(
            model_for_provider=tail,
            params=params,
            timeout_s=timeout_s,
        )
    if prefix == "aws":
        return _bedrock_chat(
            model_for_provider=tail,
            params=params,
            timeout_s=timeout_s,
        )

    # OpenAI-shape proxies.
    if prefix in _OPENAI_SHAPE_PROVIDERS:
        base_url_env, api_key_env, default_base_url = _OPENAI_SHAPE_PROVIDERS[
            prefix
        ]
        base_url = os.environ.get(base_url_env, default_base_url)
        # Provider-specific env var wins (lets multi-provider setups keep
        # distinct keys), but LLM_API_KEY is honored as the generic
        # fallback so single-provider users only set one variable.
        api_key = os.environ.get(api_key_env) or os.environ.get("LLM_API_KEY")
        if not api_key:
            raise RuntimeError(
                f"set LLM_API_KEY (or {api_key_env}) — required for {prefix}/ models."
            )
        # For pure OpenAI, pass the bare model name. For proxies that
        # accept ``vendor/model`` ids (Lilac, OpenRouter), pass the
        # full tail (which already includes the vendor segment).
        model_for_provider = tail if prefix in {
            "lilac", "openrouter", "together", "deepinfra", "qwen"
        } else tail
        return _openai_shape_chat(
            base_url=base_url,
            api_key=api_key,
            model_for_provider=model_for_provider,
            params=params,
            timeout_s=timeout_s,
        )

    # Unrecognized prefix or bare model name -> fall back to the
    # generic OpenAI-shape endpoint via LLM_BASE_URL/LLM_API_KEY.
    base_url = os.environ.get("LLM_BASE_URL")
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if base_url and api_key:
        return _openai_shape_chat(
            base_url=base_url,
            api_key=api_key,
            model_for_provider=model,
            params=params,
            timeout_s=timeout_s,
        )

    raise RuntimeError(
        f"Unrouted model {model!r}: no provider prefix matched and "
        "no LLM_BASE_URL/LLM_API_KEY fallback configured. Set "
        "<PROVIDER>_API_KEY for a known prefix, or LLM_BASE_URL + "
        "LLM_API_KEY for a generic OpenAI-shape endpoint."
    )


# Signature-only stubs kept so callers that previously imported
# ``ping``/``list_models``/``embed`` from the sandbox proxy don't
# break at import time. Each raises if invoked, since the gateway
# context that gave them meaning isn't present in OSS execution.

def ping(*, socket_path: Optional[str] = None, timeout_s: float = 5.0) -> dict[str, Any]:
    """No-op when running OSS (no gateway to ping)."""
    del socket_path, timeout_s
    return {"ok": True, "mode": "direct"}


def list_models(
    *, socket_path: Optional[str] = None, timeout_s: float = 5.0,
) -> dict[str, Any]:
    """OSS mode has no per-run allowlist; return ``unrestricted=True``."""
    del socket_path, timeout_s
    return {"models": [], "unrestricted": True}


def embed(
    *,
    model: str,
    dimension: int,
    texts: Sequence[str],
    input_type: str = "product",
    batch_size: Optional[int] = None,
    socket_path: Optional[str] = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Embeddings are out of scope for the OSS LLM client.

    The harness pattern assumes pre-built retrieval indices; embeddings
    are a corpus-prep concern handled by the consumer. If you need
    embeddings at runtime, call the provider SDK directly.
    """
    del model, dimension, texts, input_type, batch_size, socket_path, timeout_s
    raise NotImplementedError(
        "embed() is out of scope for the OSS LLM client. Pre-build your "
        "retrieval indices at corpus-prep time, or call the provider "
        "SDK directly."
    )


__all__ = ["chat", "embed", "list_models", "ping"]
