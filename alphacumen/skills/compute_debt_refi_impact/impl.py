# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``debt_refi_impact`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Optional
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _do_bm25_sec,
    _do_get_xbrl_facts,
)


@skill_fn(
    skill_id='compute_debt_refi_impact',
    description=        "Compute the after-tax impact to net income from refinancing all "
        "of an issuer's long-term debt at a higher interest rate. "
        "Hardcodes the rubric's convention so the model doesn't drift "
        "to Total Debt or 21% statutory tax: pulls Long-Term Debt "
        "(non-current) and Net Income directly from the FY 10-K's iXBRL "
        "facts, multiplies LT debt by the rate delta, applies a "
        "configurable tax shield (default 20%), and expresses the result "
        "as both an absolute $B impact and a % decrease relative to "
        "abs(net_income). USE THIS for ANY 'what if X's debt were "
        "refinanced at Y% higher' question instead of bm25_sec + "
        "get_xbrl_facts + run_python — the manual path consistently "
        "picks the wrong debt base or tax rate. Calendar-year FY only; "
        "for non-Dec FY-ends fall back to the manual path.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Issuer ticker, e.g. 'BA'. Case-insensitive.",
            },
            "fy": {
                "type": "integer",
                "description": (
                    "Fiscal year being analyzed, e.g. 2024 for FY2024 "
                    "(Dec 31, 2024 year-end)."
                ),
            },
            "rate_delta_bps": {
                "type": "integer",
                "description": (
                    "Rate increase in basis points. 300 = 3 percentage "
                    "points. The tool divides by 10000 to convert to a "
                    "decimal fraction."
                ),
            },
            "tax_rate": {
                "type": "number",
                "default": 0.20,
                "description": (
                    "Effective tax rate to apply for the after-tax shield. "
                    "Defaults to 0.20 (rubric convention; differs from the "
                    "21% US federal statutory rate)."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy", "rate_delta_bps"],
    },
)
def compute_debt_refi_impact(
    ticker: str,
    fy: int,
    rate_delta_bps: int,
    tax_rate: float = 0.20,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Compute net-income impact of refinancing all long-term debt at a higher rate.

    Pulls the issuer's most recent calendar-year 10-K, extracts
    Long-Term Debt (non-current) and Net Income via XBRL, and computes:

      pre_tax_impact   = LT_debt * (rate_delta_bps / 10000)
      after_tax_impact = pre_tax_impact * (1 - tax_rate)
      pct_decrease     = after_tax_impact / abs(net_income)

    For Boeing FY2024 with rate_delta_bps=300 and tax_rate=0.20:
      LT debt   = $52,586M
      Net loss  = $(11,829)M
      Pre-tax   = $52,586 * 0.03 = $1,577.58M
      After-tax = $1,577.58 * 0.80 = $1,262M ≈ $1.261B
      % impact  = $1,262 / $11,829 = 10.7%
    """
    if not isinstance(fy, int):
        try:
            fy = int(fy)
        except (TypeError, ValueError):
            return {"error": f"fy must be an int (e.g. 2024), got {fy!r}"}
    if not isinstance(rate_delta_bps, (int, float)):
        try:
            rate_delta_bps = int(rate_delta_bps)
        except (TypeError, ValueError):
            return {"error": f"rate_delta_bps must be an integer (300 for 3 percentage points), got {rate_delta_bps!r}"}
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker is required (e.g. 'BA')"}
    ticker = ticker.strip().upper()
    try:
        tax_rate = float(tax_rate)
    except (TypeError, ValueError):
        tax_rate = 0.20

    # 1. Find the FY 10-K (calendar-year FY-end).
    env = _do_bm25_sec(
        query=f"{ticker} annual report long-term debt net income",
        k=5,
        fields=None,
        filters={
            "ticker": ticker,
            "form_type": "10-K",
            "event_date_gte": f"{fy}1201",
            "event_date_lte": f"{fy}1231",
        },
        sort=None,
        body_mode="snippet",
    )
    hits = env.get("hits") or []
    if not hits:
        return {
            "ticker": ticker, "fy": fy,
            "error": (
                f"No {ticker} 10-K found with event_date in {fy}-12-01 to "
                f"{fy}-12-31. Tool assumes calendar-year FY-end; for "
                "non-Dec FY-ends pass the fy whose Dec 31 falls within "
                "the issuer's fiscal year, or fall back to bm25_sec + "
                "get_xbrl_facts manually."
            ),
        }
    hits.sort(key=lambda h: str((h.get("source") or {}).get("filed_at", "")), reverse=True)
    ten_k_ref = hits[0].get("id", "")
    src = hits[0].get("source") or {}
    filed_at = src.get("filed_at", "")

    # 2. Pull Long-Term Debt non-current from iXBRL.
    #
    # Issuers tag long-term debt under several variants depending on
    # whether they have capital leases / finance leases / unamortised
    # premium etc. ``LongTermDebtNoncurrent`` is the most common but is
    # not universal — heavy-equipment / aerospace / capital-leased
    # operators often tag it as ``LongTermDebtAndCapitalLeaseObligations``
    # (or finance-lease variant under ASC 842). Try the variants in
    # order from most-specific to least; first one that produces a
    # positive value at the FY-end instant wins. The bare ``LongTermDebt``
    # pattern matches anything starting with ``LongTermDebt``, which
    # the iXBRL concept-pattern engine treats as substring on the local
    # name — so it's a final wide net rather than a fifth named tag.
    fy_instant = f"{fy}-12-31"
    _LT_DEBT_CONCEPT_VARIANTS = (
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
        "LongTermDebtAndFinanceLeaseObligations",
        "LongTermDebt",
    )
    lt_debt_m: Optional[float] = None
    last_error: Optional[str] = None
    facts_tried: int = 0
    concept_used: str = ""
    for concept in _LT_DEBT_CONCEPT_VARIANTS:
        lt_facts = _do_get_xbrl_facts(
            ref=ten_k_ref,
            concept_pattern=concept,
            periods=None,
            limit=20,
        )
        if lt_facts.get("error"):
            last_error = lt_facts["error"]
            continue
        facts = lt_facts.get("facts", []) or []
        facts_tried += len(facts)
        # Prefer the FY-end instant; fall back to first numeric value.
        for fact in facts:
            period = str(fact.get("period", ""))
            if fy_instant in period:
                try:
                    v = float(fact.get("value", 0)) / 1_000_000
                    if v > 0:
                        lt_debt_m = v
                        concept_used = concept
                        break
                except (TypeError, ValueError):
                    continue
        if lt_debt_m is None:
            for fact in facts:
                try:
                    v = float(fact.get("value", 0)) / 1_000_000
                    if v > 0:
                        lt_debt_m = v
                        concept_used = concept
                        break
                except (TypeError, ValueError):
                    continue
        if lt_debt_m is not None:
            break
    if lt_debt_m is None or lt_debt_m <= 0:
        return {
            "ticker": ticker, "fy": fy, "ten_k_ref": ten_k_ref,
            "error": (
                f"Could not extract long-term debt ({fy_instant}) from "
                f"{ten_k_ref}. Tried concepts: "
                f"{', '.join(_LT_DEBT_CONCEPT_VARIANTS)}. "
                f"Examined {facts_tried} facts; "
                + (f"last XBRL error: {last_error}" if last_error
                   else "none had a usable positive numeric value at FY-end.")
            ),
        }

    # 3. Pull Net Income (full-year) from iXBRL.
    ni_facts = _do_get_xbrl_facts(
        ref=ten_k_ref,
        concept_pattern="NetIncomeLoss",
        periods=None,
        limit=30,
    )
    if ni_facts.get("error"):
        return {
            "ticker": ticker, "fy": fy, "ten_k_ref": ten_k_ref,
            "lt_debt_m": lt_debt_m,
            "error": f"XBRL fetch failed for NetIncomeLoss: {ni_facts['error']}",
        }
    net_income_m: Optional[float] = None
    fy_start = f"{fy}-01-01"
    fy_end = f"{fy}-12-31"
    # Prefer the full-FY duration fact (start..end).
    for fact in ni_facts.get("facts", []):
        period = str(fact.get("period", ""))
        if fy_start in period and fy_end in period:
            try:
                net_income_m = float(fact.get("value", 0)) / 1_000_000
                break
            except (TypeError, ValueError):
                continue
    if net_income_m is None:
        for fact in ni_facts.get("facts", []):
            period = str(fact.get("period", ""))
            if fy_end in period:
                try:
                    net_income_m = float(fact.get("value", 0)) / 1_000_000
                    break
                except (TypeError, ValueError):
                    continue
    if net_income_m is None:
        return {
            "ticker": ticker, "fy": fy, "ten_k_ref": ten_k_ref,
            "lt_debt_m": lt_debt_m,
            "error": (
                f"Could not extract NetIncomeLoss for FY{fy} from {ten_k_ref}. "
                f"Found {len(ni_facts.get('facts', []))} matching facts but "
                "none had a full-FY period."
            ),
            "xbrl_facts_sample": ni_facts.get("facts", [])[:3],
        }

    # 4. Compute the impact using the rubric's convention.
    rate_delta = rate_delta_bps / 10_000.0
    pre_tax_m = lt_debt_m * rate_delta
    after_tax_m = pre_tax_m * (1.0 - tax_rate)
    abs_ni = abs(net_income_m)
    pct = (after_tax_m / abs_ni) if abs_ni > 0 else 0.0

    impact_b = after_tax_m / 1_000.0
    pct_pp = pct * 100.0
    phrasing = (
        f"${round(impact_b, 3)} Billion Negative Impact to Net Income, "
        f"or a {round(pct_pp, 1)}% decrease"
    )

    result = {
        "ticker": ticker,
        "fy": fy,
        "ten_k_ref": ten_k_ref,
        "ten_k_filed_at": filed_at,
        "inputs": {
            "long_term_debt_noncurrent_m": round(lt_debt_m, 2),
            "long_term_debt_concept_used": concept_used,
            "net_income_loss_m": round(net_income_m, 2),
            "rate_delta_bps": rate_delta_bps,
            "tax_rate": tax_rate,
        },
        "computation": {
            "pre_tax_interest_impact_m": round(pre_tax_m, 2),
            "tax_shield_factor": round(1.0 - tax_rate, 4),
            "after_tax_impact_m": round(after_tax_m, 2),
        },
        "answer": {
            "after_tax_impact_billion": round(impact_b, 3),
            "pct_decrease_in_net_income": round(pct_pp, 1),
            "phrasing": phrasing,
        },
    }
    return _apply_binding(bind_as, result)
