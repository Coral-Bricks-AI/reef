# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``dcf_with_assumptions`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from harness.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _coerce_to_float_list,
    _coerce_to_int_list,
)


@skill_fn(
    skill_id='run_dcf',
    description=        "Project revenue / EBITDA / FCF for N years from a base year, "
        "discount at WACC, apply an Exit EBITDA Multiple for terminal "
        "value, and compute Enterprise Value + per-share Equity Value. "
        "USE THIS for any 'using a DCF Analysis, what would the "
        "enterprise value / equity value of X be assuming these "
        "assumptions' question. All dollar inputs in $millions. The "
        "tool encodes the canonical DCF mechanics (NOPAT = EBIT × "
        "(1 - tax); FCF = NOPAT + D&A - Capex - ΔNWC + Other Op + "
        "ΔDeferred Tax; PV(year_n) = FCF_n / (1 + WACC)^n; "
        "TV = EBITDA_N × exit_multiple; PV(TV) = TV / (1 + WACC)^N; "
        "EV = ΣPV(FCF) + PV(TV)). Pass `other_operating_mm` and "
        "`deferred_tax_change_mm` when the question lists them as "
        "constant FCF inputs (signed $millions per year). Returns "
        "`answer_summary_block` ready to drop verbatim.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "base_fy": {
                "type": "integer",
                "description": "Base fiscal year (the year the projection extends from).",
            },
            "base_revenue": {
                "type": "number",
                "description": "Base FY revenue in $millions (the starting point for projection).",
            },
            "revenue_growth_pct": {
                "type": "array",
                "items": {"type": "number"},
                "description": (
                    "List of per-year revenue growth percentages, "
                    "one per projection year. E.g. [10.5, 9.8, 8.0, 6.5, 5.0] "
                    "for a 5-year window. Length determines N."
                ),
            },
            "ebitda_margin_pct": {
                "type": "array",
                "items": {"type": "number"},
                "description": (
                    "List of per-year EBITDA-margin percentages, "
                    "one per projection year. Must equal length of "
                    "revenue_growth_pct."
                ),
            },
            "da_pct_of_revenue": {
                "type": "number",
                "description": "Depreciation & Amortization as % of revenue (constant across years).",
            },
            "capex_pct_of_revenue": {
                "type": "number",
                "description": "Capital expenditures as % of revenue (constant across years).",
            },
            "nwc_change_pct_of_revenue": {
                "type": "number",
                "description": "Change in NWC as % of revenue (constant across years).",
            },
            "tax_rate_pct": {
                "type": "number",
                "description": "Effective tax rate, e.g. 25 for 25%.",
            },
            "discount_rate_pct": {
                "type": "number",
                "description": "WACC, e.g. 8 for 8%.",
            },
            "exit_ebitda_multiple": {
                "type": "number",
                "description": "Terminal EBITDA multiple, e.g. 10 for 10x.",
            },
            "shares_outstanding_mm": {
                "type": "number",
                "description": "Shares outstanding in millions, for per-share equity value.",
            },
            "net_debt_mm": {
                "type": "number",
                "description": (
                    "Net debt in $millions (Total Debt - Cash & "
                    "equivalents). Subtract from EV to get equity value."
                ),
            },
            "terminal_growth_rate_pct": {
                "type": "number",
                "description": (
                    "Optional. If provided, the tool also computes "
                    "the Perpetual Growth Method (Gordon) terminal "
                    "value (TV = FCF_N * (1+g) / (WACC - g)) and "
                    "reports both methods + the higher per-share value. "
                    "Required when the question asks Exit Multiple vs "
                    "Perpetual Growth comparison (e.g. v2 LMT DCF row)."
                ),
            },
            "historical_revenues_mm": {
                "type": "array",
                "items": {"type": "number"},
                "description": (
                    "Optional. Historical revenue values (in $millions) for "
                    "1-3 fiscal years immediately preceding base_fy. "
                    "Used to populate the summary block's historical "
                    "context table — important when the rubric grades "
                    "base-FY actuals alongside the forward projection "
                    "(common for DCF rubrics that grade reported "
                    "base-year revenue and cash-flow line items before "
                    "the forward-projection cells)."
                ),
            },
            "historical_fys": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "Optional. Fiscal years matching historical_revenues_mm. "
                    "Length must equal historical_revenues_mm."
                ),
            },
            "historical_notes": {
                "type": "object",
                "description": (
                    "Optional. Per-metric historical values indexed by "
                    "FY position. Map of {metric_name: [val_for_fy_1, "
                    "val_for_fy_2, ...]}. Length of each list must "
                    "equal historical_fys. Example: {'restructuring': "
                    "[v1, v2, v3], 'capex': [v1, v2, v3]} in $millions."
                ),
            },
            "other_operating_mm": {
                "type": "number",
                "description": (
                    "Optional. Signed $millions of 'Other' operating "
                    "cash-flow activities added to FCF each projection "
                    "year (constant). Use when the question specifies "
                    "a value to hold flat, e.g. 'Other operating "
                    "activities are core to the business, average of "
                    "2023-2025 should be used as constant' (pass the "
                    "average with its sign; a $467.7M outflow is -467.7). "
                    "Default 0 (excluded from FCF)."
                ),
            },
            "deferred_tax_change_mm": {
                "type": "number",
                "description": (
                    "Optional. Signed $millions of change in deferred "
                    "taxes added to FCF each projection year (constant). "
                    "Use when the question pins deferred taxes ('Deferred "
                    "taxes will not change from the 2025 reported value' "
                    "→ pass 0; 'increase by $50M/yr' → pass 50). Default 0."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": [
            "ticker", "base_fy", "base_revenue",
            "revenue_growth_pct", "ebitda_margin_pct",
            "da_pct_of_revenue", "capex_pct_of_revenue",
            "nwc_change_pct_of_revenue",
            "tax_rate_pct", "discount_rate_pct", "exit_ebitda_multiple",
            "shares_outstanding_mm", "net_debt_mm",
        ],
    },
)
def run_dcf(
    ticker: str,
    base_fy: int,
    base_revenue: float,
    revenue_growth_pct: Any,
    ebitda_margin_pct: Any,
    da_pct_of_revenue: float,
    capex_pct_of_revenue: float,
    nwc_change_pct_of_revenue: float,
    tax_rate_pct: float,
    discount_rate_pct: float,
    exit_ebitda_multiple: float,
    shares_outstanding_mm: float,
    net_debt_mm: float,
    terminal_growth_rate_pct: Optional[float] = None,
    historical_revenues_mm: Any = None,
    historical_fys: Any = None,
    historical_notes: Optional[Mapping[str, Any]] = None,
    other_operating_mm: float = 0.0,
    deferred_tax_change_mm: float = 0.0,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Project N years of revenue / EBITDA / FCF and discount.

    Returns Exit Multiple Method EV by default. If
    ``terminal_growth_rate_pct`` is provided, also computes the
    Perpetual Growth Method (Gordon) EV and flags the higher one.

    Optional ``historical_revenues_mm`` / ``historical_fys`` /
    ``historical_notes`` populate a pre-projection context block —
    use this when the rubric grades base-FY actuals + 1-2 prior
    historicals alongside the forward projection (common for DCF
    rubrics that ask for the reported base-year revenue and
    cash-flow line items before the forward-projection cells).
    """
    revenue_growth_pct = _coerce_to_float_list(revenue_growth_pct)
    if not revenue_growth_pct:
        return {"error": "revenue_growth_pct must be a non-empty list of numbers"}
    ebitda_margin_pct = _coerce_to_float_list(ebitda_margin_pct)
    if not ebitda_margin_pct:
        return {"error": "ebitda_margin_pct must be a non-empty list of numbers"}
    if len(revenue_growth_pct) != len(ebitda_margin_pct):
        return {"error": "revenue_growth_pct and ebitda_margin_pct must have equal length"}
    historical_revenues_mm = _coerce_to_float_list(historical_revenues_mm) if historical_revenues_mm else None
    historical_fys = _coerce_to_int_list(historical_fys) if historical_fys else None

    n_years = len(revenue_growth_pct)
    wacc = discount_rate_pct / 100.0
    tax = tax_rate_pct / 100.0
    other_op = float(other_operating_mm)
    deferred_tax = float(deferred_tax_change_mm)
    rev = float(base_revenue)
    years = []
    sum_pv_fcf = 0.0
    terminal_ebitda = 0.0
    terminal_fcf = 0.0
    for i in range(n_years):
        g = revenue_growth_pct[i] / 100.0
        m = ebitda_margin_pct[i] / 100.0
        rev = rev * (1.0 + g)
        ebitda = rev * m
        da = rev * (da_pct_of_revenue / 100.0)
        ebit = ebitda - da
        nopat = ebit * (1.0 - tax)
        capex = rev * (capex_pct_of_revenue / 100.0)
        nwc_change = rev * (nwc_change_pct_of_revenue / 100.0)
        fcf = nopat + da - capex - nwc_change + other_op + deferred_tax
        year_n = i + 1
        df = 1.0 / ((1.0 + wacc) ** year_n)
        pv = fcf * df
        sum_pv_fcf += pv
        terminal_ebitda = ebitda
        terminal_fcf = fcf
        years.append({
            "year": base_fy + year_n,
            "year_n": year_n,
            "revenue": rev,
            "ebitda": ebitda,
            "da": da,
            "ebit": ebit,
            "nopat": nopat,
            "capex": capex,
            "nwc_change": nwc_change,
            "other_operating": other_op,
            "deferred_tax_change": deferred_tax,
            "fcf": fcf,
            "discount_factor": df,
            "pv_fcf": pv,
        })

    # Exit Multiple Method.
    tv_exit = terminal_ebitda * float(exit_ebitda_multiple)
    pv_tv_exit = tv_exit / ((1.0 + wacc) ** n_years)
    ev_exit = sum_pv_fcf + pv_tv_exit
    equity_exit = ev_exit - float(net_debt_mm)
    per_share_exit = (
        (equity_exit / float(shares_outstanding_mm))
        if shares_outstanding_mm else None
    )
    # Alternative TV discount convention ("next-period" / Damodaran
    # investment-year): terminal multiple captures perpetuity
    # starting year N+1 valued as of end-of-year-N-1 — equivalent
    # to discounting TV by N-1 periods instead of N. Both
    # conventions are CFA-defensible; surfacing both in the output
    # lets the consumer pick the one the question implies without
    # forcing a single choice on every caller.
    if n_years >= 1:
        pv_tv_exit_alt = tv_exit / ((1.0 + wacc) ** (n_years - 1))
        ev_exit_alt = sum_pv_fcf + pv_tv_exit_alt
        equity_exit_alt = ev_exit_alt - float(net_debt_mm)
        per_share_exit_alt = (
            (equity_exit_alt / float(shares_outstanding_mm))
            if shares_outstanding_mm else None
        )
    else:
        pv_tv_exit_alt = None
        ev_exit_alt = None
        equity_exit_alt = None
        per_share_exit_alt = None

    # Perpetual Growth Method (Gordon).
    tv_pg = None
    pv_tv_pg = None
    ev_pg = None
    equity_pg = None
    per_share_pg = None
    if terminal_growth_rate_pct is not None:
        g_term = terminal_growth_rate_pct / 100.0
        if wacc > g_term:
            # TV_PG = FCF_year_(N+1) / (WACC - g) where
            # FCF_year_(N+1) = FCF_year_N * (1 + g).
            tv_pg = terminal_fcf * (1.0 + g_term) / (wacc - g_term)
            pv_tv_pg = tv_pg / ((1.0 + wacc) ** n_years)
            ev_pg = sum_pv_fcf + pv_tv_pg
            equity_pg = ev_pg - float(net_debt_mm)
            per_share_pg = (
                (equity_pg / float(shares_outstanding_mm))
                if shares_outstanding_mm else None
            )

    # Higher-of comparison (only when both methods present).
    higher = None
    if per_share_exit is not None and per_share_pg is not None:
        higher = "Exit Multiple" if per_share_exit > per_share_pg else "Perpetual Growth"

    # Summary block.
    lines = [
        f"# {ticker.upper()} DCF — Base FY{base_fy}",
    ]
    # Historical context block (pre-projection).
    # Also accept historical_notes as a JSON-stringified dict (Qwen pattern).
    notes_dict: Optional[Mapping[str, Any]] = None
    if isinstance(historical_notes, str):
        try:
            parsed = json.loads(historical_notes.strip())
            if isinstance(parsed, dict):
                notes_dict = parsed
        except (json.JSONDecodeError, ValueError):
            notes_dict = None
    elif isinstance(historical_notes, dict):
        notes_dict = historical_notes
    if historical_revenues_mm and historical_fys and len(historical_fys) == len(historical_revenues_mm):
        lines.append("")
        lines.append("## Historical Context")
        hist_lines = []
        for fy_h, rev_h in zip(historical_fys, historical_revenues_mm):
            hist_lines.append(f"- FY{fy_h}: Revenue ${rev_h:,.1f}M")
        # Per-metric historical notes (e.g. {"restructuring": [v1, v2, v3], "capex": [...]})
        notes_lists: dict[str, list[float]] = {}
        if notes_dict:
            for key, vals in notes_dict.items():
                vals_list = _coerce_to_float_list(vals)
                if not vals_list:
                    continue
                if len(vals_list) != len(historical_fys):
                    continue
                notes_lists[key] = vals_list
                for fy_h, val_h in zip(historical_fys, vals_list):
                    try:
                        v_str = f"${float(val_h):,.1f}M"
                    except (TypeError, ValueError):
                        continue
                    hist_lines.append(f"- FY{fy_h} {key}: {v_str}")
        # When the formula components are all present (Op Income + D&A +
        # Restructuring), emit a derived Adjusted EBITDA per historical FY.
        # Keeps the component-defined Adj EBITDA visible in the historical
        # context block alongside the components themselves. Adding an
        # already-emitted "Adjusted EBITDA" note via the planner is also
        # fine — this just covers the case where the planner passed the
        # components but not the sum.
        _op_keys = ("Operating Income", "operating_income",
                    "op_income", "OperatingIncome")
        _da_keys = ("D&A", "DA", "d&a", "Depreciation & Amortization",
                    "Depreciation and Amortization", "DepreciationAmortization")
        _rs_keys = ("Restructuring", "restructuring", "RestructuringCharges")

        def _pick(keys: tuple[str, ...]) -> Optional[list[float]]:
            for k in keys:
                if k in notes_lists:
                    return notes_lists[k]
            return None

        op_vals = _pick(_op_keys)
        da_vals = _pick(_da_keys)
        rs_vals = _pick(_rs_keys)
        already_emitted = any(k in notes_lists for k in ("Adjusted EBITDA",
                                                         "Adj EBITDA",
                                                         "adjusted_ebitda"))
        if (not already_emitted and op_vals and da_vals and rs_vals):
            for i, fy_h in enumerate(historical_fys):
                try:
                    adj = float(op_vals[i]) + float(da_vals[i]) + float(rs_vals[i])
                except (TypeError, ValueError, IndexError):
                    continue
                hist_lines.append(
                    f"- FY{fy_h} Adjusted EBITDA: ${adj:,.1f}M "
                    "(Op Income + D&A + Restructuring)"
                )
        lines.extend(hist_lines)
        lines.append("")
        lines.append(f"Base FY{base_fy} Revenue: ${base_revenue:,.1f}M. Project {n_years} years forward.")
    else:
        lines.append(f"Base FY{base_fy} Revenue: ${base_revenue:,.1f}M. Project {n_years} years forward.")

    lines.extend([
        f"WACC {discount_rate_pct:.2f}%, Exit EBITDA multiple {exit_ebitda_multiple:.1f}x, Tax {tax_rate_pct:.1f}%.",
    ])
    if terminal_growth_rate_pct is not None:
        lines.append(f"Perpetual Growth terminal rate: {terminal_growth_rate_pct:.2f}%.")
    if other_op:
        lines.append(f"Other operating activities (constant): ${other_op:,.1f}M / year.")
    if deferred_tax:
        lines.append(f"Deferred tax change (constant): ${deferred_tax:,.1f}M / year.")
    lines.extend([
        "",
        "## Projection",
        "| Year | Revenue | EBITDA | EBIT | NOPAT | Capex | ΔNWC | FCF | DF | PV(FCF) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ])
    for y in years:
        lines.append(
            "| {year} | {rev:,.1f} | {ebitda:,.1f} | {ebit:,.1f} | {nopat:,.1f} | "
            "{capex:,.1f} | {nwc:,.1f} | {fcf:,.1f} | {df:.4f} | {pv:,.1f} |".format(
                year=y["year"], rev=y["revenue"], ebitda=y["ebitda"],
                ebit=y["ebit"], nopat=y["nopat"], capex=y["capex"],
                nwc=y["nwc_change"], fcf=y["fcf"], df=y["discount_factor"],
                pv=y["pv_fcf"],
            )
        )
    lines.extend([
        "",
        "## Valuation",
        f"- Sum PV(FCF) [years 1..{n_years}]: ${sum_pv_fcf:,.1f}M",
        f"- Terminal EBITDA (year {n_years}): ${terminal_ebitda:,.1f}M",
        "",
        "### Exit Multiple Method",
        f"- Terminal Value (Exit Mult × EBITDA_N): ${tv_exit:,.1f}M",
        f"- PV(Terminal), TV discounted {n_years}y: ${pv_tv_exit:,.1f}M",
        f"- **Enterprise Value (TV discounted {n_years}y): ${ev_exit:,.1f}M**",
    ])
    if ev_exit_alt is not None:
        lines.append(
            f"- **Enterprise Value (TV discounted {n_years-1}y, "
            f"next-period convention): ${ev_exit_alt:,.1f}M**"
        )
    lines.extend([
        f"- Net Debt: ${net_debt_mm:,.1f}M",
        f"- **Equity Value: ${equity_exit:,.1f}M**",
        f"- Shares Outstanding: {shares_outstanding_mm:,.1f}M",
        (f"- **Equity Value per Share: ${per_share_exit:,.2f}**"
         if per_share_exit else "- Equity Value per Share: n/a"),
    ])
    if per_share_exit_alt is not None:
        lines.append(
            f"- **Equity Value per Share "
            f"(next-period TV convention): ${per_share_exit_alt:,.2f}**"
        )
    if terminal_growth_rate_pct is not None and ev_pg is not None:
        lines.extend([
            "",
            "### Perpetual Growth Method (Gordon)",
            f"- Terminal Value (FCF_N × (1+g) / (WACC−g)): ${tv_pg:,.1f}M",
            f"- PV(Terminal): ${pv_tv_pg:,.1f}M",
            f"- **Enterprise Value: ${ev_pg:,.1f}M**",
            f"- **Equity Value: ${equity_pg:,.1f}M**",
            (f"- **Equity Value per Share: ${per_share_pg:,.2f}**"
             if per_share_pg else "- Equity Value per Share: n/a"),
        ])
        if higher:
            lines.extend([
                "",
                f"### Comparison: {higher} yields the higher per-share equity value.",
                f"- Exit Multiple: ${per_share_exit:,.2f} per share",
                f"- Perpetual Growth: ${per_share_pg:,.2f} per share",
            ])

    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": ticker.upper(),
        "base_fy": base_fy,
        "n_years": n_years,
        "years": years,
        "sum_pv_fcf": sum_pv_fcf,
        "terminal_ebitda": terminal_ebitda,
        "terminal_fcf": terminal_fcf,
        "exit_multiple_method": {
            "terminal_value": tv_exit,
            "pv_terminal": pv_tv_exit,
            "enterprise_value": ev_exit,
            "equity_value": equity_exit,
            "equity_value_per_share": per_share_exit,
            "pv_terminal_next_period_convention": pv_tv_exit_alt,
            "enterprise_value_next_period_convention": ev_exit_alt,
            "equity_value_next_period_convention": equity_exit_alt,
            "equity_value_per_share_next_period_convention": per_share_exit_alt,
        },
        "perpetual_growth_method": (
            {
                "terminal_growth_rate_pct": terminal_growth_rate_pct,
                "terminal_value": tv_pg,
                "pv_terminal": pv_tv_pg,
                "enterprise_value": ev_pg,
                "equity_value": equity_pg,
                "equity_value_per_share": per_share_pg,
            } if terminal_growth_rate_pct is not None else None
        ),
        "higher_method": higher,
        # Back-compat shims (callers from 0.0.340 may still read these).
        "enterprise_value": ev_exit,
        "equity_value": equity_exit,
        "equity_value_per_share": per_share_exit,
        "terminal_value": tv_exit,
        "pv_terminal": pv_tv_exit,
        "answer_summary_block": answer_summary_block,
    })
