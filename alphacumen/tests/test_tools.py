# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for ``alphacumen.tools``.

Each test patches ``coralbricks.sandbox.tools.<verb>`` to assert the
IA tool dispatches to the expected verb + index slug + argument
shape. The IA tools are pure dispatchers; we never need to talk to
a real OpenSearch / DuckDB / Python interpreter to validate them.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from alphacumen import tools as ac_tools
from alphacumen.index_map import (
    EQUITY_BARS_INDEX,
    GDELT_EVENTS_INDEX,
    GRAPH_INDEX,
    MACRO_INDEX,
    REDDIT_INDEX,
    SCRAPED_ARTICLES_INDEX,
    SEC_FILINGS_INDEX,
)


# --------------------------------------------------------------------------
# Tool dataclass + roster invariants
# --------------------------------------------------------------------------


def test_tool_to_openai_schema_shape() -> None:
    schema = ac_tools.BM25_GDELT.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "bm25_gdelt"
    assert "description" in schema["function"]
    assert schema["function"]["parameters"]["required"] == ["query"]


def test_to_openai_schema_returns_independent_dict() -> None:
    s1 = ac_tools.BM25_GDELT.to_openai_schema()
    s1["function"]["parameters"]["properties"]["query"]["description"] = "MUTATED"
    s2 = ac_tools.BM25_GDELT.to_openai_schema()
    assert s2["function"]["parameters"]["properties"]["query"]["description"] != "MUTATED"


def test_all_tools_unique_names() -> None:
    names = [t.name for t in ac_tools.ALL_TOOLS]
    assert len(names) == len(set(names))


def test_lookup_tool_finds_by_name() -> None:
    found = ac_tools.lookup_tool("bm25_gdelt")
    assert found is ac_tools.BM25_GDELT


def test_lookup_tool_raises_keyerror_for_unknown() -> None:
    with pytest.raises(KeyError):
        ac_tools.lookup_tool("does_not_exist")


def test_specialist_rosters_only_contain_known_tools() -> None:
    all_tool_names = {t.name for t in ac_tools.ALL_TOOLS}
    for roster in (
        ac_tools.STOCK_ANALYST_TOOLS,
        ac_tools.SECTOR_ANALYST_TOOLS,
        ac_tools.VC_ANALYST_TOOLS,
        ac_tools.RISK_ANALYST_TOOLS,
    ):
        for tool in roster:
            assert tool.name in all_tool_names


# --------------------------------------------------------------------------
# bm25_gdelt / bm25_sec / bm25_scraped_articles / vector_scraped_articles
# --------------------------------------------------------------------------


def _bm25_envelope(index: str, n: int = 2) -> dict[str, Any]:
    return {
        "index": index,
        "hits": [
            {
                "id": f"id_{i}",
                "score": 1.0 - i * 0.1,
                "source": {
                    "title": f"hit {i}",
                    "body": "x" * 100,
                    "embedding": [0.1] * 1024,
                },
            }
            for i in range(n)
        ],
    }


def test_bm25_gdelt_dispatches_to_gdelt_index() -> None:
    captured: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _bm25_envelope(GDELT_EVENTS_INDEX)

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        out = ac_tools.BM25_GDELT.fn(query="apple earnings", k=5)

    assert captured["index"] == GDELT_EVENTS_INDEX
    assert captured["query"] == "apple earnings"
    assert captured["k"] == 5
    assert out["index"] == GDELT_EVENTS_INDEX
    # Embedding should be stripped from every hit.
    for hit in out["hits"]:
        assert "embedding" not in hit["source"]


def test_bm25_sec_dispatches_to_sec_index_with_fields() -> None:
    captured: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _bm25_envelope(SEC_FILINGS_INDEX)

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        ac_tools.BM25_SEC.fn(
            query="revenue net income EPS guidance",
            fields=["title^3", "body^2"],
        )

    assert captured["index"] == SEC_FILINGS_INDEX
    assert captured["fields"] == ["title^3", "body^2"]


def test_bm25_scraped_articles_dispatches_to_scraped_index() -> None:
    captured: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _bm25_envelope(SCRAPED_ARTICLES_INDEX)

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        ac_tools.BM25_SCRAPED_ARTICLES.fn(
            query="quantization aware training",
            fields=["title^3", "article_text^2"],
            k=4,
        )

    assert captured["index"] == SCRAPED_ARTICLES_INDEX
    assert captured["fields"] == ["title^3", "article_text^2"]
    assert captured["k"] == 4


