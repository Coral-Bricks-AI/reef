# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``issuer_ratios`` skill impl — per-ticker ratio pull.

Hosts 1 ``@skill_fn``-registered callable for the sector_analyst
dispatch via ``invoke_skill_fn``. The planner decomposes
multi-ticker comparisons into per-ticker dispatches (one
``sector_analyst`` instance per ticker, per the planner seed's
"N tickers → N dispatches" rule), so each invocation covers ONE
issuer with its own as-of date. The postprocessor stitches the
per-ticker ``answer_summary_block``s into the cross-ticker
ranking + peer averages — comparison composition does not happen
at this layer.

Retrieval + ratio math share the helpers in :mod:`alphacumen.tools`
(``_pull_issuer_metrics_for_ratios``, ``_compute_one_ratio``); the
BVPS extractor + extended equity-concept list + 20-F fallback all
fire automatically per ticker.
"""

from __future__ import annotations

from typing import Any, Optional
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _RATIO_CHOICES,
    _apply_binding,
    _coerce_to_str_list,
    _compute_one_ratio,
    _format_ratio,
    _pull_issuer_metrics_for_ratios,
)


@skill_fn(
    skill_id='compute_issuer_ratios',
    name='compute_issuer_ratios',
    description=(
        "Pull XBRL + market-price for ONE US-listed ticker and compute one "
        "or more named ratios. Use when the planner has dispatched a "
        "per-ticker leg of a multi-issuer comparison — each call covers "
        "exactly one ticker with its own as-of date. Supported ratios: "
        "p_to_b (close ÷ issuer-disclosed BVPS when published, else "
        "MarketCap ÷ Equity), d_to_e (Debt ÷ Equity), d_to_tc "
        "(Debt ÷ (Debt + Equity)), ev_ebitda, ebitda_margin, "
        "ebitdar_margin, dio, fccr. Returns answer_summary_block ready "
        "to drop verbatim — the postprocessor stitches it with the "
        "sibling specialists' posts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Single US-listed ticker symbol, e.g. 'AER'.",
            },
            "ratios": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(_RATIO_CHOICES),
                },
                "description": (
                    "Ratios to compute. Choose from: "
                    + ", ".join(_RATIO_CHOICES) + "."
                ),
            },
            "fy": {
                "type": "integer",
                "description": "Fiscal year for the balance-sheet / income-statement pull, e.g. 2025.",
            },
            "asof_market_price": {
                "type": "string",
                "description": (
                    "Optional YYYY-MM-DD closing-price date for "
                    "market-cap-dependent ratios. Defaults to the "
                    "FY-end calendar date."
                ),
            },
            "bs_asof_date": {
                "type": "string",
                "description": (
                    "Optional YYYY-MM-DD balance-sheet snapshot date. "
                    "Use when the rubric pins the balance-sheet to a "
                    "non-FY-end date (e.g. row 9: FTAI/WLFC at "
                    "2025-09-30 from their Q3 10-Q while AER stays at "
                    "FY-end 2025-12-31). Pulls equity / debt / cash / "
                    "inventory from the filing whose BS snapshot "
                    "covers this date; income-statement items still "
                    "come from the FY 10-K so EBITDA-derived ratios "
                    "preserve annual semantics. Defaults to FY-end."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "ratios", "fy"],
    },
)
def compute_issuer_ratios(
    ticker: str,
    ratios: Any,
    fy: int,
    asof_market_price: Optional[str] = None,
    bs_asof_date: Optional[str] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Per-ticker ratio pull. Returns this issuer's primitives + each
    requested ratio in a per-ticker ``answer_summary_block`` ready for
    the postprocessor to stitch into a cross-ticker comparison.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        return {"error": "ticker required (string)"}
    t = ticker.strip().upper()
    ratios_list = _coerce_to_str_list(ratios)
    if not ratios_list:
        return {"error": "ratios list required (got nothing parseable)"}
    invalid = [r for r in ratios_list if r not in _RATIO_CHOICES]
    if invalid:
        return {
            "error": (
                f"unsupported ratio(s): {invalid}. "
                f"choose from: {list(_RATIO_CHOICES)}"
            )
        }
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": f"fy must be an int (e.g. 2025), got {fy!r}"}

    metrics = _pull_issuer_metrics_for_ratios(
        t, fy_int, asof_market_price, bs_asof_date=bs_asof_date,
    )
    if metrics.get("error"):
        return _apply_binding(bind_as, {
            "ticker": t, "fy": fy_int,
            "error": metrics["error"],
            "ratios": {r: None for r in ratios_list},
            "answer_summary_block": (
                f"{t} FY{fy_int}: retrieval failed — {metrics['error']}"
            ),
        })

    computed = {r: _compute_one_ratio(r, metrics) for r in ratios_list}
    formatted = {r: _format_ratio(r, computed[r]) for r in ratios_list}

    lines: list[str] = []
    lines.append(
        f"{t} FY{fy_int} (filing {metrics.get('filed_at') or 'n/a'}, "
        f"asof {metrics.get('asof') or 'n/a'}):"
    )
    for r in ratios_list:
        lines.append(f"  - {r}: {formatted.get(r, 'n/a')}")
    # Surface the primitives so the postprocessor's peer-average math
    # has the per-ticker raw values, not just the formatted strings.
    lines.append(
        f"  - equity=${(metrics.get('equity') or 0)/1e6:,.1f}M, "
        f"debt={((metrics.get('debt_total') or 0))/1e6:,.1f}M, "
        f"cash=${(metrics.get('cash') or 0)/1e6:,.1f}M, "
        f"market_cap=${(metrics.get('market_cap') or 0)/1e6:,.1f}M, "
        f"close=${metrics.get('close_price') or 0:,.2f}, "
        f"bvps_reported={metrics.get('bvps_reported')}"
    )
    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy": fy_int,
        "filed_at": metrics.get("filed_at"),
        "ref": metrics.get("ref"),
        "asof_market_price": metrics.get("asof"),
        "inputs": {
            "equity": metrics.get("equity"),
            "debt_total_financial": metrics.get("debt_total"),
            "lease_liability_total": metrics.get("lease_liability_total"),
            "cash": metrics.get("cash"),
            "revenue": metrics.get("revenue"),
            "ebitda": metrics.get("ebitda"),
            "ebitdar": metrics.get("ebitdar"),
            "cogs": metrics.get("cogs"),
            "inventory_end": metrics.get("inventory_end"),
            "operating_lease_cost": metrics.get("operating_lease_cost"),
            "interest_expense": metrics.get("interest_expense"),
            "market_cap": metrics.get("market_cap"),
            "close_price": metrics.get("close_price"),
            "bvps_reported": metrics.get("bvps_reported"),
        },
        "ratios": computed,
        "formatted": formatted,
        "answer_summary_block": answer_summary_block,
    })
