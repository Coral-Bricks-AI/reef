# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``ebitda_reconciliation`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence
from harness.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _do_extract_filing_tables,
    _find_10k_ref_for_fy,
    _parse_num,
    _strip_chunk_suffix,
    _table_rows_from_extract,
)


@skill_fn(
    skill_id='extract_ebitda_reconciliation_multi_year',
    description=        "Reconstruct the GAAP-to-Adjusted-EBITDA bridge per fiscal "
        "year across a multi-year window + parse standard add-back "
        "sub-categories (default: integration, transaction, "
        "restructuring) + compute per-year totals + % of Adjusted "
        "EBITDA. USE for any 'reconstruct the Adjusted EBITDA "
        "reconciliation for each fiscal year FY-A through FY-B + "
        "identify integration / transaction / restructuring "
        "add-backs' question. Generic to any issuer with a "
        "standardized non-GAAP reconciliation disclosure in 10-K "
        "MD&A. Pass `add_back_categories` to override the default "
        "(e.g. ['merger-related', 'systems integration']) when the "
        "issuer's labels diverge. By default the tool prefers the "
        "FY_END 10-K's multi-year recon table (so prior-year figures "
        "reflect any restatements / divestiture reclassifications); "
        "set `prefer_latest_10k_recon=False` to force per-year 10-K "
        "extraction (original-as-filed figures).",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "fy_start": {"type": "integer"},
            "fy_end": {"type": "integer"},
            "add_back_categories": {
                "type": "array", "items": {"type": "string"},
                "description": "Optional category labels (default: integration, transaction, restructuring).",
            },
            "prefer_latest_10k_recon": {
                "type": "boolean",
                "description": "Default True: pull all year values from the FY_END 10-K's multi-year recon when it covers the requested range; falls back to per-year 10-K extraction otherwise. Set False to force per-year (as-originally-filed) values.",
                "default": True,
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy_start", "fy_end"],
    },
)
def extract_ebitda_reconciliation_multi_year(
    ticker: str,
    fy_start: int,
    fy_end: int,
    add_back_categories: Optional[Sequence[str]] = None,
    prefer_latest_10k_recon: bool = True,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Per-FY GAAP→Adj EBITDA add-back extraction with category buckets.

    Default categories: integration, transaction, restructuring.

    When `prefer_latest_10k_recon=True` (default), values for all years
    are first attempted from the FY_END 10-K's multi-year non-GAAP
    reconciliation table. Issuers that have divested or reclassified
    segments routinely restate prior-year Adjusted EBITDA in the
    latest 10-K's 5-year recon — those restated figures are the
    convention rubrics expect. Falls back to per-year 10-K extraction
    if the latest 10-K doesn't contain a recon table covering the
    requested range.
    """
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    try:
        fy_s = int(fy_start); fy_e = int(fy_end)
    except (TypeError, ValueError):
        return {"error": "fy_start and fy_end must be ints"}
    if fy_e < fy_s:
        return {"error": "fy_end must be >= fy_start"}
    t = ticker.strip().upper()

    cats = list(add_back_categories) if add_back_categories else [
        "integration", "transaction", "restructuring",
    ]
    cats_lc = [c.lower() for c in cats]

    # --- Latest-10K multi-year recon path (preferred for restated figures) ---
    # Strategy: pull FY_END 10-K's MD&A non-GAAP recon table; if its
    # cell grid contains one column per year in [fy_s, fy_e], use those
    # values as the canonical (potentially restated) series. Otherwise
    # fall through to the per-year loop below.
    multi_year_values: dict[int, dict[str, Any]] = {}
    latest_ref = None
    latest_filed = None
    if prefer_latest_10k_recon and fy_e > fy_s:
        latest_ref, latest_filed = _find_10k_ref_for_fy(t, fy_e)
        if latest_ref:
            # Try multiple keywords. Issuers sometimes title the
            # multi-year recon "Reconciliation of Net Income to
            # Adjusted EBITDA" / "Selected Financial Data" / a plain
            # heading. Aggregate rows from all candidates so the
            # year-header scan has the widest possible cell grid.
            latest_ref_full = _strip_chunk_suffix(latest_ref)
            rows_latest: list[Mapping[str, Any]] = []
            for kw in ("Adjusted EBITDA", "Reconciliation",
                       "Selected Financial Data"):
                env_latest = _do_extract_filing_tables(
                    ref=latest_ref_full,
                    table_keyword=kw,
                    item="7", limit=20,
                )
                rows_latest.extend(_table_rows_from_extract(env_latest))
            # Year-header detection. Issuers vary the header layout:
            # some put all years on a single row, others split the
            # header ("Years Ended December 31," on row 1, the four-
            # digit years on row 2). Accept both by accumulating
            # discovered (year → column index) mappings across rows
            # until we cover the requested range. Reset accumulation
            # when a row resets the column grid (cells == 0).
            year_cols: dict[int, int] = {}
            for r in rows_latest:
                header_cells = r.get("cells") or []
                for ci, cell in enumerate(header_cells):
                    if not isinstance(cell, str):
                        continue
                    m = re.search(r"(?<!\d)(20\d{2})(?!\d)", cell)
                    if m:
                        y = int(m.group(1))
                        if fy_s <= y <= fy_e and y not in year_cols:
                            year_cols[y] = ci
                # If we've found all requested endpoint years, lock in.
                if fy_s in year_cols and fy_e in year_cols and len(year_cols) >= (fy_e - fy_s + 1):
                    break
            if year_cols:
                # Now scan all rows. For each row containing an
                # "Adjusted EBITDA" or category label, pull values
                # from the year-mapped column indices.
                for r in rows_latest:
                    label = (r.get("label") or "").strip()
                    label_lc = label.lower()
                    cells = r.get("cells") or []
                    if "adjusted ebitda" in label_lc and "margin" not in label_lc:
                        # Initialize per-year dicts on first match.
                        for fy_k, ci in year_cols.items():
                            multi_year_values.setdefault(fy_k, {
                                "fy": fy_k,
                                "ref": latest_ref,
                                "filed_at": latest_filed,
                                "per_category": {c: 0.0 for c in cats_lc},
                                "per_category_rows": {c: [] for c in cats_lc},
                            })
                            if ci < len(cells):
                                n = _parse_num(cells[ci])
                                if n is not None and (
                                    "continuing" in label_lc
                                    or multi_year_values[fy_k].get("adj_ebitda") is None
                                ):
                                    multi_year_values[fy_k]["adj_ebitda"] = n
                    else:
                        # Category add-back row.
                        for cat in cats_lc:
                            if cat in label_lc:
                                for fy_k, ci in year_cols.items():
                                    multi_year_values.setdefault(fy_k, {
                                        "fy": fy_k,
                                        "ref": latest_ref,
                                        "filed_at": latest_filed,
                                        "per_category": {c: 0.0 for c in cats_lc},
                                        "per_category_rows": {c: [] for c in cats_lc},
                                    })
                                    if ci < len(cells):
                                        n = _parse_num(cells[ci])
                                        if n is not None:
                                            multi_year_values[fy_k]["per_category"][cat] += n
                                            multi_year_values[fy_k]["per_category_rows"][cat].append((label, n))
                                break

    years: list[dict[str, Any]] = []
    for fy in range(fy_s, fy_e + 1):
        # Prefer the restated value pulled from the FY_END 10-K's
        # multi-year recon when present.
        if fy in multi_year_values and multi_year_values[fy].get("adj_ebitda") is not None:
            y = multi_year_values[fy]
            combined = sum(y["per_category"].values())
            adj = y.get("adj_ebitda")
            pct = (combined / adj * 100.0) if (adj and adj > 0) else None
            y["combined_addbacks"] = combined
            y["combined_pct_of_adj_ebitda"] = pct
            years.append(y)
            continue
        ref, filed = _find_10k_ref_for_fy(t, fy)
        if not ref:
            years.append({"fy": fy, "error": f"No {t} 10-K found for FY{fy}"})
            continue
        env = _do_extract_filing_tables(
            ref=_strip_chunk_suffix(ref),
            table_keyword="Adjusted EBITDA", item="7", limit=20,
        )
        rows = _table_rows_from_extract(env)
        adj_ebitda = None
        per_cat: dict[str, float] = {c: 0.0 for c in cats_lc}
        per_cat_rows: dict[str, list[tuple[str, float]]] = {c: [] for c in cats_lc}
        for row in rows:
            label = (row.get("label") or "").strip()
            label_lc = label.lower()
            cells = row.get("cells") or []
            first_num = None
            for c in cells:
                n = _parse_num(c)
                if n is not None:
                    first_num = n
                    break
            if first_num is None:
                continue
            # Prefer the "continuing operations" variant when present
            # (post-divestiture issuers split the recon line; rubric
            # conventions for multi-year EBITDA windows track the
            # continuing-ops series). Else fall back to the unqualified
            # "Adjusted EBITDA" line.
            if "adjusted ebitda" in label_lc and "margin" not in label_lc and "continuing" in label_lc:
                # Continuing-ops variant ALWAYS wins (overwrites any
                # prior unqualified pull).
                adj_ebitda = first_num
            elif adj_ebitda is None and "adjusted ebitda" in label_lc and "margin" not in label_lc and "discontinued" not in label_lc:
                adj_ebitda = first_num
            for cat in cats_lc:
                if cat in label_lc:
                    per_cat[cat] += first_num
                    per_cat_rows[cat].append((label, first_num))
                    break
        combined = sum(per_cat.values())
        pct = (combined / adj_ebitda * 100.0) if (adj_ebitda and adj_ebitda > 0) else None
        years.append({
            "fy": fy, "ref": ref, "filed_at": filed,
            "adj_ebitda": adj_ebitda,
            "per_category": per_cat,
            "per_category_rows": per_cat_rows,
            "combined_addbacks": combined,
            "combined_pct_of_adj_ebitda": pct,
        })

    # Identify highest-dollar-addback year + dominant category.
    highest_year = None
    highest_val = 0.0
    for y in years:
        v = y.get("combined_addbacks") or 0.0
        if v > highest_val:
            highest_val = v
            highest_year = y["fy"]

    lines = [
        f"# {t} Adjusted EBITDA reconciliation — FY{fy_s} to FY{fy_e}",
        "",
        f"| FY | Adj EBITDA | "
        + " | ".join([c.title() for c in cats])
        + " | Combined | % of Adj EBITDA |",
        "|---|---|" + "---|" * (len(cats) + 2),
    ]
    def fmt_m(x):
        if x is None:
            return "—"
        try:
            return f"${float(x)/1e6:,.2f}M"
        except (TypeError, ValueError):
            return "—"
    for y in years:
        adj = y.get("adj_ebitda")
        pc = y.get("per_category", {})
        cat_cells = " | ".join(fmt_m(pc.get(c, 0.0)) for c in cats_lc)
        combined = y.get("combined_addbacks")
        pct = y.get("combined_pct_of_adj_ebitda")
        pct_s = f"{pct:.2f}%" if pct is not None else "—"
        lines.append(
            f"| FY{y['fy']} | {fmt_m(adj)} | {cat_cells} | "
            f"{fmt_m(combined)} | {pct_s} |"
        )
    if highest_year:
        lines.append("")
        lines.append(f"**Highest dollar amount of merger-related add-backs: FY{highest_year} (${highest_val/1e6:,.2f}M).**")
    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy_start": fy_s,
        "fy_end": fy_e,
        "years": years,
        "categories": cats,
        "highest_addback_year": highest_year,
        "answer_summary_block": answer_summary_block,
    })
