# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shared config and low-level constants for the AlphaCumen evals.

Both eval subpackages (``valsai/``, ``financebench/``) read this. The
defaults target the **hosted AlphaCumen pipeline on the Coral
platform**; submission goes over the public gateway, grading goes
through the Anthropic API. Override the gateway URL with
``$CORAL_PLATFORM_URL`` if you have access to a private deployment.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Optional

# macOS system Python ships without a CA bundle wired up; api.anthropic.com
# and the Coral gateway fail SSL verify out of the box. Use certifi's bundle
# when it's available.
try:
    import certifi

    SSL_CTX: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()


# Public Coral platform gateway. Override per run with ``$CORAL_PLATFORM_URL``
# for a staging / private deployment.
GATEWAY_URL_DEFAULT = (
    "http://coral-platform-nlb-c26425961ea96f05.elb.us-east-1.amazonaws.com:8765"
)

# Default pipeline package to submit against. ``cb-ia`` is the production
# AlphaCumen pipeline (the open-source ``alphacumen/`` code shipped as a
# CodeArtifact wheel + a manifest the gateway resolves). Pin to an exact
# version (e.g. ``cb-ia==0.0.420``) for repeatable runs across days.
PIPELINE_PACKAGE_DEFAULT = "cb-ia==latest"

# Indices the hosted AlphaCumen pipeline retrieves over. These are the
# public-data corpora the prod runtime maintains -- SEC filings, GDELT,
# market data, macro series. Sent as an input field so the gateway scopes
# retrieval consistently across rows.
PIPELINE_INDICES = [
    "gdelt_events_v2",
    "sec_filings_chunked",
    "sec_filings_ann",
    "scraped_articles_bge_m3",
    "graph_combined",
    "macro_v1",
    "equity_bars_v1",
]

# Run polling. 1300s ceiling matches the gateway's slow-MoE sandbox wall
# budget (1200s) plus 100s of slack for the queue dispatch + judge step.
# Cerebras-class fast models typically finish in under 200s; the higher
# ceiling only matters when the run targets a slow MoE.
POLL_INTERVAL_S = 3.0
POLL_TIMEOUT_S = 1300.0

# Defaults shared by both eval subpackages. Each subpackage reads its own
# ``*_JUDGE_MODEL`` / ``*_HARNESS_MODEL`` env var so per-benchmark overrides
# keep working; these are the fallback values.
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"
# The Lilac-proxied Moonshot Kimi K2.6 MoE was the model that produced the
# published 82.6% / 90% / 89.3% numbers. Override with ``--model`` or the
# per-benchmark env var to try a different one.
DEFAULT_HARNESS_MODEL = "lilac/moonshotai/kimi-k2.6"


def read_coral_api_key(path: Optional[Path] = None) -> str:
    """Read the Coral platform API key.

    Order of precedence:

    1. ``$CORAL_API_KEY`` env var (raw ``ak_...`` value).
    2. ``path`` (or ``$CORAL_API_KEY_FILE`` if set) as a file containing
       either a raw ``ak_...`` line OR a ``KEY=VALUE`` line whose key is
       ``API_KEY`` or ``CORAL_API_KEY``.
    3. ``~/.coral/api_key`` as the same KEY=VALUE-or-raw format.

    Raises ``RuntimeError`` with a clear message if nothing is found.
    """
    env_key = (os.environ.get("CORAL_API_KEY") or "").strip()
    if env_key:
        return env_key

    candidates = []
    if path is not None:
        candidates.append(Path(path))
    env_path = os.environ.get("CORAL_API_KEY_FILE")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.home() / ".coral" / "api_key")

    for p in candidates:
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in ("API_KEY", "CORAL_API_KEY"):
                    return v.strip().strip('"').strip("'")
            elif line.startswith("ak_"):
                return line
        raise RuntimeError(
            f"no API key found in {p} (expected ``ak_...`` line or "
            f"``API_KEY=ak_...`` / ``CORAL_API_KEY=ak_...``)"
        )
    raise RuntimeError(
        "no Coral API key configured. Set $CORAL_API_KEY, $CORAL_API_KEY_FILE, "
        "or write ``ak_...`` to ~/.coral/api_key. Sign up / find your key at "
        "https://coralbricks.ai/alphacumen."
    )