def test_vector_scraped_articles_uses_ann_with_text() -> None:
    captured: dict[str, Any] = {}

    def fake_ann(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _bm25_envelope(SCRAPED_ARTICLES_INDEX)

    with patch("alphacumen.tools.cb_tools.ann", side_effect=fake_ann):
        ac_tools.VECTOR_SCRAPED_ARTICLES.fn(query="ai market consolidation", k=3)

    assert captured["index"] == SCRAPED_ARTICLES_INDEX
    assert captured["text"] == "ai market consolidation"
    assert captured["k"] == 3
    # Filters omitted -> forwarded as None (not absent), so the gateway
    # can distinguish "no filter requested" from "key wasn't passed".
    assert captured["filters"] is None


def test_vector_scraped_articles_forwards_filters_verbatim() -> None:
    """The model emits OpenSearch DSL directly; the alphacumen wrapper
    must forward it through unchanged. Regression guard for the
    timeout incident where ``filters`` were silently dropped and
    the ANN ran unfiltered over ~29M docs."""
    captured: dict[str, Any] = {}

    def fake_ann(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _bm25_envelope(SCRAPED_ARTICLES_INDEX)

    filters = {
        "range": {
            "published_date": {
                "gte": "2024-01-01", "lte": "2026-04-28",
            },
        },
    }
    with patch("alphacumen.tools.cb_tools.ann", side_effect=fake_ann):
        ac_tools.VECTOR_SCRAPED_ARTICLES.fn(
            query="EU tariffs on Chinese EVs",
            k=8,
            filters=filters,
        )

    assert captured["filters"] == filters


def test_vector_scraped_articles_schema_advertises_filters() -> None:
    """The model only knows what the JSON schema tells it. Pin that
    ``filters`` is exposed (so the model stops hallucinating
    rejected calls) and that the description names the canonical
    field, since OpenSearch returns 0 hits for misspelled filter
    fields without surfacing an error."""
    schema = ac_tools.VECTOR_SCRAPED_ARTICLES.parameters
    props = schema["properties"]
    assert "filters" in props
    assert props["filters"]["type"] == "object"
    desc = props["filters"]["description"].lower()
    assert "published_date" in desc
    assert "opensearch" in desc


# --------------------------------------------------------------------------
# get_full_text ref parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref, expected_index, expected_id",
    [
        ("sec:0001045810-26-000024:5.02", SEC_FILINGS_INDEX, "0001045810-26-000024:5.02"),
        ("art:abc123", SCRAPED_ARTICLES_INDEX, "abc123"),
        ("plain_id", GDELT_EVENTS_INDEX, "plain_id"),
        ("unknown_prefix:xyz", GDELT_EVENTS_INDEX, "unknown_prefix:xyz"),
    ],
)
def test_get_full_text_ref_parsing(
    ref: str, expected_index: str, expected_id: str,
) -> None:
    captured: dict[str, Any] = {}

    def fake_get(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "index": kwargs["index"],
            "found": True,
            "doc": {
                "id": kwargs["id"],
                "source": {
                    "body": "long body text here",
                    "embedding": [0.0] * 1024,
                },
            },
        }

    with patch("alphacumen.tools.cb_tools.get", side_effect=fake_get):
        out = ac_tools.GET_FULL_TEXT.fn(ref=ref)

    assert captured["index"] == expected_index
    assert captured["id"] == expected_id
    assert out["found"] is True
    # Embedding stripped + body kept (under cap)
    assert "embedding" not in out["source"]
    assert out["source"]["body"] == "long body text here"


def test_get_full_text_truncates_oversized_body() -> None:
    long = "X" * 50_000

    def fake_get(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"],
            "found": True,
            "doc": {"id": kwargs["id"], "source": {"body": long}},
        }

    with patch("alphacumen.tools.cb_tools.get", side_effect=fake_get):
        out = ac_tools.GET_FULL_TEXT.fn(ref="sec:abc", max_chars=1000)

    assert "[truncated at 1000 chars]" in out["source"]["body"]
    assert len(out["source"]["body"]) <= 1000 + 80


def test_get_full_text_returns_not_found_when_kernel_says_so() -> None:
    def fake_get(**kwargs: Any) -> dict[str, Any]:
        return {"index": kwargs["index"], "found": False}

    with patch("alphacumen.tools.cb_tools.get", side_effect=fake_get):
        out = ac_tools.GET_FULL_TEXT.fn(ref="art:missing")

    assert out["found"] is False


# --------------------------------------------------------------------------
# query_graph / multihop_graph
# --------------------------------------------------------------------------


def test_query_graph_dispatches_to_sql_against_graph_index() -> None:
    captured: dict[str, Any] = {}

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "index": kwargs["index"],
            "rows": [{"actor_id": 1, "name": "AAPL"}],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        ac_tools.QUERY_GRAPH.fn(sql="SELECT * FROM actor LIMIT 1")

    assert captured["index"] == GRAPH_INDEX
    assert captured["query"].startswith("SELECT")


def test_multihop_graph_splits_seeds_on_comma() -> None:
    captured: dict[str, Any] = {}

    def fake_multihop(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"nodes": [], "edges": [], "truncated": False}

    with patch("alphacumen.tools.cb_tools.multihop", side_effect=fake_multihop):
        ac_tools.MULTIHOP_GRAPH.fn(
            seed="AAPL,NVDA, GOOG",
            hops=3,
            predicate_filter=["mentions_org"],
        )

    assert captured["index"] == GRAPH_INDEX
    assert captured["start_ids"] == ["AAPL", "NVDA", "GOOG"]
    assert captured["hops"] == 3
    assert captured["predicate_filter"] == ["mentions_org"]


def test_multihop_graph_rejects_empty_seed() -> None:
    with patch("alphacumen.tools.cb_tools.multihop") as mock_mh:
        out = ac_tools.MULTIHOP_GRAPH.fn(seed="   , , ", hops=2)
    assert "error" in out
    mock_mh.assert_not_called()


# --------------------------------------------------------------------------
# get_macro_series / get_equity_bars
# --------------------------------------------------------------------------


def test_get_macro_series_resolves_alias_to_table() -> None:
    captured: dict[str, Any] = {}

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "index": kwargs["index"],
            "rows": [{"date": "2026-01-01", "value": 5.0}],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.GET_MACRO_SERIES.fn(
            series="oil", start="2026-01-01", end="2026-03-01",
        )

    assert captured["index"] == MACRO_INDEX
    assert "FROM brent" in captured["query"]  # 'oil' aliases to 'brent'
    assert "2026-01-01" in captured["query"]
    assert out["series"] == "oil"
    assert out["table"] == "brent"
    assert out["row_count"] == 1


def test_get_macro_series_rejects_non_string_series() -> None:
    with patch("alphacumen.tools.cb_tools.sql") as mock_sql:
        out = ac_tools.GET_MACRO_SERIES.fn(
            series=["SPX_FUTURES", "NDX_FUTURES"],
            start="2026-01-01",
            end="2026-03-02",
        )
    assert "error" in out
    mock_sql.assert_not_called()


def test_get_macro_series_rejects_unknown_series() -> None:
    with patch("alphacumen.tools.cb_tools.sql") as mock_sql:
        out = ac_tools.GET_MACRO_SERIES.fn(
            series="nasdaq100_futures",
            start="2026-03-01",
            end="2026-03-31",
        )
    assert "error" in out
    assert "nasdaq100_futures" in out["error"]
    assert "get_equity_bars" in out["error"]
    mock_sql.assert_not_called()


def test_get_equity_bars_dispatches_with_uppercased_symbol() -> None:
    captured: dict[str, Any] = {}

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "index": kwargs["index"],
            "rows": [{"date": "2026-01-02", "open": 100, "high": 105,
                       "low": 98, "close": 103, "volume": 1_000_000}],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.GET_EQUITY_BARS.fn(
            symbol="aapl", start="2026-01-01", end="2026-02-01",
        )

    assert captured["index"] == EQUITY_BARS_INDEX
    assert "symbol = 'AAPL'" in captured["query"]
    assert out["symbol"] == "AAPL"


# --------------------------------------------------------------------------
# get_reddit_sentiment / search_reddit_posts (sql against reddit index)
# --------------------------------------------------------------------------


def test_get_reddit_sentiment_dispatches_to_reddit_index_uppercased_ticker() -> None:
    captured: dict[str, Any] = {}

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "index": kwargs["index"],
            "rows": [
                {
                    "obs_date": "2025-04-15",
                    "subreddit": "wallstreetbets",
                    "post_count": 42,
                    "avg_sentiment": 0.21,
                    "score_weighted_sentiment": 0.34,
                    "total_score": 18_000,
                    "top_post_title": "$NVDA gamma squeeze setup",
                    "top_post_id": "abc123",
                },
            ],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.GET_REDDIT_SENTIMENT.fn(
            ticker="nvda", start="2025-04-01", end="2025-04-30",
        )

    assert captured["index"] == REDDIT_INDEX
    assert "FROM sentiment_daily" in captured["query"]
    assert "UPPER(ticker) = 'NVDA'" in captured["query"]
    assert "obs_date >= '2025-04-01'" in captured["query"]
    assert "obs_date <= '2025-04-30'" in captured["query"]
    # Default subreddit roster gets spliced into the IN clause.
    assert "'wallstreetbets'" in captured["query"]
    assert "'SecurityAnalysis'" in captured["query"]
    assert out["ticker"] == "NVDA"
    assert out["subreddits"] == [
        "wallstreetbets", "stocks", "investing",
        "options", "SecurityAnalysis",
    ]
    assert out["row_count"] == 1
    assert out["coverage_note"].startswith("pullpush.io")


def test_get_reddit_sentiment_respects_explicit_subreddit_filter() -> None:
    captured: dict[str, Any] = {}

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"index": kwargs["index"], "rows": [], "row_count": 0,
                "truncated": False}

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.GET_REDDIT_SENTIMENT.fn(
            ticker="AAPL",
            start="2025-01-01",
            end="2025-01-31",
            subreddits=["wallstreetbets", "options"],
        )

    # Only the explicit two should appear in the IN list; not the
    # other three defaults.
    assert "'wallstreetbets'" in captured["query"]
    assert "'options'" in captured["query"]
    assert "'stocks'" not in captured["query"]
    assert "'SecurityAnalysis'" not in captured["query"]
    assert out["subreddits"] == ["wallstreetbets", "options"]


