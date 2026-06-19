# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for ``alphacumen.capabilities`` -- schema fetching and rendering.

These tests cover the alphacumen-side glue that turns the gateway's typed
``IndexRegistrationRow.capabilities`` payload (slice 5d) into model-
facing tool description fragments. The platform-side validation +
:meth:`ToolKernel.list_tools` shape live in
``ml/platform/tests/test_index_capabilities.py``.

We keep this suite RPC-free: ``fetch_index_capabilities`` is exercised
by patching :func:`coralbricks.sandbox.tools.list_tools`. End-to-end
tests where a real swarm hits a real gateway live in
``test_swarm.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from alphacumen import capabilities as caps
from alphacumen import tools as ac_tools


# ---------------------------------------------------------------------------
# fetch_index_capabilities
# ---------------------------------------------------------------------------


def _list_tools_envelope(indices: list[dict[str, Any]]) -> dict[str, Any]:
    """Mimic the wire shape the gateway returns from ``tools.list``."""
    return {
        "tools": ["tools.bm25", "tools.list", "tools.ping"],
        "indices": indices,
        "unrestricted": False,
    }


def test_fetch_index_capabilities_indexes_by_slug() -> None:
    payload = _list_tools_envelope([
        {
            "slug": "news_v3",
            "description": None,
            "hardware": "cpu",
            "capabilities": {
                "bm25": {
                    "fields": [{"name": "title", "type": "text", "boost": 3}],
                    "default_fields": ["title^3"],
                }
            },
        },
        {"slug": "macro_v1", "capabilities": {"sql": {"tables": []}}},
    ])
    with patch("alphacumen.capabilities.cb_tools.list_tools", return_value=payload):
        out = caps.fetch_index_capabilities()

    assert set(out) == {"news_v3", "macro_v1"}
    assert out["news_v3"]["bm25"]["default_fields"] == ["title^3"]
    assert out["macro_v1"]["sql"] == {"tables": []}


def test_fetch_index_capabilities_swallows_rpc_errors() -> None:
    """Discovery hiccup must NOT block the swarm from launching --
    alphacumen falls back to the static tool descriptions instead."""
    def _boom() -> None:
        raise RuntimeError("gateway unreachable")

    with patch("alphacumen.capabilities.cb_tools.list_tools", side_effect=_boom):
        out = caps.fetch_index_capabilities()
    assert out == {}


def test_fetch_index_capabilities_skips_malformed_entries() -> None:
    payload = _list_tools_envelope([
        {"slug": "good", "capabilities": {"bm25": {"fields": []}}},
        {"slug": "", "capabilities": {}},  # blank slug
        {"capabilities": {"bm25": {}}},  # missing slug
        "not_a_dict",  # type: ignore[list-item]
        {"slug": "no_caps", "capabilities": "not_a_dict"},
        {"slug": "bad_inner", "capabilities": {"bm25": "not_a_dict"}},
    ])
    with patch("alphacumen.capabilities.cb_tools.list_tools", return_value=payload):
        out = caps.fetch_index_capabilities()
    assert set(out) == {"good", "bad_inner"}
    # The bad inner verb cfg is filtered out by the value-mapping check
    assert out["bad_inner"] == {}


# ---------------------------------------------------------------------------
# Per-verb renderers
# ---------------------------------------------------------------------------


def test_render_bm25_section_lists_fields_with_boost_and_desc() -> None:
    out = caps.render_bm25_section("news_v3", {
        "fields": [
            {"name": "title", "type": "text", "boost": 3,
             "description": "Article headline"},
            {"name": "body", "type": "text", "boost": 2.5},
            {"name": "url", "type": "keyword"},
        ]
    })
    assert "Available BM25 fields on `news_v3`" in out
    assert "`title` (text, default boost 3): Article headline" in out
    assert "`body` (text, default boost 2.5)" in out
    assert "`url` (keyword)" in out
    assert "ToolPolicyError" in out  # mentions the rejection contract


def test_render_bm25_section_empty_when_no_fields() -> None:
    assert caps.render_bm25_section("x", {"fields": []}) == ""
    assert caps.render_bm25_section("x", None) == ""
    assert caps.render_bm25_section("x", {}) == ""


def test_render_ann_section_with_embedder_and_filterables() -> None:
    out = caps.render_ann_section("scraped", {
        "embedder": "bge-m3", "vector_dim": 1024, "metric": "cosine",
        "filterable_fields": [
            {"name": "publish_date", "type": "date",
             "description": "ISO-8601"},
        ],
    })
    assert "embedder=`bge-m3`" in out
    assert "dim=1024" in out
    assert "metric=cosine" in out
    assert "Filterable fields on `scraped`" in out
    assert "`publish_date` (date): ISO-8601" in out


