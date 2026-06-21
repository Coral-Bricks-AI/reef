# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``gross_margin_decomposition`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.

Generic across any issuer with two comparable periods (annual or
quarterly). Pulls Revenue + COGS via XBRL for both periods, computes
GM% and Δ GM, attributes change to revenue vs cost effects,
surfaces COGS sub-components when disclosed, and produces a
normalized GM removing the dominant driver.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _do_bm25_sec,
    _do_extract_filing_tables,
    _do_get_full_text,
    _fy_full_period_fact,
    _parse_num,
    _q_period_fact,
    _strip_chunk_suffix,
    _table_rows_from_extract,
)


def _find_filing_ref_for_period(
    ticker: str, period_iso: str, form_types: Sequence[str],
) -> Optional[tuple[str, str]]:
    """Find a filing covering the given period-end date.

    Tries each form_type in priority order; returns (ref, filed_at)
    of the first hit whose event_date sits within ±90 days of the
    target period-end (catches quarter-end vs filing-event-date drift).
    """
    target_y = int(period_iso[:4])
    target_m = int(period_iso[5:7])
    target_d = int(period_iso[8:10])
    # Window: event_date in the calendar quarter containing the
    # target date, plus the two following months for filing lag.
    start_ym = (target_y, max(1, target_m))
    end_ym = (target_y, min(12, target_m + 4))
    e_start = f"{start_ym[0]:04d}{start_ym[1]:02d}01"
    e_end = f"{end_ym[0]:04d}{end_ym[1]:02d}28"
    for ft in form_types:
        env = _do_bm25_sec(
            query=f"{ticker} {ft}", k=5, fields=None,
            filters={
                "ticker": ticker, "form_type": ft,
                "event_date_gte": e_start, "event_date_lte": e_end,
            },
            sort=None, body_mode="snippet",
        )
        hits = env.get("hits") or []
        if hits:
            hits.sort(key=lambda h: str((h.get("source") or {}).get("filed_at", "")), reverse=True)
            top = hits[0]
            return top.get("id", ""), str((top.get("source") or {}).get("filed_at", ""))
    return None


def _pull_revenue_cogs(
    ref: str, period_iso: str, quarterly: bool = False,
) -> tuple[Optional[float], Optional[float]]:
    """Pull Revenue + COGS for the given period-end via XBRL.

    Returns ``(revenue, cogs)`` -- both in dollars. When
    ``quarterly=True`` uses ``_q_period_fact`` (2-4 month span);
    otherwise uses ``_fy_full_period_fact`` (11-13 month span).
    Both helpers exclude segment-scoped facts.
    """
    rev_concepts = (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    )
    cogs_concepts = (
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
    )
    if quarterly:
        revenue = _q_period_fact(ref, rev_concepts, period_iso)
        cogs = _q_period_fact(ref, cogs_concepts, period_iso)
    else:
        revenue = _fy_full_period_fact(ref, rev_concepts, period_iso)
        cogs = _fy_full_period_fact(ref, cogs_concepts, period_iso)
    return revenue, cogs


_COGS_TABLE_KEYWORDS = (
    "cost of revenue", "cost of sales", "cost of goods sold",
    "operating expenses", "cost of products",
)


def _extract_cogs_subcomponents(
    ref: str, period_iso: str,
) -> list[tuple[str, float]]:
    """Find COGS sub-component lines + dollar amounts in the filing.

    Tries Item 8 (financial statements) tables first, then Item 7
    (MD&A). Returns a list of ``(label, amount)`` pairs the
    aggregator can rank by absolute change between two periods.
    """
    ref_full = _strip_chunk_suffix(ref)
    rows_seen: list[tuple[str, float]] = []
    seen_labels: set[str] = set()
    for it in ("8", "7"):
        for kw in _COGS_TABLE_KEYWORDS:
            try:
                env = _do_extract_filing_tables(
                    ref=ref_full, table_keyword=kw, item=it, limit=10,
                )
            except Exception:
                continue
            rows = _table_rows_from_extract(env)
            for r in rows:
                label = (r.get("label") or "").strip()
                if not label or len(label) > 80:
                    continue
                lbl_lc = label.lower()
                # Skip headers / totals / margin lines / non-sub-component
                if any(skip in lbl_lc for skip in (
                    "total cost", "gross profit", "gross margin",
                    "operating income", "total revenue", "year ended",
                )):
                    continue
                # Sub-component candidate: label looks like an
                # operating-cost line (depreciation, materials,
                # labor, freight, royalties, etc.).
                cells = r.get("cells") or []
                first_num = None
                for c in cells:
                    n = _parse_num(c)
                    if n is not None and n > 0:
                        first_num = n
                        break
                if first_num is None:
                    continue
                if label not in seen_labels:
                    rows_seen.append((label, first_num))
                    seen_labels.add(label)
        if rows_seen:
            break
    return rows_seen[:20]


