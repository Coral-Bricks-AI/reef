# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tiny BM25 over the in-repo cocktails corpus."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from harness.skill_fn import skill_fn

_DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cocktails.json"
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _doc_for(cocktail: dict[str, Any]) -> str:
    """Concatenate the searchable fields into one tokenizable string."""
    return " ".join(
        [
            cocktail["name"],
            " ".join(cocktail.get("tags", [])),
            " ".join(ing["name"] for ing in cocktail.get("ingredients", [])),
        ]
    )


with _DATA_PATH.open(encoding="utf-8") as _f:
    _COCKTAILS: list[dict[str, Any]] = json.load(_f)
_DOC_TOKENS: list[list[str]] = [_tokenize(_doc_for(c)) for c in _COCKTAILS]
_DOC_LEN: list[int] = [len(t) for t in _DOC_TOKENS]
_AVG_DL: float = sum(_DOC_LEN) / max(len(_DOC_LEN), 1)
_DF: dict[str, int] = {}
for _tokens in _DOC_TOKENS:
    for _term in set(_tokens):
        _DF[_term] = _DF.get(_term, 0) + 1


def _bm25(query: str, k: int = 5, k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
    q_terms = _tokenize(query)
    N = len(_COCKTAILS)
    scored: list[tuple[int, float]] = []
    for i, tokens in enumerate(_DOC_TOKENS):
        if not tokens:
            continue
        score = 0.0
        dl = _DOC_LEN[i]
        for term in q_terms:
            df = _DF.get(term, 0)
            if df == 0:
                continue
            tf = tokens.count(term)
            if tf == 0:
                continue
            idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
            denom = tf + k1 * (1 - b + b * dl / max(_AVG_DL, 1e-9))
            score += idf * (tf * (k1 + 1)) / denom
        if score > 0:
            scored.append((i, score))
    scored.sort(key=lambda x: -x[1])
    return scored[:k]


@skill_fn(
    skill_id="search_cocktails",
    description=(
        "Rank cocktails by BM25 relevance to a free-text query over "
        "cocktail name + tags + ingredient names."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text query (e.g. 'gin citrus', 'negroni', 'italian aperitivo').",
            },
            "k": {
                "type": "integer",
                "description": "Max results to return (default 5).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
)
def search_cocktails(*, query: str, k: int = 5) -> dict[str, Any]:
    hits = _bm25(query, k=k)
    results = []
    for idx, score in hits:
        c = _COCKTAILS[idx]
        results.append(
            {
                "id": c["id"],
                "name": c["name"],
                "score": round(score, 3),
                "tags": c.get("tags", []),
                "ingredient_names": [ing["name"] for ing in c.get("ingredients", [])],
            }
        )
    return {"query": query, "results": results}