def test_search_reddit_posts_builds_ilike_with_clamped_limit() -> None:
    captured: dict[str, Any] = {}

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "index": kwargs["index"],
            "rows": [
                {
                    "post_id": "p1",
                    "subreddit": "wallstreetbets",
                    "obs_date": "2025-04-10",
                    "title": "Tesla short squeeze building",
                    "selftext_excerpt": "...",
                    "score": 9_001,
                    "upvote_ratio": 0.92,
                    "num_comments": 412,
                    "tickers_mentioned": ["TSLA"],
                    "sentiment_score": 0.45,
                    "sentiment_label": "positive",
                    "url": "https://reddit.com/r/wsb/p1",
                },
            ],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.SEARCH_REDDIT_POSTS.fn(
            query="short squeeze",
            start="2025-04-01",
            end="2025-04-30",
            # 200 should clamp to 50.
            limit=200,
        )

    assert captured["index"] == REDDIT_INDEX
    assert "FROM posts" in captured["query"]
    assert "title ILIKE '%short squeeze%'" in captured["query"]
    assert "selftext ILIKE '%short squeeze%'" in captured["query"]
    assert "obs_date >= '2025-04-01'" in captured["query"]
    assert "obs_date <= '2025-04-30'" in captured["query"]
    assert "ORDER BY score DESC" in captured["query"]
    assert "LIMIT 50" in captured["query"]
    assert out["limit"] == 50
    assert out["post_count"] == 1
    assert out["coverage_note"].startswith("pullpush.io")


def test_search_reddit_posts_escapes_single_quotes_in_query() -> None:
    captured: dict[str, Any] = {}

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"index": kwargs["index"], "rows": [], "row_count": 0,
                "truncated": False}

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        ac_tools.SEARCH_REDDIT_POSTS.fn(query="it's working")

    # DuckDB-standard escape: a single quote inside a literal is
    # written as two single quotes. The pattern is wrapped in % so
    # we look for the doubled-quote inside a complete LIKE literal.
    assert "ILIKE '%it''s working%'" in captured["query"]


def test_search_reddit_posts_rejects_empty_query_without_dispatching() -> None:
    with patch("alphacumen.tools.cb_tools.sql") as mock_sql:
        out = ac_tools.SEARCH_REDDIT_POSTS.fn(query="   ")

    mock_sql.assert_not_called()
    assert out["post_count"] == 0
    assert "must not be empty" in out["error"]


def test_get_reddit_sentiment_in_stock_analyst_roster() -> None:
    names = {t.name for t in ac_tools.STOCK_ANALYST_TOOLS}
    assert "get_reddit_sentiment" in names
    assert "search_reddit_posts" in names


# --------------------------------------------------------------------------
# pullpush.io coverage-ceiling warnings -- guards the empty envelope so the
# react loop sees "structural gap" instead of "transient miss".
# --------------------------------------------------------------------------


def test_get_reddit_sentiment_emits_coverage_warning_when_start_past_ceiling() -> None:
    """Window opens after the pullpush ceiling -- guaranteed empty.

    The warning must call out the ceiling date so the agent knows the
    empty result is not a retry candidate.
    """

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {"index": kwargs["index"], "rows": [], "row_count": 0,
                "truncated": False}

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.GET_REDDIT_SENTIMENT.fn(
            ticker="NVDA", start="2026-03-23", end="2026-03-31",
        )

    assert out["row_count"] == 0
    assert "coverage_warning" in out
    assert "2025-05-19" in out["coverage_warning"]
    assert "do NOT retry" in out["coverage_warning"]


def test_get_reddit_sentiment_no_warning_when_window_inside_coverage() -> None:
    """Window fully inside coverage and rows returned -> no warning."""

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"],
            "rows": [{"obs_date": "2025-04-15", "subreddit": "wallstreetbets"}],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.GET_REDDIT_SENTIMENT.fn(
            ticker="NVDA", start="2025-04-01", end="2025-04-30",
        )

    assert out["row_count"] == 1
    assert "coverage_warning" not in out


def test_get_reddit_sentiment_warns_when_only_end_overshoots_and_empty() -> None:
    """Tail overshoots ceiling AND the window came back empty -> warn.

    The empty result might just be "no NVDA chatter that week", but
    when the tail is also past the ceiling we surface the gap so the
    agent doesn't shrug it off as a transient miss.
    """

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {"index": kwargs["index"], "rows": [], "row_count": 0,
                "truncated": False}

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.GET_REDDIT_SENTIMENT.fn(
            ticker="NVDA", start="2025-05-10", end="2025-06-15",
        )

    assert out["row_count"] == 0
    assert "coverage_warning" in out
    assert "2025-05-19" in out["coverage_warning"]


def test_get_reddit_sentiment_no_warning_when_tail_overshoots_but_rows_returned() -> None:
    """Partial overlap that still yielded rows -> stay quiet.

    The data we did return is real; nagging the model about the
    missing tail just adds noise.
    """

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"],
            "rows": [{"obs_date": "2025-05-15"}],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.GET_REDDIT_SENTIMENT.fn(
            ticker="NVDA", start="2025-05-10", end="2025-06-15",
        )

    assert out["row_count"] == 1
    assert "coverage_warning" not in out


def test_search_reddit_posts_emits_coverage_warning_when_start_past_ceiling() -> None:
    """Same warning surface, mirrored on the keyword-search dispatcher."""

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {"index": kwargs["index"], "rows": [], "row_count": 0,
                "truncated": False}

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out = ac_tools.SEARCH_REDDIT_POSTS.fn(
            query="gamma squeeze",
            start="2026-01-01", end="2026-01-31",
        )

    assert out["post_count"] == 0
    assert "coverage_warning" in out
    assert "2025-05-19" in out["coverage_warning"]


# --------------------------------------------------------------------------
# compute_technicals (sql + py composition)
# --------------------------------------------------------------------------


_FAKE_BARS = [
    {"date": f"2026-01-{i:02d}", "open": 100 + i,
     "high": 102 + i, "low": 99 + i, "close": 101 + i,
     "volume": 1_000_000 + i * 10}
    for i in range(1, 31)
]


def test_compute_technicals_pulls_bars_then_runs_default_snippet() -> None:
    sql_calls: list[dict[str, Any]] = []
    py_calls: list[dict[str, Any]] = []

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        sql_calls.append(kwargs)
        return {
            "index": kwargs["index"],
            "rows": _FAKE_BARS,
            "row_count": len(_FAKE_BARS),
            "truncated": False,
        }

    def fake_py(code: str, *, inputs: Any = None) -> dict[str, Any]:
        py_calls.append({"code": code, "inputs": inputs})
        return {
            "ok": True,
            "result": {"symbol": "AAPL", "sma_20": 110.5, "atr_14": 2.0},
            "took_ms": 5,
            "stdout": "",
            "truncated": False,
            "globals_added": [],
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql), \
         patch("alphacumen.tools.cb_tools.py", side_effect=fake_py):
        out = ac_tools.COMPUTE_TECHNICALS.fn(
            symbol="aapl", start="2026-01-01", end="2026-01-31",
        )

    assert sql_calls[0]["index"] == EQUITY_BARS_INDEX
    assert len(py_calls) == 1
    # Default snippet pre-binds bars + symbol; no `code` override.
    assert py_calls[0]["inputs"]["symbol"] == "AAPL"
    assert py_calls[0]["inputs"]["bars"] == _FAKE_BARS
    assert "import statistics" in py_calls[0]["code"]
    assert out["symbol"] == "AAPL"
    assert out["bar_count"] == len(_FAKE_BARS)
    assert out["result"]["sma_20"] == 110.5


