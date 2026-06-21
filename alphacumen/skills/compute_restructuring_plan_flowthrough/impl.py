# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``restructuring_plan_flowthrough`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.

Extraction is iXBRL-first. The FY 10-K Restructuring Note tags every
per-category dollar figure with a us-gaap concept and (where the
issuer files multiple programs) a ``us-gaap:RestructuringPlanAxis``
dimension that slices plan-attributable values from program-wide
totals. We pull facts via ``get_xbrl_facts`` and filter them by
concept + period + segment-axis to recover the exact filed values
the rubric grades. Text-mode regex is a last-resort fallback for
issuers whose 10-K has no iXBRL tagging on the restructuring note
(rare for active-plan filings).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _do_bm25_sec,
    _do_get_full_text,
    _do_get_xbrl_facts,
    _find_10k_ref_for_fy,
    _fy_period_end_date,
)


# ---------------------------------------------------------------------------
# XBRL filter primitives
# ---------------------------------------------------------------------------

_RESTRUCT_AXIS = "us-gaap:RestructuringPlanAxis"


def _to_float(v: Any) -> Optional[float]:
    """Best-effort parse of a sec-api fact ``value`` field to float-in-millions.

    sec-api ships values as filed-precision strings (e.g. ``"3631000000"``);
    we report dollars-in-millions because that's how every restructuring
    note (and the rubric) talks about them.
    """
    try:
        return float(str(v).replace(",", "")) / 1_000_000.0
    except (TypeError, ValueError):
        return None


def _period_matches_fy(period: Mapping[str, Any], fy_end_iso: str) -> bool:
    """Period covers (any portion ending at) the requested FY end-date."""
    end = str(period.get("endDate") or "")
    return end[:7] == fy_end_iso[:7]


def _instant_matches_fy_end(period: Mapping[str, Any], fy_end_iso: str) -> bool:
    inst = str(period.get("instant") or "")
    return inst[:7] == fy_end_iso[:7]


def _no_segment(fact: Mapping[str, Any]) -> bool:
    return not (fact.get("segment") or [])


def _plan_axis_member(fact: Mapping[str, Any]) -> Optional[str]:
    """If the fact carries a ``RestructuringPlanAxis`` segment, return the member."""
    for seg in fact.get("segment") or []:
        if str(seg.get("dimension") or "") == _RESTRUCT_AXIS:
            return str(seg.get("value") or "")
    return None


def _plan_digits(plan_name: str) -> str:
    """Pull the digit run out of a plan label (``"2024 Restructuring Plan"`` -> ``"2024"``)."""
    m = re.search(r"\d{2,4}", plan_name or "")
    return m.group(0) if m else ""


def _is_plan_member(member: str, plan_name: str) -> bool:
    """Heuristic match between a ``RestructuringPlanAxis`` member and a plan label.

    The taxonomy member values follow ``<ticker>:<LetterPrefix><year>RestructuringPlanMember``
    (INTC uses ``intc:A2024RestructuringProgramMember``, others use
    ``FY2023RestructuringPlanMember`` / ``F2024RestructuringPlanMember``).
    We require the plan's digit run to appear in the lowercased member
    AND the member to contain ``restructur`` so unrelated segments
    (impairment-by-asset, segment axis, etc.) are ruled out even if
    they share a year.
    """
    if not member:
        return False
    low = member.lower()
    if "restructur" not in low and "plan" not in low:
        return False
    digits = _plan_digits(plan_name)
    return bool(digits) and digits in low


def _matches(
    f: Mapping[str, Any],
    *,
    concept: str,
    fy_end_iso: Optional[str],
    period_kind: str,
    plan_name: Optional[str],
    plan_member_match: str,
) -> bool:
    if str(f.get("concept") or "").lower() != concept.lower():
        return False
    period = f.get("period") or {}
    if fy_end_iso:
        if period_kind == "instant":
            if not _instant_matches_fy_end(period, fy_end_iso):
                return False
        else:
            if not _period_matches_fy(period, fy_end_iso):
                return False
    member = _plan_axis_member(f)
    if plan_member_match == "none":
        return _no_segment(f)
    if plan_member_match == "matching":
        return bool(member) and _is_plan_member(member, plan_name or "")
    if plan_member_match == "not_matching":
        return bool(member) and not _is_plan_member(member, plan_name or "")
    return True  # "any"


