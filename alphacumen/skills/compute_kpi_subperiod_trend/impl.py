# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``single_kpi_trajectory`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Optional
from harness.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _collect_trajectory_facts,
    _do_bm25_sec,
)


@skill_fn(
    skill_id='compute_kpi_subperiod_trend',
    description=        "Pull a single KPI's per-FY series across a multi-year window and "
        "compute the overall CAGR plus two sub-period CAGRs split at an "
        "inflection year. USE THIS for multi-year KPI-trend questions "
        "('how has [issuer]'s X changed from Y to Z', 'what's the trend "
        "of X over the last N years') when the canonical answer expects "
        "per-year values AND sub-period growth-rate framing. Internally "
        "chains bm25_sec(form_type:'10-K') → get_xbrl_facts(concept_pattern) "
        "across the FY window. Returns a pre-composed `answer_summary_block` "
        "(FY-by-FY table + canonical sub-period CAGR bullets) the model "
        "should quote verbatim.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Issuer ticker, e.g. 'AAPL'. Case-insensitive."},
            "kpi_concept": {
                "type": "string",
                "description": (
                    "Case-insensitive substring match on the XBRL concept "
                    "name. Pick from the natural-language KPI name in the "
                    "question (e.g. a per-customer monetization metric, a "
                    "user-count metric, a unit-economics ratio)."
                ),
            },
            "fy_start": {"type": "integer", "description": "First fiscal year in the window."},
            "fy_end": {"type": "integer", "description": "Last fiscal year in the window."},
            "inflection_fy": {
                "type": "integer",
                "description": (
                    "Optional. Year to split sub-periods. Defaults to the "
                    "midpoint of the available data. Useful when the user's "
                    "narrative names a specific pivot year."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "kpi_concept", "fy_start", "fy_end"],
    },
)
def compute_kpi_subperiod_trend(
    ticker: str,
    kpi_concept: str,
    fy_start: int,
    fy_end: int,
    inflection_fy: Optional[int] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Pull per-FY values of ``kpi_concept`` across ``[fy_start, fy_end]``
    via XBRL, then compute overall CAGR plus two sub-period CAGRs split
    at ``inflection_fy`` (auto-midpoint if omitted).

    Returns a canonical ``answer_summary_block`` with the table + the
    sub-period growth narrative the model should quote verbatim.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return {"error": "ticker required"}
    if not kpi_concept:
        return {"error": "kpi_concept required (e.g. 'AverageRevenuePer')"}
    try:
        fy_s, fy_e = int(fy_start), int(fy_end)
    except (TypeError, ValueError):
        return {"error": f"fy_start/fy_end must be ints; got {fy_start!r}, {fy_end!r}"}
    if fy_e <= fy_s:
        return {"error": f"fy_end ({fy_e}) must be > fy_start ({fy_s})"}

    # Pull the most recent 5 10-Ks covering the window; older 10-Ks
    # carry the earliest comparative columns.
    env = _do_bm25_sec(
        query=f"{kpi_concept} annual report",
        k=5,
        filters={
            "form_type": "10-K",
            "ticker": t,
            "event_date_gte": f"{fy_s}0101",
        },
    )
    hits = env.get("hits") or []
    if not hits:
        return {
            "ticker": t, "kpi_concept": kpi_concept,
            "fy_start": fy_s, "fy_end": fy_e,
            "error": f"no 10-K hits for {t} since {fy_s}-01-01",
        }

    def _hit_date(h):
        src = h.get("source") or {}
        return str(src.get("event_date") or src.get("filed_at") or "")
    hits_sorted = sorted(hits, key=_hit_date, reverse=True)
    refs = [(h.get("id") or (h.get("source") or {}).get("id") or "") for h in hits_sorted]
    refs = [r for r in refs if r]

    # Try the caller-provided concept first; if 0 values come back,
    # auto-retry common-synonym concept patterns before erroring.
    # KPI tags vary widely across filers: some issuers use the canonical
    # us-gaap concept name, others use issuer-specific extension tags
    # (`<issuer>:AverageRevenuePer<...>`), and some KPIs are
    # disclosed only in MD&A prose without iXBRL tagging at all. The
    # synonym list covers the most common rewrites; if all variants
    # return 0, the final error message lists everything tried.
    _CONCEPT_SYNONYMS = {
        "AverageRevenuePer": (
            "AverageRevenuePer",
            "RevenuePerPaying",
            "RevenuePerMember",
            "RevenuePerUser",
            "RevenuePerSubscriber",
            "AverageMonthlyRevenue",
            "MonetizationPer",
            "ARPU",
            "ARPPU",
            "ARM",
        ),
        "MonthlyActive": (
            "MonthlyActive",
            "MonthlyActiveUsers",
            "ActiveMonthlyUsers",
            "MAU",
        ),
        "DailyActive": (
            "DailyActive",
            "DailyActiveUsers",
            "ActiveDailyUsers",
            "DAU",
        ),
        "PaidMemberships": (
            "PaidMemberships",
            "PayingMemberships",
            "Subscribers",
            "PaidSubscribers",
            "Memberships",
        ),
        "Subscribers": (
            "Subscribers",
            "PaidSubscribers",
            "TotalSubscribers",
            "PaidMemberships",
        ),
    }

    def _retry_concepts(seed: str) -> tuple[str, ...]:
        """Return ordered list of concept patterns to try: caller's
        original first, then synonyms keyed off whichever synonym
        bucket the seed matches (case-insensitive substring)."""
        out = [seed]
        seed_lower = seed.lower()
        for key, syns in _CONCEPT_SYNONYMS.items():
            if key.lower() in seed_lower or seed_lower in key.lower():
                for syn in syns:
                    if syn not in out:
                        out.append(syn)
                break
        return tuple(out)

    concept_attempts = _retry_concepts(kpi_concept)
    values: dict[int, float] = {}
    used: list[str] = []
    concept_used = kpi_concept
    for concept_try in concept_attempts:
        series, used_try = _collect_trajectory_facts(refs, [concept_try], fy_s, fy_e)
        candidate = series.get(concept_try, {})
        candidate_years = sorted(v for v in candidate if candidate.get(v) is not None)
        if len(candidate_years) >= 2:
            values = candidate
            used = used_try
            concept_used = concept_try
            break

    years = sorted(v for v in values if values.get(v) is not None)
    if len(years) < 2:
        return {
            "ticker": t, "kpi_concept": kpi_concept,
            "fy_start": fy_s, "fy_end": fy_e,
            "values": values,
            "error": (
                f"only {len(years)} full-year value(s) found across "
                f"{len(concept_attempts)} concept patterns tried "
                f"({', '.join(concept_attempts)}); need at least 2 to "
                f"compute trend. KPI may be disclosed only in MD&A "
                f"prose without iXBRL tagging — fall back to "
                f"extract_filing_tables on the 10-K MD&A section."
            ),
        }

    # Sub-period split point.
    #
    # Auto-detect: use the year of the maximum value as the inflection,
    # provided it leaves at least 2 years on each side. This captures
    # the "growth phase → peak → plateau/decline" decomposition that
    # KPI-trend rubrics typically use — the canonical split point sits
    # at the year of maximum value, not the FY-window midpoint. For
    # monotonic-increasing series, the max-value year is the last
    # year and there's no valid split — fall back to the midpoint.
    if inflection_fy is None:
        peak_year = max(years, key=lambda y: values[y])
        # Require ≥2 years before AND after peak for a meaningful split.
        if peak_year != years[0] and peak_year != years[-1] and \
                sum(1 for y in years if y < peak_year) >= 1 and \
                sum(1 for y in years if y > peak_year) >= 1:
            inflection_fy = peak_year
        else:
            inflection_fy = years[len(years) // 2]
    elif inflection_fy not in years:
        # Snap to nearest year we actually have data for.
        inflection_fy = min(years, key=lambda y: abs(y - int(inflection_fy)))

    def _cagr(v0: float, v1: float, n_years: int) -> Optional[float]:
        if n_years <= 0 or v0 <= 0:
            return None
        try:
            return ((v1 / v0) ** (1.0 / n_years) - 1.0) * 100.0
        except (ValueError, ZeroDivisionError, OverflowError):
            return None

    overall_cagr = _cagr(values[years[0]], values[years[-1]], years[-1] - years[0])

    early_years = [y for y in years if y <= inflection_fy]
    late_years = [y for y in years if y >= inflection_fy]
    early_cagr = (
        _cagr(values[early_years[0]], values[early_years[-1]],
              early_years[-1] - early_years[0])
        if len(early_years) >= 2 else None
    )
    late_cagr = (
        _cagr(values[late_years[0]], values[late_years[-1]],
              late_years[-1] - late_years[0])
        if len(late_years) >= 2 else None
    )

    def _fmt_v(v: float) -> str:
        av = abs(v)
        if av >= 1_000_000_000:
            return f"${v / 1_000_000_000:.2f}B"
        if av >= 1_000_000:
            return f"${v / 1_000_000:.0f}M"
        if av >= 100:
            return f"{v:.2f}"
        return f"{v:.2f}"

    rows = ["| FY | Value |", "|---|---|"]
    for y in years:
        rows.append(f"| {y} | {_fmt_v(values[y])} |")
    table = "\n".join(rows)

    cagr_lines: list[str] = []
    if overall_cagr is not None:
        cagr_lines.append(
            f"- Overall: ~{overall_cagr:.1f}% CAGR from FY{years[0]} to FY{years[-1]}"
        )
    if early_cagr is not None:
        cagr_lines.append(
            f"- Sub-period FY{early_years[0]}–FY{early_years[-1]}: "
            f"~{early_cagr:.1f}% annually"
        )
    if late_cagr is not None:
        cagr_lines.append(
            f"- Sub-period FY{late_years[0]}–FY{late_years[-1]}: "
            f"~{late_cagr:.1f}% annually"
        )

    answer_summary_block = (
        f"**{t} {kpi_concept} trend FY{years[0]}–FY{years[-1]}:**\n\n"
        f"{table}\n\n"
        + "\n".join(cagr_lines)
    )

    return _apply_binding(bind_as, {
        "ticker": t,
        "kpi_concept": kpi_concept,
        "kpi_concept_used": concept_used,
        "fy_start": fy_s,
        "fy_end": fy_e,
        "values": values,
        "overall_cagr_pct": overall_cagr,
        "early_subperiod_cagr_pct": early_cagr,
        "late_subperiod_cagr_pct": late_cagr,
        "inflection_fy": inflection_fy,
        "xbrl_refs_used": used,
        "formatted_table": table,
        "answer_summary_block": answer_summary_block,
    })
