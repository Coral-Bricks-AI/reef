# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``competitive_trajectory`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _TRAJECTORY_DEFAULT_METRICS,
    _apply_binding,
    _collect_trajectory_facts,
    _do_bm25_sec,
    _do_get_full_text,
    _extract_competition_section,
    _format_trajectory_table,
)


@skill_fn(
    skill_id='compute_competitive_trajectory',
    description=        "Hard rule 5.14 packaged as ONE call. USE THIS for any "
        "competitive-*trajectory* question with an explicit multi-year "
        "dimension — 'how has [issuer]'s competitive position changed "
        "since [year]', 'evolution of [issuer]'s strategy', '[issuer]'s "
        "positioning over time'. Internally chains bm25_sec(form_type: "
        "'10-K') → get_full_text on the most-recent 10-K (extracts the "
        "Item 1 Competition sub-section) → get_xbrl_facts across the "
        "FY window for the four canonical income-statement concepts "
        "(Revenues, GrossProfit, OperatingIncomeLoss, NetIncomeLoss). "
        "Returns a pre-composed `answer_summary_block` containing the "
        "Competition quote plus a FY-by-FY markdown table — quote it "
        "verbatim in the final answer. ~25-40s wall time, one tool "
        "call from the specialist's perspective (replaces the 3-call "
        "recipe). Do NOT use for cross-sectional competitive landscape "
        "questions without a trajectory dimension — that's "
        "Hard rule 5.13 (single bm25_sec + ≤2 get_full_text).",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": (
                    "Issuer ticker, e.g. 'TSLA'. Case-insensitive. "
                    "Foreign filers using 20-F are NOT supported by "
                    "this tool (no us-gaap XBRL tagging); fall back "
                    "to bm25_sec + extract_filing_tables for those."
                ),
            },
            "fy_start": {
                "type": "integer",
                "description": (
                    "First fiscal year of the trajectory window, "
                    "e.g. 2023 for 'since 2023'."
                ),
            },
            "fy_end": {
                "type": "integer",
                "description": (
                    "Last fiscal year of the trajectory window, "
                    "e.g. 2025 for the most recent reported FY."
                ),
            },
            "metrics": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional override of the metric concept_pattern "
                    "list. Defaults to ['Revenues', 'GrossProfit', "
                    "'OperatingIncomeLoss', 'NetIncomeLoss']. Pass "
                    "a narrower list (e.g. ['Revenues']) only if the "
                    "question explicitly asks about one metric."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy_start", "fy_end"],
    },
)
def compute_competitive_trajectory(
    ticker: str,
    fy_start: int,
    fy_end: int,
    metrics: Optional[Sequence[str]] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Hard rule 5.14 packaged as one tool call.

    Internally:
    1. ``bm25_sec(form_type:"10-K", ticker:<TICKER>, event_date_gte:<fy_start-01-01>, k:5)``
    2. ``get_full_text`` on the top 1-2 hits (Item 1 Competition section)
    3. ``get_xbrl_facts`` across those accessions for the chosen
       income-statement metrics, filtered to full-year facts in
       ``[fy_start, fy_end]``.

    Returns a pre-composed ``answer_summary_block`` (Competition
    quote + FY table) the model can drop into the final answer.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return {"error": "ticker required (e.g. 'TSLA')"}
    try:
        fy_s, fy_e = int(fy_start), int(fy_end)
    except (TypeError, ValueError):
        return {"error": f"fy_start/fy_end must be ints; got {fy_start!r}, {fy_end!r}"}
    if fy_e < fy_s:
        fy_s, fy_e = fy_e, fy_s
    metric_keys = list(metrics) if metrics else list(_TRAJECTORY_DEFAULT_METRICS)

    # Step 1: locate 10-Ks covering the window.
    env = _do_bm25_sec(
        query="competition business overview",
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
            "ticker": t,
            "fy_start": fy_s,
            "fy_end": fy_e,
            "error": (
                f"no 10-K hits for {t} since {fy_s}-01-01. Issuer may "
                f"file 20-F (foreign) or use a non-calendar FY. Fall "
                f"back to bm25_sec with form_type:['10-K','20-F']."
            ),
            "filings_found": [],
        }
    # Sort hits newest-first by event_date.
    def _hit_date(h: Mapping[str, Any]) -> str:
        src = h.get("source") or {}
        return str(src.get("event_date") or src.get("filed_at") or "")
    hits_sorted = sorted(hits, key=_hit_date, reverse=True)

    # Step 2: full text on top hit for Competition narrative.
    top_hit = hits_sorted[0]
    top_ref = top_hit.get("id") or (top_hit.get("source") or {}).get("id") or ""
    competition_text = ""
    full_text_ref = ""
    if top_ref:
        ft = _do_get_full_text(top_ref, max_chars=24_000)
        if ft.get("found"):
            full_text_ref = top_ref
            body = (ft.get("source") or {}).get("body") or ""
            competition_text = _extract_competition_section(body)
            # Fallback: if the regex missed (renderer variance), grab
            # the first 2000 chars of Item 1 narrative so the answer
            # still has SOMETHING to anchor against the table.
            if not competition_text and body:
                competition_text = body[:2000].strip()

    # Step 3: XBRL facts across all 10-K accessions covering the window.
    refs_for_xbrl = [
        (h.get("id") or (h.get("source") or {}).get("id") or "")
        for h in hits_sorted
    ]
    refs_for_xbrl = [r for r in refs_for_xbrl if r]
    series, xbrl_refs_used = _collect_trajectory_facts(
        refs_for_xbrl, metric_keys, fy_s, fy_e,
    )

    table = _format_trajectory_table(series, fy_s, fy_e)
    has_any_fact = any(series[m] for m in metric_keys)

    # Compose the drop-in answer block. Three sections, gracefully
    # degrading if Competition or XBRL is missing.
    blocks: list[str] = []
    if competition_text:
        blocks.append(
            f"**Competition narrative ({t} 10-K, ref `{full_text_ref}`):**\n\n"
            f"> {competition_text.strip()}"
        )
    if table:
        blocks.append(
            f"**Financial trajectory FY{fy_s}–FY{fy_e}:**\n\n{table}"
        )
    elif has_any_fact:
        blocks.append(
            "**Financial trajectory:** facts retrieved but FY coverage "
            "incomplete (see `series` field)."
        )
    else:
        blocks.append(
            f"**Financial trajectory:** no XBRL facts matched the four "
            f"canonical concepts ({', '.join(metric_keys)}) for FY"
            f"{fy_s}–FY{fy_e}. Issuer may not tag these per us-gaap; "
            f"fall back to extract_filing_tables on the income statement."
        )
    answer_summary_block = "\n\n".join(blocks)

    filings_found = [
        {
            "ref": h.get("id") or (h.get("source") or {}).get("id") or "",
            "event_date": _hit_date(h),
            "title": (h.get("source") or {}).get("title")
                     or (h.get("source") or {}).get("form_type")
                     or "",
        }
        for h in hits_sorted
    ]

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy_start": fy_s,
        "fy_end": fy_e,
        "metrics": metric_keys,
        "filings_found": filings_found,
        "competition_ref": full_text_ref,
        "competition_text": competition_text,
        "xbrl_refs_used": xbrl_refs_used,
        "series": series,
        "formatted_table": table,
        "answer_summary_block": answer_summary_block,
    })