def test_compute_technicals_accepts_custom_code_override() -> None:
    py_calls: list[dict[str, Any]] = []

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"],
            "rows": _FAKE_BARS,
            "row_count": len(_FAKE_BARS),
            "truncated": False,
        }

    def fake_py(code: str, *, inputs: Any = None) -> dict[str, Any]:
        py_calls.append({"code": code, "inputs": inputs})
        return {
            "ok": True,
            "result": {"custom": True},
            "took_ms": 1,
            "stdout": "",
            "truncated": False,
            "globals_added": [],
        }

    custom = "result = {'custom': True, 'n': len(bars)}"
    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql), \
         patch("alphacumen.tools.cb_tools.py", side_effect=fake_py):
        out = ac_tools.COMPUTE_TECHNICALS.fn(
            symbol="AAPL", start="2026-01-01", end="2026-01-31",
            code=custom,
        )

    assert py_calls[0]["code"] == custom
    assert out["result"] == {"custom": True}


def test_compute_technicals_handles_no_bars() -> None:
    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"],
            "rows": [],
            "row_count": 0,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql), \
         patch("alphacumen.tools.cb_tools.py") as mock_py:
        out = ac_tools.COMPUTE_TECHNICALS.fn(
            symbol="ZZZZ", start="2026-01-01", end="2026-01-31",
        )

    assert "error" in out
    mock_py.assert_not_called()


