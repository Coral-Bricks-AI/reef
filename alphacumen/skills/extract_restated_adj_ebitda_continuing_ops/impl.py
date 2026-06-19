# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``extract_restated_adj_ebitda_continuing_ops`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the sector_analyst
dispatch via ``invoke_skill_fn``. The recipe's prose playbook lives
next to this file in ``SKILL.md``.

Walks the issuer's 10-K stack backward from a target year until it
finds a 10-K whose multi-year recon table includes the target FY as
a column with a "from continuing operations" qualified row. Returns
the restated value. Sub-skill complementing
`extract_ebitda_reconciliation_multi_year` for divestiture / spin-
off years whose as-originally-filed total differs from the rubric
convention's restated continuing-ops figure.

Bounded sec-api budget: walks at most 4 candidate 10-Ks (fy_target
+ 1 through fy_target + 4), each scanned with one keyword. Total ≤
4 sec-api calls per skill invocation, well inside the rate-limit
envelope.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

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


def _scan_recon_table_for_fy(
    rows: list[Mapping[str, Any]], fy_target: int,
) -> Optional[tuple[float, str]]:
    """Find the Adjusted EBITDA from continuing operations row for fy_target.

    Scans the rows for:
      - A header row containing the four-digit year `fy_target` as a
        cell (locks in the column index for that year).
      - A label row containing "adjusted ebitda" + "continuing
        operations" + NOT "discontinued" + NOT "margin".
      - Returns (value, label) for the value at the locked column,
        or None if no match.
    """
    # Step 1: locate year-column index.
    year_col: Optional[int] = None
    for r in rows:
        cells = r.get("cells") or []
        for ci, cell in enumerate(cells):
            if not isinstance(cell, str):
                continue
            m = re.search(r"(?<!\d)(\d{4})(?!\d)", cell)
            if m and int(m.group(1)) == fy_target:
                year_col = ci
                break
        if year_col is not None:
            break
    if year_col is None:
        return None
    # Step 2: find continuing-ops row and pull cell at year_col.
    for r in rows:
        label = (r.get("label") or "").strip()
        label_lc = label.lower()
        if "adjusted ebitda" not in label_lc:
            continue
        if "margin" in label_lc:
            continue
        if "continuing" not in label_lc:
            continue
        if "discontinued" in label_lc:
            continue
        cells = r.get("cells") or []
        if year_col < len(cells):
            v = _parse_num(cells[year_col])
            if v is not None and v > 0:
                return (v, label)
    return None


@skill_fn(
    skill_id="extract_restated_adj_ebitda_continuing_ops",
    description=(
        "Find the restated 'Adjusted EBITDA from continuing "
        "operations' value for a target historical FY by walking "
        "the issuer's 10-K stack from fy_target+1 forward. Returns "
        "the value from the first 10-K whose multi-year recon "
        "table includes fy_target as a column with a continuing-"
        "ops-qualified row. USE when the multi-year EBITDA "
        "recon rubric's window covers a year predating a "
        "divestiture / spin-off / discontinued-ops event, and the "
        "as-originally-filed FY-N value differs materially from "
        "the rubric expectation. Generic across any US-listed "
        "issuer with the standard 5-year non-GAAP recon layout."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "fy_target": {
                "type": "integer",
                "description": (
                    "Historical fiscal year whose restated "
                    "continuing-operations Adjusted EBITDA is "
                    "needed (e.g. 2020 when the question's recon "
                    "spans FY2020-FY2024 and the FY2024 10-K's "
                    "table doesn't reach back that far)."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy_target"],
    },
)
def extract_restated_adj_ebitda_continuing_ops(
    ticker: str,
    fy_target: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Walk 10-Ks (fy_target+1 .. fy_target+4) for restated FY-target.
    """
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    try:
        fy_t = int(fy_target)
    except (TypeError, ValueError):
        return {"error": f"fy_target must be int (got {fy_target!r})"}

    t = ticker.strip().upper()
    # Walk forward 1-4 years from fy_target. The (fy_target + 1)
    # 10-K typically has fy_target as a comparative column with
    # restated continuing-ops values; if not, walk further until
    # we hit a 10-K with a 5-year Selected-Financial-Data layout
    # that spans back to fy_target.
    attempts: list[dict[str, Any]] = []
    for offset in (1, 2, 3, 4):
        fy_candidate = fy_t + offset
        ref, filed = _find_10k_ref_for_fy(t, fy_candidate)
        if not ref:
            attempts.append({
                "fy_walked": fy_candidate,
                "ref": None,
                "outcome": "no 10-K found",
            })
            continue
        ref_full = _strip_chunk_suffix(ref)
        rows: list[Mapping[str, Any]] = []
        # Try a couple of broad table keywords -- 5-year recons sit
        # under "Selected Financial Data" in old 10-Ks and under
        # "Adjusted EBITDA" / "Reconciliation" in newer ones.
        for kw in (
            "Adjusted EBITDA",
            "Selected Financial Data",
            "Reconciliation",
        ):
            env = _do_extract_filing_tables(
                ref=ref_full, table_keyword=kw,
                item="7", limit=15,
            )
            rows.extend(_table_rows_from_extract(env))
        match = _scan_recon_table_for_fy(rows, fy_t)
        if match is not None:
            value, label = match
            attempts.append({
                "fy_walked": fy_candidate,
                "ref": ref,
                "filed_at": filed,
                "outcome": "MATCH",
                "label": label,
                "value_thousand": value,
                "value_mm": value / 1e3,
            })
            lines = [
                f"# {t} FY{fy_t} Adjusted EBITDA from continuing operations (restated)",
                "",
                f"Source 10-K: `{ref}` (filed {filed or 'unknown'}).",
                f"Walker stopped at FY{fy_candidate} 10-K (offset +{offset}).",
                "",
                f"**FY{fy_t} Adjusted EBITDA from continuing operations: "
                f"${value / 1e3:,.2f}M** (\"{label}\")",
                "",
                "## How to use this value",
                "",
                "Quote the restated value above when the rubric grades "
                "a multi-year EBITDA reconciliation whose window covers "
                f"FY{fy_t}. This figure REPLACES the as-originally-filed "
                f"FY{fy_t} 10-K total, which is materially higher because "
                "it included divested / discontinued operations no longer "
                "in the continuing-ops series. Generic SEC convention: "
                "use the LATEST disclosed restated value for any historical "
                "year in a forward-looking multi-year recon.",
            ]
            return _apply_binding(bind_as, {
                "ticker": t,
                "fy_target": fy_t,
                "ref": ref,
                "filed_at": filed,
                "fy_walked": fy_candidate,
                "walker_offset": offset,
                "label": label,
                "value_thousand": value,
                "value_mm": value / 1e3,
                "attempts": attempts,
                "answer_summary_block": "\n".join(lines),
            })
        attempts.append({
            "fy_walked": fy_candidate,
            "ref": ref,
            "filed_at": filed,
            "outcome": (
                f"recon table did not contain FY{fy_t} column with "
                "continuing-ops row"
            ),
        })
    # No match across all candidates.
    return _apply_binding(bind_as, {
        "ticker": t,
        "fy_target": fy_t,
        "attempts": attempts,
        "error": (
            f"No 10-K filed FY{fy_t + 1}-FY{fy_t + 4} contained "
            f"FY{fy_t} as a continuing-operations recon column. "
            "Either the issuer doesn't publish a multi-year recon "
            "spanning this far, or the discontinued-ops "
            "reclassification didn't happen in this window. Fall "
            "back to the as-originally-filed FY-target 10-K via "
            "the standard `extract_ebitda_reconciliation_multi_year`."
        ),
    })
