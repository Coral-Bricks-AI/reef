# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``peer_payout_ratio`` skill impl.

Hosts 2 ``@skill_fn``-registered callables for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from harness.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _CONSUMER_STAPLES_PAYOUT_COHORT,
    _apply_binding,
    _do_bm25_sec,
    _do_get_xbrl_facts,
)


@skill_fn(
    skill_id='peer_payout_ratio',
    description=        "Compute one ticker's FY dividend payout ratio (DPS / Diluted "
        "EPS) directly from its 10-K via XBRL. Returns DPS, EPS, "
        "payout_ratio, and a `formatted_atom` string in the canonical "
        "'TICKER: X.XX' shape rubrics use for peer rankings. USE THIS "
        "for any 'compare X's dividend payout to peers' question — "
        "call once per peer ticker (5-8 calls fit comfortably in the "
        "specialist budget). Tool internally fetches the 10-K, tries "
        "multiple GAAP concept names for DPS "
        "(CommonStockDividendsPerShareDeclared / "
        "CommonStockDividendsPerShareCashPaid / etc.) and for diluted "
        "EPS (EarningsPerShareDiluted / "
        "IncomeLossFromContinuingOperationsPerDilutedShare). Returns "
        "an error if the issuer doesn't XBRL-tag DPS (rare for "
        "dividend-paying large-caps); fall back to bm25_sec + "
        "get_full_text on the per-share data exhibit for those.",
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
                    "Fiscal year for the payout ratio, e.g. 2024. "
                    "Tool assumes calendar-year FY-end first then widens "
                    "the 10-K search window if no hits."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy"],
    },
)
def compute_payout_ratio(
    ticker: str,
    fy: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch DPS + Diluted EPS for one ticker's FY 10-K, compute payout ratio.

    Workflow inside the tool:
      1. Find the FY 10-K via bm25_sec (form_type:10-K,
         event_date in Y-12-01..Y-12-31 for calendar-year FY-ends;
         for non-Dec FY-ends, broaden to last filed 10-K covering
         the FY).
      2. Pull dividend-per-share and diluted EPS via get_xbrl_facts.
         Tries common GAAP concept names in order.
      3. Compute payout = DPS / Diluted EPS.

    Returns {ticker, fy, dps, diluted_eps, payout_ratio,
    formatted_atom: "<TICKER>: <ratio:.2f>"}.
    """
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker is required (Consumer Staples large-cap)"}
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": f"fy must be an int (e.g. 2024), got {fy!r}"}
    ticker_u = ticker.strip().upper()

    # Find the FY 10-K. Try calendar-year (Dec FY-end) first; if no
    # hits, widen to any 10-K whose fiscal year ends within fy_int
    # (covers Jan-May FY-ends filed mid-year and Dec FY-ends filed
    # Jan-Mar of fy_int+1).
    env = _do_bm25_sec(
        query=f"{ticker_u} annual report dividends per share earnings",
        k=5,
        fields=None,
        filters={
            "ticker": ticker_u,
            "form_type": "10-K",
            "event_date_gte": f"{fy_int}1201",
            "event_date_lte": f"{fy_int}1231",
        },
        sort=None,
        body_mode="snippet",
    )
    hits = env.get("hits") or []
    if not hits:
        # Widen: any 10-K with event_date in fy_int (catches issuers
        # whose FY ends Jan-Nov, e.g. SJM Apr-end, GIS May-end, NKE
        # May-end, CSCO Jul-end, COST Aug-end, TJX Jan-end).
        env = _do_bm25_sec(
            query=f"{ticker_u} annual report dividends earnings",
            k=5,
            fields=None,
            filters={
                "ticker": ticker_u,
                "form_type": "10-K",
                "event_date_gte": f"{fy_int}0101",
                "event_date_lte": f"{fy_int}1231",
            },
            sort=None,
            body_mode="snippet",
        )
        hits = env.get("hits") or []
    if not hits:
        # Final fallback: widest window — anything filed in
        # fy_int or fy_int+1.
        env = _do_bm25_sec(
            query=f"{ticker_u} annual report dividends earnings",
            k=5,
            fields=None,
            filters={
                "ticker": ticker_u,
                "form_type": "10-K",
                "filed_at_gte": f"{fy_int}-01-01",
                "filed_at_lte": f"{fy_int + 1}-12-31",
            },
            sort=None,
            body_mode="snippet",
        )
        hits = env.get("hits") or []
    if not hits:
        return {
            "ticker": ticker_u, "fy": fy_int,
            "error": (
                f"No {ticker_u} 10-K found for FY{fy_int}. Check "
                "the FY-end calendar (some issuers like CSCO/NKE use "
                "non-Jan FY-ends) and re-run with the right `fy`."
            ),
        }
    hits.sort(key=lambda h: str((h.get("source") or {}).get("filed_at", "")), reverse=True)
    ten_k_ref = hits[0].get("id", "")
    src = hits[0].get("source") or {}
    filed_at = src.get("filed_at", "")

    def _is_full_fy_period(period_str: str) -> bool:
        """Match periods spanning ~one fiscal year (11-13 months),
        ending within fy_int. Works for Dec FY-ends (Jan..Dec),
        non-Dec FY-ends (May..Apr for SJM, Jun..May for GIS, etc.),
        and 52/53-week filers whose dates drift a few days."""
        dates = re.findall(r"(\d{4})-(\d{2})-(\d{2})", period_str)
        if len(dates) < 2:
            return False
        try:
            (y0, m0, d0), (y1, m1, d1) = dates[0], dates[-1]
            y0, m0, d0 = int(y0), int(m0), int(d0)
            y1, m1, d1 = int(y1), int(m1), int(d1)
        except (TypeError, ValueError):
            return False
        # End year must equal fy_int (the FY being analyzed).
        if y1 != fy_int:
            return False
        # Duration must be ~12 months (11-13 to absorb 52/53-week
        # variation and leap years).
        months_span = (y1 - y0) * 12 + (m1 - m0)
        return 11 <= months_span <= 13

    def _max_positive_fact(concept: str) -> Optional[float]:
        """Take max positive value across all periods for a concept.
        DPS is usually reported once per FY; comparatives bring in
        prior years, so max picks the FY-end annual total (later FYs
        > earlier FYs for issuers that grow dividends)."""
        facts_env = _do_get_xbrl_facts(
            ref=ten_k_ref, concept_pattern=concept, periods=None, limit=10,
        )
        if facts_env.get("error"):
            return None
        best: Optional[float] = None
        for fact in facts_env.get("facts", []):
            try:
                v = float(fact.get("value", 0))
            except (TypeError, ValueError):
                continue
            if v > 0 and (best is None or v > best):
                best = v
        return best

    def _fy_period_fact(concept: str, limit: int = 30) -> Optional[float]:
        """Return the max value for a concept whose period matches the
        full FY of `fy_int`. Used for cash-flow / share-count totals
        where we need the value specifically anchored to the FY being
        analyzed (comparatives in the same filing must be excluded)."""
        facts_env = _do_get_xbrl_facts(
            ref=ten_k_ref, concept_pattern=concept, periods=None, limit=limit,
        )
        if facts_env.get("error"):
            return None
        best: Optional[float] = None
        for fact in facts_env.get("facts", []):
            period = str(fact.get("period", ""))
            try:
                v = float(fact.get("value", 0))
            except (TypeError, ValueError):
                continue
            if v > 0 and _is_full_fy_period(period):
                if best is None or v > best:
                    best = v
        return best

    # Pull dividend-per-share via tiered lookup. Prefer cash-paid
    # semantics over declared because the standard payout-ratio
    # convention (Macrotrends, stockanalysis.com, third-party
    # benchmarks) uses cash dividends actually paid during the FY,
    # not declared. Cash-paid differs from declared whenever the
    # issuer changes the quarterly rate mid-year and the Q4
    # declaration is paid in Q1 of the next FY (e.g. KDP raised
    # in Q3 2024; PEP raised in Q4 2024).
    #
    # Tier 1 — per-share CashPaid tag. Few issuers tag this (KO is
    #          one), but when present it's authoritative.
    # Tier 2 — compute from PaymentsOfDividends / WASO basic. Works
    #          for the common case where the issuer only tags
    #          Declared per share but the cash-flow $ total + basic
    #          share count are both tagged at FY granularity
    #          (KDP, PEP, KHC).
    # Tier 3 — per-share Declared tag. Fallback for issuers whose
    #          chunked index doesn't have the cash-flow concepts at
    #          the right period (e.g. SJM's April FY-end).
    dps: Optional[float] = None
    dps_concept = ""
    dps_source = ""

    for concept in ("CommonStockDividendsPerShareCashPaid", "DividendsPerCommonShareCashPaid"):
        v = _max_positive_fact(concept)
        if v is not None:
            dps = v
            dps_concept = concept
            dps_source = "cash_paid_tag"
            break

    if dps is None:
        payments_total: Optional[float] = None
        payments_concept = ""
        for concept in ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends"):
            v = _fy_period_fact(concept)
            if v is not None:
                payments_total = v
                payments_concept = concept
                break
        waso_basic = _fy_period_fact("WeightedAverageNumberOfSharesOutstandingBasic") if payments_total else None
        if payments_total and waso_basic and waso_basic > 0:
            dps = payments_total / waso_basic
            dps_concept = f"{payments_concept}/WeightedAverageNumberOfSharesOutstandingBasic"
            dps_source = "cash_flow_derived"

    if dps is None:
        for concept in ("CommonStockDividendsPerShareDeclared", "DividendsPerShareDeclared"):
            v = _max_positive_fact(concept)
            if v is not None:
                dps = v
                dps_concept = concept
                dps_source = "declared_tag"
                break

    if dps is None or dps <= 0:
        return {
            "ticker": ticker_u, "fy": fy_int, "ten_k_ref": ten_k_ref,
            "error": (
                f"Could not extract dividends-per-share from {ten_k_ref} "
                "via XBRL. Issuer may report DPS only in cash-flow "
                "narrative (not XBRL-tagged) or pay no dividend."
            ),
        }

    # Pull diluted EPS.
    eps: Optional[float] = None
    eps_concept = ""

    for concept in (
        "EarningsPerShareDiluted",
        "IncomeLossFromContinuingOperationsPerDilutedShare",
        "NetIncomeLossPerDilutedShare",
    ):
        facts_env = _do_get_xbrl_facts(
            ref=ten_k_ref,
            concept_pattern=concept,
            periods=None,
            limit=30,
        )
        if facts_env.get("error"):
            continue
        for fact in facts_env.get("facts", []):
            period = str(fact.get("period", ""))
            try:
                v = float(fact.get("value", 0))
            except (TypeError, ValueError):
                continue
            if v != 0 and _is_full_fy_period(period):
                eps = v
                eps_concept = concept
                break
        if eps is None:
            # Fallback: any non-zero fact ending in FY year.
            for fact in facts_env.get("facts", []):
                period = str(fact.get("period", ""))
                try:
                    v = float(fact.get("value", 0))
                except (TypeError, ValueError):
                    continue
                if v != 0 and f"{fy_int}-12" in period:
                    eps = v
                    eps_concept = concept
                    break
        if eps is not None and eps != 0:
            break

    if eps is None or eps == 0:
        return {
            "ticker": ticker_u, "fy": fy_int, "ten_k_ref": ten_k_ref,
            "dps": dps,
            "error": (
                f"Could not extract diluted EPS for FY{fy_int} from "
                f"{ten_k_ref} via XBRL."
            ),
        }

    payout = dps / eps
    formatted_atom = f"{ticker_u}: {payout:.2f}"

    return _apply_binding(bind_as, {
        "ticker": ticker_u,
        "fy": fy_int,
        "ten_k_ref": ten_k_ref,
        "ten_k_filed_at": filed_at,
        "inputs": {
            "dps_concept": dps_concept,
            "dps_source": dps_source,
            "eps_concept": eps_concept,
        },
        "dps": round(dps, 4),
        "diluted_eps": round(eps, 4),
        "payout_ratio": round(payout, 4),
        "formatted_atom": formatted_atom,
    })


@skill_fn(
    skill_id='peer_payout_ratio',
    description=        "Fan compute_payout_ratio out across a fixed S&P GICS Consumer "
        "Staples reference cohort (~18 large-caps spanning Beverages, "
        "Food Products, and Household & Personal Products) in one call. "
        "USE THIS for any 'compare [issuer]'s FY payout ratio to peers' "
        "question when the issuer is a Consumer Staples company — the "
        "tool handles cross-sub-industry peer breadth automatically, "
        "which is the failure mode for prompt-only peer selection "
        "(model picks pure-play same-sub-industry list and misses "
        "rubric peers in adjacent sub-industries). Returns "
        "`answer_summary_block` ready to drop verbatim into the final "
        "answer, plus a per-ticker `peers` list. Internally calls "
        "compute_payout_ratio once per cohort ticker (1 tool call from "
        "the model's perspective; ~30-45s wall time). For issuers "
        "outside Consumer Staples, fall back to compute_payout_ratio "
        "per ticker with a sector-appropriate peer list.",
    parameters=               {
        "type": "object",
        "properties": {
            "issuer": {
                "type": "string",
                "description": (
                    "Named-in-question issuer ticker (Consumer Staples large-cap). "
                    "Case-insensitive. If the issuer isn't in the "
                    "Consumer Staples cohort, the tool prepends it "
                    "to the cohort and runs anyway."
                ),
            },
            "fy": {
                "type": "integer",
                "description": (
                    "Fiscal year for the payout ratio, e.g. 2024. "
                    "Applied uniformly to every cohort ticker."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["issuer", "fy"],
    },
)
def compute_payout_ratio_peers(
    issuer: str,
    fy: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Compute payout ratios for `issuer` + a fixed S&P GICS Consumer
    Staples reference cohort (~18 large-caps spanning Beverages, Food
    Products, and Household & Personal Products). Returns one
    `formatted_atom` per ticker, ranked highest-to-lowest.

    Use this for any "compare [issuer]'s FY payout ratio to peers"
    question when the issuer is a Consumer Staples large-cap. For
    issuers outside this cohort, fall back to calling
    `compute_payout_ratio(ticker, fy)` per ticker with a sector-
    appropriate peer list.
    """
    issuer_u = issuer.strip().upper() if isinstance(issuer, str) else ""
    if not issuer_u:
        return {"error": "issuer ticker required"}
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": f"fy must be an int (e.g. 2024), got {fy!r}"}

    cohort = list(_CONSUMER_STAPLES_PAYOUT_COHORT)
    if issuer_u not in cohort:
        # Don't refuse outright -- the issuer might be in an adjacent
        # sector (e.g. a Consumer Discretionary brand). Run the cohort
        # plus the issuer so the model gets data for both.
        cohort = [issuer_u] + cohort

    results: list[dict[str, Any]] = []
    for ticker in cohort:
        r = compute_payout_ratio(ticker=ticker, fy=fy_int)
        if r.get("error"):
            results.append({
                "ticker": ticker,
                "payout_ratio": None,
                "error": r.get("error", ""),
                "formatted_atom": f"{ticker}: 0.00 (no dividend / data gap)",
            })
            continue
        results.append({
            "ticker": ticker,
            "dps": r.get("dps"),
            "diluted_eps": r.get("diluted_eps"),
            "payout_ratio": r.get("payout_ratio"),
            "formatted_atom": r.get("formatted_atom"),
            "dps_source": (r.get("inputs") or {}).get("dps_source"),
        })

    # Sort by payout ratio descending; None values at the bottom.
    ranked = sorted(
        results,
        key=lambda x: (-(x.get("payout_ratio") or -1.0), x["ticker"]),
    )

    answer_summary_block = "\n".join(
        f"- {r.get('formatted_atom') or (r['ticker'] + ': error')}"
        for r in ranked
    )

    return _apply_binding(bind_as, {
        "issuer": issuer_u,
        "fy": fy_int,
        "cohort_label": (
            f"S&P GICS Consumer Staples reference cohort "
            f"({len(cohort)} large-caps across Beverages, Food Products, "
            f"Household & Personal Products)"
        ),
        "peers": ranked,
        "ranked_descending": [r["ticker"] for r in ranked],
        "answer_summary_block": answer_summary_block,
    })
