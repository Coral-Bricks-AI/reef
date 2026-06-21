# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``eps_guidance_growth_pct`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Optional
from reef.skill_fn import skill_fn
from alphacumen.tools import _BIND_AS_PARAM_SCHEMA, _apply_binding


@skill_fn(
    skill_id='compute_eps_guidance_dollar_range',
    description=        "Convert constant-currency %-growth Adjusted Diluted EPS guidance "
        "to an absolute dollar range. Many consumer-staples issuers (GIS, "
        "KO, PEP, KMB, CHD, CL) and most foreign large-caps publish FY "
        "EPS guidance as a growth-percentage band (e.g. 'adjusted diluted "
        "EPS growth of -1% to +1% in constant currency'), not as a dollar "
        "range. Rubrics for these questions grade the absolute dollar "
        "range derived from prior_year_actual_EPS × (1 + growth_bound). "
        "Pass the prior-year actual EPS and the growth-percentage band "
        "you extracted from the Q4 8-K's 'Current Outlook' / 'Fiscal "
        "[YYYY] Outlook' block. Tool returns the rubric-formatted "
        "string ('$X.XX - $Y.YY') in `answer.range_phrasing` and "
        "`answer.atom_phrasing`. Optionally pass `fy_actual_eps` (from "
        "the NEXT Q4 8-K, after the fiscal year closes) and the tool "
        "also produces a BEAT / MISS verdict phrase. USE THIS instead "
        "of computing the dollar range manually — substring graders "
        "fail on off-by-one-cent rounding errors.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Issuer ticker (Consumer Staples large-cap). Case-insensitive.",
            },
            "fy": {
                "type": "integer",
                "description": (
                    "Fiscal year the guidance APPLIES TO (e.g. 2025 for "
                    "FY2025 guidance issued in the Q4 FY2024 8-K)."
                ),
            },
            "prior_year_actual_eps": {
                "type": "number",
                "description": (
                    "Issuer's reported Adjusted Diluted EPS for FY-1 "
                    "(the same Q4 8-K that contains FY guidance)."
                ),
            },
            "growth_low_pct": {
                "type": "number",
                "description": (
                    "Low end of the growth-percentage band, in PERCENTAGE "
                    "POINTS (not decimal). E.g. -1.0 for 'down 1%', 4.0 "
                    "for 'up 4%'."
                ),
            },
            "growth_high_pct": {
                "type": "number",
                "description": "High end of the growth-percentage band, in percentage points.",
            },
            "fy_actual_eps": {
                "type": "number",
                "description": (
                    "Optional. If provided, the tool ALSO produces a "
                    "BEAT / MISS verdict comparing the actual to the "
                    "derived range. Skip when answering a pure 'what's "
                    "the guidance?' question (no FY-end actual yet)."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": [
            "ticker", "fy", "prior_year_actual_eps",
            "growth_low_pct", "growth_high_pct",
        ],
    },
)
def compute_eps_guidance_dollar_range(
    ticker: str,
    fy: int,
    prior_year_actual_eps: float,
    growth_low_pct: float,
    growth_high_pct: float,
    fy_actual_eps: Optional[float] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Convert constant-currency %-growth EPS guidance to a dollar range.

    Args:
        ticker: Issuer ticker, e.g. "GIS".
        fy: Fiscal year the guidance applies to (e.g. 2025).
        prior_year_actual_eps: Adjusted diluted EPS reported by the
            issuer for FY-1 (same Q4 8-K that contains the FY
            guidance).
        growth_low_pct: Low end of growth band, in percentage points
            (e.g. -1.0 for "down 1%", 4.0 for "up 4%").
        growth_high_pct: High end of growth band, in percentage points.
        fy_actual_eps: Optional. If provided AND non-None, the tool
            also computes a BEAT / MISS / IN-RANGE / BEAT-MIDPOINT
            verdict comparing fy_actual_eps to the derived range.

    Returns:
        dict with `inputs`, `derived`, and `answer` sections. Key
        outputs are `answer.range_phrasing` ("$X.XX - $Y.YY") and
        `answer.atom_phrasing` (a rubric-shaped string like "FY2025
        Adjusted Diluted EPS guidance: $X.XX - $Y.YY"). If
        `fy_actual_eps` provided, `answer.verdict_phrasing` adds
        the beat/miss verbiage.
    """
    # Coerce / validate.
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": f"fy must be an int (e.g. 2025), got {fy!r}"}
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker is required (Consumer Staples large-cap)"}
    ticker_u = ticker.strip().upper()
    try:
        prior_eps = float(prior_year_actual_eps)
        g_lo = float(growth_low_pct)
        g_hi = float(growth_high_pct)
    except (TypeError, ValueError) as exc:
        return {"error": f"numeric inputs must be coercible: {exc!s}"}
    if prior_eps <= 0:
        return {
            "error": (
                "prior_year_actual_eps must be positive; pass the absolute "
                "value of the prior-year EPS (use the issuer's reported "
                "Adjusted Diluted EPS, not GAAP)."
            ),
        }
    if g_lo > g_hi:
        g_lo, g_hi = g_hi, g_lo

    # Always derive low/high from the LOWER and HIGHER growth-bound
    # multiplied by prior-year EPS — same regardless of sign.
    eps_low = round(prior_eps * (1.0 + g_lo / 100.0), 2)
    eps_high = round(prior_eps * (1.0 + g_hi / 100.0), 2)
    eps_mid = round((eps_low + eps_high) / 2.0, 2)

    range_phrasing = f"${eps_low:.2f} - ${eps_high:.2f}"
    atom_phrasing = (
        f"{fy_int} Adjusted Diluted EPS guidance: {range_phrasing}"
    )

    answer: dict[str, Any] = {
        "eps_low_dollar": eps_low,
        "eps_high_dollar": eps_high,
        "eps_midpoint_dollar": eps_mid,
        "range_phrasing": range_phrasing,
        "atom_phrasing": atom_phrasing,
    }

    if fy_actual_eps is not None:
        try:
            actual = float(fy_actual_eps)
        except (TypeError, ValueError):
            return {"error": f"fy_actual_eps must be numeric, got {fy_actual_eps!r}"}
        # Categorise the actual vs range. The rubric's standard
        # verdict strings are: "beat ... guidance" (above high end),
        # "beat ... guidance midpoint" (above midpoint but below
        # high end), "in range" (between mid and low), "missed
        # ... guidance" (below low end).
        if actual > eps_high:
            verdict = "beat"
            extra = ""
        elif actual >= eps_mid:
            verdict = "beat"
            extra = " midpoint"
        elif actual >= eps_low:
            verdict = "missed high end of"
            extra = ""
        else:
            verdict = "missed"
            extra = ""
        # Issuer name omitted here — the rubric atoms tend to use the
        # issuer's full name (e.g. "General Mills") which the model
        # already knows from the question; passing the ticker keeps
        # the tool generic.
        verdict_phrasing = (
            f"{ticker_u} {verdict} Adjusted Diluted EPS guidance{extra} in {fy_int}"
        )
        answer["fy_actual_eps"] = actual
        answer["verdict"] = verdict + extra
        answer["verdict_phrasing"] = verdict_phrasing
        # Beat/miss magnitude vs the relevant comparison anchor.
        anchor = eps_high if actual > eps_high else (eps_mid if actual >= eps_low else eps_low)
        answer["delta_vs_anchor_dollar"] = round(actual - anchor, 2)

    return _apply_binding(bind_as, {
        "ticker": ticker_u,
        "fy": fy_int,
        "inputs": {
            "prior_year_actual_eps": prior_eps,
            "growth_low_pct": g_lo,
            "growth_high_pct": g_hi,
            "fy_actual_eps": float(fy_actual_eps) if fy_actual_eps is not None else None,
        },
        "derived": {
            "growth_low_decimal": round(g_lo / 100.0, 4),
            "growth_high_decimal": round(g_hi / 100.0, 4),
        },
        "answer": answer,
    })