def test_compute_technicals_surfaces_py_error_envelope() -> None:
    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"],
            "rows": _FAKE_BARS,
            "row_count": len(_FAKE_BARS),
            "truncated": False,
        }

    def fake_py(code: str, *, inputs: Any = None) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "type": "ZeroDivisionError",
                "message": "ZeroDivisionError: division by zero",
                "traceback": "...",
            },
            "took_ms": 1,
            "stdout": "",
            "truncated": False,
            "globals_added": [],
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql), \
         patch("alphacumen.tools.cb_tools.py", side_effect=fake_py):
        out = ac_tools.COMPUTE_TECHNICALS.fn(
            symbol="AAPL", start="2026-01-01", end="2026-01-31",
            code="result = 1/0",
        )

    assert out["error_type"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in out["error"]


# --------------------------------------------------------------------------
# Default snippet: RSI / MACD / Bollinger are now part of the canonical
# pack (added 0.0.82). Exercising the snippet directly via ``exec``
# locks in the canonical numbers so a refactor of the formulas (or a
# regression in stdlib) can't silently shift stock_analyst's bull/bear
# reads. The integration tests above mock cb_tools.py so they never
# actually run the snippet -- this one does.
# --------------------------------------------------------------------------


_KNOWN_BARS_30D = [
    # 30 closes hand-picked to span both directions so RSI lands in a
    # checkable range and MACD has both EMAs available.
    {"date": f"2026-01-{i:02d}", "open": p, "high": p + 1.0,
     "low": p - 1.0, "close": p, "volume": 1_000_000}
    for i, p in enumerate([
        100.0, 101.5, 102.0, 101.0, 99.5, 98.0, 99.0, 100.5, 102.0, 103.5,
        104.0, 103.0, 101.5, 100.0, 99.0, 100.5, 102.0, 103.5, 105.0, 106.5,
        107.0, 106.0, 104.5, 103.0, 101.5, 100.0, 101.0, 102.5, 104.0, 105.5,
    ], start=1)
]


def _exec_default_snippet(bars: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    from alphacumen.tools import _TECHNICALS_DEFAULT_SNIPPET
    g: dict[str, Any] = {"bars": bars, "symbol": symbol}
    exec(_TECHNICALS_DEFAULT_SNIPPET, g)
    return g["result"]


def test_default_snippet_emits_rsi_macd_bollinger_canonical_values() -> None:
    """Lock canonical RSI(14), MACD(12,26,9), Bollinger(20,2σ) values.

    The snippet is deterministic over (symbol, window). Run it with a
    fixed 30-bar series and assert the indicator outputs are within
    floating-point tolerance of independently-computed reference values.
    """
    result = _exec_default_snippet(_KNOWN_BARS_30D, "TEST")
    assert result["symbol"] == "TEST"
    assert result["bar_count"] == 30
    # RSI(14): sample series alternates losses + gains; should sit
    # near the bullish-ish 60s for this synthetic up-trending tape.
    assert result["rsi_14"] is not None
    assert 50.0 < result["rsi_14"] < 80.0
    # MACD(12,26,9): both EMAs available at bar 26 onward; signal
    # available at bar 26 + 9 = 35 -- 30 bars is short of that, so
    # canonical-values check needs ≥ 35 bars. Confirm we get None
    # rather than a bogus value with insufficient history.
    assert result["macd"] is None
    # Bollinger(20): middle is SMA of last 20, width = 4σ.
    assert result["bollinger_20"] is not None
    bb = result["bollinger_20"]
    assert bb["upper"] > bb["middle"] > bb["lower"]
    assert abs(bb["width"] - (bb["upper"] - bb["lower"])) < 1e-9


def test_default_snippet_macd_available_with_sufficient_history() -> None:
    """MACD needs 26 + 9 = 35 bars before signal exists. Build a 60-bar
    series and check macd / signal / histogram are all real numbers."""
    bars = [
        {"date": f"2026-d{i:03d}", "open": p, "high": p + 1.0,
         "low": p - 1.0, "close": p, "volume": 1_000_000}
        for i, p in enumerate(
            [100.0 + (i * 0.3) + ((-1) ** i) * 0.5 for i in range(60)],
            start=1,
        )
    ]
    result = _exec_default_snippet(bars, "TEST")
    macd = result["macd"]
    assert macd is not None
    assert isinstance(macd["macd"], float)
    assert isinstance(macd["signal"], float)
    assert abs(macd["histogram"] - (macd["macd"] - macd["signal"])) < 1e-9


def test_default_snippet_is_deterministic_across_runs() -> None:
    """Same bars + same symbol → byte-identical result. This is the
    invariant that fixes run dc54184c vs 073f35ad — stock_analyst's
    rolled-its-own-MACD code produced -2.94 one run and +0.76 the
    next on the same close series. With the canonical pack, that
    cannot happen."""
    bars = [
        {"date": f"2026-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}",
         "open": p, "high": p + 1.0, "low": p - 1.0,
         "close": p, "volume": 1_000_000}
        for i, p in enumerate(
            [100.0 + (i * 0.3) + ((-1) ** i) * 0.5 for i in range(50)],
            start=1,
        )
    ]
    a = _exec_default_snippet(bars, "AAPL")
    b = _exec_default_snippet(bars, "AAPL")
    assert a == b


# --------------------------------------------------------------------------
# Run-scoped caches: get_equity_bars + get_macro_series memoize on
# (symbol/table, start, end) so a swarm run that re-fetches the same
# window pays the SQL cost once. ``compute_technicals`` re-uses
# ``_do_get_equity_bars`` internally, so the cache benefits it too.
# --------------------------------------------------------------------------


def test_get_equity_bars_caches_repeat_calls_for_same_window() -> None:
    sql_calls: list[dict[str, Any]] = []

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        sql_calls.append(kwargs)
        return {
            "index": kwargs["index"],
            "rows": _FAKE_BARS,
            "row_count": len(_FAKE_BARS),
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out1 = ac_tools.GET_EQUITY_BARS.fn(
            symbol="aapl", start="2026-01-01", end="2026-01-31",
        )
        out2 = ac_tools.GET_EQUITY_BARS.fn(
            symbol="AAPL", start="2026-01-01", end="2026-01-31",
        )

    # Only one SQL call despite two equivalent fetches (symbol case
    # is normalised before the cache lookup).
    assert len(sql_calls) == 1
    assert out1["row_count"] == out2["row_count"] == len(_FAKE_BARS)
    # Different windows still hit the gateway.
    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        ac_tools.GET_EQUITY_BARS.fn(
            symbol="AAPL", start="2026-02-01", end="2026-02-28",
        )
    assert len(sql_calls) == 2


def test_compute_technicals_reuses_get_equity_bars_cache() -> None:
    sql_calls: list[dict[str, Any]] = []
    py_calls: list[dict[str, Any]] = []

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        sql_calls.append(kwargs)
        return {
            "index": kwargs["index"],
            "rows": _FAKE_BARS,
            "row_count": len(_FAKE_BARS),
            "truncated": False,
        }

    def fake_py(code: str, *, inputs: Any = None) -> dict[str, Any]:
        py_calls.append({"code": code, "inputs": inputs})
        return {
            "ok": True,
            "result": {"sma_20": 110.0},
            "took_ms": 1,
            "stdout": "",
            "truncated": False,
            "globals_added": [],
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql), \
         patch("alphacumen.tools.cb_tools.py", side_effect=fake_py):
        # Stock-analyst pattern: one direct fetch + one compute over
        # the same window. The compute_technicals call should reuse
        # the cached bars, not re-issue the SQL query.
        ac_tools.GET_EQUITY_BARS.fn(
            symbol="NVDA", start="2026-03-01", end="2026-03-31",
        )
        ac_tools.COMPUTE_TECHNICALS.fn(
            symbol="NVDA", start="2026-03-01", end="2026-03-31",
        )

    assert len(sql_calls) == 1
    assert len(py_calls) == 1
    assert py_calls[0]["inputs"]["bars"] == _FAKE_BARS


def test_get_equity_bars_cache_isolates_full_payload_from_mutation() -> None:
    """``bind_as`` returns a marker dict; the cache must hand back a
    deep copy so a downstream mutation can't poison subsequent reads."""

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"],
            "rows": [{"date": "2026-01-02", "close": 100}],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql), \
         patch("alphacumen.tools.cb_tools.bind_py_global"):
        out1 = ac_tools.GET_EQUITY_BARS.fn(
            symbol="AAPL", start="2026-01-01", end="2026-01-31",
            bind_as="bars1",
        )
        out1["rows"].append({"date": "POISON", "close": -1})
        out2 = ac_tools.GET_EQUITY_BARS.fn(
            symbol="AAPL", start="2026-01-01", end="2026-01-31",
            bind_as="bars2",
        )

    assert out2["rows"] == [{"date": "2026-01-02", "close": 100}]
    # bind_as overwrites between calls — so the second call's bound_as
    # marker reflects the second name, not the first.
    assert out2.get("bound_as") == "bars2"


def test_get_macro_series_caches_repeat_calls_for_same_window() -> None:
    sql_calls: list[dict[str, Any]] = []

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        sql_calls.append(kwargs)
        return {
            "index": kwargs["index"],
            "rows": [{"date": "2026-01-01", "value": 4.25}],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out1 = ac_tools.GET_MACRO_SERIES.fn(
            series="federal_funds", start="2026-01-01", end="2026-03-31",
        )
        out2 = ac_tools.GET_MACRO_SERIES.fn(
            series="federal_funds", start="2026-01-01", end="2026-03-31",
        )

    assert len(sql_calls) == 1
    assert out1["table"] == out2["table"] == "federal_funds_rate"
    assert out1["row_count"] == out2["row_count"] == 1


def test_get_macro_series_aliases_share_a_cache_entry_via_table_key() -> None:
    """``oil`` and ``brent`` resolve to the same table; the cache key
    is the resolved table name, so the second call is a hit."""
    sql_calls: list[dict[str, Any]] = []

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        sql_calls.append(kwargs)
        return {
            "index": kwargs["index"],
            "rows": [{"date": "2026-01-01", "value": 80.0}],
            "row_count": 1,
            "truncated": False,
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql):
        out_alias = ac_tools.GET_MACRO_SERIES.fn(
            series="oil", start="2026-01-01", end="2026-01-31",
        )
        out_table = ac_tools.GET_MACRO_SERIES.fn(
            series="brent", start="2026-01-01", end="2026-01-31",
        )

    assert len(sql_calls) == 1
    assert out_alias["table"] == out_table["table"] == "brent"
    # ``series`` reflects the caller's name, not the resolved table —
    # so callers using either alias get back what they asked for.
    assert out_alias["series"] == "oil"
    assert out_table["series"] == "brent"


# --------------------------------------------------------------------------
# bm25 tools accept sort= as a no-op (model occasionally tries
# `sort=[{"event_date": "desc"}]`; was raising TypeError before).
# --------------------------------------------------------------------------


def test_bm25_sec_accepts_sort_kwarg_as_no_op() -> None:
    captured: dict[str, Any] = {}

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "index": kwargs["index"],
            "hits": [
                {"id": "sec:a", "score": 0.5, "source": {"ticker": "BA"}},
                {"id": "sec:b", "score": 0.3, "source": {"ticker": "BA"}},
            ],
        }

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_sql):
        out = ac_tools.BM25_SEC.fn(
            query="revenue",
            filters={"ticker": "BA", "form_type": "8-K"},
            sort=[{"event_date": "desc"}],
        )

    assert "hits" in out
    # The sort kwarg never reaches the gateway — it's a alphacumen-side no-op.
    assert "sort" not in captured


def test_bm25_gdelt_accepts_sort_kwarg_as_no_op() -> None:
    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        return {"index": kwargs["index"], "hits": []}

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        out = ac_tools.BM25_GDELT.fn(
            query="boeing",
            sort=[{"day": "desc"}],
        )
    assert out.get("hits") == []


def test_bm25_scraped_articles_accepts_sort_kwarg_as_no_op() -> None:
    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        return {"index": kwargs["index"], "hits": []}

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        out = ac_tools.BM25_SCRAPED_ARTICLES.fn(
            query="boeing",
            sort=[{"published_date": "desc"}],
        )
    assert out.get("hits") == []


# --------------------------------------------------------------------------
# get_full_text caches per (ref, max_chars). A swarm in which
# sector_analyst calls get_full_text twice for the same SEC ref pays the
# tool round-trip once.
# --------------------------------------------------------------------------


def test_get_full_text_caches_repeat_calls_for_same_ref() -> None:
    get_calls: list[dict[str, Any]] = []

    def fake_get(**kwargs: Any) -> dict[str, Any]:
        get_calls.append(kwargs)
        return {
            "index": kwargs["index"],
            "found": True,
            "doc": {"id": kwargs["id"], "source": {"body": "FULL FILING"}},
        }

    with patch("alphacumen.tools.cb_tools.get", side_effect=fake_get):
        out1 = ac_tools.GET_FULL_TEXT.fn(
            ref="sec:0001045810-26-000019:2.02",
        )
        out2 = ac_tools.GET_FULL_TEXT.fn(
            ref="sec:0001045810-26-000019:2.02",
        )

    assert len(get_calls) == 1
    assert out1["source"]["body"] == "FULL FILING"
    assert out2["source"]["body"] == "FULL FILING"
    assert out1["found"] is True and out2["found"] is True


def test_get_full_text_cache_keyed_on_max_chars() -> None:
    """Different ``max_chars`` values produce different envelopes
    (different truncation suffix), so each gets its own cache slot."""
    get_calls: list[dict[str, Any]] = []
    body_long = "A" * 5_000

    def fake_get(**kwargs: Any) -> dict[str, Any]:
        get_calls.append(kwargs)
        return {
            "index": kwargs["index"],
            "found": True,
            "doc": {"id": kwargs["id"], "source": {"body": body_long}},
        }

    with patch("alphacumen.tools.cb_tools.get", side_effect=fake_get):
        out_short = ac_tools.GET_FULL_TEXT.fn(
            ref="sec:foo:1", max_chars=1_000,
        )
        out_long = ac_tools.GET_FULL_TEXT.fn(
            ref="sec:foo:1", max_chars=4_000,
        )

    assert len(get_calls) == 2
    # Short was clipped, long kept the full 5000 (since 5000 > 4000 too...
    # actually 5000 > 4000, both clipped; but 1000-clip < 4000-clip.)
    assert len(out_short["source"]["body"]) < len(out_long["source"]["body"])


def test_get_full_text_isolates_cached_payload_from_mutation() -> None:
    """Returned dict is a deep copy; mutating it must not leak into
    the next cached read."""

    def fake_get(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"],
            "found": True,
            "doc": {"id": kwargs["id"], "source": {"body": "CLEAN"}},
        }

    with patch("alphacumen.tools.cb_tools.get", side_effect=fake_get):
        out1 = ac_tools.GET_FULL_TEXT.fn(ref="sec:foo:1")
        out1["source"]["body"] = "POISONED"
        out2 = ac_tools.GET_FULL_TEXT.fn(ref="sec:foo:1")

    assert out2["source"]["body"] == "CLEAN"


def test_run_python_passes_through_inputs_and_result() -> None:
    py_calls: list[dict[str, Any]] = []

    def fake_py(code: str, *, inputs: Any = None) -> dict[str, Any]:
        py_calls.append({"code": code, "inputs": inputs})
        return {
            "ok": True,
            "result": [{"id": "a", "score": 0.5}, {"id": "b", "score": 0.3}],
            "took_ms": 4,
            "stdout": "",
            "truncated": False,
            "globals_added": ["scores"],
        }

    code = (
        "k = 60\n"
        "scores = {}\n"
        "for ranked in [bm25_hits, ann_hits]:\n"
        "    for rank, h in enumerate(ranked, start=1):\n"
        "        scores[h['id']] = scores.get(h['id'], 0.0) + 1.0/(k+rank)\n"
        "result = sorted(({'id': i, 'score': s} for i, s in scores.items()), "
        "key=lambda x: -x['score'])[:10]"
    )
    bm25_hits = [{"id": "a"}, {"id": "b"}]
    ann_hits = [{"id": "b"}, {"id": "c"}]

    with patch("alphacumen.tools.cb_tools.py", side_effect=fake_py):
        out = ac_tools.RUN_PYTHON.fn(
            code=code,
            inputs={"bm25_hits": bm25_hits, "ann_hits": ann_hits},
        )

    assert out["ok"] is True
    assert out["result"][0]["id"] == "a"
    assert out["globals_added"] == ["scores"]
    assert py_calls[0]["code"] == code
    assert py_calls[0]["inputs"]["bm25_hits"] == bm25_hits
    assert py_calls[0]["inputs"]["ann_hits"] == ann_hits


def test_run_python_handles_no_inputs() -> None:
    py_calls: list[dict[str, Any]] = []

    def fake_py(code: str, *, inputs: Any = None) -> dict[str, Any]:
        py_calls.append({"code": code, "inputs": inputs})
        return {
            "ok": True,
            "result": 42,
            "took_ms": 1,
            "stdout": "",
            "truncated": False,
            "globals_added": [],
        }

    with patch("alphacumen.tools.cb_tools.py", side_effect=fake_py):
        out = ac_tools.RUN_PYTHON.fn(code="result = 6 * 7")

    assert out["result"] == 42
    assert py_calls[0]["inputs"] is None


def test_run_python_surfaces_py_error_envelope() -> None:
    def fake_py(code: str, *, inputs: Any = None) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "type": "NameError",
                "message": "NameError: name 'undefined_var' is not defined",
                "traceback": "...",
            },
            "took_ms": 1,
            "stdout": "",
            "truncated": False,
            "globals_added": [],
        }

    with patch("alphacumen.tools.cb_tools.py", side_effect=fake_py):
        out = ac_tools.RUN_PYTHON.fn(code="result = undefined_var")

    assert out["ok"] is False
    assert out["error_type"] == "NameError"
    assert "NameError" in out["error"]


