# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``fcf_margin_trend`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Optional
from harness.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _FCF_CONCEPTS,
    _apply_binding,
    _collect_trajectory_facts,
    _do_bm25_sec,
)


@skill_fn(
    skill_id='compute_fcf_margin_trend',
    description=        "Compute multi-year Free Cash Flow margin trend (FCF = CFO − Capex; "
        "margin = FCF / Revenue) via XBRL across a FY window. USE THIS for "
        "any question phrased as 'trend of FCF margin', 'approximate FCF as "
        "CFO minus Capex', or 'how has FCF/FCF-margin changed over the last "
        "N years'. Internally pulls the canonical CFO concept "
        "(continuing-ops preferred), absolute-valued Capex, and revenue, "
        "then computes per-FY margin. Returns a pre-composed "
        "`answer_summary_block` (table + trend narrative) to quote verbatim.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Issuer ticker, e.g. 'AAPL'. Case-insensitive."},
            "fy_start": {"type": "integer", "description": "Earliest FY in the trend window."},
            "fy_end": {"type": "integer", "description": "Most recent FY in the trend window."},
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy_start", "fy_end"],
    },
)
def compute_fcf_margin_trend(
    ticker: str,
    fy_start: int,
    fy_end: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Compute CFO − Capex FCF and FCF/Revenue margin per FY across
    ``[fy_start, fy_end]`` via XBRL. Returns the per-year series plus
    a trend narrative and a canonical table.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return {"error": "ticker required"}
    try:
        fy_s, fy_e = int(fy_start), int(fy_end)
    except (TypeError, ValueError):
        return {"error": f"fy_start/fy_end must be ints; got {fy_start!r}, {fy_end!r}"}
    if fy_e <= fy_s:
        return {"error": f"fy_end ({fy_e}) must be > fy_start ({fy_s})"}

    env = _do_bm25_sec(
        query="cash flow operations capex",
        k=5,
        filters={"form_type": "10-K", "ticker": t,
                 "event_date_gte": f"{fy_s}0101"},
    )
    hits = env.get("hits") or []
    if not hits:
        return {"ticker": t, "fy_start": fy_s, "fy_end": fy_e,
                "error": f"no 10-K hits for {t} since {fy_s}-01-01"}

    def _hit_date(h):
        src = h.get("source") or {}
        return str(src.get("event_date") or src.get("filed_at") or "")
    refs = [
        (h.get("id") or (h.get("source") or {}).get("id") or "")
        for h in sorted(hits, key=_hit_date, reverse=True)
    ]
    refs = [r for r in refs if r]

    series, used = _collect_trajectory_facts(refs, list(_FCF_CONCEPTS), fy_s, fy_e)

    # Pick the populated CFO + Revenue concept variant.
    cfo_key = next(
        (k for k in (
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            "NetCashProvidedByUsedInOperatingActivities",
        ) if any(series.get(k, {}).values())),
        None,
    )
    rev_key = next(
        (k for k in (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
        ) if any(series.get(k, {}).values())),
        None,
    )
    capex_key = "PaymentsToAcquirePropertyPlantAndEquipment"
    if not cfo_key or not rev_key:
        return {
            "ticker": t, "fy_start": fy_s, "fy_end": fy_e,
            "error": (
                "could not locate CFO or Revenue XBRL facts. "
                f"cfo_key={cfo_key}, rev_key={rev_key}. Fall back to "
                "extract_filing_tables on the Cash Flow Statement."
            ),
            "series_raw": series,
        }

    # Plausibility threshold. For a typical non-financial operating
    # company FCF margin sits in the −20% to +40% band. Anything above
    # 50% (or below −50%) usually indicates the CFO concept includes
    # discontinued-operations cash flow — most often mortgage
    # origination wind-downs (Zillow / Opendoor / Better.com),
    # broker-dealer cash flow (legacy financial subsidiaries), or
    # large lease-finance unwinds. When detected, attempt an
    # auto-switch: if the OTHER CFO concept variant was tagged for
    # that FY, prefer the smaller value (which the issuer typically
    # tags as the cleaner continuing-ops figure).
    PLAUSIBILITY_THRESHOLD = 50.0

    def _resolve_cfo_for_fy(fy: int) -> tuple[Optional[float], str]:
        """Return (cfo_value, concept_used) for the FY, switching
        to the smaller alternative when the primary value would
        produce an implausible margin."""
        primary = series[cfo_key].get(fy)
        if primary is None:
            return None, ""
        # Check the alternate variant.
        alt_concept = (
            "NetCashProvidedByUsedInOperatingActivities"
            if cfo_key.endswith("ContinuingOperations")
            else "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"
        )
        alt = series.get(alt_concept, {}).get(fy)
        rev = series[rev_key].get(fy)
        if rev and abs(primary / rev) * 100.0 > PLAUSIBILITY_THRESHOLD:
            # Primary is suspicious. If the alt is smaller AND would
            # produce a plausible margin, prefer it.
            if (
                alt is not None
                and abs(alt) < abs(primary)
                and abs(alt / rev) * 100.0 <= PLAUSIBILITY_THRESHOLD
            ):
                return alt, alt_concept
        return primary, cfo_key

    per_fy: dict[int, dict[str, float]] = {}
    cfo_concepts_used: dict[int, str] = {}
    for fy in range(fy_s, fy_e + 1):
        cfo, cfo_used_concept = _resolve_cfo_for_fy(fy)
        capex_raw = series[capex_key].get(fy)
        rev = series[rev_key].get(fy)
        if cfo is None or rev is None:
            continue
        # Capex is reported as a positive cash OUTFLOW under us-gaap;
        # FCF = CFO − abs(Capex). Defensive abs() guards filers that
        # tag it negative.
        capex = abs(capex_raw) if capex_raw is not None else 0.0
        fcf = cfo - capex
        margin_pct = (fcf / rev * 100.0) if rev else None
        per_fy[fy] = {
            "cfo": cfo,
            "capex": capex,
            "fcf": fcf,
            "revenue": rev,
            "fcf_margin_pct": margin_pct,
        }
        cfo_concepts_used[fy] = cfo_used_concept

    # Plausibility warning: if any FY's margin is still >threshold
    # after the auto-switch, the CFO is contaminated by discontinued
    # ops and no XBRL variant gives the clean number. Flag it so the
    # caller can fall back to the MD&A non-GAAP FCF reconciliation.
    suspect_fys = [
        fy for fy, d in per_fy.items()
        if d.get("fcf_margin_pct") is not None
        and abs(d["fcf_margin_pct"]) > PLAUSIBILITY_THRESHOLD
    ]

    if len(per_fy) < 2:
        return {
            "ticker": t, "fy_start": fy_s, "fy_end": fy_e,
            "error": (
                f"only {len(per_fy)} FY had complete CFO+Capex+Revenue. "
                "Concept variance across filings; fall back to "
                "extract_filing_tables on the Cash Flow Statement."
            ),
            "series": per_fy,
        }

    years = sorted(per_fy.keys())
    margins = [per_fy[y]["fcf_margin_pct"] for y in years if per_fy[y]["fcf_margin_pct"] is not None]
    if len(margins) < 2:
        trend = "insufficient data for trend"
    else:
        delta_pp = margins[-1] - margins[0]
        if abs(delta_pp) < 1.0:
            trend = "approximately flat"
        elif delta_pp < 0:
            trend = "declined"
        else:
            trend = "expanded"

    def _fmt_pct(v):
        return f"{v:.1f}%" if v is not None else "—"

    rows = ["| FY | FCF Margin |", "|---|---|"]
    for y in years:
        rows.append(f"| {y} | {_fmt_pct(per_fy[y]['fcf_margin_pct'])} |")
    table = "\n".join(rows)

    narrative = (
        f"FCF margin {trend} from FY{years[0]} "
        f"({_fmt_pct(per_fy[years[0]]['fcf_margin_pct'])}) to "
        f"FY{years[-1]} ({_fmt_pct(per_fy[years[-1]]['fcf_margin_pct'])})."
    )

    plausibility_note = ""
    if suspect_fys:
        plausibility_note = (
            f"\n\n*Note: FY{', FY'.join(str(y) for y in sorted(suspect_fys))} "
            f"margin(s) exceed {PLAUSIBILITY_THRESHOLD:.0f}% which usually "
            f"indicates the CFO concept includes discontinued-operations "
            f"cash flow (e.g. mortgage / broker-dealer / lease-finance "
            f"wind-downs). Cross-check against the issuer's MD&A non-GAAP "
            f"FCF reconciliation table for those years.*"
        )

    answer_summary_block = (
        f"**{t} FCF margin trend FY{years[0]}–FY{years[-1]}** "
        f"(FCF = CFO − Capex; margin = FCF / Revenue):\n\n"
        f"{table}\n\n"
        f"{narrative}"
        f"{plausibility_note}"
    )

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy_start": fy_s,
        "fy_end": fy_e,
        "cfo_concept": cfo_key,
        "cfo_concept_used_per_fy": cfo_concepts_used,
        "capex_concept": capex_key,
        "revenue_concept": rev_key,
        "series": per_fy,
        "trend": trend,
        "suspect_fys": suspect_fys,
        "xbrl_refs_used": used,
        "formatted_table": table,
        "answer_summary_block": answer_summary_block,
    })