def _do_decompose_gross_margin_change(
    ticker: str,
    period_a: str,
    period_b: str,
    period_kind: str = "annual",
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Decompose Δ gross margin between two periods.

    Returns the GM% per period, Δ GM in percentage points,
    revenue-effect vs cost-effect attribution (Δ GM expressed as
    parts contributed by Δ Revenue and Δ COGS), the dominant COGS
    sub-component when disclosed, and a normalized GM that
    re-states period A's COGS sub-component at period B's level
    (the "if the dominant driver hadn't moved" counterfactual).
    """
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    if not period_a or not period_b:
        return {"error": "period_a and period_b (ISO YYYY-MM-DD) required"}
    t = ticker.strip().upper()
    pa = period_a[:10]
    pb = period_b[:10]
    # Auto-detect quarterly vs annual periods from the date shape.
    # Issuers with a calendar fiscal year end on 12-31; common
    # off-calendar FYEs are 1-31 (retailers), 6-30, 9-30. A
    # period-end date that doesn't match a typical FYE (e.g. 2-28,
    # 5-31, 8-31, 11-30) almost always denotes an interim quarter
    # reported on 10-Q / 8-K, not an annual 10-K. When the model
    # passes period_kind='annual' but the dates look quarterly,
    # auto-flip the priority order so quarterly forms are tried
    # first. Generic across any issuer; preserves explicit
    # period_kind override when the dates DO match a known FYE
    # convention.
    _ANNUAL_FYE_PATTERNS = {
        "12-31",  # calendar
        "01-31", "02-01", "02-02",  # retailers (Feb close)
        "06-30",  # mid-year
        "09-30",  # Q3-end FYE (some industrials / federal contractors)
        "03-31",  # March close (Japanese-style; some US issuers)
    }
    def _date_suffix(d: str) -> str:
        return d[5:10] if len(d) >= 10 else ""
    looks_quarterly = (
        _date_suffix(pa) not in _ANNUAL_FYE_PATTERNS
        and _date_suffix(pb) not in _ANNUAL_FYE_PATTERNS
    )
    explicit_quarterly = (period_kind or "").lower().startswith("q")
    use_quarterly = explicit_quarterly or looks_quarterly
    if use_quarterly:
        forms = ("10-Q", "8-K", "10-K")
    else:
        forms = ("10-K", "10-Q", "8-K")
    ref_a_pair = _find_filing_ref_for_period(t, pa, forms)
    ref_b_pair = _find_filing_ref_for_period(t, pb, forms)
    if not ref_a_pair:
        return {"error": f"No filing found covering period_a={pa} for {t}"}
    if not ref_b_pair:
        return {"error": f"No filing found covering period_b={pb} for {t}"}
    ref_a, filed_a = ref_a_pair
    ref_b, filed_b = ref_b_pair

    rev_a, cogs_a = _pull_revenue_cogs(ref_a, pa, quarterly=use_quarterly)
    rev_b, cogs_b = _pull_revenue_cogs(ref_b, pb, quarterly=use_quarterly)

    def _gp_gm(rev: Optional[float], cogs: Optional[float]) -> tuple[Optional[float], Optional[float]]:
        if rev is None or cogs is None or rev <= 0:
            return None, None
        gp = rev - cogs
        return gp, (gp / rev * 100.0)

    gp_a, gm_a = _gp_gm(rev_a, cogs_a)
    gp_b, gm_b = _gp_gm(rev_b, cogs_b)

    if gm_a is None or gm_b is None:
        return {
            "error": (
                "Could not derive Revenue / COGS from XBRL for both "
                "periods. Fall back to extract_filing_tables on the "
                "income statement of the period-B filing."
            ),
            "period_a": {"ref": ref_a, "filed_at": filed_a,
                          "revenue": rev_a, "cogs": cogs_a},
            "period_b": {"ref": ref_b, "filed_at": filed_b,
                          "revenue": rev_b, "cogs": cogs_b},
        }

    delta_gm = gm_b - gm_a  # percentage points
    # Revenue-vs-cost attribution: how much of the absolute Δ GP
    # is explained by revenue growth at the OLD margin vs cost
    # change at the OLD scale. Sign convention: positive value
    # means the effect lifted gross profit.
    delta_rev = (rev_b or 0) - (rev_a or 0)
    delta_cogs = (cogs_b or 0) - (cogs_a or 0)
    revenue_effect_gp = delta_rev * (gm_a / 100.0) if gm_a is not None else 0.0
    cost_effect_gp = -delta_cogs + delta_rev * (1.0 - gm_a / 100.0)
    # Determine which side dominated. The dominant side is the one
    # whose absolute contribution to Δ GP is larger.
    revenue_driven = abs(revenue_effect_gp) >= abs(cost_effect_gp)
    driver_label = "revenue performance" if revenue_driven else "cost of sales"

    # 4. COGS sub-component analysis -- pulled from period-B filing
    # (assumes the issuer publishes a consistent break-out).
    subcomp_b = _extract_cogs_subcomponents(ref_b, pb)
    subcomp_a = _extract_cogs_subcomponents(ref_a, pa)
    # Map by lowercased label so a "Plant depreciation" line in
    # period A pairs with the same line in period B.
    a_map = {label.lower(): amount for label, amount in subcomp_a}
    deltas: list[tuple[str, float, float, float, float]] = []
    # (label, val_a, val_b, abs_delta, pct_change)
    for label, val_b in subcomp_b:
        val_a = a_map.get(label.lower())
        if val_a is None:
            continue
        abs_delta = val_b - val_a
        pct_change = ((val_b - val_a) / val_a * 100.0) if val_a > 0 else None
        deltas.append((label, val_a, val_b, abs_delta, pct_change or 0.0))
    # Dominant driver: the sub-component whose ABSOLUTE change
    # contributes the most to Δ COGS.
    deltas.sort(key=lambda d: abs(d[3]), reverse=True)
    dominant_label = deltas[0][0] if deltas else None
    dominant_a = deltas[0][1] if deltas else None
    dominant_b = deltas[0][2] if deltas else None
    dominant_pct = deltas[0][4] if deltas else None

    # Normalized GM: restate period A's dominant sub-component at
    # period B's level (counterfactual: "if this driver hadn't
    # moved, what would Δ GM have been?"). Equivalently:
    # subtract the dominant Δ from period B's COGS and recompute.
    normalized_gm_a = None
    normalized_delta_gm = None
    if dominant_label is not None and rev_a and cogs_a is not None:
        # Period A counterfactual: add dominant's Δ to A's COGS so
        # A and B share the same level of the dominant driver.
        cogs_a_norm = cogs_a + (dominant_b - dominant_a)
        gp_a_norm = rev_a - cogs_a_norm
        normalized_gm_a = (gp_a_norm / rev_a * 100.0) if rev_a > 0 else None
        if normalized_gm_a is not None:
            normalized_delta_gm = gm_b - normalized_gm_a

    def fmt_b(v):
        if v is None:
            return "—"
        return f"${v/1e6:,.2f}M"

    def fmt_pct(v):
        if v is None:
            return "—"
        return f"{v:.2f}%"

    lines = [
        f"# {t} gross-margin decomposition: {pa} → {pb}",
        "",
        f"| Metric | Period A ({pa}) | Period B ({pb}) | Δ |",
        f"|---|---|---|---|",
        f"| Revenue | {fmt_b(rev_a)} | {fmt_b(rev_b)} | {fmt_b((rev_b or 0) - (rev_a or 0))} |",
        f"| COGS | {fmt_b(cogs_a)} | {fmt_b(cogs_b)} | {fmt_b((cogs_b or 0) - (cogs_a or 0))} |",
        f"| Gross Profit | {fmt_b(gp_a)} | {fmt_b(gp_b)} | {fmt_b((gp_b or 0) - (gp_a or 0))} |",
        f"| Gross Margin | {fmt_pct(gm_a)} | {fmt_pct(gm_b)} | **{delta_gm:+.2f}pp** |",
        "",
        f"**Reported change in gross margin: {delta_gm:+.2f} percentage points.**",
        "",
        f"## Attribution",
        f"- Revenue-side effect on gross profit: {fmt_b(revenue_effect_gp)}",
        f"- Cost-side effect on gross profit: {fmt_b(cost_effect_gp)}",
        f"- **Dominant driver: {driver_label}**",
        "",
    ]
    if deltas:
        lines.extend([
            "## COGS sub-components (ranked by absolute Δ)",
            "| Component | Period A | Period B | Δ | % change |",
            "|---|---|---|---|---|",
        ])
        for label, va, vb, ad, pc in deltas[:10]:
            lines.append(
                f"| {label[:50]} | {fmt_b(va)} | {fmt_b(vb)} | {fmt_b(ad)} | {fmt_pct(pc)} |"
            )
        lines.append("")
        lines.append(
            f"**Dominant sub-component: {dominant_label}** "
            f"({fmt_b(dominant_a)} → {fmt_b(dominant_b)}, "
            f"{fmt_pct(dominant_pct)} change)."
        )
        if normalized_delta_gm is not None:
            lines.append("")
            lines.append(
                f"**Normalized gross margin** (period A restated at "
                f"period B's level of the dominant sub-component): "
                f"{fmt_pct(normalized_gm_a)}. Normalized Δ GM = "
                f"{normalized_delta_gm:+.2f} percentage points "
                f"(vs reported {delta_gm:+.2f}pp). The reported "
                f"deterioration {'overstates' if delta_gm < normalized_delta_gm else 'understates'} "
                f"the underlying core-business margin trend because "
                f"the dominant sub-component distorts comparability."
            )
    else:
        lines.append(
            "## COGS sub-components: not disclosed in extract_filing_tables. "
            "Fall back to manual extract_filing_tables / get_full_text on the "
            "period-B income-statement footnote."
        )

    answer_summary_block = "\n".join(lines)
    return _apply_binding(bind_as, {
        "ticker": t,
        "period_a": pa,
        "period_b": pb,
        "period_a_ref": ref_a,
        "period_b_ref": ref_b,
        "revenue_a": rev_a,
        "revenue_b": rev_b,
        "cogs_a": cogs_a,
        "cogs_b": cogs_b,
        "gross_profit_a": gp_a,
        "gross_profit_b": gp_b,
        "gross_margin_a_pct": gm_a,
        "gross_margin_b_pct": gm_b,
        "delta_gross_margin_pp": delta_gm,
        "revenue_effect_on_gp": revenue_effect_gp,
        "cost_effect_on_gp": cost_effect_gp,
        "driver": driver_label,
        "subcomponents": [
            {"label": label, "value_a": va, "value_b": vb,
             "abs_delta": ad, "pct_change": pc}
            for label, va, vb, ad, pc in deltas
        ],
        "dominant_subcomponent": dominant_label,
        "dominant_value_a": dominant_a,
        "dominant_value_b": dominant_b,
        "dominant_pct_change": dominant_pct,
        "normalized_gross_margin_a_pct": normalized_gm_a,
        "normalized_delta_gross_margin_pp": normalized_delta_gm,
        "answer_summary_block": answer_summary_block,
    })


@skill_fn(
    skill_id='decompose_gross_margin_change',
    description=(
        "Decompose change in gross margin between two periods on a "
        "single issuer. Pulls Revenue + COGS via XBRL for both "
        "periods, computes GM% per period and Δ GM in percentage "
        "points, attributes the change to revenue-side vs cost-side "
        "effects on gross profit, surfaces COGS sub-component "
        "deltas when the issuer discloses them, identifies the "
        "dominant sub-component contributor, and produces a "
        "normalized gross margin (period A restated at period B's "
        "level of the dominant driver). Use for any 'decompose the "
        "change in gross margin between [period A] and [period B]' "
        "question. Returns `answer_summary_block` ready to drop "
        "verbatim. Generic across any issuer with two comparable "
        "periods (annual or quarterly)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "period_a": {
                "type": "string",
                "description": "Earlier period-end date in ISO YYYY-MM-DD format.",
            },
            "period_b": {
                "type": "string",
                "description": "Later period-end date in ISO YYYY-MM-DD format.",
            },
            "period_kind": {
                "type": "string",
                "enum": ["annual", "quarterly"],
                "description": "Whether the periods are annual (10-K) or quarterly (10-Q). Default 'annual'.",
                "default": "annual",
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "period_a", "period_b"],
    },
)
def decompose_gross_margin_change(
    ticker: str,
    period_a: str,
    period_b: str,
    period_kind: str = "annual",
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    return _do_decompose_gross_margin_change(
        ticker=ticker, period_a=period_a, period_b=period_b,
        period_kind=period_kind, bind_as=bind_as,
    )
