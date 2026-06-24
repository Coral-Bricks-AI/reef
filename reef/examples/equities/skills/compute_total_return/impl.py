# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Trailing 1-year price return computation over the in-repo companies corpus."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reef.skill_fn import skill_fn

_DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "companies.json"


with _DATA_PATH.open(encoding="utf-8") as _f:
    _COMPANIES: list[dict[str, Any]] = json.load(_f)
_BY_TICKER: dict[str, dict[str, Any]] = {c["ticker"]: c for c in _COMPANIES}


@skill_fn(
    skill_id="compute_total_return",
    description=(
        "Compute trailing 1-year price return for a ticker. "
        "Returns pct_return_1y + a grader-ready answer_summary_block."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Ticker symbol from a search_companies result (e.g. 'NVDA').",
            },
        },
        "required": ["ticker"],
    },
)
def compute_total_return(*, ticker: str) -> dict[str, Any]:
    company = _BY_TICKER.get(ticker.upper())
    if company is None:
        return {
            "error": (
                f"unknown ticker={ticker!r}; "
                f"call search_companies first to get a valid ticker."
            )
        }
    price_now = float(company["price_now"])
    price_1y_ago = float(company["price_1y_ago"])
    pct = (price_now - price_1y_ago) / price_1y_ago * 100
    pct = round(pct, 1)
    direction = "returned" if pct >= 0 else "lost"
    summary_block = (
        f"**{company['name']} ({company['ticker']})** {direction} "
        f"**{pct:+.1f}%** over the trailing 12 months "
        f"(${price_1y_ago:.2f} → ${price_now:.2f}). "
        f"Price return only; does not include dividends."
    )
    return {
        "ticker": company["ticker"],
        "pct_return_1y": pct,
        "price_now": price_now,
        "price_1y_ago": price_1y_ago,
        "answer_summary_block": summary_block,
    }
