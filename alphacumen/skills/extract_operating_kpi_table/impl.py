# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``operating_kpi_enumeration`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Optional
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _OPERATING_KPI_KEYWORDS,
    _apply_binding,
    _do_bm25_sec,
    _do_extract_filing_tables,
)


@skill_fn(
    skill_id='extract_operating_kpi_table',
    description=        "Find the issuer's MD&A operating-statistics / KPI table in the "
        "FY 10-K and dump every row as a canonical bullet list. USE THIS "
        "for 'list the operating KPIs [issuer] tracked in FY [X]' or "
        "'what operational metrics does [issuer] report' questions. "
        "Internally chains bm25_sec(form_type:'10-K') → extract_filing_tables "
        "with the seven known section-name variants (Operating Statistics, "
        "Key Operating Metrics, Selected Operating Data, etc.) and "
        "returns the largest matching table. Replaces ~3-5 model rounds "
        "of trial-and-error keyword guessing.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Issuer ticker. Case-insensitive."},
            "fy": {"type": "integer", "description": "Fiscal year of interest."},
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy"],
    },
)
def extract_operating_kpi_table(
    ticker: str,
    fy: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Find the issuer's MD&A operating-statistics table in the FY 10-K
    and dump every row as a canonical bullet list.

    Tries known keyword variants in priority order; returns the first
    table with ≥3 data rows.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return {"error": "ticker required"}
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": f"fy must be int; got {fy!r}"}

    env = _do_bm25_sec(
        query="operating statistics key metrics MD&A",
        k=5,
        filters={
            "form_type": "10-K",
            "ticker": t,
            "event_date_gte": f"{fy_int}0101",
            "event_date_lte": f"{fy_int + 1}0930",
        },
    )
    hits = env.get("hits") or []
    if not hits:
        return {"ticker": t, "fy": fy_int,
                "error": f"no 10-K hits for {t} FY{fy_int}"}

    def _hit_date(h):
        src = h.get("source") or {}
        return str(src.get("event_date") or src.get("filed_at") or "")
    hits_sorted = sorted(hits, key=_hit_date, reverse=True)
    top_ref = hits_sorted[0].get("id") or (hits_sorted[0].get("source") or {}).get("id") or ""

    best: dict[str, Any] = {}
    best_rows = 0
    for keyword in _OPERATING_KPI_KEYWORDS:
        tbl = _do_extract_filing_tables(ref=top_ref, table_keyword=keyword)
        tables = tbl.get("tables") or []
        for table in tables:
            rows = table.get("rows") or []
            if len(rows) >= 3 and len(rows) > best_rows:
                best = {"keyword": keyword, "table": table, "rows": rows}
                best_rows = len(rows)
        if best_rows >= 5:
            break

    if not best:
        return {
            "ticker": t, "fy": fy_int, "source_ref": top_ref,
            "error": (
                f"no Operating-Statistics table found via the {len(_OPERATING_KPI_KEYWORDS)} "
                f"keyword variants. Fall back to get_full_text on the 10-K "
                f"and read MD&A directly."
            ),
            "keywords_tried": list(_OPERATING_KPI_KEYWORDS),
        }

    bullet_lines: list[str] = []
    for row in best["rows"]:
        cells = row.get("cells") or row if isinstance(row, list) else (row.get("cells") or [])
        # cells could be list of strings or list of dicts; coerce.
        flat = [
            (c if isinstance(c, str) else (c.get("text") or "")).strip()
            for c in (cells if isinstance(cells, list) else [])
        ]
        flat = [c for c in flat if c]
        if not flat:
            continue
        if len(flat) == 1:
            bullet_lines.append(f"- {flat[0]}")
        else:
            bullet_lines.append(f"- **{flat[0]}**: " + " | ".join(flat[1:]))

    answer_summary_block = (
        f"**{t} FY{fy_int} Operating KPIs (per {best['keyword']} table, ref `{top_ref}`):**\n\n"
        + "\n".join(bullet_lines)
    )

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy": fy_int,
        "source_ref": top_ref,
        "keyword_used": best["keyword"],
        "row_count": best_rows,
        "rows": best["rows"],
        "answer_summary_block": answer_summary_block,
    })