def test_run_python_is_in_all_specialist_rosters_except_stock() -> None:
    """`run_python` lives in sector / vc / risk rosters; stock has
    `compute_technicals` as its python escape hatch."""
    assert ac_tools.RUN_PYTHON in ac_tools.SECTOR_ANALYST_TOOLS
    assert ac_tools.RUN_PYTHON in ac_tools.VC_ANALYST_TOOLS
    assert ac_tools.RUN_PYTHON in ac_tools.RISK_ANALYST_TOOLS
    assert ac_tools.RUN_PYTHON not in ac_tools.STOCK_ANALYST_TOOLS
    assert ac_tools.RUN_PYTHON in ac_tools.ALL_TOOLS


# --------------------------------------------------------------------------
# bind_as -- "tool result -> python variable" affordance
# --------------------------------------------------------------------------


def test_bind_as_returns_full_envelope_plus_marker_and_binds() -> None:
    """When `bind_as` is set, the model still gets the FULL projected
    envelope (so it can read titles + scores inline). The only
    differences vs the no-bind path are: a `bound_as` marker is
    added, and the envelope is also pushed into the runner globals
    so a later `run_python` snippet can reference it by name."""
    bound: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        return _bm25_envelope(GDELT_EVENTS_INDEX)

    def fake_bind(name: str, value: Any) -> None:
        bound[name] = value

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25), \
         patch("alphacumen.tools.cb_tools.bind_py_global", side_effect=fake_bind):
        out = ac_tools.BM25_GDELT.fn(
            query="apple earnings", k=3, bind_as="gd_hits",
        )

    expected_hit_count = len(_bm25_envelope(GDELT_EVENTS_INDEX)["hits"])
    assert out["bound_as"] == "gd_hits"
    assert out["index"] == GDELT_EVENTS_INDEX
    # Full hits in the response (not a 3-row preview).
    assert "hits" in out and len(out["hits"]) == expected_hit_count
    # AND bound under the same value.
    assert "gd_hits" in bound
    assert len(bound["gd_hits"]["hits"]) == expected_hit_count


