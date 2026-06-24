#!/usr/bin/env python3
# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Ask the **hosted** AlphaCumen pipeline a single question.

The agent code under ``alphacumen/`` is open source and standalone --
the framework primitives (planner, specialists, skills, runtime
constraints) all work in-process. But the kernel retrieval verbs
(``bm25``, ``ann``, ``sql``, ``multihop``, ``get``, ``py``) live in
``reef/stubs/`` as ``NotImplementedError`` stubs in the OSS clone.
The hosted runtime on the Coral platform swaps these for the real
backends (~4.5TB of SEC filings, GDELT, macro series, market data).

This example submits over HTTPS to the hosted pipeline so you can
exercise the full agent end-to-end without wiring your own retrieval
backends.

Set ``CORAL_API_KEY`` before running -- sign up / get a key at
https://coralbricks.ai/alphacumen. Optionally point at a non-default
gateway with ``$CORAL_PLATFORM_URL`` (staging / private deployments).

Wiring your own retrieval (so the in-process ``alphacumen.swarm.run()``
works against your data) is also supported -- see
[`reef/stubs/`](../../reef/stubs).

Usage::

    export CORAL_API_KEY=ak_...
    python -m alphacumen.examples.ask_hosted_alphacumen \\
        "What was Apple's FY2024 total revenue?"
    python -m alphacumen.examples.ask_hosted_alphacumen \\
        --model anthropic/claude-sonnet-4-6 \\
        --asof 2025-03-01T00:00:00Z \\
        "Compare Nvidia and AMD data-center revenue Q1 2025."
"""

from __future__ import annotations

import argparse
import sys

from alphacumen.evals.common import config, runner


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Submit one question to hosted AlphaCumen and print the answer.",
    )
    parser.add_argument(
        "--model", type=str, default=config.DEFAULT_HARNESS_MODEL,
        help=f"Model id forwarded to the hosted gateway. "
             f"Default: {config.DEFAULT_HARNESS_MODEL!r}.",
    )
    parser.add_argument(
        "--asof", type=str, default=None,
        help="Optional ISO-8601 UTC date (e.g. '2025-03-01T00:00:00Z'). "
             "Pins the run to a past date via mode=backtest.",
    )
    parser.add_argument(
        "--pipeline-package", type=str,
        default=config.PIPELINE_PACKAGE_DEFAULT,
        help="Pipeline package spec forwarded to the gateway. "
             f"Default: {config.PIPELINE_PACKAGE_DEFAULT!r}.",
    )
    parser.add_argument(
        "question", nargs="*",
        help="The question to ask. Defaults to a sample if omitted.",
    )
    args = parser.parse_args()

    q = " ".join(args.question) or "What was Apple's FY2024 total revenue?"

    try:
        api_key = config.read_coral_api_key()
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    print(f"Q: {q}\n")
    try:
        answer, _result, _rec = runner.ask_alphacumen_hosted(
            q,
            model=args.model,
            api_key=api_key,
            pipeline_package=args.pipeline_package,
            asof=args.asof,
        )
    except Exception as exc:  # noqa: BLE001 -- surface and exit
        print(f"[error] hosted run failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nA: {answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
