# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Volume-weighted ABV computation across a cocktail's ingredients."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reef.skill_fn import skill_fn

_DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cocktails.json"


with _DATA_PATH.open(encoding="utf-8") as _f:
    _COCKTAILS: list[dict[str, Any]] = json.load(_f)
_BY_ID: dict[str, dict[str, Any]] = {c["id"]: c for c in _COCKTAILS}


@skill_fn(
    skill_id="compute_alcohol_content",
    description=(
        "Compute the volume-weighted ABV of a cocktail by id. "
        "Returns ABV pct + a grader-ready answer_summary_block."
    ),
    parameters={
        "type": "object",
        "properties": {
            "cocktail_id": {
                "type": "string",
                "description": "The cocktail id from a search_cocktails result (e.g. 'negroni').",
            },
        },
        "required": ["cocktail_id"],
    },
)
def compute_alcohol_content(*, cocktail_id: str) -> dict[str, Any]:
    cocktail = _BY_ID.get(cocktail_id)
    if cocktail is None:
        return {
            "error": (
                f"unknown cocktail_id={cocktail_id!r}; "
                f"call search_cocktails first to get a valid id."
            )
        }
    total_vol = 0.0
    alcohol_vol = 0.0
    parts: list[str] = []
    for ing in cocktail.get("ingredients", []):
        vol = float(ing.get("volume_ml", 0))
        abv = float(ing.get("abv", 0))
        total_vol += vol
        alcohol_vol += vol * abv
        if abv > 0:
            parts.append(f"{vol:g}ml {ing['name']} @ {abv * 100:.0f}%")
        else:
            parts.append(f"{vol:g}ml {ing['name']}")
    abv_pct = (alcohol_vol / total_vol * 100) if total_vol > 0 else 0.0
    abv_pct = round(abv_pct, 1)
    summary_block = (
        f"**{cocktail['name']}** — build ABV ≈ **{abv_pct}%** "
        f"({' + '.join(parts)}; total {total_vol:g} ml). "
        f"Served ABV will be lower after ice dilution."
    )
    return {
        "cocktail_id": cocktail_id,
        "abv_pct": abv_pct,
        "ingredients_summary": " + ".join(parts),
        "answer_summary_block": summary_block,
    }
