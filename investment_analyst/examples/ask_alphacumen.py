"""Ask AlphaCumen a single question via the Coral platform SDK.

This is the minimum submit-and-poll path the eval harness uses to score
the Vals AI Finance Agent Benchmark and FinanceBench. Wrap it in a loop
over benchmark rows (plus per-row ``asof`` injection and an override
store) and you have the harness that produced the numbers in the
companion blog post.

Note: ``coralbricks-platform`` is in design-partner preview today; the
public ``pip install`` path opens in Phase 2. Existing design partners
can install from the private package index that ships with their API
key.

Setup:
    pip install coralbricks-platform
    export CORAL_API_KEY=ak_...
    export CORAL_PLATFORM_URL=https://...   # provided with your API key
"""

from __future__ import annotations

import os
import time

from coralbricks.client import PlatformClient


def ask(question: str, *, asof: str | None = None) -> str:
    """Submit one question to cb-ia and return the answer summary.

    ``asof`` (ISO-8601 UTC) pins the run to a specific date via
    ``mode="backtest"``; when omitted the run uses ``mode="live"`` and
    sees the corpus as of submit time.
    """
    client = PlatformClient(
        base_url=os.environ["CORAL_PLATFORM_URL"],
        api_key=os.environ["CORAL_API_KEY"],
    )
    try:
        submit_kwargs = {
            "pipeline_package": "cb-ia==latest",
            "inputs": [{"query": question}],
            "mode": "backtest" if asof else "live",
        }
        if asof:
            submit_kwargs["asof"] = asof

        batch = client.submit_batch(**submit_kwargs)
        request_id = batch.runs[0].request_id

        while True:
            rec = client.get_run(request_id)
            status = getattr(rec, "status", None) or rec.__dict__.get("status")
            if status in ("completed", "failed", "cancelled"):
                break
            time.sleep(3)

        if status != "completed":
            raise RuntimeError(f"run did not complete: status={status}")

        result = rec.__dict__.get("result_json") or {}
        return result.get("answer_summary") or ""
    finally:
        client.close()


if __name__ == "__main__":
    answer = ask(
        "What was Coca-Cola's FY24 dividend payout ratio vs. its peers?",
    )
    print(answer)