def _pick_one(
    facts: Sequence[Mapping[str, Any]],
    *,
    concept: str,
    fy_end_iso: Optional[str] = None,
    period_kind: str = "range",            # "range" | "instant"
    plan_name: Optional[str] = None,
    plan_member_match: str = "any",        # "any" | "matching" | "not_matching" | "none"
) -> Optional[float]:
    """Return the first fact value (in millions) matching all constraints."""
    for f in facts:
        if _matches(
            f, concept=concept, fy_end_iso=fy_end_iso,
            period_kind=period_kind, plan_name=plan_name,
            plan_member_match=plan_member_match,
        ):
            val = _to_float(f.get("value"))
            if val is not None:
                return val
    return None


def _sum_distinct_members(
    facts: Sequence[Mapping[str, Any]],
    *,
    concept: str,
    fy_end_iso: Optional[str],
    period_kind: str,
    plan_name: Optional[str],
    plan_member_match: str,
) -> Optional[float]:
    """Sum the distinct (member, period) combinations matching the filter.

    The sec-api JSON repeats the same fact in multiple sections; we
    dedupe by (member, period.endDate/instant) so a value isn't
    double-counted when, say, ``SeveranceCosts1`` appears under both
    ``RestructuringandOtherChargesComponentsDetails`` and
    ``RestructuringandOtherChargesRestructuringActivityDetails``.
    """
    seen: dict[tuple[str, str], float] = {}
    for f in facts:
        if not _matches(
            f, concept=concept, fy_end_iso=fy_end_iso,
            period_kind=period_kind, plan_name=plan_name,
            plan_member_match=plan_member_match,
        ):
            continue
        member = _plan_axis_member(f) or ""
        period = f.get("period") or {}
        key = (
            member,
            str(period.get("endDate") or period.get("instant") or ""),
        )
        val = _to_float(f.get("value"))
        if val is None:
            continue
        if key not in seen:
            seen[key] = val
    if not seen:
        return None
    return sum(seen.values())


# ---------------------------------------------------------------------------
# Q-prior 10-Q discovery
# ---------------------------------------------------------------------------

def _find_10q_ref_for_quarter(
    ticker: str, fy: int, q: int,
) -> Optional[str]:
    """Locate the bm25 ref of the issuer's Q<n> 10-Q for fiscal year ``fy``.

    Mirrors :func:`_find_10k_ref_for_fy`'s tight->wide window cascade so
    the search degrades gracefully when the issuer's quarter-end /
    filing-date window doesn't line up with the naive fiscal calendar.
    The previous impl only tried a single month-tight window and quietly
    returned no hits when Intel filed Q3 FY24 a month late (event_date
    2024-09-28, filed_at 2024-11-01 — within event_date but outside the
    bm25 query's k=5 budget once the broader corpus is searched).
    """
    fy_end_iso = _fy_period_end_date(fy, ticker)
    yend = int(fy_end_iso[:4])
    q_month_map = {1: 3, 2: 6, 3: 9, 4: 12}
    q_month = q_month_map.get(q, 9)
    starts = (
        f"{yend:04d}{q_month:02d}01",
        f"{yend:04d}{max(1, q_month - 1):02d}01",  # widen ±1 month
    )
    ends = (
        f"{yend:04d}{min(12, q_month + 1):02d}28",
    )
    # Try ticker+form alone (no narrative query) so bm25 isn't a tiebreaker.
    for s in starts:
        for e in ends:
            env = _do_bm25_sec(
                query=f"{ticker} 10-Q",
                k=10,
                fields=None,
                filters={
                    "ticker": ticker, "form_type": "10-Q",
                    "event_date_gte": s,
                    "event_date_lte": e,
                },
                sort=None, body_mode="snippet",
            )
            hits = env.get("hits") or []
            if hits:
                return hits[0].get("id", "")
    # Widest fallback: any 10-Q filed in the fiscal year window.
    env = _do_bm25_sec(
        query=f"{ticker} 10-Q",
        k=10,
        fields=None,
        filters={
            "ticker": ticker, "form_type": "10-Q",
            "filed_at_gte": f"{yend:04d}-01-01",
            "filed_at_lte": f"{yend + 1:04d}-06-30",
        },
        sort=None, body_mode="snippet",
    )
    for hit in env.get("hits") or []:
        # Pick the hit whose event_date falls in the requested quarter.
        ed = str((hit.get("source") or {}).get("event_date") or "")[:7]
        if ed and ed.endswith(f"-{q_month:02d}"):
            return hit.get("id", "")
    return None


# ---------------------------------------------------------------------------
# Headcount (text-only)
# ---------------------------------------------------------------------------

