# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``multi_issuer_capex_guidance`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence
from harness.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _do_bm25_sec,
    _do_get_full_text,
)


@skill_fn(
    skill_id='compute_fy_capex_guidance_multi',
    description=        "Pull each issuer's FY capex guidance from their most recent "
        "Q4 FY-1 earnings 8-K and rank highest-to-lowest. USE THIS for "
        "multi-issuer FY-capex-plan questions ('who plans to spend the "
        "most on capex in [FY]: A, B, or C'). Internally fans across the "
        "ticker list — one bm25_sec(form_type:'8-K') per ticker, then "
        "get_full_text + regex on the capex sentence — and returns one "
        "pre-composed `answer_summary_block` (ranked table + leader "
        "callout). When the regex fails to extract a number, the entry's "
        "`source_text` carries the surrounding paragraph for the model "
        "to quote directly. Caps cost at ~2 tool calls per ticker.",
    parameters=               {
        "type": "object",
        "properties": {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of issuer tickers (2+). Case-insensitive. Each "
                    "runs an independent bm25_sec + get_full_text + regex "
                    "pipeline."
                ),
            },
            "fy": {
                "type": "integer",
                "description": (
                    "Target fiscal year for the capex guidance. The tool "
                    "searches Q4 FY-1 earnings 8-Ks for guidance covering "
                    "this FY."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["tickers", "fy"],
    },
)
def compute_fy_capex_guidance_multi(
    tickers: Sequence[str],
    fy: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """For each ticker, locate the most recent Q4 FY-1 earnings 8-K and
    extract any explicit FY capex guidance (dollar value or range).

    Returns a ranked table (highest first) plus per-ticker source refs.
    Uses a simple regex to extract dollar values from the 8-K full text;
    when the regex finds no number, the entry is flagged as ``source_text``
    so the model can quote the relevant passage directly.
    """
    if not tickers:
        return {"error": "tickers list required (e.g. ['XOM','CVX'])"}
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": f"fy must be int; got {fy!r}"}

    # Look for Q4 FY-1 earnings 8-K, which contains FY guidance.
    fy_prior = fy_int - 1

    # Regex patterns for "$XX billion" / "$XXX million" capex context.
    capex_re = re.compile(
        r"(?:capital\s+(?:expenditure|investment|expenditures?)|capex)"
        r"[^.]{0,300}?\$\s*([\d,]+\.?\d*)\s*(billion|million|B|M|bn|mn)\b",
        re.IGNORECASE,
    )
    # Implied-guidance pattern — issuers often skip an explicit FY
    # dollar figure and instead say "we expect FY capex to be
    # reasonably consistent with our Q4 run-rate". When the
    # explicit-figure regex misses, scan for this phrasing and a
    # Q4-run-rate dollar value, then annualize × 4.
    implied_run_rate_re = re.compile(
        r"(?:reasonably\s+consistent\s+with|consistent\s+with|approximately|"
        r"approximate|in\s+line\s+with|similar\s+to)\s+(?:our\s+|the\s+)?"
        r"(?:Q4|fourth[\-\s]?quarter|4Q)\s*(?:20\d\d\s*)?(?:capex\s+)?"
        r"(?:capital\s+(?:expenditure|investment|expenditures?)\s+)?"
        r"run[\-\s]?rate"
        r"|"
        r"(?:Q4|fourth[\-\s]?quarter|4Q)\s*(?:20\d\d\s*)?(?:capex\s+)?"
        r"(?:capital\s+(?:expenditure|investment|expenditures?)\s+)?"
        r"(?:of|was|reached|totaled)\s*\$\s*([\d,]+\.?\d*)\s*(billion|million|B|M|bn|mn)\b",
        re.IGNORECASE,
    )

    per_ticker: list[dict[str, Any]] = []
    for raw_t in tickers:
        t = raw_t.strip().upper()
        if not t:
            continue
        # Find Q4 FY-1 earnings 8-K (Q4 calendar issuers file in late Jan/Feb).
        env = _do_bm25_sec(
            query="full year capital expenditure guidance",
            k=6,
            filters={
                "form_type": "8-K",
                "ticker": t,
                "filed_at_gte": f"{fy_prior + 1}0101",
                "filed_at_lte": f"{fy_int}0501",
            },
        )
        hits = env.get("hits") or []
        if not hits:
            per_ticker.append({
                "ticker": t, "fy_capex_usd": None, "raw_match": None,
                "source_ref": None,
                "error": (
                    f"no 8-K hits in {fy_prior + 1}-01-01 to {fy_int}-05-01 "
                    f"window for {t}"
                ),
            })
            continue
        top = hits[0]
        ref = top.get("id") or (top.get("source") or {}).get("id") or ""
        # Pull full text, search for capex guidance language.
        ft = _do_get_full_text(ref, max_chars=24_000)
        body = ((ft.get("source") or {}).get("body") or "") if ft.get("found") else ""
        m = capex_re.search(body)
        if not m:
            # Try the implied-Q4-run-rate fallback. Some issuers skip
            # an explicit FY dollar figure and instead anchor the
            # next-year guidance on the trailing Q4 run-rate. When we
            # find both the "consistent with Q4 run-rate" phrasing AND
            # a Q4 capex dollar value (either in the same 8-K or by
            # searching the FY-1 10-K Cash Flow statement), annualize × 4.
            run_rate_match = implied_run_rate_re.search(body)
            if run_rate_match and any(g for g in run_rate_match.groups() if g):
                groups = [g for g in run_rate_match.groups() if g]
                val_str = groups[0] if groups else None
                unit = groups[1].lower() if len(groups) > 1 and groups[1] else None
                if val_str and unit:
                    try:
                        q4_val = float(val_str.replace(",", ""))
                        # Normalise unit to USD billions.
                        if unit in ("billion", "b", "bn"):
                            q4_usd_b = q4_val
                        elif unit in ("million", "m", "mn"):
                            q4_usd_b = q4_val / 1000.0
                        else:
                            q4_usd_b = None
                        if q4_usd_b is not None:
                            implied_fy = q4_usd_b * 4
                            per_ticker.append({
                                "ticker": t,
                                "fy_capex_usd": implied_fy * 1_000_000_000,
                                "fy_capex_display_b": round(implied_fy, 2),
                                "raw_match": f"Q4 run-rate ${q4_usd_b:.2f}B × 4",
                                "source_ref": ref,
                                "guidance_basis": "implied_q4_run_rate",
                                "note": (
                                    f"No explicit FY{fy_int} capex guidance "
                                    f"figure; using stated Q4 {fy_prior} "
                                    f"run-rate of ${q4_usd_b:.2f}B "
                                    f"annualized (× 4) = ${implied_fy:.1f}B."
                                ),
                            })
                            continue
                    except (ValueError, IndexError):
                        pass
            per_ticker.append({
                "ticker": t,
                "fy_capex_usd": None,
                "raw_match": None,
                "source_ref": ref,
                "source_text": body[:600],
                "error": (
                    f"capex-guidance regex did not match in 8-K {ref}; "
                    "model should get_full_text and quote the relevant "
                    "paragraph directly."
                ),
            })
            continue
        val_str, unit = m.group(1), m.group(2).lower()
        try:
            val = float(val_str.replace(",", ""))
        except ValueError:
            per_ticker.append({
                "ticker": t, "fy_capex_usd": None,
                "raw_match": m.group(0)[:200], "source_ref": ref,
                "error": f"could not parse {val_str!r} as float",
            })
            continue
        multiplier = 1_000_000_000 if unit.startswith("b") else 1_000_000
        per_ticker.append({
            "ticker": t,
            "fy_capex_usd": val * multiplier,
            "fy_capex_label": f"${val:.1f} {unit}",
            "raw_match": m.group(0).strip(),
            "source_ref": ref,
        })

    ranked = sorted(
        per_ticker,
        key=lambda r: (-(r.get("fy_capex_usd") or -1.0), r["ticker"]),
    )

    def _fmt_usd(v):
        if v is None:
            return "n/a"
        if v >= 1_000_000_000:
            return f"${v / 1_000_000_000:.1f}B"
        return f"${v / 1_000_000:.0f}M"

    rows = [f"| Rank | Ticker | FY{fy_int} Capex |", "|---|---|---|"]
    for i, r in enumerate(ranked, 1):
        rows.append(f"| {i} | {r['ticker']} | {_fmt_usd(r.get('fy_capex_usd'))} |")
    table = "\n".join(rows)

    leader = next((r for r in ranked if r.get("fy_capex_usd") is not None), None)
    leader_line = (
        f"**{leader['ticker']} guided the highest FY{fy_int} capex at "
        f"{_fmt_usd(leader['fy_capex_usd'])}**." if leader else
        "**No capex guidance values parsed; see per-ticker `source_ref`.**"
    )

    answer_summary_block = (
        f"**FY{fy_int} capital expenditure guidance:**\n\n{table}\n\n{leader_line}"
    )

    return _apply_binding(bind_as, {
        "fy": fy_int,
        "tickers": [t.strip().upper() for t in tickers if t.strip()],
        "guidance": ranked,
        "ranked_descending": [r["ticker"] for r in ranked],
        "formatted_table": table,
        "answer_summary_block": answer_summary_block,
    })