def test_render_sql_section_lists_tables_with_columns() -> None:
    out = caps.render_sql_section("macro", {
        "tables": [
            {"name": "brent", "description": "Daily Brent crude OHLC",
             "columns": [
                 {"name": "date", "type": "date"},
                 {"name": "close", "type": "double",
                  "description": "USD per barrel"},
             ]},
        ]
    })
    assert "Tables available via SQL on `macro`" in out
    assert "`brent` -- Daily Brent crude OHLC" in out
    assert "`date` (date)" in out
    assert "`close` (double) -- USD per barrel" in out


def test_render_multihop_section_lists_node_types_and_predicates() -> None:
    out = caps.render_multihop_section("graph", {
        "node_types": [
            {"name": "Entity", "description": "Persons / orgs"},
        ],
        "predicates": [
            {"name": "MENTIONED_IN", "description": "Entity in event"},
        ],
    })
    assert "Node types on `graph`" in out
    assert "`Entity`: Persons / orgs" in out
    assert "Edge predicates on `graph`" in out
    assert "predicate_filter" in out
    assert "`MENTIONED_IN`: Entity in event" in out


def test_render_get_section_lists_doc_schema() -> None:
    out = caps.render_get_section("docs", {
        "doc_schema": [
            {"name": "title", "type": "text"},
            {"name": "url", "type": "keyword",
             "description": "Canonical URL"},
        ]
    })
    assert "Fields returned by `tools.get` on `docs`" in out
    assert "`title` (text)" in out
    assert "`url` (keyword): Canonical URL" in out


# ---------------------------------------------------------------------------
# render_index_section -- per-index dispatcher
# ---------------------------------------------------------------------------


def test_render_index_section_returns_empty_when_slug_unknown() -> None:
    assert caps.render_index_section(
        "ghost", ("bm25",), capabilities={"news_v3": {}},
    ) == ""


def test_render_index_section_concatenates_requested_verbs() -> None:
    cap_map: caps.IndexCapabilitiesMap = {
        "scraped": {
            "bm25": {"fields": [{"name": "title", "type": "text"}]},
            "ann": {"embedder": "bge-m3", "vector_dim": 8},
        }
    }
    out = caps.render_index_section("scraped", ("bm25", "ann"), cap_map)
    assert "Available BM25 fields on `scraped`" in out
    assert "embedder=`bge-m3`" in out


def test_render_index_section_ignores_unknown_verb() -> None:
    """Defensive: a typo in ``bound_indices`` shouldn't crash render."""
    cap_map: caps.IndexCapabilitiesMap = {
        "x": {"bm25": {"fields": [{"name": "t", "type": "text"}]}}
    }
    out = caps.render_index_section("x", ("bogus",), cap_map)
    assert out == ""


# ---------------------------------------------------------------------------
# Tool.with_capabilities + bind_tools integration
# ---------------------------------------------------------------------------


def test_tool_with_capabilities_appends_schema_to_description() -> None:
    cap_map: caps.IndexCapabilitiesMap = {
        ac_tools.GDELT_EVENTS_INDEX: {
            "bm25": {
                "fields": [
                    {"name": "title", "type": "text", "boost": 3,
                     "description": "Article headline"},
                ]
            }
        }
    }
    bound = ac_tools.BM25_GDELT.with_capabilities(cap_map)

    assert bound is not ac_tools.BM25_GDELT  # immutability
    assert bound.description.startswith(ac_tools.BM25_GDELT.description)
    assert "Available BM25 fields on" in bound.description
    assert "title" in bound.description
    assert "Article headline" in bound.description
    # Original constant must be untouched.
    assert "Available BM25 fields on" not in ac_tools.BM25_GDELT.description


def test_tool_with_capabilities_no_op_when_caps_empty() -> None:
    same = ac_tools.BM25_GDELT.with_capabilities({})
    assert same is ac_tools.BM25_GDELT


def test_tool_with_capabilities_no_op_for_unbound_tool() -> None:
    """``COMPUTE_TECHNICALS`` actually IS bound (to EQUITY_BARS); for
    the no-op path we synthesise a tiny tool with no bindings."""
    standalone = ac_tools.Tool(
        name="ping",
        description="just pings",
        parameters={"type": "object", "properties": {}},
        fn=lambda **_: {"ok": True},
    )
    same = standalone.with_capabilities({"any": {"bm25": {}}})
    assert same is standalone


def test_bind_tools_applies_to_every_tool() -> None:
    cap_map: caps.IndexCapabilitiesMap = {
        ac_tools.GDELT_EVENTS_INDEX: {
            "bm25": {"fields": [{"name": "title", "type": "text"}]}
        }
    }
    bound = ac_tools.bind_tools(
        (ac_tools.BM25_GDELT, ac_tools.BM25_SEC),
        cap_map,
    )
    assert len(bound) == 2
    # First tool is bound to GDELT_EVENTS_INDEX -> should pick up the schema.
    assert "Available BM25 fields on" in bound[0].description
    # SEC is bound to a different index whose caps weren't supplied;
    # falls back to the original description.
    assert bound[1].description == ac_tools.BM25_SEC.description