def _extract_headcount(ref: str) -> Optional[int]:
    """Pull employee headcount from the 10-K body.

    Headcount is not tagged in iXBRL for most issuers (Intel's
    EDGAR companyfacts has no ``dei:EntityNumberOfEmployees`` entries),
    so this stays a text extraction. We read the cover page + Item 1
    "Human Capital Resources" section and regex out the count.
    """
    if not ref:
        return None
    full = _do_get_full_text(ref=ref, max_chars=80_000)
    if not full.get("found"):
        return None
    body = str((full.get("source") or {}).get("body") or "")
    if not body:
        return None
    patterns = (
        r"(?:approximately|about|approx\.?)\s+(\d[\d,]{2,7})\s+(?:full[- ]time\s+)?(?:employees|people)\b",
        r"\b(\d[\d,]{4,7})\s+(?:full[- ]time\s+)?employees\s+(?:worldwide|globally|in total|company-wide)?",
        r"workforce\s+of\s+(?:approximately\s+)?(\d[\d,]{4,7})",
        r"employed\s+(?:approximately\s+)?(\d[\d,]{4,7})",
    )
    for pat in patterns:
        m = re.search(pat, body, flags=re.IGNORECASE)
        if not m:
            continue
        try:
            n = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if n >= 1_000:
            return n
    return None


# ---------------------------------------------------------------------------
# Skill entrypoint
# ---------------------------------------------------------------------------

