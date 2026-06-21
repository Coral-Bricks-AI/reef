# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``note_diff_across_years`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Optional
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _do_extract_filing_tables,
    _find_10k_ref_for_fy,
    _parse_num,
    _table_rows_from_extract,
)


@skill_fn(
    skill_id='compute_note_diff_across_years',
    description=        "Find the named footnote / note (e.g. 'Acquisitions', "
        "'Restructuring', 'Goodwill', 'Intangible Assets', "
        "'Inventory') in both FY-A and FY-B 10-Ks for the issuer, "
        "line-align rows by label, and return a markdown diff table "
        "with delta + percentage-change columns. USE THIS for any "
        "'how did X change from FY-A to FY-B' note-level question — "
        "the tool handles the two-filing extraction + line alignment "
        "in one call. Returns `answer_summary_block` ready to drop "
        "verbatim, plus structured `rows` for downstream analysis.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "note_keyword": {
                "type": "string",
                "description": (
                    "Keyword that uniquely identifies the table in "
                    "the 10-K (e.g. 'Acquisitions', 'Restructuring', "
                    "'Purchase Price Allocation', 'Goodwill', "
                    "'Identifiable Intangible Assets'). Matched by "
                    "extract_filing_tables' keyword filter."
                ),
            },
            "fy_a": {
                "type": "integer",
                "description": "Earlier fiscal year (e.g. 2023).",
            },
            "fy_b": {
                "type": "integer",
                "description": "Later fiscal year (e.g. 2024).",
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "note_keyword", "fy_a", "fy_b"],
    },
)
def compute_note_diff_across_years(
    ticker: str,
    note_keyword: str,
    fy_a: int,
    fy_b: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Find the `note_keyword` table in FY-A and FY-B 10-Ks, diff."""
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    if not note_keyword or not isinstance(note_keyword, str):
        return {"error": "note_keyword required"}
    try:
        fy_a_int = int(fy_a); fy_b_int = int(fy_b)
    except (TypeError, ValueError):
        return {"error": "fy_a and fy_b must be ints"}
    t = ticker.strip().upper()
    ref_a, filed_a = _find_10k_ref_for_fy(t, fy_a_int)
    ref_b, filed_b = _find_10k_ref_for_fy(t, fy_b_int)
    if not ref_a or not ref_b:
        return {
            "error": (
                f"Could not locate both 10-Ks for {t}: "
                f"FY{fy_a_int}={ref_a or 'missing'}, "
                f"FY{fy_b_int}={ref_b or 'missing'}"
            )
        }
    env_a = _do_extract_filing_tables(
        ref=ref_a, table_keyword=note_keyword, limit=5,
    )
    env_b = _do_extract_filing_tables(
        ref=ref_b, table_keyword=note_keyword, limit=5,
    )
    rows_a = _table_rows_from_extract(env_a)
    rows_b = _table_rows_from_extract(env_b)
    if not rows_a or not rows_b:
        return {
            "ticker": t, "note_keyword": note_keyword,
            "fy_a": fy_a_int, "fy_b": fy_b_int,
            "ref_a": ref_a, "ref_b": ref_b,
            "error": (
                f"Table with keyword {note_keyword!r} not located in "
                f"one or both 10-Ks. Try `extract_filing_tables` "
                f"directly with alternative keywords."
            ),
            "tables_a_count": len(rows_a), "tables_b_count": len(rows_b),
        }
    # Index by normalized label.
    idx_a = {r["label"].lower().strip(): r for r in rows_a}
    idx_b = {r["label"].lower().strip(): r for r in rows_b}
    all_labels = []
    seen = set()
    for r in rows_a + rows_b:
        k = r["label"].lower().strip()
        if k not in seen:
            seen.add(k); all_labels.append(r["label"])
    diff_rows = []
    for lbl in all_labels:
        k = lbl.lower().strip()
        ra = idx_a.get(k); rb = idx_b.get(k)
        # Use the FIRST numeric cell as the canonical value.
        va = None; vb = None
        if ra and ra["cells"]:
            for c in ra["cells"]:
                va = _parse_num(c)
                if va is not None:
                    break
        if rb and rb["cells"]:
            for c in rb["cells"]:
                vb = _parse_num(c)
                if vb is not None:
                    break
        delta = (vb - va) if (va is not None and vb is not None) else None
        pct = (delta / va * 100) if (delta is not None and va not in (None, 0)) else None
        diff_rows.append({
            "label": lbl,
            f"fy{fy_a_int}": va, f"fy{fy_b_int}": vb,
            "delta": delta,
            "pct_change": pct,
        })

    # Markdown summary.
    lines = [
        f"# {t} — {note_keyword} delta (FY{fy_a_int} → FY{fy_b_int})",
        f"FY{fy_a_int} 10-K filed {filed_a}; FY{fy_b_int} 10-K filed {filed_b}.",
        "",
        f"| Line item | FY{fy_a_int} | FY{fy_b_int} | Δ | Δ% |",
        "|---|---|---|---|---|",
    ]
    for row in diff_rows:
        va = row[f"fy{fy_a_int}"]; vb = row[f"fy{fy_b_int}"]
        delta = row["delta"]; pct = row["pct_change"]
        def fmt(x):
            if x is None:
                return "—"
            return f"{x:,.2f}"
        pct_s = f"{pct:+.1f}%" if pct is not None else "—"
        lines.append(
            f"| {row['label']} | {fmt(va)} | {fmt(vb)} | {fmt(delta)} | {pct_s} |"
        )
    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "note_keyword": note_keyword,
        "fy_a": fy_a_int, "fy_b": fy_b_int,
        "ref_a": ref_a, "ref_b": ref_b,
        "filed_a": filed_a, "filed_b": filed_b,
        "rows": diff_rows,
        "answer_summary_block": answer_summary_block,
    })
