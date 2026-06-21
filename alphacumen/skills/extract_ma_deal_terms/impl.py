# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``ma_deal_terms`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Optional
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _MA_EQUITY_RE,
    _MA_EV_RE,
    _MA_PPS_RE,
    _MA_RATIO_RE,
    _apply_binding,
    _do_bm25_sec,
    _do_get_full_text,
)


@skill_fn(
    skill_id='extract_ma_deal_terms',
    description=        "Extract the canonical M&A deal terms (share ratio, price per "
        "share, equity value, enterprise value, acquirer) from the "
        "merger 8-K naming the target. USE THIS for 'what price was "
        "[target] acquired at' / 'deal terms for [target]' / 'equity "
        "and enterprise value of the [target] acquisition' questions. "
        "Internally chains bm25_sec(form_type:'8-K', ticker:target) → "
        "get_full_text → regex extraction of the four canonical fields. "
        "Returns a pre-composed `answer_summary_block` listing each "
        "field. When regex extraction fails, returns the candidate "
        "8-K refs so the model can fall back to get_full_text manually.",
    parameters=               {
        "type": "object",
        "properties": {
            "target_ticker": {
                "type": "string",
                "description": "Target company ticker (the company being acquired). Case-insensitive.",
            },
            "asof": {
                "type": "string",
                "description": (
                    "Optional ISO date (YYYY-MM-DD). When set, restricts "
                    "the 8-K search to the 2-year window ending at this "
                    "date. Defaults to no date clamp."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["target_ticker"],
    },
)
def extract_ma_deal_terms(
    target_ticker: str,
    asof: Optional[str] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Locate the merger 8-K announcing the acquisition of
    ``target_ticker`` and extract the canonical deal terms
    (acquirer, share ratio, price per share, equity value, EV).

    The tool searches for Item 1.01 / 8.01 8-Ks naming the target;
    the model can refine via the returned ``filings_found`` list if
    multiple deals match.
    """
    t = (target_ticker or "").strip().upper()
    if not t:
        return {"error": "target_ticker required"}

    filters: dict[str, Any] = {
        "form_type": "8-K",
        "ticker": t,
    }
    if asof:
        # Search a 2-year window ending at asof.
        try:
            ymd = asof[:10].replace("-", "")
            filters["filed_at_lte"] = ymd
            from datetime import date as _d, timedelta as _td  # noqa: PLC0415
            ad = _d.fromisoformat(asof[:10])
            filters["filed_at_gte"] = (ad - _td(days=730)).strftime("%Y%m%d")
        except Exception:  # noqa: BLE001
            pass

    # Pass 1: target's own 8-Ks (when target is still filing).
    env = _do_bm25_sec(
        query="merger agreement acquisition definitive",
        k=6,
        filters=filters,
    )
    hits = env.get("hits") or []

    # Pass 2: acquirer-side search. When the target stopped filing
    # at deal close (or never filed an 8-K because the deal moved
    # fast), the acquirer's 8-K names the target in the body.
    # Search without the ticker filter, scoped to recent 8-Ks.
    if not hits:
        fb_filters: dict[str, Any] = {"form_type": "8-K"}
        if asof:
            try:
                ymd = asof[:10].replace("-", "")
                fb_filters["filed_at_lte"] = ymd
                from datetime import date as _d, timedelta as _td  # noqa: PLC0415
                ad = _d.fromisoformat(asof[:10])
                fb_filters["filed_at_gte"] = (ad - _td(days=730)).strftime("%Y%m%d")
            except Exception:  # noqa: BLE001
                pass
        env_fb = _do_bm25_sec(
            query=f"{t} acquisition merger agreement",
            k=10,
            filters=fb_filters,
        )
        hits = env_fb.get("hits") or []

    if not hits:
        return {
            "target_ticker": t,
            "error": (
                f"no 8-K hits for target={t} via either ticker-filter "
                f"OR acquirer-side keyword search. Target may not have "
                f"been acquired in the window, or the deal may live in "
                f"a different form type (S-4, 425, DEFM14A)."
            ),
        }

    # Prefer the first hit that has Item 1.01 or 8.01 markers in title/body.
    def _hit_date(h):
        src = h.get("source") or {}
        return str(src.get("event_date") or src.get("filed_at") or "")
    hits_sorted = sorted(hits, key=_hit_date, reverse=True)

    extracted: dict[str, Any] = {}
    source_ref = ""
    for h in hits_sorted:
        ref = h.get("id") or (h.get("source") or {}).get("id") or ""
        if not ref:
            continue
        ft = _do_get_full_text(ref, max_chars=24_000)
        if not ft.get("found"):
            continue
        body = (ft.get("source") or {}).get("body") or ""

        ratio_m = _MA_RATIO_RE.search(body)
        equity_m = _MA_EQUITY_RE.search(body)
        ev_m = _MA_EV_RE.search(body)
        pps_m = _MA_PPS_RE.search(body)

        if not (ratio_m or equity_m or ev_m or pps_m):
            continue

        source_ref = ref
        if ratio_m:
            extracted["share_ratio"] = float(ratio_m.group(1))
            extracted["acquirer"] = ratio_m.group(2).strip()
            extracted["share_ratio_text"] = ratio_m.group(0).strip()
        if pps_m and "share_ratio" not in extracted:
            extracted["price_per_share_usd"] = float(pps_m.group(1))
        if equity_m:
            val = float(equity_m.group(1).replace(",", ""))
            mult = 1_000_000_000 if equity_m.group(2).lower().startswith("b") else 1_000_000
            extracted["equity_value_usd"] = val * mult
            extracted["equity_value_label"] = f"${val:.2f} {equity_m.group(2)}"
        if ev_m:
            val = float(ev_m.group(1).replace(",", ""))
            mult = 1_000_000_000 if ev_m.group(2).lower().startswith("b") else 1_000_000
            extracted["enterprise_value_usd"] = val * mult
            extracted["enterprise_value_label"] = f"${val:.2f} {ev_m.group(2)}"
        break

    if not extracted:
        return {
            "target_ticker": t,
            "error": (
                "no deal terms extracted via regex. Fall back to "
                "get_full_text on the most-recent 8-K and quote the "
                "deal-terms paragraph directly."
            ),
            "filings_found": [
                {
                    "ref": h.get("id") or (h.get("source") or {}).get("id") or "",
                    "event_date": _hit_date(h),
                }
                for h in hits_sorted[:6]
            ],
        }

    parts: list[str] = []
    if "share_ratio" in extracted:
        acq = extracted.get("acquirer", "[acquirer]")
        parts.append(
            f"- Share ratio: **{extracted['share_ratio']} shares of {acq} "
            f"per share of {t}**"
        )
    if "price_per_share_usd" in extracted:
        parts.append(f"- Price per share: **${extracted['price_per_share_usd']:.2f}**")
    if "equity_value_label" in extracted:
        parts.append(f"- Equity value: **{extracted['equity_value_label']}**")
    if "enterprise_value_label" in extracted:
        parts.append(f"- Enterprise value: **{extracted['enterprise_value_label']}**")

    answer_summary_block = (
        f"**{t} acquisition terms** (per 8-K `{source_ref}`):\n\n"
        + "\n".join(parts)
    )

    return _apply_binding(bind_as, {
        "target_ticker": t,
        "source_ref": source_ref,
        "extracted": extracted,
        "answer_summary_block": answer_summary_block,
    })
