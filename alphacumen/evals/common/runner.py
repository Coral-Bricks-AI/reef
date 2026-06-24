# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Hosted AlphaCumen submission over the Coral platform gateway.

Thin urllib client. Submits a single question to the hosted pipeline,
polls until terminal, returns the candidate ``answer_summary`` plus the
full run record. Same wire protocol as the internal ``coralbricks.client``
package -- but here it lives inline so this OSS eval has no private
dependencies.

Why hosted instead of in-process? The AlphaCumen agent code under
``alphacumen/`` runs ``alphacumen.swarm.run()`` standalone, but the
kernel retrieval verbs (``bm25``, ``ann``, ``sql``, ``multihop``,
``get``, ``py``) in ``reef/stubs/`` are ``NotImplementedError`` stubs
in the OSS clone. The hosted runtime swaps these for real backends
(SEC filings, GDELT, macro). Submitting to hosted lets the published
benchmark numbers be reproduced exactly without rebuilding the data
plane locally.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from alphacumen.evals.common.config import (
    GATEWAY_URL_DEFAULT,
    PIPELINE_INDICES,
    POLL_INTERVAL_S,
    POLL_TIMEOUT_S,
    SSL_CTX,
)

_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _http_request(
    method: str,
    url: str,
    *,
    api_key: str,
    body: Optional[dict[str, Any]] = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """One JSON-in / JSON-out request to the gateway. Bearer-auth."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url=url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=SSL_CTX) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(
            f"coral gateway {method} {url} -> HTTP {exc.code}: {text or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"coral gateway unreachable: {exc.reason}") from exc


def _submit_run(
    question: str,
    *,
    model: str,
    api_key: str,
    gateway_url: str,
    pipeline_package: str,
    asof: Optional[str],
) -> str:
    """Submit one question. Returns the ``request_id``.

    Builds the same envelope ``coralbricks.client.PlatformClient.submit_batch``
    sends: one ``inputs[]`` entry with the question and the standard set of
    indices. ``mode=backtest`` + ``backtest.asof`` pins the run to a date
    (used by Vals AI rows referencing specific filing periods); otherwise
    ``mode=live``.
    """
    input_payload: dict[str, Any] = {"query": question, "indices": list(PIPELINE_INDICES)}
    body: dict[str, Any] = {
        "pipeline_package": pipeline_package,
        "framework": "langgraph",
        "model": model,
        "inputs": [input_payload],
        "mode": "live",
    }
    if asof:
        body["mode"] = "backtest"
        body["backtest"] = {"asof": asof, "egress_policy": "deny"}
    url = f"{gateway_url.rstrip('/')}/v1/batches"
    data = _http_request("POST", url, api_key=api_key, body=body)
    runs = data.get("runs") or []
    if not runs:
        raise RuntimeError(f"coral submit returned no runs: {data}")
    return runs[0]["request_id"]


def _poll_run(request_id: str, *, api_key: str, gateway_url: str) -> dict[str, Any]:
    """Poll one run until it reaches a terminal status (or we time out)."""
    url = f"{gateway_url.rstrip('/')}/v1/runs/{request_id}"
    deadline = time.time() + POLL_TIMEOUT_S
    last_status: Optional[str] = None
    while time.time() < deadline:
        rec = _http_request("GET", url, api_key=api_key)
        status = str(rec.get("status") or "")
        if status != last_status:
            print(f"  [coral] {request_id} status={status}")
            last_status = status
        if status in _TERMINAL_STATUSES:
            return rec
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(
        f"coral run {request_id} did not reach a terminal status in {POLL_TIMEOUT_S}s"
    )


def ask_alphacumen_hosted(
    question: str,
    *,
    model: str,
    api_key: str,
    gateway_url: str = GATEWAY_URL_DEFAULT,
    pipeline_package: str = "cb-ia==latest",
    asof: Optional[str] = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Submit ``question`` to hosted AlphaCumen and wait for the answer.

    Returns ``(answer_summary, full_result_json, run_record)`` where
    ``run_record`` carries fields useful for the eval log
    (``request_id``, ``pipeline_slug``, ``time_taken_ms``, status, etc.).

    Raises ``RuntimeError`` if the run finishes in a non-completed state
    or returns an empty answer envelope.
    """
    print(f"[coral] gateway={gateway_url}")
    print(f"[coral] api_key={api_key[:8]}... model={model}")
    request_id = _submit_run(
        question,
        model=model,
        api_key=api_key,
        gateway_url=gateway_url,
        pipeline_package=pipeline_package,
        asof=asof,
    )
    print(f"[coral] submitted request_id={request_id}")
    rec = _poll_run(request_id, api_key=api_key, gateway_url=gateway_url)

    if rec.get("status") != "completed":
        raise RuntimeError(f"coral run did not complete: {rec}")

    result = rec.get("result_json") or {}
    answer_summary = result.get("answer_summary") or ""
    if not answer_summary:
        # Fall back to the structured answer, JSON-stringified.
        answer = result.get("answer") or result.get("final_answer")
        answer_summary = json.dumps(answer) if answer else ""
    if not answer_summary:
        raise RuntimeError(
            f"coral returned empty answer; result_json keys={list(result.keys())}"
        )
    return answer_summary, result, rec
