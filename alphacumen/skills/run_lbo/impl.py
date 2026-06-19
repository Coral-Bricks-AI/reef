# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``lbo_take_private`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Optional
from harness.skill_fn import skill_fn
from alphacumen.tools import _BIND_AS_PARAM_SCHEMA, _apply_binding, _do_get_equity_bars


@skill_fn(
    skill_id='run_lbo',
    description=        "Run a simplified LBO model for a take-private + return canonical "
        "sources/uses, year-by-year projection (avg-balance interest "
        "expense + income via fixed-point iteration), exit EV, exit "
        "equity value, IRR, and MOIC. All dollar inputs in $millions. "
        "Mirrors run_dcf in shape — assumption-driven. The model should "
        "supply basic WASO + base revenue + base margin from filings; "
        "for the recent share price, pass `asof_date=<YYYY-MM-DD>` and "
        "the tool will fetch the close from get_equity_bars internally "
        "(safer than passing a placeholder). Use for any 'simplified "
        "LBO / illustrative take-private' question with explicit premium "
        "/ leverage / hold / exit-multiple assumptions. Quote "
        "`answer_summary_block` verbatim.",
    parameters=               {
        "type": "object",
        "properties": {
            "target_ticker": {"type": "string"},
            "asof_date": {
                "type": "string",
                "description": (
                    "Optional YYYY-MM-DD date to fetch the recent share "
                    "close from get_equity_bars (safer than passing "
                    "recent_close directly). If supplied, the tool snaps "
                    "to the most-recent trading close on or before this "
                    "date. STRONGLY PREFERRED over recent_close — the "
                    "model has historically used round-number placeholders "
                    "like $200 instead of fetching the actual close, "
                    "which materially miscalculates sponsor equity."
                ),
            },
            "recent_close": {
                "type": "number",
                "description": (
                    "Optional: recent share price in $. PREFER passing "
                    "asof_date instead — if you supply both and the "
                    "model-passed value differs from the equity_bars "
                    "close by >20%, the tool uses the fetched value."
                ),
            },
            "offer_premium_pct": {"type": "number", "description": "Offer premium percent (e.g. 30 for 30%)."},
            "shares_outstanding_mm": {"type": "number", "description": "Basic weighted-avg shares outstanding in millions."},
            "base_revenue_mm": {"type": "number", "description": "Base FY revenue in $M."},
            "base_ebitda_margin_pct": {"type": "number", "description": "Base FY EBITDA margin percent (e.g. 17.5 for 17.5%)."},
            "revenue_growth_pct_per_year": {"type": "number", "description": "Revenue growth per year percent (e.g. 8 for 8%)."},
            "ebitda_margin_expansion_bps_per_year": {"type": "number", "description": "EBITDA margin expansion per year in BPS (e.g. 100 for 1pp/year)."},
            "da_pct_of_revenue": {"type": "number", "description": "Depreciation & Amortization as % of revenue."},
            "capex_pct_of_revenue": {"type": "number", "description": "CapEx as % of revenue."},
            "nwc_change_pct_of_revenue": {"type": "number", "description": "Change in NWC as % of revenue (0 if neutral)."},
            "tax_rate_pct": {"type": "number", "description": "Effective tax rate percent (e.g. 25 for 25%)."},
            "leverage_multiple_x": {"type": "number", "description": "Sponsor leverage multiple in x EBITDA (e.g. 5.0)."},
            "debt_interest_rate_pct": {"type": "number", "description": "Debt interest rate percent (e.g. 8 for 8%)."},
            "exit_ebitda_multiple": {"type": "number", "description": "Exit EBITDA multiple (e.g. 10 for 10x)."},
            "hold_years": {"type": "integer", "description": "Hold period in years (e.g. 5)."},
            "transaction_expenses_mm": {"type": "number", "description": "Transaction expenses in $M (default 0)."},
            "refinance_debt_mm": {"type": "number", "description": "Existing debt being refinanced in $M (default 0)."},
            "cash_balance_min_mm": {"type": "number", "description": "Minimum cash balance to maintain in $M (default 0)."},
            "cash_interest_rate_pct": {"type": "number", "description": "Interest rate earned on cash balance percent (default 0)."},
            "starting_cash_mm": {"type": "number", "description": "Cash on the balance sheet at close in $M (default 0)."},
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": [
            "target_ticker", "offer_premium_pct",
            "shares_outstanding_mm", "base_revenue_mm",
            "base_ebitda_margin_pct", "revenue_growth_pct_per_year",
            "ebitda_margin_expansion_bps_per_year",
            "da_pct_of_revenue", "capex_pct_of_revenue",
            "nwc_change_pct_of_revenue", "tax_rate_pct",
            "leverage_multiple_x", "debt_interest_rate_pct",
            "exit_ebitda_multiple", "hold_years",
        ],
    },
)
def run_lbo(
    target_ticker: str,
    offer_premium_pct: float,
    shares_outstanding_mm: float,
    base_revenue_mm: float,
    base_ebitda_margin_pct: float,
    revenue_growth_pct_per_year: float,
    ebitda_margin_expansion_bps_per_year: float,
    da_pct_of_revenue: float,
    capex_pct_of_revenue: float,
    nwc_change_pct_of_revenue: float,
    tax_rate_pct: float,
    leverage_multiple_x: float,
    debt_interest_rate_pct: float,
    exit_ebitda_multiple: float,
    hold_years: int,
    asof_date: Optional[str] = None,
    recent_close: Optional[float] = None,
    transaction_expenses_mm: float = 0.0,
    refinance_debt_mm: float = 0.0,
    cash_balance_min_mm: float = 0.0,
    cash_interest_rate_pct: float = 0.0,
    starting_cash_mm: float = 0.0,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Run a simplified LBO + return sources/uses, projection, IRR, MOIC.

    All dollar inputs in $ millions. Year-by-year projection uses
    avg-balance interest expense + income via fixed-point iteration
    (matches the v2 row-25 rubric convention).
    """
    if shares_outstanding_mm <= 0 or base_revenue_mm <= 0:
        return {"error": "shares_outstanding_mm and base_revenue_mm must be positive"}
    if hold_years < 1:
        return {"error": "hold_years must be >= 1"}

    # Auto-fetch recent_close from equity_bars if not supplied (or if
    # the model passed a suspiciously round-number placeholder like
    # exactly $100 / $150 / $200). v2 row 25 EPAM diagnostic: Qwen
    # passed recent_close=200.0 vs actual 2/27/26 close $141.00,
    # inflating sponsor equity from $6.3B to $10.9B. With asof_date,
    # the tool snaps to the most-recent trading close in equity_bars.
    fetched_close: Optional[float] = None
    fetched_date: Optional[str] = None
    if asof_date:
        from datetime import datetime, timedelta
        try:
            asof_dt = datetime.fromisoformat(asof_date[:10])
            start = (asof_dt - timedelta(days=10)).strftime("%Y-%m-%d")
            end = asof_date[:10]
            bars_env = _do_get_equity_bars(target_ticker, start=start, end=end)
            rows = bars_env.get("rows") or []
            for row in rows[::-1]:
                d_str = str(row.get("date") or "")[:10]
                if d_str <= end:
                    try:
                        c = float(row.get("close") or 0)
                    except (TypeError, ValueError):
                        continue
                    if c > 0:
                        fetched_close = c
                        fetched_date = d_str
                        break
        except Exception:  # noqa: BLE001
            fetched_close = None
    chosen_close = recent_close
    close_note = ""
    if fetched_close is not None:
        if recent_close is None:
            chosen_close = fetched_close
            close_note = f" (fetched from equity_bars: {fetched_date} close ${fetched_close:.2f})"
        else:
            # Sanity check: model-supplied price within 20% of fetched.
            if recent_close > 0 and abs(recent_close - fetched_close) / fetched_close > 0.20:
                chosen_close = fetched_close
                close_note = (
                    f" (model-supplied ${recent_close:.2f} differs >20% "
                    f"from equity_bars ${fetched_close:.2f}; using fetched value)"
                )
    if chosen_close is None or chosen_close <= 0:
        return {
            "error": (
                "recent_close not supplied and equity_bars lookup failed. "
                "Either pass recent_close=<$> directly or pass asof_date "
                "(YYYY-MM-DD) so the tool can fetch from get_equity_bars."
            )
        }

    premium = offer_premium_pct / 100.0
    offer_price = float(chosen_close) * (1.0 + premium)
    equity_purchase_price = offer_price * float(shares_outstanding_mm)

    base_ebitda = base_revenue_mm * (base_ebitda_margin_pct / 100.0)
    new_debt = leverage_multiple_x * base_ebitda

    total_uses = equity_purchase_price + transaction_expenses_mm + refinance_debt_mm
    # Standard LBO sources/uses with a minimum cash floor: any
    # starting cash ABOVE the operating-minimum is "excess cash" and
    # funds the transaction as a source, reducing the sponsor equity
    # required. The remaining cash on the balance sheet at close
    # equals the stated minimum floor. Generic convention -- applies
    # to any LBO with an explicit min-cash assumption.
    excess_cash = max(0.0, float(starting_cash_mm) - float(cash_balance_min_mm))
    sponsor_equity = total_uses - new_debt - excess_cash
    # Projection starts with cash = minimum floor (excess was deployed
    # to fund the deal).
    projection_starting_cash = float(starting_cash_mm) - excess_cash

    # Project years.
    rate_debt = debt_interest_rate_pct / 100.0
    rate_cash = cash_interest_rate_pct / 100.0
    tax = tax_rate_pct / 100.0
    g = revenue_growth_pct_per_year / 100.0
    margin_step = ebitda_margin_expansion_bps_per_year / 10000.0
    da_pct = da_pct_of_revenue / 100.0
    capex_pct = capex_pct_of_revenue / 100.0
    nwc_pct = nwc_change_pct_of_revenue / 100.0

    rev = float(base_revenue_mm)
    margin = base_ebitda_margin_pct / 100.0
    debt_beg = float(new_debt)
    cash_beg = projection_starting_cash
    years: list[dict[str, Any]] = []
    for i in range(int(hold_years)):
        rev = rev * (1.0 + g)
        margin = margin + margin_step
        ebitda = rev * margin
        da = rev * da_pct
        ebit = ebitda - da
        capex = rev * capex_pct
        nwc_change = rev * nwc_pct

        # Fixed-point iteration on ending debt / ending cash.
        debt_end = debt_beg
        cash_end = cash_beg
        for _ in range(20):
            avg_debt = (debt_beg + debt_end) / 2.0
            avg_cash = (cash_beg + cash_end) / 2.0
            int_exp = avg_debt * rate_debt
            int_inc = avg_cash * rate_cash
            ebt = ebit - int_exp + int_inc
            tax_paid = max(0.0, ebt * tax)
            ni = ebt - tax_paid
            fcf = ni + da - capex - nwc_change
            # Apply FCF to debt paydown first, then build cash above
            # the minimum floor.
            paydown = max(0.0, min(fcf, debt_beg))
            new_debt_end = max(0.0, debt_beg - paydown)
            new_cash_end = cash_beg + (fcf - paydown)
            # Ensure cash floor.
            if new_cash_end < cash_balance_min_mm:
                shortfall = cash_balance_min_mm - new_cash_end
                # Borrow to cover floor (rare; only when FCF negative).
                new_cash_end = cash_balance_min_mm
                new_debt_end += shortfall
            if abs(new_debt_end - debt_end) < 0.01 and abs(new_cash_end - cash_end) < 0.01:
                debt_end = new_debt_end
                cash_end = new_cash_end
                break
            debt_end = new_debt_end
            cash_end = new_cash_end
        years.append({
            "year_n": i + 1,
            "revenue": rev,
            "ebitda_margin": margin,
            "ebitda": ebitda,
            "da": da,
            "ebit": ebit,
            "int_exp": int_exp,
            "int_inc": int_inc,
            "ebt": ebt,
            "tax_paid": tax_paid,
            "net_income": ni,
            "capex": capex,
            "nwc_change": nwc_change,
            "fcf": fcf,
            "debt_beg": debt_beg, "debt_end": debt_end,
            "cash_beg": cash_beg, "cash_end": cash_end,
        })
        debt_beg = debt_end
        cash_beg = cash_end

    exit_ebitda = years[-1]["ebitda"]
    exit_ev = exit_ebitda * float(exit_ebitda_multiple)
    exit_equity = exit_ev - debt_beg + cash_beg
    moic = exit_equity / sponsor_equity if sponsor_equity > 0 else None
    irr = None
    if moic and moic > 0:
        irr = moic ** (1.0 / float(hold_years)) - 1.0

    lines = [
        f"# {target_ticker.upper()} LBO Model",
        "",
        "## Sources & Uses",
        f"- Recent close: ${chosen_close:,.2f}{close_note}",
        f"- Offer price ({offer_premium_pct:.1f}% premium): ${offer_price:,.2f}",
        f"- Basic shares outstanding: {shares_outstanding_mm:,.2f}M",
        f"- Equity purchase price: ${equity_purchase_price:,.2f}M",
        f"- Transaction expenses: ${transaction_expenses_mm:,.2f}M",
        f"- Refinance existing debt: ${refinance_debt_mm:,.2f}M",
        f"- **Total Uses: ${total_uses:,.2f}M**",
        "",
        f"- Base FY EBITDA (Revenue × margin): ${base_ebitda:,.2f}M",
        f"- New debt ({leverage_multiple_x:.1f}× EBITDA at {debt_interest_rate_pct:.1f}%): ${new_debt:,.2f}M",
        f"- Excess cash deployed (starting cash ${starting_cash_mm:,.2f}M − min floor ${cash_balance_min_mm:,.2f}M): ${excess_cash:,.2f}M",
        f"- **Sponsor Equity: ${sponsor_equity:,.2f}M**",
        "",
        "## Projection",
        "| Yr | Revenue | EBITDA | EBIT | NI | FCF | Debt End | Cash End |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for y in years:
        lines.append(
            "| {n} | {r:,.1f} | {e:,.1f} | {ebit:,.1f} | {ni:,.1f} | {f:,.1f} | {d:,.1f} | {c:,.1f} |".format(
                n=y["year_n"], r=y["revenue"], e=y["ebitda"],
                ebit=y["ebit"], ni=y["net_income"], f=y["fcf"],
                d=y["debt_end"], c=y["cash_end"],
            )
        )
    lines.extend([
        "",
        "## Exit",
        f"- Exit EBITDA (Year {hold_years}): ${exit_ebitda:,.2f}M",
        f"- Exit EV ({exit_ebitda_multiple:.1f}× EBITDA): ${exit_ev:,.2f}M",
        f"- Less: Ending Debt: ${debt_beg:,.2f}M",
        f"- Plus: Ending Cash: ${cash_beg:,.2f}M",
        f"- **Exit Equity Value: ${exit_equity:,.2f}M**",
        "",
        f"- **MOIC: {moic:.2f}x**" if moic else "- MOIC: n/a",
        f"- **IRR: {irr * 100:.2f}%**" if irr is not None else "- IRR: n/a",
    ])
    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "target_ticker": target_ticker.upper(),
        "offer_price": offer_price,
        "equity_purchase_price": equity_purchase_price,
        "total_uses": total_uses,
        "new_debt": new_debt,
        "sponsor_equity": sponsor_equity,
        "years": years,
        "exit_ebitda": exit_ebitda,
        "exit_enterprise_value": exit_ev,
        "exit_equity_value": exit_equity,
        "moic": moic,
        "irr": irr,
        "answer_summary_block": answer_summary_block,
    })
