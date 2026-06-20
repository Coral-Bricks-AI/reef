# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""In-process AlphaCumen example.

Demonstrates the framework wiring end-to-end -- planner, specialists,
skills, constraints, the post-processor synthesis hop. The kernel
retrieval verbs (`bm25`, `ann`, `sql`, `multihop`, `get`, `py`) are
stubs in the open-source repo. The first specialist that tries to
retrieve will raise :class:`NotImplementedError` with a message that
points you at:

  * The hosted experience over the prefab finance corpus (~4.5TB of
    SEC filings, market data, news) -- talk to the Coral Bricks team:
        https://coralbricks.ai/alphacumen

  * Or wire your own retrieval backends (OpenSearch / Pinecone /
    DuckDB / your graph DB / your Python sandbox) and you can run
    AlphaCumen against your own data.

What this example **does** demonstrate (without any backend wired):

  * The planner LLM dispatches specialists in parallel.
  * Specialists each run a ReAct loop with their own tool roster
    and skills.
  * Constraints (asof, max_rounds, allowed_indices) are threaded
    through the run.
  * The error path on a missing retrieval backend is a clean
    NotImplementedError, not a silent failure.

Set ``OPENAI_API_KEY`` (or ``ANTHROPIC_API_KEY`` + change the model
prefix below) before running -- the planner LLM call goes out to the
provider you picked.
"""

from __future__ import annotations

import os
import sys

# Make the coral-ai checkout importable when running from the examples/
# directory without ``pip install -e .``.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from alphacumen.swarm import run


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "Set OPENAI_API_KEY (or change `model` below to an "
            "anthropic/... or aws/... prefix and set the matching key).",
            file=sys.stderr,
        )

    try:
        result = run(
            query="What was Apple's FY2024 total revenue?",
            pipeline="investment_analyst",
            model="openai/gpt-4o",
            asof="2026-06-30",
        )
    except NotImplementedError as exc:
        # Expected outcome when no retrieval backend is wired up.
        print("AlphaCumen raised NotImplementedError as expected:\n")
        print(str(exc))
        sys.exit(0)

    answer = result.get("final_answer") or {}
    summary = answer.get("answer_summary") or "(no answer)"
    print(summary)


if __name__ == "__main__":
    main()