def test_bind_as_unset_returns_legacy_envelope_unchanged() -> None:
    """No bind_as -> the tool returns the projected envelope and
    NOTHING is pushed to the interpreter. Same shape as before this
    feature existed."""
    bound: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        return _bm25_envelope(GDELT_EVENTS_INDEX)

    def fake_bind(name: str, value: Any) -> None:
        bound[name] = value

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25), \
         patch("alphacumen.tools.cb_tools.bind_py_global", side_effect=fake_bind):
        out = ac_tools.BM25_GDELT.fn(query="apple earnings", k=3)

    assert out["index"] == GDELT_EVENTS_INDEX
    assert "hits" in out
    assert "bound_as" not in out
    assert bound == {}


def test_bind_as_strips_embeddings_in_bound_value() -> None:
    """The kernel's embedding fields are the only truly heavy
    payload; the projection layer that runs BEFORE binding strips
    them. Verifying here so a future kernel change that started
    leaking embeddings would be caught at the bind boundary too."""
    bound: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        env = _bm25_envelope(GDELT_EVENTS_INDEX)
        # Inject an embedding to simulate a kernel that didn't
        # strip it. The alphacumen projection should drop it.
        env["hits"][0]["source"]["embedding"] = [0.0] * 1024
        return env

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25), \
         patch("alphacumen.tools.cb_tools.bind_py_global",
               side_effect=lambda n, v: bound.update({n: v})):
        out = ac_tools.BM25_GDELT.fn(query="x", bind_as="hits")

    for hit in out["hits"]:
        assert "embedding" not in hit["source"]
    for hit in bound["hits"]["hits"]:
        assert "embedding" not in hit["source"]


def test_bind_as_on_get_full_text_returns_full_doc_and_binds() -> None:
    """`get_full_text` returns the (truncated) doc inline AND binds
    it. The body is the same one the model sees in the response."""
    bound: dict[str, Any] = {}
    body_text = "A" * 5000

    def fake_get(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"],
            "found": True,
            "doc": {
                "id": kwargs["id"],
                "source": {
                    "title": "Some Filing",
                    "body": body_text,
                },
            },
        }

    with patch("alphacumen.tools.cb_tools.get", side_effect=fake_get), \
         patch("alphacumen.tools.cb_tools.bind_py_global",
               side_effect=lambda n, v: bound.update({n: v})):
        out = ac_tools.GET_FULL_TEXT.fn(
            ref="sec:0000123-26-000001:7.01", bind_as="filing_body",
        )

    assert out["bound_as"] == "filing_body"
    assert out["found"] is True
    assert out["source"]["body"] == body_text
    assert bound["filing_body"]["source"]["body"] == body_text


def test_bind_as_on_get_equity_bars_returns_full_rows_and_binds() -> None:
    bound: dict[str, Any] = {}
    rows = [
        {"date": f"2026-01-{d:02d}", "open": 100, "high": 101,
         "low": 99, "close": 100.5, "volume": 1000}
        for d in range(1, 11)
    ]

    def fake_sql(**kwargs: Any) -> dict[str, Any]:
        return {
            "index": kwargs["index"], "rows": rows,
            "row_count": len(rows), "truncated": False,
            "columns": ["date", "open", "high", "low", "close", "volume"],
        }

    with patch("alphacumen.tools.cb_tools.sql", side_effect=fake_sql), \
         patch("alphacumen.tools.cb_tools.bind_py_global",
               side_effect=lambda n, v: bound.update({n: v})):
        out = ac_tools.GET_EQUITY_BARS.fn(
            symbol="AAPL", start="2026-01-01", end="2026-01-10",
            bind_as="aapl_bars",
        )

    assert out["bound_as"] == "aapl_bars"
    assert out["row_count"] == 10
    assert len(out["rows"]) == 10
    assert len(bound["aapl_bars"]["rows"]) == 10


def test_bind_as_invalid_name_attaches_error_marker_not_raises() -> None:
    """Bad bind_as name: the fetch already happened (paid cost),
    the data is still useful. Tool returns the full result with a
    `bind_error` marker so the model knows the variable is NOT
    available and can self-correct on retry."""
    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        return _bm25_envelope(GDELT_EVENTS_INDEX)

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        out = ac_tools.BM25_GDELT.fn(
            query="x", bind_as="not an identifier",
        )

    # Result is still there...
    assert out["index"] == GDELT_EVENTS_INDEX
    assert "hits" in out
    # ...with an error marker explaining why the bind didn't take.
    assert "bind_error" in out
    assert "identifier" in out["bind_error"].lower()
    # And no `bound_as` marker, since the bind failed.
    assert "bound_as" not in out


def test_bind_as_param_in_every_search_tool_schema() -> None:
    """Every search/get/sql tool exposes the same `bind_as`
    parameter so the model picks it up uniformly across the
    surface."""
    tools_with_bind_as = (
        ac_tools.BM25_GDELT,
        ac_tools.BM25_SEC,
        ac_tools.BM25_SCRAPED_ARTICLES,
        ac_tools.VECTOR_SCRAPED_ARTICLES,
        ac_tools.GET_FULL_TEXT,
        ac_tools.QUERY_GRAPH,
        ac_tools.MULTIHOP_GRAPH,
        ac_tools.GET_MACRO_SERIES,
        ac_tools.GET_EQUITY_BARS,
        ac_tools.GET_REDDIT_SENTIMENT,
        ac_tools.SEARCH_REDDIT_POSTS,
    )
    for tool in tools_with_bind_as:
        assert "bind_as" in tool.parameters["properties"], (
            f"{tool.name} missing bind_as in parameters schema"
        )
        prop = tool.parameters["properties"]["bind_as"]
        assert prop["type"] == "string"
        # bind_as must NEVER be required -- it's always opt-in.
        assert "bind_as" not in tool.parameters.get("required", [])


# --------------------------------------------------------------------------
# Recency-floor escape hatch: when the model passes ANY explicit date
# filter, the wrapper must NOT inject a default gte that would push
# gte past lte and silently zero out historical queries (regression
# guard for the Netflix-2019-2024 incident; the same bug pattern was
# fixed earlier for bm25_sec in commit 12f78ccb).
# --------------------------------------------------------------------------


def _collect_range_clauses(dsl: Any, field: str) -> list[dict[str, Any]]:
    """Find every {"range": {field: {...}}} clause in a _flat_to_os_dsl
    result, regardless of whether it was returned unwrapped or wrapped
    inside bool.filter."""
    out: list[dict[str, Any]] = []
    if not isinstance(dsl, dict):
        return out
    if "range" in dsl and isinstance(dsl["range"], dict) and field in dsl["range"]:
        out.append(dsl["range"][field])
    if "bool" in dsl and isinstance(dsl["bool"], dict):
        for clause in dsl["bool"].get("filter", []) or []:
            if isinstance(clause, dict) and field in clause.get("range", {}):
                out.append(clause["range"][field])
    return out


def test_bm25_scraped_articles_respects_explicit_historical_range() -> None:
    captured: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _bm25_envelope(SCRAPED_ARTICLES_INDEX)

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        ac_tools.BM25_SCRAPED_ARTICLES.fn(
            query="Netflix ARPPU streaming wars",
            filters={
                "published_date_gte": "2019-01-01",
                "published_date_lte": "2024-12-31",
            },
        )

    ranges = _collect_range_clauses(captured["filters"], "published_date")
    assert any(r.get("gte") == "2019-01-01" for r in ranges), (
        f"expected gte=2019-01-01 to survive, got {ranges}"
    )
    assert any(r.get("lte") == "2024-12-31" for r in ranges)