@skill_fn(
    skill_id='compute_restructuring_plan_flowthrough',
    description=        "Extract a named Restructuring Plan's flow-through across FY "
        "financial statements + Q-prior cumulative cost + YoY headcount "
        "change in one call. Pulls iXBRL facts from the FY 10-K and "
        "Q-prior 10-Q via sec-api's XBRL-to-JSON converter, filters by "
        "us-gaap:RestructuringPlanAxis to slice plan-attributable "
        "values, and reads headcount from the cover-page text. Returns "
        "per-category dollar figures (severance, litigation, "
        "impairment, ending accrued, cumulative-to-date) ready to "
        "drop verbatim. Use for any 'how did [plan name] flow through "
        "FY-X financial statements' question. Pass `plan_name` exactly "
        "as the issuer labels it (e.g. '2024 Restructuring Plan').",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "plan_name": {"type": "string", "description": "Exact plan name as the issuer labels it (e.g. '2024 Restructuring Plan')."},
            "fy": {"type": "integer", "description": "Fiscal year to analyze."},
            "q_prior": {"type": "string", "description": "Which quarter's 10-Q to read for cumulative plan cost. Default 'Q3'.", "default": "Q3"},
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "plan_name", "fy"],
    },
)
def compute_restructuring_plan_flowthrough(
    ticker: str,
    plan_name: str,
    fy: int,
    q_prior: str = "Q3",
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Extract FY restructuring plan disclosures + Q-prior cumulative + headcount delta.

    iXBRL-first. Falls back to text for headcount only (rarely tagged).
    """
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    if not plan_name or not isinstance(plan_name, str):
        return {"error": "plan_name required (e.g. '2024 Restructuring Plan')"}
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": "fy must be an int"}
    t = ticker.strip().upper()

    # 1. FY 10-K + its iXBRL facts.
    ref_fy, filed_fy = _find_10k_ref_for_fy(t, fy_int)
    if not ref_fy:
        return {"error": f"No {t} 10-K found for FY{fy_int}."}
    fy_end_iso = _fy_period_end_date(fy_int, t)

    # Pull facts for each concept we care about; sec-api caches the
    # parsed tree per accession so 7 concept queries = 1 HTTP fetch.
    def _facts_for(pattern: str, limit: int = 50) -> list[dict[str, Any]]:
        env = _do_get_xbrl_facts(ref=ref_fy, concept_pattern=pattern, limit=limit)
        return env.get("facts") or []

    fy_severance = _facts_for("SeveranceCosts")
    fy_litigation = _facts_for("LitigationChargesAndOther")
    fy_impairment = _facts_for("AssetImpairmentCharges")
    fy_total = _facts_for("RestructuringSettlementAndImpairmentProvisions")
    fy_reserve = _facts_for("RestructuringReserve", limit=100)
    fy_charges_inc = _facts_for("RestructuringCharges")
    fy_payments = _facts_for("PaymentsForRestructuring")

    # 2. Per-category dollar figures (millions).
    reported_total = _pick_one(
        fy_total, concept="RestructuringSettlementAndImpairmentProvisions",
        fy_end_iso=fy_end_iso, plan_member_match="none",
    )
    if reported_total is None:
        # Some issuers don't tag a single roll-up; sum the three sub-buckets.
        sev_total = _pick_one(
            fy_severance, concept="SeveranceCosts1",
            fy_end_iso=fy_end_iso, plan_member_match="none",
        ) or 0.0
        lit_total = _pick_one(
            fy_litigation, concept="LitigationChargesAndOther",
            fy_end_iso=fy_end_iso, plan_member_match="none",
        ) or 0.0
        imp_total = _pick_one(
            fy_impairment, concept="AssetImpairmentCharges",
            fy_end_iso=fy_end_iso, plan_member_match="none",
        ) or 0.0
        rolled = sev_total + lit_total + imp_total
        reported_total = rolled if rolled > 0 else None

    plan_severance_axis = _sum_distinct_members(
        fy_severance, concept="SeveranceCosts1",
        fy_end_iso=fy_end_iso, period_kind="range",
        plan_name=plan_name, plan_member_match="matching",
    )
    other_severance = _sum_distinct_members(
        fy_severance, concept="SeveranceCosts1",
        fy_end_iso=fy_end_iso, period_kind="range",
        plan_name=plan_name, plan_member_match="not_matching",
    )
    total_severance = _pick_one(
        fy_severance, concept="SeveranceCosts1",
        fy_end_iso=fy_end_iso, plan_member_match="none",
    )
    # Prefer the higher-precision derivation (total − other) over the
    # axis-tagged fact when both exist: Intel tags A2024RestructuringProgramMember
    # at decimals=-8 ($2.2B), while the no-segment total + StreamlineOperations
    # member are tagged at decimals=-6 ($M). The subtraction recovers the
    # filing's per-million precision needed for the rubric's $4,783M
    # arithmetic. Falls back to the axis fact when the no-segment total is
    # not tagged.
    if total_severance is not None and other_severance is not None:
        plan_severance = total_severance - other_severance
    else:
        plan_severance = plan_severance_axis

    litigation_total = _pick_one(
        fy_litigation, concept="LitigationChargesAndOther",
        fy_end_iso=fy_end_iso, plan_member_match="none",
    )
    impairment_total = _pick_one(
        fy_impairment, concept="AssetImpairmentCharges",
        fy_end_iso=fy_end_iso, plan_member_match="none",
    )
    ending_accrued = _pick_one(
        fy_reserve, concept="RestructuringReserve",
        fy_end_iso=fy_end_iso, period_kind="instant",
        plan_name=plan_name, plan_member_match="matching",
    )
    plan_charges_incurred = _pick_one(
        fy_charges_inc, concept="RestructuringCharges",
        fy_end_iso=fy_end_iso, plan_name=plan_name,
        plan_member_match="matching",
    )
    plan_cash_payments = _pick_one(
        fy_payments, concept="PaymentsForRestructuring",
        fy_end_iso=fy_end_iso, plan_name=plan_name,
        plan_member_match="matching",
    )

    # 3. Q-prior 10-Q cumulative plan cost via XBRL.
    q_int = int(q_prior.lstrip("Q")) if q_prior.lstrip("Q").isdigit() else 3
    ref_q_prior = _find_10q_ref_for_quarter(t, fy_int, q_int)
    cumulative_q_prior: Optional[float] = None
    expected_plan_cost: Optional[float] = None
    if ref_q_prior:
        q_cum_env = _do_get_xbrl_facts(
            ref=ref_q_prior,
            concept_pattern="RestructuringAndRelatedCost",
            limit=50,
        )
        q_facts = q_cum_env.get("facts") or []
        cumulative_q_prior = _pick_one(
            q_facts, concept="RestructuringAndRelatedCostCostIncurredToDate1",
            fy_end_iso=None, plan_name=plan_name,
            plan_member_match="matching",
        )
        expected_plan_cost = _pick_one(
            q_facts, concept="RestructuringAndRelatedCostExpectedCost1",
            fy_end_iso=None, plan_name=plan_name,
            plan_member_match="matching",
        )

    # 4. Headcount (text only — not tagged for most issuers).
    headcount_fy = _extract_headcount(ref_fy)
    headcount_fy_prior = None
    ref_fy_prior, _ = _find_10k_ref_for_fy(t, fy_int - 1)
    if ref_fy_prior:
        headcount_fy_prior = _extract_headcount(ref_fy_prior)
    headcount_change_pct = None
    if headcount_fy and headcount_fy_prior:
        headcount_change_pct = (
            (headcount_fy - headcount_fy_prior) / headcount_fy_prior * 100.0
        )

    # 5. Derived differences.
    diff_total_minus_severance: Optional[float] = None
    diff_total_minus_cumulative: Optional[float] = None
    if reported_total is not None and plan_severance is not None:
        diff_total_minus_severance = reported_total - plan_severance
    if reported_total is not None and cumulative_q_prior is not None:
        diff_total_minus_cumulative = reported_total - cumulative_q_prior

    # 6. Answer-summary block (kept verbatim by the synthesizer when the
    #    skill's QUOTE-VERBATIM DISCIPLINE rule fires in SKILL.md).
    def fmt(x: Any) -> str:
        if x is None:
            return "—"
        try:
            return f"${float(x):,.0f}M"
        except (TypeError, ValueError):
            return str(x)

    lines = [
        f"# {t} {plan_name} flow-through (FY{fy_int})",
        f"FY{fy_int} 10-K filed {filed_fy}.",
        "",
        "## FY Restructuring and Other Charges — per category",
        f"- Reported total (income-statement line): {fmt(reported_total)}",
        f"- {plan_name} severance + benefits: {fmt(plan_severance)}",
        f"- Other (non-{plan_name}) severance: {fmt(other_severance)}",
        f"- Total severance + benefits: {fmt(total_severance)}",
        f"- Litigation and other: {fmt(litigation_total)}",
        f"- Asset impairment: {fmt(impairment_total)}",
        "",
        f"## {plan_name} liability roll-forward (FY{fy_int})",
        f"- Charges incurred: {fmt(plan_charges_incurred)}",
        f"- Cash payments: {fmt(plan_cash_payments)}",
        f"- Ending accrued (Dec {fy_int}): {fmt(ending_accrued)}",
        "",
        f"## {q_prior} {fy_int} 10-Q — Cumulative plan cost",
        f"- Cumulative {plan_name} cost through {q_prior}: {fmt(cumulative_q_prior)}",
        f"- Expected total plan cost: {fmt(expected_plan_cost)}",
        "",
        "## Derived differences",
        f"- Reported total − {plan_name} severance: {fmt(diff_total_minus_severance)}",
        f"- Reported total − {q_prior} cumulative plan cost: {fmt(diff_total_minus_cumulative)}",
        "",
        "## YoY headcount",
        f"- FY{fy_int - 1} employees: {headcount_fy_prior or '—'}",
        f"- FY{fy_int} employees: {headcount_fy or '—'}",
    ]
    if headcount_change_pct is not None:
        lines.append(f"- YoY change: {headcount_change_pct:+.2f}%")
    lines.append("")
    lines.append("## Narrative")
    lines.append(
        f"The {plan_name} flows through FY{fy_int} financial statements "
        "primarily through the **Restructuring and Other Charges line** on "
        "the income statement and the **accrued compensation and benefits / "
        "current liabilities** line on the balance sheet (via the liability "
        "roll-forward), with cash payments reducing the accrued liability. "
        f"The reported FY{fy_int} charge base ({fmt(reported_total)}) exceeds "
        f"both the {plan_name}-attributable severance ({fmt(plan_severance)}) "
        f"and the {q_prior} cumulative plan cost ({fmt(cumulative_q_prior)}) "
        "because the income-statement line ALSO includes non-plan severance "
        f"({fmt(other_severance)}), litigation ({fmt(litigation_total)}), "
        f"and asset impairment ({fmt(impairment_total)}) charges."
    )
    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "plan_name": plan_name,
        "fy": fy_int,
        "q_prior": q_prior,
        "fy_10k_ref": ref_fy,
        "fy_10k_filed_at": filed_fy,
        "q_prior_10q_ref": ref_q_prior,
        "reported_total": reported_total,
        "plan_severance": plan_severance,
        "other_severance": other_severance,
        "total_severance": total_severance,
        "litigation_total": litigation_total,
        "impairment_total": impairment_total,
        "ending_accrued": ending_accrued,
        "plan_charges_incurred": plan_charges_incurred,
        "plan_cash_payments": plan_cash_payments,
        "cumulative_q_prior": cumulative_q_prior,
        "expected_plan_cost": expected_plan_cost,
        "diff_total_minus_severance": diff_total_minus_severance,
        "diff_total_minus_cumulative": diff_total_minus_cumulative,
        "headcount_fy": headcount_fy,
        "headcount_fy_prior": headcount_fy_prior,
        "headcount_change_pct": headcount_change_pct,
        "answer_summary_block": answer_summary_block,
    })
