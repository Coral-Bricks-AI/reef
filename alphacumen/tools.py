# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen.tools`` -- IA tool surface, expressed over the platform verbs.

This is the slice 5c.5 cutover: every IA-specific @tool that used to
live in ``gdelt.project.agents.lg_tools`` and dispatch to the
hand-rolled ``eval/memory_bench/.../tool_*`` functions is now a
3-to-15-line dispatcher over one of the seven generic kernel verbs
exposed by ``coralbricks.sandbox.tools`` and
``coralbricks.sandbox.llm`` (the platform foundation):

- ``tools.bm25``      -- OpenSearch BM25 search.
- ``tools.ann``       -- approximate nearest-neighbor (HNSW).
- ``tools.sql``       -- DuckDB-over-Parquet SELECT.
- ``tools.multihop``  -- knowledge-graph BFS.
- ``tools.get``       -- single-doc hydration by ``(index, id)``.
- ``tools.py``        -- in-runner Python interpreter for analytics.

Tool names follow a ``<verb>_<corpus>`` shape so the model can pick
the right surface from the name alone:

- ``bm25_gdelt``              -- BM25 over GDELT events (OpenSearch).
- ``bm25_sec``                -- BM25 over SEC EDGAR filings (Turbopuffer).
- ``bm25_scraped_articles``   -- BM25 over scraped web articles (OpenSearch).
- ``vector_scraped_articles`` -- ANN over scraped web articles (BGE-M3, OpenSearch).
- ``query_graph``             -- DuckDB SQL on the GDELT+SEC KG.
- ``multihop_graph``          -- BFS on the same KG.
- ``get_full_text``           -- single-doc hydration across the search corpora.
- ``get_macro_series`` / ``get_equity_bars`` -- typed time-series accessors.
- ``get_reddit_sentiment`` / ``search_reddit_posts`` -- daily VADER
                                 sentiment aggregates and full-text post
                                 search over the pullpush.io Reddit
                                 backfill (typed dispatchers over
                                 ``tools.sql`` against ``reddit_pullpush_v1``).
- ``compute_technicals``      -- ``get_equity_bars`` + ``tools.py`` snippet
                                 (server-side composition; bars never enter
                                 the model context).
- ``run_python``              -- raw ``tools.py`` over arbitrary
                                 ``inputs={...}``; use for fusing
                                 search rankings (RRF), ad-hoc dedup,
                                 small post-processing.

Each one is a thin index-binding adapter: it picks the right
kernel verb + index slug + parameter shape and forwards the call.

Score fusion across legs (BM25 + ANN, multi-field BM25, ...) is
*not* exposed as its own tool. Within a single ``bm25_*`` call the
gateway transparently fuses per-field BM25 sub-queries with
weighted RRF when the index is Turbopuffer-backed (see
:func:`gateway.search.turbopuffer_bm25._weighted_rrf`); OpenSearch
multi-field BM25 is fused natively by the OS ``multi_match``
scorer. Cross-verb fusion (BM25 + ANN) is the model's job: invoke
``bm25_scraped_articles`` and ``vector_scraped_articles`` as two
separate tool calls and reason over both rankings.

Why this matters
----------------

1. **One tool kernel, many tools.** Previously every IA tool was a
   bespoke implementation reaching directly into OpenSearch /
   DuckDB / cuGraph / S3 from inside the pipeline. That code path
   is now gone -- the gateway's :class:`~gateway.rpc.tool_kernel.ToolKernel`
   owns all backend access. alphacumen just describes which index it
   wants and what shape of result it needs.

2. **Sandboxed by construction.** All alphacumen tool calls now flow
   through the per-run RPC socket, which is the only outbound
   surface the sandbox has. No backend credentials reach the
   pipeline, no direct egress is possible, the gateway can meter /
   cap / log every call.

3. **Trajectory + caching are the gateway's problem.** The legacy
   :class:`_TOOL_CACHE` / ``_session_cache`` machinery that lived in
   ``lg_tools.py`` is replaced by the gateway's per-run accounting.
   Tools here are stateless dispatchers; rate-limiting, idempotency
   keys, and trajectory recording are platform concerns.

4. **``compute_technicals`` is now ``tools.py`` instead of a
   bespoke handler.** This is the validation gate that motivated
   the in-runner Python interpreter: if the model can author the
   technicals snippet itself (rolling means, ATR, S/R) over a
   ``tools.sql`` bars pull, we never need a server-side
   ``compute_technicals`` again. The implementation here is a
   straight one-liner over the new ``py`` verb.

Tool surface
------------

Each tool is a :class:`Tool` dataclass carrying its name,
human-readable description, OpenAI-shaped JSON parameters schema,
and the Python callable. The runtime (:mod:`reef.react`) feeds
the schemas into ``llm.chat(tools=[...])`` and dispatches by name
when the model returns ``tool_calls``. Personas pick subsets via
:data:`STOCK_ANALYST_TOOLS` / :data:`SECTOR_ANALYST_TOOLS` /
:data:`VC_ANALYST_TOOLS` / :data:`RISK_ANALYST_TOOLS` -- same
roster shape the legacy code shipped, fewer LOC behind it.

Index slugs
-----------

The IA-facing names map to index slugs registered in the platform's
:class:`~gateway.store.indices.IndexRegistrationStore` via
:mod:`alphacumen.index_map`. The slugs are *not* baked into the tool
bodies so a deployment can re-target an index by editing one map
entry rather than chasing call sites; the manifest's ``indices``
list is the contract.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from reef.stubs import tools as cb_tools
from reef.stubs.py_executor import PyValidationError

from alphacumen.capabilities import (
    IndexCapabilitiesMap,
    render_index_section,
)
from alphacumen.skill_registry import LOAD_SKILL
from alphacumen.skills import LOAD_PLANNER_SKILL
from reef.decorators import time_bounded
from reef.skill_tools import INVOKE_SKILL_FN
from reef.tool import (
    _BIND_AS_PARAM_SCHEMA,
    _TOOL_RESULT_MAX_CHARS,
    Tool,
    _apply_binding,
    _truncate_for_model,
    bind_tools,
)
from reef.tool import (
    lookup_tool as _harness_lookup_tool,
)
from alphacumen.index_map import (
    EQUITY_BARS_INDEX,
    GDELT_EVENTS_INDEX,
    GRAPH_INDEX,
    MACRO_INDEX,
    OPTIONS_INDEX,
    REDDIT_INDEX,
    SCRAPED_ARTICLES_INDEX,
    SEC_FILINGS_ANN_INDEX,
    SEC_FILINGS_INDEX,
)

logger = logging.getLogger(__name__)


_TICKER_TO_COMPANY: dict[str, str] = {
    "AAPL": "Apple", "MSFT": "Microsoft", "GOOGL": "Google Alphabet",
    "GOOG": "Google Alphabet", "AMZN": "Amazon", "META": "Meta Facebook",
    "NVDA": "NVIDIA", "TSLA": "Tesla", "BRK.B": "Berkshire Hathaway",
    "JPM": "JPMorgan", "V": "Visa", "JNJ": "Johnson Johnson",
    "WMT": "Walmart", "PG": "Procter Gamble", "MA": "Mastercard",
    "UNH": "UnitedHealth", "HD": "Home Depot", "DIS": "Disney",
    "BAC": "Bank of America", "XOM": "Exxon Mobil", "PFE": "Pfizer",
    "CSCO": "Cisco", "ADBE": "Adobe", "CRM": "Salesforce",
    "NFLX": "Netflix", "AMD": "AMD Advanced Micro Devices",
    "INTC": "Intel", "QCOM": "Qualcomm", "COST": "Costco",
    "AVGO": "Broadcom", "TXN": "Texas Instruments", "ORCL": "Oracle",
    "BA": "Boeing", "CVX": "Chevron", "LLY": "Eli Lilly",
    "MRK": "Merck", "ABBV": "AbbVie", "TMO": "Thermo Fisher",
    "NKE": "Nike", "SNAP": "Snap Snapchat", "PYPL": "PayPal",
    "SQ": "Block Square", "COIN": "Coinbase", "ROKU": "Roku",
    "UBER": "Uber", "LYFT": "Lyft", "ABNB": "Airbnb",
    "ASML": "ASML", "TSM": "TSMC Taiwan Semiconductor",
    "NVO": "Novo Nordisk", "BABA": "Alibaba", "PDD": "PDD Pinduoduo",
    "GS": "Goldman Sachs", "MS": "Morgan Stanley", "C": "Citigroup",
    "WFC": "Wells Fargo", "BLK": "BlackRock", "SCHW": "Charles Schwab",
    "GM": "General Motors", "F": "Ford", "RIVN": "Rivian",
    "LCID": "Lucid Motors", "PLTR": "Palantir", "SNOW": "Snowflake",
    "NET": "Cloudflare", "ZS": "Zscaler", "CRWD": "CrowdStrike",
    "PANW": "Palo Alto Networks", "DDOG": "Datadog",
    "MU": "Micron", "LRCX": "Lam Research", "KLAC": "KLA",
    "AMAT": "Applied Materials", "ARM": "Arm Holdings",
}
_TICKER_PATTERN = re.compile(r"\b([A-Z]{1,5})\b")

# Thread-local storage for per-specialist temporal ceiling. When the
# synthesizer sets a filed_at_lte for a specialist invocation, the
# swarm writes it here before launching the specialist. _do_bm25_sec
# reads it and auto-injects the ceiling on every call that doesn't
# already have an explicit filed_at_lte.
import threading
_THREAD_LOCAL = threading.local()

def set_temporal_ceiling(filed_at_lte: str | None) -> None:
    """Set a filed_at_lte ceiling for the current specialist thread."""
    _THREAD_LOCAL.filed_at_lte = filed_at_lte

def get_temporal_ceiling() -> str | None:
    """Get the filed_at_lte ceiling for the current specialist thread."""
    return getattr(_THREAD_LOCAL, 'filed_at_lte', None)

# SEC ticker aliases: some companies file under a different ticker than
# the one a question references. Two flavors:
#   1. Dual-class shares — both tickers refer to the same issuer and the
#      SEC happens to file under one of them (GOOGL/GOOG, BRK.A/BRK.B,
#      FOXA/FOX, NWSA/NWS).
#   2. Historical renames — the index stamps every chunk with the
#      issuer's *current* ticker, so a question about "Facebook" or
#      "Square" comes in with the historical ticker (FB, SQ) and finds
#      zero hits even though the underlying 10-K is indexed under the
#      new ticker.
# Maps lookup-ticker → also-search ticker.  When bm25_sec sees a
# ``ticker`` filter matching a key, it expands to a ``terms`` filter
# covering both.
_SEC_TICKER_ALIASES: dict[str, str] = {
    "GOOGL": "GOOG",
    "BRK.A": "BRK.B",
    "FOXA": "FOX",
    "NWSA": "NWS",
    "SQ": "XYZ",       # Square → Block (Jan 2024)
    "FB": "META",      # Facebook → Meta (Jun 2022)
    "TWTR": "X",       # Twitter → X (Jul 2023)
    "DISCA": "WBD",    # Discovery → Warner Bros. Discovery (Apr 2022)
    "DISCK": "WBD",
    "VIAC": "PARA",    # ViacomCBS → Paramount (Feb 2022)
    "VIACA": "PARA",
    "SAVE": "FLYYQ",   # Spirit Airlines → FLYYQ (Chapter 11, Nov 2024; Q-suffix during bankruptcy)
}


def _expand_tickers_for_gdelt(query: str) -> str:
    """Prepend company names for bare ticker symbols in a GDELT BM25 query.

    GDELT stores entity names like "NVIDIA CORPORATION", not ticker
    symbols. A query for "NVDA" returns 0 hits; "NVIDIA NVDA" matches.
    """
    tokens = _TICKER_PATTERN.findall(query)
    additions = []
    for tok in tokens:
        company = _TICKER_TO_COMPANY.get(tok)
        if company and company.lower() not in query.lower():
            additions.append(company)
    if not additions:
        return query
    expanded = " ".join(additions) + " " + query
    logger.debug("bm25_gdelt: expanded query %r → %r", query, expanded)
    return expanded


# Tool dataclass, lookup_tool, bind_tools, _truncate_for_model,
# _TOOL_RESULT_MAX_CHARS, _apply_binding, and _BIND_AS_PARAM_SCHEMA are
# now defined in :mod:`reef.tool` and imported at the top of
# this module. The finance verbs below splice _BIND_AS_PARAM_SCHEMA into
# their parameter schemas and call _apply_binding to side-effect the
# bind_as variable into the runner globals; the call sites are
# unchanged -- only the canonical location moved.


_SEC_BODY_FIELDS = ("body", "article_text", "content", "text")
_BM25_SNIPPET_RADIUS = 150  # chars before AND after the matched term
_BM25_SNIPPET_MAX = 400      # hard cap on snippet length


def _bm25_snippet(body: str, query: str) -> str:
    """Extract a short window of ``body`` around the first query-term hit.

    Picks the longest token from ``query`` (>=4 chars, alpha) and locates
    its first case-insensitive occurrence in ``body``. Returns ~300 chars
    centered on the match. If no token matches, returns the body's leading
    window. Always capped at :data:`_BM25_SNIPPET_MAX` chars.
    """
    if not body:
        return ""
    if len(body) <= _BM25_SNIPPET_MAX:
        return body
    # Pick the most distinctive query token (longest alpha word).
    tokens = sorted(
        (t for t in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", query or "")),
        key=len, reverse=True,
    )
    body_lc = body.lower()
    pos = -1
    for tok in tokens:
        pos = body_lc.find(tok.lower())
        if pos >= 0:
            break
    if pos < 0:
        return body[:_BM25_SNIPPET_MAX] + " ..."
    start = max(0, pos - _BM25_SNIPPET_RADIUS)
    end = min(len(body), pos + _BM25_SNIPPET_RADIUS)
    snippet = body[start:end]
    if start > 0:
        snippet = "... " + snippet
    if end < len(body):
        snippet = snippet + " ..."
    if len(snippet) > _BM25_SNIPPET_MAX:
        snippet = snippet[:_BM25_SNIPPET_MAX] + " ..."
    return snippet


def _project_bm25_hit(
    hit: Mapping[str, Any],
    *,
    body_mode: str = "full",
    query: str = "",
) -> dict[str, Any]:
    """Drop heavy fields from a single BM25 hit before showing the model.

    Strips embeddings and truncates URLs. For SEC-shaped hits the body
    field is reshaped per ``body_mode``:

    - ``"full"`` -- pass the body through verbatim (legacy behaviour).
    - ``"snippet"`` -- replace the body with a ~300 char window around
      the first query-term hit. Forces the model to call
      :func:`_do_get_full_text` or :func:`_do_get_xbrl_facts` for the
      actual values instead of quoting from a paraphrased chunk.
    - ``"none"`` -- drop the body field entirely; metadata only.
    """
    src = dict(hit.get("source") or {})
    for key in ("embedding", "embedding_vec", "vector", "vec"):
        src.pop(key, None)
    for key in ("url_normalized", "embedded_url", "url", "source_url"):
        if key in src and isinstance(src[key], str) and len(src[key]) > 80:
            src[key] = src[key][:80] + "..."
    if body_mode != "full":
        for key in _SEC_BODY_FIELDS:
            v = src.get(key)
            if not isinstance(v, str) or not v:
                continue
            if body_mode == "snippet":
                src[key] = _bm25_snippet(v, query)
            elif body_mode == "none":
                src.pop(key, None)
    return {
        "id": hit.get("id"),
        "score": hit.get("score"),
        "source": src,
    }


def _project_bm25_envelope(
    env: Mapping[str, Any],
    *,
    body_mode: str = "full",
    query: str = "",
) -> dict[str, Any]:
    """Slim down a ``tools.bm25`` envelope into a model-friendly shape.

    The kernel returns ``{index, hits: [...]}``; we keep both keys
    (so the model can correlate which index a hit came from when
    multiple tools target different slugs) and project each hit
    through :func:`_project_bm25_hit`.
    """
    return {
        "index": env.get("index"),
        "hits": [
            _project_bm25_hit(h, body_mode=body_mode, query=query)
            for h in env.get("hits") or []
        ],
    }


# --------------------------------------------------------------------------
# Bind-as helpers -- "tool result -> in-runner Python variable" affordance
# --------------------------------------------------------------------------

# Shared JSON-schema fragment for the optional ``bind_as`` parameter.
# Every search / retrieval / SQL tool exposes this knob so the model
# has a uniform way to ask "also make this result available as a
# Python variable so I can reference it from `run_python` later
# without re-emitting the bytes". Defined once and spliced into each
# tool's `parameters` so a future docstring tweak doesn't require
# touching every tool.
#
# Design note: ``bind_as`` does NOT change what the model sees in
# the tool response. The full (embedding-stripped) envelope is
# returned either way; the only effect of `bind_as` is the
# additional side effect of binding the value into the in-runner
# interpreter's globals so a later `run_python` snippet can
# reference it by name. This matters for OUTPUT-token cost (the
# model never has to re-emit a 5KB hits list as a `run_python(
# inputs={...})` argument) but does NOT alter what enters the
# model's context on this turn -- the kernel-side embedding strip
# is what keeps the on-the-wire payload manageable; bodies (the
# only remaining heavy field) the model usually wants to read
# inline anyway.
# _BIND_AS_PARAM_SCHEMA and _apply_binding moved to
# :mod:`reef.tool` and are imported at the top of this module.
# Splice sites and call sites below are unchanged.


# --------------------------------------------------------------------------
# Search / retrieval
# --------------------------------------------------------------------------


def _do_bm25_gdelt(
    query: str,
    *,
    k: int = 10,
    fields: Optional[Sequence[str]] = None,
    filters: Optional[Mapping[str, Any]] = None,
    sort: Optional[Sequence[Mapping[str, str]]] = None,
    bind_as: Optional[str] = None,
    **extra: Any,
) -> dict[str, Any]:
    """Implementation of :data:`BM25_GDELT` -- one ``tools.bm25`` call
    against the GDELT events index.

    ``fields`` is forwarded verbatim so a caller can override the
    index's default BM25 field config (e.g.
    ``["title^3", "article_text^2"]``). Multi-field weighting is
    handled by OpenSearch's native ``multi_match`` scorer; no
    Python-side fusion happens here.

    When ``bind_as`` is set, the projected envelope is also bound
    under that name in the in-runner Python interpreter -- the
    return value is unchanged (just gets a ``bound_as`` marker).
    See :func:`_apply_binding`.
    """
    if sort:
        # Accepted for forward-compat: the SEC index emits hits in
        # (score desc, filed_at desc) order already and the GDELT /
        # scraped indices in pure score order, which covers ~all
        # reasonable sort intents the model expresses today. Logged at
        # debug so we can spot a sort spec we ought to actually
        # implement if it shows up in traces.
        logger.debug("bm25_gdelt: sort= no-op (hits stay in score order): %r", sort)
    if extra:
        filters = dict(filters or {})
        filters.update(extra)
    # Recency floor: inject day_gte when the model omits date context
    # entirely. Skip the floor when the model passes ANY date filter
    # (day_gte / day_lte) — that means it's targeting a specific
    # historical period (e.g. "Netflix 2019–2024") and the floor would
    # silently push gte past lte and return zero hits.
    if filters is None:
        filters = {}
    else:
        filters = dict(filters)
    has_explicit_date = any(filters.get(k) for k in ("day_gte", "day_lte"))
    if not has_explicit_date:
        from datetime import date, timedelta
        gdelt_floor = (date.today() - timedelta(days=365)).strftime("%Y%m%d")
        filters["day_gte"] = gdelt_floor
        logger.debug(
            "bm25_gdelt: injected default day_gte=%s (no explicit date filter)",
            gdelt_floor,
        )
    expanded_query = _expand_tickers_for_gdelt(query)
    env = cb_tools.bm25(
        index=GDELT_EVENTS_INDEX,
        query=expanded_query,
        k=k,
        fields=list(fields) if fields else None,
        filters=_flat_to_os_dsl(filters),
    )
    projected = _project_bm25_envelope(env)
    # Recency-first sort: day desc, then score desc.
    hits = projected.get("hits")
    if isinstance(hits, list) and hits:
        hits.sort(
            key=lambda h: (
                str((h.get("source") or {}).get("day", "")),
                float(h.get("score", 0.0)),
            ),
            reverse=True,
        )
    return _apply_binding(bind_as, projected)


BM25_GDELT = Tool(
    name="bm25_gdelt",
    description=(
        "BM25 lexical search over the GDELT event-news corpus "
        "(70M+ articles). Best for keyword-anchored narrative scans "
        "(named entities, event mentions, headline-language searches). "
        "Returns a ranked list of {id, score, source} hits with the "
        "embedding stripped; call get_full_text(ref) to read the full "
        "body of a single hit. Pass `fields` to bias the score toward "
        "specific fields registered for this index (the available "
        "names are listed below). Pass `bind_as=<name>` to ALSO "
        "make the hits available as a Python variable for "
        "`run_python` (avoids re-emitting them as `inputs=`)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "BM25 query text. Required.",
            },
            "k": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
                "description": "Max hits to return (gateway clamps at 200).",
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of registered field names "
                    "(optionally with `^boost`, e.g. `\"title^4\"`). "
                    "See the field list below; unknown names are "
                    "rejected by the gateway."
                ),
            },
            "filters": {
                "type": "object",
                "description": (
                    "Optional pre-filters (AND-combined). PREFERRED "
                    "flat shape: "
                    '{"day_gte": "20260101", "day_lte": "20260331"} '
                    "(append `_gte`/`_lte`/`_gt`/`_lt` for range bounds; "
                    "bare field name with a scalar = exact match, with "
                    "a list = any-of). The GDELT date field is `day` "
                    "(YYYYMMDD string, NOT `event_date`). OpenSearch DSL "
                    "is also accepted: "
                    '{"range": {"day": {"gte": "20260101", '
                    '"lte": "20260331"}}}.'
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["query"],
    },
    fn=_do_bm25_gdelt,
    bound_indices=((GDELT_EVENTS_INDEX, ("bm25",)),),
)


def _do_bm25_sec(
    query: str,
    *,
    k: int = 10,
    fields: Optional[Sequence[str]] = None,
    filters: Optional[Mapping[str, Any]] = None,
    sort: Optional[Sequence[Mapping[str, str]]] = None,
    body_mode: str = "snippet",
    bind_as: Optional[str] = None,
    **extra: Any,
) -> dict[str, Any]:
    """``bm25_sec`` is BM25 narrowed to the SEC EDGAR filings index.

    Same wire shape as :func:`_do_bm25_gdelt`; the only difference
    is the bound index slug. Picking ``query`` carefully matters
    here -- BM25 against the SEC corpus scores form-type tokens
    (``"8-K"``, ``"10-K"``) near zero (every doc has them) so
    callers should query for the *content* they're after, e.g.
    ``"revenue earnings guidance"`` for an earnings 8-K.

    The SEC index lives in Turbopuffer; when ``fields`` lists more
    than one field the gateway issues one BM25 sub-query per field
    and weighted-RRF-fuses the per-field rankings server-side
    (see :func:`gateway.search.turbopuffer_bm25._weighted_rrf`).
    """
    if sort:
        # Accepted for forward-compat. SEC hits are already returned
        # in (score desc, filed_at desc) order via the post-processing
        # below, which matches every ``sort=[{"event_date":"desc"}]``
        # / ``[{"filed_at":"desc"}]`` we've seen the model emit.
        logger.debug("bm25_sec: sort= no-op (default is score+filed_at desc): %r", sort)
    if extra:
        filters = dict(filters or {})
        filters.update(extra)
    # Recency floor: inject filed_at_gte when the model omits date
    # context entirely. Skip the floor when the model passes ANY
    # date filter (filed_at_gte, event_date_gte, event_date_lte) —
    # that means it's targeting a specific historical period (e.g.
    # "JPM Q2 2022") and the floor would block the intended results.
    if filters is None:
        filters = {}
    else:
        filters = dict(filters)
    from datetime import date, timedelta
    has_explicit_date = any(
        filters.get(k)
        for k in ("filed_at_gte", "event_date_gte", "event_date_lte", "filed_at_lte")
    )
    if not has_explicit_date:
        min_floor = (date.today() - timedelta(days=730)).strftime("%Y%m%d")
        filters["filed_at_gte"] = min_floor
    # Thread-local temporal ceiling: auto-inject filed_at_lte when
    # the synthesizer set one for this specialist invocation and the
    # model didn't pass its own.
    ceiling = get_temporal_ceiling()
    if ceiling and not filters.get("filed_at_lte"):
        filters["filed_at_lte"] = ceiling
        logger.debug("bm25_sec: injected thread-local filed_at_lte=%s", ceiling)
    # Ticker aliases: expand e.g. GOOGL → [GOOGL, GOOG] so filings
    # indexed under the alternate ticker are also returned.
    raw_ticker = filters.get("ticker")
    if isinstance(raw_ticker, str) and raw_ticker in _SEC_TICKER_ALIASES:
        alt = _SEC_TICKER_ALIASES[raw_ticker]
        filters["ticker"] = [raw_ticker, alt]
        logger.debug("bm25_sec: expanded ticker %s → %s", raw_ticker, filters["ticker"])
    elif isinstance(raw_ticker, list):
        expanded = list(raw_ticker)
        for t in raw_ticker:
            if t in _SEC_TICKER_ALIASES and _SEC_TICKER_ALIASES[t] not in expanded:
                expanded.append(_SEC_TICKER_ALIASES[t])
        if len(expanded) > len(raw_ticker):
            filters["ticker"] = expanded
            logger.debug("bm25_sec: expanded tickers %s → %s", raw_ticker, expanded)
    translated_filters = _flat_to_os_dsl(filters)
    # Fetch at least 10 candidates so the recency re-sort below has
    # enough hits to surface the most recent filing. With k=1 the BM25
    # engine picks the best keyword match, which may be an older filing
    # that matches better on terms. After sorting we truncate to the
    # caller's requested k.
    fetch_k = max(k, 15)
    if body_mode not in ("snippet", "full", "none"):
        raise ValueError(
            f"body_mode must be one of 'snippet','full','none'; got {body_mode!r}"
        )
    env = cb_tools.bm25(
        index=SEC_FILINGS_INDEX,
        query=query,
        k=fetch_k,
        fields=list(fields) if fields else None,
        filters=translated_filters,
    )
    # Ticker-less fallback for 424B2/S-3: prospectus supplements are
    # often filed by underwriters without a ticker. If ticker + 424B2
    # returns empty, retry without the ticker filter.
    if (
        not env.get("hits")
        and filters.get("form_type") in ("424B2", "424B5", "S-3")
        and filters.get("ticker")
    ):
        fallback_filters = {k: v for k, v in filters.items() if k != "ticker"}
        translated_fb = _flat_to_os_dsl(fallback_filters)
        env = cb_tools.bm25(
            index=SEC_FILINGS_INDEX,
            query=query,
            k=fetch_k,
            fields=list(fields) if fields else None,
            filters=translated_fb,
        )
        logger.debug("bm25_sec: 424B2/S-3 ticker fallback fired, retried without ticker")
    # Table-data fallback: when top hits are narrative-only (no dollar
    # amounts or percentages in the first 3 hit bodies), retry with a
    # simplified query that targets financial table content. Tables
    # with exact figures rank low in BM25 because narrative chunks
    # have higher keyword density for the same topic.
    _hits = env.get("hits") or []
    _NUMERIC_PATTERN = re.compile(r"\$[\d,]+|\d+\.\d+%|\d{1,3}(?:,\d{3})+")
    if _hits and len(_hits) >= 3 and filters.get("ticker"):
        _top3_bodies = " ".join(
            str((h.get("source") or {}).get("body", ""))[:500]
            for h in _hits[:3]
        )
        if not _NUMERIC_PATTERN.search(_top3_bodies):
            # Top hits are narrative — retry targeting tables
            _table_query = query
            for noise in ["percentage", "percent", "partner-sourced", "customer-derived"]:
                _table_query = _table_query.replace(noise, "")
            _table_query = _table_query.strip() + " revenue amount total"
            _env2 = cb_tools.bm25(
                index=SEC_FILINGS_INDEX,
                query=_table_query,
                k=fetch_k,
                fields=list(fields) if fields else None,
                filters=translated_filters,
            )
            if _env2.get("hits"):
                # Merge and re-rank: put hits containing numbers FIRST
                _all_hits = (env.get("hits") or []) + (_env2.get("hits") or [])
                _seen_ids = set()
                _deduped: list[dict] = []
                for _h in _all_hits:
                    _hid = _h.get("id", "")
                    if _hid not in _seen_ids:
                        _seen_ids.add(_hid)
                        _deduped.append(_h)
                # Sort: hits with dollar/pct figures first, then by score
                def _has_numbers(_h: dict) -> bool:
                    _b = str((_h.get("source") or {}).get("body", ""))[:1000]
                    return bool(_NUMERIC_PATTERN.search(_b))
                _deduped.sort(key=lambda _h: (not _has_numbers(_h), -float(_h.get("score", 0))))
                env["hits"] = _deduped
                logger.debug(
                    "bm25_sec: table-data fallback fired, merged %d unique hits (numbers-first)",
                    len(_deduped),
                )
    # ANN merge (always-on when ticker is bound): BM25 ranks chunks by
    # keyword density, so a tiny revenue-disaggregation table cell
    # ("Channel partners $337,394 20%") gets buried under narrative
    # chunks that mention "channel partners" repeatedly. Semantic
    # search via BGE-M3 surfaces those chunks regardless of keyword
    # density. We fire ANN whenever a ticker filter is present (so the
    # search is scoped to one company's filings), even when BM25's top
    # hits already contain numbers — those numbers may be for the
    # wrong topic (e.g. Cloudflare BM25 returned the balance sheet
    # chunk, which has $-figures but for stockholders' equity, not
    # channel partners). Dedup against BM25 hits keeps the response
    # tight; ANN hits are appended at the end so BM25's keyword
    # confidence still wins ranking ties.
    if _hits and filters.get("ticker"):
        try:
            _ann_env = cb_tools.ann(
                index=SEC_FILINGS_ANN_INDEX,
                text=query,
                k=5,
                filters=translated_filters,
            )
            _ann_hits = _ann_env.get("hits") or []
            if _ann_hits:
                _existing_ids = {h.get("id", "") for h in (env.get("hits") or [])}
                _new_ann = [h for h in _ann_hits if h.get("id", "") not in _existing_ids]
                if _new_ann:
                    # Normalize ANN hit scores so they survive the post-
                    # sort `hits[:k]` truncation downstream. BM25 scores
                    # live in 5-30 range; ANN cosine is 0-1, so without
                    # rewriting, ANN hits sort to the bottom (same
                    # filed_at across chunks, score breaks the tie) and
                    # get cut. Slot them just above the weakest BM25
                    # hit so each ANN hit displaces one weak BM25 hit
                    # rather than getting dropped entirely; preserve
                    # ANN-internal rank with small score increments so
                    # the best ANN hit still edges the second-best.
                    _bm25_scores = [
                        float(h.get("score", 0.0)) for h in (env.get("hits") or [])
                    ]
                    _bm25_min = min(_bm25_scores) if _bm25_scores else 0.0
                    _ann_sorted = sorted(
                        _new_ann,
                        key=lambda _h: -float(_h.get("score", 0.0)),
                    )
                    for _i, _h in enumerate(_ann_sorted):
                        _h["score"] = _bm25_min + 0.01 + 0.001 * (
                            len(_ann_sorted) - _i
                        )
                    env.setdefault("hits", []).extend(_ann_sorted)
                    logger.info(
                        "bm25_sec: ANN merge added %d hits from "
                        "sec_filings_ann (scores rewritten to "
                        "[%.3f..%.3f] to survive top-k truncation)",
                        len(_ann_sorted),
                        _ann_sorted[-1].get("score", 0.0),
                        _ann_sorted[0].get("score", 0.0),
                    )
        except Exception as _ann_exc:
            # WARNING (not debug) so an upstream embedder outage stops
            # silently degrading bm25_sec — when the BGE-M3 embedder
            # returns HTTP 401 / 5xx, ANN merge can't fire and rows
            # that depend on semantic recall (like Row 33's Cloudflare
            # 20% channel-partner chunk) silently fail. Visible in
            # logs lets us page on it instead of hunting through
            # eval verdicts.
            logger.warning(
                "bm25_sec: ANN merge skipped (embedder/ann RPC failed): %s",
                _ann_exc,
            )
    # Beat-or-miss auto-upgrade: when the query looks like a guidance-
    # vs-actuals comparison, return full body for the top 3 hits instead
    # of 300-char snippets. The guidance table (IFP, GEP, Revenue, SBC,
    # CapEx, shares) is inside the 20K chunk but a snippet only captures
    # 1-2 headline metrics. Returning the full text lets the specialist
    # read the complete table without an extra get_full_text call.
    _BEAT_MISS_KEYWORDS = {"guidance", "guided", "beat", "miss", "outlook", "forecast"}
    _query_words = set(query.lower().split())
    _is_beat_miss = bool(_query_words & _BEAT_MISS_KEYWORDS)
    if _is_beat_miss and body_mode == "snippet":
        _hits_raw = env.get("hits") or []
        _full_hits = []
        for i, h in enumerate(_hits_raw):
            if i < 3:
                _full_hits.append(_project_bm25_hit(h, body_mode="full", query=query))
            else:
                _full_hits.append(_project_bm25_hit(h, body_mode=body_mode, query=query))
        projected = {"index": env.get("index"), "hits": _full_hits}
        logger.debug("bm25_sec: beat-or-miss detected, top 3 hits upgraded to full body")
    else:
        projected = _project_bm25_envelope(env, body_mode=body_mode, query=query)
    # SEC recency-first sort: filed_at desc, then score desc. For SEC
    # filings, a newer filing is almost always more useful than an
    # older one with a slightly better keyword match.
    # DEF 14A special handling: proxy statements cover the PRIOR fiscal
    # year's compensation. For "FY2023 director comp" the model needs
    # the DEF 14A filed in 2024, not the one filed in 2023. Force
    # strict recency sort (filed_at only, ignore BM25 score) so the
    # most recent proxy always comes first.
    _is_def14a = filters.get("form_type") in ("DEF 14A",)
    hits = projected.get("hits")
    if isinstance(hits, list) and hits:
        hits.sort(key=lambda h: str(h.get("id", "")))
        if _is_def14a:
            hits.sort(
                key=lambda h: str((h.get("source") or {}).get("filed_at", "")),
                reverse=True,
            )
        else:
            hits.sort(
                key=lambda h: (
                    str((h.get("source") or {}).get("filed_at", "")),
                    float(h.get("score", 0.0)),
                ),
                reverse=True,
            )
        if len(hits) > k:
            hits[:] = hits[:k]

    # Issuer-ambiguity advisory. The SEC corpus spans ~15k issuers;
    # without a ticker filter the top BM25 hits are pulled from the
    # whole corpus and almost never match the company the question
    # targets. Persona prompts already tell the model this, but in
    # practice larger models (Qwen-3-235b) learn to pattern-match
    # "earnings 8-K" without the ticker pin. Prepending this note to
    # the result envelope is a per-call correction the model sees
    # immediately after making the mistake.
    if not _filters_have_field(translated_filters, "ticker"):
        projected = {
            "_advisory": (
                "No `ticker` filter was supplied. SEC filings span "
                "~15k issuers, so these hits are BM25-matched "
                "across the whole corpus and likely do NOT include "
                "the company your question targets. RETRY with "
                "`filters.term.ticker = 'TICKER'` (upper-case, "
                "single issuer) or "
                "`filters.terms.ticker = ['A', 'B']` (multiple)."
            ),
            **projected,
        }
    return _apply_binding(bind_as, projected)


def _filters_have_field(
    filters: Optional[Mapping[str, Any]], field_name: str,
) -> bool:
    """Return ``True`` iff the OpenSearch-DSL filters dict pins
    ``field_name`` via a ``term`` or ``terms`` clause.

    Checks top-level clauses AND clauses nested inside ``bool.filter``
    (which ``_flat_to_os_dsl`` produces when multiple flat scalar
    filters are present).
    """
    if not isinstance(filters, Mapping):
        return False

    def _check(d: Mapping[str, Any]) -> bool:
        for clause in ("term", "terms"):
            body = d.get(clause)
            if isinstance(body, Mapping) and field_name in body:
                return True
        return False

    if _check(filters):
        return True
    bool_body = filters.get("bool")
    if isinstance(bool_body, Mapping):
        for clause in bool_body.get("filter", []):
            if isinstance(clause, Mapping) and _check(clause):
                return True
    return False


# OpenSearch-DSL clause types the kernel's ``filters`` arg accepts.
# Any key in the caller's filter dict matching one of these is
# treated as native DSL and passes through; anything else is
# translated from the flat shape below.
_OS_DSL_CLAUSES = frozenset({
    "term", "terms", "range", "match", "match_phrase",
    "bool", "exists", "wildcard", "prefix",
})


def _flat_to_os_dsl(
    filters: Optional[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """Translate alphacumen's flat filter shape to OpenSearch DSL.

    The kernel's ``tools.bm25`` / ``tools.ann`` accept OpenSearch-DSL
    filters (``{"term": {"field": v}, "range": {...}}``); alphacumen
    exposes a flat, domain-DSL shape to personas that's easier for
    the LLM to populate reliably (see the memory-demo ``search_sec``
    tool for the lineage -- LLMs pin named attribute slots far more
    reliably than they nest the right DSL tree). Flat keys translate:

    - scalar value -> ``term`` clause (exact match)
    - list value -> ``terms`` clause (any-of)
    - ``<field>_gte`` / ``_lte`` / ``_gt`` / ``_lt`` suffix -> ``range``
      clause (with the bound op matching the suffix)

    Known DSL clauses in the input pass through verbatim; flat keys
    alongside them merge into the same output dict so callers can
    mix the two shapes (e.g. a human-authored ``bool`` compound
    alongside LLM-emitted flat keys).
    """
    if not isinstance(filters, Mapping) or not filters:
        return None

    flat_term: dict[str, Any] = {}
    flat_terms: dict[str, list[Any]] = {}
    flat_range: dict[str, dict[str, Any]] = {}
    dsl_passthrough: dict[str, Any] = {}

    for key, value in filters.items():
        if key in _OS_DSL_CLAUSES:
            dsl_passthrough[key] = value
            continue
        matched_suffix = False
        for suffix, op in (
            ("_gte", "gte"), ("_lte", "lte"),
            ("_gt", "gt"), ("_lt", "lt"),
        ):
            if key.endswith(suffix) and len(key) > len(suffix):
                field = key[: -len(suffix)]
                flat_range.setdefault(field, {})[op] = value
                matched_suffix = True
                break
        if matched_suffix:
            continue
        if isinstance(value, list):
            flat_terms[key] = list(value)
        elif isinstance(value, Mapping) and "terms" in value:
            # Model emitted {"domain": {"terms": [...]}} — unwrap to
            # a flat terms clause so we produce {"terms": {"domain": [...]}}
            # instead of the invalid {"term": {"domain": {"terms": [...]}}}.
            flat_terms[key] = list(value["terms"])
        else:
            flat_term[key] = value

    # Date fields stored as ISO (YYYY-MM-DD). If the model passes
    # YYYYMMDD (no dashes), insert them so range comparisons work.
    # day / event_date are stored as YYYYMMDD and don't need dashes.
    _ISO_DATE_FIELDS = frozenset({"published_date", "filed_at"})

    def _maybe_add_dashes(field: str, val: Any) -> Any:
        if field not in _ISO_DATE_FIELDS or not isinstance(val, str):
            return val
        v = val.strip()
        if len(v) == 8 and v.isdigit():
            return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
        return val

    # Build individual OpenSearch clauses from flat keys.
    flat_clauses: list[dict[str, Any]] = []
    for k, v in flat_term.items():
        flat_clauses.append({"term": {k: v}})
    for k, v in flat_terms.items():
        flat_clauses.append({"terms": {k: v}})
    for field, ops in flat_range.items():
        normalized_ops = {op: _maybe_add_dashes(field, val) for op, val in ops.items()}
        flat_clauses.append({"range": {field: normalized_ops}})

    if not flat_clauses and not dsl_passthrough:
        return None

    # When there are no DSL passthrough clauses, emit compact form
    # (single clause unwrapped, multiple in bool.filter).
    if not dsl_passthrough:
        if len(flat_clauses) == 1:
            return flat_clauses[0]
        return {"bool": {"filter": flat_clauses}}

    # DSL passthrough exists. Wrap ALL clauses (flat + DSL) in
    # bool.filter so OpenSearch sees one valid query tree. Keeping
    # DSL clauses like {"range": {"goldstein_scale": ...}} at the
    # top level alongside flat-generated {"range": {"day": ...}}
    # causes "range doesn't support multiple fields" errors.
    all_clauses = list(flat_clauses)
    for dsl_key, dsl_val in dsl_passthrough.items():
        if dsl_key == "bool" and isinstance(dsl_val, Mapping):
            all_clauses.extend(dsl_val.get("filter", []))
        else:
            all_clauses.append({dsl_key: dsl_val})
    if len(all_clauses) == 1:
        return all_clauses[0]
    return {"bool": {"filter": all_clauses}} if all_clauses else None


BM25_SEC = Tool(
    name="bm25_sec",
    description=(
        "BM25 search over SEC EDGAR filing bodies (8-K, 10-K, 10-Q). "
        "Returns ranked hits with `full_text_ref`-shaped ids. By "
        "default each hit's body is replaced with a ~300-char snippet "
        "centered on the BM25 match -- enough to verify the hit is "
        "the right filing/section, NOT enough to quote a number from. "
        "Decide per hit: pass the id to `get_xbrl_facts` for numeric "
        "values (revenue, EPS, KPIs, balance-sheet items) or to "
        "`get_full_text` for narrative (MD&A risk factors, item "
        "discussion). Override with `body_mode='full'` only when you "
        "specifically need the entire chunk in one shot. "
        "IMPORTANT: never pass form-type tokens like '8-K' as the "
        "query (BM25 scores them ~0 because every filing contains "
        "them). For earnings filings use 'revenue net income EPS "
        "guidance'; for governance filings use 'departure director "
        "officer compensation'. Pass `bind_as=<name>` to ALSO "
        "expose the hit list as a Python variable for `run_python`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {
                "type": "integer", "default": 10,
                "minimum": 1, "maximum": 50,
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of registered field names; see "
                    "the field list below."
                ),
            },
            "filters": {
                "type": "object",
                "description": (
                    "Optional pre-filters (AND-combined). PREFERRED "
                    "flat shape -- ALWAYS pin the ticker when you know "
                    "the issuer: "
                    '{"ticker": "NVDA", "form_type": "8-K", '
                    '"event_date_gte": "20260120", '
                    '"event_date_lte": "20260331"}. Bare field name '
                    "= exact match (scalar) or any-of (list); append "
                    "`_gte`/`_lte`/`_gt`/`_lt` for range bounds. "
                    "Filterable fields are listed below. OpenSearch "
                    "DSL is also accepted: "
                    '{"term": {"ticker": "NVDA"}, '
                    '"range": {"event_date": {"gte": "20260101"}}}.'
                ),
            },
            "body_mode": {
                "type": "string",
                "enum": ["snippet", "full", "none"],
                "default": "snippet",
                "description": (
                    "How much of each hit's body to return. "
                    "'snippet' (default) -- ~300 chars around the "
                    "BM25 match; use this and then drill into the "
                    "right hit with get_xbrl_facts (numbers) or "
                    "get_full_text (narrative). 'full' -- the whole "
                    "indexed chunk (~20K chars per hit, can blow "
                    "context with k>3). 'none' -- metadata only, "
                    "for filing-enumeration questions where titles "
                    "and dates are sufficient."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["query"],
    },
    fn=_do_bm25_sec,
    bound_indices=((SEC_FILINGS_INDEX, ("bm25",)),),
)


def _do_vector_scraped_articles(
    query: str,
    *,
    k: int = 8,
    filters: Optional[Mapping[str, Any]] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """``tools.ann`` against the BGE-M3 scraped-articles index.

    The legacy implementation did a hybrid BM25+ANN+RRF on the same
    corpus inside one tool; we deliberately do not fuse here. The
    gateway runs the embedder server-side from raw text so the
    pipeline never has to know about BGE-M3, and the model can mix
    in the lexical leg by separately invoking
    :data:`BM25_SCRAPED_ARTICLES` against the same index. Cross-verb
    fusion (BM25 + ANN) is the model's job, not the tool's.

    ``filters`` accept the same flat-or-DSL shape as the sibling
    BM25 tools (forwarded to ``cb_tools.ann``). They are applied
    **before** the HNSW scoring stage -- the kNN graph is restricted
    to docs that match the filter, not post-filtered after the fact.
    Without a filter the cluster runs unfiltered HNSW over the full
    corpus (~29M docs); on a cold cache that can blow past the
    gateway's vector timeout. Passing a tight ``published_date``
    range is the single biggest knob for keeping ANN latency in
    budget.

    When ``bind_as`` is set, the projected envelope is also bound
    under that name in the in-runner Python interpreter for use
    in a later ``run_python`` snippet (e.g. RRF fusion with the
    sibling BM25 leg). The return value is unchanged.
    """
    env = cb_tools.ann(
        index=SCRAPED_ARTICLES_INDEX,
        text=query,
        k=k,
        filters=_flat_to_os_dsl(filters),
    )
    projected = _project_bm25_envelope(env)
    return _apply_binding(bind_as, projected)


VECTOR_SCRAPED_ARTICLES = Tool(
    name="vector_scraped_articles",
    description=(
        "Approximate semantic search (BGE-M3 vector) over the "
        "scraped web-articles corpus (~29M docs, OpenSearch HNSW). "
        "Best for long-tail web news and blogs that GDELT may not "
        "cover -- VC ecosystem coverage, narrative analysis, "
        "opinion pieces. Returns ranked hits with the embedding "
        "stripped. Pair with bm25_scraped_articles when the query "
        "mentions specific named entities the lexical leg will "
        "catch better. RECOMMENDED when you intend to fuse with "
        "the BM25 leg: pass `bind_as=<name>` so a later "
        "`run_python` RRF snippet can reference the hits by name "
        "without you re-emitting them as `inputs=`. "
        "FILTERS MATTER A LOT on this index -- it is "
        "DISK-RESIDENT (mode=on_disk, 16x compression). The "
        "OpenSearch kNN engine takes the fast pre-filter path "
        "ONLY when filters cut the candidate set to under ~10% of "
        "the corpus; above that it falls back to a full HNSW scan + "
        "post-filter (no speedup vs unfiltered). Concretely: a "
        "30-90 day `published_date` range gets you there on its "
        "own and is the PREFERRED selectivity knob -- prefer it "
        "over a `domain` filter whenever the question's time "
        "horizon allows. A multi-year range does NOT cut the "
        "candidate set on its own -- in that case ALSO add a "
        "`domain` `terms` filter, but the filter is for LATENCY, "
        "not credibility, so the list should be WIDE (15-25 "
        "domains) and topic-appropriate. Use general business "
        "press for macro/markets questions (reuters.com, "
        "bloomberg.com, ft.com, wsj.com, cnbc.com, fortune.com, "
        "businessinsider.com, finance.yahoo.com, marketwatch.com, "
        "theguardian.com); ADD enterprise / trade press for any "
        "B2B / SaaS / data-platform / cloud / AI question "
        "(siliconangle.com, theregister.com, techcrunch.com, "
        "venturebeat.com, theverge.com, arstechnica.com, "
        "zdnet.com, crn.com, infoworld.com, computerworld.com); "
        "ADD wire / press-release distribution when the question "
        "is about announcements, launches, or partnerships "
        "(prnewswire.com, businesswire.com, globenewswire.com); "
        "ADD finance dailies / equity-research aggregators for "
        "earnings / guidance / valuation questions "
        "(seekingalpha.com, benzinga.com, fool.com, "
        "marketscreener.com). DO NOT default to the 8-outlet "
        "general-news list -- it omits exactly the trade press "
        "and wires where enterprise product launches and "
        "partnerships break first. "
        "Latency profile with selective filters: cold first call "
        "10-30s while HNSW segments fault into memory; subsequent "
        "calls in the same run land in 200ms-2s. If a call times "
        "out, the right move is USUALLY to retry the same call "
        "once (the page cache is now warm). If it times out a "
        "SECOND time, your filter is in the post-filter regime -- "
        "narrow `published_date` to a quarter and add a `domain` "
        "`terms` filter."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {
                "type": "integer", "default": 8,
                "minimum": 1, "maximum": 30,
            },
            "filters": {
                "type": "object",
                "description": (
                    "Optional pre-filters (AND-combined) applied "
                    "before HNSW scoring. PREFERRED flat shape: "
                    '{"published_date_gte": "2026-01-01", '
                    '"published_date_lte": "2026-03-31"} '
                    "(append `_gte`/`_lte`/`_gt`/`_lt` for ranges; "
                    "bare field = exact match). OpenSearch DSL also "
                    "accepted (e.g. `bool`/`filter` for OR/NOT or "
                    "to combine multiple clauses). Two filterable "
                    "fields are advertised: "
                    "(1) **`published_date`** (date, ISO YYYY-MM-DD "
                    "-- NOT `event_date`, `day`, or any other guess; "
                    "OpenSearch silently returns 0 hits for filters "
                    "on non-existent fields). "
                    "(2) **`domain`** (keyword, publisher hostname). "
                    "Selectivity rule for this disk-resident index: "
                    "prefer a tight `published_date` range alone "
                    "(<=90 days is plenty); only ADD a `domain` "
                    "`terms` clause when you need a multi-year "
                    "window, and when you do, the list is for "
                    "LATENCY not credibility -- make it WIDE "
                    "(15-25 domains) and topic-appropriate, "
                    "including trade press / wires / finance "
                    "dailies relevant to the question (NOT just "
                    "the 8 general-news outlets). Example for an "
                    "enterprise / SaaS launches / partnerships "
                    "question: "
                    '{"bool": {"filter": ['
                    '{"range": {"published_date": '
                    '{"gte": "2025-01-01", "lte": "2026-04-28"}}}, '
                    '{"terms": {"domain": ['
                    '"reuters.com", "bloomberg.com", "cnbc.com", '
                    '"techcrunch.com", "siliconangle.com", '
                    '"theregister.com", "venturebeat.com", '
                    '"theverge.com", "arstechnica.com", '
                    '"crn.com", "zdnet.com", "infoworld.com", '
                    '"prnewswire.com", "businesswire.com", '
                    '"globenewswire.com", "seekingalpha.com", '
                    '"benzinga.com", "fortune.com"]}}]}}. '
                    "If you only need a tight time window "
                    "(<= 90 days), a bare `published_date` range "
                    "(or `published_date_gte`/`published_date_lte` "
                    "in flat form) is also fine -- skip `domain` "
                    "entirely so trade-press launch announcements "
                    "are not filtered out."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["query"],
    },
    fn=_do_vector_scraped_articles,
    bound_indices=((SCRAPED_ARTICLES_INDEX, ("ann",)),),
)


def _do_bm25_scraped_articles(
    query: str,
    *,
    k: int = 8,
    fields: Optional[Sequence[str]] = None,
    filters: Optional[Mapping[str, Any]] = None,
    sort: Optional[Sequence[Mapping[str, str]]] = None,
    bind_as: Optional[str] = None,
    **extra: Any,
) -> dict[str, Any]:
    """BM25 leg over the scraped-articles index (OpenSearch).

    Same shape as :func:`_do_bm25_gdelt` against a different slug.
    The scraped-articles index advertises both ``bm25`` and ``ann``
    capabilities at registration; this tool exposes the lexical leg
    so the model can author its own hybrid by issuing
    ``vector_scraped_articles`` + ``bm25_scraped_articles`` and
    reasoning over both rankings.
    """
    if sort:
        logger.debug(
            "bm25_scraped_articles: sort= no-op (hits stay in score "
            "order): %r", sort,
        )
    if extra:
        filters = dict(filters or {})
        filters.update(extra)
    # Recency floor: inject published_date_gte when the model omits date
    # context entirely. Skip the floor when the model passes ANY date
    # filter (published_date_gte / published_date_lte) — that means
    # it's targeting a specific historical period (e.g. "Netflix
    # 2019–2024") and the floor would silently push gte past lte and
    # return zero hits.
    if filters is None:
        filters = {}
    else:
        filters = dict(filters)
    has_explicit_date = any(
        filters.get(k) for k in ("published_date_gte", "published_date_lte")
    )
    if not has_explicit_date:
        from datetime import date, timedelta
        sa_floor = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
        filters["published_date_gte"] = sa_floor
        logger.debug(
            "bm25_scraped_articles: injected default published_date_gte=%s "
            "(no explicit date filter)",
            sa_floor,
        )
    env = cb_tools.bm25(
        index=SCRAPED_ARTICLES_INDEX,
        query=query,
        k=k,
        fields=list(fields) if fields else None,
        filters=_flat_to_os_dsl(filters),
    )
    projected = _project_bm25_envelope(env)
    # Recency-first sort: published_date desc, then score desc.
    hits = projected.get("hits")
    if isinstance(hits, list) and hits:
        hits.sort(
            key=lambda h: (
                str((h.get("source") or {}).get("published_date", "")),
                float(h.get("score", 0.0)),
            ),
            reverse=True,
        )
    return _apply_binding(bind_as, projected)


BM25_SCRAPED_ARTICLES = Tool(
    name="bm25_scraped_articles",
    description=(
        "BM25 lexical search over the scraped web-articles corpus "
        "(~29M docs, OpenSearch). Use when the query has specific "
        "named entities, product codenames, or technical terms that "
        "semantic search may dilute. Pair with "
        "vector_scraped_articles for narrative / topical coverage; "
        "the model fuses the two rankings (no server-side RRF). "
        "Pass `fields` to bias the score toward registered fields "
        "(see field list below). RECOMMENDED when fusing with the "
        "vector leg: pass `bind_as=<name>` (and the same on the "
        "vector call) so the RRF `run_python` snippet can read "
        "both lists by name."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {
                "type": "integer", "default": 8,
                "minimum": 1, "maximum": 30,
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of registered field names "
                    "(optionally with `^boost`). See the field "
                    "list below; unknown names are rejected."
                ),
            },
            "filters": {
                "type": "object",
                "description": (
                    "Optional pre-filters (AND-combined). PREFERRED "
                    "flat shape: "
                    '{"published_date_gte": "2026-01-01", '
                    '"published_date_lte": "2026-03-31"} '
                    "(append `_gte`/`_lte`/`_gt`/`_lt` for ranges; "
                    "bare field = exact match). OpenSearch DSL also "
                    "accepted: "
                    '{"range": {"published_date": {"gte": "2026-01-01"}}}.'
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["query"],
    },
    fn=_do_bm25_scraped_articles,
    bound_indices=((SCRAPED_ARTICLES_INDEX, ("bm25",)),),
)


def _do_get_full_text(
    ref: str,
    *,
    max_chars: int = 16_000,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Hydrate a single document by ``(index, id)`` via ``tools.get``.

    ``ref`` is the ``id`` field the BM25 / ANN hits return. For SEC
    filings and scraped articles, that id already carries a source
    prefix (e.g. ``"sec:0001045810-26-000019:2.02"``) and the tpuf
    ``doc_id`` attribute is stored in the same prefixed shape -- we
    use the full ref verbatim as the lookup key so callers can pass
    whatever they got from the hit list without stripping anything.

    The prefix is only used to route to the right index:

    - ``"sec:..."``  -> SEC filings index.
    - ``"art:..."`` / ``"scraped:..."``  -> scraped articles index.
    - Anything else  -> :data:`GDELT_EVENTS_INDEX` (bare GDELT id).
    """
    _HEX32 = re.compile(r"^[0-9a-f]{32}$")
    prefix, sep, _rest = ref.partition(":")
    if not sep:
        if _HEX32.match(ref):
            # 32-char hex hash — scraped article ID. Try scraped first.
            index = SCRAPED_ARTICLES_INDEX
        else:
            index = GDELT_EVENTS_INDEX
    elif prefix == "sec":
        index = SEC_FILINGS_INDEX
    elif prefix in ("art", "scraped"):
        index = SCRAPED_ARTICLES_INDEX
    else:
        index = GDELT_EVENTS_INDEX

    cache_key = (ref, int(max_chars))
    cached = _FULL_TEXT_CACHE.get(cache_key)
    if cached is None:
        env = cb_tools.get(index=index, id=ref)
        # Fallback: if scraped articles didn't find it, try GDELT
        if not env.get("found") and index == SCRAPED_ARTICLES_INDEX:
            env = cb_tools.get(index=GDELT_EVENTS_INDEX, id=ref)
        if not env.get("found"):
            cached = {"index": env.get("index"), "found": False, "ref": ref}
        else:
            body_doc = env.get("doc") or {}
            src = dict(body_doc.get("source") or {})
            for key in ("embedding", "embedding_vec", "vector", "vec"):
                src.pop(key, None)

            # Find the longest text-ish field and clip to ``max_chars``.
            # SEC docs put the body in ``body``; scraped articles use
            # ``content`` or ``text``; GDELT uses ``snippet``. Scanning by
            # known names keeps the truncation predictable and lets the
            # model match on a known key when reading the result.
            body_text = ""
            body_key = ""
            for key in ("body", "article_text", "content", "text", "snippet"):
                v = src.get(key)
                if isinstance(v, str) and v:
                    body_text = v
                    body_key = key
                    break

            # EDGAR-direct fallback for SEC filings with stub or
            # iXBRL-cover bodies. Two flavors of stubs in our index:
            # (1) Older 6-K / 8-K HTML wrappers that slipped through
            # the chunking pipeline with only the filename + form
            # header extracted (~49-1500 chars). For those, the
            # primary-doc HTML is publicly hosted at sec.gov and the
            # body has the real text. (2) Modern 8-K item sub-docs
            # (e.g. `sec:<acc>:2.02`) where the body is mostly iXBRL
            # XBRL/XML metadata (tokens like `false 0000XXX`,
            # `us-gaap:`, `gis:M0.125NotesDue20...`) and the actual
            # press-release content lives in an attached exhibit
            # (ex99.X). For those, walk the filing directory and
            # concatenate every .htm exhibit (same logic as
            # extract_filing_tables uses).
            # EDGAR-direct fallback. Three triggers, in order of
            # specificity:
            # (1) 8-K item subdocs (`sec:<acc>:<item_key>` where
            #     item_key is e.g. `2.02`, `7.01`): the body is always
            #     a cover stub — actual press-release content lives
            #     in the ex99.X attachment. Walk the filing directory
            #     and concatenate every .htm exhibit. The chunker
            #     pipeline expanded the iXBRL header into the body
            #     field but never followed the exhibit references.
            # (2) Short stubs (< 2000 chars): older 6-K / 8-K HTML
            #     wrappers that fell through the chunking pipeline
            #     with only filename + form header extracted. Fetch
            #     the primary-doc URL directly.
            edgar_fallback_used = False
            ref_parts = ref.split(":") if ref.startswith("sec:") else []
            is_8k_item_subdoc = (
                len(ref_parts) >= 3
                and re.fullmatch(r"\d+\.\d+", ref_parts[2]) is not None
            )
            is_short_stub = (
                index == SEC_FILINGS_INDEX
                and ref.startswith("sec:")
                and len(body_text) < 2000
            )
            # Long-form-stub fallback: 10-K / 10-Q / 20-F filings
            # routinely run 100K-1M chars of text. If the indexed
            # body is below ~10K chars for any of these form types,
            # the chunking pipeline failed to extract the substantive
            # body and only kept the cover page (e.g. INTC FY2024
            # 10-K: indexed body 3,912 chars from a 3.3 MB raw HTML
            # because BeautifulSoup couldn't parse INTC's inline-XBRL
            # / div-based layout). Trigger EDGAR direct-fetch +
            # re-parse to recover the full body. Form is read from
            # src['form_type'] (preferred) or inferred from the
            # filing URL extension.
            form_type_raw = str(
                src.get("form_type") or src.get("formType") or ""
            ).upper()
            is_long_form_stub = (
                index == SEC_FILINGS_INDEX
                and ref.startswith("sec:")
                and form_type_raw in (
                    "10-K", "10-K/A", "10-Q", "10-Q/A",
                    "20-F", "20-F/A", "S-1", "S-1/A",
                )
                and len(body_text) < 10_000
            )
            if is_8k_item_subdoc or is_short_stub or is_long_form_stub:
                edgar_url = src.get("url") or ""
                combined_text = ""
                if is_8k_item_subdoc and edgar_url.startswith("https://www.sec.gov/"):
                    # The item-subdoc URL points at the cover sheet
                    # (e.g. `four-20240509.htm`); the actual content
                    # lives in adjacent ex99.X exhibits. Walk all
                    # .htm files in the same directory.
                    exhibits_html, _err = _fetch_filing_exhibits_from_edgar(edgar_url)
                    if exhibits_html:
                        try:
                            from bs4 import BeautifulSoup  # noqa: PLC0415
                            combined_text = BeautifulSoup(exhibits_html, "html.parser").get_text(" ", strip=True)
                        except Exception:  # noqa: BLE001
                            combined_text = ""
                if not combined_text and edgar_url.startswith("https://www.sec.gov/"):
                    html, _edgar_err = _fetch_filing_html_from_edgar(edgar_url)
                    if html:
                        try:
                            from bs4 import BeautifulSoup  # noqa: PLC0415
                            combined_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
                        except Exception:  # noqa: BLE001
                            combined_text = ""
                if combined_text and len(combined_text) > len(body_text):
                    body_text = combined_text
                    if not body_key:
                        body_key = "body"
                    src[body_key] = body_text
                    edgar_fallback_used = True

            if body_text and len(body_text) > max_chars:
                body_text = body_text[:max_chars] + (
                    f"\n... [truncated at {max_chars} chars]"
                )
                if body_key:
                    src[body_key] = body_text

            cached = {
                "index": env.get("index"),
                "found": True,
                "ref": ref,
                "id": body_doc.get("id"),
                "source": src,
            }
            if edgar_fallback_used:
                cached["source_origin"] = "edgar_direct"
        _FULL_TEXT_CACHE[cache_key] = cached
    return _apply_binding(bind_as, copy.deepcopy(cached))


# --------------------------------------------------------------------------
# XBRL fact retrieval (sec-api.io xbrl-to-json)
# --------------------------------------------------------------------------

# sec-api.io XBRL-to-JSON converter. Returns the iXBRL facts of an
# SEC filing as a structured JSON tree (one section per US-GAAP
# statement: ``CoverPage``, ``StatementsOfIncome``, ``BalanceSheets``,
# ``StatementsOfCashFlows``, etc., plus any company-extension facts
# the filer tagged). We ship the request from inside the sandbox
# subprocess; the gateway's credential injector ships ``SEC_API_KEY``
# into the env when the manifest declares ``api.sec-api.io`` in its
# egress allowlist (same shape as the Langfuse integration).
_SEC_API_XBRL_URL = "https://api.sec-api.io/xbrl-to-json"
_SEC_API_TIMEOUT_S = 30.0
_SEC_API_SSL_CTX = ssl.create_default_context()

# Per-run cache: avoid re-fetching the same filing's XBRL tree if
# the model issues two get_xbrl_facts calls on the same ref with
# different concept_pattern / periods filters. Keyed by accession
# alone; filtering is applied to the cached tree.
_XBRL_FACTS_CACHE: dict[str, dict[str, Any]] = {}

# Hard cap on facts returned to the model (post-filter). The Netflix
# 10-K iXBRL has ~3000 facts; an unfiltered fetch with no cap would
# blow the model context. The model can narrow with concept_pattern
# / periods if 200 isn't enough for its question.
_XBRL_FACTS_MAX = 200

# Approximate cap on the JSON length of the *upstream* sec-api response
# we keep in cache. A quarterly 10-Q's iXBRL tree is ~500 KB, but a
# large annual filing's can be much bigger -- AES's FY2022 10-K is
# ~7.3 MB, and bailing on it forced the swarm to scrape line items out
# of a raw text chunk and misread net loss ($505M vs the correct $546M),
# flipping a FinanceBench ROA answer (-1.42% -> -0.01 instead of -0.02).
# The cap only guards the fetch/parse step -- ``_do_get_xbrl_facts``
# already post-filters to ``_XBRL_FACTS_MAX`` facts + ``concept_pattern``
# before anything reaches the model context -- so it can be generous.
_XBRL_RESPONSE_MAX_CHARS = 16_000_000


def _xbrl_extract_accession(ref: str) -> str:
    """Pull the accession number out of a bm25_sec hit ref.

    The ref shape is ``"sec:<accession>[:<item>]"`` -- e.g.
    ``"sec:0001065280-24-000128:7"``. Returns just the accession.
    """
    s = ref.strip()
    if s.startswith("sec:"):
        s = s[4:]
    # Drop any item-suffix (``:7``, ``:2.02``, etc.).
    return s.split(":", 1)[0]


def _xbrl_flatten(tree: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Walk the sec-api XBRL-JSON tree to a flat fact list.

    sec-api.io returns one top-level key per statement / cover-page
    section; each section's value is a dict of ``{concept: [fact, ...]}``
    where each fact carries ``value``, ``period`` (``{startDate, endDate}``
    or ``{instant}``), ``decimals``, ``unitRef``, etc. Some
    company-extension concepts use a ``:`` in the key (``nflx:Foo``).
    We project to a uniform shape so the model never has to parse the
    nested tree.
    """
    facts: list[dict[str, Any]] = []
    for section, body in tree.items():
        if not isinstance(body, Mapping):
            continue
        for concept, raw in body.items():
            entries = raw if isinstance(raw, list) else [raw]
            for fact in entries:
                if not isinstance(fact, Mapping):
                    continue
                period = fact.get("period") or {}
                if isinstance(period, Mapping):
                    period_view = {
                        k: period.get(k)
                        for k in ("startDate", "endDate", "instant")
                        if period.get(k)
                    }
                else:
                    period_view = {}
                # ``segment`` carries the iXBRL dimension axis + member
                # for facts that are dimensionalized -- e.g. cover-page
                # ``EntityCommonStockSharesOutstanding`` is filed once
                # per share class with a ``StatementClassOfStockAxis ->
                # CommonClassAMember`` segment. sec-api's JSON encodes
                # this as either a single mapping or a list of mappings
                # (one per axis when multiple dimensions are stacked).
                # Normalize to a list of ``{dimension, value}`` dicts so
                # downstream code never has to branch.
                seg_raw = fact.get("segment") or []
                if isinstance(seg_raw, Mapping):
                    seg_raw = [seg_raw]
                segment = [
                    {"dimension": s.get("dimension"), "value": s.get("value")}
                    for s in seg_raw
                    if isinstance(s, Mapping) and (s.get("dimension") or s.get("value"))
                ]
                facts.append({
                    "section": section,
                    "concept": concept,
                    "value": fact.get("value"),
                    "period": period_view,
                    "unit": fact.get("unitRef"),
                    "decimals": fact.get("decimals"),
                    "is_extension": ":" in str(concept),
                    "segment": segment,
                })
    return facts


def _xbrl_filter(
    facts: Sequence[Mapping[str, Any]],
    *,
    concept_pattern: Optional[str],
    periods: Optional[Sequence[str]],
) -> list[dict[str, Any]]:
    """Apply concept (substring, case-insensitive) and period filters."""
    out: list[dict[str, Any]] = []
    cp = (concept_pattern or "").strip().lower()
    period_strs = tuple(str(p) for p in (periods or []))
    for f in facts:
        if cp:
            concept = str(f.get("concept") or "").lower()
            if cp not in concept:
                continue
        if period_strs:
            period = f.get("period") or {}
            joined = " ".join(
                str(v) for v in (
                    period.get("startDate"), period.get("endDate"),
                    period.get("instant"),
                ) if v
            )
            if not any(p in joined for p in period_strs):
                continue
        out.append(dict(f))
    return out


def _do_get_xbrl_facts(
    ref: str,
    *,
    concept_pattern: Optional[str] = None,
    periods: Optional[Sequence[str]] = None,
    limit: int = 50,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch the iXBRL facts for a SEC filing as a flat list.

    ``ref`` is a hit id from :func:`_do_bm25_sec` (shape:
    ``"sec:<accession>[:<item>]"``). The accession is extracted and
    submitted to sec-api.io's XBRL-to-JSON converter; the structured
    tree is flattened to ``[{section, concept, value, period, unit,
    decimals, is_extension}, ...]`` and filtered by ``concept_pattern``
    (case-insensitive substring) and ``periods`` (list of strings,
    matched against startDate / endDate / instant).

    Use this instead of quoting numbers from BM25 narrative chunks --
    iXBRL facts are the same numbers the company filed with the SEC,
    untouched by table-rendering or LLM paraphrasing.
    """
    api_key = os.environ.get("SEC_API_KEY", "").strip()
    if not api_key:
        return {
            "ref": ref,
            "error": (
                "SEC_API_KEY not present in sandbox env. The gateway "
                "credential injector should ship it when the alphacumen "
                "manifest declares 'api.sec-api.io' in egress; if "
                "you're seeing this in prod, check the gateway's "
                "ManifestSecretsManagerResolver logs."
            ),
            "facts": [],
        }
    accession = _xbrl_extract_accession(ref)
    if not accession:
        return {"ref": ref, "error": f"could not parse accession from ref={ref!r}", "facts": []}

    cached = _XBRL_FACTS_CACHE.get(accession)
    if cached is None:
        params = urllib.parse.urlencode({"accession-no": accession, "token": api_key})
        url = f"{_SEC_API_XBRL_URL}?{params}"
        req = urllib.request.Request(url=url, method="GET")
        # sec-api transparently gzips xbrl-to-json responses for some
        # filings (large 10-Ks especially -- ABNB's FY2024 response is
        # ~600 KB compressed, multi-MB raw). urllib does not auto-
        # decompress, so we must opt in and gunzip ourselves; without
        # this, large filings raise UnicodeDecodeError when ``decode
        # ('utf-8')`` runs over raw gzip bytes. Mirrors the same gzip
        # handling already in _fetch_filing_section_html.
        req.add_header("Accept-Encoding", "gzip")
        try:
            with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S, context=_SEC_API_SSL_CTX) as resp:
                body = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip" or body[:2] == b"\x1f\x8b":
                    import gzip  # noqa: PLC0415
                    body = gzip.decompress(body)
                raw = body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                text = exc.read().decode("utf-8")[:500]
            except Exception:  # noqa: BLE001
                pass
            return {
                "ref": ref, "accession": accession,
                "error": f"sec-api HTTP {exc.code}: {text or exc.reason}",
                "facts": [],
            }
        except urllib.error.URLError as exc:
            return {
                "ref": ref, "accession": accession,
                "error": f"sec-api unreachable: {exc.reason}",
                "facts": [],
            }
        if len(raw) > _XBRL_RESPONSE_MAX_CHARS:
            return {
                "ref": ref, "accession": accession,
                "error": f"sec-api response too large ({len(raw)} chars > cap {_XBRL_RESPONSE_MAX_CHARS})",
                "facts": [],
            }
        try:
            tree = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {
                "ref": ref, "accession": accession,
                "error": f"sec-api response not JSON: {exc}",
                "facts": [],
            }
        if not isinstance(tree, Mapping):
            return {
                "ref": ref, "accession": accession,
                "error": f"sec-api response is not a JSON object (got {type(tree).__name__})",
                "facts": [],
            }
        flat = _xbrl_flatten(tree)
        cached = {
            "accession": accession,
            "fact_count": len(flat),
            "sections": sorted({str(f.get("section")) for f in flat if f.get("section")}),
            "facts": flat,
        }
        _XBRL_FACTS_CACHE[accession] = cached

    filtered = _xbrl_filter(
        cached["facts"],
        concept_pattern=concept_pattern,
        periods=periods,
    )
    cap = max(1, min(int(limit), _XBRL_FACTS_MAX))
    truncated = filtered[:cap]
    result = {
        "ref": ref,
        "accession": cached["accession"],
        "total_facts_in_filing": cached["fact_count"],
        "sections_in_filing": cached["sections"],
        "matched_count": len(filtered),
        "returned_count": len(truncated),
        "concept_pattern": concept_pattern,
        "periods": list(periods) if periods else None,
        "facts": truncated,
    }
    if len(filtered) > len(truncated):
        result["truncation_note"] = (
            f"Returned {len(truncated)} of {len(filtered)} matching facts "
            f"(limit={cap}). Tighten concept_pattern or periods to narrow."
        )
    return _apply_binding(bind_as, result)


GET_XBRL_FACTS = Tool(
    name="get_xbrl_facts",
    description=(
        "Retrieve the iXBRL-tagged numeric facts for a SEC filing as "
        "a flat list -- the same numbers the issuer filed with the "
        "SEC, untouched by table rendering or LLM paraphrasing. "
        "Use this for ANY specific numeric value (revenue line items, "
        "EPS, segment metrics, balance-sheet items, KPIs like ARM / "
        "ARPPU / paid memberships); do NOT quote a number from a "
        "BM25 narrative chunk -- chunks are paraphrased / partial. "
        "Pass `ref` from a `bm25_sec` hit "
        "(shape: 'sec:<accession>[:<item>]'). Filter with "
        "`concept_pattern` (case-insensitive substring on the concept "
        "name -- e.g. 'revenue', 'AverageRevenuePerMembership', "
        "'EarningsPerShare') and `periods` (list of date strings; "
        "matched as substrings against startDate / endDate / "
        "instant). Company-extension concepts (filer-specific KPIs) "
        "carry a 'nflx:' / 'tsla:' / etc. prefix -- the response's "
        "is_extension=true marks them. If the filer didn't tag the "
        "value you need (some non-financial KPIs are narrative-only), "
        "fall back to get_full_text for the relevant section."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "Hit id from bm25_sec, shape "
                    "'sec:<accession>[:<item>]'. The accession is "
                    "what's used; any item suffix is ignored."
                ),
            },
            "concept_pattern": {
                "type": "string",
                "description": (
                    "Case-insensitive substring match on the concept "
                    "name. Examples: 'revenue', 'OperatingIncome', "
                    "'AverageRevenuePerMembership', 'CostOfGoodsSold'. "
                    "Omit to return all facts (capped at limit)."
                ),
            },
            "periods": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of date-string filters. Each "
                    "string is matched as a substring against the "
                    "fact's period startDate, endDate, and instant. "
                    "Examples: ['2023', '2024'] for full-year facts; "
                    "['2024-12-31'] for instants on that date."
                ),
            },
            "limit": {
                "type": "integer",
                "default": 50,
                "minimum": 1,
                "maximum": 200,
                "description": (
                    "Max facts to return after filtering. Default 50. "
                    "If matched_count > returned_count, narrow the "
                    "filter rather than raising this -- the model "
                    "rarely benefits from >50 facts in one shot."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ref"],
    },
    fn=_do_get_xbrl_facts,
)


# --------------------------------------------------------------------------
# extract_filing_tables -- pull <table> elements from an SEC filing
# --------------------------------------------------------------------------
#
# Why this exists. Many of the numbers Vals AI grades us on are
# **non-GAAP operating KPIs** (Netflix ARM, Spotify MAU, segment KPI
# tables) that the issuer publishes in MD&A as plain HTML tables but
# does NOT tag in iXBRL. ``get_xbrl_facts`` returns nothing for those
# concepts; ``get_full_text`` returns the chunk-flattened text where
# ``<table>`` boundaries have already been collapsed to newlines by
# the SEC ingestion pipeline (``data_prep/sec_edgar/clean_filings``
# strips/flattens HTML at index time). To get the structured table
# back we have to fetch the raw filing HTML upstream of the index.
#
# Two-step path (both via sec-api, already in the manifest egress):
#   1. Query API: accession -> filing's primary document URL
#   2. Extractor API: filing URL + (optional) item -> HTML region
# Then parse with BeautifulSoup, find tables matching a caption /
# heading / first-column substring, and return each as Markdown rows.

_SEC_API_QUERY_URL = "https://api.sec-api.io"
_SEC_API_EXTRACTOR_URL = "https://api.sec-api.io/extractor"

# Filing-URL cache keyed by accession -- the URL never changes after
# filing, so once we resolve it we never re-query for the same
# accession in the same run.
_FILING_URL_CACHE: dict[str, str] = {}

# Per-(accession, item) raw-HTML cache. Extractor calls are slower than
# Query calls (Item 7 of a 10-K is a few hundred KB of HTML); a single
# run that asks for two different keywords on the same item should
# only fetch once.
_FILING_HTML_CACHE: dict[tuple[str, str], str] = {}

# Hard cap on Extractor response length we're willing to parse. Item 7
# of a Netflix 10-K is ~300 KB, but Item 8 (full financial statements +
# notes) of a large filer can be much bigger -- AES's FY2022 10-K Item 8
# came back ~5 MB and bailing on it left the swarm scraping line items
# from raw text (see _XBRL_RESPONSE_MAX_CHARS above). Only ``limit``
# tables (post-keyword-filter) reach the model context, so this fetch
# cap can be generous; it just guards against a pathological response.
_FILING_HTML_MAX_CHARS = 12_000_000

# Cap on tables returned per call (post-keyword-filter). Each table
# rendered to Markdown is typically 200-3000 chars; 5 tables keeps the
# response well under the global ``_TOOL_RESULT_MAX_CHARS`` ceiling.
_FILING_TABLES_MAX = 5

# Cap on rows per returned table -- some filings have 100+-row
# schedules of regional data that would dominate the response. The
# model rarely benefits from >40 rows of one table; if the question
# needs more, pass a tighter ``table_keyword``.
_FILING_TABLE_ROWS_MAX = 40


def _filing_url_for_accession(accession: str, api_key: str) -> tuple[str | None, str | None]:
    """Look up the primary-document URL for ``accession`` via sec-api.

    Returns ``(filing_url, error)``. Cached per-accession for the run.
    """
    cached = _FILING_URL_CACHE.get(accession)
    if cached is not None:
        return cached, None
    body = json.dumps({
        "query": f'accessionNo:"{accession}"',
        "from": "0", "size": "1",
    }).encode("utf-8")
    url = f"{_SEC_API_QUERY_URL}?token={urllib.parse.quote(api_key)}"
    req = urllib.request.Request(url=url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S, context=_SEC_API_SSL_CTX) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                import gzip  # noqa: PLC0415
                raw = gzip.decompress(raw)
            doc = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001
            pass
        return None, f"sec-api Query HTTP {exc.code}: {text or exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"sec-api Query unreachable: {exc.reason}"
    filings = doc.get("filings") or []
    if not filings:
        return None, f"no filing matched accessionNo={accession!r}"
    filing_url = filings[0].get("linkToFilingDetails") or filings[0].get("linkToHtml")
    if not filing_url:
        return None, "filing record missing linkToFilingDetails / linkToHtml"
    _FILING_URL_CACHE[accession] = filing_url
    return filing_url, None


def _fetch_filing_section_html(
    filing_url: str,
    item: str,
    api_key: str,
) -> tuple[str | None, str | None]:
    """Pull a filing item's raw HTML via sec-api Extractor. Cached."""
    cache_key = (filing_url, item)
    cached = _FILING_HTML_CACHE.get(cache_key)
    if cached is not None:
        return cached, None
    params = urllib.parse.urlencode({
        "url": filing_url,
        "item": item,
        "type": "html",
        "token": api_key,
    })
    url = f"{_SEC_API_EXTRACTOR_URL}?{params}"
    req = urllib.request.Request(url=url, method="GET")
    # sec-api returns gzip-compressed responses unconditionally for the
    # Extractor endpoint (even without Accept-Encoding negotiation). The
    # urllib stack does not auto-decompress, so we explicitly opt in and
    # gunzip the body. Falling back to identity decoding when the server
    # does respect Accept-Encoding: identity keeps us safe either way.
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S * 2, context=_SEC_API_SSL_CTX) as resp:
            body = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip" or body[:2] == b"\x1f\x8b":
                import gzip  # noqa: PLC0415
                body = gzip.decompress(body)
            raw = body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001
            pass
        return None, f"sec-api Extractor HTTP {exc.code}: {text or exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"sec-api Extractor unreachable: {exc.reason}"
    if len(raw) > _FILING_HTML_MAX_CHARS:
        return None, (
            f"sec-api Extractor response too large ({len(raw)} chars > "
            f"cap {_FILING_HTML_MAX_CHARS})"
        )
    _FILING_HTML_CACHE[cache_key] = raw
    return raw, None


# sec-api's Extractor endpoint returns HTTP 404 with this exact body
# for filing types it doesn't process (DEF 14A, 8-A12B, S-1A, ...).
# Detecting the string lets us route around it to a direct EDGAR
# fetch — the filing HTML itself is publicly accessible, sec-api just
# doesn't have a parsed item index for non-periodic filings.
_SEC_API_UNSUPPORTED_MARKER = "filing type not supported"

# Per SEC EDGAR fair-access guidance, every request must carry a
# descriptive User-Agent (company name + contact email). Using a real
# corporate identity is a requirement, not a recommendation -- requests
# with a default UA get throttled or refused.
_EDGAR_USER_AGENT = "Coral Bricks AI research@coralbricks.ai"


def _fetch_filing_html_from_edgar(filing_url: str) -> tuple[str | None, str | None]:
    """Pull a filing's complete HTML directly from EDGAR. Cached.

    Used as the fallback for filings that sec-api's Extractor doesn't
    process (DEF 14A, registration statements, etc.) but that are
    publicly hosted at ``www.sec.gov/Archives/edgar/...``. Returns the
    whole document body -- there are no item boundaries for non-
    periodic filings to slice on, so the caller scans every ``<table>``
    rather than a single item region.
    """
    cache_key = (filing_url, "__edgar_full__")
    cached = _FILING_HTML_CACHE.get(cache_key)
    if cached is not None:
        return cached, None
    req = urllib.request.Request(url=filing_url, method="GET")
    req.add_header("User-Agent", _EDGAR_USER_AGENT)
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S * 2, context=_SEC_API_SSL_CTX) as resp:
            body = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip" or body[:2] == b"\x1f\x8b":
                import gzip  # noqa: PLC0415
                body = gzip.decompress(body)
            raw = body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001
            pass
        return None, f"EDGAR HTTP {exc.code}: {text or exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"EDGAR unreachable: {exc.reason}"
    if len(raw) > _FILING_HTML_MAX_CHARS:
        return None, (
            f"EDGAR filing too large ({len(raw)} chars > cap "
            f"{_FILING_HTML_MAX_CHARS})"
        )
    _FILING_HTML_CACHE[cache_key] = raw
    return raw, None


# Cap on the number of .htm files we'll pull from a single filing
# directory. Most 8-Ks have 2-4 (cover + 1-3 exhibits + the R*.htm
# financial reports); 12 leaves enough headroom for chunky filings
# (multi-exhibit shareholder letters) without paying for hundreds of
# tiny R*.htm financial reports in old 10-Ks.
_FILING_DIR_HTM_MAX = 12

# Lower bound on a "real" Extractor response. The sec-api Extractor
# returns ~600-1500 character cover-page stubs ("On <date>, <issuer>
# announced its results... furnished as Exhibit 99.1") when an 8-K
# item's substantive content lives in an exhibit it doesn't process.
# Empirically 4000 chars is comfortably above any real item-2.02 stub
# and well below any real item with embedded tables (which typically
# run 20K+).
_FILING_EXTRACTOR_STUB_MAX_CHARS = 4000


def _looks_like_extractor_exhibit_stub(html: str) -> bool:
    """Heuristic for "sec-api returned the cover stub, real content
    is in an exhibit". See :func:`_fetch_filing_exhibits_from_edgar`
    for the recovery path.
    """
    if not html:
        return False
    if len(html) > _FILING_EXTRACTOR_STUB_MAX_CHARS:
        return False
    lower = html.lower()
    return "exhibit" in lower and (
        "furnished as" in lower
        or "attached" in lower
        or "incorporated by reference" in lower
        or "see exhibit" in lower
    )


def _fetch_filing_exhibits_from_edgar(filing_url: str) -> tuple[str | None, str | None]:
    """Walk an EDGAR filing's directory and concatenate every .htm
    exhibit in it.

    Required for 8-Ks whose substantive content (earnings press
    release, shareholder letter, supplemental tables) lives in an
    Exhibit 99.X file. sec-api's Extractor only knows how to slice
    out items 1.01 / 2.02 / etc. inside the 8-K cover sheet itself,
    not the attached exhibits, so a query for a table inside the
    earnings exhibit comes back empty even though it's publicly
    hosted alongside the cover at ``/Archives/edgar/data/<CIK>/<acc>/``.

    Returns concatenated HTML with ``<!-- EDGAR EXHIBIT: <name> -->``
    sentinels between files so callers can attribute table matches
    to specific exhibits.
    """
    parsed = urllib.parse.urlparse(filing_url)
    dir_path = parsed.path.rsplit("/", 1)[0] + "/"
    dir_url = f"{parsed.scheme}://{parsed.netloc}{dir_path}"
    cover_filename = parsed.path.rsplit("/", 1)[-1]

    cache_key = (dir_url, "__edgar_exhibits__")
    cached = _FILING_HTML_CACHE.get(cache_key)
    if cached is not None:
        return cached, None

    req = urllib.request.Request(url=dir_url, method="GET")
    req.add_header("User-Agent", _EDGAR_USER_AGENT)
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S * 2, context=_SEC_API_SSL_CTX) as resp:
            body = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip" or body[:2] == b"\x1f\x8b":
                import gzip  # noqa: PLC0415
                body = gzip.decompress(body)
            listing = body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return None, f"EDGAR dir HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"EDGAR dir unreachable: {exc.reason}"

    # Pull every same-directory .htm/.html link. The EDGAR directory
    # page mixes the in-filing files with global nav links — filtering
    # to ``href`` values that start with ``dir_path`` drops the
    # navigation (``/index.htm``, ``/search/search.htm``, etc.) without
    # any allowlist maintenance.
    seen: set[str] = set()
    htm_urls: list[str] = []
    for m in re.finditer(r'href="([^"]+\.html?)"', listing, re.IGNORECASE):
        href = m.group(1)
        if not href.startswith(dir_path):
            continue
        filename = href.rsplit("/", 1)[-1]
        # Skip the cover sheet (caller already fetched / will fetch it)
        # and the per-section R*.htm "financial report" splits that
        # EDGAR emits alongside the full document — they duplicate
        # numbers without giving us the surrounding context the
        # keyword match wants.
        if filename == cover_filename:
            continue
        if filename.lower() in ("index.htm", "index.html"):
            continue
        if re.fullmatch(r"R\d+\.htm", filename):
            continue
        # EDGAR submission metadata pages (e.g.
        # 0001794669-24-000014-index.html or -index-headers.html)
        # carry zero business content and just describe the filing
        # itself — skip to avoid burning a fetch + 50 KB of nav HTML.
        if re.match(r"^\d{10}-\d{2}-\d{6}-index", filename):
            continue
        if filename in seen:
            continue
        seen.add(filename)
        htm_urls.append(f"{parsed.scheme}://{parsed.netloc}{href}")
        if len(htm_urls) >= _FILING_DIR_HTM_MAX:
            break

    if not htm_urls:
        return None, f"no .htm exhibits found in {dir_url}"

    parts: list[str] = []
    total_chars = 0
    for url in htm_urls:
        html, _err = _fetch_filing_html_from_edgar(url)
        if not html:
            continue
        name = url.rsplit("/", 1)[-1]
        # Sentinel comment so a caller (or human reader) can tell
        # which exhibit a matched table came from.
        chunk = f"<!-- EDGAR EXHIBIT: {name} -->\n{html}"
        if total_chars + len(chunk) > _FILING_HTML_MAX_CHARS:
            chunk = chunk[: max(0, _FILING_HTML_MAX_CHARS - total_chars)]
            parts.append(chunk)
            break
        parts.append(chunk)
        total_chars += len(chunk)

    if not parts:
        return None, f"could not fetch any of the {len(htm_urls)} .htm exhibits from {dir_url}"

    combined = "\n".join(parts)
    _FILING_HTML_CACHE[cache_key] = combined
    return combined, None


def _text_snippet_matches(
    html: str,
    needle: str,
    *,
    context_chars: int = 1200,
    max_snippets: int = 5,
) -> list[dict[str, Any]]:
    """Plain-text snippet extraction as a final fallback when no real
    ``<table>`` element matches the keyword but the keyword IS in the
    document body.

    Modern shareholder letters frequently
    render tabular data as CSS-styled ``<div>`` blocks rather than
    HTML tables; BeautifulSoup's ``find_all('table')`` then returns
    nothing useful even though the numbers are right there in the
    text. This function collapses the HTML to text and returns up to
    ``max_snippets`` windows of ±``context_chars`` around each
    needle occurrence, packaged in the same shape as a table match
    (``heading`` / ``rows_md`` / ``char_count``) so the caller's
    downstream rendering path is unchanged.
    """
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415
    except ImportError:
        return []
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    needle_l = needle.lower()
    text_l = text.lower()
    out: list[dict[str, Any]] = []
    seen_starts: set[int] = set()
    pos = 0
    while len(out) < max_snippets:
        idx = text_l.find(needle_l, pos)
        if idx < 0:
            break
        start = max(0, idx - context_chars // 2)
        end = min(len(text), idx + len(needle) + context_chars // 2)
        # Dedupe overlapping snippets — if two needle hits land
        # within `context_chars/2` of each other, the second's
        # window is a subset of the first's. Skip it.
        if any(abs(start - s) < context_chars // 2 for s in seen_starts):
            pos = idx + len(needle)
            continue
        seen_starts.add(start)
        snippet = text[start:end].strip()
        out.append({
            "heading": f"[text snippet, ±{context_chars // 2} chars around match]",
            "rows_md": snippet,
            "char_count": len(snippet),
        })
        pos = end
    return out


def _table_searchable_text(table: Any, max_preceding_headings: int = 3) -> str:
    """Build the matchable text for a ``<table>`` element.

    Combines (1) any ``<caption>`` text, (2) preceding heading text
    (walks backwards looking for ``<h1>``-``<h6>`` or strong/bold
    blocks, up to ``max_preceding_headings``), and (3) the **full
    text content** of the table itself. SEC tables are bounded
    (typically <10 KB of text); scanning all cell text catches metric
    rows that appear deep in long financial schedules (e.g. the
    global ARM row is row 15+ of Netflix's consolidated 10-K table,
    after revenue / opex / membership rows). Substring matching
    against ``table_keyword`` runs over this concatenation.
    """
    parts: list[str] = []
    cap = table.find("caption")
    if cap is not None:
        parts.append(cap.get_text(" ", strip=True))
    headings_seen = 0
    sib = table
    while headings_seen < max_preceding_headings:
        sib = sib.find_previous(["h1", "h2", "h3", "h4", "h5", "h6", "p", "b", "strong", "div"])
        if sib is None:
            break
        text = sib.get_text(" ", strip=True)
        if not text:
            continue
        # Bound preceding-context size; SEC filings have huge container divs.
        if len(text) > 400:
            text = text[:400]
        parts.append(text)
        headings_seen += 1
    parts.append(table.get_text(" ", strip=True))
    return " | ".join(p for p in parts if p)


def _table_to_markdown(table: Any) -> str:
    """Render a ``<table>`` as a Markdown table.

    SEC filings use ``colspan`` heavily for visual layout (empty
    spacer cells, dollar-sign columns next to value columns, multi-
    column period headers). Naively repeating each cell ``colspan``
    times produces 30-column tables full of empty cells. Strategy:

    1. Emit each non-empty cell ONCE (ignore colspan for content).
    2. Drop cells that are entirely empty after whitespace strip;
       SEC tables are mostly spacer cells and stripping them
       collapses the table to its actual semantic columns.
    3. Drop rows that end up with no cells.
    4. Treat the first surviving row as the header.

    Output is capped at :data:`_FILING_TABLE_ROWS_MAX` rows; if
    truncated, appends a ``... [N more rows truncated]`` marker.
    """
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row: list[str] = []
        for c in cells:
            txt = c.get_text(" ", strip=True)
            txt = re.sub(r"\s+", " ", txt).replace("|", "\\|")
            if txt:
                row.append(txt)
        if row:
            rows.append(row)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    truncation_note = ""
    if len(rows) > _FILING_TABLE_ROWS_MAX:
        truncation_note = f"\n... [{len(rows) - _FILING_TABLE_ROWS_MAX} more rows truncated]"
        rows = rows[:_FILING_TABLE_ROWS_MAX]
    header, *body = rows
    out = ["| " + " | ".join(header) + " |"]
    out.append("|" + "|".join("---" for _ in range(width)) + "|")
    for r in body:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out) + truncation_note


# ----------------------------------------------------------------------
# Filing deck-image OCR via Claude Sonnet vision
# ----------------------------------------------------------------------
#
# Investor-presentation decks attached as 8-K Exhibit 99.2 are
# typically PDF/PowerPoint exports rendered as ONE JPEG PER SLIDE
# alongside the HTML cover (``g<acc>ex99_2p<N>g1.jpg``). The HTML
# cover ships cover-page boilerplate + footnotes + a sparse table
# of contents, but the substantive content (revenue mix charts,
# pro-forma percentages, synergy waterfalls, competitive
# positioning bars) lives IN THE SLIDE IMAGES, not in
# extractable text. ``extract_filing_tables`` + ``get_full_text``
# both miss this content because the HTML is mostly a wrapper.
#
# This helper walks the filing dir, identifies same-directory
# slide-image graphics, fetches them, and runs Claude Sonnet
# vision over them in small batches to extract the textual
# content (numeric figures, chart legends, table cells).
#
# Generic across any 8-K / 425 / S-4 / DEFM14A that attaches a
# graphics-heavy investor deck. Out of scope: scanned PDF filings
# (cover-page level "image" filings); those need a different
# pipeline.
_FILING_DECK_IMAGE_CACHE: dict[tuple[str, str], bytes] = {}
_FILING_DECK_OCR_CACHE: dict[tuple[str, str], str] = {}
_SEC_API_FILING_URL_CACHE: dict[str, str] = {}


def _sec_api_filing_url_from_accession(
    accession: str,
) -> tuple[str | None, str | None]:
    """Look up an EDGAR primary-doc URL given an accession number.

    Falls back when the local sec_filings_chunked index hasn't
    ingested the filing yet (recent 8-Ks routinely lag by hours).
    Returns ``(url, err)``; ``url`` is the absolute
    ``https://www.sec.gov/Archives/edgar/data/<CIK>/<acc-no-dashes>/<doc>.htm``
    which sits in the same dir as any deck-image graphics the
    filing attaches.
    """
    if not accession:
        return None, "empty accession"
    cached = _SEC_API_FILING_URL_CACHE.get(accession)
    if cached is not None:
        return cached, None
    api_key = os.environ.get("SEC_API_KEY", "").strip()
    if not api_key:
        return None, "SEC_API_KEY missing"
    body = json.dumps({
        "query": f'accessionNo:"{accession}"',
        "from": "0", "size": "1",
    }).encode("utf-8")
    url = f"{_SEC_API_FILINGS_URL}?token={api_key}"
    req = urllib.request.Request(url=url, method="POST", data=body)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(
            req, timeout=_SEC_API_TIMEOUT_S, context=_SEC_API_SSL_CTX,
        ) as resp:
            raw = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
                import gzip  # noqa: PLC0415
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return None, f"sec-api HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"sec-api unreachable: {exc.reason}"
    try:
        body_json = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"sec-api response not JSON: {exc}"
    filings = body_json.get("filings") or []
    if not filings:
        return None, f"sec-api returned no filings for accession {accession!r}"
    primary_url = (
        filings[0].get("primaryDocumentUrl")
        or filings[0].get("linkToFilingDetails")
        or ""
    )
    if not primary_url.startswith("https://www.sec.gov/"):
        return None, f"sec-api primary url not on sec.gov: {primary_url!r}"
    _SEC_API_FILING_URL_CACHE[accession] = primary_url
    return primary_url, None

# Hard cap on slide pages OCR'd per call -- decks routinely run
# 30-50 pages. The pro-rated cost (~$0.01/image with Sonnet vision)
# adds up; the cap also stops a runaway request from blowing the
# wall budget. Skills typically need the front-half (overview /
# transaction terms) anyway.
_FILING_DECK_OCR_MAX_PAGES = 25
# Sonnet vision accepts up to ~20 images per message but tokens
# scale linearly with image count and prompt re-cost dominates at
# small batches. Five-image batches keep each call <8K input
# tokens.
_FILING_DECK_OCR_BATCH_SIZE = 5


def _fetch_deck_image_urls_from_edgar(
    filing_url: str,
) -> tuple[list[str] | None, str | None]:
    """Walk an EDGAR filing's directory and return same-dir image URLs.

    Returns ``(image_urls, err)``. ``image_urls`` is ordered by the
    page-number suffix the EDGAR filer typically embeds in the
    graphic name (``g<acc>ex99_2p<N>g1.jpg`` -- ``N`` is the slide
    page index). Filters to ``.jpg`` / ``.jpeg`` / ``.png`` /
    ``.gif`` extensions only; PDF / SVG ignored (SVG often carries
    only logos / icons and would confuse the OCR).
    """
    parsed = urllib.parse.urlparse(filing_url)
    dir_path = parsed.path.rsplit("/", 1)[0] + "/"
    dir_url = f"{parsed.scheme}://{parsed.netloc}{dir_path}"
    cache_key = (dir_url, "__edgar_deck_images__")
    cached_bytes = _FILING_HTML_CACHE.get(cache_key)
    if cached_bytes is not None:
        # Parse cached listing back to URL list.
        listing = cached_bytes
    else:
        req = urllib.request.Request(url=dir_url, method="GET")
        req.add_header("User-Agent", _EDGAR_USER_AGENT)
        req.add_header("Accept-Encoding", "gzip")
        try:
            with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S * 2, context=_SEC_API_SSL_CTX) as resp:
                body = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip" or body[:2] == b"\x1f\x8b":
                    import gzip  # noqa: PLC0415
                    body = gzip.decompress(body)
                listing = body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return None, f"EDGAR dir HTTP {exc.code}: {exc.reason}"
        except urllib.error.URLError as exc:
            return None, f"EDGAR dir unreachable: {exc.reason}"
        _FILING_HTML_CACHE[cache_key] = listing
    image_urls: list[tuple[int, str]] = []
    for m in re.finditer(
        r'href="([^"]+\.(?:jpg|jpeg|png|gif))"', listing, re.IGNORECASE,
    ):
        href = m.group(1)
        if not href.startswith(dir_path):
            continue
        # Extract the page index from the filename if present
        # ("p<N>g<M>.jpg" → N) so the OCR pass walks slides in
        # presentation order; otherwise fall back to lexicographic
        # ordering which the EDGAR upload tooling produces.
        filename = href.rsplit("/", 1)[-1]
        page_match = re.search(r"p(\d+)g\d+\.", filename, re.IGNORECASE)
        page_idx = int(page_match.group(1)) if page_match else 999999
        image_urls.append(
            (page_idx, f"{parsed.scheme}://{parsed.netloc}{href}"),
        )
    image_urls.sort()
    return [u for _, u in image_urls], None


def _fetch_image_bytes_from_edgar(
    image_url: str,
) -> tuple[bytes | None, str | None]:
    """Fetch a single EDGAR-hosted image. Per-URL cache."""
    cache_key = (image_url, "__image__")
    cached = _FILING_DECK_IMAGE_CACHE.get(cache_key)
    if cached is not None:
        return cached, None
    req = urllib.request.Request(url=image_url, method="GET")
    req.add_header("User-Agent", _EDGAR_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S * 2, context=_SEC_API_SSL_CTX) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return None, f"EDGAR image HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"EDGAR image unreachable: {exc.reason}"
    _FILING_DECK_IMAGE_CACHE[cache_key] = body
    return body, None


def _extract_deck_text_via_vision(
    image_urls: list[str],
    *,
    focus_query: Optional[str] = None,
    max_pages: int = _FILING_DECK_OCR_MAX_PAGES,
    batch_size: int = _FILING_DECK_OCR_BATCH_SIZE,
) -> tuple[str, list[str]]:
    """Run Claude Sonnet vision over slide images, return concatenated text.

    Returns ``(combined_text, errors)``. Each batch is sent as a
    single message with N image blocks + one text instruction.
    The instruction asks the model to dump every numeric figure,
    table cell, percentage, and chart legend it sees, preserving
    the slide layout. ``focus_query`` is appended to the
    instruction so the model prioritises content relevant to the
    caller's question.
    """
    from reef import llm as cb_llm  # noqa: PLC0415
    import base64  # noqa: PLC0415

    # Vision-model selection. Qwen2.5-VL-72B via DeepInfra is the
    # default: ~$0.02/deck (3-5x cheaper than Haiku at ~$0.06, 17x
    # vs Sonnet at ~$0.35). OCR is a text-dump + numeric-extraction
    # workload, no reasoning required, so the open-source VL model
    # is sufficient. Haiku stays available as the Anthropic-direct
    # fallback when DeepInfra is overloaded.
    vision_model = "Qwen/Qwen3-VL-30B-A3B-Instruct"
    is_anthropic_vision = vision_model.lower().startswith("claude-")

    pages = image_urls[:max_pages]
    if not pages:
        return "", ["no slide images found in filing directory"]
    chunks: list[str] = []
    errors: list[str] = []
    for batch_start in range(0, len(pages), batch_size):
        batch = pages[batch_start:batch_start + batch_size]
        content: list[dict[str, Any]] = []
        valid_count = 0
        for url in batch:
            cache_key = (url, "__ocr__")
            cached_text = _FILING_DECK_OCR_CACHE.get(cache_key)
            if cached_text is not None:
                chunks.append(cached_text)
                continue
            img_bytes, err = _fetch_image_bytes_from_edgar(url)
            if err is not None or not img_bytes:
                errors.append(f"{url.rsplit('/', 1)[-1]}: {err or 'empty body'}")
                continue
            img_b64 = base64.b64encode(img_bytes).decode("ascii")
            # Content-block format branches by provider. Anthropic
            # native expects {type: image, source: {type: base64,
            # media_type, data}}; OpenAI-shape (Qwen/Llama/etc. via
            # DeepInfra's OpenAI-compatible API) expects {type:
            # image_url, image_url: {url: "data:image/jpeg;base64,..."}}.
            if is_anthropic_vision:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": img_b64,
                    },
                })
            else:
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{img_b64}",
                    },
                })
            valid_count += 1
        if valid_count == 0:
            continue
        prompt = (
            "These are sequential slides from an investor presentation "
            "filed as an 8-K exhibit. For each slide, transcribe ALL "
            "text content verbatim: title, headers, bullet points, "
            "table cells, axis labels, chart legends, footnotes, "
            "and EVERY numeric figure with its label / unit / "
            "denomination (% / $B / $M / x). Preserve slide order; "
            "begin each slide with 'Slide <n>:'. Do NOT summarise or "
            "interpret -- the goal is a complete text dump of the "
            "deck's visual content."
        )
        if focus_query:
            prompt += (
                "\n\nFOCUS: pay particular attention to any figures / "
                f"labels / chart segments related to: {focus_query}"
            )
        content.append({"type": "text", "text": prompt})
        try:
            resp = cb_llm.chat(
                model=vision_model,
                messages=[{"role": "user", "content": content}],
                max_tokens=4096,
                timeout_s=120.0,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"batch {batch_start}: vision call failed: {exc}")
            continue
        choices = ((resp or {}).get("response") or {}).get("choices") or []
        text = ""
        if choices:
            msg = choices[0].get("message") or {}
            text = str(msg.get("content") or "").strip()
        if text:
            # Index the per-page cache from the response. The model's
            # "Slide N:" header lets us bucket the response back to
            # individual page URLs for caching, but the parsing is
            # brittle -- just cache the whole batch under the first
            # image's URL so repeat calls on the same deck skip the
            # round.
            batch_first = batch[0]
            _FILING_DECK_OCR_CACHE[(batch_first, "__ocr__")] = text
            chunks.append(text)
    combined = "\n\n--- batch boundary ---\n\n".join(chunks)
    return combined, errors


def _do_extract_filing_deck_text(
    ref: str,
    *,
    focus_query: Optional[str] = None,
    max_pages: int = _FILING_DECK_OCR_MAX_PAGES,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """OCR a filing's attached slide-image deck via vision LLM.

    ``ref`` is a bm25_sec / get hit id (``sec:<accession>[:<item>]``).
    Resolves the accession's primary-doc URL via the index, then
    walks the same EDGAR directory for graphic-extension files.
    Sonnet vision processes them in batches, returning a single
    concatenated text dump the caller can keyword-search or pass
    back to the LLM for narrative answers.

    Use for 8-K Ex 99.2 investor decks, S-4 fairness-opinion exhibits,
    DEFM14A merger-deck attachments. NOT for scanned-PDF cover-page
    filings -- those don't follow the dir-of-graphics pattern.
    """
    if not ref or not ref.startswith("sec:"):
        return {"error": "ref must be a 'sec:<accession>' identifier"}
    # The sec_filings_chunked index keys by full chunked ref
    # (sec:<acc>:chunk_N or sec:<acc>:<item>). The model may pass:
    #   - bare accession (sec:0001193125-25-167118) -> not indexed
    #   - item-suffixed (sec:0001193125-25-167118:7.01) -> may or may not be
    #   - chunk-suffixed (sec:0001193125-25-167118:chunk_3) -> indexed
    # Try the ref as-is first; on miss, try a series of chunked
    # variants until one resolves. Every chunk of an accession
    # shares the same `src.url` (the primary doc), so any indexed
    # chunk gives us the dir URL.
    candidates = [ref]
    accession_only = _strip_chunk_suffix(ref)
    if accession_only != ref:
        candidates.append(accession_only)
    # Strip an item-style suffix (":7.01", ":2.02") if present.
    bare_acc = re.sub(r":[^:]+$", "", accession_only) if ":" in accession_only[4:] else accession_only
    if bare_acc != accession_only:
        candidates.append(bare_acc)
    for n in range(10):
        candidates.append(f"{bare_acc}:chunk_{n}")
    chunk_env = None
    resolved_ref = None
    for cand in candidates:
        env = cb_tools.get(index=SEC_FILINGS_INDEX, id=cand)
        if env.get("found"):
            chunk_env = env
            resolved_ref = cand
            break
    # If no chunked variant resolves in the local index, fall back
    # to sec-api by accession number — recent 8-Ks routinely lag the
    # ingestion pipeline, but sec-api returns the cover-page URL
    # which sits in the same EDGAR directory as the deck images.
    edgar_url = ""
    if chunk_env is None or not chunk_env.get("found"):
        acc_no = bare_acc[4:] if bare_acc.startswith("sec:") else bare_acc
        edgar_url_via_api, sec_api_err = _sec_api_filing_url_from_accession(acc_no)
        if not edgar_url_via_api:
            return {
                "error": (
                    f"could not resolve ref {ref!r} in {SEC_FILINGS_INDEX}; "
                    f"sec-api by accession failed: {sec_api_err}"
                ),
                "tried": candidates[:5],
            }
        edgar_url = edgar_url_via_api
        resolved_ref = bare_acc
    else:
        src = (chunk_env.get("doc") or {}).get("source") or {}
        edgar_url = str(src.get("url") or "")
    if not edgar_url.startswith("https://www.sec.gov/"):
        return {"error": f"ref has no EDGAR URL: {edgar_url!r}"}
    image_urls, err = _fetch_deck_image_urls_from_edgar(edgar_url)
    if err is not None:
        return {"error": err, "ref": resolved_ref}
    if not image_urls:
        result = {
            "ref": resolved_ref,
            "edgar_url": edgar_url,
            "image_count": 0,
            "deck_text": "",
            "errors": ["no slide images found in filing directory"],
        }
        return _apply_binding(bind_as, result)
    text, errors = _extract_deck_text_via_vision(
        image_urls, focus_query=focus_query, max_pages=max_pages,
    )
    result = {
        "ref": resolved_ref,
        "edgar_url": edgar_url,
        "image_count": len(image_urls),
        "pages_processed": min(len(image_urls), max_pages),
        "deck_text": text,
        "errors": errors,
    }
    return _apply_binding(bind_as, result)


def _do_extract_filing_tables(
    ref: str,
    *,
    table_keyword: str,
    item: str = "7",
    limit: int = 5,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Extract ``<table>`` elements from an SEC filing matching a keyword.

    Use this for **non-GAAP / operating-KPI tables** that live in MD&A
    HTML (Netflix ARM, Spotify MAU, segment-revenue breakouts that
    aren't iXBRL-tagged). Two upstream calls -- one to resolve the
    filing URL from the accession, one to pull the requested item's
    raw HTML -- both via sec-api with the existing SEC_API_KEY.
    Tables are matched against ``table_keyword`` (case-insensitive
    substring on caption + preceding headings + first-column /
    first-row cell text) and returned as Markdown rows.

    Defaults to ``item="7"`` (10-K MD&A). For 10-Q use ``"part1item2"``;
    for 8-K Item 2.02 use ``"2-2"``. See sec-api Extractor docs for
    other item identifiers.
    """
    api_key = os.environ.get("SEC_API_KEY", "").strip()
    if not api_key:
        return {
            "ref": ref,
            "error": (
                "SEC_API_KEY not present in sandbox env. The gateway "
                "credential injector should ship it when the alphacumen "
                "manifest declares 'api.sec-api.io' in egress."
            ),
            "tables": [],
        }
    accession = _xbrl_extract_accession(ref)
    if not accession:
        return {"ref": ref, "error": f"could not parse accession from ref={ref!r}", "tables": []}
    if not table_keyword or not table_keyword.strip():
        return {"ref": ref, "error": "table_keyword is required (e.g. 'average revenue per')", "tables": []}

    filing_url, err = _filing_url_for_accession(accession, api_key)
    if err:
        return {"ref": ref, "accession": accession, "error": err, "tables": []}

    html, err = _fetch_filing_section_html(filing_url, item, api_key)
    edgar_fallback_used = False
    if err and _SEC_API_UNSUPPORTED_MARKER in err.lower():
        # sec-api Extractor doesn't process this filing type (most
        # commonly DEF 14A proxy statements -- which contain the
        # Director Compensation tables Vals AI grades on for row 12).
        # Bypass to a direct EDGAR fetch; the HTML is public and has
        # the same tables, just without sec-api's item-level slicing.
        html, edgar_err = _fetch_filing_html_from_edgar(filing_url)
        if edgar_err:
            return {
                "ref": ref, "accession": accession, "filing_url": filing_url,
                "item": item,
                "error": f"{err} | EDGAR fallback also failed: {edgar_err}",
                "tables": [],
            }
        edgar_fallback_used = True
        err = None
    if err:
        return {
            "ref": ref, "accession": accession, "filing_url": filing_url,
            "item": item, "error": err, "tables": [],
        }

    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415 -- lazy so import error is caught here
    except ImportError as exc:
        return {
            "ref": ref, "accession": accession, "error": f"beautifulsoup4 not installed in sandbox venv: {exc}",
            "tables": [],
        }

    needle = table_keyword.strip().lower()

    def _scan(html_text: str) -> tuple[list[dict[str, Any]], int]:
        """Find every table in ``html_text`` whose searchable text
        contains ``needle``. Returns (matches, total_tables_seen)."""
        soup = BeautifulSoup(html_text, "html.parser")
        out: list[dict[str, Any]] = []
        seen_tables = 0
        for table in soup.find_all("table"):
            seen_tables += 1
            searchable = _table_searchable_text(table).lower()
            if needle not in searchable:
                continue
            heading = ""
            sib = table
            for _ in range(8):
                sib = sib.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
                if sib is None:
                    break
                t = sib.get_text(" ", strip=True)
                if t:
                    heading = t[:200]
                    break
            rows_md = _table_to_markdown(table)
            if not rows_md:
                continue
            out.append({
                "heading": heading,
                "rows_md": rows_md,
                "char_count": len(rows_md),
            })
        return out, seen_tables

    matches, total_tables = _scan(html)

    # Exhibit-walker fallback. Triggers when sec-api's Extractor
    # returned a "see Exhibit 99.X" cover stub (small, mentions
    # 'Exhibit' + 'furnished as'/'attached'/etc.) and the table scan
    # came back empty. 8-K earnings releases are the canonical case:
    # the substantive press release + quarterly guidance tables live
    # in ex99*.htm alongside the cover. Walks the filing directory,
    # concatenates every .htm exhibit, and re-scans. Skipped if the
    # initial fetch already came from EDGAR-direct (DEF 14A path) —
    # that already returned the whole filing.
    exhibits_fallback_used = False
    exhibits_err: str | None = None
    snippet_fallback_used = False
    if (
        not edgar_fallback_used
        and not matches
        and _looks_like_extractor_exhibit_stub(html)
    ):
        exhibits_html, exhibits_err = _fetch_filing_exhibits_from_edgar(filing_url)
        if exhibits_html:
            ex_matches, ex_total = _scan(exhibits_html)
            if ex_matches or ex_total > total_tables:
                matches = ex_matches
                total_tables = ex_total
                exhibits_fallback_used = True
            # If the exhibits' HTML contains no <table> elements that
            # match the keyword (modern shareholder letters often use
            # CSS-styled <div> blocks for tabular data — a payments-
            # processing issuer's quarterly guidance breakdown is one
            # such case), fall back
            # to plain-text snippet extraction so the caller still gets
            # the surrounding numbers.
            if not matches:
                snippets = _text_snippet_matches(exhibits_html, needle)
                if snippets:
                    matches = snippets
                    snippet_fallback_used = True
                    exhibits_fallback_used = True

    # Rank matches by size (char_count proxies cell count × cell
    # density), then truncate to `limit`. The real data table for a
    # keyword like "Director Compensation" -- many directors × many
    # comp columns -- typically renders to 800-3000 chars. Tables of
    # Contents that happen to contain the same words as a section
    # header render to 50-300 chars (one row per TOC entry). Without
    # this re-rank we'd return TOC rows first because they appear
    # earlier in document order and we'd break at `limit`. Walking
    # all matching tables before sorting is cheap (BS4 is fast; a
    # 72-table DEF 14A renders in well under a second).
    _limit = max(1, min(int(limit), _FILING_TABLES_MAX))
    matches.sort(key=lambda _m: -int(_m.get("char_count", 0)))
    matches = matches[:_limit]

    result = {
        "ref": ref,
        "accession": accession,
        "filing_url": filing_url,
        "item": item,
        "table_keyword": table_keyword,
        "tables_in_item": total_tables,
        "matched_count": len(matches),
        "tables": matches,
    }
    if edgar_fallback_used:
        result["source"] = "edgar_direct"
    elif snippet_fallback_used:
        result["source"] = "edgar_exhibits_text_snippets"
    elif exhibits_fallback_used:
        result["source"] = "edgar_exhibits"
    if snippet_fallback_used:
        # Text-snippet matches don't render as proper tables. Tell the
        # caller so it parses the snippets as prose, not as Markdown
        # rows.
        result["hint"] = (
            "Matches are text snippets from the exhibit body, not "
            "<table> elements (the exhibit uses CSS-styled divs for "
            "tabular layout). Parse the numbers out of the snippet "
            "prose directly."
        )
    elif total_tables > 0 and not matches:
        scope = (
            "exhibits"
            if exhibits_fallback_used
            else ("filing" if edgar_fallback_used else f"item {item!r}")
        )
        result["hint"] = (
            f"Found {total_tables} tables in {scope} but none matched "
            f"keyword {table_keyword!r}. Try a shorter / more distinctive "
            "substring (e.g. one or two words from the table caption or "
            "first-column metric name)."
        )
    elif total_tables == 0 and exhibits_err:
        result["hint"] = (
            f"sec-api Extractor returned an exhibit-stub for item {item!r} "
            f"and the EDGAR exhibits-walker fallback also failed: {exhibits_err}"
        )
    return _apply_binding(bind_as, result)


EXTRACT_FILING_TABLES = Tool(
    name="extract_filing_tables",
    description=(
        "Extract HTML tables from an SEC filing's MD&A (or other "
        "Item) matching a caption / heading / first-column keyword. "
        "Returns each matched table as a Markdown rows block plus its "
        "preceding heading. Use this when the value you need is in a "
        "**non-GAAP / operating-KPI table** that the issuer publishes "
        "in MD&A but does NOT tag in iXBRL -- e.g. Netflix's "
        "'Average monthly revenue per paying membership' table, "
        "Spotify's MAU breakouts, segment-KPI tables that aren't in "
        "us-gaap or the company's XBRL extension. "
        "PREFER `get_xbrl_facts` for GAAP line items (revenue, EPS, "
        "balance-sheet items); use this tool ONLY when XBRL returned "
        "no match for the metric AND the answer needs the issuer's "
        "published figure verbatim (not a re-derivation from raw "
        "subscribers / revenue). Defaults to item='7' (10-K MD&A); "
        "for 10-Q pass 'part1item2'; for 8-K Item 2.02 pass '2-2'. "
        "**Also supports DEF 14A proxy statements** (director "
        "compensation, executive compensation tables) -- pass the "
        "DEF 14A accession in `ref` and any value for `item` (the "
        "tool auto-falls-back to a full-filing EDGAR scan for DEF "
        "14A since sec-api's Extractor has no item-level index for "
        "proxy statements)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "Hit id from bm25_sec, shape "
                    "'sec:<accession>[:<item>]'. Only the accession is "
                    "used; any item suffix in the ref is ignored in "
                    "favor of the explicit `item` parameter below."
                ),
            },
            "table_keyword": {
                "type": "string",
                "description": (
                    "Case-insensitive substring matched against each "
                    "table's caption + preceding headings + first-row "
                    "and first-column cell text. Pick distinctive "
                    "words from the table's row label / caption: "
                    "'average revenue per' (Netflix ARM), 'paid "
                    "memberships' (subscriber tables), 'monthly active "
                    "users' (MAU). One or two words is usually enough."
                ),
            },
            "item": {
                "type": "string",
                "default": "7",
                "description": (
                    "sec-api Extractor item identifier. Defaults to "
                    "'7' (10-K MD&A). Common values: '7' (10-K MD&A), "
                    "'1A' (10-K Risk Factors), '8' (10-K Financial "
                    "Statements), 'part1item2' (10-Q MD&A), '2-2' "
                    "(8-K Item 2.02 Results of Operations)."
                ),
            },
            "limit": {
                "type": "integer",
                "default": 5,
                "minimum": 1,
                "maximum": 5,
                "description": (
                    "Max tables to return after the keyword filter "
                    "(hard cap = 5). Tighten `table_keyword` if you "
                    "need to disambiguate among many similar tables."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ref", "table_keyword"],
    },
    fn=_do_extract_filing_tables,
)


EXTRACT_FILING_DECK_TEXT = Tool(
    name="extract_filing_deck_text",
    description=(
        "OCR an SEC filing's attached slide-image deck via Claude "
        "vision and return the concatenated text content. USE when "
        "the question references content disclosed in an investor "
        "presentation / merger-announcement deck / supplemental "
        "fairness-opinion exhibit that's attached as a graphics-heavy "
        "8-K Exhibit 99.2 (or S-4 / DEFM14A appendix). These decks "
        "render each slide as a JPEG alongside an HTML cover -- "
        "`extract_filing_tables` + `get_full_text` see only the HTML "
        "wrapper (cover + footnotes + table-of-contents) and miss the "
        "substantive content (revenue-mix charts, pro-forma "
        "percentages, synergy waterfalls, competitive-positioning "
        "bars) which lives IN the slide images. Pass `focus_query` "
        "with the keywords / percentages the question targets so the "
        "model prioritises those slides. NOT for non-deck filings "
        "(10-K / 10-Q / 8-K / press releases) -- use `get_full_text` "
        "or `extract_filing_tables` for those."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "Hit id from bm25_sec, shape "
                    "'sec:<accession>[:<item>]'. The chunk suffix is "
                    "stripped; image discovery walks the accession's "
                    "EDGAR directory for graphic-extension files."
                ),
            },
            "focus_query": {
                "type": "string",
                "description": (
                    "Optional natural-language hint about what the "
                    "caller needs from the deck (e.g. 'pro forma "
                    "non-O&G revenue mix percentage', 'synergy "
                    "waterfall by year', 'segment EBITDA bars'). "
                    "Appended to the vision prompt so the model "
                    "prioritises matching slides + figures in its "
                    "transcription."
                ),
            },
            "max_pages": {
                "type": "integer",
                "default": _FILING_DECK_OCR_MAX_PAGES,
                "minimum": 1,
                "maximum": _FILING_DECK_OCR_MAX_PAGES,
                "description": (
                    "Cap on slide pages to OCR. Decks typically run "
                    "30-50 pages but front-half (overview + transaction "
                    "terms) usually covers the rubric-relevant content. "
                    "Default 25 balances coverage vs latency / cost."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ref"],
    },
    fn=_do_extract_filing_deck_text,
)


# --------------------------------------------------------------------------
# find_sec_filing_edgar -- doc-not-in-index fallback via direct EDGAR fetch
# --------------------------------------------------------------------------
#
# Why this exists. bm25_sec runs against ``sec_filings_chunked`` (an
# OpenSearch ingestion of SEC EDGAR). Two failure modes leave a
# substantively-correct question unanswerable from the index alone:
#
# 1. Ingestion gap on a specific accession. The Item 5.07 vote-result
#    8-K Foot Locker filed 2022-05-20 is filed by proxy agent CIK
#    0001539497 (not FL's own CIK 0000850209), and is missing from
#    the index, while the same-day earnings 8-K filed by FL itself is
#    indexed normally. The pattern repeats at other issuers --
#    proxy-agent-filed 5.07s are systematically under-indexed.
# 2. Recency-floor wall (see :func:`_do_bm25_sec` -- ``filed_at_gte``
#    auto-injection at today-730d). Without an explicit date filter
#    older filings are invisible.
#
# This tool reaches EDGAR directly: ticker -> CIK via the public
# company_tickers.json, then EDGAR's browse-edgar atom feed for
# filings list, then per-filing directory walk -> primary document
# HTML -> BS4 text. www.sec.gov is already on the manifest's egress
# allowlist (added for the DEF 14A path :func:`_fetch_filing_html_from_edgar`
# took earlier), so no platform change is needed to roll this out.

import xml.etree.ElementTree as _ET  # noqa: PLC0415, E402 -- only this tool uses ET

_CIK_CACHE: dict[str, str] = {}
_SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_EDGAR_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
_EDGAR_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def _resolve_cik_from_ticker(ticker: str) -> tuple[str | None, str | None]:
    """Resolve uppercase ticker -> 10-digit CIK.

    Two-step lookup so delisted tickers (e.g. ``FL`` after the Sept 2025
    Foot Locker / Dick's merger) still resolve:

    1. ``company_tickers.json`` for active issuers (one fetch warms the
       cache for the whole run).
    2. Fallback: ``browse-edgar?CIK=<ticker>`` — EDGAR accepts a ticker
       in the ``CIK`` parameter and returns the canonical 10-digit CIK
       under ``<company-info><cik>...`` even for delisted issuers, as
       long as the ticker ever resolved to a CIK historically.

    If a bare numeric string is passed we accept it as-is (treat as a
    pre-resolved CIK; zero-pad to 10 chars).
    """
    tkr = ticker.strip().upper()
    if not tkr:
        return None, "ticker is empty"
    if tkr.isdigit():
        return tkr.zfill(10), None
    if tkr in _CIK_CACHE:
        return _CIK_CACHE[tkr], None

    # Step 1: active-issuer tickers map.
    req = urllib.request.Request(_SEC_COMPANY_TICKERS_URL, method="GET")
    req.add_header("User-Agent", _EDGAR_USER_AGENT)
    json_err: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S, context=_SEC_API_SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for entry in data.values():
            t = str(entry.get("ticker") or "").upper()
            cik_int = entry.get("cik_str")
            if t and isinstance(cik_int, int):
                _CIK_CACHE[t] = f"{cik_int:010d}"
        if tkr in _CIK_CACHE:
            return _CIK_CACHE[tkr], None
    except urllib.error.HTTPError as exc:
        json_err = f"SEC tickers HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        json_err = f"SEC tickers unreachable: {exc.reason}"
    except json.JSONDecodeError as exc:
        json_err = f"SEC tickers JSON parse error: {exc}"

    # Step 2: EDGAR atom fallback for delisted / off-the-list tickers.
    url = (
        f"{_EDGAR_BROWSE_URL}?action=getcompany&CIK={urllib.parse.quote(tkr)}"
        "&type=&dateb=&owner=include&count=1&output=atom"
    )
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", _EDGAR_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S, context=_SEC_API_SSL_CTX) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return None, f"EDGAR ticker-fallback HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"EDGAR ticker-fallback unreachable: {exc.reason}"
    # EDGAR responds with the issuer's ``<company-info>`` block containing
    # ``<cik>0000850209</cik>``. A plain regex avoids dragging an XML
    # parser into the resolution path — the response is small and the
    # element shape is stable.
    m = re.search(r"<cik>\s*(\d+)\s*</cik>", body)
    if m:
        cik = m.group(1).zfill(10)
        _CIK_CACHE[tkr] = cik
        return cik, None
    return None, (
        f"ticker {tkr!r} did not resolve via company_tickers.json "
        f"({json_err or 'not in active list'}) and EDGAR fallback "
        "returned no <cik> element"
    )


def _yyyymmdd(d: str | None) -> str | None:
    if not d:
        return None
    s = d.strip().replace("-", "")
    if len(s) == 8 and s.isdigit():
        return s
    return None


def _browse_edgar_for_filings(
    *,
    cik: str,
    form_type: str | None,
    filed_at_gte: str | None,
    filed_at_lte: str | None,
    count: int,
) -> tuple[list[dict[str, str]], str | None]:
    """Hit EDGAR's atom feed and parse the per-entry filing metadata.

    Returns a list of ``{"accession", "filed_at", "form_type",
    "items_desc", "filing_href"}`` entries (one per ``<entry>``).
    """
    params: dict[str, str] = {
        "action": "getcompany",
        "CIK": cik,
        "owner": "include",
        "count": str(count),
        "output": "atom",
    }
    if form_type:
        params["type"] = form_type
    if (gte := _yyyymmdd(filed_at_gte)):
        params["datea"] = gte
    if (lte := _yyyymmdd(filed_at_lte)):
        params["dateb"] = lte
    url = f"{_EDGAR_BROWSE_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", _EDGAR_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S, context=_SEC_API_SSL_CTX) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return [], f"EDGAR browse HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return [], f"EDGAR browse unreachable: {exc.reason}"
    try:
        root = _ET.fromstring(body)
    except _ET.ParseError as exc:
        return [], f"EDGAR atom parse error: {exc}"
    hits: list[dict[str, str]] = []
    for entry in root.findall("a:entry", _EDGAR_ATOM_NS):
        content = entry.find("a:content", _EDGAR_ATOM_NS)
        if content is None:
            continue
        # SEC nests the per-filing fields inside <content> but they
        # inherit the feed's default atom namespace, so they appear
        # under ``{...}accession-number`` rather than the bare tag.
        # The local closure pins ``content`` so the helper stays
        # readable inline.
        def _txt(tag: str, *, _c=content) -> str:
            node = _c.find(f"a:{tag}", _EDGAR_ATOM_NS)
            return (node.text or "").strip() if node is not None else ""
        hits.append({
            "accession": _txt("accession-number"),
            "filed_at": _txt("filing-date"),
            "form_type": _txt("filing-type"),
            "items_desc": _txt("items-desc"),
            "filing_href": _txt("filing-href"),
        })
    return hits, None


def _list_filing_directory(filing_href: str) -> tuple[list[dict[str, str]], str | None]:
    """List files in a filing's EDGAR directory via ``index.json``.

    ``filing_href`` is the atom feed's ``-index.htm`` URL; we drop the
    trailing index page to get the directory, then fetch ``index.json``
    for the structured listing.
    """
    base = filing_href.rsplit("/", 1)[0]
    url = f"{base}/index.json"
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", _EDGAR_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S, context=_SEC_API_SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        return [], f"EDGAR directory unreachable: {exc}"
    except json.JSONDecodeError as exc:
        return [], f"EDGAR directory JSON parse error: {exc}"
    items = ((data.get("directory") or {}).get("item") or [])
    return [
        {
            "name": str(it.get("name") or ""),
            "type": str(it.get("type") or ""),
            "size": str(it.get("size") or ""),
        }
        for it in items
    ], None


def _fetch_primary_doc_text(
    filing_href: str,
    form_type: str,
) -> tuple[str, str | None]:
    """Pick the substantive document from a filing's directory and
    return BS4-stripped text. Picks the largest .htm whose ``type``
    matches ``form_type`` exactly; falls back to the largest non-
    exhibit .htm, then to any .htm.
    """
    items, err = _list_filing_directory(filing_href)
    if err:
        return "", err
    base = filing_href.rsplit("/", 1)[0]
    candidates = [
        it for it in items
        if it["name"].lower().endswith(".htm")
        and it["type"].upper() == form_type.upper()
    ]
    if not candidates:
        candidates = [
            it for it in items
            if it["name"].lower().endswith(".htm")
            and "ex" not in it["name"].lower()
        ]
    if not candidates:
        candidates = [it for it in items if it["name"].lower().endswith(".htm")]
    if not candidates:
        return "", "no .htm document in filing directory"

    def _size_int(it: dict[str, str]) -> int:
        try:
            return int(it.get("size") or "0")
        except ValueError:
            return 0

    primary = max(candidates, key=_size_int)
    doc_url = f"{base}/{primary['name']}"
    raw, err = _fetch_filing_html_from_edgar(doc_url)
    if err or not raw:
        return "", err or "empty EDGAR response"
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415
    except ImportError as exc:
        return "", f"BeautifulSoup not available: {exc}"
    return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True), None


@time_bounded(asof_arg="filed_at_lte", mode="clamp")
def _do_find_sec_filing_edgar(
    ticker: str,
    *,
    form_type: str = "8-K",
    item_section: Optional[str] = None,
    filed_at_gte: Optional[str] = None,
    filed_at_lte: Optional[str] = None,
    k: int = 5,
    max_chars_per_filing: int = 12_000,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Doc-not-in-index fallback for SEC filings. See
    :data:`FIND_SEC_FILING_EDGAR` for the model-facing contract.
    """
    cik, err = _resolve_cik_from_ticker(ticker)
    if err or not cik:
        return {"error": err or "no CIK resolved", "ticker": ticker, "hits": []}

    hits, err = _browse_edgar_for_filings(
        cik=cik, form_type=form_type,
        filed_at_gte=filed_at_gte, filed_at_lte=filed_at_lte,
        count=40,
    )
    if err:
        return {"error": err, "ticker": ticker, "cik": cik, "hits": []}

    # items_desc shape from EDGAR is "items 5.07, 8.01and9.01" -- substring
    # match on the bare item code handles both spaced and 'and'-joined forms.
    if item_section:
        needle = item_section.strip()
        hits = [h for h in hits if needle in (h.get("items_desc") or "")]

    hits = hits[: max(1, min(int(k), 10))]

    enriched: list[dict[str, Any]] = []
    for h in hits:
        text, ferr = "", None
        if h.get("filing_href"):
            text, ferr = _fetch_primary_doc_text(
                h["filing_href"], h.get("form_type") or form_type,
            )
        if text and len(text) > max_chars_per_filing:
            text = text[:max_chars_per_filing].rstrip() + "..."
        enriched.append({
            "ticker": ticker.upper(),
            "cik": cik,
            "accession": h.get("accession"),
            "filed_at": h.get("filed_at"),
            "form_type": h.get("form_type"),
            "items": h.get("items_desc"),
            "filing_url": h.get("filing_href"),
            "body": text,
            **({"error": ferr} if ferr else {}),
        })

    return _apply_binding(bind_as, {
        "source": "edgar_direct",
        "ticker": ticker.upper(),
        "cik": cik,
        "form_type": form_type,
        "item_section": item_section,
        "filed_at_gte": filed_at_gte,
        "filed_at_lte": filed_at_lte,
        "hit_count": len(enriched),
        "hits": enriched,
    })


FIND_SEC_FILING_EDGAR = Tool(
    name="find_sec_filing_edgar",
    description=(
        "DOC-NOT-IN-INDEX FALLBACK. Fetches SEC filings directly from "
        "EDGAR (not from sec_filings_chunked) when bm25_sec has missed "
        "a filing you have external evidence exists. Common triggers: "
        "Item 5.07 vote-results 8-Ks (often filed by proxy agents under "
        "a non-issuer CIK and occasionally dropped at ingestion), newly "
        "filed amendments bm25_sec hasn't indexed yet, filings older "
        "than the 730-day recency floor when no explicit date filter "
        "was passed. NOT a replacement for bm25_sec -- this tool is "
        "slower (2-5s per filing) and rate-limited by EDGAR. Returns "
        "up to `k` matching filings with primary-document text "
        "(BS4-stripped, capped per `max_chars_per_filing`). Each hit "
        "carries accession + filing_url so a follow-up call can drill "
        "in with extract_filing_tables when you need specific tables. "
        "Use this AFTER bm25_sec returns zero hits for a "
        "(ticker, form_type, date_range) the question explicitly "
        "anchors to."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": (
                    "Issuer ticker, e.g. 'FL' (Foot Locker), 'AAPL'. "
                    "Resolved to CIK via SEC's public "
                    "company_tickers.json."
                ),
            },
            "form_type": {
                "type": "string",
                "default": "8-K",
                "description": (
                    "SEC form type: '8-K', '10-K', '10-Q', 'DEF 14A', "
                    "etc. Matches EDGAR's browse-edgar `type` parameter."
                ),
            },
            "item_section": {
                "type": "string",
                "description": (
                    "Optional. 8-K item code to filter on: '5.07' "
                    "(vote results), '2.02' (earnings), '5.02' "
                    "(director changes). Substring match against the "
                    "filing's items descriptor."
                ),
            },
            "filed_at_gte": {
                "type": "string",
                "description": (
                    "Optional ISO date (YYYY-MM-DD) lower bound on "
                    "filed_at. EDGAR uses YYYYMMDD; this tool reformats."
                ),
            },
            "filed_at_lte": {
                "type": "string",
                "description": "Optional ISO date upper bound on filed_at.",
            },
            "k": {
                "type": "integer",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
                "description": (
                    "Max matching filings to fetch (each costs one "
                    "directory listing + one HTML doc fetch)."
                ),
            },
            "max_chars_per_filing": {
                "type": "integer",
                "default": 12_000,
                "minimum": 1_000,
                "maximum": 30_000,
                "description": (
                    "Per-filing text cap. 8-K primary docs are typically "
                    "3-15 KB; 10-Ks are much larger -- use "
                    "extract_filing_tables on the returned accession "
                    "for those."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker"],
    },
    fn=_do_find_sec_filing_edgar,
)


# --------------------------------------------------------------------------
# get_cover_page_share_counts -- per-class shares-outstanding from cover page
# --------------------------------------------------------------------------
#
# Why this exists. The 10-K / 10-Q cover page carries the SEC-mandated
# "shares of class X outstanding as of <date>" disclosure, tagged in
# iXBRL as ``dei:EntityCommonStockSharesOutstanding`` with a
# ``us-gaap:StatementClassOfStockAxis`` segment dimension identifying
# the share class. This is the canonical answer to "how many shares
# are outstanding" questions -- *not* the consolidated stockholders'
# equity table, which can show different numbers (e.g. Airbnb's Class
# H shares are 9,200,000 outstanding on the cover page but net to zero
# in the equity rollforward because the consolidated Host Endowment
# Fund holds them and is eliminated in consolidation).
#
# Implementation reuses the existing ``get_xbrl_facts`` plumbing
# (cache, sec-api auth, error handling) and just filters + groups the
# returned facts by the segment-axis member, mapping
# ``CommonClass[A-Z]Member`` to a clean class letter.

_COVER_PAGE_SHARES_CONCEPT = "EntityCommonStockSharesOutstanding"
_COVER_PAGE_CLASS_RE = re.compile(r"CommonClass([A-Z]+)Member", re.IGNORECASE)


def _class_letter_from_segment(segment: Sequence[Mapping[str, Any]]) -> str:
    """Map a segment list to a clean class identifier.

    Looks for any ``...CommonClass<X>Member`` value across the axes
    and returns the captured letter(s). Returns ``""`` for facts with
    no class-of-stock dimension (single-class filers).
    """
    for entry in segment or []:
        value = str(entry.get("value") or "")
        m = _COVER_PAGE_CLASS_RE.search(value)
        if m:
            return m.group(1).upper()
    return ""


def _do_get_cover_page_share_counts(
    ref: str,
    *,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Return per-class shares outstanding from the filing's cover page.

    ``ref`` is a hit id from :func:`_do_bm25_sec` (shape:
    ``"sec:<accession>[:<item>]"``). The accession is extracted, the
    iXBRL facts are pulled (cached) via :func:`_do_get_xbrl_facts`,
    and the cover-page-mandated
    ``dei:EntityCommonStockSharesOutstanding`` facts are grouped by
    the share-class axis member.

    Returns a structured shape with one entry per class, plus the
    "as of" instant date so the caller can verify the disclosure
    date matches what they asked about. For single-class filers the
    ``class`` field is the empty string.
    """
    accession = _xbrl_extract_accession(ref)
    if not accession:
        return {
            "ref": ref,
            "error": f"could not parse accession from ref={ref!r}",
            "classes": [],
        }
    facts_result = _do_get_xbrl_facts(
        ref,
        concept_pattern=_COVER_PAGE_SHARES_CONCEPT,
        limit=50,
    )
    if facts_result.get("error"):
        return {
            "ref": ref,
            "accession": accession,
            "error": facts_result["error"],
            "classes": [],
        }
    raw_facts = facts_result.get("facts", []) or []

    classes: list[dict[str, Any]] = []
    for f in raw_facts:
        # Only keep cover-page DEI facts. sec-api groups iXBRL output
        # by section. For most filings the canonical section name is
        # ``CoverPage`` (exact); some issuers' filings come back tagged
        # with variants like ``Cover``, ``CoverDocument``, ``Cover
        # Page``, or ``DocumentInformation``. Accept any section that
        # contains "cover" case-insensitively, plus the empty-section
        # case (some filings emit DEI facts without a section tag).
        # Other concept variants (us-gaap CommonStockSharesOutstanding
        # in XBRL equity roll-forwards) are excluded upstream by the
        # ``_COVER_PAGE_SHARES_CONCEPT`` substring filter on
        # ``EntityCommonStockSharesOutstanding`` -- a DEI-only concept.
        section = str(f.get("section") or "").lower()
        if section and "cover" not in section:
            continue
        try:
            shares = int(str(f.get("value")).replace(",", ""))
        except (TypeError, ValueError):
            shares = f.get("value")
        period = f.get("period") or {}
        as_of = period.get("instant") or period.get("endDate")
        segment = f.get("segment") or []
        classes.append({
            "class": _class_letter_from_segment(segment),
            "shares": shares,
            "as_of": as_of,
            "axis_member": (segment[0].get("value") if segment else None),
        })

    # Stable ordering: class letter alphabetical, with single-class
    # (empty string) sorted last so multi-class filings present A, B,
    # C, H in that order.
    classes.sort(key=lambda c: (c["class"] == "", c["class"] or "~"))

    result = {
        "ref": ref,
        "accession": accession,
        "concept": _COVER_PAGE_SHARES_CONCEPT,
        "classes": classes,
        "matched_count": len(classes),
    }
    if not classes:
        result["hint"] = (
            "No cover-page EntityCommonStockSharesOutstanding facts "
            "found for this filing. The issuer may not have iXBRL-"
            "tagged the cover page (rare for 10-K/10-Q; common for "
            "older 8-K). Fall back to extract_filing_tables on the "
            "primary document or get_full_text to read the cover-page "
            "language directly."
        )
    return _apply_binding(bind_as, result)


GET_COVER_PAGE_SHARE_COUNTS = Tool(
    name="get_cover_page_share_counts",
    description=(
        "Return the per-class shares-outstanding count from a SEC "
        "filing's cover page -- the SEC-mandated "
        "'X shares of Class Y common stock outstanding as of <date>' "
        "disclosure, sourced from the iXBRL "
        "dei:EntityCommonStockSharesOutstanding tag. PREFER this over "
        "extract_filing_tables / get_xbrl_facts for ANY question of "
        "the form 'how many shares of <ticker> are outstanding [by "
        "class]'. The cover-page count is the canonical answer; the "
        "consolidated stockholders' equity table can disagree (e.g. "
        "Airbnb's Class H shares are 9,200,000 on the cover page but "
        "net to zero in the equity rollforward because the "
        "consolidated Host Endowment Fund holds them and is "
        "eliminated in consolidation). Pass `ref` from a `bm25_sec` "
        "hit (shape: 'sec:<accession>[:<item>]'); the accession is "
        "what's used. Returns a list of `{class, shares, as_of, "
        "axis_member}` dicts, one per share class, sorted by class "
        "letter."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "Hit id from bm25_sec, shape "
                    "'sec:<accession>[:<item>]'. The accession is "
                    "what's used; any item suffix is ignored."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ref"],
    },
    fn=_do_get_cover_page_share_counts,
)


# --------------------------------------------------------------------------
# get_registered_securities -- cover-page "Securities registered under 12(b)"
# --------------------------------------------------------------------------
#
# Why this exists. The 10-K / 10-Q cover page carries the SEC-mandated
# "Securities registered pursuant to Section 12(b) of the Act" table --
# Title of each class | Trading Symbol(s) | Name of each exchange on
# which registered -- tagged in iXBRL as ``dei:Security12bTitle`` /
# ``dei:TradingSymbol`` / ``dei:SecurityExchangeName``, dimensioned once
# per registered security via a class-of-stock / equity-class axis. This
# is the canonical answer to "which securities -- common stock, debt
# notes, preferred, depositary shares, warrants, units -- are
# registered / listed to trade on a national securities exchange under
# <issuer>'s name". It is NOT the S-3 / 424B / 8-K "new offering" path:
# those announce *issuances*; this table is the standing list of what's
# on an exchange right now. (This was a documented FinanceBench miss --
# "which debt securities are registered to trade on a national exchange
# under 3M's name as of Q2 2023" -- where the swarm chased S-3 / 424B /
# 8-K filings instead of reading the 10-Q cover page.)
#
# Implementation reuses the ``get_xbrl_facts`` plumbing (cache, sec-api
# auth, error handling): one cached fetch, then per-concept filters,
# grouped by the segment-axis member.

# ``TradingSymbol`` as a substring also matches ``NoTradingSymbolFlag``,
# so the per-fact dispatch below handles that concept too without a
# separate query.
_REGISTERED_SECURITIES_CONCEPTS = (
    "Security12bTitle",       # title of each class registered under 12(b)
    "TradingSymbol",          # ticker / trading symbol (+ NoTradingSymbolFlag)
    "SecurityExchangeName",   # exchange on which registered
    "Security12gTitle",       # registered under 12(g) (no exchange) -- rare on cover
)

_CLASS_AXIS_SUFFIXES = (
    "statementclassofstockaxis",
    "classofstockaxis",
    "equityclassaxis",
)


def _security_member_key(segment: Sequence[Mapping[str, Any]]) -> str:
    """Group key for a cover-page registered-security fact.

    The Section 12(b) facts are dimensioned by a class-of-stock /
    equity-class axis, one member per registered security (common
    stock, each note series, ...). Return the member value as the
    grouping key; ``""`` for an undimensioned fact (single-security
    filer that tagged the table without a dimension).
    """
    for entry in segment or []:
        dim = str(entry.get("dimension") or "").lower()
        if dim.endswith(_CLASS_AXIS_SUFFIXES):
            return str(entry.get("value") or "")
    # Filers occasionally use an extension axis -- fall back to the
    # first member value rather than collapsing everything to one row.
    for entry in segment or []:
        v = entry.get("value")
        if v:
            return str(v)
    return ""


def _do_get_registered_securities(
    ref: str,
    *,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Return the cover-page Section 12(b) registered-securities table.

    ``ref`` is a hit id from :func:`_do_bm25_sec` (shape
    ``"sec:<accession>[:<item>]"``). Pulls the cover-page iXBRL DEI
    facts (``Security12bTitle`` / ``TradingSymbol`` /
    ``SecurityExchangeName``), grouped one row per registered security.
    Returns ``{ref, accession, securities: [{title, trading_symbol,
    exchange, section_12b, axis_member}], matched_count}``, common
    stock first.
    """
    accession = _xbrl_extract_accession(ref)
    if not accession:
        return {
            "ref": ref,
            "error": f"could not parse accession from ref={ref!r}",
            "securities": [],
        }

    # One network fetch (cached by accession); the per-concept calls
    # below just re-filter the cached tree.
    by_member: dict[str, dict[str, Any]] = {}
    err: Optional[str] = None
    for concept in _REGISTERED_SECURITIES_CONCEPTS:
        fr = _do_get_xbrl_facts(ref, concept_pattern=concept, limit=50)
        if fr.get("error"):
            err = fr["error"]
            continue
        for f in fr.get("facts", []) or []:
            section = str(f.get("section") or "")
            if section and section.lower() != "coverpage":
                continue
            short = str(f.get("concept") or "").split(":")[-1]
            key = _security_member_key(f.get("segment") or [])
            row = by_member.setdefault(
                key,
                {
                    "title": None,
                    "trading_symbol": None,
                    "exchange": None,
                    "section_12b": True,
                    "axis_member": key or None,
                },
            )
            val = f.get("value")
            if short == "Security12bTitle":
                row["title"] = val
                row["section_12b"] = True
            elif short == "Security12gTitle":
                if row["title"] is None:
                    row["title"] = val
                row["section_12b"] = False
            elif short == "TradingSymbol":
                row["trading_symbol"] = val
            elif short == "SecurityExchangeName":
                row["exchange"] = val
            # NoTradingSymbolFlag is informational only -- absence of a
            # symbol is already represented by trading_symbol=None.

    if not by_member and err:
        return {"ref": ref, "accession": accession, "error": err, "securities": []}

    def _is_common(s: Mapping[str, Any]) -> bool:
        m = str(s.get("axis_member") or "").lower()
        t = str(s.get("title") or "").lower()
        return "common" in m or "common stock" in t

    securities = sorted(
        by_member.values(),
        key=lambda s: (0 if _is_common(s) else 1, str(s.get("title") or "~")),
    )

    result = {
        "ref": ref,
        "accession": accession,
        "securities": securities,
        "matched_count": len(securities),
    }
    if not securities:
        result["hint"] = (
            "No cover-page Section 12(b) iXBRL facts found for this "
            "filing (dei:Security12bTitle / dei:TradingSymbol / "
            "dei:SecurityExchangeName). Either the filing pre-dates "
            "cover-page iXBRL tagging, or it's the wrong filing for the "
            "issuer/period -- the registered-securities table lives on "
            "the cover page of the 10-K / 10-Q (use the 10-Q whose "
            "reporting period covers the date asked about). Fall back "
            "to get_full_text on the cover-page chunk (item ':1' / "
            "':cover' or chunk_0) and read the 'Securities registered "
            "pursuant to Section 12(b)' table directly."
        )
    return _apply_binding(bind_as, result)


GET_REGISTERED_SECURITIES = Tool(
    name="get_registered_securities",
    description=(
        "Return the cover-page 'Securities registered pursuant to "
        "Section 12(b) of the Act' table from a SEC filing -- Title of "
        "each class / Trading Symbol(s) / Name of each exchange on "
        "which registered -- sourced from the iXBRL dei:Security12bTitle "
        "/ dei:TradingSymbol / dei:SecurityExchangeName tags. PREFER "
        "this over bm25_sec / get_full_text / extract_filing_tables for "
        "ANY question of the form 'which securities are registered / "
        "listed to trade on a national securities exchange under "
        "<issuer>'s name' -- common stock AND debt notes, preferred "
        "stock, depositary shares, warrants, units. NOTE: this is the "
        "STANDING list of what is on an exchange; it is NOT the S-3 / "
        "424B2 / 424B5 / 8-K path -- those announce *new offerings*, "
        "not the registered-securities table. Pass `ref` from a "
        "`bm25_sec` hit on the issuer's 10-K or 10-Q (for an 'as of "
        "Q[N] YYYY' / 'as of <date>' question, use the 10-Q whose "
        "reporting period covers that date); the accession is what's "
        "used. Returns `{securities: [{title, trading_symbol, exchange, "
        "section_12b, axis_member}], matched_count}`, common stock "
        "first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "Hit id from bm25_sec, shape "
                    "'sec:<accession>[:<item>]'. The accession is what's "
                    "used; any item suffix is ignored."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ref"],
    },
    fn=_do_get_registered_securities,
)



_CONSUMER_STAPLES_PAYOUT_COHORT: tuple[str, ...] = (
    # Beverages
    "KO", "PEP", "KDP", "MNST", "STZ",
    # Food Products
    "GIS", "K", "KHC", "SJM", "MDLZ", "CAG", "HSY", "HRL", "CPB",
    # Household & Personal Products
    "PG", "CL", "KMB", "CHD", "CLX",
)






_TRAJECTORY_DEFAULT_METRICS: tuple[str, ...] = (
    "Revenues",  # also matches RevenueFromContractWithCustomerExcludingAssessedTax
    "GrossProfit",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
)

# Regex for the Item 1 "Competition" sub-section. SEC 10-Ks typically
# render this as a header followed by 2-4 paragraphs, then the next
# Item-1 sub-heading (Human Capital / Government Regulation / etc.).
# We grab a generous window — the model can re-quote what it wants.
_COMPETITION_SECTION_RE = re.compile(
    r"(?is)(?:^|\n|\.)\s*Competition\s*[\n:]+(.{200,4000}?)"
    r"(?=\n\s*(?:Human\s+Capital|Government\s+Regulation|Intellectual\s+Property"
    r"|Employees|Environmental|Available\s+Information|Properties|Risk\s+Factors|Item\s+\d))"
)


def _extract_competition_section(body_text: str) -> str:
    """Return the Item 1 'Competition' sub-section if locatable, else empty.

    Falls back to empty string (caller can decide to emit the whole
    Item 1 chunk). Conservative: only matches when both the
    'Competition' header and a known terminating sub-heading are
    present so we don't grab pages of unrelated text.
    """
    if not body_text:
        return ""
    m = _COMPETITION_SECTION_RE.search(body_text)
    if not m:
        return ""
    snippet = m.group(1).strip()
    # Collapse runs of whitespace introduced by HTML-to-text.
    snippet = re.sub(r"[ \t]+", " ", snippet)
    snippet = re.sub(r"\n{3,}", "\n\n", snippet)
    return snippet


def _format_trajectory_table(
    series: dict[str, dict[int, float]],
    fy_start: int,
    fy_end: int,
) -> str:
    """Render a FY-by-FY markdown table from {metric: {fy: value}}.

    Empty cells render as '—'. Header order matches
    _TRAJECTORY_DEFAULT_METRICS so the layout is stable across calls.
    """
    years = list(range(fy_start, fy_end + 1))
    metrics = [m for m in series.keys() if series[m]]
    if not metrics or not years:
        return ""

    def _fmt(v: Optional[float]) -> str:
        if v is None:
            return "—"
        av = abs(v)
        if av >= 1_000_000_000:
            return f"${v / 1_000_000_000:.2f}B"
        if av >= 1_000_000:
            return f"${v / 1_000_000:.0f}M"
        if av >= 1_000:
            return f"${v / 1_000:.1f}K"
        return f"${v:.2f}"

    header = "| FY | " + " | ".join(metrics) + " |"
    sep = "|---|" + "|".join("---" for _ in metrics) + "|"
    rows = [header, sep]
    for fy in years:
        cells = [_fmt(series[m].get(fy)) for m in metrics]
        rows.append(f"| {fy} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _collect_trajectory_facts(
    accession_refs: Sequence[str],
    metric_keys: Sequence[str],
    fy_start: int,
    fy_end: int,
) -> tuple[dict[str, dict[int, float]], list[str]]:
    """Pull XBRL facts from ``accession_refs`` (most recent first) and
    fold them into ``{metric: {fy: value}}`` for full-year periods in
    ``[fy_start, fy_end]``. Returns the series plus a list of
    accessions actually consumed (those that returned facts).

    For each (metric, FY) pair the first non-segment, full-year value
    wins — that's the most recent 10-K's reported number when refs are
    ordered newest-first.
    """
    series: dict[str, dict[int, float]] = {m: {} for m in metric_keys}
    used: list[str] = []
    for ref in accession_refs:
        for metric in metric_keys:
            need = [fy for fy in range(fy_start, fy_end + 1) if fy not in series[metric]]
            if not need:
                continue
            facts_env = _do_get_xbrl_facts(
                ref,
                concept_pattern=metric,
                limit=200,
            )
            if facts_env.get("error"):
                continue
            for fact in facts_env.get("facts") or []:
                period = fact.get("period") or {}
                start = (period.get("startDate") or "").strip()
                end = (period.get("endDate") or "").strip()
                # Full-year fact: 360-380 day span ending in a year we want.
                if not (start and end):
                    continue
                try:
                    from datetime import date as _date  # noqa: PLC0415
                    s_d = _date.fromisoformat(start)
                    e_d = _date.fromisoformat(end)
                except ValueError:
                    continue
                if (e_d - s_d).days < 350 or (e_d - s_d).days > 380:
                    continue
                fy = e_d.year
                if fy not in need:
                    continue
                # Skip dimension-scoped facts (segment breakdowns).
                # _xbrl_flatten emits dim info under 'dimensions' or
                # 'segments'; absence = consolidated total.
                dims = fact.get("dimensions") or fact.get("segments") or []
                if dims:
                    continue
                value = fact.get("value")
                try:
                    series[metric][fy] = float(value)
                except (TypeError, ValueError):
                    continue
            if ref not in used:
                used.append(ref)
    return series, used






# ==========================================================================
# Tool batch 0.0.313 — six tools targeting Vals AI hard-fail rows that the
# best-of-both Qwen+Kimi stack couldn't crack with prompts alone. Same
# plateau-resolution pattern as `compute_payout_ratio_peers` (Row 41) and
# `compute_competitive_trajectory` (Hard rule 5.14): each tool encodes a
# financial convention in code so the model can't drift off it.
#
# All six are designed for GENERAL patterns, not eval-keyed values. The
# tool takes (ticker, fy_window, kpi_or_concept) as parameters and returns
# a pre-composed `answer_summary_block` ready to quote verbatim.
# ==========================================================================



_FCF_CONCEPTS: tuple[str, ...] = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
)






_MA_RATIO_RE = re.compile(
    r"(?:exchange\s+ratio\s+of\s+)?"
    r"(\d+\.\d{3,5})\s+shares?\s+of\s+([\w\s.&,\-']{2,80}?)"
    r"\s+(?:Class\s+[\w\s]+\s+)?(?:common\s+(?:stock|share)|stock|share)s?\s+"
    r"per\s+(?:share\s+of\s+)?(\w[\w\s.\-]{1,40}?)\b",
    re.IGNORECASE,
)
# Equity value: any of "equity value of $X B", "total equity value of $X B",
# "implied equity value of $X B".
_MA_EQUITY_RE = re.compile(
    r"(?:total\s+|implied\s+|aggregate\s+)?equity\s+value\s+of\s+"
    r"(?:approximately\s+|about\s+)?\$\s*([\d,]+\.?\d*)\s*(billion|million|B|M|bn|mn)\b",
    re.IGNORECASE,
)
# Enterprise value: same flexibility.
_MA_EV_RE = re.compile(
    r"(?:total\s+|implied\s+|aggregate\s+)?enterprise\s+value\s+of\s+"
    r"(?:approximately\s+|about\s+)?\$\s*([\d,]+\.?\d*)\s*(billion|million|B|M|bn|mn)\b",
    re.IGNORECASE,
)
# Per-share consideration. Tolerates "approximately $X per share",
# "consideration of $X per share", "$X.XX per [Target] share".
_MA_PPS_RE = re.compile(
    r"\$\s*(\d+\.?\d*)\s+per\s+(?:share|[A-Z]{1,5}\s+share)",
    re.IGNORECASE,
)







_OPERATING_KPI_KEYWORDS: tuple[str, ...] = (
    "Operating Statistics",
    "Key Operating Metrics",
    "Selected Operating Data",
    "Key Performance Indicators",
    "Operating Highlights",
    "Operating Data",
    "Operational Metrics",
)







_TAKE_RATE_REVENUE_CONCEPTS: tuple[str, ...] = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
)
_TAKE_RATE_VOLUME_CONCEPTS: tuple[str, ...] = (
    "GrossBookings",
    "GrossMerchandiseVolume",
    "GrossMerchandiseValue",
    "TotalGrossBookings",
    "Bookings",
)


def _collect_segment_facts(
    accession_ref: str,
    concept_pattern: str,
    fy_target: int,
    fy_prior: int,
) -> dict[str, dict[int, float]]:
    """Pull XBRL facts matching ``concept_pattern`` and group by the
    segment-axis dimension value. Returns ``{segment_name: {fy: value}}``
    for full-year facts only. When the issuer doesn't tag a segment-axis
    dimension (or the concept is consolidated-only), the result is empty
    and the caller falls back to extract_filing_tables.
    """
    facts_env = _do_get_xbrl_facts(
        accession_ref, concept_pattern=concept_pattern, limit=200,
    )
    if facts_env.get("error"):
        return {}
    segments: dict[str, dict[int, float]] = {}
    for fact in facts_env.get("facts") or []:
        period = fact.get("period") or {}
        start = (period.get("startDate") or "").strip()
        end = (period.get("endDate") or "").strip()
        if not (start and end):
            continue
        try:
            from datetime import date as _date  # noqa: PLC0415
            s_d = _date.fromisoformat(start)
            e_d = _date.fromisoformat(end)
        except ValueError:
            continue
        if (e_d - s_d).days < 350 or (e_d - s_d).days > 380:
            continue
        fy = e_d.year
        if fy not in (fy_target, fy_prior):
            continue
        # Segment-scoped facts carry a dimensions/segments field. The
        # value is typically a member like
        # ``uber:MobilitySegmentMember`` or
        # ``us-gaap:OperatingSegmentsMember``. We want the FIRST
        # non-default segment-axis dimension.
        dims = fact.get("dimensions") or fact.get("segments") or []
        if not dims:
            # Consolidated total; skip (handled separately).
            continue
        # Each dim is a {axis, member} pair OR a flat string. Try both.
        seg_name = None
        for d in dims:
            if isinstance(d, dict):
                axis = (d.get("axis") or "").lower()
                member = d.get("member") or d.get("value") or ""
                if "segment" in axis or "businesssegments" in axis or "operatingsegments" in axis:
                    seg_name = str(member)
                    break
            elif isinstance(d, str) and "segment" in d.lower():
                seg_name = d
                break
        if not seg_name:
            continue
        # Normalize segment name: strip namespace prefix + "Member" suffix.
        clean = seg_name.split(":")[-1]
        if clean.endswith("Member"):
            clean = clean[:-6]
        if clean.endswith("Segment"):
            clean = clean[:-7]
        if not clean:
            continue
        try:
            value = float(fact.get("value"))
        except (TypeError, ValueError):
            continue
        segments.setdefault(clean, {})[fy] = value
    return segments







_SEC_API_FILINGS_URL = "https://api.sec-api.io"

# Regex patterns for monthly revenue press releases. The dominant
# format is TSMC's: "Net revenue for [Month] [Year] was approximately
# NT$XXX,XXX million" (with comma-separated thousands or billion suffix).
# Other variants:
#   "consolidated net revenue for [Month] [Year] of NT$X.XX billion"
#   "revenue for [Month] [Year] reached NT$XXX,XXX million"
#   "[Month] [Year] revenue: NT$XXX,XXX million"
_MONTHLY_REVENUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:net\s+)?(?:consolidated\s+)?revenue\s+for\s+"
        r"([A-Z][a-z]+(?:\s+\d{1,2})?)[,\s]+(\d{4})\s+"
        r"(?:was|of|reached|totaled|amounted\s+to)\s+(?:approximately\s+)?"
        r"(NT\$|US\$|RMB|HK\$|\$|¥|€)\s*([\d,]+\.?\d*)\s*"
        r"(billion|million|B|M|bn|mn)\b",
        re.IGNORECASE,
    ),
    # Reverse-order: "[Month] [Year] revenue: NT$X million"
    re.compile(
        r"([A-Z][a-z]+(?:\s+\d{1,2})?)[,\s]+(\d{4})\s+(?:net\s+)?(?:consolidated\s+)?"
        r"revenue[:\s]+(?:was|of|reached|totaled|amounted\s+to)?\s*(?:approximately\s+)?"
        r"(NT\$|US\$|RMB|HK\$|\$|¥|€)\s*([\d,]+\.?\d*)\s*"
        r"(billion|million|B|M|bn|mn)\b",
        re.IGNORECASE,
    ),
)

_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def _month_name_to_num(name: str) -> Optional[int]:
    """Return 1-12 for English month names; None for non-matches."""
    if not name:
        return None
    s = name.strip().lower()
    for i, m in enumerate(_MONTH_NAMES, 1):
        if s.startswith(m) or m.startswith(s[:3]) and len(s) >= 3:
            return i
    return None


# sec-api.io's query endpoint caps each request at size=50; paginate
# via `from` offset to walk through longer windows. We cap total
# pagination at 5 pages (250 filings) so a runaway query never
# hammers the API.
_SEC_API_PAGE_SIZE = 50
_SEC_API_MAX_PAGES = 5


def _list_sec_filings(
    ticker: str,
    form_type: str,
    filed_at_gte: str,
    filed_at_lte: str,
    max_results: int = 250,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Query sec-api.io for filings of ``ticker`` + ``form_type`` in the
    [filed_at_gte, filed_at_lte] window (dates in YYYY-MM-DD).

    Returns (filings_list, error). Each filing carries at minimum
    ``filedAt``, ``accessionNo``, ``primaryDocumentUrl``. Paginates
    automatically at size=50 (sec-api's hard cap) up to ``max_results``.
    """
    api_key = os.environ.get("SEC_API_KEY", "").strip()
    if not api_key:
        return [], (
            "SEC_API_KEY missing from sandbox env. The gateway "
            "credential injector should provide it when the alphacumen "
            "manifest declares api.sec-api.io in egress."
        )
    query = (
        f'ticker:{ticker.upper()} AND formType:"{form_type}" '
        f'AND filedAt:[{filed_at_gte} TO {filed_at_lte}]'
    )
    url = f"{_SEC_API_FILINGS_URL}?token={api_key}"
    pages = min(_SEC_API_MAX_PAGES, max(1, (max_results + _SEC_API_PAGE_SIZE - 1) // _SEC_API_PAGE_SIZE))
    out: list[dict[str, Any]] = []
    for page in range(pages):
        body = json.dumps({
            "query": query,
            "from": str(page * _SEC_API_PAGE_SIZE),
            "size": str(_SEC_API_PAGE_SIZE),
            "sort": [{"filedAt": {"order": "asc"}}],
        }).encode("utf-8")
        req = urllib.request.Request(url=url, method="POST", data=body)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept-Encoding", "gzip")
        try:
            with urllib.request.urlopen(
                req, timeout=_SEC_API_TIMEOUT_S, context=_SEC_API_SSL_CTX,
            ) as resp:
                raw = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
                    import gzip  # noqa: PLC0415
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return out, f"sec-api HTTP {exc.code}: {exc.reason} (page={page})"
        except urllib.error.URLError as exc:
            return out, f"sec-api unreachable: {exc.reason} (page={page})"
        try:
            body_json = json.loads(text)
        except json.JSONDecodeError as exc:
            return out, f"sec-api response not JSON: {exc} (page={page})"
        page_filings = body_json.get("filings") or []
        out.extend(page_filings)
        # If the page returned fewer than the page size, we're done.
        if len(page_filings) < _SEC_API_PAGE_SIZE:
            break
        if len(out) >= max_results:
            break
    return out[:max_results], None







_RATIO_CHOICES = (
    "p_to_b", "d_to_e", "d_to_tc",
    "ev_ebitda", "ebitda_margin", "dio",
    "fccr", "ebitdar_margin",
)


def _fy_period_end_date(fy: int, ticker: str) -> str:
    """Best-effort FY-end calendar date.

    Most US large-caps file Dec FY-ends. For known non-Dec calendars
    we hardcode common cases; everything else defaults to Dec 31.
    """
    nondec = {
        # Jan / Feb FY-ends (retailers, mostly)
        "TGT": (1, 31), "WMT": (1, 31), "HD": (2, 1), "LOW": (1, 31),
        "TJX": (1, 31), "ROST": (1, 31), "COST": (8, 31), "WBA": (8, 31),
        "DELL": (1, 31), "DPZ": (12, 31), "ADBE": (11, 30),
        "LULU": (2, 2), "VSCO": (2, 1),  # apparel retail Jan/Feb FY-ends
        "ASPI": (2, 28),  # specialty (Feb FY-end)
        "BBSI": (12, 31),  # default Dec
        # May / Jun / Jul FY-ends
        "GIS": (5, 31), "MU": (8, 31), "ORCL": (5, 31), "NKE": (5, 31),
        "CSCO": (7, 31), "SJM": (4, 30), "HPE": (10, 31),
        # Other
        "CRM": (1, 31), "WSC": (12, 31), "PFE": (12, 31),
    }
    t = ticker.upper()
    if t in nondec:
        m, d = nondec[t]
        # The fiscal "FY2024" for retailers ending Jan/Feb 2025 still
        # refers to FY ending in the calendar year 2024 by Vals AI
        # convention (Target FY2024 = period ending Feb 1 2025 ≈
        # FY-end falls in `fy+1` for early-year FY-ends).
        if m <= 2:
            return f"{fy + 1:04d}-{m:02d}-{d:02d}"
        return f"{fy:04d}-{m:02d}-{d:02d}"
    return f"{fy:04d}-12-31"


def _strip_chunk_suffix(ref: str) -> str:
    """Return the accession-only ref (``sec:<accession>``).

    ``bm25_sec`` hits expose chunk-bounded refs of the form
    ``sec:<accession>:chunk_<N>`` (and 10-Q hits often carry
    additional dimensional suffixes). Downstream extractors that
    work over the full filing's Item HTML (``extract_filing_tables``)
    need the accession-only form -- passing the chunk-bounded ref
    restricts them to a single chunk's HTML, which silently misses
    tables that live in other chunks (e.g. the consolidated non-GAAP
    recon often sits in a different chunk than the segment overview).
    """
    if not isinstance(ref, str) or not ref:
        return ref
    parts = ref.split(":")
    if len(parts) >= 2 and parts[0] == "sec":
        return f"sec:{parts[1]}"
    return ref


def _find_10k_ref_for_fy(ticker: str, fy: int) -> tuple[str, str]:
    """Find the most-recent FY annual-report accession ref + filed_at.

    Tries 10-K first (US-listed domestic issuers) and falls back to
    20-F (foreign private issuers) when no 10-K is indexed. The
    fallback is generic — domestic issuers never have a 20-F so the
    fallback search returns empty for them; FPIs (AER, BABA, NVO,
    etc.) get unblocked without the caller needing to know the filer
    type a priori.
    """
    fy_end_iso = _fy_period_end_date(fy, ticker)
    yend = int(fy_end_iso[:4])
    tight_start = f"{yend:04d}{int(fy_end_iso[5:7]):02d}01"
    tight_end = f"{yend:04d}{int(fy_end_iso[5:7]):02d}31"

    def _search(form_type: str) -> list[dict[str, Any]]:
        env = _do_bm25_sec(
            query=f"{ticker} annual report {form_type}", k=5, fields=None,
            filters={
                "ticker": ticker, "form_type": form_type,
                "event_date_gte": tight_start,
                "event_date_lte": tight_end,
            },
            sort=None, body_mode="snippet",
        )
        h = env.get("hits") or []
        if h:
            return h
        env = _do_bm25_sec(
            query=f"{ticker} annual report {form_type}", k=5, fields=None,
            filters={
                "ticker": ticker, "form_type": form_type,
                "event_date_gte": f"{yend:04d}0101",
                "event_date_lte": f"{yend:04d}1231",
            },
            sort=None, body_mode="snippet",
        )
        h = env.get("hits") or []
        if h:
            return h
        env = _do_bm25_sec(
            query=f"{ticker} annual report {form_type}", k=5, fields=None,
            filters={
                "ticker": ticker, "form_type": form_type,
                "filed_at_gte": f"{yend:04d}-01-01",
                "filed_at_lte": f"{yend + 1:04d}-12-31",
            },
            sort=None, body_mode="snippet",
        )
        return env.get("hits") or []

    hits = _search("10-K") or _search("20-F")
    if not hits:
        return "", ""
    hits.sort(key=lambda h: str((h.get("source") or {}).get("filed_at", "")), reverse=True)
    return hits[0].get("id", ""), str((hits[0].get("source") or {}).get("filed_at", ""))


def _find_filing_ref_for_asof(
    ticker: str, fy: int, asof_iso: Optional[str],
) -> tuple[str, str]:
    """Pick the SEC filing whose balance-sheet date matches ``asof_iso``.

    When ``asof_iso`` is near the issuer's FY-end (within ±35 days) or
    not supplied, defer to :func:`_find_10k_ref_for_fy` for the annual
    report. Otherwise the asof is mid-fiscal-year — search for a 10-Q
    with an event_date within ±45 days of asof_iso (quarter-end report
    dates align with the asof). Falls back to the annual report if no
    matching 10-Q is found, so callers always get a usable ref.

    Generic across issuers: the 10-Q is the right balance-sheet source
    for any interim asof (e.g. Sept 30 2025 for a December-FY issuer
    like FTAI / WLFC), so the multi-issuer comparison rows that hand
    different as-ofs per ticker land on the right per-ticker filings.
    """
    if not asof_iso:
        return _find_10k_ref_for_fy(ticker, fy)
    fy_end_iso = _fy_period_end_date(fy, ticker)
    try:
        from datetime import datetime, timedelta
        d_asof = datetime.fromisoformat(asof_iso[:10])
        d_fy_end = datetime.fromisoformat(fy_end_iso[:10])
    except Exception:  # noqa: BLE001
        return _find_10k_ref_for_fy(ticker, fy)
    if abs((d_asof - d_fy_end).days) <= 35:
        return _find_10k_ref_for_fy(ticker, fy)
    win_lo = (d_asof - timedelta(days=45)).strftime("%Y%m%d")
    win_hi = (d_asof + timedelta(days=45)).strftime("%Y%m%d")
    env = _do_bm25_sec(
        query=f"{ticker} quarterly report 10-Q", k=5, fields=None,
        filters={
            "ticker": ticker, "form_type": "10-Q",
            "event_date_gte": win_lo,
            "event_date_lte": win_hi,
        },
        sort=None, body_mode="snippet",
    )
    hits = env.get("hits") or []
    if not hits:
        return _find_10k_ref_for_fy(ticker, fy)
    hits.sort(
        key=lambda h: str((h.get("source") or {}).get("filed_at", "")),
        reverse=True,
    )
    return hits[0].get("id", ""), str((hits[0].get("source") or {}).get("filed_at", ""))


def _max_positive_xbrl(ref: str, concept_patterns: Sequence[str]) -> Optional[float]:
    """Try a list of concept-name substrings, return first positive value.

    Tries each pattern in order; for each returns the max positive
    fact (covers issuers that report both consolidated and segment-
    scoped values for the same concept).
    """
    for pattern in concept_patterns:
        env = _do_get_xbrl_facts(
            ref=ref, concept_pattern=pattern, periods=None, limit=10,
        )
        if env.get("error"):
            continue
        best: Optional[float] = None
        for fact in env.get("facts", []):
            try:
                v = float(fact.get("value", 0))
            except (TypeError, ValueError):
                continue
            if v > 0 and (best is None or v > best):
                best = v
        if best is not None:
            return best
    return None


def _close_price_at(ticker: str, asof_iso: str) -> Optional[float]:
    """Return the closing price for ``ticker`` on ``asof_iso``.

    Snap to the exact date when bars cover it; fall back to the most-
    recent prior trading close when asof is a weekend / holiday. Mirrors
    the price-selection logic inside :func:`_market_cap_at` so callers
    that also need the raw close (e.g. price-to-book using issuer-
    disclosed BVPS) don't have to refetch.
    """
    try:
        from datetime import datetime, timedelta
        d = datetime.fromisoformat(asof_iso[:10])
        start = (d - timedelta(days=7)).strftime("%Y-%m-%d")
        end = asof_iso[:10]
    except Exception:  # noqa: BLE001
        return None
    bars_env = _do_get_equity_bars(ticker, start=start, end=end)
    rows = bars_env.get("rows") or []
    if not rows:
        return None
    exact_match = next(
        (r for r in rows if str(r.get("date") or "").startswith(end)),
        None,
    )
    if exact_match is not None:
        try:
            c = float(exact_match.get("close") or 0)
            if c > 0:
                return c
        except (TypeError, ValueError):
            pass
    for row in rows[::-1]:
        try:
            c = float(row.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if c > 0:
            return c
    return None


_BVPS_PATTERNS = (
    # AER 20-F shape: "Book value per ordinary share outstanding,
    # excluding shares of unvested restricted stock $ 112.59"
    r"book\s+value\s+per\s+(?:ordinary|common)\s+share[^$]{0,200}?\$\s*([0-9]+(?:\.[0-9]+)?)",
    # Generic "Book value per share $ 12.34" / "Book value per share: 12.34"
    r"book\s+value\s+per\s+share[^$\n]{0,80}?\$?\s*([0-9]+(?:\.[0-9]+)?)",
)


def _scan_bvps(text: Optional[str]) -> Optional[float]:
    """Run the BVPS regex against a body of filing text. Returns the
    first plausible match (≥ $1) or ``None``.
    """
    if not isinstance(text, str) or not text:
        return None
    import re as _re
    clean = _re.sub(r"<[^>]+>", " ", text)
    clean = _re.sub(r"\s+", " ", clean)
    for pat in _BVPS_PATTERNS:
        m = _re.search(pat, clean, _re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if v >= 1.0:
                    return v
            except (TypeError, ValueError):
                continue
    return None


def _extract_bvps_from_filing(ref: str) -> Optional[float]:
    """Scan the filing for issuer-disclosed Book Value Per Share.

    Two-stage scan, because chunked refs return only ONE chunk's body
    (typically 5-15K chars sliced out of a multi-MB filing) and the
    long-form-stub EDGAR fallback in :func:`_do_get_full_text` only
    fires when that chunk body is < 10K chars. Substantive chunks miss
    the fallback and miss the BVPS table (which lives mid-document,
    around page 59 of a 20-F).

    Stage 1: try the body returned by ``_do_get_full_text(ref)`` — if
    the chunk happens to contain the BVPS table or the long-form-stub
    fallback fired, this match is sufficient.

    Stage 2: if stage 1 misses, pull the filing's ``source.url`` and
    fetch the full HTML directly from EDGAR via
    :func:`_fetch_filing_html_from_edgar`. Scan the resulting full
    text. This is the path that catches AER's $112.59 BVPS sitting
    on page 59 of its 20-F when the bm25 hit returned, say, chunk 20.

    Returns ``None`` when no match — caller falls back to MC / equity.
    """
    if not ref:
        return None
    try:
        env = _do_get_full_text(ref, max_chars=2_000_000)
    except Exception:  # noqa: BLE001
        env = {}
    src = env.get("source") if isinstance(env, Mapping) else None
    body = (src or {}).get("body") if isinstance(src, Mapping) else None
    hit = _scan_bvps(body)
    if hit is not None:
        return hit
    url = (src or {}).get("url") if isinstance(src, Mapping) else None
    if isinstance(url, str) and url.startswith("https://www.sec.gov/"):
        try:
            html, _err = _fetch_filing_html_from_edgar(url)
        except Exception:  # noqa: BLE001
            html = None
        if html:
            try:
                from bs4 import BeautifulSoup  # noqa: PLC0415
                full_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
            except Exception:  # noqa: BLE001
                full_text = html
            hit = _scan_bvps(full_text)
            if hit is not None:
                return hit
    return None


def _market_cap_at(
    ticker: str, asof_iso: str, ref: str,
    bs_period_iso: Optional[str] = None,
) -> Optional[float]:
    """Market cap = close price * shares outstanding.

    Snap to the EXACT asof date when bars cover it; only fall back to
    the prior trading close when asof is a weekend / holiday. v0 used
    a 7-day backward window and accepted any close in it, which on
    leasing issuers like FTAI (where shares × wrong-date-close can
    diverge by 5-15%) introduced material precision error.

    Share-count basis: when ``bs_period_iso`` is supplied, prefer the
    XBRL `CommonStockSharesOutstanding` fact AT that instant (FY-end
    or BS-snapshot-date) over the cover-page filing-date count. This
    matches the standard equity-comparables convention of pairing
    period-end actuals with period-end shares; cover-page counts
    typically sit 2-8 weeks AFTER the period-end and reflect interim
    buybacks (LULU/VSCO row-11 wedge: cover page short by ~3M shares
    each vs FY-end basic, driving 1-3% EV/EBITDA precision error).
    """
    try:
        from datetime import datetime, timedelta
        d = datetime.fromisoformat(asof_iso[:10])
        start = (d - timedelta(days=7)).strftime("%Y-%m-%d")
        end = asof_iso[:10]
    except Exception:  # noqa: BLE001
        return None
    bars_env = _do_get_equity_bars(ticker, start=start, end=end)
    rows = bars_env.get("rows") or []
    if not rows:
        return None
    # Prefer EXACT asof match first; fall back to most-recent prior
    # trading close (typical when asof is Sat/Sun/holiday).
    close = None
    exact_match = next(
        (r for r in rows if str(r.get("date") or "").startswith(end)),
        None,
    )
    if exact_match is not None:
        try:
            close = float(exact_match.get("close") or 0)
        except (TypeError, ValueError):
            close = None
    if not close:
        for row in rows[::-1]:
            try:
                close = float(row.get("close") or 0)
            except (TypeError, ValueError):
                continue
            if close > 0:
                break
    if not close:
        return None
    total_shares: Optional[float] = None
    # Stage 1: XBRL shares at the BS-snapshot instant (preferred).
    # Aligns market-cap shares with the same period-end the BS items
    # (equity / debt / cash) are pulled from. Matches the textbook
    # comparables convention -- without it, mid-fiscal-year buybacks
    # between period-end and the cover-page certification date inflate
    # the apparent "as-of" share count vs what the rubric expects.
    if bs_period_iso:
        total_shares = _fy_end_period_fact(
            ref,
            ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"),
            bs_period_iso,
            strict=True,
        )
    # Stage 1.5: when stage 1 fails (no XBRL instant fact at
    # bs_period_iso in `ref` — typical when `ref` is a stale 10-K
    # whose latest XBRL period predates bs_period_iso by a quarter
    # or more), fall through to the LATEST 10-Q / 10-K cover-page
    # share count filed before `asof_iso`. Handles the "stale
    # mkt_ref" case where the nearest annual report has a period_end
    # 6-12 months before the market-price asof but a more recent
    # quarterly filing exists with a closer cover-page date — the
    # quarterly's cover-page count is much closer to the valuation-
    # date share count than the older annual's. Generic across any
    # issuer in the FYE-to-10K-filing interregnum.
    if total_shares is None:
        total_shares = _latest_cover_page_shares_before(ticker, asof_iso)
    # Stage 2: cover-page tag, when no XBRL instant match.
    # ``_do_get_cover_page_share_counts`` returns ``classes`` (per
    # the function's contract at line ~3314); a prior version read
    # ``class_summary`` which never existed, so this path silently
    # always fell through to the XBRL fallback. That mattered for
    # issuers like WLFC that only tag shares on the cover page
    # (``dei:EntityCommonStockSharesOutstanding``) and not in the
    # 10-K body's iXBRL, leaving ``_max_positive_xbrl`` empty and
    # market_cap=None.
    if total_shares is None:
        cover = _do_get_cover_page_share_counts(ref=ref)
        if not cover.get("error"):
            cs = cover.get("classes") or []
            total = 0.0
            for cls in cs:
                try:
                    total += float(cls.get("shares") or 0)
                except (TypeError, ValueError):
                    continue
            if total > 0:
                total_shares = total
    # Stage 3: XBRL max-positive last resort (legacy fallback).
    if total_shares is None:
        total_shares = _max_positive_xbrl(
            ref, ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"),
        )
    if not total_shares:
        return None
    return close * total_shares


def _latest_cover_page_shares_before(
    ticker: str, asof_iso: str,
) -> Optional[float]:
    """Latest cover-page share count from any 10-Q / 10-K filed before asof_iso.

    Used by :func:`_market_cap_at` Stage 1.5 to handle the case where
    the issuer's nearest annual report is older than the market-price
    asof and a more recent quarterly filing exists with a closer
    cover-page certification date. Iterates the top 5 most-recently-
    filed 10-Q / 10-K hits filed on or before ``asof_iso``, returns
    the first non-empty cover-page sum across share classes.

    Generic across issuers: no per-ticker logic, no period-distance
    threshold, no rubric-keyed conditions.
    """
    if not ticker or not asof_iso:
        return None
    asof_dt = asof_iso[:10]
    env = _do_bm25_sec(
        query=f"{ticker} report",
        k=10, fields=None,
        filters={
            "ticker": ticker, "form_type": ["10-Q", "10-K"],
            "filed_at_lte": asof_dt,
        },
        sort=None, body_mode="snippet",
    )
    hits = env.get("hits") or []
    if not hits:
        return None
    hits.sort(
        key=lambda h: str((h.get("source") or {}).get("filed_at", "")),
        reverse=True,
    )
    for hit in hits[:5]:
        hit_ref = hit.get("id", "")
        if not hit_ref:
            continue
        cover = _do_get_cover_page_share_counts(ref=_strip_chunk_suffix(hit_ref))
        if cover.get("error"):
            continue
        cs = cover.get("classes") or []
        total = 0.0
        for cls in cs:
            try:
                total += float(cls.get("shares") or 0)
            except (TypeError, ValueError):
                continue
        if total > 0:
            return total
    return None


_SEGMENT_AXIS_KEYWORDS = (
    "businesssegments", "operatingsegments", "segmentsaxis",
    "consolidationitems", "reportablesegments", "productorservice",
    "geographicareas", "majorcustomers",
)


def _fact_is_segment_scoped(fact: Mapping[str, Any]) -> bool:
    """True iff a flattened XBRL fact carries a segment-axis dimension.

    Multi-segment issuers tag the same concept once at the consolidated
    level (empty segment list) AND once per operating segment / product
    line / geographic area, with the segment-axis dimension identifying
    which slice. Picking a dimensioned fact when the model wanted the
    consolidated total is the row-4 CZR Adjusted EBITDA bug
    ($1.568B Las Vegas segment vs $3-4B consolidated).
    """
    seg = fact.get("segment") or []
    if not isinstance(seg, list):
        return False
    for s in seg:
        if not isinstance(s, Mapping):
            continue
        dim = str(s.get("dimension") or "").lower().replace(":", "").replace("-", "")
        for kw in _SEGMENT_AXIS_KEYWORDS:
            if kw in dim:
                return True
    return False


def _fy_end_period_fact(
    ref: str, concept_patterns: Sequence[str], fy_end_iso: str,
    *,
    strict: bool = False,
) -> Optional[float]:
    """Pick the fact whose period END equals the FY-end calendar date.

    For balance-sheet-instant concepts (equity, debt, inventory,
    cash) the XBRL period is a single date — match the FY-end date
    exactly. Falls back to ``_max_positive_xbrl`` if no exact match,
    UNLESS ``strict=True`` in which case None is returned so the
    caller can route to its own fallback (e.g. cover-page lookup).

    Four-pass priority on each pattern (most specific first):

    1. EXACT concept-name match + consolidated. ``_do_get_xbrl_facts``
       filters by case-insensitive substring, so a pattern like
       ``"StockholdersEquity"`` ALSO matches ``StockholdersEquity
       IncludingPortionAttributableToNoncontrollingInterest``. For
       issuers with material non-controlling interest (FTAI, complex
       holdcos), the consolidated variant can be 10-20x larger than
       parent-only, blowing up P/B. Exact match wins so the parent-
       only concept anchors the ratio.
    2. EXACT concept-name match + segment-scoped. Some Q3 filings
       tag the parent-only equity only at a per-segment scope; this
       pass catches that.
    3. SUBSTRING match + consolidated. Issuers that ONLY tag an
       ``Including...`` variant (no plain `StockholdersEquity`) fall
       through to here.
    4. SUBSTRING match + any (segment-scoped allowed). Last resort.
    """
    target = fy_end_iso[:10]
    for pattern in concept_patterns:
        env = _do_get_xbrl_facts(
            ref=ref, concept_pattern=pattern, periods=None, limit=20,
        )
        if env.get("error"):
            continue
        facts = env.get("facts", [])
        pat_lc = pattern.lower()
        for exact_only in (True, False):
            for require_consolidated in (True, False):
                for fact in facts:
                    if exact_only:
                        concept_local = (
                            str(fact.get("concept") or "")
                            .split(":")[-1]
                            .lower()
                        )
                        if concept_local != pat_lc:
                            continue
                    if require_consolidated and _fact_is_segment_scoped(fact):
                        continue
                    period = str(fact.get("period", ""))
                    if target not in period:
                        continue
                    try:
                        v = float(fact.get("value", 0))
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        return v
    # Fall back if no exact period match — unless strict mode is on,
    # in which case let the caller route to its own next-best lookup.
    if strict:
        return None
    return _max_positive_xbrl(ref, concept_patterns)


def _fy_full_period_fact(
    ref: str, concept_patterns: Sequence[str], fy_end_iso: str,
) -> Optional[float]:
    """Pick the fact whose period spans the full FY (11-13 month duration ending on FY-end).

    For income-statement-duration concepts (Revenue, OperatingIncome,
    D&A, COGS, InterestExpense, OperatingLeaseCost), XBRL periods
    are ``[start, end]`` ranges. The right value is the one whose
    period ends on the FY-end date AND spans 11-13 months (catches
    52/53-week filers + leap-year drift). Without this filter
    ``_max_positive_xbrl`` can pick segment-scope / quarterly /
    restated comparatives — the VSCO row-11 bug.
    """
    target = fy_end_iso[:10]
    target_year = int(target[:4]) if len(target) >= 4 else None
    for pattern in concept_patterns:
        env = _do_get_xbrl_facts(
            ref=ref, concept_pattern=pattern, periods=None, limit=30,
        )
        if env.get("error"):
            continue
        # Two-pass consolidated-first scan -- same logic as
        # _fy_end_period_fact. Without this filter the parser can
        # silently pick a segment-dimensioned duration-fact for
        # Adjusted EBITDA / Revenue / OperatingIncome on multi-segment
        # issuers (the row-4 CZR Adjusted EBITDA bug).
        for require_consolidated in (True, False):
            candidates: list[tuple[float, str]] = []
            for fact in env.get("facts", []):
                if require_consolidated and _fact_is_segment_scoped(fact):
                    continue
                period = str(fact.get("period", ""))
                dates = re.findall(r"(\d{4})-(\d{2})-(\d{2})", period)
                if len(dates) < 2:
                    continue
                (y0, m0, d0), (y1, m1, d1) = dates[0], dates[-1]
                try:
                    y0, m0, d0 = int(y0), int(m0), int(d0)
                    y1, m1, d1 = int(y1), int(m1), int(d1)
                except (TypeError, ValueError):
                    continue
                # End date must match the FY-end (within 3 days for 52/53-week drift).
                end_iso = f"{y1:04d}-{m1:02d}-{d1:02d}"
                if end_iso[:7] != target[:7]:  # same year-month
                    if not (end_iso[:4] == target[:4] and abs(m1 - int(target[5:7])) <= 1):
                        continue
                # Span must be 11-13 months (annual).
                months_span = (y1 - y0) * 12 + (m1 - m0)
                if not (11 <= months_span <= 13):
                    continue
                try:
                    v = float(fact.get("value", 0))
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    candidates.append((v, end_iso))
            if candidates:
                break
        if candidates:
            # Prefer the candidate whose end date is closest to target.
            target_end = target
            best = sorted(
                candidates,
                key=lambda kv: abs(int(kv[1][:4]) * 372 + int(kv[1][5:7]) * 31 + int(kv[1][8:10])
                                   - (int(target_end[:4]) * 372 + int(target_end[5:7]) * 31 + int(target_end[8:10]))),
            )
            return best[0][0]
    # Fall back to max-positive if no period match found.
    return _max_positive_xbrl(ref, concept_patterns)


def _q_period_fact(
    ref: str, concept_patterns: Sequence[str], q_end_iso: str,
) -> Optional[float]:
    """Pick the duration-fact whose period is a single quarter (~3 months)
    ending on the given date.

    Mirror of ``_fy_full_period_fact`` for sub-annual data. Filter
    accepts spans of 2-4 months (catches mid-quarter date drift and
    13-week fiscal-quarter conventions). Use for any sub-annual
    income-statement pull (Q-N 10-Q figures, 8-K quarterly earnings).
    """
    target = q_end_iso[:10]
    for pattern in concept_patterns:
        env = _do_get_xbrl_facts(
            ref=ref, concept_pattern=pattern, periods=None, limit=30,
        )
        if env.get("error"):
            continue
        for require_consolidated in (True, False):
            candidates: list[tuple[float, str]] = []
            for fact in env.get("facts", []):
                if require_consolidated and _fact_is_segment_scoped(fact):
                    continue
                period = str(fact.get("period", ""))
                dates = re.findall(r"(\d{4})-(\d{2})-(\d{2})", period)
                if len(dates) < 2:
                    continue
                (y0, m0, _d0), (y1, m1, _d1) = dates[0], dates[-1]
                try:
                    y0, m0 = int(y0), int(m0)
                    y1, m1 = int(y1), int(m1)
                except (TypeError, ValueError):
                    continue
                end_iso = f"{y1:04d}-{m1:02d}-{int(_d1):02d}"
                # End must match target month within ±1 month for
                # 52/53-week fiscal-quarter drift.
                if end_iso[:4] != target[:4]:
                    continue
                if abs(m1 - int(target[5:7])) > 1:
                    continue
                # Span must be 2-4 months (quarterly).
                months_span = (y1 - y0) * 12 + (m1 - m0)
                if not (2 <= months_span <= 4):
                    continue
                try:
                    v = float(fact.get("value", 0))
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    candidates.append((v, end_iso))
            if candidates:
                break
        if candidates:
            target_end = target
            best = sorted(
                candidates,
                key=lambda kv: abs(int(kv[1][:4]) * 372 + int(kv[1][5:7]) * 31 + int(kv[1][8:10])
                                   - (int(target_end[:4]) * 372 + int(target_end[5:7]) * 31 + int(target_end[8:10]))),
            )
            return best[0][0]
    return None


def _find_filing_for_bs_asof(
    ticker: str, asof_date_iso: str,
) -> tuple[str, str]:
    """Find the 10-Q or 10-K whose balance-sheet snapshot lands on the asof date.

    For interim asof dates (3/31, 6/30, 9/30 for calendar-FY issuers),
    pulls the corresponding 10-Q. For fiscal-year-end dates, pulls
    the 10-K.

    The filter is on ``event_date`` (the *period of report* — the
    date the balance sheet snapshots), not ``filed_at`` (when the
    filing was submitted). For ``asof=2025-09-30`` we want filings
    whose period of report IS 2025-09-30 (FTAI's Q3 10-Q). We
    allow a ±10-day cushion so issuers with non-standard period
    boundaries (e.g. 52-week retailers ending late January
    instead of 12-31) still resolve.

    Falls back to "" when no filing matches; the caller handles
    that by using the FY 10-K instead.
    """
    yend = int(asof_date_iso[:4])
    month = int(asof_date_iso[5:7])
    from datetime import datetime, timedelta
    try:
        asof_dt = datetime.fromisoformat(asof_date_iso[:10])
    except Exception:  # noqa: BLE001
        asof_dt = datetime(yend, month, 28)
    # Search a ±10-day window around the asof on event_date.
    # Previously this used (asof+1, asof+75) which excluded the
    # very date we wanted on every interim BS asof; the bug
    # silently fell back to the FY 10-K and `_max_positive_xbrl`
    # returned FY-end (not Q3) equity.
    e_start = (asof_dt - timedelta(days=10)).strftime("%Y%m%d")
    e_end = (asof_dt + timedelta(days=10)).strftime("%Y%m%d")
    forms_priority = ("10-Q", "10-K", "20-F", "6-K")
    for ft in forms_priority:
        env = _do_bm25_sec(
            query=f"{ticker} {ft}", k=5, fields=None,
            filters={
                "ticker": ticker, "form_type": ft,
                "event_date_gte": e_start,
                "event_date_lte": e_end,
            },
            sort=None, body_mode="snippet",
        )
        hits = env.get("hits") or []
        if hits:
            hits.sort(key=lambda h: str((h.get("source") or {}).get("filed_at", "")))
            top = hits[0]
            return top.get("id", ""), str((top.get("source") or {}).get("filed_at", ""))
    return "", ""


def _pull_issuer_metrics_for_ratios(
    ticker: str, fy: int, asof_market_price: Optional[str],
    bs_asof_date: Optional[str] = None,
) -> dict[str, Any]:
    """Return a dict of all metrics needed by any supported ratio.

    When ``bs_asof_date`` (ISO YYYY-MM-DD) is provided, finds the
    10-Q / 10-K whose balance-sheet snapshot covers that date and
    pulls equity / debt / cash / inventory from THAT filing. Used
    for cross-ticker comparisons where rubric specifies different
    balance-sheet snapshot dates per issuer (e.g. row 9: FTAI 9/30,
    AER 12/31). Income-statement items (revenue, EBITDA, COGS,
    operating-lease cost, interest expense) still come from the
    FY 10-K to preserve the annual-period semantics of EBITDA-
    derived ratios.

    Precision-tightening pass (0.0.341):
    - Balance-sheet items (equity, debt, inventory, cash) pulled with
      a FY-end period-match preference (was: max positive across all
      periods, which sometimes picked a prior-year comparative when
      it exceeded the current FY).
    - Financial debt distinguished from operating-lease liabilities.
      Aircraft-leasing issuers (FTAI, AER, WLFC) and retail issuers
      (LULU, VSCO, TGT) report material `OperatingLeaseLiability*`
      that should NOT be in the leverage ratios' debt numerator
      (rubric convention: leverage = financial debt only).
      ``lease_liability_total`` is tracked separately so EBITDAR /
      FCCR can add it back when needed.
    - Interest expense + operating lease cost pulled for FCCR /
      EBITDAR.
    """
    # FY 10-K used for income-statement items + as a fallback when
    # bs_asof_date isn't provided.
    fy_ref, fy_filed_at = _find_10k_ref_for_fy(ticker, fy)
    if not fy_ref:
        return {"ticker": ticker, "fy": fy, "error": f"No {ticker} 10-K found for FY{fy}."}
    # Balance-sheet ref: when bs_asof_date is supplied, find the
    # filing whose BS snapshot covers that date. Otherwise fall
    # back to the FY 10-K (preserves prior behavior).
    if bs_asof_date:
        bs_ref, bs_filed_at = _find_filing_for_bs_asof(ticker, bs_asof_date)
        if not bs_ref:
            bs_ref, bs_filed_at = fy_ref, fy_filed_at
        bs_period_iso = bs_asof_date[:10]
    else:
        bs_ref, bs_filed_at = fy_ref, fy_filed_at
        bs_period_iso = None  # use fy_end_iso below
    # Three-way ref split, intentional and load-bearing:
    #   fy_ref    -- the FY 10-K. Used for all income-statement pulls
    #                (revenue / op_income / D&A / COGS / interest /
    #                operating-lease cost) so they reflect the full
    #                FY span, not a mid-fiscal-year quarterly slice.
    #   bs_ref    -- the filing whose balance sheet covers
    #                ``bs_asof_date`` (or fy_ref when not supplied).
    #                Already resolved upstream.
    #   mkt_ref   -- the filing knowable on ``asof_market_price`` whose
    #                cover-page share count drives market_cap. Only
    #                used by ``_market_cap_at`` / ``_extract_bvps``.
    # Before the split, ``ref`` was reassigned to ``mkt_ref`` here and
    # every income-statement pull below silently hit the wrong filing
    # (a Q1 10-Q for asof 2 months past FY-end → $2.37B "revenue"
    # instead of LULU's $10.6B FY2024 actual; row-11 LULU/VSCO bug).
    ref = fy_ref
    filed_at = fy_filed_at
    fy_end_iso = _fy_period_end_date(fy, ticker)
    if bs_period_iso is None:
        bs_period_iso = fy_end_iso
    asof = asof_market_price or fy_end_iso
    mkt_ref, mkt_filed_at = _find_filing_ref_for_asof(ticker, fy, asof)
    if not mkt_ref:
        # The asof-driven ref is only required for market-cap /
        # cover-page lookups. Fall back to the FY 10-K -- its cover
        # page carries a share count too (will be the FY-end snapshot,
        # not the asof snapshot, which is fine when asof is near FY-end
        # or no closer filing exists).
        mkt_ref, mkt_filed_at = fy_ref, fy_filed_at

    # Prefer parent-only equity (canonical for P/B). The
    # ``IncludingPortionAttributableToNoncontrolling`` concept name
    # is a substring match that on some issuers (FTAI, AER) returns
    # a sub-segment or NCL-only fact rather than total equity, which
    # makes P/B explode (e.g. FTAI returned $1M instead of $125M).
    # Order: parent-only → total. Sanity-floor handled by the
    # downstream ratio computation (None vs absurd value).
    # Order: parent-only US-GAAP equity → consolidated → partnership /
    # LP / member equity (partnership issuers like FTAI, BX-managed
    # vehicles) → IFRS equity (foreign private issuers filing 20-F).
    # Substring match on the concept name, so the IFRS variant catches
    # both ``Equity`` (plain) and ``EquityAttributableToOwnersOfParent``
    # without listing every IFRS extension explicitly.
    equity = _fy_end_period_fact(bs_ref, (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrolling",
        "PartnersCapital",
        "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
        "LimitedPartnersCapitalAccount",
        "MembersEquity",
        "EquityAttributableToOwnersOfParent",
        "Equity",
    ), bs_period_iso)
    # FINANCIAL debt only (exclude operating leases — those are
    # tracked separately below). The rubric convention for leverage
    # ratios is financial-debt-only across all 27 v2 rows.
    lt_debt_nc = _fy_end_period_fact(bs_ref, (
        "LongTermDebtNoncurrent", "LongTermDebt",
    ), bs_period_iso) or 0.0
    lt_debt_c = _fy_end_period_fact(bs_ref, ("LongTermDebtCurrent",), bs_period_iso) or 0.0
    st_borrow = _fy_end_period_fact(bs_ref, (
        "ShortTermBorrowings", "CommercialPaper",
    ), bs_period_iso) or 0.0
    debt_total = lt_debt_nc + lt_debt_c + st_borrow
    # Lease liabilities tracked separately. Sum current + non-current
    # operating lease liabilities. Aircraft / retail issuers
    # frequently have $10B+ here.
    op_lease_nc = _fy_end_period_fact(bs_ref, (
        "OperatingLeaseLiabilityNoncurrent",
    ), bs_period_iso) or 0.0
    op_lease_c = _fy_end_period_fact(bs_ref, (
        "OperatingLeaseLiabilityCurrent",
    ), bs_period_iso) or 0.0
    lease_liability_total = op_lease_nc + op_lease_c

    cash = _fy_end_period_fact(bs_ref, (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
    ), bs_period_iso)
    # Short-term investments / marketable securities — standard
    # equity-research EV-deduction convention treats these as
    # cash-equivalent liquid assets that should net against debt
    # alongside cash. Issuers with material excess-cash positions
    # (LULU, AAPL, MSFT, GOOGL) carry billions in ST investments;
    # ignoring them inflates EV by 1-5%. The pull tries the four
    # most-common concept names; falls back to 0 for issuers that
    # don't have any (retail, casinos, leasing).
    st_investments = _fy_end_period_fact(bs_ref, (
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesCurrent",
        "InvestmentsCurrent",
        "AvailableForSaleSecuritiesDebtSecurities",
        "AvailableForSaleSecurities",
        "MarketableSecurities",
        "OtherShortTermInvestments",
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterest",
        "EquitySecuritiesFvNi",
    ), bs_period_iso) or 0.0
    if cash is not None:
        cash = cash + st_investments
    # Income-statement items use FULL-FY period filtering (period
    # spans 11-13 months ending on FY-end). Without this filter,
    # _max_positive_xbrl picks segment-scope or comparative-period
    # values — diagnosed during row 11 VSCO where OperatingIncomeLoss
    # max returned $3.74B (impossibly high) because the substring
    # match also caught Cost-of-Goods-Sold-related concepts and
    # multi-period sums.
    revenue = _fy_full_period_fact(ref, (
        "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
    ), fy_end_iso)
    op_income = _fy_full_period_fact(ref, (
        "OperatingIncomeLoss",
    ), fy_end_iso)
    da = _fy_full_period_fact(ref, (
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ), fy_end_iso) or 0.0
    ebitda = None
    if op_income is not None:
        ebitda = op_income + da
        # Sanity floor: if EBITDA exceeds revenue, the period match
        # picked the wrong fact. Suppress to None so the ratio
        # returns ``n/a`` instead of an absurd value.
        if revenue is not None and ebitda > revenue:
            ebitda = None
    cogs = _fy_full_period_fact(ref, (
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
    ), fy_end_iso)
    # Inventory is a balance-sheet item -- use the BS ref / BS period
    # date (same as equity / debt / cash above), not the IS ref. When
    # ``bs_asof_date`` is set this picks the correct interim snapshot;
    # otherwise both default to fy_ref + fy_end_iso.
    inventory_end = _fy_end_period_fact(bs_ref, ("InventoryNet",), bs_period_iso)
    # FCCR + EBITDAR inputs.
    operating_lease_cost = _fy_full_period_fact(ref, (
        "OperatingLeaseCost", "LeaseAndRentalExpense",
        "OperatingLeaseExpense",
    ), fy_end_iso)
    interest_expense = _fy_full_period_fact(ref, (
        "InterestExpense",
        "InterestExpenseDebt",
    ), fy_end_iso)
    # Interest income — paired with InterestExpense for the NET
    # interest convention. Most credit-analysis fixed-charge
    # coverage rubrics use net interest (gross interest minus
    # interest / investment income) in the denominator; treating
    # the gross figure as fixed charges over-states the obligation
    # by the amount the issuer earns on its own cash balance.
    # Generic across any issuer; large-cap retailers / industrials
    # routinely earn $100M+ of interest income on cash and money-
    # market positions.
    interest_income = _fy_full_period_fact(ref, (
        "InterestIncomeOperating",
        "InvestmentIncomeInterest",
        "InterestAndDividendIncomeOperating",
        "InterestIncome",
        "InterestAndOtherIncome",
        "OtherInterestAndDividendIncome",
        "InterestIncomeFromShortTermInvestments",
        "InterestIncomeAndOtherIncome",
        # Retailers / industrials often bundle interest income into
        # a broader non-operating-income line; pull that too as
        # a last resort. The downstream max() guard ensures a
        # legitimately-large "other income" doesn't drive net
        # interest below zero (would mask debt-service obligations).
        "NonoperatingIncomeExpense",
    ), fy_end_iso) or 0.0
    # Text-mode fallback for interest income. Some issuers (e.g. TGT)
    # tag their cash-yield income with a custom extension concept
    # or aggregate it into a broader line ('net interest expense',
    # 'other income, net') that doesn't match any of the standard
    # us-gaap names above. When XBRL returns 0, scan the income-
    # statement table for an explicit 'interest income' / 'investment
    # income' label adjacent to a dollar amount. Generic across any
    # issuer whose XBRL tagging diverges from us-gaap standards.
    if interest_income <= 0:
        try:
            env_is = _do_extract_filing_tables(
                ref=_strip_chunk_suffix(ref),
                table_keyword="interest", item="8", limit=10,
            )
            rows_is = _table_rows_from_extract(env_is)
            interest_income_phrases = (
                "interest income",
                "investment income",
                "interest and other income",
                "interest and dividend income",
                "other interest and",
                "income from short-term investments",
            )
            candidates_ii: list[float] = []
            for row in rows_is:
                lbl = (row.get("label") or "").strip().lower()
                if not any(p in lbl for p in interest_income_phrases):
                    continue
                # Skip if the row label also contains "expense" (might
                # be a net-interest line where the dollar represents
                # the net expense, not pure income).
                if "expense" in lbl and "net" not in lbl:
                    continue
                for c in (row.get("cells") or []):
                    n = _parse_num(c)
                    if n is not None and n > 0:
                        candidates_ii.append(n)
                        break
            if candidates_ii:
                interest_income = max(candidates_ii)
        except Exception:  # noqa: BLE001
            pass
    interest_expense_net = None
    if interest_expense is not None:
        interest_expense_net = max(0.0, float(interest_expense) - float(interest_income))
    ebitdar = None
    if ebitda is not None and operating_lease_cost is not None:
        ebitdar = ebitda + operating_lease_cost
    # Pair shares with the BS-snapshot date (defaults to fy_end_iso
    # when no bs_asof_date override is supplied). Closes the ~1-3%
    # EV/EBITDA precision wedge from cover-page filing-date drift.
    market_cap = _market_cap_at(ticker, asof, mkt_ref, bs_period_iso=bs_period_iso)
    close_price = _close_price_at(ticker, asof)
    # Issuer-disclosed BVPS lives in the annual report (MD&A / financial
    # tables); interim 10-Qs rarely restate it. Scan fy_ref, not mkt_ref.
    bvps_reported = _extract_bvps_from_filing(fy_ref)
    # Return the FY 10-K ref/filed_at -- that's where the
    # income-statement primitives the caller will quote came from.
    # mkt_ref/bs_ref are internal; downstream answer formatting
    # ("FY2024 financials from 10-K filed <date>") is correct
    # only when this points at fy_ref.
    return {
        "ticker": ticker, "fy": fy, "filed_at": fy_filed_at, "ref": fy_ref,
        "fy_end_iso": fy_end_iso, "asof": asof,
        "equity": equity, "debt_total": debt_total,
        "lease_liability_total": lease_liability_total,
        "cash": cash,
        "revenue": revenue, "ebitda": ebitda, "ebitdar": ebitdar,
        "cogs": cogs, "inventory_end": inventory_end,
        "operating_lease_cost": operating_lease_cost,
        "interest_expense": interest_expense,
        "interest_income": interest_income,
        "interest_expense_net": interest_expense_net,
        "market_cap": market_cap,
        "close_price": close_price,
        "bvps_reported": bvps_reported,
    }


def _compute_one_ratio(ratio: str, m: Mapping[str, Any]) -> Optional[float]:
    eq = m.get("equity"); debt = m.get("debt_total") or 0.0
    cash = m.get("cash") or 0.0; rev = m.get("revenue")
    ebitda = m.get("ebitda"); cogs = m.get("cogs")
    inv = m.get("inventory_end"); mc = m.get("market_cap")
    ebitdar = m.get("ebitdar")
    rent = m.get("operating_lease_cost") or 0.0
    interest = m.get("interest_expense") or 0.0
    lease_liab = m.get("lease_liability_total") or 0.0
    if ratio == "p_to_b":
        # Prefer price ÷ issuer-disclosed BVPS when the 10-K / 20-F
        # publishes one. Aircraft lessors (AER), banks, REITs, and
        # insurers report a per-share book value that bakes in
        # treasury-stock netting, unvested-restricted exclusion, or
        # other issuer-specific conventions — analyst rubrics usually
        # follow the issuer's published metric. Falls back to MC ÷
        # equity when no BVPS is disclosed (FTAI, WLFC, retail, etc.).
        bvps = m.get("bvps_reported")
        close = m.get("close_price")
        if bvps and close:
            return close / bvps
        return (mc / eq) if (mc and eq) else None
    if ratio == "d_to_e":
        return (debt / eq) if (eq and debt > 0) else None
    if ratio == "d_to_tc":
        denom = (debt + eq) if (eq is not None) else None
        return (debt / denom) if (denom and denom > 0 and debt > 0) else None
    if ratio == "ebitda_margin":
        return (ebitda / rev) if (ebitda and rev) else None
    if ratio == "ebitdar_margin":
        return (ebitdar / rev) if (ebitdar and rev) else None
    if ratio == "ev_ebitda":
        # EV = Market Cap + Financial Debt + Operating Lease Liability − Cash.
        # Including operating leases as a debt-like item matches modern
        # equity-research convention and the v2 row-11 rubric ("include
        # operating leases as debt-like items"). For issuers with zero
        # lease liability (financial / insurance / pure-software), the
        # term is 0 and the formula collapses to the classic
        # (MC + Debt − Cash) form, so v1 rows that use ev_ebitda see
        # no change.
        ev = (mc or 0) + debt + lease_liab - cash
        return (ev / ebitda) if (ebitda and ev > 0) else None
    if ratio == "dio":
        return ((inv / cogs) * 365.0) if (inv and cogs) else None
    if ratio == "fccr":
        # FCCR = EBITDAR / (Net Interest + Rent). Credit-analysis
        # rubrics use NET interest (gross interest expense minus
        # interest income / investment income) — gross interest
        # double-counts cash-yield offsets the issuer already earns
        # and over-states fixed charges by 5-25% for cash-rich
        # large-caps. Falls back to gross interest when net is
        # absent (issuers without interest-income tagging).
        net_interest = m.get("interest_expense_net")
        if net_interest is not None and net_interest > 0:
            interest_for_fccr = float(net_interest)
        else:
            interest_for_fccr = float(interest or 0.0)
        if ebitdar is None or interest_for_fccr <= 0 or rent <= 0:
            return None
        denom = interest_for_fccr + rent
        return (ebitdar / denom) if denom > 0 else None
    return None


def _format_ratio(ratio: str, v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    if ratio in ("p_to_b", "d_to_e", "ev_ebitda", "fccr"):
        return f"{v:.2f}x"
    if ratio == "d_to_tc":
        return f"{v * 100:.1f}%"
    if ratio in ("ebitda_margin", "ebitdar_margin"):
        return f"{v * 100:.2f}%"
    if ratio == "dio":
        return f"{v:.2f} days"
    return f"{v:.4f}"







def _table_rows_from_extract(env: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten extract_filing_tables output into a list of {label, cells}."""
    out: list[dict[str, Any]] = []
    for tbl in (env.get("tables") or [])[:10]:
        markdown = tbl.get("markdown") or ""
        for line in markdown.splitlines():
            line = line.strip().lstrip("|").rstrip("|")
            if not line or set(line) <= set("-|: "):
                continue
            cells = [c.strip() for c in line.split("|")]
            if not cells or not cells[0]:
                continue
            out.append({"label": cells[0], "cells": cells[1:]})
    return out


def _parse_num(cell: str) -> Optional[float]:
    """Parse 'in millions' / '$1,234.5' / '(123)' / '12.3%' style cells."""
    if not cell or not isinstance(cell, str):
        return None
    s = cell.strip().replace("$", "").replace(",", "").replace("%", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v







def _coerce_to_float_list(value: Any) -> Optional[list[float]]:
    """Defensive list-of-floats parser for tool args.

    LLMs may pass arrays as JSON-stringified arrays or
    comma-separated strings. Returns ``None`` on parse failure.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        out: list[float] = []
        for v in value:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                return None
        return out
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [float(v) for v in parsed]
            except (json.JSONDecodeError, ValueError, TypeError):
                return None
        if "," in stripped:
            try:
                return [float(p.strip()) for p in stripped.split(",") if p.strip()]
            except ValueError:
                return None
        try:
            return [float(stripped)]
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return [float(value)]
    return None


def _coerce_to_int_list(value: Any) -> Optional[list[int]]:
    fl = _coerce_to_float_list(value)
    if fl is None:
        return None
    return [int(v) for v in fl]











# --------------------------------------------------------------------------
# compute_price_returns_multi -- fan get_equity_bars across N tickers
# --------------------------------------------------------------------------
#
# Why this exists. Vals AI v2 row 8 (and the user-reported live run
# 6b7095b5-5fb9-4908-be97-6d7e2d1c86a8) showed the same pattern: a
# 3-ticker × 2-date price-return question burned `stock_analyst`'s
# 7-round max-step budget on 16 per-ticker `get_equity_bars` calls
# that mostly returned empty (wrong start/end format guesses,
# per-ticker retries on bad arg shape). Specialist then reported
# "the tools do not include price data" — incorrect, the tool does,
# but the model used it wrong. The data is present in equity_bars_v1
# (verified via direct probe: NOW/HUBS/TOST/AAPL/SPY all have bars
# through 2026-02-27 with 252 trading days for 2024).
#
# Same plateau-resolution pattern as compute_payout_ratio_peers:
# encode the multi-ticker fan-out in code so the model makes ONE
# call instead of N×M per-cell calls.
# Returns the start_close / end_close / pct_return matrix + per-
# end-date ranking. Supports the SUI-row shape (multi-horizon)
# via `end_dates: list[str]` rather than forcing a single end date.

def _snap_to_nearest_trading_close(
    rows: Sequence[Mapping[str, Any]],
    target_iso: str,
    *, direction: str = "backward",
) -> tuple[Optional[float], Optional[str]]:
    """Return (close, actual_trading_date_used).

    direction="backward": snap to the most-recent trading day on
    or before ``target_iso`` (used for the start-of-window price).
    direction="forward": snap to the next trading day on or after
    ``target_iso`` (rare; explicit non-trading-day handling).
    direction="exact_or_backward": prefer exact match; fall back
    to most-recent prior trading close. This is the default for
    end-of-window prices when the user names a specific date.
    """
    if not rows:
        return None, None
    target = target_iso[:10]
    # Exact match wins.
    for r in rows:
        d = str(r.get("date") or "")[:10]
        if d == target:
            try:
                c = float(r.get("close") or 0)
            except (TypeError, ValueError):
                continue
            if c > 0:
                return c, d
    # Backward fallback: walk newest-to-oldest, return the first
    # row whose date <= target.
    if direction in ("backward", "exact_or_backward"):
        for r in reversed(rows):
            d = str(r.get("date") or "")[:10]
            if d <= target:
                try:
                    c = float(r.get("close") or 0)
                except (TypeError, ValueError):
                    continue
                if c > 0:
                    return c, d
    # Forward fallback: walk oldest-to-newest, first row >= target.
    if direction == "forward":
        for r in rows:
            d = str(r.get("date") or "")[:10]
            if d >= target:
                try:
                    c = float(r.get("close") or 0)
                except (TypeError, ValueError):
                    continue
                if c > 0:
                    return c, d
    return None, None


def _coerce_to_str_list(value: Any) -> Optional[list[str]]:
    """Defensive list-of-strings parser for tool args.

    LLMs frequently pass a JSON-encoded array as a single string
    when the schema uses ``oneOf``. ``json.loads`` a stringified
    array; pass through real lists; wrap a single string as a
    one-element list. Returns ``None`` on parse failure so the
    caller can emit a clean error.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        # Try JSON-encoded array first.
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
            except (json.JSONDecodeError, ValueError):
                pass
        # Fall back to comma-split (catches "a, b, c" shapes).
        if "," in stripped:
            return [
                p.strip()
                for p in stripped.split(",")
                if p.strip()
            ]
        # Single value.
        return [stripped] if stripped else []
    return None


def _spearman_rank_correlation(
    a_values: Sequence[float], b_values: Sequence[float],
) -> Optional[float]:
    """Compute Spearman's ρ between two equal-length numeric vectors.

    Returns None when fewer than 3 paired values or zero variance.
    Uses average rank for ties.
    """
    if len(a_values) != len(b_values) or len(a_values) < 3:
        return None

    def _rank(vals: Sequence[float]) -> list[float]:
        indexed = sorted(enumerate(vals), key=lambda kv: kv[1])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(indexed):
            j = i
            while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
                j += 1
            avg = (i + j + 2) / 2.0  # 1-indexed ranks
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = avg
            i = j + 1
        return ranks

    ra = _rank(list(a_values))
    rb = _rank(list(b_values))
    n = len(ra)
    mean_a = sum(ra) / n
    mean_b = sum(rb) / n
    num = sum((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(n))
    den_a = sum((ra[i] - mean_a) ** 2 for i in range(n)) ** 0.5
    den_b = sum((rb[i] - mean_b) ** 2 for i in range(n)) ** 0.5
    if den_a == 0 or den_b == 0:
        return None
    return num / (den_a * den_b)


def _do_compute_price_returns_multi(
    tickers: Any,
    start_date: str,
    end_dates: Any,
    paired_metric: Optional[Mapping[str, Any]] = None,
    paired_metric_label: Optional[str] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Fan ``get_equity_bars`` across N tickers; compute % return per (ticker, end_date).

    Parameters
    ----------
    tickers
        List of US-listed ticker symbols (accepted as a real list,
        a JSON-stringified list, or a comma-separated string).
    start_date
        ISO ``YYYY-MM-DD`` start-of-window date. If non-trading,
        snaps to the most-recent prior trading close (the convention
        the v2 rubric uses for "from December 31").
    end_dates
        ISO date string OR list of ISO date strings (for multi-
        horizon analyses like the SUI 1/14/30-day question). Also
        accepts JSON-stringified arrays / comma-separated strings
        defensively. If non-trading, snaps to the most-recent
        prior trading close.

    Returns one row per (ticker, end_date) combo with
    ``start_close``, ``end_close``, ``abs_change``, ``pct_change``
    plus a ranked summary block.
    """
    tickers_list = _coerce_to_str_list(tickers)
    if not tickers_list:
        return {"error": "tickers list required (got nothing parseable)"}
    if not start_date or not isinstance(start_date, str):
        return {"error": "start_date (YYYY-MM-DD) required"}
    end_dates_list = _coerce_to_str_list(end_dates)
    if not end_dates_list:
        return {"error": "end_dates must be a YYYY-MM-DD string, list of strings, or JSON-encoded array"}

    # Fetch a single big window per ticker that covers
    # [start_date - 7d, max(end_dates) + 1d]. Avoids one S3 round-trip
    # per (ticker, end_date) pair.
    try:
        from datetime import datetime, timedelta
        start_d = datetime.fromisoformat(start_date[:10])
        end_max = max(
            datetime.fromisoformat(e[:10]) for e in end_dates_list
        )
        query_start = (start_d - timedelta(days=10)).strftime("%Y-%m-%d")
        query_end = (end_max + timedelta(days=2)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return {"error": "start_date or end_dates have invalid YYYY-MM-DD format"}

    per_ticker_rows: dict[str, list[Mapping[str, Any]]] = {}
    per_ticker_err: dict[str, str] = {}
    for raw in tickers_list:
        t = str(raw).strip().upper()
        if not t:
            continue
        env = _do_get_equity_bars(t, start=query_start, end=query_end)
        rows = env.get("rows") or []
        per_ticker_rows[t] = rows
        if not rows:
            per_ticker_err[t] = (
                f"No equity_bars data for {t} in {query_start}..{query_end}. "
                f"Check ticker symbol and date window."
            )

    # Build result matrix.
    rows_out: list[dict[str, Any]] = []
    for t, rows in per_ticker_rows.items():
        if per_ticker_err.get(t):
            for end in end_dates_list:
                rows_out.append({
                    "ticker": t, "end_date": end,
                    "error": per_ticker_err[t],
                })
            continue
        s_close, s_date = _snap_to_nearest_trading_close(
            rows, start_date, direction="exact_or_backward",
        )
        for end in end_dates_list:
            e_close, e_date = _snap_to_nearest_trading_close(
                rows, end, direction="exact_or_backward",
            )
            if s_close is None or e_close is None:
                rows_out.append({
                    "ticker": t, "end_date": end,
                    "start_close": s_close, "end_close": e_close,
                    "error": (
                        f"Could not snap to a trading close for "
                        f"start={start_date} (got {s_date}) or end={end} (got {e_date})."
                    ),
                })
                continue
            abs_ch = e_close - s_close
            pct_ch = (abs_ch / s_close) * 100.0 if s_close else None
            rows_out.append({
                "ticker": t, "end_date": end,
                "start_close": s_close, "start_trading_date": s_date,
                "end_close": e_close, "end_trading_date": e_date,
                "abs_change": abs_ch, "pct_change": pct_ch,
            })

    # Markdown summary block — table + per-end-date ranking.
    lines = [
        f"# Price Returns: start={start_date}, end={', '.join(end_dates_list)}",
        "",
        "| Ticker | End Date | Start Close | End Close | Δ | Δ% |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows_out:
        if row.get("error"):
            lines.append(
                f"| {row['ticker']} | {row['end_date']} | — | — | — | "
                f"_{row['error'][:80]}_ |"
            )
            continue
        lines.append(
            "| {t} | {end} | ${sc:.2f} | ${ec:.2f} | ${ac:+.2f} | {pc:+.2f}% |".format(
                t=row["ticker"], end=row["end_date"],
                sc=row["start_close"], ec=row["end_close"],
                ac=row["abs_change"], pc=row["pct_change"],
            )
        )
    for end in end_dates_list:
        cohort = [
            r for r in rows_out
            if r.get("end_date") == end and r.get("pct_change") is not None
        ]
        if not cohort:
            continue
        # Ranking: descending pct_change (best performer first).
        ranked = sorted(cohort, key=lambda x: -x["pct_change"])
        lines.append("")
        lines.append(
            f"### Ranking by Δ% return as of {end} (best → worst):"
        )
        for r in ranked:
            lines.append(f"- {r['ticker']}: {r['pct_change']:+.2f}%")

    # Rank-correlation block — emitted when ``paired_metric`` is
    # supplied. Lets rank-correlation questions (v2 rows 7, 8) bake
    # the Spearman conclusion line into the tool's verbatim quote
    # so the model doesn't have to derive it.
    paired = None
    if isinstance(paired_metric, dict) and paired_metric:
        # Normalize: ticker-keyed → float values.
        paired = {}
        for t_raw, v_raw in paired_metric.items():
            t_up = str(t_raw).strip().upper()
            try:
                paired[t_up] = float(v_raw)
            except (TypeError, ValueError):
                continue
    if paired and len(end_dates_list) >= 1:
        last_end = end_dates_list[-1]
        # Use the last end-date's returns for correlation.
        returns_by_ticker = {
            r["ticker"]: r["pct_change"]
            for r in rows_out
            if r.get("end_date") == last_end and r.get("pct_change") is not None
        }
        common = [t for t in returns_by_ticker if t in paired]
        if len(common) >= 3:
            ret_vec = [returns_by_ticker[t] for t in common]
            paired_vec = [paired[t] for t in common]
            rho = _spearman_rank_correlation(ret_vec, paired_vec)
            ret_rank = sorted(common, key=lambda t: -returns_by_ticker[t])
            paired_rank = sorted(common, key=lambda t: -paired[t])
            same_order = ret_rank == paired_rank
            label = paired_metric_label or "secondary metric"
            lines.append("")
            lines.append(
                f"### Rank-Order Correlation: Δ% return vs {label}"
            )
            lines.append(
                f"- Ranked by Δ% return (best→worst): {', '.join(ret_rank)}"
            )
            lines.append(
                f"- Ranked by {label} (highest→lowest): {', '.join(paired_rank)}"
            )
            if rho is not None:
                lines.append(
                    f"- **Spearman ρ = {rho:+.4f}** "
                    f"(positive → same rank order; negative → inverted; near 0 → no correlation)"
                )
            verdict = "SAME rank order" if same_order else "DIFFERENT rank order"
            lines.append(
                f"- **Rank-order conclusion: {verdict}.** "
                f"The Δ% return ranking {'matches' if same_order else 'does NOT match'} "
                f"the {label} ranking."
            )
    answer_summary_block = "\n".join(lines)

    out_payload = {
        "tickers": [t.upper() for t in tickers_list],
        "start_date": start_date,
        "end_dates": end_dates_list,
        "rows": rows_out,
        "answer_summary_block": answer_summary_block,
    }
    if paired:
        out_payload["paired_metric"] = paired
        out_payload["paired_metric_label"] = paired_metric_label
    return _apply_binding(bind_as, out_payload)


COMPUTE_PRICE_RETURNS_MULTI = Tool(
    name="compute_price_returns_multi",
    description=(
        "Fan get_equity_bars across N US-listed tickers and compute "
        "% return per (ticker, end_date) pair. USE THIS for any "
        "'compare price performance / share-price return / Y-day "
        "reaction across N tickers' question — the tool handles the "
        "fan-out + ranking in one call, replacing the 6+ per-ticker / "
        "per-date get_equity_bars calls the model would otherwise "
        "need (which historically burn stock_analyst's round budget "
        "before the matrix is filled). Snaps non-trading start / end "
        "dates to the most-recent prior trading close. Supports "
        "multi-horizon (`end_dates` as a list) for 1/14/30-day "
        "event-reaction questions. Returns `answer_summary_block` "
        "ready to drop verbatim — includes the price matrix table + "
        "per-end-date ranking lines."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of US-listed ticker symbols.",
            },
            "start_date": {
                "type": "string",
                "description": (
                    "Start-of-window date in YYYY-MM-DD. Snaps to the "
                    "most-recent prior trading close when non-trading."
                ),
            },
            "end_dates": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of end-of-window dates in YYYY-MM-DD. For a "
                    "single end date pass a 1-element list (e.g. "
                    "[\"2026-02-27\"]). For multi-horizon analyses "
                    "(e.g. SUI 1/14/30-day reaction: pass "
                    "[\"2025-07-22\", \"2025-08-04\", \"2025-08-20\"]). "
                    "Each non-trading end date snaps to the most-recent "
                    "prior trading close. The tool also accepts a "
                    "JSON-encoded array string defensively, but pass "
                    "a real array whenever possible."
                ),
            },
            "paired_metric": {
                "type": "object",
                "description": (
                    "OPTIONAL — supply for rank-correlation questions. "
                    "Map of {ticker: numeric_value} that pairs each "
                    "ticker with the secondary metric the question asks "
                    "you to rank-correlate against (e.g. FY2025 revenue "
                    "growth %, EBITDA margin, etc.). When supplied (and "
                    "≥3 tickers overlap), the tool computes Spearman ρ "
                    "between Δ% return at the LAST end_date and the "
                    "paired metric, emits both rankings + a "
                    "'SAME / DIFFERENT rank order' conclusion line in "
                    "`answer_summary_block`. Critical for v2 rows that "
                    "grade a rank-order verdict alongside the matrix."
                ),
            },
            "paired_metric_label": {
                "type": "string",
                "description": (
                    "OPTIONAL — human-readable label for the paired "
                    "metric (e.g. 'FY2025 revenue growth %'). Used in "
                    "the answer_summary_block rank-correlation block. "
                    "Omit if no paired_metric supplied."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["tickers", "start_date", "end_dates"],
    },
    fn=_do_compute_price_returns_multi,
)



_RESTRUCTURING_KEYWORDS = (
    "Restructuring", "Restructuring and other charges",
    "Restructuring liability", "Restructuring plan",
)
_HEADCOUNT_KEYWORDS = (
    "employees", "headcount", "workforce", "Human capital",
)


def _parse_restructuring_categories(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bucket parsed-table rows into severance / litigation / impairment / other.

    Heuristic match on the label column. Returns a dict with named
    buckets, plus the original row list for downstream inspection.

    Label normalization is intentionally broad: issuers use
    inconsistent phrasings for the same sub-category
    ("Employee severance" / "Severance and benefits" / "Workforce
    reduction" / "Severance and related charges" all denote the same
    bucket; "Asset impairment" / "Impairment of long-lived assets" /
    "Impairment charges" all denote impairment; "Litigation charges
    and other" / "Other charges" with a litigation-context sibling
    row both denote the litigation bucket). The bucket router below
    fires on ANY of the recognized phrasings; ordering of `if`
    branches resolves ambiguities (impairment first so an impairment
    row that also mentions "other" lands in impairment, not litigation).
    """
    buckets: dict[str, list[tuple[str, Optional[float]]]] = {
        "severance_and_benefits": [],
        "litigation_and_other": [],
        "asset_impairment": [],
        "ending_accrued": [],
        "total_restructuring": [],
        "cumulative_plan_cost": [],
    }
    # Phrase clusters per bucket -- generic across issuers.
    severance_phrases = (
        "severance", "employee severance", "benefits", "termination",
        "workforce reduction", "workforce realignment",
        "severance and related", "severance-related",
        "employee separation", "personnel reduction",
    )
    impairment_phrases = (
        "impairment", "asset impairment", "asset write-down",
        "asset write down", "asset writeoff", "asset writedown",
        "impairment of long-lived", "impairment charge",
        "goodwill impairment", "intangible impairment",
    )
    litigation_phrases = (
        "litigation", "litigation and other", "legal settlement",
        "legal accrual", "settlement charge",
    )
    ending_phrases = (
        "ending balance", "ending accrued", "balance, end",
        "balance at end", "end of period", "end of year",
        "accrual balance",
    )
    total_phrases = (
        "total restructuring", "restructuring and other charges",
        "total charges", "total restructuring charges",
        "total restructuring expense",
    )
    cumulative_phrases = (
        "cumulative", "inception-to-date", "since inception",
        "life-to-date", "inception to date",
    )
    for row in rows:
        label = (row.get("label") or "").strip().lower()
        cells = row.get("cells") or []
        first_num = None
        for c in cells:
            n = _parse_num(c)
            if n is not None:
                first_num = n
                break
        if first_num is None or first_num <= 0:
            continue
        # Order matters: check impairment + total BEFORE severance/
        # litigation/etc. so labels like "Asset impairment charges and
        # other" land in the impairment bucket rather than litigation.
        if any(k in label for k in impairment_phrases):
            buckets["asset_impairment"].append((row.get("label"), first_num))
        elif any(k in label for k in total_phrases):
            buckets["total_restructuring"].append((row.get("label"), first_num))
        elif any(k in label for k in severance_phrases):
            buckets["severance_and_benefits"].append((row.get("label"), first_num))
        elif any(k in label for k in litigation_phrases):
            buckets["litigation_and_other"].append((row.get("label"), first_num))
        elif any(k in label for k in ending_phrases):
            buckets["ending_accrued"].append((row.get("label"), first_num))
        elif any(k in label for k in cumulative_phrases):
            buckets["cumulative_plan_cost"].append((row.get("label"), first_num))
    return buckets


def _parse_restructuring_from_text(text: str) -> dict[str, list[tuple[str, Optional[float]]]]:
    """Text-mode fallback for restructuring categorization.

    Used when `extract_filing_tables` can't parse the issuer's HTML
    (sec-api Extractor returns 0 tables) but `get_full_text` does
    return the substantive body via the EDGAR direct-fetch fallback.
    Regex-extracts `(category-label, dollar-amount)` pairs from prose
    that follows the patterns common in restructuring-note write-ups:

      "employee severance and related charges of $1,234 million"
      "asset impairment charges totaled $3,631 million"
      "litigation charges and other of $858 million"
      "total restructuring and other charges of $5,723 million"

    Buckets returned in the same shape as
    `_parse_restructuring_categories` so downstream consumers don't
    need to branch on the source path.
    """
    buckets: dict[str, list[tuple[str, Optional[float]]]] = {
        "severance_and_benefits": [],
        "litigation_and_other": [],
        "asset_impairment": [],
        "ending_accrued": [],
        "total_restructuring": [],
        "cumulative_plan_cost": [],
    }
    if not isinstance(text, str) or not text.strip():
        return buckets
    # Normalize whitespace so multi-line wrapping doesn't fragment
    # the regex matches.
    norm = re.sub(r"\s+", " ", text)

    # Dollar amount in millions / billions. Accepts $X,XXX or
    # $X,XXX million / $X.X billion. Returns canonical $ in millions.
    _amount_re = (
        r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|thousand|M|B|m|b)?\b"
    )

    def _to_millions(num_s: str, unit: Optional[str]) -> Optional[float]:
        try:
            v = float(num_s.replace(",", ""))
        except (TypeError, ValueError):
            return None
        unit_lc = (unit or "").strip().lower()
        if unit_lc in ("billion", "b"):
            return v * 1000.0
        if unit_lc in ("thousand",):
            return v / 1000.0
        # 'million' / 'M' / no-unit default → already in millions
        return v

    # Each bucket has a list of trigger phrases that anchor the
    # search; the helper scans for the phrase + a dollar amount
    # within a short window after it.
    patterns: dict[str, tuple[str, ...]] = {
        "severance_and_benefits": (
            r"(?:employee\s+)?severance(?:\s+and\s+(?:related|benefit)s?)?\s+(?:charges|costs|expenses?)?",
            r"workforce\s+(?:reduction|realignment)",
            r"employee\s+separation",
        ),
        "asset_impairment": (
            r"asset\s+impairment(?:\s+charges?)?",
            r"impairment\s+of\s+long-lived\s+assets",
            r"goodwill\s+impairment",
            r"intangible(?:\s+asset)?\s+impairment",
        ),
        "litigation_and_other": (
            r"litigation\s+(?:charges?|expenses?|accruals?|and\s+other)",
            r"legal\s+(?:settlement|accrual)",
        ),
        "total_restructuring": (
            r"total\s+restructuring(?:\s+and\s+other(?:\s+charges)?)?",
            r"restructuring\s+and\s+other\s+charges",
        ),
        "cumulative_plan_cost": (
            r"cumulative(?:\s+plan)?\s+(?:cost|charges)",
            r"inception[\s-]to[\s-]date",
            r"life[\s-]to[\s-]date",
        ),
    }
    # Income-statement context markers preferred over balance-sheet
    # context (reserves / liabilities / accrual roll-forwards). Rubrics
    # grade the income-statement CHARGES figures, not balance-sheet
    # carrying-amount reserves.
    income_statement_markers = (
        "charges", "expense", "expenses", "recorded", "recognized",
        "incurred",
    )
    balance_sheet_markers = (
        "reserve", "liability", "accrued", "balance",
        "carrying amount",
    )
    # "Total" / "consolidated" anchors push a candidate to the top
    # — when the issuer has separately disclosed both a sub-component
    # and a total (e.g. "asset impairment charges of $1,500 million"
    # in one paragraph and "total impairment charges of $3,631
    # million" later), the rubric typically grades the total.
    total_anchor_markers = (
        "total", "consolidated", "aggregate", "combined",
    )

    for bucket, phrases in patterns.items():
        all_candidates: list[tuple[str, float, int]] = []  # (label, amount, score)
        for phrase in phrases:
            # Phrase followed within ~80 chars by a dollar amount.
            full_re = (
                r"(?P<label>" + phrase + r")[^.$\n]{0,80}?" + _amount_re
            )
            for m in re.finditer(full_re, norm, re.IGNORECASE):
                amount = _to_millions(m.group(2), m.group(3))
                if amount is None or amount <= 0:
                    continue
                label_text = m.group("label").strip()
                # Score by surrounding context: +2 for IS markers,
                # -2 for balance-sheet markers. Look in ±100 chars
                # around the match.
                ctx_start = max(0, m.start() - 100)
                ctx_end = min(len(norm), m.end() + 100)
                ctx = norm[ctx_start:ctx_end].lower()
                score = 0
                if any(k in ctx for k in income_statement_markers):
                    score += 2
                if any(k in ctx for k in balance_sheet_markers):
                    score -= 2
                # "Total" / "consolidated" anchor close to the match
                # (within the ±100-char window) signals the rubric-
                # preferred total. Scope tight to the immediate
                # vicinity (±30 chars of the match) so a passing
                # mention of "total" elsewhere in the paragraph
                # doesn't false-positive.
                tight_start = max(0, m.start() - 30)
                tight_end = min(len(norm), m.end() + 30)
                tight_ctx = norm[tight_start:tight_end].lower()
                if any(k in tight_ctx for k in total_anchor_markers):
                    score += 3
                all_candidates.append((label_text, amount, score))
        if not all_candidates:
            continue
        # Disambiguation: prefer income-statement-context matches; among
        # those, prefer the LARGEST value (totals exceed sub-components
        # for impairment / litigation / total-restructuring; only
        # exception is cumulative-plan-cost which is itself a roll-up).
        # All-balance-sheet matches still emit but at the bottom of the
        # preference order.
        all_candidates.sort(key=lambda c: (c[2], c[1]), reverse=True)
        # Keep top 3 distinct values to expose multi-match without
        # flooding the bucket.
        seen_vals: set[float] = set()
        for label_text, amount, _score in all_candidates:
            if amount in seen_vals:
                continue
            buckets[bucket].append((label_text, amount))
            seen_vals.add(amount)
            if len(buckets[bucket]) >= 3:
                break
    return buckets











# --------------------------------------------------------------------------
# find_quarterly_earnings_8ks -- locate Q1-Q4 earnings 8-K pairs (guidance + actuals)
# --------------------------------------------------------------------------
#
# Why this exists. Multi-quarter beat-or-miss questions (e.g. "by how
# much did [issuer] beat/miss its non-GAAP gross profit guide in each
# of the last 4 quarters") require finding 5 separate 8-Ks: the Q4 of FY-1
# (which provides Q1 FY guidance), then each of Q1-Q4 FY (each provides
# the next quarter's guidance + the prior quarter's actuals). Without help
# the model burns 8-12 bm25_sec calls rediscovering the right filings, gets
# distracted by 10-K/10-Q hits, and frequently quotes guidance from the
# wrong quarter. This tool packages the 5 8-Ks in one call so the model can
# focus on extraction + arithmetic.
#
# Output shape: per-quarter pairs of (guidance_8k_ref, actuals_8k_ref) plus
# the raw list of 8-Ks found. Model then calls get_full_text on each ref
# (or extract_filing_tables) to pull guidance ranges and actual values, and
# uses run_python for the beat/miss math.

def _do_find_quarterly_earnings_8ks(
    ticker: str,
    fy: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Locate the 5 quarterly earnings 8-Ks (Q4 FY-1 + Q1-Q4 FY) for a ticker.

    Assumes calendar-year fiscal year. For non-Dec FY-ends (GIS June, ORCL
    May, etc.) shift the windows or use ``bm25_sec`` directly.

    Returns a per-quarter pairing of guidance and actuals 8-K refs:

      {
        "ticker": "AMD",
        "fy": 2024,
        "pairs": [
          {"quarter": "Q1 FY2024",
           "guidance_8k_ref": "sec:0000002488-24-000XXX:2.02",  # filed Q4 FY23
           "actuals_8k_ref":  "sec:0000002488-24-000YYY:2.02",  # filed Q1 FY24
           ...},
          ... (Q2, Q3, Q4)
        ],
        "raw_8ks_found": [...],  # the underlying 8-K records, with snippets
      }

    Workflow the model should follow after calling this tool:
      1. For each quarter pair, call ``get_full_text(guidance_8k_ref)`` to
         retrieve the prior-quarter 8-K body containing the next-quarter
         guidance.
      2. Call ``get_full_text(actuals_8k_ref)`` for the actual figures.
      3. Use ``run_python`` to compute beat/miss as
         ``(actual - guidance_midpoint) / guidance_midpoint * 100``.
         For "non-GAAP gross profit" questions specifically:
         ``guidance = revenue_midpoint * non_GAAP_margin_midpoint``.
    """
    if not isinstance(fy, int):
        try:
            fy = int(fy)
        except (TypeError, ValueError):
            return {
                "ticker": ticker,
                "error": f"fy must be an int (e.g. 2024), got {fy!r}",
                "pairs": [],
            }
    if not ticker or not isinstance(ticker, str):
        return {
            "ticker": ticker,
            "error": "ticker is required (e.g. any US-listed issuer that files quarterly earnings 8-Ks)",
            "pairs": [],
        }
    ticker = ticker.strip().upper()

    # Earnings 8-Ks for calendar-year filers typically land within 30-45 days
    # of period end. Windows are deliberately wide to catch slow filers and
    # one week of slippage on either side.
    quarterly_windows = [
        # (label, filed_at_gte, filed_at_lte, period_end_label)
        (f"Q4 FY{fy - 1}", f"{fy}-01-15", f"{fy}-03-15", f"Dec-{fy - 1}"),
        (f"Q1 FY{fy}",     f"{fy}-04-15", f"{fy}-06-15", f"Mar-{fy}"),
        (f"Q2 FY{fy}",     f"{fy}-07-15", f"{fy}-09-15", f"Jun-{fy}"),
        (f"Q3 FY{fy}",     f"{fy}-10-15", f"{fy}-12-15", f"Sep-{fy}"),
        (f"Q4 FY{fy}",     f"{fy + 1}-01-15", f"{fy + 1}-03-15", f"Dec-{fy}"),
    ]

    raw_8ks: list[dict[str, Any]] = []
    for label, gte, lte, period in quarterly_windows:
        env = _do_bm25_sec(
            query=f"{ticker} earnings revenue gross margin operating",
            k=5,
            fields=None,
            filters={
                "ticker": ticker,
                "form_type": "8-K",
                "item_key": "2.02",
                "filed_at_gte": gte,
                "filed_at_lte": lte,
            },
            sort=None,
            body_mode="snippet",
        )
        hits = env.get("hits") or []
        # Take the EARLIEST 8-K in the window — that's the earnings release;
        # later 8-Ks in the same window are usually exhibit re-filings.
        hits.sort(key=lambda h: str((h.get("source") or {}).get("filed_at", "")))
        if hits:
            best = hits[0]
            src = best.get("source") or {}
            raw_8ks.append({
                "quarter_label": label,
                "period_end": period,
                "filed_at": src.get("filed_at", ""),
                "ref": best.get("id", ""),
                "snippet": str(src.get("body", ""))[:400],
            })
        else:
            raw_8ks.append({
                "quarter_label": label,
                "period_end": period,
                "ref": None,
                "filed_at_window": [gte, lte],
                "note": (
                    f"No 8-K Item 2.02 found for {ticker} between {gte} and {lte}. "
                    "Quarter likely missing from index, or issuer uses non-Dec FY-end."
                ),
            })

    # Build per-quarter beat-or-miss pairs:
    # Q[N]'s guidance was published in Q[N-1]'s earnings 8-K; Q[N]'s actuals
    # are in Q[N]'s earnings 8-K. raw_8ks[0] = Q4 FY-1 (Q1 FY guidance source);
    # raw_8ks[1..4] = Q1..Q4 FY.
    pairs: list[dict[str, Any]] = []
    for i in range(1, 5):
        guidance = raw_8ks[i - 1]
        actuals = raw_8ks[i]
        pairs.append({
            "quarter": f"Q{i} FY{fy}",
            "guidance_8k_ref": guidance.get("ref"),
            "guidance_8k_filed_at": guidance.get("filed_at", guidance.get("filed_at_window", "")),
            "guidance_8k_period": guidance.get("period_end"),
            "actuals_8k_ref": actuals.get("ref"),
            "actuals_8k_filed_at": actuals.get("filed_at", actuals.get("filed_at_window", "")),
            "actuals_8k_period": actuals.get("period_end"),
        })

    found_count = sum(1 for p in pairs if p["guidance_8k_ref"] and p["actuals_8k_ref"])
    result = {
        "ticker": ticker,
        "fy": fy,
        "pairs": pairs,
        "raw_8ks_found": raw_8ks,
        "complete_pairs": found_count,
        "next_steps": (
            "For each pair: (1) get_full_text on guidance_8k_ref to find the "
            "next-quarter guidance section; (2) get_full_text on actuals_8k_ref "
            "for the actual figure; (3) run_python to compute beat/miss as "
            "round((actual - guidance_midpoint) / guidance_midpoint * 100, 1). "
            "For 'non-GAAP gross profit' specifically: guidance midpoint = "
            "revenue_midpoint * non_GAAP_gross_margin_midpoint, both extracted "
            "from the 'Outlook' / 'Forward-looking statements' section of the "
            "guidance 8-K."
        ),
    }
    return _apply_binding(bind_as, result)


FIND_QUARTERLY_EARNINGS_8KS = Tool(
    name="find_quarterly_earnings_8ks",
    description=(
        "Locate all quarterly earnings 8-Ks (Item 2.02) for a ticker across "
        "fiscal year FY in one call. Returns 5 8-Ks (Q4 FY-1 + Q1-Q4 FY) "
        "paired so you can see, for each quarter, which 8-K provides "
        "guidance and which 8-K provides actuals. Use this for multi-"
        "quarter beat-or-miss questions ('by how much did X beat/miss its "
        "Y guide in each of the last 4 quarters') instead of issuing 8-12 "
        "separate bm25_sec calls to rediscover the same filings. "
        "ASSUMES calendar-year (Dec 31) FY-end. For non-Dec FY-ends "
        "(GIS June, ORCL May, NKE May, COST early-Sep, retailers late-"
        "Jan), use bm25_sec with month-specific filed_at windows instead. "
        "After this tool returns the pairs, call get_full_text on each "
        "ref to read the guidance + actuals sections, then use run_python "
        "to compute beat/miss percentages."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Issuer ticker (US-listed, files quarterly earnings 8-Ks). Case-insensitive.",
            },
            "fy": {
                "type": "integer",
                "description": (
                    "Calendar year of the fiscal year being analyzed, "
                    "e.g. 2024 for FY2024 (Dec 31, 2024 year-end). The "
                    "tool searches for Q4 FY-1 + Q1-Q4 FY 8-Ks."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy"],
    },
    fn=_do_find_quarterly_earnings_8ks,
)


GET_FULL_TEXT = Tool(
    name="get_full_text",
    description=(
        "Retrieve the complete body of a document referenced by a "
        "search hit. Works for SEC filings (ref='sec:<accession>[:<item>]'), "
        "scraped articles (bare 32-char hex id from bm25_scraped_articles "
        "or vector_scraped_articles hits), and GDELT events. Use this to "
        "read full article text with specific figures (fine amounts, verdict "
        "dollars, deal terms) that BM25 snippets truncate. max_chars caps "
        "the returned body length (default 16000). Pass `bind_as=<name>` to "
        "ALSO make the doc available in run_python."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "max_chars": {
                "type": "integer", "default": 16_000,
                "minimum": 500, "maximum": 30_000,
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ref"],
    },
    fn=_do_get_full_text,
    bound_indices=(
        (GDELT_EVENTS_INDEX, ("get",)),
        (SEC_FILINGS_INDEX, ("get",)),
        (SCRAPED_ARTICLES_INDEX, ("get",)),
    ),
)


# --------------------------------------------------------------------------
# Knowledge graph
# --------------------------------------------------------------------------


def _do_query_graph(
    sql: Optional[str] = None,
    *,
    query: Optional[str] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """``query_graph`` is now a SELECT against the KG's parquet tables.

    The legacy implementation talked to a Trino-shaped HTTP endpoint
    with a hand-rolled timeout. The new world: the KG is registered
    as an index whose ``capabilities.sql.tables`` includes ``actor``,
    ``filing``, ``article``, ``mentions_org``, etc., backed by parquet
    files on S3. ``tools.sql`` runs DuckDB-over-parquet against the
    same data with stronger guarantees (validator rejects DDL /
    multi-statement input, gateway clamps row count + wall budget,
    rows come back JSON-shaped). The persona-supplied query language
    doesn't change -- DuckDB's SQL dialect is a near-superset of the
    Trino dialect the legacy queries used.

    Accepts ``query`` as an alias for ``sql`` because LLMs frequently
    infer the parameter name from the tool name ``query_graph`` and
    send ``query=`` instead of ``sql=``.
    """
    resolved_sql = sql or query
    if not resolved_sql:
        return {"error": "query_graph requires a 'sql' parameter (a SELECT statement)"}
    try:
        env = cb_tools.sql(index=GRAPH_INDEX, query=resolved_sql)
    except Exception as exc:  # noqa: BLE001 -- duckdb / kernel raise a zoo
        # Surface DuckDB / kernel error text to the LLM as a structured
        # envelope instead of letting the runtime's generic exception
        # wrapper produce an opaque ``{"error": "<ExceptionType>: ..."}``
        # blob. BinderExceptions on unknown columns (the common failure
        # mode) are self-correctable when the model can see both the
        # error message and the SQL it submitted side-by-side.
        err_text = f"{type(exc).__name__}: {exc!s}"
        if len(err_text) > 800:
            err_text = err_text[:800] + "..."
        return {
            "index": GRAPH_INDEX,
            "error": err_text,
            "submitted_sql": (
                resolved_sql if len(resolved_sql) <= 500
                else resolved_sql[:500] + "..."
            ),
            "hint": (
                "Check the tables / columns listed in this tool's "
                "description before retrying -- unknown column or "
                "table names trigger a DuckDB BinderException."
            ),
        }
    truncated = _truncate_for_model(env)
    return _apply_binding(bind_as, truncated)


QUERY_GRAPH = Tool(
    name="query_graph",
    description=(
        "DuckDB read-only SQL on the GDELT+SEC knowledge graph "
        "(Parquet-backed). Use quoted YYYYMMDD strings for "
        "filing.event_date / article.gkg_date. actor.ticker is "
        "comma-separated -- use ILIKE '%TICKER%', not '='. Always "
        "pre-filter has_theme by actor (mentions_org). Returns rows as a list of "
        "dicts with column metadata in `columns` and a `truncated` "
        "flag. The exact tables / columns available on this index "
        "are listed below. Pass `bind_as=<name>` to ALSO bind the "
        "rows as a Python variable for `run_python` post-processing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": (
                    "A single SELECT (or WITH) statement. DDL and "
                    "DML are rejected by the validator."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["sql"],
    },
    fn=_do_query_graph,
    bound_indices=((GRAPH_INDEX, ("sql",)),),
)


_NUMERIC_SEED_RE = re.compile(r"^\d+$")


def _resolve_seed_to_actor_id(seed: str) -> Optional[str]:
    """Translate a ticker / company name into a numeric ``actor.id``.

    ``actor.id`` in ``graph_combined`` is a BIGINT, and the multihop
    kernel matches seeds against it verbatim (no server-side
    resolution). LLMs regularly pass names ("NVIDIA") or tickers
    ("NVDA") instead, which raise a DuckDB
    ``ConversionException: Could not convert string 'NVIDIA' to INT64``.

    Numeric seeds pass through unchanged. Non-numeric seeds issue a
    one-row lookup against the ``actor`` table matching ``ticker``
    (comma-separated keyword; ``ILIKE '%X%'`` covers multi-ticker
    actors) or ``name``. Ticker matches rank above name matches so
    an NVDA lookup doesn't accidentally resolve to
    "NVIDIA PARTNERS HOLDING LLC"; within each tier we prefer the
    shortest name so "NVIDIA CORP" beats
    "NVIDIA CORPORATION FY 2023 PROXY SUBMITTER".

    Returns ``None`` when nothing resolves; the caller decides
    whether to surface a clean error or drop the seed.
    """
    seed = (seed or "").strip()
    if not seed:
        return None
    if _NUMERIC_SEED_RE.match(seed):
        return seed
    # Defensive SQL escaping -- seed is LLM-authored.
    safe = seed.replace("'", "''")
    # Two sequential queries instead of a single `ORDER BY CASE WHEN
    # ... END` -- the platform's SQL validator blocks the ``END``
    # keyword (overbroad rule intended to catch END TRANSACTION), so
    # we can't express "prefer ticker match over name match" inline.
    # Ticker-first match means an NVDA lookup finds NVIDIA CORP
    # before any partnership whose `name` happens to contain the
    # substring "NVDA".
    for where in (
        f"ticker ILIKE '%{safe}%'",
        f"name ILIKE '%{safe}%'",
    ):
        sql = (
            f"SELECT id FROM actor WHERE {where} "
            f"ORDER BY LENGTH(name) LIMIT 1"
        )
        try:
            env = cb_tools.sql(index=GRAPH_INDEX, query=sql)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "multihop_graph: actor id lookup for %r (%s) failed: %s",
                seed, where, exc,
            )
            continue
        rows = env.get("rows") or []
        if not rows:
            continue
        first = rows[0]
        if not isinstance(first, Mapping):
            continue
        raw = first.get("id")
        if raw is not None:
            return str(raw)
    return None


def _do_multihop_graph(
    seed: str,
    *,
    hops: int = 2,
    predicate_filter: Optional[Sequence[str]] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """``tools.multihop`` BFS from one or more seed actor ids.

    Accepts a single ``seed`` or a comma-separated list
    (``"NVDA,AAPL"``). Each seed is either a numeric ``actor.id``
    (passes through) OR a ticker / company name that we resolve
    server-side via :func:`_resolve_seed_to_actor_id`. The kernel
    only accepts numeric ids, so resolving here lets personas
    express intent ("expand from NVIDIA") without an extra round
    trip through ``query_graph`` for the id lookup.

    Resolution results are returned under ``_resolved_seeds`` so the
    model can see what ``NVDA`` actually became; unresolved entries
    are listed under ``unresolved_seeds`` in the error envelope.
    """
    raw_seeds = [s.strip() for s in seed.split(",") if s.strip()]
    if not raw_seeds:
        return {
            "index": GRAPH_INDEX,
            "error": "multihop_graph requires at least one seed",
        }

    resolved: list[str] = []
    resolution_trace: dict[str, str] = {}
    unresolved: list[str] = []
    for s in raw_seeds:
        rid = _resolve_seed_to_actor_id(s)
        if rid is None:
            unresolved.append(s)
        else:
            resolved.append(rid)
            if rid != s:
                resolution_trace[s] = rid

    if not resolved:
        return {
            "index": GRAPH_INDEX,
            "error": (
                "multihop_graph: could not resolve any seed to an "
                "actor.id. Try a ticker ('NVDA'), a company name "
                "('NVIDIA'), or a numeric id from "
                "`query_graph` (`SELECT id FROM actor WHERE "
                "ticker ILIKE '%NVDA%'`)."
            ),
            "unresolved_seeds": unresolved,
        }

    env = cb_tools.multihop(
        index=GRAPH_INDEX,
        start_ids=resolved,
        hops=hops,
        predicate_filter=(
            list(predicate_filter) if predicate_filter else None
        ),
    )
    if isinstance(env, dict):
        if resolution_trace:
            env["_resolved_seeds"] = resolution_trace
        if unresolved:
            env["_unresolved_seeds"] = unresolved
    return _apply_binding(bind_as, env)


MULTIHOP_GRAPH = Tool(
    name="multihop_graph",
    description=(
        "BFS multi-hop traversal from a seed actor (or "
        "comma-separated seeds) over the GDELT+SEC knowledge graph. "
        "Returns the visited subgraph as {nodes: [...], edges: [...]}. "
        "Use predicate_filter to narrow to specific edge relations; "
        "the registered set of predicates / node types is listed "
        "below. hops is clamped by the index's per-call cap; "
        "truncated=true means the node or edge cap fired. Pass "
        "`bind_as=<name>` to ALSO expose the subgraph as a Python "
        "variable for `run_python` (e.g. centrality, path search). "
        "\n\n`seed` accepts a ticker ('NVDA'), a company name "
        "('NVIDIA'), or a numeric `actor.id` -- names and tickers "
        "are resolved server-side against the `actor` table (ticker "
        "matches preferred, then shortest name) before the BFS "
        "runs. The resolved ids come back as `_resolved_seeds` in "
        "the response so you can see what was picked; anything that "
        "didn't resolve shows up under `_unresolved_seeds`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "seed": {
                "type": "string",
                "description": (
                    "Ticker ('NVDA'), company name ('NVIDIA'), or "
                    "numeric `actor.id` as a string. Multiple seeds "
                    "may be comma-separated ('NVDA,AAPL'). Names / "
                    "tickers are resolved to ids server-side."
                ),
            },
            "hops": {
                "type": "integer", "default": 2,
                "minimum": 1, "maximum": 6,
            },
            "predicate_filter": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional whitelist of edge relations to "
                    "traverse; see the predicate list below. "
                    "Unknown predicates are rejected by the gateway."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["seed"],
    },
    fn=_do_multihop_graph,
    bound_indices=((GRAPH_INDEX, ("multihop",)),),
)


# --------------------------------------------------------------------------
# Macro / equity time series (DuckDB-over-Parquet)
# --------------------------------------------------------------------------


# Macro series alias -> parquet table name in the macro_v1 index.
# The registration's ``capabilities.sql.tables`` declares each as a
# separate view; the model's persona-supplement nudges it to use
# the alias on the left so we keep the legacy alias surface intact
# while the kernel sees a stable table name on the right.
_MACRO_SERIES_TABLES: dict[str, str] = {
    "brent": "brent",
    "oil": "brent",
    "federal_funds": "federal_funds_rate",
    "fed_funds": "federal_funds_rate",
    "treasury_10y": "treasury_yield_10y",
    "10y": "treasury_yield_10y",
    "cpi": "cpi",
    "inflation": "inflation",
    "unemployment": "unemployment",
}


# Run-scoped caches for SQL-backed time-series tools.
#
# alphacumen is loaded once per pipeline subprocess, and each pipeline run
# spawns a fresh subprocess — so module-level dicts are naturally
# request-scoped (entries die when the subprocess exits). We use this
# to memoize the two big offenders we observed in the swarm logs:
#
#   - ``stock_analyst`` calls ``get_equity_bars(NVDA, ...)`` (~2 s) and
#     then ``compute_technicals(NVDA, ...)`` over the same window
#     (~2 s) — but ``compute_technicals`` internally calls
#     ``_do_get_equity_bars`` again, paying the same S3 cost twice.
#   - ``sector_analyst`` and ``risk_analyst`` both pull the same
#     ``federal_funds`` macro series in parallel.
#
# A simple in-memory cache keyed on the call args is sufficient: a
# single swarm run rarely re-fetches more than a handful of distinct
# windows, so the cache stays small (no eviction needed). We
# ``deepcopy`` on retrieval so downstream binding / mutation cannot
# poison the cache.
_EQUITY_BARS_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}
_MACRO_SERIES_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}
# get_full_text re-fetch dedup. Sector_analyst observed in traces
# calling get_full_text on the same ref twice in the same run (once to
# bind_as=filing_body, then again later to "re-read" it because the
# bound payload had aged out of the model's reasoning context).
# Keying on (ref, max_chars) — different max_chars produces a
# different result envelope so each pair-state needs its own slot.
_FULL_TEXT_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
# Options re-fetch dedup. The run that motivated this
# (request d1489859-…) had two specialists both call
# ``compute_options_stats(AAPL, 2026-02-02)`` ~4 s apart — both paid
# the full ~35 s fetch on Fargate because there was no in-flight
# dedup. The chain itself is also pulled by ``get_options_chain`` with
# a different filter set, but the underlying parquet read is the same
# fixed cost. Keying ``compute_options_stats`` on (sym, requested_date)
# — the snippet's snapshot_date fallback is deterministic given those
# inputs — and ``get_options_chain`` on the full filter tuple gives
# us tight dedup without ever serving wrong filters.
_OPTIONS_STATS_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_OPTIONS_CHAIN_CACHE: dict[
    tuple[str, str, Optional[str], Optional[float], Optional[float],
          Optional[str], Optional[int], Optional[int], str, int],
    dict[str, Any],
] = {}


def _clear_run_caches() -> None:
    """Clear the run-scoped tool caches.

    Production code never calls this — the subprocess exits at end of
    run and module state goes with it. Tests use this to reset state
    between cases since a single ``pytest`` process loads the module
    once.
    """
    _EQUITY_BARS_CACHE.clear()
    _MACRO_SERIES_CACHE.clear()
    _FULL_TEXT_CACHE.clear()
    _XBRL_FACTS_CACHE.clear()
    _FILING_URL_CACHE.clear()
    _FILING_HTML_CACHE.clear()
    _OPTIONS_STATS_CACHE.clear()
    _OPTIONS_CHAIN_CACHE.clear()


def _do_get_macro_series(
    series: str,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Macro time series via ``tools.sql`` over the macro Parquet tables.

    The legacy ``get_macro_series`` had a hand-coded S3-fetch +
    pandas pipeline per series. We now compose a DuckDB SELECT
    against the right view and let the kernel handle materialisation,
    row caps, and timeout. ``start`` / ``end`` flow in as
    ``YYYY-MM-DD`` -- consistent with the legacy interface and what
    the persona supplement teaches the model to send.

    Also accepts ``start_date`` / ``end_date`` as aliases because the
    model occasionally drifts to those names (seen on the Qwen 3 235b
    traces). Either naming works; the aliases are applied before
    validation so there's no surprise precedence.

    The series alias is resolved against :data:`_MACRO_SERIES_TABLES`
    server-side here (rather than at the gateway) because alias
    resolution is IA-policy, not platform-policy. Anything else
    falls through unchanged so a future series the platform learns
    about doesn't require an IA release.
    """
    # Accept legacy / drifted aliases; explicit ``start`` / ``end``
    # wins when both shapes are passed.
    start = start or start_date
    end = end or end_date
    if not start or not end:
        return {
            "error": (
                "get_macro_series requires `start` and `end` "
                "(inclusive YYYY-MM-DD)."
            ),
            "series": series,
        }
    if not isinstance(series, str):
        return {
            "error": (
                "get_macro_series expects a single series string "
                "(one of: "
                f"{', '.join(sorted(set(_MACRO_SERIES_TABLES.keys())))}). "
                "Call the tool once per series; futures / equity "
                "indices aren't macro series -- use get_equity_bars."
            ),
        }
    key = series.lower()
    if key not in _MACRO_SERIES_TABLES:
        return {
            "error": (
                f"get_macro_series: unknown series {series!r}. Valid: "
                f"{', '.join(sorted(set(_MACRO_SERIES_TABLES.keys())))}. "
                "Futures / equity indices (e.g. nasdaq100_futures, "
                "spx_futures, NDX, SPX) aren't macro series -- use "
                "get_equity_bars with the underlying ETF symbol "
                "(QQQ for NDX, SPY for SPX)."
            ),
        }
    table = _MACRO_SERIES_TABLES[key]
    cache_key = (table, start, end)
    cached = _MACRO_SERIES_CACHE.get(cache_key)
    if cached is None:
        # Order descending so the most recent data survives result
        # truncation (the 12KB cap drops the tail, which is now the
        # oldest rows — much less important than the latest levels).
        sql = (
            f"SELECT CAST(obs_date AS DATE) AS date, value FROM {table} "
            f"WHERE obs_date >= '{start}' AND obs_date <= '{end}' "
            f"ORDER BY obs_date DESC"
        )
        env = cb_tools.sql(index=MACRO_INDEX, query=sql)
        rows = env.get("rows", [])
        rows.reverse()
        cached = {
            "table": table,
            "start": start,
            "end": end,
            "rows": rows,
            "row_count": env.get("row_count", 0),
            "truncated": env.get("truncated", False),
        }
        _MACRO_SERIES_CACHE[cache_key] = cached
    full = {"series": series, **copy.deepcopy(cached)}
    return _apply_binding(bind_as, full)


GET_MACRO_SERIES = Tool(
    name="get_macro_series",
    description=(
        "US macro / commodity benchmark time series. Valid series: "
        "brent (oil), federal_funds, treasury_10y, cpi, inflation, "
        "unemployment. Pass start/end as YYYY-MM-DD inclusive. "
        "Returns rows of {date, value} ordered by date. Use this for "
        "rates, inflation, oil prices -- not for individual stock "
        "OHLC (use get_equity_bars for that). Pass `bind_as=<name>` "
        "to ALSO bind the rows as a Python variable for "
        "`run_python` slicing / aggregation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "series": {
                "type": "string",
                "enum": sorted(set(_MACRO_SERIES_TABLES.keys())),
            },
            "start": {
                "type": "string",
                "description": "Inclusive YYYY-MM-DD.",
            },
            "end": {
                "type": "string",
                "description": "Inclusive YYYY-MM-DD.",
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["series", "start", "end"],
    },
    fn=_do_get_macro_series,
    bound_indices=((MACRO_INDEX, ("sql",)),),
)


def _do_get_equity_bars(
    symbol: str, *, start: str, end: str,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """OHLCV bars for a single ticker via ``tools.sql``.

    Joins the equity-bars index's ``bars`` table on ``symbol``;
    returns one row per trading day. The bars are the raw input
    that :func:`compute_technicals` then aggregates with ``tools.py``;
    the model is encouraged (in the persona supplement) to pull
    bars + author its own technicals snippet rather than relying on
    a server-side ``compute_technicals`` like the legacy code did.
    """
    sym = symbol.upper()
    cache_key = (sym, start, end)
    cached = _EQUITY_BARS_CACHE.get(cache_key)
    if cached is None:
        sql = (
            f"SELECT date, open, high, low, close, volume "
            f"FROM bars "
            f"WHERE symbol = '{sym}' "
            f"AND date >= '{start}' AND date <= '{end}' "
            f"ORDER BY date"
        )
        env = cb_tools.sql(index=EQUITY_BARS_INDEX, query=sql)
        cached = {
            "symbol": sym,
            "start": start,
            "end": end,
            "rows": env.get("rows", []),
            "row_count": env.get("row_count", 0),
            "truncated": env.get("truncated", False),
        }
        _EQUITY_BARS_CACHE[cache_key] = cached
    full = copy.deepcopy(cached)
    return _apply_binding(bind_as, full)


GET_EQUITY_BARS = Tool(
    name="get_equity_bars",
    description=(
        "Daily OHLCV bars for one US-listed ticker. Returns rows of "
        "{date, open, high, low, close, volume} ordered by date. "
        "Pair with compute_technicals to get rolling SMA / ATR / "
        "support / resistance over the same window. For long windows "
        "(year+) prefer compute_technicals -- it keeps the bars in "
        "the tool boundary. If you need raw bars in run_python for a "
        "bespoke analysis, pass `bind_as=<name>` here."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["symbol", "start", "end"],
    },
    fn=_do_get_equity_bars,
    bound_indices=((EQUITY_BARS_INDEX, ("sql",)),),
)


# --------------------------------------------------------------------------
# Reddit / retail sentiment (DuckDB-over-Parquet on reddit_pullpush_v1)
# --------------------------------------------------------------------------
#
# The two Reddit tools mirror what the legacy gdelt swarm carries
# in ``gdelt.project.reddit_sentiment_s3`` (raw boto3 + DuckDB
# httpfs against ``s3://.../research-tables/reddit/pullpush/``)
# but expressed over the platform's ``tools.sql`` verb so the alphacumen
# pipeline keeps its "no direct backend access from user code"
# property. The platform registers two views on the
# ``reddit_pullpush_v1`` index:
#
# - ``sentiment_daily`` -- one row per (ticker, subreddit, obs_date)
#   carrying ``post_count``, ``avg_sentiment``,
#   ``score_weighted_sentiment``, ``total_score``, ``top_post_title``,
#   ``top_post_id``. Pre-aggregated VADER on the body+title text;
#   used for "is retail sentiment improving / deteriorating" style
#   questions.
# - ``posts`` -- one row per Reddit submission with ``post_id``,
#   ``subreddit``, ``obs_date``, ``title``, ``selftext``, ``score``,
#   ``upvote_ratio``, ``num_comments``, ``tickers_mentioned``,
#   ``sentiment_score``, ``sentiment_label``, ``url``. Used for
#   keyword search over individual discussions.
#
# Coverage ceiling is ~ May 2025 (pullpush.io indexing horizon);
# queries beyond that return empty rows, NOT errors -- the persona
# supplement explains this so the model doesn't keep retrying.

# Default subreddit roster when the model doesn't pass one. Mirrors
# the "general subs (no canonical_tickers)" set that
# :func:`gdelt.project.reddit_sentiment_s3._load_registries` falls
# back to. Kept here (rather than pulled from the gateway) so the
# tool surface is self-describing -- the model can read the enum
# values straight off the JSON schema.
_DEFAULT_REDDIT_SUBREDDITS: tuple[str, ...] = (
    "wallstreetbets",
    "stocks",
    "investing",
    "options",
    "SecurityAnalysis",
)

# Hard ceiling of the pullpush.io backfill currently sitting in
# ``s3://coralbricks-research/research-tables/reddit/pullpush/``. Verified
# empirically against the parquet (``MAX(obs_date)`` in
# ``sentiment_daily``); the underlying ingest stops here because
# pullpush.io's own indexing horizon ended around mid-May 2025. If a
# fresh backfill lands, bump this constant in the same PR that
# republishes the parquet so the warnings below stay honest.
_REDDIT_COVERAGE_CEILING = "2025-05-19"


def _reddit_window_past_ceiling(start: Optional[str]) -> bool:
    """True iff the request window starts strictly past the pullpush.io
    coverage ceiling.

    When this returns True, the Reddit tools can short-circuit and
    return an empty envelope without issuing a DuckDB query — no rows
    are possible for any (ticker, subreddit) combination in the window.
    Empirically this saves 1.5-2.5s per skipped tool call (the DuckDB
    scan still touches every parquet partition even when the result is
    empty); on a single specialist round it can shave several seconds
    off wall-clock for queries about events outside the coverage band
    (e.g. 2026 NVIDIA filings).

    Comparison is string-wise on ISO ``YYYY-MM-DD``, same as
    :func:`_reddit_coverage_warning`.
    """
    s = (start or "").strip()
    return bool(s) and s > _REDDIT_COVERAGE_CEILING


def _reddit_coverage_warning(
    start: Optional[str],
    end: Optional[str],
    row_count: int,
) -> Optional[str]:
    """Build a structured warning when the request window overshoots
    the pullpush.io ceiling.

    Returns ``None`` when the window is entirely inside coverage, or
    when it overshoots only on the tail end and we still managed to
    return rows (the partial overlap was useful, no need to nag).
    The string returned is meant to be surfaced verbatim back to the
    react loop so the model recognises ``"empty"`` as a coverage gap
    rather than a transient miss to retry.

    String comparison is safe here because both ``start`` / ``end``
    and the ceiling are ISO ``YYYY-MM-DD``.
    """
    ceiling = _REDDIT_COVERAGE_CEILING
    s = (start or "").strip()
    e = (end or "").strip()
    if s and s > ceiling:
        return (
            f"request window starts {s}, which is past the "
            f"pullpush.io coverage ceiling ({ceiling}); no rows "
            "possible -- do NOT retry with adjacent dates."
        )
    if row_count == 0 and e and e > ceiling:
        return (
            f"request window ends {e}, past the pullpush.io "
            f"coverage ceiling ({ceiling}); the empty result "
            "reflects missing data, not a transient miss -- "
            "do NOT retry with adjacent dates."
        )
    return None


def _sql_str_literal(s: str) -> str:
    """Quote a Python string for inline use in DuckDB SQL.

    The alphacumen SQL dispatchers don't use parameter binding -- they
    splice typed args (regex-validated tickers, ISO dates) into
    f-string SQL, the same shape :func:`_do_get_equity_bars` uses.
    For free-form text (``search_reddit_posts``'s ``query``,
    subreddit names) we need to defend the SQL boundary; doubling
    single quotes is the DuckDB-standard escape.

    NB: this is NOT a sanitizer for arbitrary SQL injection -- the
    gateway's SQL backend is a query verb that only accepts SELECT
    statements; this helper just keeps the literal valid.
    """
    return "'" + s.replace("'", "''") + "'"


def _resolve_reddit_subs(subreddits: Optional[Sequence[str]]) -> list[str]:
    """Validate / default the subreddit list passed to a Reddit tool.

    Empty / unset -> the five-sub default roster (matches the
    legacy ``_GENERAL_SUBREDDITS`` fallback). Caller-supplied
    values are passed through verbatim (the gateway-side index
    enforces the canonical case); we only de-duplicate.
    """
    if not subreddits:
        return list(_DEFAULT_REDDIT_SUBREDDITS)
    seen: dict[str, None] = {}
    for s in subreddits:
        if isinstance(s, str) and s.strip():
            seen[s.strip()] = None
    return list(seen) or list(_DEFAULT_REDDIT_SUBREDDITS)


def _do_get_reddit_sentiment(
    ticker: str,
    *,
    start: str,
    end: str,
    subreddits: Optional[Sequence[str]] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Daily VADER sentiment aggregates for one ticker via ``tools.sql``.

    Composes a SELECT against the ``sentiment_daily`` view on the
    ``reddit_pullpush_v1`` index. Mirrors the legacy
    :func:`gdelt.project.reddit_sentiment_s3.get_reddit_sentiment_dict`
    column projection (``obs_date``, ``subreddit``, ``post_count``,
    ``avg_sentiment``, ``score_weighted_sentiment``, ``total_score``,
    ``top_post_title``, ``top_post_id``) so a downstream consumer
    can cut over without changing its row-shape expectations.

    The IN-clause on ``subreddit`` is built from validated string
    literals, not parameter-bound, to stay consistent with the other
    alphacumen SQL dispatchers (see :func:`_sql_str_literal`).
    """
    ticker_clean = (ticker or "").strip().upper()
    subs = _resolve_reddit_subs(subreddits)

    # Fast-path: window starts past the pullpush.io coverage ceiling →
    # no rows are possible, skip the DuckDB scan entirely. Saves
    # ~1.5-2.5s per call when the model asks for a post-coverage date
    # range (e.g. any 2026 event).
    if _reddit_window_past_ceiling(start):
        return _apply_binding(bind_as, {
            "ticker": ticker_clean,
            "subreddits": subs,
            "start": start,
            "end": end,
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "coverage_note": (
                "pullpush.io data available up to approximately May 2025"
            ),
            "coverage_warning": _reddit_coverage_warning(start, end, 0),
            "short_circuited": True,
        })

    sub_list = ", ".join(_sql_str_literal(s) for s in subs)
    sql = (
        "SELECT CAST(obs_date AS DATE) AS obs_date, subreddit, "
        "post_count, avg_sentiment, score_weighted_sentiment, "
        "total_score, top_post_title, top_post_id "
        "FROM sentiment_daily "
        f"WHERE UPPER(ticker) = {_sql_str_literal(ticker_clean)} "
        f"AND obs_date >= '{start}' AND obs_date <= '{end}' "
        f"AND subreddit IN ({sub_list}) "
        "ORDER BY obs_date ASC, subreddit ASC"
    )
    env = cb_tools.sql(index=REDDIT_INDEX, query=sql)
    row_count = env.get("row_count", 0)
    full = {
        "ticker": ticker_clean,
        "subreddits": subs,
        "start": start,
        "end": end,
        "rows": env.get("rows", []),
        "row_count": row_count,
        "truncated": env.get("truncated", False),
        "coverage_note": (
            "pullpush.io data available up to approximately May 2025"
        ),
    }
    warning = _reddit_coverage_warning(start, end, row_count)
    if warning is not None:
        full["coverage_warning"] = warning
    return _apply_binding(bind_as, full)


GET_REDDIT_SENTIMENT = Tool(
    name="get_reddit_sentiment",
    description=(
        "Pre-aggregated daily Reddit sentiment for a stock ticker "
        "across r/wallstreetbets, r/stocks, r/investing, r/options, "
        "and r/SecurityAnalysis (plus any ticker-specific subs the "
        "ingest registers). Returns rows of {obs_date, subreddit, "
        "post_count, avg_sentiment, score_weighted_sentiment, "
        "total_score, top_post_title, top_post_id} ordered by date. "
        "Use when the question asks about retail investor sentiment, "
        "Reddit buzz, WSB/community reaction, meme-stock momentum, "
        "or short-squeeze opinion on a ticker over a date range. "
        "Coverage ceiling 2025-05-19 (pullpush.io). Windows past "
        "that come back empty with a `coverage_warning` field set; "
        "treat the empty result as terminal -- do NOT retry with "
        "adjacent dates, and clamp `end` to 2025-05-19 (or skip the "
        "call entirely) if the question is about a more recent "
        "period. Pair with get_equity_bars or compute_technicals for "
        "price+sentiment context. Pass `bind_as=<name>` to ALSO "
        "expose the rows as a Python variable for `run_python` "
        "(e.g. correlate sentiment with returns, regroup by week)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": (
                    "Uppercase ticker symbol, e.g. NVDA, AAPL, TSLA."
                ),
            },
            "start": {
                "type": "string",
                "description": "Inclusive YYYY-MM-DD.",
            },
            "end": {
                "type": "string",
                "description": "Inclusive YYYY-MM-DD.",
            },
            "subreddits": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of subreddits to filter to "
                    "(e.g. [\"wallstreetbets\", \"options\"]). "
                    "Default: the five tracked communities."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "start", "end"],
    },
    fn=_do_get_reddit_sentiment,
    bound_indices=((REDDIT_INDEX, ("sql",)),),
)


def _do_search_reddit_posts(
    query: str,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    subreddits: Optional[Sequence[str]] = None,
    limit: int = 20,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Full-text keyword search over Reddit post titles + bodies.

    Composes an ILIKE-on-title-OR-selftext SELECT against the
    ``posts`` view on the ``reddit_pullpush_v1`` index. Posts are
    ranked by ``score`` (Reddit upvotes) DESC and the body excerpt
    is truncated server-side to 300 chars to keep the model context
    lean -- :func:`get_reddit_posts_dict` in the legacy module
    enforces the same cap, so the row shape carries over.
    """
    query_clean = (query or "").strip()
    if not query_clean:
        return {
            "query": "",
            "subreddits": _resolve_reddit_subs(subreddits),
            "error": "query must not be empty.",
            "posts": [],
            "post_count": 0,
        }

    # Clamp to the same 1..50 envelope the legacy reader uses, so
    # the model can't ask for a million posts in one shot.
    try:
        clamped_limit = int(limit) if limit else 20
    except (TypeError, ValueError):
        clamped_limit = 20
    clamped_limit = max(1, min(50, clamped_limit))

    subs = _resolve_reddit_subs(subreddits)

    # Fast-path: window starts past the pullpush.io coverage ceiling →
    # no posts are possible, skip the DuckDB ILIKE scan entirely.
    # Same rationale as :func:`_do_get_reddit_sentiment` above.
    if _reddit_window_past_ceiling(start):
        return _apply_binding(bind_as, {
            "query": query_clean,
            "subreddits": subs,
            "start": start,
            "end": end,
            "limit": clamped_limit,
            "posts": [],
            "post_count": 0,
            "truncated": False,
            "coverage_note": (
                "pullpush.io data available up to approximately May 2025"
            ),
            "coverage_warning": _reddit_coverage_warning(start, end, 0),
            "short_circuited": True,
        })

    sub_list = ", ".join(_sql_str_literal(s) for s in subs)
    pattern = _sql_str_literal(f"%{query_clean}%")

    where_parts = [
        f"(title ILIKE {pattern} OR selftext ILIKE {pattern})",
        f"subreddit IN ({sub_list})",
    ]
    if start:
        where_parts.append(f"obs_date >= '{start}'")
    if end:
        where_parts.append(f"obs_date <= '{end}'")

    sql = (
        "SELECT post_id, subreddit, CAST(obs_date AS DATE) AS obs_date, "
        "title, LEFT(selftext, 300) AS selftext_excerpt, score, "
        "upvote_ratio, num_comments, tickers_mentioned, "
        "sentiment_score, sentiment_label, url "
        "FROM posts WHERE " + " AND ".join(where_parts) + " "
        "ORDER BY score DESC "
        f"LIMIT {clamped_limit}"
    )
    env = cb_tools.sql(index=REDDIT_INDEX, query=sql)
    post_count = env.get("row_count", 0)
    full = {
        "query": query_clean,
        "subreddits": subs,
        "start": start,
        "end": end,
        "limit": clamped_limit,
        "posts": env.get("rows", []),
        "post_count": post_count,
        "truncated": env.get("truncated", False),
        "coverage_note": (
            "pullpush.io data available up to approximately May 2025"
        ),
    }
    warning = _reddit_coverage_warning(start, end, post_count)
    if warning is not None:
        full["coverage_warning"] = warning
    return _apply_binding(bind_as, full)


SEARCH_REDDIT_POSTS = Tool(
    name="search_reddit_posts",
    description=(
        "Full-text keyword search over Reddit post titles and "
        "bodies across r/wallstreetbets, r/stocks, r/investing, "
        "r/options, and r/SecurityAnalysis (plus any ticker-"
        "specific subs the ingest registers). Returns posts ranked "
        "by score (upvotes) DESC -- each carries title, 300-char "
        "body excerpt, score, upvote_ratio, num_comments, "
        "tickers_mentioned, sentiment_score, sentiment_label, url. "
        "Use for specific Reddit discussions, DD posts, or "
        "short-squeeze commentary around a company / event / "
        "topic. Coverage ceiling 2025-05-19 (pullpush.io). "
        "Windows past that come back empty with a "
        "`coverage_warning` field set; treat the empty result as "
        "terminal -- do NOT retry with adjacent dates, and clamp "
        "`end` to 2025-05-19 (or skip the call entirely) if the "
        "question is about a more recent period. "
        "Default 20 results, max 50. Pass `bind_as=<name>` to "
        "ALSO expose the posts list as a Python variable for "
        "`run_python` (e.g. dedup by author, regroup by sub)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keyword or phrase to search for "
                    "(case-insensitive)."
                ),
            },
            "start": {
                "type": "string",
                "description": (
                    "Optional inclusive start date YYYY-MM-DD."
                ),
            },
            "end": {
                "type": "string",
                "description": (
                    "Optional inclusive end date YYYY-MM-DD."
                ),
            },
            "subreddits": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subreddit filter "
                    "(e.g. [\"wallstreetbets\", \"SecurityAnalysis\"])."
                    " Default: the five tracked communities."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Max posts to return (default 20, max 50)."
                ),
                "minimum": 1,
                "maximum": 50,
                "default": 20,
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["query"],
    },
    fn=_do_search_reddit_posts,
    bound_indices=((REDDIT_INDEX, ("sql",)),),
)


# --------------------------------------------------------------------------
# In-runner Python -- compute_technicals lives here now
# --------------------------------------------------------------------------


# Default snippet ``compute_technicals`` runs when the model just
# wants the standard SMA(20)/SMA(50)/ATR(14)/range stats. The model
# can pass ``code=`` to override with anything bespoke. Kept short
# so the entire snippet fits well inside the in-runner timeout.
_TECHNICALS_DEFAULT_SNIPPET = """
import statistics

closes = [r['close'] for r in bars]
highs  = [r['high']  for r in bars]
lows   = [r['low']   for r in bars]

def _sma(xs, n):
    return statistics.mean(xs[-n:]) if len(xs) >= n else None

def _atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1]),
        )
        trs.append(tr)
    return statistics.mean(trs[-n:])

# Wilder's RSI(n). First avg = SMA of first n changes; subsequent avgs
# use Wilder smoothing: avg' = (avg * (n-1) + current) / n.
def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in diffs]
    losses = [-d if d < 0 else 0.0 for d in diffs]
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(diffs)):
        avg_g = (avg_g * (n - 1) + gains[i])  / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))

# Standard EMA(n) seeded with the SMA of the first n samples, alpha
# = 2/(n+1). Returns the full EMA series (None for indices < n-1) so
# callers can compose multi-stage EMA chains (e.g. MACD's signal line).
def _ema_series(xs, n):
    if len(xs) < n:
        return [None] * len(xs)
    out = [None] * len(xs)
    seed = sum(xs[:n]) / n
    out[n-1] = seed
    alpha = 2.0 / (n + 1)
    for i in range(n, len(xs)):
        out[i] = xs[i] * alpha + out[i-1] * (1 - alpha)
    return out

# MACD(fast=12, slow=26, signal=9): MACD line = EMA(fast) - EMA(slow);
# signal line = EMA(signal) of the MACD line; histogram = MACD - signal.
def _macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    macd_line = [
        (ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else None
        for i in range(len(closes))
    ]
    macd_clean = [m for m in macd_line if m is not None]
    sig = _ema_series(macd_clean, signal)
    macd_now = macd_line[-1]
    sig_now = sig[-1] if sig else None
    if macd_now is None or sig_now is None:
        return None
    return {
        'macd':      macd_now,
        'signal':    sig_now,
        'histogram': macd_now - sig_now,
    }

# Bollinger(n=20, k=2): middle = SMA(n); upper/lower = middle ± k·σ
# where σ is the population stdev of the last n closes (ddof=0,
# matches TradingView / most charting libs).
def _bollinger(closes, n=20, k=2):
    if len(closes) < n:
        return None
    window = closes[-n:]
    middle = statistics.mean(window)
    sd = statistics.pstdev(window)
    return {
        'middle': middle,
        'upper':  middle + k * sd,
        'lower':  middle - k * sd,
        'width':  2 * k * sd,
    }

result = {
    'symbol': symbol,
    'as_of':  bars[-1]['date'] if bars else None,
    'last_close': closes[-1] if closes else None,
    'sma_20': _sma(closes, 20),
    'sma_50': _sma(closes, 50),
    'atr_14': _atr(highs, lows, closes, 14),
    'rsi_14': _rsi(closes, 14),
    'macd':   _macd(closes, 12, 26, 9),
    'bollinger_20': _bollinger(closes, 20, 2),
    'recent_high': max(highs[-50:]) if len(highs) >= 1 else None,
    'recent_low':  min(lows[-50:])  if len(lows)  >= 1 else None,
    'bar_count': len(bars),
}
""".strip()


def _do_compute_technicals(
    symbol: str,
    *,
    start: str,
    end: str,
    code: Optional[str] = None,
) -> dict[str, Any]:
    """``compute_technicals`` is now ``get_equity_bars`` + ``tools.py``.

    Pull bars over the window, then hand the rows + the symbol to
    the in-runner Python interpreter, which evaluates either the
    default :data:`_TECHNICALS_DEFAULT_SNIPPET` or a model-supplied
    ``code=`` override. The default snippet computes the same set
    of stats the legacy server-side ``compute_technicals`` returned
    (SMA20/50, ATR14, recent range), so persona-level expectations
    don't shift.

    Why this exists alongside :data:`RUN_PYTHON`
    --------------------------------------------

    This is a *server-side composition*: the bars never enter the
    model's context. A 1-year daily window is ~250 rows of OHLCV
    (~12 KB JSON); going through the model would mean 12 KB into
    the next prompt and 12 KB back out as the ``run_python(inputs=...)``
    arg, plus an extra LLM round-trip and a brittle JSON copy. So
    the rule is:

    - Voluminous "fetch X then compute Y(X)" -> use this tool, the
      bars stay inside the tool boundary.
    - Cheap "compute over things the model already saw" (RRF on a
      handful of search hits, regrouping macro rows, dedup) -> use
      :data:`RUN_PYTHON` directly.

    The two-call pattern (``sql`` then ``py``) is deliberately
    visible -- it's the canonical example of the new tools.py
    contract: pull rows over the wire, do the math locally with
    the Python interpreter, never round-trip dataframes back
    through the gateway. Personas can author their own snippets
    (rolling Bollinger bands, custom support/resistance, regime
    classifiers) without a platform release.
    """
    sym = symbol.upper()
    bars_env = _do_get_equity_bars(sym, start=start, end=end)
    bars = bars_env.get("rows", [])
    if not bars:
        return {
            "symbol": sym,
            "start": start,
            "end": end,
            "error": "no bars returned for window; check symbol / dates",
        }

    snippet = code or _TECHNICALS_DEFAULT_SNIPPET
    py_env = cb_tools.py(
        snippet,
        inputs={"bars": bars, "symbol": sym},
    )
    if not py_env.get("ok"):
        err = py_env.get("error") or {}
        return {
            "symbol": sym,
            "start": start,
            "end": end,
            "bar_count": len(bars),
            "error": err.get("message") or "py snippet failed",
            "error_type": err.get("type"),
        }

    return {
        "symbol": sym,
        "start": start,
        "end": end,
        "bar_count": len(bars),
        "result": py_env.get("result"),
        "took_ms": py_env.get("took_ms"),
        # Raw OHLC rows kept on the envelope so the swarm's
        # equity_chart shaper (see
        # swarm._build_equity_chart_from_specialists) can lift them
        # when stock_analyst's canonical flow was
        # ``compute_technicals`` rather than a direct
        # ``get_equity_bars`` call. A few KB of rows in the model
        # context is a cheap price for a reliable chart; the
        # prompt already tells the model to read ``result`` (the
        # aggregated pack) rather than the per-bar data.
        "bars": bars,
    }


COMPUTE_TECHNICALS = Tool(
    name="compute_technicals",
    description=(
        "Pull daily OHLCV bars for a ticker over a window and run a "
        "Python snippet over them in the in-runner interpreter. With "
        "no `code` argument, returns the standard pack: last_close, "
        "sma_20, sma_50, atr_14, rsi_14 (Wilder), macd "
        "{macd,signal,histogram} at (12,26,9), bollinger_20 "
        "{middle,upper,lower,width} at 20-period 2-sigma, "
        "recent_high, recent_low. RSI / MACD / Bollinger are "
        "deterministic over (symbol, window) -- do NOT override "
        "`code` to recompute them. Pass `code` only for indicators "
        "outside the default pack (regime classifiers, custom "
        "lookbacks, multi-symbol cross-stats); the bars list is "
        "bound as `bars` and the ticker as `symbol`; set a "
        "`result` variable to anything JSON-serializable. Stdlib "
        "only by default; the manifest's "
        "[tool.coralbricks.pipeline.py].libraries section can opt "
        "into pandas / numpy."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "start": {
                "type": "string",
                "description": "Inclusive YYYY-MM-DD.",
            },
            "end": {
                "type": "string",
                "description": "Inclusive YYYY-MM-DD.",
            },
            "code": {
                "type": "string",
                "description": (
                    "Optional Python snippet override. The bars list "
                    "is bound as `bars` and the ticker as `symbol`. "
                    "Set `result = {...}` to return a payload."
                ),
            },
        },
        "required": ["symbol", "start", "end"],
    },
    fn=_do_compute_technicals,
    bound_indices=((EQUITY_BARS_INDEX, ("sql",)),),
)


# --------------------------------------------------------------------------
# Generic in-runner Python -- arbitrary compute over arbitrary inputs
# --------------------------------------------------------------------------


def _do_run_python(
    code: str, *, inputs: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Pass-through to ``tools.py`` with caller-supplied ``inputs``.

    This is the unscoped sibling of :func:`_do_compute_technicals`.
    No index binding, no implicit data fetch -- the model authors
    the snippet AND supplies the inputs as JSON. Cheap when the
    inputs are already in the model's context (search hits, macro
    rows, a handful of numbers); use :func:`_do_compute_technicals`
    or another fetch+compute pair when the inputs would otherwise
    cost a couple of round-trips through the LLM.

    The most common use is reciprocal-rank fusion across
    BM25 + ANN tool outputs: hand the two ``hits`` lists in,
    weight them with field boosts (or 1.0/1.0), and let the
    snippet emit the fused ranking. The kernel envelope flows
    through verbatim so the model can read ``ok`` / ``error`` /
    ``took_ms`` and self-correct on a malformed snippet.
    """
    py_env = cb_tools.py(code, inputs=dict(inputs) if inputs else None)
    if not py_env.get("ok"):
        err = py_env.get("error") or {}
        return {
            "ok": False,
            "error": err.get("message") or "py snippet failed",
            "error_type": err.get("type"),
            "stdout": py_env.get("stdout", ""),
            "took_ms": py_env.get("took_ms"),
        }
    return {
        "ok": True,
        "result": py_env.get("result"),
        "stdout": py_env.get("stdout", ""),
        "globals_added": py_env.get("globals_added", []),
        "took_ms": py_env.get("took_ms"),
    }


_RUN_PYTHON_RRF_HINT = """\
Canonical reciprocal-rank fusion (k=60) across two hit lists, using
the recommended `bind_as=` pattern. The hits are returned to you
inline (so you can read titles + scores) AND bound as Python
variables, so the RRF snippet doesn't have to re-emit them:

    # Step 1: fetch + bind. You get the hits in your context AND
    # they become Python variables `bm25_hits` / `ann_hits`.
    bm25_scraped_articles(query="...", bind_as="bm25_hits")
    vector_scraped_articles(query="...", bind_as="ann_hits")

    # Step 2: fuse. Names resolve straight from runner globals --
    # no inputs= needed, so no output tokens spent re-shipping
    # the hits.
    run_python(code='''
    k = 60
    scores = {}
    for ranked in [bm25_hits["hits"], ann_hits["hits"]]:
        for rank, h in enumerate(ranked, start=1):
            scores[h["id"]] = scores.get(h["id"], 0.0) + 1.0 / (k + rank)
    fused = sorted(scores.items(), key=lambda kv: -kv[1])[:10]
    result = [{"id": i, "score": s} for i, s in fused]
    ''')

    # Step 3: hydrate the top fused IDs.
    get_full_text(ref="art:<id-from-fused>")
"""


RUN_PYTHON = Tool(
    name="run_python",
    description=(
        "Run a Python snippet in the in-runner interpreter. The "
        "snippet sees three sources of names as top-level globals:\n"
        "  1. Anything you pass via `inputs={...}` (transient -- "
        "dropped after this call).\n"
        "  2. Anything bound via `bind_as=<name>` on a prior tool "
        "call from this same specialist (PERSISTENT -- stays bound "
        "across run_python calls until you overwrite it).\n"
        "  3. Anything you set in a previous run_python snippet "
        "(persistent globals; the interpreter is stateful).\n"
        "\n"
        "Use this for reciprocal-rank fusion across search-tool "
        "outputs (RRF: score = sum_i weight_i / (60 + rank_i)), "
        "weighted score blends, dedup-by-domain on hits lists, "
        "regrouping macro / equity rows, walking a multihop "
        "subgraph, scoring chunks of a full-text doc -- any "
        "post-processing whose inputs you already pulled with a "
        "prior tool call. Set a `result` variable to anything "
        "JSON-serializable; that's what comes back. Stdlib by "
        "default; pandas + numpy are opt-in via the manifest's "
        "[tool.coralbricks.pipeline.py].libraries table.\n"
        "\n"
        "PREFER `bind_as=` over `inputs=` to ferry data here. "
        "Re-emitting an upstream tool's hits list or a filing body "
        "as an `inputs={...}` arg costs you thousands of OUTPUT "
        "tokens (the model has to reproduce every byte). If the "
        "upstream tool was called with `bind_as='hits'`, just "
        "reference `hits` directly in your snippet -- no inputs "
        "needed.\n"
        "\n"
        "DO NOT use this to re-ship voluminous data the model "
        "doesn't already have (e.g. a 250-row OHLCV pull). For "
        "those, use the dedicated server-side composition tools "
        "(e.g. compute_technicals) so the data stays inside the "
        "tool boundary.\n"
        "\n" + _RUN_PYTHON_RRF_HINT
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Python snippet. Reads names from `inputs` as "
                    "module-level globals; sets `result = ...` to "
                    "return a payload. AST-validated by the kernel; "
                    "banned imports / attrs raise a clean error."
                ),
            },
            "inputs": {
                "type": "object",
                "description": (
                    "JSON-serializable dict bound as module globals "
                    "before the snippet runs. Empty / omitted "
                    "means no pre-bound names."
                ),
            },
        },
        "required": ["code"],
    },
    fn=_do_run_python,
    bound_indices=(),
)


# --------------------------------------------------------------------------
# Options data (HISTORICAL_OPTIONS Parquet on S3)
# --------------------------------------------------------------------------


_OPTIONS_STATS_SNIPPET = """
import statistics

calls = [r for r in rows if (r.get('type') or '').lower() == 'call']
puts  = [r for r in rows if (r.get('type') or '').lower() == 'put']

call_oi = sum(float(r.get('open_interest') or 0) for r in calls)
put_oi  = sum(float(r.get('open_interest') or 0) for r in puts)
call_vol = sum(float(r.get('volume') or 0) for r in calls)
put_vol  = sum(float(r.get('volume') or 0) for r in puts)
pc_oi = round(put_oi / call_oi, 2) if call_oi > 0 else None
pc_vol = round(put_vol / call_vol, 2) if call_vol > 0 else None

def _interp(ratio):
    if ratio is None: return 'insufficient data'
    if ratio > 1.2: return f'{ratio} -- bearish (heavy put positioning)'
    if ratio < 0.7: return f'{ratio} -- bullish (heavy call positioning)'
    return f'{ratio} -- neutral'

# Spot: prefer the actual equity close from EQUITY_BARS_INDEX on
# snap_date (passed in as `spot_close`). The median OI-weighted call
# strike is only a positioning statistic, not a price -- it lands
# wherever traders are most heavily positioned, which can sit several
# percent away from real spot. Fall back to it only when the close
# lookup returned nothing (delisted ticker, holiday, missing bar).
if spot_close is not None:
    spot = round(float(spot_close), 2)
    spot_source = 'equity_close'
else:
    c_strikes = [float(r['strike']) for r in calls if float(r.get('open_interest') or 0) > 0]
    spot = round(statistics.median(c_strikes), 2) if c_strikes else None
    spot_source = 'median_call_strike_fallback' if spot is not None else 'unavailable'

# ATM IV (within 5% of spot)
iv_atm = None
if spot:
    near = [r for r in rows
            if float(r.get('implied_volatility') or 0) > 0
            and abs(float(r['strike']) - spot) / spot <= 0.05]
    if near:
        ws = [float(r.get('open_interest') or 1) for r in near]
        vs = [float(r['implied_volatility']) for r in near]
        iv_atm = round(sum(w*v for w,v in zip(ws,vs)) / sum(ws) * 100, 1)

iv_interp = 'historical IV data needed for full rank'
if iv_atm is not None:
    if iv_atm > 60: iv_interp = 'elevated -- options expensive, big move expected'
    elif iv_atm > 35: iv_interp = 'moderate -- options fairly priced'
    else: iv_interp = 'low -- options cheap, market expects calm'

# max pain — compute for the nearest future expiry only (not all
# expirations blended). Mixing all expirations dilutes the signal;
# options queries almost always target a specific expiry.
max_pain = None
max_pain_vs = None
max_pain_expiry = None
if spot and calls and puts:
    all_expiries = sorted({r.get('expiration','') for r in rows if r.get('expiration')})
    target_expiry = all_expiries[0] if all_expiries else None
    if target_expiry:
        exp_rows = [r for r in rows if r.get('expiration') == target_expiry]
        exp_calls = [r for r in exp_rows if (r.get('type') or '').lower() == 'call']
        exp_puts  = [r for r in exp_rows if (r.get('type') or '').lower() == 'put']
    else:
        exp_calls, exp_puts = calls, puts
    strikes = sorted({float(r['strike']) for r in exp_calls + exp_puts})
    call_strikes = [float(r['strike']) for r in exp_calls]
    call_ois = [float(r.get('open_interest') or 0) for r in exp_calls]
    put_strikes = [float(r['strike']) for r in exp_puts]
    put_ois = [float(r.get('open_interest') or 0) for r in exp_puts]
    best_s = None
    best_p = None
    for s in strikes:
        cp = 0.0
        for cs, oi in zip(call_strikes, call_ois):
            d = s - cs
            if d > 0:
                cp += d * oi
        pp = 0.0
        for ps, oi in zip(put_strikes, put_ois):
            d = ps - s
            if d > 0:
                pp += d * oi
        total = cp + pp
        if best_p is None or total < best_p:
            best_p = total
            best_s = s
    max_pain = round(best_s, 2)
    max_pain_expiry = target_expiry
    d = ((max_pain - spot) / spot) * 100
    max_pain_vs = f'{"above" if d >= 0 else "below"} spot by {abs(d):.1f}%'

# top OI strikes
top_call_oi = sorted(calls, key=lambda r: -float(r.get('open_interest',0)))[:3]
top_put_oi  = sorted(puts,  key=lambda r: -float(r.get('open_interest',0)))[:3]
top_c = [{'strike': round(float(r['strike']),2), 'oi': int(float(r.get('open_interest',0)))} for r in top_call_oi]
top_p = [{'strike': round(float(r['strike']),2), 'oi': int(float(r.get('open_interest',0)))} for r in top_put_oi]

result = {
    'symbol': symbol,
    'date': requested_date,
    'snapshot_date_used': snap_date,
    'stale': snap_date != requested_date,
    'spot_price': spot,
    'spot_price_source': spot_source,
    'put_call_oi_ratio': pc_oi, 'put_call_oi_interpretation': _interp(pc_oi),
    'put_call_volume_ratio': pc_vol,
    'iv_30d_atm': iv_atm, 'iv_rank_interpretation': iv_interp,
    'max_pain': max_pain, 'max_pain_expiry': max_pain_expiry, 'max_pain_vs_spot': max_pain_vs,
    'top_oi_calls': top_c, 'top_oi_puts': top_p,
    'total_call_oi': int(call_oi), 'total_put_oi': int(put_oi),
    'contract_count': len(rows),
}
""".strip()


_OPTIONS_S3_BASE = (
    "s3://coralbricks-research/research-tables/stock/alphavantage/options"
)


def _options_table_uris(symbol: str, date: str) -> dict[str, str]:
    """Narrow the options glob to a single partition file."""
    return {
        "chains": f"{_OPTIONS_S3_BASE}/symbol={symbol}/date={date}/part.parquet",
    }


def _options_table_uris_range(symbol: str, date_start: str, date_end: str) -> dict[str, str]:
    """Use a glob scoped to one symbol for date-range queries."""
    return {
        "chains": f"{_OPTIONS_S3_BASE}/symbol={symbol}/*/part.parquet",
    }


def _do_compute_options_stats(symbol: str, *, date: str) -> dict[str, Any]:
    sym = symbol.strip().upper()
    requested_date = date.strip()

    cache_key = (sym, requested_date)
    cached = _OPTIONS_STATS_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)

    target_date = requested_date

    sql = (
        f"SELECT contractID, symbol, expiration, strike, type, "
        f"last, mark, bid, ask, volume, open_interest, "
        f"implied_volatility, delta, gamma, theta, vega, rho, date "
        f"FROM chains "
        f"WHERE symbol = '{sym}' AND date = '{target_date}' "
        f"ORDER BY strike"
    )
    # The Hive partition for requested_date may not exist on S3 (most
    # commonly: same-day or weekend lookup where the daily ingest hasn't
    # landed yet). DuckDB's read_parquet() raises an HTTP 404 during
    # CREATE VIEW in that case, which propagates as RpcCallError /
    # ToolBackendError out of cb_tools.sql. Catch it and fall through
    # to the 7-day glob fallback below — same intended behaviour as
    # "query returned no rows", just via a different failure path.
    try:
        env = cb_tools.sql(
            index=OPTIONS_INDEX, query=sql,
            table_uris=_options_table_uris(sym, target_date),
        )
        rows = env.get("rows", [])
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "404" in msg or "Not Found" in msg or "failed to register parquet" in msg:
            logger.warning(
                "compute_options_stats: partition s3://.../symbol=%s/date=%s "
                "missing (HTTP 404); falling through to 7-day glob fallback",
                sym, target_date,
            )
            rows = []
        else:
            raise
    if not rows:
        # Fallback: find the most recent date with data (within 7 days)
        seven_days_ago = (
            f"STRFTIME(CAST('{requested_date}' AS DATE) - INTERVAL 7 DAY, '%Y-%m-%d')"
        )
        fallback_sql = (
            f"SELECT DISTINCT date FROM chains "
            f"WHERE symbol = '{sym}' "
            f"AND CAST(date AS VARCHAR) <= '{requested_date}' "
            f"AND CAST(date AS VARCHAR) >= {seven_days_ago} "
            f"ORDER BY date DESC LIMIT 1"
        )
        fb_env = cb_tools.sql(
            index=OPTIONS_INDEX, query=fallback_sql,
            table_uris=_options_table_uris_range(sym, "", ""),
        )
        fb_rows = fb_env.get("rows", [])
        if fb_rows and fb_rows[0].get("date"):
            target_date = fb_rows[0]["date"]
            logger.warning(
                "compute_options_stats: no chain for %s on %s; "
                "falling back to stale snapshot from %s",
                sym, requested_date, target_date,
            )
            sql = (
                f"SELECT contractID, symbol, expiration, strike, type, "
                f"last, mark, bid, ask, volume, open_interest, "
                f"implied_volatility, delta, gamma, theta, vega, rho, date "
                f"FROM chains "
                f"WHERE symbol = '{sym}' AND date = '{target_date}' "
                f"ORDER BY strike"
            )
            env = cb_tools.sql(
                index=OPTIONS_INDEX, query=sql,
                table_uris=_options_table_uris(sym, target_date),
            )
            rows = env.get("rows", [])
    if not rows:
        return {
            "error": f"No options data for {sym} within 7 days on or before {requested_date}",
            "symbol": sym,
            "date": requested_date,
        }

    # Pull the equity close on the same snapshot date the chain came from.
    # This is the authoritative spot price; the snippet uses it instead of
    # the median-strike heuristic that previously stood in for spot.
    spot_close = None
    try:
        spot_env = cb_tools.sql(
            index=EQUITY_BARS_INDEX,
            query=(
                f"SELECT close FROM bars "
                f"WHERE symbol = '{sym}' AND date = '{target_date}' "
                f"LIMIT 1"
            ),
        )
        spot_rows = spot_env.get("rows", [])
        if spot_rows and spot_rows[0].get("close") is not None:
            spot_close = float(spot_rows[0]["close"])
    except Exception:
        logger.debug(
            "compute_options_stats: equity_bars close lookup failed for %s on %s",
            sym, target_date,
        )

    py_env = cb_tools.py(
        _OPTIONS_STATS_SNIPPET,
        inputs={
            "rows": rows,
            "symbol": sym,
            "snap_date": target_date,
            "requested_date": requested_date,
            "spot_close": spot_close,
        },
    )
    if not py_env.get("ok"):
        err = py_env.get("error") or {}
        # Don't cache error states — they may be transient.
        return {
            "symbol": sym,
            "date": requested_date,
            "snapshot_date_used": target_date,
            "stale": target_date != requested_date,
            "error": err.get("message") or "stats computation failed",
        }

    result = py_env.get("result", {})
    _OPTIONS_STATS_CACHE[cache_key] = result
    return copy.deepcopy(result)


COMPUTE_OPTIONS_STATS = Tool(
    name="compute_options_stats",
    description=(
        "Pre-computed options positioning stats for a US equity ticker. "
        "Returns put/call ratio (OI- and volume-weighted), IV rank, "
        "expected move (nearest-expiry straddle), max pain, IV skew "
        "(25-delta put vs call), and top-3 OI strikes for calls and puts. "
        "Use to assess whether a move is priced in, gauge sentiment, and "
        "find options-implied support/resistance. Call at most ONCE per "
        "ticker (cached server-side). The response also reports "
        "`snapshot_date_used` (the actual chain date served, which may "
        "trail the requested date by up to 7 days when the exact-day "
        "chain is missing) and `stale: true` when that fallback fired -- "
        "treat the spot/IV/positioning as describing `snapshot_date_used`, "
        "not the requested date. `spot_price_source` is `equity_close` "
        "when read from the daily-bars index (the normal path) and "
        "`median_call_strike_fallback` if the bar lookup failed -- the "
        "fallback can drift several percent from real spot, so downstream "
        "predictions should de-rate confidence accordingly."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker, e.g. AAPL"},
            "date": {"type": "string", "description": "Snapshot date YYYY-MM-DD"},
        },
        "required": ["symbol", "date"],
    },
    fn=_do_compute_options_stats,
    bound_indices=(),
)


def _do_get_options_chain(
    symbol: str,
    *,
    date: str,
    contract_type: Optional[str] = None,
    min_strike: Optional[float] = None,
    max_strike: Optional[float] = None,
    expiry_min: Optional[str] = None,
    expiry_max: Optional[str] = None,
    min_volume: Optional[int] = None,
    min_oi: Optional[int] = None,
    sort_by: str = "strike",
    max_rows: int = 200,
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    target_date = date.strip()

    # Normalised cache key — match exactly the args that affect the SQL
    # below so different filter combos don't collide.
    ct_key = contract_type.strip().lower() if contract_type else None
    sort_key = sort_by if sort_by else "strike"
    cache_key = (
        sym, target_date, ct_key, min_strike, max_strike,
        expiry_min, expiry_max, min_volume, min_oi, sort_key, int(max_rows),
    )
    cached = _OPTIONS_CHAIN_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)

    wheres = [f"symbol = '{sym}'", f"date = '{target_date}'"]
    if contract_type and contract_type.lower() in ("call", "put"):
        wheres.append(f"LOWER(type) = '{contract_type.lower()}'")
    if min_strike is not None:
        wheres.append(f"strike >= {min_strike}")
    if max_strike is not None:
        wheres.append(f"strike <= {max_strike}")
    if expiry_min:
        wheres.append(f"expiration >= '{expiry_min}'")
    if expiry_max:
        wheres.append(f"expiration <= '{expiry_max}'")
    if min_volume is not None:
        wheres.append(f"volume >= {min_volume}")
    if min_oi is not None:
        wheres.append(f"open_interest >= {min_oi}")

    valid_sorts = {"strike", "volume", "open_interest", "implied_volatility", "delta", "gamma", "expiration"}
    order_col = sort_by if sort_by in valid_sorts else "strike"
    desc = " DESC" if order_col in ("volume", "open_interest") else ""

    table_uris = _options_table_uris(sym, target_date)
    sql = (
        f"SELECT contractID, symbol, expiration, strike, type, "
        f"last, mark, bid, ask, volume, open_interest, "
        f"implied_volatility, delta, gamma, theta, vega, rho "
        f"FROM chains "
        f"WHERE {' AND '.join(wheres)} "
        f"ORDER BY {order_col}{desc} "
        f"LIMIT {min(max_rows, 500)}"
    )
    env = cb_tools.sql(
        index=OPTIONS_INDEX, query=sql,
        table_uris=table_uris,
    )
    rows = env.get("rows", [])
    result: dict[str, Any] = {
        "symbol": sym,
        "date": target_date,
        "row_count": len(rows),
        "rows": rows,
    }
    if not rows:
        # Self-diagnosing empty result: tell the caller which expirations
        # actually exist for this (symbol, date), so the LLM can retry
        # without flailing.
        diag_env = cb_tools.sql(
            index=OPTIONS_INDEX,
            query=(
                f"SELECT DISTINCT expiration FROM chains "
                f"WHERE symbol = '{sym}' AND date = '{target_date}' "
                f"ORDER BY expiration"
            ),
            table_uris=table_uris,
        )
        available = [
            r.get("expiration") for r in diag_env.get("rows", [])
            if r.get("expiration") is not None
        ]
        result["available_expirations"] = available
        if not available:
            result["note"] = (
                f"No chain data found for {sym} on {target_date}. "
                f"Verify the snapshot date is a trading day with loaded data."
            )
        elif expiry_min or expiry_max:
            result["note"] = (
                f"No contracts matched the expiry window "
                f"[{expiry_min or '-inf'}, {expiry_max or '+inf'}]. "
                f"See available_expirations and retry."
            )
        else:
            result["note"] = (
                "No contracts matched the supplied filters "
                "(contract_type/strike/volume/oi). Loosen filters and retry."
            )
    _OPTIONS_CHAIN_CACHE[cache_key] = result
    return copy.deepcopy(result)


GET_OPTIONS_CHAIN = Tool(
    name="get_options_chain",
    description=(
        "Raw options chain for a US equity ticker on a given date. "
        "Returns individual contracts with strike, expiration, bid/ask, "
        "volume, open interest, IV, and Greeks (delta, gamma, theta, "
        "vega, rho). Use compute_options_stats for pre-computed "
        "positioning summaries; use this when you need contract-level "
        "data (specific strikes, expiry filtering, unusual volume "
        "screening). Expiry is filtered as an inclusive range via "
        "expiry_min / expiry_max; pass either or both. When the result "
        "is empty, available_expirations lists every expiration loaded "
        "for (symbol, date) so you can retry without guessing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker, e.g. AAPL"},
            "date": {"type": "string", "description": "Trading date YYYY-MM-DD"},
            "contract_type": {
                "type": "string",
                "description": "Optional: 'call' or 'put'",
            },
            "min_strike": {"type": "number", "description": "Optional: min strike"},
            "max_strike": {"type": "number", "description": "Optional: max strike"},
            "expiry_min": {
                "type": "string",
                "description": "Optional YYYY-MM-DD: keep contracts with expiration >= this date (inclusive lower bound).",
            },
            "expiry_max": {
                "type": "string",
                "description": "Optional YYYY-MM-DD: keep contracts with expiration <= this date (inclusive upper bound). For an exact expiry, set expiry_min == expiry_max.",
            },
            "min_volume": {"type": "integer", "description": "Optional: min volume"},
            "min_oi": {"type": "integer", "description": "Optional: min open interest"},
            "sort_by": {
                "type": "string",
                "description": "strike (default), volume, open_interest, implied_volatility, delta, gamma, expiration",
            },
            "max_rows": {"type": "integer", "description": "Max contracts (default 200)"},
        },
        "required": ["symbol", "date"],
    },
    fn=_do_get_options_chain,
    bound_indices=(),
)


# --------------------------------------------------------------------------
# compute_market_cap / compute_float / fetch_insider_trades
# --------------------------------------------------------------------------
#
# Micro-cap screening primitives. These three tools fill the gap that
# caused the swarm to refuse "find me X-bagger micro-caps" queries: the
# stock_analyst persona had no way to pull market-cap, public float, or
# Form-4 insider-buying data.
#
# Implementation strategy: ride on the existing sec-api.io plumbing.
# - compute_market_cap = recent shares-outstanding (cover-page XBRL)
#   times latest close from get_equity_bars.
# - compute_float = dei:EntityPublicFloat from the latest 10-K cover
#   page. (Quarterlies do not tag float.)
# - fetch_insider_trades = sec-api.io /insider-trading endpoint, the
#   canonical Form-4 transaction feed. Same host as the existing
#   manifest egress entry, so no manifest change needed.

_SEC_API_INSIDER_URL = "https://api.sec-api.io/insider-trading"

_PUBLIC_FLOAT_CONCEPT = "EntityPublicFloat"

_MARKET_CAP_TIERS = (
    ("mega",   200_000_000_000),
    ("large",   10_000_000_000),
    ("mid",      2_000_000_000),
    ("small",      300_000_000),
    ("micro",       50_000_000),
    ("nano",                 0),
)


def _classify_market_cap(usd: float) -> str:
    for label, floor in _MARKET_CAP_TIERS:
        if usd >= floor:
            return label
    return "nano"


def _most_recent_shares_filing(
    ticker: str,
    asof: Optional[str],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Find the most-recent 10-K or 10-Q for ``ticker`` ending at ``asof``.

    Returns ``(filing_dict, error)``. The filing dict carries at minimum
    ``accessionNo``, ``filedAt``, ``formType``.
    """
    from datetime import date as _d, timedelta as _td  # noqa: PLC0415
    today = asof or _d.today().isoformat()
    # Window: 18 months back to be safe across quarterly filers.
    window_start = (_d.fromisoformat(today[:10]) - _td(days=540)).isoformat()
    # Prefer 10-Q (more recent share count); fall back to 10-K.
    for form in ("10-Q", "10-K", "20-F", "40-F"):
        filings, err = _list_sec_filings(
            ticker, form, window_start, today, max_results=20,
        )
        if err:
            continue
        if filings:
            # filings come back sorted ascending by filedAt; pick latest.
            filings.sort(key=lambda f: f.get("filedAt") or "", reverse=True)
            return filings[0], None
    return None, f"no 10-K / 10-Q / 20-F / 40-F found for {ticker} in [{window_start}, {today}]"


def _do_compute_market_cap(
    ticker: str,
    *,
    asof: Optional[str] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Compute approximate market cap = shares outstanding x latest close.

    Pulls per-class ``EntityCommonStockSharesOutstanding`` from the most
    recent 10-K / 10-Q / 20-F cover page (sums across all classes), then
    multiplies by the latest available close from ``get_equity_bars``.
    Returns a tier classification (mega / large / mid / small / micro /
    nano) suitable for screening.

    ``asof`` (YYYY-MM-DD) clamps both the filing search and the price
    lookup to ``<= asof``. Defaults to today.
    """
    from datetime import date as _d, timedelta as _td  # noqa: PLC0415
    t = ticker.upper().strip()
    today = asof or _d.today().isoformat()

    filing, err = _most_recent_shares_filing(t, today)
    if err or not filing:
        out = {"ticker": t, "asof": today, "error": err or "no filing"}
        return _apply_binding(bind_as, out)

    accession = filing.get("accessionNo", "")
    ref = f"sec:{accession}"
    facts = _do_get_xbrl_facts(
        ref,
        concept_pattern=_COVER_PAGE_SHARES_CONCEPT,
        limit=20,
    )
    if facts.get("error"):
        out = {
            "ticker": t, "asof": today,
            "source_filing_accession": accession,
            "source_filing_filedAt": filing.get("filedAt"),
            "error": f"xbrl facts: {facts['error']}",
        }
        return _apply_binding(bind_as, out)

    classes: list[dict[str, Any]] = []
    total_shares = 0
    for f in facts.get("facts", []) or []:
        section = str(f.get("section") or "")
        if section and section.lower() != "coverpage":
            continue
        try:
            n = int(str(f.get("value")).replace(",", ""))
        except (TypeError, ValueError):
            continue
        seg = f.get("segment") or []
        cls = _class_letter_from_segment(seg)
        classes.append({"class": cls, "shares": n})
        total_shares += n

    if not total_shares:
        out = {
            "ticker": t, "asof": today,
            "source_filing_accession": accession,
            "source_filing_filedAt": filing.get("filedAt"),
            "error": "cover-page share-count not found in iXBRL facts",
        }
        return _apply_binding(bind_as, out)

    # Pull latest close <= asof. Look back 14 days to handle weekends /
    # market holidays.
    price_start = (_d.fromisoformat(today[:10]) - _td(days=14)).isoformat()
    bars = _do_get_equity_bars(t, start=price_start, end=today)
    rows = bars.get("rows", []) or []
    if not rows:
        out = {
            "ticker": t, "asof": today,
            "source_filing_accession": accession,
            "source_filing_filedAt": filing.get("filedAt"),
            "shares_outstanding": total_shares,
            "classes": classes,
            "error": f"no equity bars for {t} in [{price_start}, {today}]",
        }
        return _apply_binding(bind_as, out)
    last = rows[-1]
    price = float(last.get("close") or 0.0)
    if price <= 0:
        out = {
            "ticker": t, "asof": today,
            "source_filing_accession": accession,
            "source_filing_filedAt": filing.get("filedAt"),
            "shares_outstanding": total_shares,
            "classes": classes,
            "error": "latest close is zero or missing",
        }
        return _apply_binding(bind_as, out)

    market_cap = float(total_shares) * price
    out = {
        "ticker": t,
        "asof": today,
        "shares_outstanding": total_shares,
        "classes": classes,
        "price_usd": price,
        "price_date": str(last.get("date") or ""),
        "market_cap_usd": market_cap,
        "market_cap_classification": _classify_market_cap(market_cap),
        "source_filing_accession": accession,
        "source_filing_filedAt": filing.get("filedAt"),
        "source_filing_formType": filing.get("formType"),
    }
    return _apply_binding(bind_as, out)


COMPUTE_MARKET_CAP = Tool(
    name="compute_market_cap",
    description=(
        "Compute approximate USD market capitalisation = cover-page "
        "shares outstanding (summed across all classes) x latest close. "
        "Uses the most-recent 10-K / 10-Q / 20-F cover page for share "
        "count and equity bars for price. Returns a tier classification "
        "(mega / large / mid / small / micro / nano) suitable for "
        "screening micro-cap or large-cap subsets."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "asof": {
                "type": "string",
                "description": "Optional YYYY-MM-DD clamp. Defaults to today.",
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker"],
    },
    fn=_do_compute_market_cap,
    bound_indices=((EQUITY_BARS_INDEX, ("sql",)),),
)


def _do_compute_float(
    ticker: str,
    *,
    asof: Optional[str] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Return public float (USD) from the most recent 10-K cover page.

    The SEC mandates that 10-K filers disclose
    ``dei:EntityPublicFloat`` on the cover page, measured as of the
    last business day of the most recently completed second fiscal
    quarter. Quarterlies (10-Q) do not tag public float. Foreign
    private issuers on 20-F similarly disclose float.
    """
    from datetime import date as _d, timedelta as _td  # noqa: PLC0415
    t = ticker.upper().strip()
    today = asof or _d.today().isoformat()
    # 10-K cadence is annual; window back 18 months to be safe.
    window_start = (_d.fromisoformat(today[:10]) - _td(days=540)).isoformat()
    filing = None
    for form in ("10-K", "20-F", "40-F"):
        filings, err = _list_sec_filings(t, form, window_start, today, max_results=10)
        if err:
            continue
        if filings:
            filings.sort(key=lambda f: f.get("filedAt") or "", reverse=True)
            filing = filings[0]
            break
    if filing is None:
        out = {"ticker": t, "asof": today, "error": "no 10-K / 20-F / 40-F in 18-month window"}
        return _apply_binding(bind_as, out)
    accession = filing.get("accessionNo", "")
    ref = f"sec:{accession}"
    facts = _do_get_xbrl_facts(
        ref,
        concept_pattern=_PUBLIC_FLOAT_CONCEPT,
        limit=10,
    )
    if facts.get("error"):
        out = {
            "ticker": t, "asof": today,
            "source_filing_accession": accession,
            "source_filing_filedAt": filing.get("filedAt"),
            "error": f"xbrl facts: {facts['error']}",
        }
        return _apply_binding(bind_as, out)
    raw = facts.get("facts", []) or []
    float_usd = None
    measurement_date = None
    for f in raw:
        section = str(f.get("section") or "")
        if section and section.lower() != "coverpage":
            continue
        try:
            float_usd = float(str(f.get("value")).replace(",", ""))
        except (TypeError, ValueError):
            continue
        period = f.get("period") or {}
        measurement_date = period.get("instant") or period.get("endDate")
        break
    if float_usd is None:
        out = {
            "ticker": t, "asof": today,
            "source_filing_accession": accession,
            "source_filing_filedAt": filing.get("filedAt"),
            "error": (
                "EntityPublicFloat not found in cover-page facts. "
                "Smaller reporting companies sometimes omit it; fall "
                "back to get_full_text on the cover page."
            ),
        }
        return _apply_binding(bind_as, out)
    # Float classification thresholds match the screening heuristic in
    # common low-float scanners.
    if float_usd < 50_000_000:
        classification = "very_low"
    elif float_usd < 200_000_000:
        classification = "low"
    elif float_usd < 2_000_000_000:
        classification = "mid"
    else:
        classification = "high"
    out = {
        "ticker": t,
        "asof": today,
        "public_float_usd": float_usd,
        "float_classification": classification,
        "measurement_date": measurement_date,
        "source_filing_accession": accession,
        "source_filing_filedAt": filing.get("filedAt"),
        "source_filing_formType": filing.get("formType"),
    }
    return _apply_binding(bind_as, out)


COMPUTE_FLOAT = Tool(
    name="compute_float",
    description=(
        "Return the issuer's public float in USD from the most recent "
        "10-K / 20-F / 40-F cover page (dei:EntityPublicFloat). "
        "Measurement date is the last business day of the most "
        "recently completed second fiscal quarter at the time of "
        "filing. Returns a float classification (very_low / low / mid / "
        "high) suitable for low-float screening."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "asof": {
                "type": "string",
                "description": "Optional YYYY-MM-DD clamp on filing-search end date.",
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker"],
    },
    fn=_do_compute_float,
    bound_indices=(),
)


_INSIDER_BUY_CODES = {"P"}   # Form-4 transaction code "P" = open-market purchase
_INSIDER_SELL_CODES = {"S"}  # Form-4 transaction code "S" = open-market sale
_INSIDER_RECENT_LIMIT = 10   # cap returned transactions for response size


def _do_fetch_insider_trades(
    ticker: str,
    *,
    days: int = 30,
    asof: Optional[str] = None,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Aggregate Form-4 insider transactions for ``ticker`` over ``days``.

    Queries sec-api.io's ``/insider-trading`` endpoint. Splits the
    transaction stream into open-market buys (code ``P``) and sells
    (code ``S``); ignores grants, awards, gifts, derivative
    transactions. Returns counts, total dollar volume per side, and a
    short list of the most recent ``_INSIDER_RECENT_LIMIT``
    transactions.
    """
    from datetime import date as _d, timedelta as _td  # noqa: PLC0415
    t = ticker.upper().strip()
    today = asof or _d.today().isoformat()
    days = max(1, min(int(days or 30), 365))
    window_start = (_d.fromisoformat(today[:10]) - _td(days=days)).isoformat()
    api_key = os.environ.get("SEC_API_KEY", "").strip()
    if not api_key:
        return _apply_binding(bind_as, {
            "ticker": t, "asof": today,
            "error": "SEC_API_KEY missing from sandbox env",
        })
    body = json.dumps({
        "query": (
            f'issuer.tradingSymbol:"{t}" '
            f'AND nonDerivativeTable.transactions.transactionDate:'
            f'[{window_start} TO {today}]'
        ),
        "from": "0",
        "size": "50",
        "sort": [{"filedAt": {"order": "desc"}}],
    }).encode("utf-8")
    url = f"{_SEC_API_INSIDER_URL}?token={urllib.parse.quote(api_key)}"
    req = urllib.request.Request(url=url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=_SEC_API_TIMEOUT_S, context=_SEC_API_SSL_CTX) as resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                import gzip  # noqa: PLC0415
                raw = gzip.decompress(raw)
            doc = json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001
            pass
        return _apply_binding(bind_as, {
            "ticker": t, "asof": today,
            "error": f"sec-api insider HTTP {exc.code}: {text or exc.reason}",
        })
    except urllib.error.URLError as exc:
        return _apply_binding(bind_as, {
            "ticker": t, "asof": today,
            "error": f"sec-api insider unreachable: {exc.reason}",
        })
    except Exception as exc:  # noqa: BLE001
        return _apply_binding(bind_as, {
            "ticker": t, "asof": today,
            "error": f"sec-api insider parse error: {exc}",
        })

    filings = doc.get("data") or doc.get("transactions") or doc.get("filings") or []
    buy_count = sell_count = 0
    buy_usd = sell_usd = 0.0
    recent: list[dict[str, Any]] = []
    for f in filings:
        reporter = (
            f.get("reportingOwner", {}).get("name")
            or f.get("ownerName")
            or ""
        )
        relationship = (
            f.get("reportingOwner", {}).get("relationship", {})
            or f.get("relationship", {})
            or {}
        )
        is_director = bool(relationship.get("isDirector"))
        is_officer = bool(relationship.get("isOfficer"))
        officer_title = relationship.get("officerTitle") or ""
        non_deriv = (
            f.get("nonDerivativeTable", {}).get("transactions")
            or f.get("nonDerivativeTransactions")
            or []
        )
        for tx in non_deriv:
            code = (tx.get("coding", {}) or {}).get("code") or tx.get("transactionCode") or ""
            shares = (tx.get("amounts", {}) or {}).get("shares") or tx.get("shares") or 0
            price = (tx.get("amounts", {}) or {}).get("pricePerShare") or tx.get("pricePerShare") or 0
            tx_date = tx.get("transactionDate") or f.get("periodOfReport") or ""
            try:
                shares = float(shares or 0)
                price = float(price or 0)
            except (TypeError, ValueError):
                continue
            value = shares * price
            entry = {
                "date": str(tx_date),
                "reporter": reporter,
                "is_director": is_director,
                "is_officer": is_officer,
                "officer_title": officer_title,
                "code": code,
                "shares": shares,
                "price_usd": price,
                "value_usd": value,
            }
            if code in _INSIDER_BUY_CODES:
                buy_count += 1
                buy_usd += value
                entry["side"] = "buy"
                recent.append(entry)
            elif code in _INSIDER_SELL_CODES:
                sell_count += 1
                sell_usd += value
                entry["side"] = "sell"
                recent.append(entry)
    recent.sort(key=lambda r: r.get("date", ""), reverse=True)
    out = {
        "ticker": t,
        "asof": today,
        "window_days": days,
        "window_start": window_start,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_buy_usd": buy_usd,
        "total_sell_usd": sell_usd,
        "net_usd": buy_usd - sell_usd,
        "transactions_count": len(recent),
        "recent_transactions": recent[:_INSIDER_RECENT_LIMIT],
    }
    return _apply_binding(bind_as, out)


FETCH_INSIDER_TRADES = Tool(
    name="fetch_insider_trades",
    description=(
        "Aggregate open-market Form-4 insider transactions (codes P / "
        "S) for a US-listed issuer over the trailing N days. Returns "
        "buy count, sell count, total USD value per side, net USD, and "
        "the 10 most recent transactions. Use to detect insider-buying "
        "signals on micro-cap / momentum screens, or to flag concentrated "
        "selling around earnings. Grants, awards, and derivative "
        "transactions are excluded."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "days": {
                "type": "integer",
                "description": "Trailing window in days (default 30, max 365).",
            },
            "asof": {
                "type": "string",
                "description": "Optional YYYY-MM-DD clamp on the trailing window's end.",
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker"],
    },
    fn=_do_fetch_insider_trades,
    bound_indices=(),
)


# --------------------------------------------------------------------------
# Per-specialist rosters
# --------------------------------------------------------------------------

# Roster shape mirrors gdelt.project.agents.lg_specialists so the
# personas can be lifted across without rebalancing tool budgets.
# Tools are referenced by frozen dataclass instance, NOT by name, so
# accidentally renaming one in a roster fails at import time rather
# than at runtime when the model issues the call.

STOCK_ANALYST_TOOLS: tuple[Tool, ...] = (
    GET_EQUITY_BARS,
    COMPUTE_PRICE_RETURNS_MULTI,
    COMPUTE_TECHNICALS,
    COMPUTE_OPTIONS_STATS,
    GET_OPTIONS_CHAIN,
    GET_MACRO_SERIES,
    COMPUTE_MARKET_CAP,
    COMPUTE_FLOAT,
    FETCH_INSIDER_TRADES,
    BM25_SEC,
    GET_FULL_TEXT,
    # GET_REDDIT_SENTIMENT + SEARCH_REDDIT_POSTS were briefly removed
    # 2026-04-28 (alphacumen 0.0.34) on the theory that empty Reddit results
    # were "wasted" tool calls. Three subsequent runs showed:
    #   - the LLM filled the freed steps with bm25_sec + get_full_text
    #     chasing irrelevant 8-Ks (e.g. NVDA's exec-comp 5.02 filing)
    #   - stock_analyst hit max_steps=6 without producing a final
    #     answer in one of the three runs (success rate dropped)
    #   - total swarm latency *worsened*: 14.9 s → 17–26 s
    # Restored 2026-04-28 (alphacumen 0.0.35). The cheap Reddit-empty calls
    # were acting as planning rails that kept the LLM out of trouble.
    GET_REDDIT_SENTIMENT,
    SEARCH_REDDIT_POSTS,
)

# query_graph re-added in 0.0.56 but reverted: the DuckDB pool
# fixes cold-start, but complex multi-join queries (5-way
# actor→mentions_org→article→mentions_org→actor) still exceed the
# 45s RPC timeout scanning millions of parquet rows. Needs
# pre-materialized co-mention views or query-complexity limits
# before it's safe in a 6-step budget.
SECTOR_ANALYST_TOOLS: tuple[Tool, ...] = (
    BM25_SEC,
    GET_FULL_TEXT,
    GET_XBRL_FACTS,
    EXTRACT_FILING_TABLES,
    EXTRACT_FILING_DECK_TEXT,
    FIND_SEC_FILING_EDGAR,
    GET_COVER_PAGE_SHARE_COUNTS,
    GET_REGISTERED_SECURITIES,
    FIND_QUARTERLY_EARNINGS_8KS,
    COMPUTE_PRICE_RETURNS_MULTI,
    VECTOR_SCRAPED_ARTICLES,
    GET_MACRO_SERIES,
    RUN_PYTHON,
)

# Skills-variant roster lives below, after ``INVOKE_SKILL_FN`` is
# defined -- see ``SECTOR_ANALYST_TOOLS_SLIM`` near the bottom of
# this module.

VC_ANALYST_TOOLS: tuple[Tool, ...] = (
    VECTOR_SCRAPED_ARTICLES,
    BM25_SCRAPED_ARTICLES,
    BM25_GDELT,
    GET_FULL_TEXT,
    RUN_PYTHON,
)

# multihop_graph is deliberately NOT in the risk roster. The DuckDB-
# over-S3 backend has a ~15s cold-start penalty per connection
# (httpfs install + parquet metadata read), and a typical risk run
# hits it twice: once for the name->id resolver SQL and again for
# the BFS itself. That ~25s adds no signal the bm25_gdelt + scraped-
# article + macro legs don't already carry. Memory-demo's production
# risk roster makes the same call. Re-add once the gateway caches
# DuckDB connections per index.
RISK_ANALYST_TOOLS: tuple[Tool, ...] = (
    BM25_GDELT,
    VECTOR_SCRAPED_ARTICLES,
    GET_FULL_TEXT,
    GET_MACRO_SERIES,
    RUN_PYTHON,
)


# ---------------------------------------------------------------------------
# Grok analyst — single-tool specialist that queries xAI's Grok API
# with web search for real-time event chronology and news coverage.
# ---------------------------------------------------------------------------

def _call_grok(query: str) -> dict[str, Any]:
    """Call Grok via the gateway's RPC channel (tools.grok kernel verb).

    The gateway holds the XAI_API_KEY and a warm httpx pool; the
    sandbox just sends the query over the proven UDS RPC channel.
    """
    try:
        result = cb_tools.grok(query=query)
    except Exception as exc:
        return {"error": f"tools.grok RPC failed: {exc!s}", "answer": "", "sources": []}
    return {
        "answer": result.get("answer", ""),
        "sources": result.get("sources", []),
    }


ASK_GROK = Tool(
    name="ask_grok",
    description=(
        "Query Grok (xAI) with real-time web search. Use this tool to get "
        "an independent research perspective on the user's question. Grok "
        "has access to live web data and is particularly strong on event "
        "chronology, breaking news, and recent developments."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The research question to send to Grok.",
            },
        },
        "required": ["query"],
    },
    fn=_call_grok,
)

GROK_ANALYST_TOOLS: tuple[Tool, ...] = (ASK_GROK,)


# INVOKE_SKILL_FN, LOAD_SKILL, LOAD_PLANNER_SKILL (and their
# _do_* executors) moved to :mod:`reef.skill_tools` and
# are imported at the top of this module. The rosters below pick
# up the same Tool instances from there.


# ---------------------------------------------------------------------------
# Specialist dispatch tools -- the planner emits one of these per unit of
# work instead of writing an `invoke_next` JSON field. The tool's ``fn``
# is a sentinel (returns a status string); the swarm loop walks the
# planner's trajectory after run_react completes, collects each
# dispatch_* call, and runs the named specialists in parallel.
#
# One tool per persona keeps the schema self-describing: the model sees
# the persona's brief in the tool description and emits typed args.
# ---------------------------------------------------------------------------


# Personas that always operate on a single named issuer / ticker.
# Their dispatch tool requires `ticker`; the rest treat it as optional
# (sector-wide or non-ticker subjects are common for vc_/risk_).
_TICKER_REQUIRED_PERSONAS = frozenset({
    "sector_analyst",
    "stock_analyst",
    "news_quant_analyst",
})


def _make_dispatch_tool(
    persona_key: str,
    label: str,
    brief: str,
) -> "Tool":
    """Build a `dispatch_<persona>` tool whose `fn` is a sentinel."""
    ticker_required = persona_key in _TICKER_REQUIRED_PERSONAS

    def _fn(
        instruction: str,
        ticker: Optional[str] = None,
        max_steps: Optional[int] = None,
    ) -> str:
        # No-op: the swarm loop reads this call out of the planner's
        # trajectory and runs the specialist out-of-band. The return
        # string just gives the planner a non-error response so it
        # can keep emitting more dispatches in the same round.
        head = f"queued: {persona_key}"
        if ticker:
            head += f" ticker={ticker}"
        return f"{head} (max_steps={max_steps or 6})"

    ticker_desc = (
        "Single US-listed ticker this dispatch covers (e.g. 'FTAI'). "
        "ONE ticker per call -- for multi-ticker queries, emit N "
        "parallel `dispatch_" + persona_key + "` calls."
    )
    if not ticker_required:
        ticker_desc += (
            " Optional for this persona: omit when the dispatch "
            "covers a sector, topic, or non-US-listed subject."
        )

    properties = {
        "ticker": {"type": "string", "description": ticker_desc},
        "instruction": {
            "type": "string",
            "description": (
                "OUTCOME-framed instruction: name the financial "
                "concept the specialist must produce (e.g. "
                "'Compute P/B, D/E, D/Cap as of 2025-09-30; market "
                "price as of 2025-12-31'). Do NOT prescribe specific "
                "tools or break the concept into primitives -- the "
                "specialist owns its retrieval recipe."
            ),
        },
        "max_steps": {
            "type": "integer",
            "description": (
                "Tool budget cap for this specialist (default 6, "
                "max 14). Raise only when a single dispatch "
                "genuinely has to cover multiple entities."
            ),
        },
    }
    required = ["instruction"] + (["ticker"] if ticker_required else [])

    return Tool(
        name=f"dispatch_{persona_key}",
        description=f"Dispatch a query to the **{label}**. {brief}",
        parameters={
            "type": "object",
            "properties": properties,
            "required": required,
        },
        fn=_fn,
    )


def build_planner_dispatch_tools(
    roster_keys: Sequence[str],
    specialist_configs: Mapping[str, Any],
    specialist_briefs: Mapping[str, str],
) -> tuple["Tool", ...]:
    """Construct the planner's dispatch tools for the active roster.

    Lives in tools.py to keep tool factories colocated; called from
    swarm.py (which owns SPECIALIST_CONFIGS / SPECIALIST_BRIEFS) so
    the dependency direction stays specialists -> tools.
    """
    out: list[Tool] = []
    for k in roster_keys:
        cfg = specialist_configs.get(k)
        if cfg is None:
            continue
        label = getattr(cfg, "label", k)
        brief = specialist_briefs.get(k) or label
        out.append(_make_dispatch_tool(k, label, brief))
    return tuple(out)


# Sector_analyst's live roster. Each recipe migrated to
# ``alphacumen/sector/skills/<slug>/`` (with an ``impl.py`` registering
# ``@skill_fn`` callables) drops its corresponding Tool here -- the
# model reaches it through ``INVOKE_SKILL_FN`` instead, so the schema
# prefix the LLM sees doesn't grow with each new recipe. Universal
# workhorses (bm25_sec / get_full_text / extract_filing_tables /
# get_xbrl_facts / run_python) and seed-level utilities
# (find_sec_filing_edgar / get_registered_securities /
# compute_price_returns_multi / vector_scraped_articles /
# get_macro_series) stay first-class because they're called outside
# any skill context.
#
# ``SECTOR_ANALYST_TOOLS`` (above) stays exported for external
# gdelt experiments that took a snapshot of the full pre-migration
# surface; sector_analyst's own ``SpecialistConfig.tools`` points at
# this slim tuple instead.
#
# Migration accounting -- drop from this tuple when the matching skill
# lands in folder shape with a registered ``@skill_fn`` callable:
#   - 2026-06-01: removed 22 recipe-bound tools (debt_refi_impact canary
#     + 21 in the broad sweep across 19 skill folders). The slim roster
#     shrank from 34 to 13 schemas in one cut. Every dropped tool now
#     dispatches through INVOKE_SKILL_FN -> alphacumen.sector.skill_fn registry.
SECTOR_ANALYST_TOOLS_SLIM: tuple[Tool, ...] = (
    BM25_SEC,
    GET_FULL_TEXT,
    GET_XBRL_FACTS,
    EXTRACT_FILING_TABLES,
    EXTRACT_FILING_DECK_TEXT,
    FIND_SEC_FILING_EDGAR,
    GET_COVER_PAGE_SHARE_COUNTS,
    GET_REGISTERED_SECURITIES,
    FIND_QUARTERLY_EARNINGS_8KS,
    VECTOR_SCRAPED_ARTICLES,
    GET_MACRO_SERIES,
    RUN_PYTHON,
    LOAD_SKILL,
    INVOKE_SKILL_FN,
)


ALL_TOOLS: tuple[Tool, ...] = (
    BM25_GDELT,
    BM25_SEC,
    BM25_SCRAPED_ARTICLES,
    VECTOR_SCRAPED_ARTICLES,
    GET_FULL_TEXT,
    GET_XBRL_FACTS,
    EXTRACT_FILING_TABLES,
    EXTRACT_FILING_DECK_TEXT,
    FIND_SEC_FILING_EDGAR,
    GET_COVER_PAGE_SHARE_COUNTS,
    GET_REGISTERED_SECURITIES,
    GET_MACRO_SERIES,
    GET_EQUITY_BARS,
    GET_REDDIT_SENTIMENT,
    SEARCH_REDDIT_POSTS,
    COMPUTE_TECHNICALS,
    COMPUTE_PRICE_RETURNS_MULTI,
    RUN_PYTHON,
    LOAD_SKILL,
    INVOKE_SKILL_FN,
)
"""Convenience flat list -- tests / introspection."""


def lookup_tool(name: str, tools: Sequence[Tool] = ALL_TOOLS) -> Tool:
    """Find a :class:`Tool` by name within a roster.

    Convenience wrapper over the framework-level
    :func:`reef.tool.lookup_tool` that defaults ``tools`` to
    :data:`ALL_TOOLS` (the finance roster) -- existing callers and the
    test suite rely on the default.
    """
    return _harness_lookup_tool(name, tools)


# bind_tools is re-exported from reef.tool at the top of this
# module so existing importers (``from alphacumen.tools import bind_tools``)
# resolve through here unchanged.


__all__ = [
    "ALL_TOOLS",
    "BM25_GDELT",
    "BM25_SCRAPED_ARTICLES",
    "BM25_SEC",
    "COMPUTE_PRICE_RETURNS_MULTI",
    "COMPUTE_TECHNICALS",
    "EXTRACT_FILING_DECK_TEXT",
    "EXTRACT_FILING_TABLES",
    "FIND_QUARTERLY_EARNINGS_8KS",
    "FIND_SEC_FILING_EDGAR",
    "GET_COVER_PAGE_SHARE_COUNTS",
    "GET_EQUITY_BARS",
    "GET_FULL_TEXT",
    "COMPUTE_FLOAT",
    "COMPUTE_MARKET_CAP",
    "FETCH_INSIDER_TRADES",
    "GET_MACRO_SERIES",
    "GET_REDDIT_SENTIMENT",
    "GET_REGISTERED_SECURITIES",
    "GET_XBRL_FACTS",
    "MULTIHOP_GRAPH",
    "QUERY_GRAPH",
    "RISK_ANALYST_TOOLS",
    "RUN_PYTHON",
    "SEARCH_REDDIT_POSTS",
    "SECTOR_ANALYST_TOOLS",
    "SECTOR_ANALYST_TOOLS_SLIM",
    "INVOKE_SKILL_FN",
    "LOAD_SKILL",
    "LOAD_PLANNER_SKILL",
    "build_planner_dispatch_tools",
    "STOCK_ANALYST_TOOLS",
    "Tool",
    "VC_ANALYST_TOOLS",
    "VECTOR_SCRAPED_ARTICLES",
    "bind_tools",
    "lookup_tool",
]