def test_bm25_scraped_articles_injects_floor_when_no_date_filter() -> None:
    captured: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _bm25_envelope(SCRAPED_ARTICLES_INDEX)

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        ac_tools.BM25_SCRAPED_ARTICLES.fn(query="quarterly earnings")

    ranges = _collect_range_clauses(captured["filters"], "published_date")
    assert ranges, f"expected injected published_date range, got {captured['filters']!r}"
    assert any("gte" in r for r in ranges)


def test_bm25_gdelt_respects_explicit_historical_range() -> None:
    captured: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _bm25_envelope(GDELT_EVENTS_INDEX)

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        ac_tools.BM25_GDELT.fn(
            query="Netflix regulation",
            filters={"day_gte": "20190101", "day_lte": "20241231"},
        )

    ranges = _collect_range_clauses(captured["filters"], "day")
    assert any(r.get("gte") == "20190101" for r in ranges), (
        f"expected gte=20190101 to survive, got {ranges}"
    )
    assert any(r.get("lte") == "20241231" for r in ranges)


def test_bm25_gdelt_injects_floor_when_no_date_filter() -> None:
    captured: dict[str, Any] = {}

    def fake_bm25(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _bm25_envelope(GDELT_EVENTS_INDEX)

    with patch("alphacumen.tools.cb_tools.bm25", side_effect=fake_bm25):
        ac_tools.BM25_GDELT.fn(query="aapl earnings")

    ranges = _collect_range_clauses(captured["filters"], "day")
    assert ranges, f"expected injected day range, got {captured['filters']!r}"
    assert any("gte" in r for r in ranges)


# --------------------------------------------------------------------------
# get_cover_page_share_counts -- per-class shares-outstanding parser
# --------------------------------------------------------------------------
#
# Mirrors the get_xbrl_facts test surface: we never hit sec-api.io,
# we patch _do_get_xbrl_facts to return a synthetic cover-page-shaped
# fact list. The interesting behavior under test is the segment ->
# class-letter mapping and the CoverPage section filter.


def _abnb_cover_page_facts() -> list[dict[str, Any]]:
    """Synthetic ABNB cover-page facts (FY 2024 10-K).

    Mirrors what _xbrl_flatten produces from sec-api's XBRL JSON for
    a multi-class filer. Includes Class H (Host Endowment Fund) so
    the consolidation-vs-cover-page distinction is exercised.
    """
    base = {
        "concept": "EntityCommonStockSharesOutstanding",
        "section": "CoverPage",
        "period": {"instant": "2025-01-31"},
        "unit": "shares",
        "decimals": "0",
        "is_extension": False,
    }
    return [
        {**base, "value": "432876657",
         "segment": [{"dimension": "us-gaap:StatementClassOfStockAxis",
                      "value": "us-gaap:CommonClassAMember"}]},
        {**base, "value": "188462942",
         "segment": [{"dimension": "us-gaap:StatementClassOfStockAxis",
                      "value": "us-gaap:CommonClassBMember"}]},
        {**base, "value": "0",
         "segment": [{"dimension": "us-gaap:StatementClassOfStockAxis",
                      "value": "us-gaap:CommonClassCMember"}]},
        {**base, "value": "9200000",
         "segment": [{"dimension": "us-gaap:StatementClassOfStockAxis",
                      "value": "abnb:CommonClassHMember"}]},
        # Noise: a SharesOutstanding fact from the equity rollforward
        # in a different section -- must be filtered out by the
        # CoverPage section gate.
        {**base, "section": "StatementsOfStockholdersEquity",
         "value": "0",
         "segment": [{"dimension": "us-gaap:StatementClassOfStockAxis",
                      "value": "abnb:CommonClassHMember"}]},
    ]


def test_get_cover_page_share_counts_parses_multi_class_filing() -> None:
    """Multi-class filer (ABNB) -- four classes returned in A/B/C/H order."""

    def fake_xbrl(ref: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ref": ref,
            "accession": "0001559720-25-000010",
            "facts": _abnb_cover_page_facts(),
        }

    with patch("alphacumen.tools._do_get_xbrl_facts", side_effect=fake_xbrl):
        result = ac_tools.GET_COVER_PAGE_SHARE_COUNTS.fn(
            ref="sec:0001559720-25-000010"
        )

    assert result["accession"] == "0001559720-25-000010"
    assert result["matched_count"] == 4, result
    classes = {c["class"]: c for c in result["classes"]}
    assert classes["A"]["shares"] == 432876657
    assert classes["B"]["shares"] == 188462942
    assert classes["C"]["shares"] == 0
    # Class H -- the cover-page count, NOT the equity-table 0.
    assert classes["H"]["shares"] == 9200000
    assert classes["H"]["axis_member"] == "abnb:CommonClassHMember"
    # All cover-page facts share the same as_of (filing-cover-page date).
    assert {c["as_of"] for c in result["classes"]} == {"2025-01-31"}
    # Sorted A -> B -> C -> H.
    assert [c["class"] for c in result["classes"]] == ["A", "B", "C", "H"]


def test_get_cover_page_share_counts_handles_single_class_filer() -> None:
    """Single-class filer -- no segment dimension, class is empty string."""

    def fake_xbrl(ref: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ref": ref,
            "accession": "0000000000-25-000001",
            "facts": [{
                "concept": "EntityCommonStockSharesOutstanding",
                "section": "CoverPage",
                "value": "1234567890",
                "period": {"instant": "2025-02-01"},
                "unit": "shares",
                "decimals": "0",
                "is_extension": False,
                "segment": [],
            }],
        }

    with patch("alphacumen.tools._do_get_xbrl_facts", side_effect=fake_xbrl):
        result = ac_tools.GET_COVER_PAGE_SHARE_COUNTS.fn(ref="sec:0000000000-25-000001")

    assert result["matched_count"] == 1
    assert result["classes"][0]["class"] == ""
    assert result["classes"][0]["shares"] == 1234567890


def test_get_cover_page_share_counts_propagates_xbrl_error() -> None:
    """Upstream sec-api error short-circuits with a clear classes=[] response."""

    def fake_xbrl(ref: str, **_kwargs: Any) -> dict[str, Any]:
        return {"ref": ref, "accession": "x", "error": "sec-api HTTP 500: boom", "facts": []}

    with patch("alphacumen.tools._do_get_xbrl_facts", side_effect=fake_xbrl):
        result = ac_tools.GET_COVER_PAGE_SHARE_COUNTS.fn(ref="sec:bogus")

    assert result["classes"] == []
    assert "sec-api HTTP 500" in result["error"]


def test_get_cover_page_share_counts_emits_hint_on_empty_match() -> None:
    """Untagged cover page (e.g. older 8-K) returns a fallback hint."""

    def fake_xbrl(ref: str, **_kwargs: Any) -> dict[str, Any]:
        return {"ref": ref, "accession": "0000000000-25-000002", "facts": []}

    with patch("alphacumen.tools._do_get_xbrl_facts", side_effect=fake_xbrl):
        result = ac_tools.GET_COVER_PAGE_SHARE_COUNTS.fn(ref="sec:0000000000-25-000002")

    assert result["matched_count"] == 0
    assert "extract_filing_tables" in result["hint"]


def test_get_cover_page_share_counts_in_sector_roster() -> None:
    """The new tool must ship in the sector-analyst roster."""
    names = [t.name for t in ac_tools.SECTOR_ANALYST_TOOLS]
    assert "get_cover_page_share_counts" in names
