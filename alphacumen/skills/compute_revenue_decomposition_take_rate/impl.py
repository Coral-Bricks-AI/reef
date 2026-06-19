# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``marketplace_revenue_decomp`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from harness.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _TAKE_RATE_REVENUE_CONCEPTS,
    _TAKE_RATE_VOLUME_CONCEPTS,
    _apply_binding,
    _collect_segment_facts,
    _collect_trajectory_facts,
    _do_bm25_sec,
    _do_get_full_text,
)


@skill_fn(
    skill_id='compute_revenue_decomposition_take_rate',
    description=        "Decompose marketplace YoY revenue growth into take-rate "
        "expansion vs volume growth. USE THIS for 'what portion of "
        "[issuer]'s revenue growth was driven by take-rate vs volume' "
        "questions for two-sided marketplace issuers whose top-line "
        "is Revenue = Take Rate × Gross Bookings (or GMV). Internally "
        "pulls Revenue + Gross Bookings / GMV "
        "via XBRL for FY and FY-1, computes per-FY take rate = "
        "Revenue / Volume, and attributes growth using the canonical "
        "take_rate × volume identity. Returns a pre-composed "
        "`answer_summary_block` with the decomposition the model "
        "should quote verbatim.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Marketplace issuer ticker. Case-insensitive."},
            "fy": {"type": "integer", "description": "Fiscal year of interest. The tool also pulls FY-1 automatically."},
            "volume_concept": {
                "type": "string",
                "description": (
                    "Optional XBRL concept substring for the volume metric. "
                    "Defaults to trying GrossBookings / GrossMerchandiseVolume / "
                    "GrossMerchandiseValue / TotalGrossBookings / Bookings in order. "
                    "Pass an explicit value (e.g. '<issuer>:GrossBookings') if the "
                    "issuer uses an extension concept."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy"],
    },
)
def compute_revenue_decomposition_take_rate(
    ticker: str,
    fy: int,
    volume_concept: Optional[str] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Decompose YoY revenue growth into take-rate expansion vs volume
    growth for a two-sided marketplace issuer whose top-line is
    Revenue = Take Rate × Gross Bookings (or GMV).

    Pulls Revenue + Volume (Gross Bookings / GMV) for FY and FY-1,
    computes take_rate = Revenue / Volume per FY, then attributes:
      - volume contribution: take_rate[FY-1] × volume_growth
      - take-rate contribution: volume[FY] × (take_rate[FY] − take_rate[FY-1])
    """
    t = (ticker or "").strip().upper()
    if not t:
        return {"error": "ticker required"}
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": f"fy must be int; got {fy!r}"}
    fy_prior = fy_int - 1
    volume_keys = [volume_concept] if volume_concept else list(_TAKE_RATE_VOLUME_CONCEPTS)

    env = _do_bm25_sec(
        query="gross bookings take rate revenue",
        k=5,
        filters={"form_type": "10-K", "ticker": t,
                 "event_date_gte": f"{fy_int}0101"},
    )
    hits = env.get("hits") or []
    if not hits:
        return {"ticker": t, "fy": fy_int,
                "error": f"no 10-K hits for {t} FY{fy_int}"}

    def _hit_date(h):
        src = h.get("source") or {}
        return str(src.get("event_date") or src.get("filed_at") or "")
    refs = [
        (h.get("id") or (h.get("source") or {}).get("id") or "")
        for h in sorted(hits, key=_hit_date, reverse=True)
    ]
    refs = [r for r in refs if r]

    series, _ = _collect_trajectory_facts(
        refs,
        list(_TAKE_RATE_REVENUE_CONCEPTS) + volume_keys,
        fy_prior, fy_int,
    )

    rev_key = next(
        (k for k in _TAKE_RATE_REVENUE_CONCEPTS
         if all(y in series.get(k, {}) for y in (fy_prior, fy_int))),
        None,
    )
    vol_key = next(
        (k for k in volume_keys
         if all(y in series.get(k, {}) for y in (fy_prior, fy_int))),
        None,
    )
    if not rev_key or not vol_key:
        return {
            "ticker": t, "fy": fy_int,
            "error": (
                "could not locate both Revenue and Volume (gross bookings / "
                "GMV) XBRL facts for FY-1 and FY. Volume concept may be "
                "issuer-extension (e.g. 'uber:GrossBookings'); pass an "
                "explicit `volume_concept` argument."
            ),
            "concepts_available": {k: list(v.keys()) for k, v in series.items() if v},
        }

    rev_prior = series[rev_key][fy_prior]
    rev_curr = series[rev_key][fy_int]
    vol_prior = series[vol_key][fy_prior]
    vol_curr = series[vol_key][fy_int]

    take_rate_prior = (rev_prior / vol_prior) if vol_prior else None
    take_rate_curr = (rev_curr / vol_curr) if vol_curr else None
    revenue_growth_pct = ((rev_curr - rev_prior) / rev_prior * 100.0) if rev_prior else None
    volume_growth_pct = ((vol_curr - vol_prior) / vol_prior * 100.0) if vol_prior else None
    take_rate_change_pp = (
        (take_rate_curr - take_rate_prior) * 100.0
        if (take_rate_prior is not None and take_rate_curr is not None) else None
    )
    # Volume contribution + take-rate contribution to revenue growth.
    volume_contrib_usd = (
        (vol_curr - vol_prior) * take_rate_prior
        if (vol_prior is not None and take_rate_prior is not None) else None
    )
    take_rate_contrib_usd = (
        vol_curr * (take_rate_curr - take_rate_prior)
        if (vol_curr is not None and take_rate_prior is not None and take_rate_curr is not None) else None
    )

    def _fmt_pct(v):
        return f"{v:.2f}%" if v is not None else "—"

    # Per-segment volume growth. Pull segment-axis XBRL facts for the
    # same volume concept on the most-recent 10-K. The canonical
    # answer for marketplace decomposition includes the per-segment
    # breakdown alongside the aggregate take-rate split.
    segment_growth: list[dict[str, Any]] = []
    if refs and vol_key:
        seg_facts = _collect_segment_facts(refs[0], vol_key, fy_int, fy_prior)
        for seg_name, year_map in seg_facts.items():
            prior = year_map.get(fy_prior)
            curr = year_map.get(fy_int)
            if prior is None or curr is None or prior == 0:
                continue
            growth_pct = (curr - prior) / abs(prior) * 100.0
            segment_growth.append({
                "segment": seg_name,
                "prior_value": prior,
                "current_value": curr,
                "growth_pct": growth_pct,
            })
        segment_growth.sort(key=lambda r: -r["growth_pct"])

    # MD&A text-regex fallback. Marketplace issuers that don't tag
    # segment-level volume metrics in XBRL (some marketplace issuers
    # publish per-segment Gross Bookings growth in the Item 7 MD&A
    # narrative rather than as
    # tagged facts) still disclose the canonical numbers in a stable
    # sentence template: "[Segment] Gross Bookings (grew|declined|
    # increased|decreased|rose|fell) [by] X% year-over-year". This
    # pattern is highly specific — segment-name + "Gross Bookings" +
    # signed verb + percentage — and avoids the false-positive trap
    # of the broader "X% in [Segment]" phrasing the 0.0.320 build
    # tried (which mis-grabbed Q4 / Revenue numbers).
    #
    # 0.0.323: only one pattern, anchored to "Gross Bookings". Signed
    # growth where "declined / decreased / fell" → negative pct.
    if not segment_growth and refs:
        ft = _do_get_full_text(refs[0], max_chars=80_000)
        if ft.get("found"):
            body = (ft.get("source") or {}).get("body") or ""
            body_flat = re.sub(r"\s+", " ", body)
            sentence_pat = re.compile(
                r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+Gross\s+Bookings\s+"
                r"(grew|increased|rose|declined|decreased|fell)\s+"
                r"(?:by\s+)?(\d+(?:\.\d+)?)\s*%",
            )
            seen_segments: set[str] = set()
            for m in sentence_pat.finditer(body_flat):
                seg = m.group(1).strip()
                verb = m.group(2).lower()
                pct = float(m.group(3))
                if verb in ("declined", "decreased", "fell"):
                    pct = -pct
                if seg in seen_segments:
                    continue
                # Filter common-noun false positives that happen to
                # capitalize before "Gross Bookings" (e.g. "Total Gross
                # Bookings", "Adjusted Gross Bookings"). The actual
                # marketplace segment names are issuer-specific but
                # always distinct from these aggregate / qualifier
                # words.
                if seg in (
                    "Total", "Adjusted", "Aggregate", "Consolidated",
                    "Worldwide", "Global", "Overall",
                ):
                    continue
                seen_segments.add(seg)
                segment_growth.append({
                    "segment": seg,
                    "prior_value": None,
                    "current_value": None,
                    "growth_pct": pct,
                    "source": "mdna_regex",
                })
            segment_growth.sort(key=lambda r: -r["growth_pct"])

    segment_lines = ""
    if segment_growth:
        bullets = "\n".join(
            f"- **{r['segment']}**: {_fmt_pct(r['growth_pct'])}"
            for r in segment_growth
        )
        segment_lines = (
            f"\n\n**Per-segment {vol_key} growth FY{fy_prior}→FY{fy_int}:**\n\n"
            f"{bullets}"
        )

    answer_summary_block = (
        f"**{t} FY{fy_int} revenue-growth decomposition** "
        f"(Revenue / Gross Bookings = take rate):\n\n"
        f"- FY{fy_prior} take rate: {_fmt_pct(take_rate_prior * 100) if take_rate_prior else '—'}\n"
        f"- FY{fy_int} take rate:  {_fmt_pct(take_rate_curr * 100) if take_rate_curr else '—'}\n"
        f"- Take-rate change: **{_fmt_pct(take_rate_change_pp)} pp**\n"
        f"- Volume (Gross Bookings) growth: **{_fmt_pct(volume_growth_pct)}**\n"
        f"- Revenue growth: **{_fmt_pct(revenue_growth_pct)}**\n\n"
        f"Overall revenue growth was driven primarily by "
        f"{'volume growth (take rate flat)' if take_rate_change_pp is not None and abs(take_rate_change_pp) < 0.5 else 'take-rate expansion' if take_rate_change_pp and take_rate_change_pp > 0 else 'take-rate compression offset' if take_rate_change_pp and take_rate_change_pp < 0 else 'mixed factors'}."
        f"{segment_lines}"
    )

    return _apply_binding(bind_as, {
        "ticker": t, "fy": fy_int,
        "revenue_concept": rev_key,
        "volume_concept": vol_key,
        "revenue": {fy_prior: rev_prior, fy_int: rev_curr},
        "volume": {fy_prior: vol_prior, fy_int: vol_curr},
        "take_rate": {fy_prior: take_rate_prior, fy_int: take_rate_curr},
        "take_rate_change_pp": take_rate_change_pp,
        "volume_growth_pct": volume_growth_pct,
        "revenue_growth_pct": revenue_growth_pct,
        "volume_contribution_usd": volume_contrib_usd,
        "take_rate_contribution_usd": take_rate_contrib_usd,
        "per_segment_growth": segment_growth,
        "answer_summary_block": answer_summary_block,
    })
