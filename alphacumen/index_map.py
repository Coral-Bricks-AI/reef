# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen.index_map`` -- IA index slug constants.

Centralising the slugs in one module means a deployment can re-target
an index (e.g. flip from ``"sec_filings_chunked"`` to
``"sec_filings_chunked_v2"`` for a re-ingest) by editing one line
rather than chasing every call site
in :mod:`alphacumen.tools`. The manifest's ``[tool.coralbricks.pipeline].indices``
list MUST stay in sync with the values here -- the gateway rejects
the run if a tool dispatches to an index outside the declared
allowlist, which is exactly the failure mode you want when the two
drift.

Slugs match what
:class:`gateway.store.indices.IndexRegistrationStore` reports back
from ``GET /indices``; production ops registers each one separately
with the right backend metadata (OpenSearch endpoint for BM25 / ANN,
S3 prefix for SQL/Parquet, cuGraph endpoint for multihop).
"""

from __future__ import annotations

GDELT_EVENTS_INDEX = "gdelt_events_v2"
"""GDELT event-news corpus (BM25-only). 2.7M articles, OpenSearch."""

SEC_FILINGS_INDEX = "sec_filings_chunked"
"""SEC EDGAR filing bodies (8-K, 10-K, 10-Q). BM25 + ``get`` hydration."""

SEC_FILINGS_ANN_INDEX = "sec_filings_ann"
"""SEC EDGAR filing chunks, BGE-M3 1024-dim. ANN fallback for table/numeric
chunks that BM25 ranks too low (e.g. revenue disaggregation tables,
director compensation tables, loan origination breakdowns)."""

SCRAPED_ARTICLES_INDEX = "scraped_articles_bge_m3"
"""Scraped web-articles corpus, BGE-M3 1024-dim. ANN + ``get``."""

GRAPH_INDEX = "graph_combined"
"""GDELT+SEC knowledge graph. Backs both ``sql`` and ``multihop``."""

MACRO_INDEX = "macro_v1"
"""US macro / commodity benchmarks (FRED + EIA). Parquet, ``sql``-only."""

EQUITY_BARS_INDEX = "equity_bars_v1"
"""Daily OHLCV bars for US-listed tickers. Parquet, ``sql``-only."""

REDDIT_INDEX = "reddit_pullpush_v1"
"""Reddit posts + daily VADER sentiment aggregates from the
pullpush.io backfill across r/wallstreetbets, r/stocks, r/investing,
r/options, r/SecurityAnalysis (+ ticker-specific subs). Parquet,
``sql``-only. Coverage ceiling ~ May 2025 (pullpush.io indexing
horizon)."""

OPTIONS_INDEX = "options_v1"
"""Options chains (HISTORICAL_OPTIONS). Parquet, ``sql``-only.
Hive-partitioned by symbol= and date=."""


ALL_INDICES: tuple[str, ...] = (
    GDELT_EVENTS_INDEX,
    SEC_FILINGS_INDEX,
    SEC_FILINGS_ANN_INDEX,
    SCRAPED_ARTICLES_INDEX,
    GRAPH_INDEX,
    MACRO_INDEX,
    EQUITY_BARS_INDEX,
    REDDIT_INDEX,
    OPTIONS_INDEX,
)
"""Order matches the manifest's ``indices = [...]`` list for
diffability when the manifest is regenerated."""


__all__ = [
    "ALL_INDICES",
    "EQUITY_BARS_INDEX",
    "GDELT_EVENTS_INDEX",
    "GRAPH_INDEX",
    "MACRO_INDEX",
    "OPTIONS_INDEX",
    "REDDIT_INDEX",
    "SCRAPED_ARTICLES_INDEX",
    "SEC_FILINGS_ANN_INDEX",
    "SEC_FILINGS_INDEX",
]
