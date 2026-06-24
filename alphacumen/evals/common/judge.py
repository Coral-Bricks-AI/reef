# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Anthropic judge transport.

Stdlib ``urllib`` to ``api.anthropic.com/v1/messages``.
``ANTHROPIC_API_KEY`` from env, retries on 429/503/529, robust JSON
extraction from the model's text.

The two benchmarks use different verdict shapes (FinanceBench returns a
single ``{"verdict": ...}`` object; Vals AI grades one rubric atom at a
time and returns ``{"pass": ...}``), so the JSON extractor is
parameterized by the expected key.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from alphacumen.evals.common.config import SSL_CTX


def post_anthropic(
    api_key: str, model: str, system: str, user: str, max_tokens: int = 1024
) -> tuple[int, str]:
    """POST one message to the Anthropic API. Returns ``(status, body_text)``."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": user}],
    }
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            pass
        return exc.code, text or exc.reason
    except urllib.error.URLError as exc:
        return 503, f"network error: {exc.reason!s}"


def _json_re(verdict_key: str) -> re.Pattern[str]:
    """A ``{...}`` matcher that contains ``verdict_key``. Spans multi-line reasons."""
    return re.compile(
        r"\{[^{}]*\"" + re.escape(verdict_key) + r"\"[^{}]*\}", re.DOTALL
    )


def parse_judge_json(text: str, *, verdict_key: str) -> dict[str, Any]:
    """Extract the judge verdict JSON from Claude's text.

    Robust to prose preambles and ```json fences. ``verdict_key`` is the
    field the verdict object is guaranteed to carry (``"verdict"`` or
    ``"pass"``) -- used by the last-resort regex.
    """
    raw = text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    if raw.startswith("```"):
        stripped = raw.strip("`").lstrip("json").strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    matches = _json_re(verdict_key).findall(raw)
    if matches:
        try:
            return json.loads(matches[-1])
        except json.JSONDecodeError:
            pass
    raise RuntimeError(
        f"could not parse judge JSON from claude output: {raw[:500]!r}"
    )


def judge_with_retry(
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    verdict_key: str,
) -> dict[str, Any]:
    """Call Claude; retry on 429/503/529."""
    last_status, last_text = 0, ""
    for attempt in range(4):
        status, text = post_anthropic(api_key, model, system, user)
        last_status, last_text = status, text
        if status == 200:
            envelope = json.loads(text)
            content = envelope.get("content") or []
            for block in content:
                if block.get("type") == "text":
                    return parse_judge_json(
                        block.get("text", ""), verdict_key=verdict_key
                    )
            raise RuntimeError(f"no text block in claude response: {envelope}")
        if status in (429, 503, 529):
            time.sleep(1.5 + attempt * 1.5)
            continue
        raise RuntimeError(f"claude judge failed status={status} body={text[:500]}")
    raise RuntimeError(
        f"claude judge exhausted retries; last status={last_status} body={last_text[:500]}"
    )
