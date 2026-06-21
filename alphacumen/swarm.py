# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen.swarm`` -- the platform-facing pipeline entrypoint.

The Coral Bricks platform resolves the manifest's
``[tool.coralbricks.pipeline].entrypoint = "alphacumen.swarm:run"`` to
:func:`run` and dispatches it inside a per-task sandbox subprocess.

This is the multi-round investment-analyst swarm, in parity with the
prod ``gdelt.project.agents.lg_pipelines.run_investment_analyst_swarm``
shape but framework-free (no LangGraph, no LangChain ChatModel
adapters):

- The synthesizer (the GP) drives the loop. Each round it reviews
  the shared ``common_thread``, prunes dead ends, and either invokes
  more specialists with focused per-task instructions or sets
  ``converged=true`` and emits a ``final_answer``.
- Specialists run in parallel for each round. Each one is one
  :func:`harness.react.run_react` call against its own roster of
  tools; the final ``answer_summary`` (or first-4000-chars raw if no
  JSON) gets posted back onto the common thread for the next GP turn.
- The GP can re-invoke the same specialist across rounds with new
  instructions; the loop terminates on ``converged=true``, on an
  empty ``invoke_next`` (the GP has nothing more to ask), or on
  hitting ``max_rounds`` (forced final round in the prompt).
- Tools dispatch to the seven generic kernel verbs (see
  :mod:`alphacumen.tools`); LLM calls go through
  ``coralbricks.sandbox.llm.chat`` (see :mod:`harness.react`); both
  layers are bounded-retry on transient 429 / CUDA hiccups.
- Cancellation = gateway terminate. We do not poll an in-process
  ``cancel_event``; ``POST /runs/{id}/terminate`` kills the sandbox
  subprocess, which is the platform's hard guarantee.

Pipeline contract (see ``../platform/plans/07_PIPELINE_PACKAGES.md``)::

    def run(query, *, model=None, framework="langgraph",
            **kwargs) -> dict[str, Any]

``framework`` is accepted for contract compliance but ignored -- the
swarm is framework-free.
"""

from __future__ import annotations

import concurrent.futures as _cf
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from harness import _langfuse
from harness import (
    ConstraintEnforcer,
    HarnessConstraints,
)
from harness.context import begin_run, end_run
from alphacumen.capabilities import (
    IndexCapabilitiesMap,
    fetch_index_capabilities,
)
from alphacumen.roster import (
    INVESTMENT_ANALYST_ROSTER,
    SpecialistConfig,
    augment_sector_instruction,
    specialists_for,
)
from alphacumen.postprocessor import postprocess
from harness.react import (
    Trajectory,
    chat_with_retry,
    extract_json_payload,
    format_common_thread,
    json_llm_call,
    run_react,
)
from alphacumen.capabilities import render_index_section
from alphacumen.memo import persist_memo
from alphacumen.skills import load_skills, render_index, render_loaded, validate_ids
from alphacumen.tools import bind_tools

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-round planner decision (inlined from the retired
# harness.synthesizer module; only the swarm consumes this shape now)
# ---------------------------------------------------------------------------


@dataclass
class SynthesizerDecision:
    """Outcome of one planner round.

    The swarm reads this to decide whether to fan out more specialists,
    store the final answer, or surface a hard error.  Every field is
    JSON-friendly so the swarm can serialize it straight into the run
    result for the Console / IA UI.

    - ``converged`` -- ``True`` when the planner has produced no
      ``invoke_next`` tool calls; downstream postprocessor writes the
      ``final_answer``.
    - ``invoke_next`` -- list of ``{persona_key, instruction}`` to
      dispatch in the next round (filtered to the active roster).
    - ``final_answer`` -- left ``None`` here; populated by the
      postprocessor on convergence.
    - ``pruning_notes`` / ``reasoning`` -- the planner's narration;
      surfaced on the common thread + per-round trajectory.
    - ``raw_assistant_text`` -- the verbatim planner reply.
    - ``token_usage`` / ``latency_ms`` -- per-round cost.
    - ``attempts`` -- how many planner rounds the React loop used.
    - ``error`` -- short marker on hard failure; swarm force-converges.
    """

    converged: bool = False
    invoke_next: list[dict[str, Any]] = field(default_factory=list)
    final_answer: Optional[dict[str, Any]] = None
    pruning_notes: Optional[str] = None
    reasoning: Optional[str] = None
    raw_assistant_text: str = ""
    token_usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "tool_calls": 0,
        }
    )
    latency_ms: int = 0
    attempts: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Planner-mode helpers (always on for investment_analyst pipeline)
# ---------------------------------------------------------------------------

_PLANNER_SEED_CACHE: dict[tuple[tuple[str, ...], bool], tuple[str, str]] = {}


def _planner_system_prompt(
    roster_keys: Sequence[str],
    *,
    asof: Optional[str] = None,
) -> str:
    from alphacumen.roster import (
        SPECIALIST_BRIEFS,
        SPECIALIST_CONFIGS,
        _apply_tokens,
        _date_tokens,
        _resolve_today,
    )
    from importlib import resources

    from alphacumen.planner import _variant_planner

    today, is_backtest = _resolve_today(asof)
    key = (tuple(roster_keys), is_backtest)
    cached = _PLANNER_SEED_CACHE.get(key)
    if cached is not None and cached[0] == today:
        return cached[1]

    template = (
        resources.files("alphacumen.prompts")
        .joinpath("planner_seed.md")
        .read_text(encoding="utf-8")
    )
    # Slot pass first: variant text may itself embed ``{today}``, which
    # the next pass (date-token sub) renders against the resolved asof.
    rendered = _variant_planner.apply(template, is_backtest=is_backtest)
    rendered = _apply_tokens(rendered, _date_tokens(asof))
    # `{roster_brief}` is no longer substituted: dispatch is now a
    # tool call (one `dispatch_<persona>` per specialist) and each
    # tool's `description` carries the persona brief. The schema
    # surface the planner sees is self-describing.
    rendered = (
        rendered
        .replace("{skill_index}", render_index(load_skills()))
    )
    _PLANNER_SEED_CACHE[key] = (today, rendered)
    return rendered


def _planner_round_hint(
    *, round_count: int, max_rounds: int, new_messages: int,
    pruning_interval: int,
) -> str:
    if round_count >= max_rounds:
        return (
            "FINAL ROUND — you MUST converge now. Emit NO dispatch_* "
            "tool calls; the synthesis step will write the answer "
            "from whatever findings are on the thread."
        )
    if round_count == 1:
        return (
            "This is your first turn. Call `load_skill` for every "
            "plausibly-matching skill in the index, then emit one "
            "`dispatch_<persona>` tool call per decomposed unit with "
            "an OUTCOME-framed instruction."
        )
    if new_messages >= pruning_interval:
        return (
            f"{new_messages} new specialist messages since your last "
            "review. Review them for quality. Prune any dead ends or "
            "off-topic tangents before dispatching more specialists. "
            "You may also converge (emit no dispatch_* calls) if "
            "findings are sufficient."
        )
    return (
        "Review findings and decide: dispatch more specialists, or "
        "converge by emitting no dispatch_* calls."
    )


DEFAULT_MODEL = "lilac/moonshotai/kimi-k2.6"
"""Default model for the entire run.

A cb-ia run is one model end-to-end — specialists, planner /
synthesizer, and the terminal postprocessor all share the same id.
The per-stage knobs that used to allow a Kimi-specialists /
Cerebras-postprocessor split were removed (2026-06-06) after a
prod run surfaced a Kimi-vs-GPT-20B divergence that no caller had
asked for. One model id, one quality bar, one number to attribute
spend against.

Cerebras (gpt-oss-120b / zai-glm-4.7) remains in the watchdog
fallback chain — when the primary exceeds wall-budget or returns
transport errors, react.py falls back per
``_LLM_FALLBACK_MODEL_MAP`` (see react.py)."""

# ---------------------------------------------------------------------------
# MODEL PROFILES
# ---------------------------------------------------------------------------
# Each profile is a single model id used by every stage in the run.
# Changing the live default is a one-line edit to ``DEFAULT_PROFILE``;
# pinning a specific model for one call is the ``model`` kwarg to
# :func:`run`. Adding a new provider/model is one new entry below + an
# :data:`_LLM_FALLBACK_MODEL_MAP` row in ``react.py`` if it needs a
# non-default watchdog fallback.

MODEL_PROFILES: dict[str, str] = {
    # Cerebras gpt-oss-120b. Latency-optimised. Eval-quality floor
    # matches what the existing kimi-Lilac fallback chain was already
    # producing on watchdog timeouts.
    "cerebras-gpt-oss": "cerebras/gpt-oss-120b",
    # Kimi K2.6 via Lilac proxy. Pin this profile for Vals AI v1
    # reproducibility -- the 96% headline (2026-05-19) was measured
    # under all-kimi.
    "kimi-lilac":      "lilac/moonshotai/kimi-k2.6",
    # Kimi K2.6 served directly by DeepInfra. Slower per call than the
    # Lilac proxy (2:09-2:48 observed vs Lilac's 64-67s) -- kept for
    # cost-comparison sweeps only.
    "kimi-deepinfra":  "moonshotai/Kimi-K2.6",
    # Qwen 397B on DeepInfra. The pre-2026-06 default; used for the
    # Vals AI v2 cycle work.
    "qwen-deepinfra":  "Qwen/Qwen3.5-397B-A17B",
    # Self-hosted gpt-oss-20b on AWS (sglang on a single L40S; see
    # ``aws/`` branch in ``coralbricks.shared.llm.chat_client``). 20B
    # is far weaker than the 120B / Kimi K2.6 fleet, so this profile
    # is a measurement / cost-comparison target, not a production
    # candidate. Requires AWS_GPT_BASE_URL + AWS_GPT_API_KEY in the
    # gateway env and ``aws/gpt-oss-20b`` in the pipeline.json models
    # allowlist.
    "aws-gpt-oss-20b": "aws/gpt-oss-20b",
}

DEFAULT_PROFILE = "kimi-lilac"
"""Profile applied when the caller passes no ``model``. Change this
single line to flip the live UX default across the fleet (then bump
cb-ia version + publish)."""


def resolve_active_profile(profile_name: Optional[str] = None) -> str:
    """Return the resolved model id for this run.

    Resolution order, highest priority first:

    1. ``CB_IA_MODEL`` env var (pins a literal model id).
    2. ``CB_IA_PROFILE`` env var (chooses a named profile).
    3. ``profile_name`` argument (for tests / programmatic overrides).
    4. :data:`DEFAULT_PROFILE`.
    """
    pinned = os.environ.get("CB_IA_MODEL")
    if pinned:
        return pinned
    name = (
        os.environ.get("CB_IA_PROFILE")
        or profile_name
        or DEFAULT_PROFILE
    )
    if name not in MODEL_PROFILES:
        raise ValueError(
            f"unknown CB_IA_PROFILE={name!r}; choices={sorted(MODEL_PROFILES)}"
        )
    return MODEL_PROFILES[name]


# Back-compat aliases for retired model ids. The platform UI, the
# /v1/runs API, and saved-query snapshots may still send legacy
# model strings that were removed from the manifest allowlist; we
# rewrite them at swarm entry so the gateway sees only currently
# valid ids. Keys are lower-cased for case-insensitive match.
# Add an entry here whenever a model is retired without breaking
# in-flight clients.
_MODEL_ALIASES: dict[str, str] = {
    # DeepInfra-direct Kimi K2.6 was retired in cb-ia 0.0.519:
    # per-call wall 2:09-2:48 vs Lilac's 64-67s, busted every
    # watchdog and silently fell back to gpt-oss-120b. Route legacy
    # ``moonshotai/Kimi-K2.6`` strings to the Lilac proxy id so old
    # UI submissions and saved API queries continue to work.
    "moonshotai/kimi-k2.6": "lilac/moonshotai/kimi-k2.6",
}


def _alias_model(model: str) -> str:
    """Return the canonical model id for ``model`` (legacy → current).

    Used at swarm entry so retired allowlist ids don't surface as
    ``LLMNotAllowed`` to the caller. The mapping is one-way and
    case-insensitive; a hit is logged at INFO so reruns make the
    rewrite obvious in sandbox logs.
    """
    if not isinstance(model, str):
        return model
    rewritten = _MODEL_ALIASES.get(model.lower())
    if rewritten is not None and rewritten != model:
        logger.info(
            "alphacumen model alias: %s -> %s (legacy id; update caller "
            "to send canonical form)", model, rewritten,
        )
        return rewritten
    return model


# Default loop knobs. Picked to match prod exactly so behaviour does
# not silently drift when the gateway forwards a request without
# explicit overrides. Caller can shift via kwargs.
DEFAULT_MAX_ROUNDS = 3
DEFAULT_PRUNING_INTERVAL = 3


# Max specialists running concurrently per round. The roster has 4
# specialists today, but the GP can invoke the same specialist
# multiple times in one round; cap parallelism so we don't blow the
# sandbox's CPU / memory budget when the GP gets enthusiastic.
SPECIALIST_PARALLELISM = 6
_MAX_STEPS_CEILING = 20
"""Hard ceiling for auto-raised max_steps. Sized for ≥6 tickers ×
2 calls each (bm25 + get_full_text) + format-tool synthesis + spare.
Monthly-seasonal-forecast questions need 8 bm25 + 8 get_full_text +
1 format tool ≈ 18; bumped from 14 to make room."""

_MULTI_ENTITY_SPECIALISTS = frozenset({"sector_analyst", "stock_analyst"})

# Code-level dispatch override for ecosystem / competitive-landscape
# queries. Same plateau pattern as the Row 11 seasonality override
# below: the planner has the dispatch row (now in
# ``alphacumen.planner.dispatch_table`` after the
# dedicated_specialist_dispatch skill was deprecated) but the GP's
# training-data prior to dispatch widely (more specialists = more
# thorough) crowds it out —
# observed in production run ef421297 (NVIDIA ecosystem on 0.0.289)
# where the GP dispatched all 4 specialists despite the matching rule.
# When the user's query is an ecosystem/landscape question, sector_
# analyst (SEC filings) and stock_analyst (price/options) add ~zero
# signal — the answer is about private startups + partner relationships
# that don't appear in SEC corpus or price action. Stripping them at
# code level both halves the wallclock and frees the watchdog budget
# for the specialists that actually contribute.
_ECOSYSTEM_QUERY_RE = re.compile(
    r"\b("
    r"ecosystem\s+(?:around|of|for|surrounding)"
    r"|competitive\s+landscape"
    r"|startups?\s+(?:around|in|for)"
    r"|partners?\s+(?:around|of|with)"
    r"|suppliers?\s+(?:of|to)"
    r"|customers?\s+(?:of)"
    r"|key\s+players?\s+(?:in|around)"
    r"|map\s+the\s+(?:ecosystem|landscape|players)"
    r")\b",
    re.IGNORECASE,
)
# Specialists kept on ecosystem queries. Everything else is stripped.
_ECOSYSTEM_KEEP_SPECIALISTS = frozenset({"vc_analyst", "risk_analyst"})


def _is_ecosystem_landscape_query(query: str) -> bool:
    """True if the user's query is an open-ended ecosystem / landscape /
    private-company-network question that should NOT fan out to
    sector_analyst (no SEC signal on private startups) or stock_analyst
    (no price/options angle)."""
    if not query:
        return False
    return bool(_ECOSYSTEM_QUERY_RE.search(query))


# Code-level override for competitive-analysis narrative queries (sister
# to the ecosystem override above). Empirically: Hard rule 5.13 in
# specialist_sector.md tells sector_analyst to cap at 4 rounds for
# "how has X's competitive position changed" questions and pull only
# the 10-K Item 1 "Competition" sub-section, but the model follows the
# rule non-deterministically — observed in production runs d4864e83
# (0.0.292, rule fired, sector_analyst converged in 5 rounds with 10K
# chars) vs 3b0381b6 (0.0.293, rule did NOT fire, sector_analyst went
# 12/12 with 0 chars). Same plateau pattern as Row 11 / Row 41 in
# evals: a prompt-only rule isn't reliable enforcement.
#
# stock_analyst also doesn't earn its keep on competitive-analysis
# questions: price/options/technicals aren't competitive-landscape
# signal. In run 3b0381b6 stock_analyst burned 53s, hit max_steps=6,
# and produced 0 chars of final content. Strip it from invoke_next
# so the budget goes to the specialists that actually contribute.
#
# Trajectory carve-out (run 83e38dfe, 2026-05-25): the original
# "how has X's competitive position (changed|evolved|shifted)" matcher
# was too broad — for the Tesla-since-2023 query, stock_analyst's
# price-action / margin context IS on-topic, and stripping it left the
# answer without specific TSLA financials. Trajectory-flavored
# competitive questions (X's *position changed/evolved/shifted* over
# time, *evolution of* X's strategy, *strategy/positioning changes
# since*) want stock_analyst back, so they're NOT in this regex.
# Structural / cross-sectional patterns ("where does X stand vs",
# "X's competitive moat") stay — those really are landscape-only.
# Hard rule 5.14 in specialist_sector.md handles XBRL-fact pulls for
# the trajectory shape so financial rigor doesn't depend on
# stock_analyst alone.
_COMPETITIVE_ANALYSIS_QUERY_RE = re.compile(
    r"\b("
    r"where\s+does\s+\w+\s+stand\s+vs"
    r"|\w+(?:'s)?\s+competitive\s+moat"
    r")\b",
    re.IGNORECASE,
)
# Specialists kept on competitive-analysis queries. sector_analyst stays
# because the 10-K Item 1 "Competition" sub-section IS the issuer's own
# competitive-position framing (Hard rule 5.13 owns this) — but its
# max_steps is also capped (see _COMPETITIVE_ANALYSIS_MAX_STEPS).
# stock_analyst is dropped: no competitive-landscape signal in
# price/options/technicals.
_COMPETITIVE_ANALYSIS_KEEP_SPECIALISTS = frozenset(
    {"sector_analyst", "vc_analyst", "risk_analyst"}
)
# Cap sector_analyst's max_steps for this query class so even when
# Hard rule 5.13's "cap at 4 rounds" guidance is ignored by the model,
# the round budget itself bounds the damage. 5 rounds = 1 bm25_sec +
# 2 get_full_text + 1 reflection + 1 final = matches the 5.13 recipe's
# expected shape; lower would risk truncating the final-answer turn.
_COMPETITIVE_ANALYSIS_MAX_STEPS = 5


def _is_competitive_analysis_query(query: str) -> bool:
    """True if the user's query is a competitive-analysis narrative
    question. See Hard rule 5.13 + the comment block above for context."""
    if not query:
        return False
    return bool(_COMPETITIVE_ANALYSIS_QUERY_RE.search(query))


# NOTE: TRAJECTORY FORCE-IN (cb-ia 0.0.305-0.0.307) was removed in
# 0.0.308. See the corresponding NOTE in the dispatch-override block
# below for rationale. The regex / helper are kept removed; if it ever
# needs to come back, git history has the implementation.


# Multi-issuer regulatory queries (sister to ecosystem / competitive
# overrides). Empirical signal from the 7-query preset sweep on
# cb-ia@0.0.295: the TikTok/Meta/Alphabet regulatory query (run
# 076b03dc) was the slowest run NOT bound by a Cerebras incident
# (only 1 watchdog hit). The wallclock was 57s because sector_analyst
# was invoked TWICE (synth round 1 + round 2) and went 12/12 rounds
# each time, totaling 64s on the critical path. Per-specialist:
#   sector_analyst round 1: 46s, 12 rounds, success=True
#   sector_analyst round 2: 18.5s, 12 rounds, success=True
#   vc_analyst:               3.7s, 3 rounds (clean)
#   risk_analyst:             7.2s, 6 rounds (clean)
# For a 3-issuer regulatory question, sector_analyst is trying to
# fetch a 10-K for each issuer and read the Risk Factors / Item 1A
# section — but that's BOILERPLATE for major issuers, and the
# "latest" regulatory signal (what the question actually asks for)
# lives in news + GDELT, not in the 10-K. risk_analyst covers exactly
# that. vc_analyst adds competitive context. sector_analyst burning
# 64s of 10-K body extractions for 3 mega-caps is wasted budget —
# strip it.
#
# stock_analyst is also stripped: regulatory risks aren't price-
# action data, and on this run stock_analyst already had no tools
# in the final mix.
_MULTI_ISSUER_REGULATORY_QUERY_RE = re.compile(
    r"\b("
    r"regulatory\s+(?:risks?|issues?|developments?|scrutiny|exposure|landscape|outlook)"
    r"|antitrust"
    r"|investigation(?:s)?\s+(?:against|on|of|surrounding)"
    r"|enforcement\s+actions?"
    r"|congressional\s+(?:hearings?|scrutiny)"
    r"|legal\s+(?:exposure|risks?|challenges?)"
    r"|policy\s+(?:risks?|changes?)\s+(?:affecting|on|around)"
    r")\b",
    re.IGNORECASE,
)
_MULTI_ISSUER_REGULATORY_KEEP_SPECIALISTS = frozenset({"vc_analyst", "risk_analyst"})
# Minimum entity count to trigger the multi-issuer rule. Single-issuer
# regulatory questions ("Paylocity's regulatory risks") DO benefit from
# sector_analyst because the 10-K Item 1A enumeration IS the answer
# shape (see Hard rule 5.8). The multi-issuer pattern is different:
# the answer is the cross-issuer landscape, not a deep dive into any
# one filing.
_MULTI_ISSUER_REGULATORY_MIN_ENTITIES = 2


def _is_multi_issuer_regulatory_query(query: str) -> bool:
    """True if the user's query is a regulatory-risks question that
    names multiple issuers. See the comment block above for context."""
    if not query:
        return False
    if not _MULTI_ISSUER_REGULATORY_QUERY_RE.search(query):
        return False
    return _count_entities(query) >= _MULTI_ISSUER_REGULATORY_MIN_ENTITIES

_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")
_COMPANY_NAMES = {
    "apple", "microsoft", "google", "alphabet", "amazon", "meta",
    "facebook", "nvidia", "tesla", "berkshire", "jpmorgan", "visa",
    "walmart", "mastercard", "unitedhealth", "disney", "netflix",
    "adobe", "salesforce", "intel", "qualcomm", "costco", "broadcom",
    "oracle", "boeing", "chevron", "eli lilly", "merck", "abbvie",
    "nike", "snap", "snapchat", "paypal", "coinbase", "uber",
    "lyft", "airbnb", "asml", "tsmc", "taiwan semiconductor",
    "novo nordisk", "alibaba", "goldman sachs", "morgan stanley",
    "citigroup", "wells fargo", "blackrock", "general motors", "ford",
    "rivian", "lucid", "palantir", "snowflake", "cloudflare",
    "crowdstrike", "palo alto", "datadog", "micron", "arm",
    "tiktok", "bytedance", "spotify", "pinterest", "twitter",
    "openai", "anthropic", "amd",
}


def _count_entities(query: str) -> int:
    """Count distinct entities (tickers + company names) in a query."""
    tickers = set(_TICKER_RE.findall(query or ""))
    noise = {"AND", "OR", "NOT", "THE", "FOR", "GDP", "CPI", "EPS",
             "IPO", "CEO", "CFO", "ETF", "SEC", "FDA", "FTC",
             "RSI", "ATR", "SMA", "EMA", "VS"}
    tickers -= noise
    q_lower = (query or "").lower()
    companies = {name for name in _COMPANY_NAMES if name in q_lower}
    return max(len(tickers), len(companies))


# ---------------------------------------------------------------------------
# Entity canonicalization (applied to the model's `final_answer.entities`
# and `final_answer.key_events[].actor1/actor2` before persistence)
# ---------------------------------------------------------------------------
#
# Why this exists. The alphacumen model emits entity strings inconsistently
# across runs of the same query — e.g. one run says "TSMC", the next
# says "Taiwan Semiconductor Manufacturing Co.", a third says
# "TSMC (Taiwan Semiconductor)". The downstream "what changed since
# your last query" diff in the UI compares these strings literally, so
# same-entity differs-string pairs get spuriously flagged as
# "+ new entity" AND "− no longer surfaced" at the same time.
# Canonicalizing at the cb-ia output boundary fixes the source: all
# downstream consumers (UI diff, analytics, future eval comparisons)
# see one canonical string per entity. Production case that motivated
# this: NVIDIA 8-K runs e3676943 (today, "TSMC") vs b868edaa (1d ago,
# "Taiwan Semiconductor Manufacturing Co.") — both refer to the same
# company; the UI diff confused this with a real-world entity change.
#
# Design notes:
# - Each tuple is (canonical_name, frozenset_of_aliases). The aliases
#   are matched case-insensitively + whitespace-normalized; the
#   canonical form is what we write back.
# - Coverage is the long-tail of public-equity tickers and their
#   commonly-emitted name variants. Not exhaustive — we add as we
#   observe real cases (cheaper than precomputing a 10K-entry registry
#   most of which wouldn't fire in practice).
# - When the model already uses the canonical name (or no alias
#   matches), the string is passed through unchanged.

_ENTITY_CANONICAL_FORMS: tuple[tuple[str, frozenset[str]], ...] = (
    # Semiconductors
    ("TSMC", frozenset({
        "tsmc",
        "taiwan semiconductor",
        "taiwan semiconductor manufacturing",
        "taiwan semiconductor manufacturing co.",
        "taiwan semiconductor manufacturing co",
        "taiwan semiconductor manufacturing company",
        "taiwan semiconductor manufacturing company limited",
        "taiwan semi",
        "tsm",
    })),
    ("Super Micro", frozenset({
        "super micro",
        "supermicro",
        "super micro computer",
        "super micro computer inc",
        "super micro computer, inc.",
        "supermicro computer",
        "smci",
    })),
    ("NVIDIA", frozenset({
        "nvidia", "nvidia corporation", "nvidia corp.", "nvidia corp",
        "nvda",
    })),
    ("AMD", frozenset({
        "amd", "advanced micro devices", "advanced micro devices inc",
        "advanced micro devices, inc.",
    })),
    ("Intel", frozenset({
        "intel", "intel corporation", "intel corp", "intel corp.",
        "intc",
    })),
    ("Broadcom", frozenset({
        "broadcom", "broadcom inc", "broadcom inc.", "avgo",
    })),
    ("ASML", frozenset({"asml", "asml holding", "asml holding n.v."})),
    ("Micron", frozenset({"micron", "micron technology", "mu"})),
    ("Qualcomm", frozenset({"qualcomm", "qualcomm incorporated", "qcom"})),
    ("Arm", frozenset({"arm", "arm holdings", "arm ltd"})),

    # Big Tech
    ("Alphabet", frozenset({
        "alphabet", "alphabet inc", "alphabet inc.", "google",
        "google llc", "googl", "goog",
    })),
    ("Meta", frozenset({
        "meta", "meta platforms", "meta platforms inc",
        "meta platforms, inc.", "facebook", "fb",
    })),
    ("Microsoft", frozenset({
        "microsoft", "microsoft corporation", "microsoft corp", "msft",
    })),
    ("Amazon", frozenset({
        "amazon", "amazon.com", "amazon.com inc", "amzn",
    })),
    ("Apple", frozenset({"apple", "apple inc", "apple inc.", "aapl"})),
    ("Oracle", frozenset({"oracle", "oracle corporation", "oracle corp", "orcl"})),
    ("Salesforce", frozenset({"salesforce", "salesforce.com", "crm"})),
    ("IBM", frozenset({"ibm", "international business machines"})),
    ("Adobe", frozenset({"adobe", "adobe systems", "adobe inc", "adbe"})),
    ("Snowflake", frozenset({"snowflake", "snowflake inc", "snow"})),
    ("Databricks", frozenset({"databricks", "databricks inc", "databricks, inc."})),
    ("Cloudflare", frozenset({"cloudflare", "cloudflare inc", "net"})),
    ("Palantir", frozenset({"palantir", "palantir technologies", "pltr"})),
    ("Datadog", frozenset({"datadog", "datadog inc", "ddog"})),
    ("OpenAI", frozenset({"openai", "open ai", "open-ai"})),
    ("Anthropic", frozenset({"anthropic", "anthropic pbc"})),

    # Consumer / industrial
    ("Tesla", frozenset({"tesla", "tesla inc", "tesla, inc.", "tsla"})),
    ("Boeing", frozenset({
        "boeing", "the boeing company", "boeing co", "ba",
    })),
    ("Coca-Cola", frozenset({
        "coca-cola", "coca cola", "the coca-cola company", "ko",
    })),
    ("PepsiCo", frozenset({
        "pepsico", "pepsi", "pepsico inc", "pep",
    })),
    ("Netflix", frozenset({"netflix", "netflix inc", "nflx"})),
    ("Disney", frozenset({"disney", "the walt disney company", "dis"})),
    ("Airbnb", frozenset({"airbnb", "airbnb inc", "abnb"})),
    ("Uber", frozenset({"uber", "uber technologies", "uber"})),
    ("Lyft", frozenset({"lyft", "lyft inc", "lyft"})),

    # Automotive (US + EU OEMs + Chinese EVs). Added after run 22a35e7b
    # surfaced "Hyundai Motor Group" as an entity while events used
    # bare "Hyundai" — the cb-ia output itself was internally
    # inconsistent on issuer naming for autos, and the canonicalizer
    # had ZERO automotive entries to collapse them. Same story for
    # "Lucid Motors" vs bare "Lucid". The "what changed" UI diff
    # surfaced both as bogus add/remove pairs.
    ("Ford", frozenset({
        "ford", "ford motor", "ford motor company", "f",
    })),
    ("GM", frozenset({
        "gm", "general motors", "general motors company",
    })),
    ("Hyundai", frozenset({
        "hyundai", "hyundai motor", "hyundai motor group",
        "hyundai motor company", "hmc",
    })),
    ("Kia", frozenset({
        "kia", "kia motors", "kia corporation", "kia corp",
    })),
    ("Lucid", frozenset({
        "lucid", "lucid motors", "lucid group", "lucid group inc", "lcid",
    })),
    ("Rivian", frozenset({
        "rivian", "rivian automotive", "rivian automotive inc", "rivn",
    })),
    ("BYD", frozenset({
        "byd", "byd auto", "byd company", "byd co", "byd company ltd",
    })),
    ("Toyota", frozenset({
        "toyota", "toyota motor", "toyota motor corporation",
        "toyota motor corp", "tm",
    })),
    ("Mercedes-Benz", frozenset({
        "mercedes", "mercedes-benz", "mercedes benz",
        "mercedes-benz group", "mercedes-benz group ag",
    })),
    ("Volkswagen", frozenset({
        "volkswagen", "vw", "volkswagen group", "volkswagen ag",
    })),
    ("Nio", frozenset({"nio", "nio inc", "nio limited"})),
    ("XPeng", frozenset({"xpeng", "xpeng inc", "xpev"})),
    ("Li Auto", frozenset({
        "li auto", "li auto inc", "li", "lixiang", "lixiang auto",
    })),

    # Foreign large-caps + Asia ecosystem
    ("Alibaba", frozenset({"alibaba", "alibaba group", "baba"})),
    ("Tencent", frozenset({"tencent", "tencent holdings", "0700.hk"})),
    ("ByteDance", frozenset({"bytedance", "byte dance"})),
    ("TikTok", frozenset({"tiktok", "tik tok"})),
    ("Samsung", frozenset({
        "samsung", "samsung electronics", "samsung electronics co",
        "samsung electronics co., ltd.",
    })),
    ("SK Hynix", frozenset({"sk hynix", "skhynix", "hynix"})),

    # Regulators / agencies (cleans up DOJ / SEC / FTC variants)
    ("US Department of Justice", frozenset({
        "us department of justice", "u.s. department of justice",
        "department of justice", "doj", "u.s. doj", "us doj",
        "united states department of justice",
    })),
    ("SEC", frozenset({
        "sec", "u.s. sec",
        "securities and exchange commission",
        "u.s. securities and exchange commission",
    })),
    ("FTC", frozenset({
        "ftc", "federal trade commission", "u.s. ftc",
    })),
    ("FDA", frozenset({
        "fda", "food and drug administration",
        "u.s. food and drug administration",
    })),
)


def _canonicalize_entity_name(name: Optional[str]) -> Optional[str]:
    """Map an entity-name string to its canonical form, or pass through
    if no registered alias matches. Comparison is case-insensitive and
    whitespace-normalized; the canonical form is returned verbatim
    from the registry.

    Empty/None input passes through unchanged so callers don't need to
    special-case missing actor2 slots in key_events.
    """
    if not name or not isinstance(name, str):
        return name
    # Normalize for lookup: lower-case + collapse whitespace.
    needle = " ".join(name.split()).lower().strip()
    if not needle:
        return name
    for canonical, aliases in _ENTITY_CANONICAL_FORMS:
        if needle in aliases:
            return canonical
    return name


_INTERNAL_LEAK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Specialist persona names. Replace with neutral source language.
    (re.compile(r"\bthe\s+(sector|stock|risk|vc|news[_\s]+quant)[_\s]+analyst\b", re.IGNORECASE),
     "available data"),
    (re.compile(r"\b(sector|stock|risk|vc|news[_\s]+quant)[_\s]+analyst\b", re.IGNORECASE),
     "available data"),
    (re.compile(r"\bthe\s+analyst\s+team\b", re.IGNORECASE), "available data"),
    (re.compile(r"\bthe\s+analysts\b", re.IGNORECASE), "available sources"),
    (re.compile(r"\bthe\s+specialists?\b", re.IGNORECASE), "available data"),
    (re.compile(r"\bspecialists?\s+(could|cannot|can't|did|failed|were unable)", re.IGNORECASE),
     r"available data \1"),
    # Internal tool name leaks beyond what the prompt covered.
    (re.compile(r"\bcompute_(technicals|options_stats|market_cap|float|payout_ratio|payout_ratio_peers|"
                r"competitive_trajectory|kpi_subperiod_trend|fcf_margin_trend|fy_capex_guidance_multi|"
                r"revenue_decomposition_take_rate|debt_refi_impact|eps_guidance_dollar_range)\b", re.IGNORECASE),
     "standard financial computation"),
    (re.compile(r"\b(get_equity_bars|get_options_chain|get_macro_series|get_full_text|get_xbrl_facts|"
                r"extract_filing_tables|extract_ma_deal_terms|extract_operating_kpi_table|"
                r"find_sec_filing_edgar|find_quarterly_earnings_8ks|fetch_insider_trades|"
                r"fetch_foreign_monthly_revenue|format_seasonal_forecast|format_guidance_comparison|"
                r"summarize_securities_offering|get_cover_page_share_counts|get_reddit_sentiment|"
                r"search_reddit_posts|bm25_sec|bm25_gdelt|bm25_scraped_articles|"
                r"vector_scraped_articles|run_python|query_athena|query_graph|multihop_graph)\b",
                re.IGNORECASE),
     "public data sources"),
    # Self-narrating-pipeline phrases. Order matters: longer / more
    # specific patterns first so the shorter ones don't consume their
    # operands.
    (re.compile(r"\bthe\s+lack\s+of\s+concrete\s+ticker[\-\s]+level\s+data\s+in\s+this\s+response\s+reflects\s+"
                r"the\s+limitations\s+of\s+the\s+available\s+(retrieval\s+)?tools?\b", re.IGNORECASE),
     "concrete ticker-level data was not available from public sources within the requested window"),
    (re.compile(r"\bthe\s+limitations\s+of\s+the\s+available\s+(retrieval\s+)?tools?\b", re.IGNORECASE),
     "data-source limits"),
    (re.compile(r"\bavailable\s+retrieval\s+tools?\b", re.IGNORECASE), "available data sources"),
    (re.compile(r"\bthe\s+inability\s+to\s+(retrieve|access|fetch|pull|enumerate|surface)\b", re.IGNORECASE),
     "the absence of"),
    (re.compile(r"\bdue\s+to\s+tool\s+limitations\b", re.IGNORECASE),
     "due to data-source limitations"),
    (re.compile(r"\bthe\s+available\s+toolset\s+(does\s+not|doesn't|cannot)\b", re.IGNORECASE),
     r"public data sources do not"),
    (re.compile(r"\bin\s+the\s+(current|available)\s+toolset\b", re.IGNORECASE),
     "in the available public data sources"),
    (re.compile(r"\bno\s+specialist\s+(could|was\s+able\s+to)\b", re.IGNORECASE),
     "available data sources did not"),
    (re.compile(r"\bI\s+(could\s+not|cannot|can't|am\s+unable\s+to)\s+(access|retrieve|pull|fetch|enumerate|find)\b",
                re.IGNORECASE),
     "the available data did not surface"),
    (re.compile(r"\b(?:I|we)\s+re[\-\s]?framed\b", re.IGNORECASE),
     "the request was refocused"),
    (re.compile(r"\b(?:I|we)\s+(?:therefore\s+)?(?:then\s+)?refocused\b", re.IGNORECASE),
     "the request was refocused"),
    (re.compile(r"\b(GDELT|BM25|DuckDB|Turbopuffer|pullpush)\b", re.IGNORECASE),
     "public data sources"),
)

# Use a character class that includes ASCII hyphen + the common
# unicode dash family (hyphen, non-breaking hyphen, figure dash, en
# dash, em dash) so the prompts' Markdown — which routinely uses
# unicode en dashes — matches the same patterns as ASCII text.
_DASH = r"[\-‐-—]"

# Sentence-level drop triggers: if a sentence / bullet contains any
# of these, drop the entire sentence rather than try to rewrite it.
# These are sentences that are purely self-narrative about the
# pipeline's internal state and offer the user no information.
_INTERNAL_LEAK_SENTENCE_DROPS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\b(?:I|we)\s+re{_DASH}?\s?framed\b", re.IGNORECASE),
    re.compile(r"\b(?:I|we)\s+(?:therefore\s+)?(?:then\s+)?refocused\b", re.IGNORECASE),
    re.compile(r"\b(?:I|we)\s+(?:could\s+not|cannot|can't|am\s+unable\s+to|was\s+unable\s+to)\b", re.IGNORECASE),
    re.compile(r"\bcombining\s+these\s+findings\b", re.IGNORECASE),
    re.compile(r"\bdeterministic\s+list\s+is\s+unavailable\b", re.IGNORECASE),
    re.compile(rf"\bfilings{_DASH}+based\s+fundamentals\s+for\s+a\s+pre{_DASH}?selected\s+set\b", re.IGNORECASE),
    re.compile(rf"\bsupplied\s+three\s+concrete\s+tail{_DASH}?risk\s+events\b", re.IGNORECASE),
    re.compile(r"\bavailable\s+data\s+(?:also\s+)?(?:could\s+not|failed\s+to)\b", re.IGNORECASE),
    re.compile(r"\b(?:the\s+)?analyst\s+team\s+(?:also\s+)?(?:could\s+not|failed\s+to|did\s+not|supplied|provided|highlighted)\b", re.IGNORECASE),
    re.compile(r"\bthe\s+(?:sector|stock|risk|vc|news[_\s]+quant)[_\s]+analyst\s+(?:also\s+)?(?:could\s+not|failed\s+to|did\s+not|supplied|provided|highlighted)\b", re.IGNORECASE),
    # Plural / multi-persona forms — "stock and sector analysts could
    # not retrieve" / "the risk and stock analysts failed to" / etc.
    re.compile(r"\b(?:the\s+)?(?:sector|stock|risk|vc|news[_\s]+quant)(?:\s*(?:,|and|/)\s*(?:sector|stock|risk|vc|news[_\s]+quant))+\s+analysts?\b", re.IGNORECASE),
    re.compile(r"\banalysts\s+(?:also\s+)?(?:could\s+not|cannot|can't|failed\s+to|did\s+not|were\s+unable\s+to|supplied|provided|surfaced|highlighted|retrieved)\b", re.IGNORECASE),
    # Possessive forms of the scrubber substitutions ("available
    # data's macro snapshot" / "the available data's output").
    re.compile(r"\bavailable\s+data['’]s\b", re.IGNORECASE),
    re.compile(r"\bavailable\s+data\s+output\b", re.IGNORECASE),
    re.compile(rf"\bcould\s+not\s+(?:enumerate|retrieve|access|fetch|pull)\s+(?:the\s+)?(?:universe|list|set)\b", re.IGNORECASE),
    # Pipeline-implementation language. Sentences that describe how
    # AlphaCumen executes (tool budget, single run, batch processing,
    # tool chain, toolset, full-universe scan) leak that there is an
    # AI tool behind the answer. Users do not need to know the
    # pipeline's internal budget / batch / chain mechanics.
    re.compile(r"\btool[\-\s]?call\s+budget\b", re.IGNORECASE),
    re.compile(r"\btool\s+budget\b", re.IGNORECASE),
    re.compile(r"\btoolset\b", re.IGNORECASE),
    re.compile(r"\btool\s+chain\b", re.IGNORECASE),
    re.compile(r"\b(?:current\s+)?specialist\s+tools?\b", re.IGNORECASE),
    re.compile(r"\bavailable\s+data\s+tools?\b", re.IGNORECASE),
    re.compile(r"\bavailable\s+tool\s+(?:capabilit|capacit|infrastructure|stack)", re.IGNORECASE),
    re.compile(r"\bno\s+specialist\s+(?:could|was\s+able\s+to|returned|surfaced|supplied|provided|found)\b", re.IGNORECASE),
    # Bare "specialist" mention — drop any line that contains the
    # word "specialist" / "specialists" as a standalone noun. The
    # word should not appear in user-facing prose at all (it tells
    # the user there is a multi-agent pipeline behind the answer).
    re.compile(r"\bspecialists?\b", re.IGNORECASE),
    re.compile(rf"\bfull{_DASH}?universe\s+scan\b", re.IGNORECASE),
    re.compile(r"\bin\s+a\s+single\s+run\b", re.IGNORECASE),
    re.compile(r"\busers?\s+must\s+supply\s+(?:an\s+)?initial\s+ticker\s+list\b", re.IGNORECASE),
    re.compile(r"\bfeed\s+them\s+into\s+the\s+tool\s+chain\b", re.IGNORECASE),
    re.compile(r"\bbulk\s+universe\s+query\b", re.IGNORECASE),
    re.compile(r"\bbatch\s+processing\s*[:.]", re.IGNORECASE),
    re.compile(r"\bthe\s+risk\s+analysis\s+(?:provides|surfaced|highlighted|supplied)\b", re.IGNORECASE),
    re.compile(r"\black\s+of\s+(?:quantitative\s+)?(?:volatility|short[\-\s]?interest|insider[\-\s]?trading)\s+data\s+limits\b", re.IGNORECASE),
    # Pipeline meta-narration at the START of a sentence / bullet.
    # The verbs only matter when they describe what *the pipeline*
    # did — sentence-start anchoring avoids clobbering legitimate
    # prose ("the company refocused its strategy").
    re.compile(rf"(?:^|(?<=[\n\-])\s*)(?:Re{_DASH}?\s?framed|Re{_DASH}?\s?frames|Refocused|Refocuses|Reframed|Reframes)\b", re.IGNORECASE),
    re.compile(r"(?:^|(?<=[\n\-])\s*)the\s+answer\s+notes\s+(?:these\s+)?gaps\b", re.IGNORECASE),
    re.compile(r"\bthe\s+answer\s+(?:therefore\s+)?(?:reframes|reframe|refocuses|refocus|notes|reports|provides|presents|outlines)\b", re.IGNORECASE),
    re.compile(r"\bcurrent\s+tool\s+run\b", re.IGNORECASE),
    re.compile(r"\busers?\s+must\s+execute\b", re.IGNORECASE),
    re.compile(r"\bnot\s+directly\s+accessible\s+in\s+the\s+current\s+tool\s+run\b", re.IGNORECASE),
    re.compile(r"\blacked\s+the\s+core\s+data\b", re.IGNORECASE),
)


def _drop_self_narrative_lines(s: str) -> str:
    """Drop bullet / sentence lines whose only content is pipeline
    self-narration. Operates line-by-line on Markdown; a line is
    dropped entirely if any sentence-drop trigger matches.
    """
    out_lines: list[str] = []
    for line in s.splitlines():
        if any(p.search(line) for p in _INTERNAL_LEAK_SENTENCE_DROPS):
            continue
        out_lines.append(line)
    # Collapse runs of empty lines that result from dropped bullets.
    cleaned: list[str] = []
    blank_run = 0
    for line in out_lines:
        if not line.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        cleaned.append(line)
    return _drop_empty_markdown_sections("\n".join(cleaned))


_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+\S")
_MD_HR_RE = re.compile(r"^\s*(?:---|\*\*\*|___)\s*$")

# Markdown section headers that duplicate structured JSON fields the
# webapp renders separately ("Supporting Details" UI panel). Dropping
# these sections from answer_summary stops the duplicate render. Match
# the header title prefix only — "## Ranked Entities (by tail-risk)"
# still triggers on "Ranked Entities".
_DUPLICATE_STRUCTURED_SECTION_TITLES: tuple[str, ...] = (
    "entities",
    "ranked entities",
    "metrics evidence",
    "time range",
    "confidence",
)


def _drop_duplicate_structured_sections(s: str) -> str:
    """Drop trailing Markdown sections that duplicate structured
    top-level fields (entities / ranked_entities / metrics_evidence /
    time_range / confidence). The webapp renders these from the
    structured JSON; appending a Markdown copy at the bottom of
    answer_summary causes the UI to show them twice.
    """
    lines = s.splitlines()
    keep = [True] * len(lines)
    i = 0
    while i < len(lines):
        m = _MD_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        # Strip header markup + bold + optional parenthetical
        # ("## Ranked Entities (by tail‑risk rating)") to match against
        # the known-duplicate list.
        body = re.sub(r"^#{1,6}\s+", "", lines[i]).strip()
        body = re.sub(r"\*+", "", body)
        body = re.sub(r"\s*\(.*?\)\s*$", "", body)
        body = body.lower().rstrip(":").strip()
        if body in _DUPLICATE_STRUCTURED_SECTION_TITLES:
            keep[i] = False
            j = i + 1
            while j < len(lines):
                if _MD_HEADER_RE.match(lines[j]):
                    break
                keep[j] = False
                j += 1
            # Trim a trailing HR line that the section block sat above.
            if j > 0 and j - 1 < len(lines) and _MD_HR_RE.match(lines[j - 1]):
                keep[j - 1] = False
            # Also trim the preceding HR (the convention is
            # `prev section ... \n --- \n ## Confidence \n ...`).
            k = i - 1
            while k >= 0 and not lines[k].strip():
                k -= 1
            if k >= 0 and _MD_HR_RE.match(lines[k]):
                keep[k] = False
            i = j
            continue
        i += 1
    pruned = [ln for ln, kept in zip(lines, keep) if kept]
    # Collapse multiple trailing blank lines.
    while pruned and not pruned[-1].strip():
        pruned.pop()
    return "\n".join(pruned)


def _drop_empty_markdown_sections(s: str) -> str:
    """Drop Markdown headings that have no content under them.

    A heading is considered empty if everything between it and the next
    heading / horizontal-rule / EOF is whitespace. Used after
    :func:`_drop_self_narrative_lines` has stripped pipeline-self-
    narration bullets, leaving dangling headers that read as broken UI.
    """
    lines = s.splitlines()
    keep = [True] * len(lines)
    i = 0
    while i < len(lines):
        if _MD_HEADER_RE.match(lines[i]):
            # Scan forward until next header / hr / eof; if nothing
            # non-blank in between, drop both the header and the body
            # (which is already blank).
            j = i + 1
            has_content = False
            while j < len(lines):
                line = lines[j]
                if _MD_HEADER_RE.match(line) or _MD_HR_RE.match(line):
                    break
                if line.strip():
                    has_content = True
                    break
                j += 1
            if not has_content:
                # Drop the header line itself; the trailing blank
                # lines / hr will be collapsed by the next pass.
                keep[i] = False
                # Also drop a single trailing hr if present (the
                # convention `## X\n...\n---` would otherwise leave a
                # dangling rule).
                if j < len(lines) and _MD_HR_RE.match(lines[j]):
                    keep[j] = False
                i = j
                continue
        i += 1
    pruned = [ln for ln, k in zip(lines, keep) if k]
    # Final pass: collapse runs of blank lines so the section break
    # reads naturally without 3+ empties in a row.
    out: list[str] = []
    blank_run = 0
    for line in pruned:
        if not line.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        out.append(line)
    return "\n".join(out)


def _scrub_internal_leaks(s: Optional[str]) -> Optional[str]:
    """Strip internal pipeline references from a user-facing string.

    Belt-and-suspenders pairing with the synthesizer prompt rule banning
    specialist names + tool names + self-narration. Tractable on small
    open-weight models that occasionally ignore the prompt rule.
    Returns the input unchanged if it is None, empty, or non-string.

    Two-pass:
      1. Drop entire lines that are pure self-narration about the
         pipeline's internal state.
      2. Regex-substitute remaining leak fragments with neutral
         source-language equivalents.
    """
    if not isinstance(s, str) or not s.strip():
        return s
    # Pass 1: drop pre-substitution sentences (catches the model's
    # original "the X analyst supplied / failed" phrasing).
    out = _drop_self_narrative_lines(s)
    # Pass 2: substitute remaining leak fragments.
    for pat, repl in _INTERNAL_LEAK_PATTERNS:
        out = pat.sub(repl, out)
    # Pass 3: drop post-substitution sentences (catches the substituted
    # "available data ... failed to" / "available data ... supplied" that
    # earlier passes wrote out and that are now also self-narrative).
    out = _drop_self_narrative_lines(out)
    # Pass 4: strip Markdown backticks around the scrubber's neutral-
    # source substitutions. The model often wraps tool names in `code`
    # backticks (e.g. `compute_technicals`). After substitution the
    # output reads `standard financial computation` which still looks
    # like a tool identifier to a reader. Remove the backticks for those
    # specific neutral-source phrases only — other code spans (tickers,
    # ratios, units) keep their formatting.
    for phrase in (
        "standard financial computation",
        "public data sources",
        "available data",
        "available data sources",
        "available sources",
    ):
        out = re.sub(rf"`\s*{re.escape(phrase)}\s*`", phrase, out, flags=re.IGNORECASE)
    # Pass 5: drop trailing Markdown sections that duplicate structured
    # top-level fields (entities / ranked_entities / metrics_evidence /
    # time_range / confidence). The webapp's RunDetailPage renders these
    # in a separate "Supporting Details" panel; appending a Markdown
    # copy at the bottom of answer_summary causes the UI to show them
    # twice and surfaces the raw JSON inside a prose card.
    out = _drop_duplicate_structured_sections(out)
    # Pass 6: strip the words "Reframed", "Reframe", "Re-framed",
    # "Refocused" from Markdown headers ("## Reframed Goal" → "## Goal").
    # The synth prompt asks the model to write the answer to the
    # answerable variant directly rather than label the reframe; this
    # is a safety net for models that still emit the label.
    out = re.sub(
        rf"(?m)^(#{{1,6}}\s+)(?:Re{_DASH}?\s?framed|Reframed|Refocused|Reframe)\s+",
        r"\1",
        out,
        flags=re.IGNORECASE,
    )
    return out


def _canonicalize_final_answer(answer: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """Return a copy of `answer` with entity strings canonicalized.

    Touches three fields:
      - `entities`: list of strings; canonicalized + de-duplicated
        order-preservingly.
      - `ranked_entities`: list of strings or list of dicts with an
        `entity`/`name`/`ticker` field; canonicalized in place.
      - `key_events`: list of dicts; `actor1` and `actor2` keys
        canonicalized.

    Also scrubs internal pipeline references (specialist names, tool
    names, self-narrating phrases) from `answer_summary` and `reasoning`
    via :func:`_scrub_internal_leaks`. Belt-and-suspenders pairing with
    the synthesizer prompt rule banning those tokens.

    Other fields pass through unchanged. Returns None if `answer` is
    None. Designed to be cheap to call on every successful run — the
    registry lookup is a frozenset hit (O(1)) per entity.
    """
    if answer is None:
        return None
    if not isinstance(answer, Mapping):
        return None
    out = dict(answer)

    # entities: dedupe preserving order, on the canonical form
    raw_entities = out.get("entities")
    if isinstance(raw_entities, list):
        seen: set[str] = set()
        canon_entities: list[Any] = []
        for e in raw_entities:
            if isinstance(e, str):
                c = _canonicalize_entity_name(e) or e
                if c not in seen:
                    seen.add(c)
                    canon_entities.append(c)
            else:
                # Pass through non-string entries unchanged (the model
                # occasionally emits {ticker, name} dicts).
                canon_entities.append(e)
        out["entities"] = canon_entities

    # ranked_entities: either a list of strings or a list of dicts
    raw_ranked = out.get("ranked_entities")
    if isinstance(raw_ranked, list):
        canon_ranked: list[Any] = []
        for r in raw_ranked:
            if isinstance(r, str):
                canon_ranked.append(_canonicalize_entity_name(r) or r)
            elif isinstance(r, Mapping):
                rd = dict(r)
                for field in ("entity", "name", "ticker", "issuer"):
                    if field in rd and isinstance(rd[field], str):
                        rd[field] = _canonicalize_entity_name(rd[field]) or rd[field]
                canon_ranked.append(rd)
            else:
                canon_ranked.append(r)
        out["ranked_entities"] = canon_ranked

    # key_events: actor1 + actor2 — canonicalize then sort the pair
    # alphabetically so semantically-symmetric events ("DOJ + Super
    # Micro: conflict" vs "Super Micro + DOJ: conflict") emit the
    # same canonical (actor1, actor2) tuple. Lets the downstream diff
    # treat these as the same event.
    #
    # Drop single-actor events (empty/missing actor2): these come from
    # specialists attaching generic news items (Microsoft outage, NVIDIA
    # cyberattack) as "key events" with no relational partner. They
    # render as misleading "Microsoft: conflict" entries in the UI diff
    # and don't describe a relationship between two parties. Belt-and-
    # suspenders with the postprocessor key_events relational rule.
    raw_events = out.get("key_events")
    if isinstance(raw_events, list):
        canon_events: list[Any] = []
        for ev in raw_events:
            if isinstance(ev, Mapping):
                evd = dict(ev)
                for field in ("actor1", "actor2"):
                    if field in evd and isinstance(evd[field], str):
                        evd[field] = _canonicalize_entity_name(evd[field]) or evd[field]
                a1 = evd.get("actor1") or ""
                a2 = evd.get("actor2") or ""
                if not (isinstance(a1, str) and isinstance(a2, str) and a1.strip() and a2.strip()):
                    continue
                if a1 == a2:
                    continue
                if a1 > a2:
                    evd["actor1"], evd["actor2"] = a2, a1
                canon_events.append(evd)
            else:
                canon_events.append(ev)
        out["key_events"] = canon_events

    # Scrub internal-pipeline references from user-facing prose. The
    # synthesizer prompt covers this rule textually; the regex scrub
    # is a safety net for small open-weight models that occasionally
    # leak specialist persona names or tool identifiers despite the
    # rule.
    for field in ("answer_summary", "reasoning"):
        v = out.get(field)
        if isinstance(v, str):
            out[field] = _scrub_internal_leaks(v)

    return out


def _assemble_fallback_summary(specialist_outputs: list[dict[str, Any]]) -> str:
    """Concat each successful specialist's ``answer_summary`` into a
    single Markdown document. Used by the deterministic LAST-CHANCE
    fallback when the synthesizer exhausted max_rounds without
    converging — produces a non-empty final answer without paying for
    another synth LLM call. Section per specialist, ordered by
    invocation. Drops specialists that returned no payload."""
    sections: list[str] = []
    for out in specialist_outputs or []:
        if not out.get("success"):
            continue
        label = out.get("label") or out.get("key") or "specialist"
        payload = out.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        summary = payload.get("answer_summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        sections.append(f"## {label}\n\n{summary.strip()}")
    if not sections:
        return ""
    header = (
        "_Synthesizer hit its round budget before converging; "
        "answer below is assembled from raw specialist findings._\n\n"
    )
    return header + "\n\n---\n\n".join(sections)


def _collect_ranked_entities(specialist_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union ``ranked_entities`` across specialists, deduped by name."""
    seen: dict[str, dict[str, Any]] = {}
    for out in specialist_outputs or []:
        payload = out.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        for ent in payload.get("ranked_entities") or []:
            if not isinstance(ent, Mapping):
                continue
            name = ent.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            key = name.strip().lower()
            if key not in seen:
                seen[key] = dict(ent)
    return list(seen.values())


def _collect_key_events(specialist_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union ``key_events`` across specialists, deduped by
    (actor1, actor2, type)."""
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for out in specialist_outputs or []:
        payload = out.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        for ev in payload.get("key_events") or []:
            if not isinstance(ev, Mapping):
                continue
            a1 = str(ev.get("actor1", "")).strip()
            a2 = str(ev.get("actor2", "")).strip()
            t = str(ev.get("type", "")).strip()
            if not a1 or not a2:
                continue  # actor2-required rule (Hard rule actor2)
            key = (a1.lower(), a2.lower(), t.lower())
            if key not in seen:
                seen[key] = dict(ev)
    return list(seen.values())


def _auto_raise_budget(query: str, specialist_key: str) -> Optional[int]:
    """Auto-raise tool budget for multi-entity queries.

    sector_analyst: 2 × entity_count + 1 (bm25 + get_full_text per ticker)
    stock_analyst:  1 × entity_count + 3 (technicals per ticker + macro + synthesis)
    """
    if specialist_key not in _MULTI_ENTITY_SPECIALISTS:
        return None
    entity_count = _count_entities(query)
    if entity_count >= 3:
        if specialist_key == "stock_analyst":
            budget = min(entity_count + 3, _MAX_STEPS_CEILING)
        else:
            budget = min(2 * entity_count + 1, _MAX_STEPS_CEILING)
        logger.info(
            "auto_raise_budget: %s → %d (detected %d entities in query)",
            specialist_key, budget, entity_count,
        )
        return budget
    return None


# Cap on the per-specialist common-thread post. The full ReAct
# trajectory is preserved in ``specialist_outputs`` for the UI; the
# common thread is what the GP reads, and 4k chars is what prod ships
# (any longer makes the per-round system+thread payload bloat).
_THREAD_POST_MAX_CHARS = 4_000


# ---------------------------------------------------------------------------
# news_quant_analyst quant-extract enforcement (post-runtime backstop)
# ---------------------------------------------------------------------------
#
# Background. ``news_quant_analyst`` is the dedicated
# figure-extraction specialist (see specialists.py). Its persona
# prompt is short and laser-focused on retrieve-then-answer; its
# SpecialistConfig sets ``min_tool_calls_before_final=1`` which the
# ReAct loop in ``harness.react.run_react`` uses as a coercion gate:
# if the model tries to emit a final answer with zero tool calls, the
# loop injects a system reminder and continues instead of
# terminating. That's the primary defense.
#
# This swarm-level helper is the BACKSTOP. The runtime gate caps its
# coercion turns at ``_MAX_MUST_RETRIEVE_COERCIONS`` so a model that
# simply refuses to retrieve doesn't burn the entire step budget on
# coercion no-ops -- after the cap, the next no-tool message is
# accepted as final and we fall through to here. If we reach this
# point with ``tool_calls=0`` on ``news_quant_analyst``, the model
# defeated the gate; we discard its payload's ``answer_summary`` so
# the common thread doesn't get polluted with the same hallucinated
# Reuters/Bloomberg citations that motivated this whole subsystem
# (Vals AI row 5 on cb-ia 0.0.149: vc_analyst quant-extract returned
# tool_calls=0 + 4220 chars of fabricated citations).
#
# Scope. Fires only for ``news_quant_analyst``. We do NOT fire for
# vc_analyst because vc_analyst is no longer the figure-extraction
# persona -- the synthesizer prompt routes those dispatches to
# news_quant_analyst, and vc_analyst is back to its narrative
# competitive-context role where ``tool_calls=0`` is sometimes a
# legitimate "I'm asking for clarification" signal.

_QUANT_EXTRACT_FAILURE_SUMMARY = (
    "[quant-extract enforcement] news_quant_analyst returned "
    "tool_calls=0 even after the runtime must-retrieve coercion "
    "budget was exhausted. The model has defeated the gate -- "
    "either by refusing to retrieve across multiple coercion "
    "turns or by exiting the loop on the last allowed round (where "
    "the coercion is suppressed to allow synthesis). The "
    "ungrounded answer_summary has been discarded so it cannot "
    "pollute the common thread with unsourced figures. Treat this "
    "leg as \"no news-side evidence retrieved this round\". "
    "Recovery options: (a) derive the figure from sector_analyst's "
    "filed numbers + management framing if available (the "
    "Q4-actual-times-4 implied-FY rule), or (b) re-route with a "
    "much more directive instruction that names specific outlets "
    "(\"issue bm25_scraped_articles with "
    "query='[COMPANY] [METRIC] [PERIOD] Reuters Bloomberg CNBC' "
    "FIRST, then summarize\")."
)


def _enforce_quant_extract_no_retrieval(
    *,
    spec_key: str,
    payload: Optional[Mapping[str, Any]],
    tool_calls: int,
    raw_final: str,
) -> tuple[Optional[Mapping[str, Any]], str, bool]:
    """Discard ungrounded news_quant_analyst output (backstop to runtime gate).

    Returns ``(payload, raw_final, discarded)``. When ``discarded``
    is True the payload's ``answer_summary`` (and the raw final
    text) have been replaced with the failure marker; the rest of
    the payload (entities, time_range, etc.) is preserved on the
    chance the model populated those structurally even without
    retrieval.

    Only fires for ``news_quant_analyst`` -- the runtime ReAct gate
    (``min_tool_calls_before_final=1`` set in SpecialistConfig) is
    the primary defense. This is the backstop for when the runtime
    gate's coercion budget is exhausted.
    """
    if spec_key != "news_quant_analyst":
        return payload, raw_final, False
    if tool_calls > 0:
        return payload, raw_final, False
    # Don't bother enforcing if there was nothing to discard.
    has_summary = bool(
        isinstance(payload, Mapping)
        and isinstance(payload.get("answer_summary"), str)
        and payload["answer_summary"].strip()
    ) or bool((raw_final or "").strip())
    if not has_summary:
        return payload, raw_final, False

    new_payload: dict[str, Any]
    if isinstance(payload, Mapping):
        new_payload = dict(payload)
    else:
        new_payload = {}
    new_payload["answer_summary"] = _QUANT_EXTRACT_FAILURE_SUMMARY
    new_payload["__discarded_for_no_retrieval"] = True
    new_payload["answerable"] = False
    return new_payload, _QUANT_EXTRACT_FAILURE_SUMMARY, True


# ---------------------------------------------------------------------------
# JSON-envelope coercion (rescue when last-round content was prose)
# ---------------------------------------------------------------------------
#
# The runtime's ``_BUDGET_PENULTIMATE`` nudge tries to make the model
# start drafting the JSON answer envelope on the round before tools
# disappear. When the nudge doesn't bind -- typical Qwen failure mode:
# the model uses the penultimate round for one more retrieval, then
# emits planning prose ("Let me check FY2023...") on the cliff turn
# even with tools removed -- the React loop accepts that prose as
# ``traj.final_message`` and ``extract_json_payload`` returns None,
# leaving the specialist with ``success=False`` and no answer_summary
# despite a thread full of usable tool results.
#
# This coerce path replays the conversation with one trailing system
# directive that strips out the planning permission ("emit ONLY the
# JSON envelope using data already in this thread"). At most one extra
# LLM call per failed-synthesis specialist, billed only on the failure
# path. If the second pass also returns prose, we fall through to the
# existing ``success=False`` handling.

_JSON_COERCE_DIRECTIVE = (
    "STOP. Your previous response was not the JSON answer envelope "
    "this specialist contract requires -- it was planning prose, "
    "tool-call markup, or empty output. Tool access is NOT coming "
    "back; further retrieval attempts will fail. Re-emit ONLY a "
    "single JSON object now, populating every field from data "
    "already in this thread (tool results above + your prior "
    "reasoning). If a field has no evidence, use null / empty list, "
    "or state the gap in `reasoning`. The schema is:\n\n"
    "{\n"
    '  "answerable": true|false,\n'
    '  "answer_summary": "<Markdown answer body>",\n'
    '  "entities": [...],\n'
    '  "ranked_entities": [...],\n'
    '  "key_events": [...],\n'
    '  "metrics_evidence": [...],\n'
    '  "time_range": "<period covered>",\n'
    '  "confidence": "high"|"medium"|"low",\n'
    '  "reasoning": "<terse rationale incl. data gaps>"\n'
    "}\n\n"
    "Output raw JSON only -- no code fences, no narration before or "
    "after. Begin your response with `{`."
)


def _coerce_json_envelope(
    *,
    spec_key: str,
    spec_label: str,
    model: str,
    messages: Optional[Sequence[Mapping[str, Any]]],
    max_tokens: int,
) -> tuple[Optional[Mapping[str, Any]], str]:
    """One-shot JSON-format coerce when the last-round content was prose.

    Returns ``(payload, raw_final)`` where ``payload`` is the parsed
    envelope (None if the coerce also failed) and ``raw_final`` is
    the assistant text from the coerce call (empty string on
    transport failure).
    """
    if not messages:
        return None, ""
    coerce_messages: list[dict[str, Any]] = [
        dict(m) for m in messages
    ]
    coerce_messages.append(
        {"role": "system", "content": _JSON_COERCE_DIRECTIVE}
    )
    try:
        env = chat_with_retry(
            model=model,
            messages=coerce_messages,
            temperature=0.0,
            max_completion_tokens=max_tokens,
            timeout_s=180.0,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "specialist=%s json-coerce LLM call failed: %s",
            spec_key, exc,
        )
        return None, ""

    response = env.get("response") or {}
    choices = response.get("choices") or []
    if not choices:
        return None, ""
    msg = (choices[0] or {}).get("message") or {}
    raw = str(msg.get("content") or "")
    payload = extract_json_payload(raw)
    if payload is not None:
        logger.info(
            "specialist=%s (%s) json-coerce SUCCESS (raw_chars=%d)",
            spec_key, spec_label, len(raw),
        )
    else:
        sample = raw[:200].replace("\n", " ")
        logger.warning(
            "specialist=%s (%s) json-coerce STILL FAILED "
            "(raw_chars=%d); sample=%r",
            spec_key, spec_label, len(raw), sample,
        )
    return payload, raw


# ---------------------------------------------------------------------------
# Trajectory / common-thread serialization
# ---------------------------------------------------------------------------

def _serialize_step(step: Any) -> dict[str, Any]:
    return {
        "kind": step.kind,
        "name": step.name,
        "started_at_ms": step.started_at_ms,
        "elapsed_ms": step.elapsed_ms,
        "arguments": dict(step.arguments),
        "result_preview": step.result_preview,
        "has_error": step.has_error,
        "error_message": step.error_message,
    }


def _serialize_trajectory(traj: Trajectory) -> dict[str, Any]:
    return {
        "rounds": traj.rounds,
        "error": traj.error,
        "token_usage": dict(traj.token_usage),
        "steps": [_serialize_step(s) for s in traj.steps],
        "final_message_content": (
            (traj.final_message or {}).get("content")
            if traj.final_message else None
        ),
    }


def _specialist_thread_text(
    payload: Optional[Mapping[str, Any]],
    raw: str,
    *,
    trajectory: Optional[Trajectory] = None,
) -> str:
    """Render a specialist's ReAct output for the GP-readable common thread.

    The GP reads markdown summaries on the common thread, not raw
    JSON envelopes. Mirror prod's ``_swarm_specialist_thread_text``:
    if the specialist emitted parseable JSON with an
    ``answer_summary`` field, post that; otherwise post the raw
    final text. Always clip to :data:`_THREAD_POST_MAX_CHARS` so a
    runaway specialist can't blow the GP's prompt budget.

    If both the payload and the raw final text are empty, fall back
    to a one-line summary of which tools the specialist actually
    dispatched (with ``(error)`` annotations on failed calls). This
    matters when the provider (Cerebras+Qwen) emitted only
    ``<tool_call>`` text envelopes that ``runtime`` parsed out --
    the model never wrote a final-answer envelope, so without this
    fallback the GP sees ``[Sector Analyst]: `` (empty) and has no
    signal that the specialist actually executed work. Surfacing
    the tool names lets the GP route around the missing summary
    (e.g. re-invoke with a more directive instruction).
    """
    if payload is not None:
        summary = payload.get("answer_summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()[:_THREAD_POST_MAX_CHARS]
    cleaned = (raw or "").strip()
    if cleaned:
        return cleaned[:_THREAD_POST_MAX_CHARS]

    if trajectory is not None:
        tool_steps = [s for s in trajectory.steps if s.kind == "tool"]
        if tool_steps:
            tags: list[str] = []
            for s in tool_steps:
                tag = s.name
                if s.has_error:
                    tag += "(error)"
                tags.append(tag)
            return (
                "[no final summary; tools called: " + ", ".join(tags) + "]"
            )[:_THREAD_POST_MAX_CHARS]
    return ""


# ---------------------------------------------------------------------------
# Specialist dispatch (one round)
# ---------------------------------------------------------------------------

def _run_one_specialist(
    *,
    spec: SpecialistConfig,
    model: str,
    instruction: str,
    round_idx: int,
    invocation_idx: int,
    index_caps: IndexCapabilitiesMap,
    asof: Optional[str] = None,
    max_steps_override: Optional[int] = None,
    filed_at_lte: Optional[str] = None,
) -> dict[str, Any]:
    """Drive one specialist invocation through ``run_react``; never raises.

    Errors land in the returned dict's ``error`` field so the
    synthesizer can route around a partial result instead of the
    whole swarm collapsing on a single bad provider call.

    ``instruction`` is the GP-authored per-round task -- NOT the raw
    user query. The GP is responsible for crafting focused
    instructions; the swarm just transports them.

    ``index_caps`` is the per-run snapshot from
    :func:`fetch_index_capabilities` -- we stamp it into the
    specialist's tools right before handing them to ``run_react``
    so each tool's description carries the registered field /
    table / predicate schemas the model actually has access to.

    ``max_steps_override`` lets the synthesizer raise (but not
    lower) the specialist's default step budget for complex
    queries. Capped at :data:`_MAX_STEPS_CEILING`.
    """
    effective_steps = spec.max_steps
    if max_steps_override is not None and max_steps_override > spec.max_steps:
        effective_steps = min(max_steps_override, _MAX_STEPS_CEILING)
    bound = bind_tools(spec.tools, index_caps, render_index_section)
    log_label = f"{spec.key}#r{round_idx}.i{invocation_idx}"
    logger.info(
        "alphacumen specialist=%s round=%d invocation=%d starting "
        "max_steps=%d tools=%s instruction=%r",
        spec.key, round_idx, invocation_idx, effective_steps,
        [t.name for t in bound], instruction or "",
    )
    t0 = time.perf_counter()
    # Thread-local temporal ceiling: when the synthesizer sets
    # filed_at_lte for this specialist, inject it so every bm25_sec
    # call in this thread auto-applies the ceiling.
    from alphacumen.tools import set_temporal_ceiling
    set_temporal_ceiling(filed_at_lte)
    try:
        traj = run_react(
            model=model,
            system_prompt=spec.system_prompt(asof=asof),
            user_message=(
                augment_sector_instruction(instruction)
                if spec.key == "sector_analyst" else instruction
            ),
            tools=bound,
            max_steps=effective_steps,
            # Per-specialist output-token cap (see SpecialistConfig
            # docstring). sector_analyst / stock_analyst are raised
            # above the 4096 default to prevent silent truncation
            # of the structured JSON envelope.
            max_tokens=spec.max_tokens,
            # Per-specialist temperature -- sector_analyst is forced
            # to 0.0 to keep numeric atom rendering deterministic on
            # tool ``answer_summary_block`` strings the rubric grades
            # verbatim. See SpecialistConfig.temperature docstring.
            temperature=spec.temperature,
            # Must-retrieve gate -- see SpecialistConfig field
            # docstring. news_quant_analyst sets this to 1 so the
            # ReAct loop refuses to accept a confident no-tool-call
            # answer as final. Other personas leave it at 0.
            min_tool_calls_before_final=spec.min_tool_calls_before_final,
            log_label=log_label,
            # Keep full OHLC bar payloads around after run_react
            # returns -- the post-synth equity_chart shaper in
            # ``run()`` walks specialist outputs for these to
            # assemble the {symbol, interval, points[...]} payload
            # the webapp renders. Step.result_preview is clipped
            # well before a full bar list ends. ``compute_technicals``
            # also ships the raw bars on its envelope (tools.py
            # :func:`_do_compute_technicals`), so capture both --
            # stock_analyst often reaches for compute_technicals
            # first on "price / trade" questions and skips the
            # direct get_equity_bars call.
            capture_tools=("get_equity_bars", "compute_technicals"),
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.exception(
            "specialist=%s (round=%d, invocation=%d) crashed",
            spec.key, round_idx, invocation_idx,
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        err_str = f"{type(exc).__name__}: {exc!s}"
        return {
            # Canonical fields (kept for back-compat).
            "key": spec.key,
            "label": spec.label,
            "round": round_idx,
            "invocation": invocation_idx,
            "instruction": instruction,
            "payload": None,
            "trajectory": None,
            "thread_text": f"ERROR: {err_str}"[:_THREAD_POST_MAX_CHARS],
            "error": err_str,
            # Top-level convenience fields. Mirrored from the trajectory
            # so a UI / downstream consumer can render an invocation
            # row without having to dig into ``trajectory.*``.
            "persona_key": spec.key,
            "round_index": round_idx,
            "success": False,
            "tool_calls": 0,
            "token_usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "tool_calls": 0,
            },
            "latency_ms": elapsed_ms,
        }

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    payload: Optional[Mapping[str, Any]] = None
    raw_final = ""
    coerce_status: Optional[str] = None  # None | "success" | "failure"
    coerce_raw_sample = ""  # first 500 chars of coerce output, success or fail
    if traj.final_message:
        raw_final = traj.final_message.get("content") or ""
        payload = extract_json_payload(raw_final)
        if payload is None and traj.final_messages:
            # Last-round content was either prose ("Let me check FY2023...")
            # or empty (Qwen sometimes emits a stripped `<tool_call>`
            # envelope on the cliff turn, leaving cleaned_content=""). In
            # both cases the trajectory still has the tool results --
            # replay the conversation with a strict "JSON only" directive
            # so the data reaches the synthesizer instead of being thrown
            # away as ``success=False``. The empty-content case matters:
            # without coercing it we lose every retrieval the specialist
            # made before the cliff turn.
            #
            # Fast-path: when the specialist hit `max_steps` AND emitted
            # zero content, there's no point asking the LLM to "coerce"
            # — the trajectory is by definition large (the model used
            # every round retrieving), so the coerce input is hundreds
            # of K tokens. Production run c3f960ef burned 75s on three
            # consecutive 25s watchdog timeouts trying to coerce a
            # 575k-token sector_analyst history that hit max_steps=12
            # with 0 chars of final content. Write a synthetic envelope
            # locally instead — the trajectory + tool results are still
            # serialized into the per-specialist record, the
            # synthesizer still sees the partial work, and we save the
            # 25-75s wallclock.
            hit_max_steps = (
                traj.rounds >= effective_steps and not raw_final.strip()
            )
            if hit_max_steps:
                logger.warning(
                    "specialist=%s (%s) hit max_steps=%d with 0 chars "
                    "of final content; skipping json-coerce LLM call to "
                    "avoid the 25-75s timeout-retry path on a likely-"
                    "huge trajectory (tokens_in_so_far~%d)",
                    spec.key, spec.label, effective_steps,
                    int(traj.token_usage.get("input_tokens", 0))
                    if isinstance(traj.token_usage, Mapping) else 0,
                )
                # Synthetic envelope: tells the synth that this
                # specialist failed-out but the trajectory carries the
                # partial work it did. answerable=False so the synth
                # doesn't quote this as a final answer.
                payload = {
                    "answerable": False,
                    "answer_summary": (
                        f"{spec.label} hit the max-step ceiling "
                        f"({effective_steps} rounds) without converging "
                        "on a final answer; partial retrieval results "
                        "remain in this specialist's trajectory."
                    ),
                    "confidence": "low",
                    "__json_coerced_skipped": True,
                    "__json_coerced_reason": "max_steps_zero_content",
                }
                coerce_status = "skipped_max_steps"
                coerce_raw_sample = ""
            else:
                coerce_payload, coerce_raw = _coerce_json_envelope(
                    spec_key=spec.key,
                    spec_label=spec.label,
                    model=model,
                    messages=traj.final_messages,
                    max_tokens=spec.max_tokens,
                )
                coerce_raw_sample = (coerce_raw or "")[:500]
                if coerce_payload is not None:
                    payload = dict(coerce_payload)
                    payload["__json_coerced"] = True
                    raw_final = coerce_raw
                    coerce_status = "success"
                else:
                    coerce_status = "failure"

    # Visibility on the two known failure modes:
    # (a) extract_json_payload had to do a truncation-repair (model
    #     hit its max_tokens cap mid-emission); the payload is usable
    #     but the tail is missing -- log so we can correlate against
    #     output_tokens hitting the cap.
    # (b) No payload at all even though raw_final is non-empty -- the
    #     model emitted prose that doesn't even *look* like the
    #     structured envelope. Log a sample so the operator can see
    #     what the specialist actually said instead of getting a
    #     "success=False, payload=None" mystery.
    if isinstance(payload, Mapping) and payload.get("__repaired_json"):
        logger.warning(
            "specialist=%s round=%d invocation=%d emitted truncated JSON "
            "(max_tokens cap likely hit); using repaired payload "
            "(output_tokens=%d, raw_final_chars=%d)",
            spec.key, round_idx, invocation_idx,
            int((traj.token_usage or {}).get("output_tokens") or 0),
            len(raw_final),
        )
    elif payload is None and raw_final:
        sample = raw_final[:300].replace("\n", " ")
        logger.warning(
            "specialist=%s round=%d invocation=%d produced unparseable "
            "final message (raw_final_chars=%d, output_tokens=%d). "
            "Sample: %r",
            spec.key, round_idx, invocation_idx,
            len(raw_final),
            int((traj.token_usage or {}).get("output_tokens") or 0),
            sample,
        )

    tool_calls_emitted = int(traj.token_usage.get("tool_calls", 0) or 0)
    payload, raw_final, _quant_discarded = _enforce_quant_extract_no_retrieval(
        spec_key=spec.key,
        payload=payload,
        tool_calls=tool_calls_emitted,
        raw_final=raw_final,
    )
    if _quant_discarded:
        logger.warning(
            "specialist=%s round=%d invocation=%d quant-extract enforcement "
            "BACKSTOP fired: tool_calls=0 even after runtime must-retrieve "
            "gate; payload discarded as ungrounded (instruction=%r)",
            spec.key, round_idx, invocation_idx,
            (instruction or "")[:200],
        )

    thread_text = _specialist_thread_text(payload, raw_final, trajectory=traj)

    # ``success`` reflects "the runtime ran cleanly to completion AND we
    # got a parseable JSON envelope back from the model". A specialist
    # that ran fine but came back ``answerable=false`` is still a
    # successful invocation -- the GP will route around it on the next
    # round; it's not a runtime failure. A repaired-from-truncation
    # payload still counts as success -- the synthesizer can see the
    # repair flag and degrade trust if needed.
    #
    # A vc_analyst payload discarded by quant-extract enforcement is
    # NOT counted as a successful invocation: the runtime ran cleanly
    # but the model failed its hard-rule contract (issue at least one
    # retrieval before answering). Marking it as success=False lets
    # the GP's routing logic correctly treat the leg as "no usable
    # output" rather than "specialist answered, here's the (replaced)
    # text".
    success = (
        traj.error is None
        and payload is not None
        and not (isinstance(payload, Mapping) and payload.get("__discarded_for_no_retrieval"))
    )

    return {
        # Canonical fields (kept for back-compat).
        "key": spec.key,
        "label": spec.label,
        "round": round_idx,
        "invocation": invocation_idx,
        "instruction": instruction,
        "payload": dict(payload) if payload is not None else None,
        "trajectory": _serialize_trajectory(traj),
        "thread_text": thread_text,
        "error": traj.error,
        # Full captured tool results (only present for tools in the
        # ``run_react(capture_tools=...)`` whitelist). Keyed by tool
        # name; value is the list of payloads in call order. Used by
        # :func:`_build_equity_chart_from_specialists` to lift OHLC
        # bars into the ``equity_chart`` response field without
        # needing a separate post-synth tool call.
        "captured_tool_results": dict(traj.captured_tool_results),
        # Top-level convenience fields (see contract docstring on ``run``).
        "persona_key": spec.key,
        "round_index": round_idx,
        "success": success,
        "tool_calls": tool_calls_emitted,
        "token_usage": dict(traj.token_usage),
        "latency_ms": elapsed_ms,
        "json_coerce_status": coerce_status,
        "json_coerce_raw_sample": coerce_raw_sample,
    }


def _run_round_specialists(
    *,
    invocations: Sequence[Mapping[str, Any]],
    spec_configs: Mapping[str, SpecialistConfig],
    model: str,
    round_idx: int,
    index_caps: IndexCapabilitiesMap,
    asof: Optional[str] = None,
    query_text: str = "",
) -> list[dict[str, Any]]:
    """Fan out one GP-authored round of specialist invocations.

    Order of returned outputs matches ``invocations`` so the swarm
    can post them onto the common thread in the order the GP asked.
    Uses a thread pool because each specialist is dominated by I/O
    waiting on ``llm.chat`` -- CPU-bound work per specialist is in
    the low milliseconds.
    """
    if not invocations:
        return []
    width = min(SPECIALIST_PARALLELISM, len(invocations))
    outputs: list[dict[str, Any]] = [None] * len(invocations)  # type: ignore[list-item]
    with _cf.ThreadPoolExecutor(
        max_workers=width, thread_name_prefix="alphacumen_spec",
    ) as pool:
        futures = {}
        for i, task in enumerate(invocations):
            persona_key = task.get("persona_key")
            spec = spec_configs.get(persona_key) if isinstance(persona_key, str) else None
            if spec is None:
                # Already filtered by the synthesizer, but be
                # defensive -- a pool that swallows tasks silently
                # is the worst kind of bug to debug.
                outputs[i] = {
                    "key": str(persona_key),
                    "label": str(persona_key),
                    "round": round_idx,
                    "invocation": i,
                    "instruction": task.get("instruction") or "",
                    "payload": None,
                    "trajectory": None,
                    "thread_text": f"ERROR: unknown specialist {persona_key!r}",
                    "error": f"unknown specialist {persona_key!r}",
                    "persona_key": str(persona_key),
                    "round_index": round_idx,
                    "success": False,
                    "tool_calls": 0,
                    "token_usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cached_tokens": 0,
                        "tool_calls": 0,
                    },
                    "latency_ms": 0,
                }
                continue
            instruction = task.get("instruction") or ""
            # Prepend the user's original question so the specialist can
            # pattern-match against it (e.g. Hard rule 0 in specialist_sector
            # routes "refinanced at X% higher" → compute_debt_refi_impact even
            # when the GP's dispatch prescribed a manual bm25_sec recipe).
            # The GP sometimes paraphrases away the trigger phrase; the raw
            # question always contains it.
            if query_text and persona_key in ("sector_analyst", "stock_analyst"):
                instruction = (
                    f"=== USER'S ORIGINAL QUESTION (verbatim) ===\n"
                    f"{query_text.strip()}\n"
                    f"=== END USER QUESTION ===\n\n"
                    f"=== GP DISPATCH INSTRUCTION ===\n"
                    f"{instruction}"
                )
                # ``alphacumen.planner.dispatch_table`` carries a deterministic
                # keyword-bag matcher + canonical intent template per
                # known dispatch pattern. It was wired in here briefly
                # to bypass planner-LLM paraphrasing of dispatch
                # instructions for cohort-style answer questions, but
                # the deterministic match lost the planner LLM's
                # judgment on ambiguous / off-table queries while only
                # fixing the narrow subset of patterns we explicitly
                # encoded. Reverting to LLM-only dispatch routing
                # (measured tradeoff in regression-rate sweep). The
                # module is left in-tree as parked code; the seasonality
                # routing override below remains because that's a
                # narrow, well-characterised single-pattern fix.
                # Pattern-rewrite: for "[Month] seasonality" questions
                # the GP consistently misframes the task as a quarterly
                # Q[N]→Q[N+1] comparison (anchored on the question's
                # literal "Q[N+1] guidance" wording), bypassing the
                # specialist's Hard rule 5.5 monthly-seasonal recipe.
                # When we detect the pattern, append an OVERRIDE block
                # that names the right tool and tells the specialist
                # to ignore the GP's framing. Matches the same pattern
                # the synth-prompt gate is supposed to catch but can't
                # reliably enforce on its own.
                import re as _re
                _season_m = _re.search(
                    r"\bnormal\s+(\w+)\s+seasonality\b|\b(\w+)\s+seasonality\b",
                    query_text,
                    flags=_re.IGNORECASE,
                )
                if _season_m and persona_key == "sector_analyst":
                    _month = (_season_m.group(1) or _season_m.group(2)).strip().capitalize()
                    # Only kick in for actual calendar months.
                    if _month in {
                        "January", "February", "March", "April", "May", "June",
                        "July", "August", "September", "October", "November", "December",
                    }:
                        _prev_month = {
                            "January": "December", "February": "January", "March": "February",
                            "April": "March", "May": "April", "June": "May",
                            "July": "June", "August": "July", "September": "August",
                            "October": "September", "November": "October", "December": "November",
                        }[_month]
                        _q = (1 if _month in ("January", "February", "March") else
                              2 if _month in ("April", "May", "June") else
                              3 if _month in ("July", "August", "September") else 4)
                        instruction += (
                            f"\n\n=== ROUTING OVERRIDE (system) ===\n"
                            f"The question contains a calendar-month seasonality trigger "
                            f"('{_month} seasonality'). This is a MONTHLY seasonal-forecast "
                            f"question, NOT a quarterly Q[N]→Q[N+1] comparison — even if "
                            f"the question literally mentions 'Q[X] guidance'. Apply "
                            f"specialist Hard rule 5.5:\n"
                            f"1. **First call `fetch_foreign_monthly_revenue("
                            f"ticker=<TICKER>, fy_start_month=\"<Y-3>-01\", "
                            f"fy_end_month=\"<Y>-{_q:02d}\")`** to pull the full "
                            f"monthly revenue series at once. Foreign-private issuers "
                            f"typically publish monthly revenue as 6-K press releases "
                            f"that are NOT in the local sec_filings_chunked BM25 corpus; this "
                            f"tool bypasses the local index by querying sec-api.io's "
                            f"filings endpoint directly + EDGAR direct fetch + "
                            f"regex-extracting the canonical revenue values. The "
                            f"returned `month_over_month_growth` array already carries "
                            f"the {_prev_month}→{_month} growth-rate-by-year you need.\n"
                            f"2. Pull the Q[N-1] earnings 6-K for the USD revenue "
                            f"guidance and the issuer's outlook FX rate (filed mid-"
                            f"January for calendar-FY foreign-private issuers). The "
                            f"earnings 6-K is usually indexed in the local BM25 corpus "
                            f"even when monthly press-releases are not; try "
                            f"`bm25_sec(form_type:'6-K', ticker:<TICKER>, "
                            f"filed_at_gte:'<Y>-0115', filed_at_lte:'<Y>-0131')` first.\n"
                            f"3. Call `format_seasonal_forecast(ticker=<X>, "
                            f"target_year=<Y>, target_quarter={_q}, "
                            f"prior_month_name='{_prev_month}', "
                            f"target_month_name='{_month}', guidance_usd_low=<low>, "
                            f"guidance_usd_high=<high>, fx_local_per_usd=<fx>, "
                            f"history_growth_rates_pct=[...], history_years=[...], "
                            f"prior_month_actual_local=<...>, ytd_cumulative_local=<...>)`.\n"
                            f"4. Quote `answer.answer_summary_block` directly in "
                            f"your `answer_summary`. Do NOT paraphrase — the canonical "
                            f"answer expects the tool's exact phrasing."
                        )
            raw_steps = task.get("max_steps")
            steps_override = int(raw_steps) if isinstance(raw_steps, (int, float)) and raw_steps > 0 else None
            # Bump max_steps for monthly-seasonal-forecast questions —
            # they need 8 bm25_sec + 8 get_full_text + 1 tool call ≈ 17
            # rounds. The synth's default of 12 starves out the final
            # format_seasonal_forecast call.
            if (
                persona_key == "sector_analyst"
                and query_text
                and _re.search(r"\bnormal\s+\w+\s+seasonality|\b\w+\s+seasonality\b", query_text, _re.IGNORECASE)
            ):
                steps_override = max(steps_override or 0, 18)
            if steps_override is None and persona_key in ("sector_analyst", "stock_analyst"):
                steps_override = _auto_raise_budget(query_text, persona_key)
            # Honor a hard `force_max_steps` field on the task dict.
            # Unlike `max_steps_override` (raise-only by design),
            # `force_max_steps` lets the caller pin a budget BELOW the
            # spec's default. Used by competitive-analysis override
            # to cap sector_analyst at 5 rounds (default is 12) even
            # though Hard rule 5.13 says "use 4 rounds" — prevents
            # the 64-80s zero-content burn observed in run 3b0381b6
            # when the model ignores the prompt rule.
            raw_forced = task.get("force_max_steps")
            if isinstance(raw_forced, (int, float)) and raw_forced > 0:
                forced = int(raw_forced)
                # If the spec's default is higher than the forced cap,
                # we still want to apply the cap. Since the React
                # runtime takes `max_steps_override` as raise-only, we
                # need to actually mutate the spec's view here. Simplest:
                # build a shallow copy of the spec with the lower
                # ceiling and substitute it for this invocation.
                if forced < spec.max_steps:
                    import dataclasses as _dc
                    try:
                        spec = _dc.replace(spec, max_steps=forced)
                        steps_override = None  # spec default IS now the cap
                    except (TypeError, ValueError):
                        # spec isn't a dataclass — fall back to passing
                        # forced through max_steps_override (which will
                        # be a no-op due to the raise-only rule, but
                        # better than crashing).
                        steps_override = forced
                else:
                    # Forced cap is >= default: equivalent to raise.
                    steps_override = forced
            # Thread-local temporal ceiling: the synthesizer can set
            # filed_at_lte per specialist to prevent recency bias.
            task_ceiling = task.get("filed_at_lte")
            # Also auto-parse from instruction text: if the instruction
            # contains "filed_at_lte=YYYY-MM-DD", extract it.
            if not task_ceiling and instruction:
                import re as _re
                _ceiling_match = _re.search(
                    r"filed_at_lte\s*[=:]\s*(\d{4}-\d{2}-\d{2})", instruction
                )
                if _ceiling_match:
                    task_ceiling = _ceiling_match.group(1)
            futures[
                pool.submit(
                    _run_one_specialist,
                    spec=spec,
                    model=model,
                    instruction=instruction,
                    round_idx=round_idx,
                    invocation_idx=i,
                    index_caps=index_caps,
                    asof=asof,
                    max_steps_override=steps_override,
                    filed_at_lte=task_ceiling,
                )
            ] = i
        for fut in _cf.as_completed(futures):
            i = futures[fut]
            outputs[i] = fut.result()
    return outputs


# ---------------------------------------------------------------------------
# Common-thread + token aggregation
# ---------------------------------------------------------------------------

def _summarize_decision_for_thread(decision: SynthesizerDecision) -> str:
    """Render a synthesizer decision into the common-thread post.

    Mirrors prod's narration shape so the GP's next-round read of
    the thread sees the same kind of message it produced previously.
    """
    parts: list[str] = []
    if decision.reasoning:
        parts.append(decision.reasoning)
    if decision.pruning_notes:
        parts.append(f"PRUNING: {decision.pruning_notes}")
    if not decision.converged and decision.invoke_next:
        names = ", ".join(t["persona_key"] for t in decision.invoke_next)
        parts.append(f"Invoking: {names}")
    if decision.converged:
        parts.append("CONVERGED -- final answer produced.")
    if not parts:
        return "Orchestrating..."
    return " | ".join(parts)


def _build_equity_chart_from_specialists(
    specialist_outputs: Sequence[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    """Lift OHLC bars from a stock_analyst's captured get_equity_bars
    result into the ``equity_chart`` payload the webapp renders.

    Matches memory-demo's ``equity_chart`` shape
    (``{symbol, interval, y_default, points: [{t, open, high, low,
    close, volume}, ...]}``) so the same webapp component
    (``MemoryDemoEquitySection``) renders alphacumen runs without any
    client-side changes. We prefer the stock_analyst's capture (the
    ticker window is intentionally aligned with its as-of date), but
    fall back to any specialist that happened to call
    ``get_equity_bars``.

    Returns ``None`` when no specialist captured bars or the captured
    rows are empty -- mirrors memory-demo's "skip chart on no-intent"
    branch so the webapp just doesn't render the section.
    """
    ordered_keys = ("stock_analyst",)

    def _first_bars_capture(out: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Return a ``{symbol, rows[...]}`` capture from either the
        direct ``get_equity_bars`` tool or the ``compute_technicals``
        envelope (which carries the underlying bars on its ``bars``
        field so a chart can still be drawn when the model only
        reached for technicals). Prefers a direct get_equity_bars
        capture when both are present.
        """
        caps = out.get("captured_tool_results")
        if not isinstance(caps, Mapping):
            return None
        direct = caps.get("get_equity_bars")
        if isinstance(direct, list) and direct:
            first = direct[0]
            if isinstance(first, Mapping):
                return first
        tech = caps.get("compute_technicals")
        if isinstance(tech, list) and tech:
            for entry in tech:
                if not isinstance(entry, Mapping):
                    continue
                bars = entry.get("bars")
                if isinstance(bars, list) and bars:
                    return {
                        "symbol": entry.get("symbol"),
                        "rows": bars,
                    }
        return None

    capture: Optional[Mapping[str, Any]] = None
    for preferred in ordered_keys:
        for out in specialist_outputs:
            if (out.get("key") or out.get("persona_key")) == preferred:
                capture = _first_bars_capture(out)
                if capture is not None:
                    break
        if capture is not None:
            break
    if capture is None:
        for out in specialist_outputs:
            capture = _first_bars_capture(out)
            if capture is not None:
                break
    if capture is None:
        return None

    rows = capture.get("rows")
    if not isinstance(rows, list) or not rows:
        return None

    points: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, Mapping):
            continue
        # ``date`` on the equity_bars_v1 index comes back as either
        # a bare YYYY-MM-DD string or a timestamp (``2026-03-25
        # 00:00:00``). Normalise to the first 10 chars so the
        # webapp's x-axis formatter always sees a clean date.
        date_raw = r.get("date") or r.get("t") or ""
        if not isinstance(date_raw, str):
            date_raw = str(date_raw)
        point: dict[str, Any] = {"t": date_raw[:10]}
        for k in ("open", "high", "low", "close", "volume"):
            v = r.get(k)
            if v is not None:
                point[k] = v
        points.append(point)

    if not points:
        return None

    return {
        "symbol": str(capture.get("symbol") or ""),
        "interval": "1d",
        "y_default": "close",
        "points": points,
    }


def _build_common_thread_summary(
    common_thread: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Render the common thread for the API + sum per-message metrics.

    Returns the list of API rows + a totals dict. Matches prod's
    :func:`serialize_swarm_thread_for_api` shape so the gateway's
    Console / IA UI doesn't need to special-case alphacumen: each row
    carries ``agent``, ``preview`` (first 300 chars), ``full_text``
    (the entire post), and an optional ``metrics`` block when the
    message had per-call usage attached.
    """
    rows: list[dict[str, Any]] = []
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "tool_calls": 0,
    }
    for msg in common_thread:
        role = msg.get("role")
        name = msg.get("name") or role or "agent"
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        row: dict[str, Any] = {
            "agent": name,
            "preview": content[:300],
            "full_text": content,
        }
        round_no = msg.get("round")
        if isinstance(round_no, int):
            row["round"] = round_no
        metrics = msg.get("metrics")
        if isinstance(metrics, Mapping):
            cleaned = {
                k: int(metrics.get(k) or 0)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "tool_calls",
                )
            }
            latency = metrics.get("latency_ms")
            if isinstance(latency, int):
                cleaned["latency_ms"] = latency
            row["metrics"] = cleaned
            for k in totals:
                totals[k] += cleaned.get(k, 0)
        rows.append(row)
    return rows, totals


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def run(
    query: str,
    *,
    model: Optional[str] = None,
    framework: str = "langgraph",
    roster: Optional[Sequence[str]] = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    pruning_interval: int = DEFAULT_PRUNING_INTERVAL,
    pipeline: str = "investment_analyst",
    mode: str = "live",
    asof: Optional[str] = None,
    profile: Optional[str] = None,
    constraints: Optional[HarnessConstraints] = None,
    enforcer: Optional[ConstraintEnforcer] = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Run the IA swarm against a single query end-to-end.

    Parameters mirror the platform's pipeline-callable contract.

    - ``model`` -- the model id used by the entire run (specialists,
      planner / synthesizer, postprocessor). Falls back to the
      ``profile`` kwarg, then :data:`DEFAULT_PROFILE`. Must appear
      in the manifest's ``models`` allowlist.
    - ``framework`` -- accepted for contract compliance; ignored
      (the swarm is framework-free).
    - ``roster`` -- optional override of which specialists are
      available to the GP. Default: :data:`INVESTMENT_ANALYST_ROSTER`.
      Useful for eval runs that want to ablate one specialist at a
      time.
    - ``max_rounds`` -- hard cap on synthesizer rounds. The GP is
      forced to converge at the last round via a per-round hint.
    - ``pruning_interval`` -- after this many new specialist
      messages, the GP gets a "review + prune" hint instead of the
      default review-or-converge nudge.
    - ``pipeline`` -- only ``"investment_analyst"`` is supported.
    - ``mode`` -- ``"live"`` (default) or ``"backtest"``. Set by the
      platform from ``SubmitBatchRequest.mode``; pipelines may also
      override locally for tests. Today only used to flip the
      prompt scaffold via ``asof``.
    - ``asof`` -- ISO-8601 UTC string when ``mode='backtest'``;
      ``None`` otherwise. The platform's tool-kernel clamp guarantees
      retrieval results are point-in-time-correct against this asof;
      the swarm uses it to render ``{today}`` in specialist + planner
      prompts so the LLM's own reasoning is also anchored there
      (otherwise the model would treat wallclock now as the present
      and leak post-asof world knowledge into the answer).
    - ``**_kwargs`` -- forward-compat sink. The platform may add
      new kwargs over time; alphacumen silently drops unknown ones.

    Returns a dict the platform persists verbatim into
    ``SandboxRun.resultJson``::

        {
          "success": bool,
          "answer": dict | None,            # the structured final_answer
          "answer_summary": str | None,     # convenience shortcut
          "final_answer": dict | None,      # alias for answer (prod-shape)
          "memo_id": str | None,            # minted on success (investment_analyst)
          "memory_id": str | None,          # set when the memo was persisted
          "rounds": int,
          "common_thread_length": int,
          "common_thread_summary": [        # API thread rows
            {agent, preview, full_text, round?, metrics?}, ...
          ],
          "token_usage": {
            input_tokens, output_tokens, cached_tokens, tool_calls,
          },
          "elapsed_ms": int,
          "error": str | None,
          "pipeline": str,
          "model": str,                     # one model id for the whole run
          "roster": [str, ...],
          "specialist_outputs": [           # per-invocation rich detail
            {
              # canonical envelope (back-compat with prod swarm shape)
              key, label, round, invocation, instruction,
              payload, trajectory, thread_text, error,
              # convenience fields lifted from the trajectory so the
              # Console / IA UI can render an invocation row without
              # walking ``trajectory.*``. ``tool_calls`` is the total
              # number of tool dispatches (matches
              # ``token_usage.tool_calls``); LLM-turn count lives on
              # ``trajectory.rounds``.
              persona_key, round_index, success, tool_calls,
              token_usage, latency_ms,
            }, ...
          ],
          "synthesizer_rounds": [           # per-round GP detail
            {round, round_index, converged, invoke_next, final_answer,
             pruning_notes, reasoning, attempts, latency_ms,
             token_usage, error, raw_assistant_text}, ...
          ],
        }
    """
    del _kwargs

    if pipeline != "investment_analyst":
        return _early_exit(
            error=(
                f"unsupported pipeline={pipeline!r}; only "
                "'investment_analyst' is implemented"
            ),
            model=model or DEFAULT_MODEL,
            roster=tuple(roster) if roster else INVESTMENT_ANALYST_ROSTER,
            pipeline=pipeline,
        )

    # Resolve the active model for this run. Caller-pinned ``model``
    # wins; otherwise the profile (``profile`` kwarg / ``CB_IA_PROFILE``
    # / ``CB_IA_MODEL`` / :data:`DEFAULT_PROFILE`) picks the id. Every
    # stage — specialists, planner / synthesizer, postprocessor — uses
    # this same id; no per-stage divergence is supported.
    chosen_model = _alias_model(
        model if model else resolve_active_profile(profile_name=profile)
    )
    chosen_roster: tuple[str, ...] = (
        tuple(roster) if roster else INVESTMENT_ANALYST_ROSTER
    )

    # Open the Langfuse root span before any LLM call so every
    # :func:`chat_with_retry` emission below nests under it. The
    # trace is the single seam alphacumen uses -- ``runtime.chat_with_retry``
    # and the ReAct tool dispatcher pick it up via
    # :func:`_langfuse.get_active`, so we don't have to thread a
    # trace kwarg through synthesizer / specialists / tools.
    trace = _langfuse.RunTrace(
        request_id=_langfuse.resolve_request_id(),
        pipeline=pipeline,
        query=query,
        model=chosen_model,
    )
    trace.start()
    _langfuse.set_active(trace)

    logger.info(
        "alphacumen.swarm.run start pipeline=%s request_id=%s trace_id=%s "
        "langfuse_url=%s langfuse_enabled=%s model=%s "
        "roster=%s max_rounds=%d pruning_interval=%d mode=%s asof=%s "
        "query=%r",
        pipeline, trace.request_id, trace.trace_id, trace.trace_url,
        _langfuse.is_enabled(), chosen_model,
        list(chosen_roster), max_rounds, pruning_interval,
        mode, asof, query or "",
    )

    t0 = time.perf_counter()

    # Constraint refactor (Pass 6): bind HarnessConstraints + enforcer
    # for this run via context-vars. If the caller didn't pass an
    # explicit ``constraints`` but did pass legacy ``asof``, build one
    # from the loose kwargs so back-compat callers still get
    # enforcement engaged.
    if constraints is None and asof is not None:
        constraints = HarnessConstraints.from_legacy_kwargs(
            asof=asof,
            max_rounds=max_rounds,
        )
    _harness_c_token, _harness_e_token, _harness_enforcer = begin_run(
        constraints, enforcer,
    )

    result_for_trace: Optional[dict[str, Any]] = None
    trace_error: Optional[BaseException] = None
    try:
        try:
            spec_configs = {
                cfg.key: cfg for cfg in specialists_for(chosen_roster)
            }
        except KeyError as exc:
            result_for_trace = _early_exit(
                error=f"unknown specialist key: {exc!s}",
                model=chosen_model,
                roster=chosen_roster,
                pipeline=pipeline,
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
            )
            return result_for_trace

        # Slice 5d: discover the per-index typed schema once at swarm
        # startup so every specialist's tool descriptions carry the
        # model-facing field / table / predicate names. Falls back to
        # an empty map (static descriptions) if the gateway is
        # unreachable -- discovery hiccups must not gate the swarm.
        index_caps = fetch_index_capabilities()

        common_thread: list[dict[str, Any]] = [
            {"role": "user", "name": "user", "content": query, "round": 0},
        ]
        specialist_outputs: list[dict[str, Any]] = []
        synthesizer_rounds: list[dict[str, Any]] = []
        last_synth_idx = len(common_thread)
        rounds_completed = 0
        final_decision: Optional[SynthesizerDecision] = None

        planner_loaded_skills: list[str] = []

        for round_count in range(1, max_rounds + 1):
            new_messages = len(common_thread) - last_synth_idx
            logger.info(
                "alphacumen planner round=%d/%d starting new_messages=%d "
                "thread_len=%d",
                round_count, max_rounds, new_messages, len(common_thread),
            )

            sys_prompt = _planner_system_prompt(chosen_roster, asof=asof)
            hint = _planner_round_hint(
                round_count=round_count,
                max_rounds=max_rounds,
                new_messages=new_messages,
                pruning_interval=pruning_interval,
            )
            loaded_block = (
                render_loaded(planner_loaded_skills)
                if planner_loaded_skills else ""
            )
            sections = [
                f"=== COMMON THREAD ===\n"
                f"{format_common_thread(common_thread)}\n===",
            ]
            if loaded_block:
                sections.append(loaded_block)
            sections.append(hint)

            from alphacumen.tools import (  # noqa: PLC0415
                LOAD_PLANNER_SKILL,
                RUN_PYTHON,
                build_planner_dispatch_tools,
            )
            from alphacumen.roster import (  # noqa: PLC0415
                SPECIALIST_BRIEFS,
                SPECIALIST_CONFIGS as _SPEC_CFG,
            )
            _dispatch_tools = build_planner_dispatch_tools(
                chosen_roster, _SPEC_CFG, SPECIALIST_BRIEFS,
            )
            planner_traj = run_react(
                model=chosen_model,
                system_prompt=sys_prompt,
                user_message="\n\n".join(sections),
                tools=(*_dispatch_tools, LOAD_PLANNER_SKILL, RUN_PYTHON),
                max_steps=6,
                max_tokens=6_144,
                log_label="planner",
            )
            # Walk planner_traj.steps and partition tool calls into:
            # - run_python -> surface result onto common_thread
            # - load_skill -> add ids to planner_loaded_skills
            # - dispatch_<persona> -> collect into invoke_next
            # The planner emits these as native tool calls and the
            # runtime captures them here for the swarm dispatch loop.
            invoke_next: list[dict[str, Any]] = []
            roster_set = set(chosen_roster)
            for step in planner_traj.steps or []:
                if step.kind != "tool":
                    continue
                name = step.name or ""
                if name == "run_python":
                    py_result = step.result_preview or ""
                    common_thread.append({
                        "role": "planner_python",
                        "name": "planner_python",
                        "round": round_count,
                        "content": (
                            f"[planner run_python | round={round_count}]: "
                            f"{py_result}"
                        ),
                    })
                    continue
                if name == "load_skill":
                    args = step.arguments or {}
                    ids = args.get("skill_ids") or []
                    new_ids = validate_ids(ids)
                    for sid in new_ids:
                        if sid not in planner_loaded_skills:
                            planner_loaded_skills.append(sid)
                    continue
                if name.startswith("dispatch_"):
                    persona = name[len("dispatch_"):]
                    if persona not in roster_set:
                        continue
                    args = step.arguments or {}
                    instr = args.get("instruction") or ""
                    if not isinstance(instr, str) or not instr.strip():
                        continue
                    # Prefix the typed `ticker` arg onto the
                    # instruction so the existing specialist
                    # dispatch pipeline (which reads a single
                    # instruction string) carries the structured
                    # entity through verbatim.
                    ticker = args.get("ticker")
                    if isinstance(ticker, str) and ticker.strip():
                        instr = f"{ticker.strip().upper()}: {instr.strip()}"
                    else:
                        instr = instr.strip()
                    entry: dict[str, Any] = {
                        "persona_key": persona,
                        "instruction": instr,
                    }
                    ms = args.get("max_steps")
                    if isinstance(ms, int) and ms > 0:
                        entry["max_steps"] = ms
                    invoke_next.append(entry)
            final_msg = planner_traj.final_message or {}
            raw_final = (final_msg.get("content") or "") if isinstance(final_msg, Mapping) else ""
            # Reasoning is the planner's final assistant text.
            # Convergence is implicit (no dispatch_* tool calls in
            # this round) and the text content carries the rationale.
            reasoning = raw_final.strip() if isinstance(raw_final, str) else None
            converged = not invoke_next
            decision = SynthesizerDecision(
                converged=converged,
                invoke_next=invoke_next,
                final_answer=None,
                pruning_notes=None,
                reasoning=reasoning,
                raw_assistant_text=raw_final,
                token_usage=planner_traj.token_usage,
                latency_ms=0,
                attempts=planner_traj.rounds or 0,
                error=planner_traj.error,
            )
            if planner_traj.error:
                decision.converged = True  # type: ignore[misc]
            rounds_completed = round_count
            final_decision = decision

            invoke_names = [t.get("persona_key") for t in decision.invoke_next]
            logger.info(
                "alphacumen synthesizer round=%d decision converged=%s "
                "invoke_next=%s attempts=%d latency_ms=%d tokens_in=%d "
                "tokens_out=%d error=%s",
                round_count, decision.converged, invoke_names,
                decision.attempts, decision.latency_ms,
                decision.token_usage.get("input_tokens", 0),
                decision.token_usage.get("output_tokens", 0),
                decision.error,
            )

            # Code-level dispatch override: ecosystem / landscape /
            # private-company-network queries should NOT fan out to
            # sector_analyst or stock_analyst (no SEC signal on private
            # startups; no price angle on ecosystem questions). The
            # planner skill dispatch row covers this but the GP's
            # dispatch-widely prior crowds it out — observed in
            # production run ef421297. Strip them here, after the synth
            # decision, before the fan-out.
            if (
                not decision.converged
                and decision.invoke_next
                and _is_ecosystem_landscape_query(query)
            ):
                kept = [
                    t for t in decision.invoke_next
                    if t.get("persona_key") in _ECOSYSTEM_KEEP_SPECIALISTS
                ]
                if kept and len(kept) < len(decision.invoke_next):
                    stripped = [
                        t.get("persona_key") for t in decision.invoke_next
                        if t.get("persona_key") not in _ECOSYSTEM_KEEP_SPECIALISTS
                    ]
                    logger.warning(
                        "alphacumen synthesizer round=%d ECOSYSTEM OVERRIDE: "
                        "query matched ecosystem/landscape pattern; "
                        "stripping %s from invoke_next, keeping %s",
                        round_count,
                        stripped,
                        [t.get("persona_key") for t in kept],
                    )
                    # Mutate decision.invoke_next so the downstream
                    # serialization (line ~1734) records the filtered
                    # list — preserves audit-trail honesty.
                    try:
                        decision.invoke_next = kept  # type: ignore[misc]
                    except (AttributeError, TypeError):
                        # If the decision is frozen, we still pass `kept`
                        # to _run_round_specialists below; the serialized
                        # decision will record the GP's original list.
                        pass
                    invoke_names = [t.get("persona_key") for t in kept]

            # Code-level dispatch override: competitive-analysis
            # narrative queries ("how has X's competitive position
            # changed", etc.). Strip stock_analyst from invoke_next
            # (price/options aren't competitive-landscape signal) AND
            # clamp sector_analyst max_steps to 5 (Hard rule 5.13's
            # recipe needs 4-5 rounds; allowing 12 just means burning
            # 7 wasted rounds when the model ignores the prompt rule).
            # See _is_competitive_analysis_query for the trigger
            # rationale + the plateau pattern this handles.
            if (
                not decision.converged
                and decision.invoke_next
                and _is_competitive_analysis_query(query)
            ):
                kept = [
                    t for t in decision.invoke_next
                    if t.get("persona_key") in _COMPETITIVE_ANALYSIS_KEEP_SPECIALISTS
                ]
                stripped = [
                    t.get("persona_key") for t in decision.invoke_next
                    if t.get("persona_key") not in _COMPETITIVE_ANALYSIS_KEEP_SPECIALISTS
                ]
                # Clamp sector_analyst's max_steps to 5. Use
                # `force_max_steps` (vs `max_steps`) because
                # `max_steps_override` in _run_one_specialist is
                # "raise-only" (line 575) by design — it lets the
                # synth bump up a specialist's budget for complex
                # queries but won't let it drop below the spec's
                # default. We need to override that for this case:
                # sector_analyst's default is 12, but for competitive-
                # analysis we want a HARD cap of 5. _run_round_specialists
                # reads `force_max_steps` and uses it absolutely.
                clamped_sector = False
                for t in kept:
                    if t.get("persona_key") == "sector_analyst":
                        try:
                            t["force_max_steps"] = _COMPETITIVE_ANALYSIS_MAX_STEPS
                            clamped_sector = True
                        except TypeError:
                            # Frozen mapping — log but proceed; the
                            # specialist will use its default budget.
                            pass
                if kept and (stripped or clamped_sector):
                    logger.warning(
                        "alphacumen synthesizer round=%d COMPETITIVE-ANALYSIS "
                        "OVERRIDE: query matched competitive-position/"
                        "strategy-evolution pattern; stripping %s from "
                        "invoke_next, keeping %s; sector_analyst "
                        "max_steps clamped to %d (was %s)",
                        round_count,
                        stripped or "<none>",
                        [t.get("persona_key") for t in kept],
                        _COMPETITIVE_ANALYSIS_MAX_STEPS,
                        "clamped" if clamped_sector else "not changed",
                    )
                    try:
                        decision.invoke_next = kept  # type: ignore[misc]
                    except (AttributeError, TypeError):
                        pass
                    invoke_names = [t.get("persona_key") for t in kept]

            # Code-level dispatch override: multi-issuer regulatory
            # queries ("regulatory risks around X, Y, Z"). Strip
            # sector_analyst (10-K Risk Factors are boilerplate for
            # mega-caps and don't carry the "latest" regulatory signal
            # the question asks for) AND stock_analyst (no price angle
            # on regulatory). Keep vc_analyst + risk_analyst (the
            # GDELT + scraped-news signal IS the regulatory landscape).
            # See _is_multi_issuer_regulatory_query comment block for
            # the empirical motivation (TikTok run 076b03dc, 57s
            # critical-path on sector_analyst running twice).
            if (
                not decision.converged
                and decision.invoke_next
                and _is_multi_issuer_regulatory_query(query)
            ):
                kept = [
                    t for t in decision.invoke_next
                    if t.get("persona_key") in _MULTI_ISSUER_REGULATORY_KEEP_SPECIALISTS
                ]
                if kept and len(kept) < len(decision.invoke_next):
                    stripped = [
                        t.get("persona_key") for t in decision.invoke_next
                        if t.get("persona_key") not in _MULTI_ISSUER_REGULATORY_KEEP_SPECIALISTS
                    ]
                    logger.warning(
                        "alphacumen synthesizer round=%d MULTI-ISSUER-REGULATORY "
                        "OVERRIDE: query matched regulatory-risks pattern "
                        "with %d+ named entities; stripping %s from "
                        "invoke_next, keeping %s",
                        round_count,
                        _MULTI_ISSUER_REGULATORY_MIN_ENTITIES,
                        stripped,
                        [t.get("persona_key") for t in kept],
                    )
                    try:
                        decision.invoke_next = kept  # type: ignore[misc]
                    except (AttributeError, TypeError):
                        pass
                    invoke_names = [t.get("persona_key") for t in kept]

            # NOTE: TRAJECTORY FORCE-IN (cb-ia 0.0.305-0.0.307) was
            # removed in 0.0.308. The override added sector_analyst on
            # single-issuer multi-year trajectory queries whenever the
            # synth had skipped it, on the theory that Hard rule 5.14
            # (Item 1 + XBRL trajectory) needed sector_analyst to fire.
            # Measured cost: +1 specialist on every trajectory query =
            # +20-60s wallclock. Measured benefit: incremental — the
            # synth picks sector_analyst on its own most of the time on
            # gpt-oss-120b; the force-in fired in a minority of runs.
            # Net wasn't worth the latency floor. Hard rule 5.14 stays
            # as a prompt-side nudge; trust the synth's routing.

            synthesizer_rounds.append(_serialize_decision(decision, round_count))

            common_thread.append(
                {
                    "role": "synthesizer",
                    "name": "synthesizer",
                    "round": round_count,
                    "content": _summarize_decision_for_thread(decision),
                    "metrics": {
                        **decision.token_usage,
                        "latency_ms": decision.latency_ms,
                    },
                }
            )
            last_synth_idx = len(common_thread)

            if decision.converged:
                break

            if not decision.invoke_next:
                # GP didn't converge but also didn't ask for anything.
                # Force an exit so we don't infinite-loop on a confused
                # GP -- prod has the same guard via its route function
                # returning END.
                logger.warning(
                    "synthesizer round=%d returned no invoke_next without "
                    "converging; forcing convergence",
                    round_count,
                )
                break

            round_outputs = _run_round_specialists(
                invocations=decision.invoke_next,
                spec_configs=spec_configs,
                model=chosen_model,
                round_idx=round_count,
                index_caps=index_caps,
                asof=asof,
                query_text=query,
            )
            for out in round_outputs:
                usage = (out.get("trajectory") or {}).get("token_usage") or {}
                logger.info(
                    "alphacumen specialist=%s round=%d success=%s rounds=%d "
                    "tool_calls=%d latency_ms=%d tokens_in=%d tokens_out=%d "
                    "error=%s",
                    out.get("key"), round_count, out.get("success"),
                    int((out.get("trajectory") or {}).get("rounds") or 0),
                    out.get("tool_calls", 0),
                    out.get("latency_ms", 0),
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    out.get("error"),
                )
                specialist_outputs.append(out)
                traj = out.get("trajectory") or {}
                usage = traj.get("token_usage") or {}
                # Surface the specialist's grounding signal directly in
                # the GP-readable common-thread post. Without this the
                # synthesizer reads only the rendered ``answer_summary``
                # and has no way to distinguish a finding backed by tool
                # retrievals from one the model fabricated from
                # pre-training memory. The Vals AI row 5 failure
                # (vc_analyst returned specific dollar figures with
                # ``tool_calls=0``; one of the three numbers was
                # hallucinated) motivates this.
                #
                # We also flag ``__repaired_json`` payloads so the GP
                # treats truncation-recovered findings with caution
                # (the tail of ``answer_summary`` is missing).
                spec_tool_calls = int(usage.get("tool_calls") or 0)
                payload = out.get("payload") or {}
                was_repaired = bool(
                    isinstance(payload, Mapping)
                    and payload.get("__repaired_json")
                )
                was_quant_discarded = bool(
                    isinstance(payload, Mapping)
                    and payload.get("__discarded_for_no_retrieval")
                )
                marker_bits: list[str] = [f"tool_calls={spec_tool_calls}"]
                if was_quant_discarded:
                    # Stronger marker than the generic ``WARN: 0
                    # retrievals`` -- the runtime gate AND the
                    # swarm backstop have both fired, the answer
                    # text has been replaced with the discard
                    # notice, so this header just signals to the GP
                    # that the news leg is a hard miss for this
                    # round (not a soft "trust at your peril"
                    # output). See specialist_news_quant.md and the
                    # ``_enforce_quant_extract_no_retrieval``
                    # docstring for the failure mode.
                    marker_bits.append(
                        "DISCARDED: 0 retrievals despite must-retrieve "
                        "runtime gate -- payload replaced with failure "
                        "marker; treat as no news-side evidence; do NOT "
                        "re-route with the same instruction"
                    )
                elif spec_tool_calls == 0 and out.get("success"):
                    marker_bits.append(
                        "WARN: 0 retrievals -- any factual claims here "
                        "come from LLM memory, not tool output; "
                        "verify before quoting"
                    )
                if was_repaired:
                    marker_bits.append(
                        "WARN: response was truncated (max_tokens hit); "
                        "tail of answer_summary may be missing"
                    )
                marker = " | ".join(marker_bits)
                common_thread.append(
                    {
                        "role": "specialist",
                        "name": out["key"],
                        "round": round_count,
                        "content": (
                            f"[{out['label']} | {marker}]: "
                            f"{out.get('thread_text') or ''}"
                        ),
                        "metrics": {
                            "input_tokens": int(usage.get("input_tokens") or 0),
                            "output_tokens": int(usage.get("output_tokens") or 0),
                            "cached_tokens": int(usage.get("cached_tokens") or 0),
                            "tool_calls": int(usage.get("tool_calls") or 0),
                            # wall-clock run_react time for this specialist --
                            # the webapp's SwarmTraceTimeline reads this to
                            # size bars proportionally; without it the bars
                            # fall back to a token+tool-call proxy (see
                            # SwarmTraceTimeline.tsx ``proxyMs``).
                            "latency_ms": int(out.get("latency_ms") or 0),
                        },
                    }
                )

        # Last-chance synthesis pass for the max_rounds boundary case.
        # If the for-loop exited because ``max_rounds`` was exhausted
        # (rather than via converged-break or empty-invoke_next-break),
        # the final round was a DISPATCH round whose specialists ran
        # and landed in ``common_thread`` — but no synthesizer round
        # consumed them. Caller gets an empty answer despite the work
        # having been done. Observed in TikTok run 2b177467 (2026-05-25,
        # 0.0.304): synth round 2 dispatched sector_analyst → ran
        # successfully (572K input tokens, 12 rounds, success=True) →
        # run terminated immediately, empty answer_summary.
        #
        # The synth's ``FINAL ROUND -- you MUST converge`` per-round
        # hint already tells the synth not to do this, but gpt-oss-120b
        # ignored the hint on this query. So we enforce code-side:
        # run one more synth call with the same FINAL-ROUND framing,
        # and force-set converged=True even if the synth tries to
        # dispatch again. The dispatched specialists from the prior
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        thread_rows, token_totals = _build_common_thread_summary(common_thread)

        # --- Terminal synthesis: run the post-processor ---
        # The postprocessor is the ONLY terminal synthesis stage. Run
        # it unconditionally on whatever is in ``common_thread`` --
        # regardless of whether the planner declared converged=true,
        # ran out of rounds, or errored mid-loop. The planner's job is
        # routing; turning specialist findings into a final report is
        # the postprocessor's job. Only fall back to the deterministic
        # concat if the postprocessor itself fails to produce a
        # non-empty answer.
        if final_decision is not None and common_thread:
            logger.info(
                "alphacumen planner terminal synthesis: running postprocessor "
                "converged=%s planner_error=%s rounds_completed=%d "
                "loaded_skills=%s",
                final_decision.converged,
                final_decision.error,
                rounds_completed,
                planner_loaded_skills,
            )
            pp_result = postprocess(
                model=chosen_model,
                common_thread=common_thread,
                asof=asof,
            )
            pp_summary = ""
            if isinstance(pp_result.final_answer, Mapping):
                v = pp_result.final_answer.get("answer_summary")
                if isinstance(v, str):
                    pp_summary = v.strip()
            if pp_result.error or not pp_summary:
                # Postprocessor failed (LLM timeout / JSON-parse exhausted)
                # or returned empty prose. Last-resort: assemble specialist
                # outputs deterministically so the user-facing answer is
                # never empty.
                logger.warning(
                    "alphacumen postprocessor unusable (error=%s summary_chars=%d); "
                    "falling back to deterministic specialist concat",
                    pp_result.error, len(pp_summary),
                )
                fallback_summary = _assemble_fallback_summary(specialist_outputs)
                fallback_final = {
                    "answerable": bool(fallback_summary),
                    "answer_summary": fallback_summary,
                    "ranked_entities": _collect_ranked_entities(specialist_outputs),
                    "key_events": _collect_key_events(specialist_outputs),
                    "metrics_evidence": [],
                    "reasoning": (
                        "Postprocessor failed to produce a final report; "
                        "this answer was assembled deterministically from "
                        "specialist outputs as a last-resort safety net."
                    ),
                    "confidence": "low",
                }
                try:
                    final_decision.converged = bool(fallback_summary)  # type: ignore[misc]
                    final_decision.invoke_next = []  # type: ignore[misc]
                    final_decision.final_answer = fallback_final  # type: ignore[misc]
                    final_decision.error = (
                        pp_result.error or "postprocessor_empty_summary"
                    )  # type: ignore[misc]
                except (AttributeError, TypeError):
                    pass
            else:
                try:
                    final_decision.converged = True  # type: ignore[misc]
                    final_decision.invoke_next = []  # type: ignore[misc]
                    final_decision.final_answer = pp_result.final_answer  # type: ignore[misc]
                    # Clear any prior planner-loop error: the postprocessor
                    # successfully synthesized a final answer, so the run
                    # is successful even if the planner itself bailed.
                    final_decision.error = None  # type: ignore[misc]
                except (AttributeError, TypeError):
                    pass

        final_answer: Optional[dict[str, Any]] = None
        answer_summary: Optional[str] = None

        if final_decision is not None and final_decision.final_answer is not None:
            final_answer = _canonicalize_final_answer(final_decision.final_answer)
            v = (final_answer or {}).get("answer_summary")
            if isinstance(v, str):
                answer_summary = v

        success = bool(
            final_decision is not None
            and final_decision.converged
            and final_answer is not None
            and final_decision.error is None
            and answer_summary
        )

        error: Optional[str] = None
        if final_decision is not None and final_decision.error:
            error = final_decision.error
        elif final_decision is None:
            error = "no planner round completed"
        elif not final_decision.converged:
            error = "planner did not converge before max_rounds"

        # Equity chart is an IA-UI affordance (the webapp's
        # MemoryDemoEquitySection renders it).
        equity_chart = _build_equity_chart_from_specialists(specialist_outputs)

        memo_id: Optional[str] = None
        memory_id: Optional[str] = None
        if success:
            memo_id, memory_id = persist_memo(
                query=query,
                answer=final_answer,
                answer_summary=answer_summary,
                equity_chart=equity_chart,
                pipeline=pipeline,
                model=chosen_model,
                mode=mode,
                asof=asof,
                rounds=rounds_completed,
                elapsed_ms=elapsed_ms,
            )

        result_for_trace = {
            "success": success,
            "answer": final_answer,
            "answer_summary": answer_summary,
            "final_answer": final_answer,
            "memo_id": memo_id,
            "memory_id": memory_id,
            "rounds": rounds_completed,
            "common_thread_length": len(common_thread),
            "common_thread_summary": thread_rows,
            "token_usage": token_totals,
            "elapsed_ms": elapsed_ms,
            "error": error,
            "pipeline": pipeline,
            "model": chosen_model,
            "roster": list(chosen_roster),
            "specialist_outputs": specialist_outputs,
            "synthesizer_rounds": synthesizer_rounds,
            # Optional chart payload the webapp's
            # ``MemoryDemoEquitySection`` renders as an interactive
            # close-price chart + OHLCV table. Present whenever a
            # specialist (typically stock_analyst) called
            # ``get_equity_bars`` and got rows back; ``None`` when
            # the run didn't touch equity data. Same wire shape as
            # memory-demo's equity_chart so zero webapp changes are
            # needed.
            "equity_chart": equity_chart,
        }
        logger.info(
            "alphacumen.swarm.run done request_id=%s trace_id=%s langfuse_url=%s "
            "success=%s rounds=%d specialists=%d elapsed_ms=%d tokens_in=%d "
            "tokens_out=%d tool_calls=%d error=%s",
            trace.request_id, trace.trace_id, trace.trace_url,
            success, rounds_completed,
            len(specialist_outputs), elapsed_ms,
            int(token_totals.get("input_tokens") or 0),
            int(token_totals.get("output_tokens") or 0),
            int(token_totals.get("tool_calls") or 0),
            error,
        )
        return result_for_trace
    except BaseException as exc:
        # Preserve the original exception (re-raised below) while
        # still giving the Langfuse root span a status_message.
        trace_error = exc
        raise
    finally:
        # Condense the result to the fields that make a good root-span
        # output preview. The full dict is too large and mostly repeats
        # what the child observations already carry.
        summary: Optional[dict[str, Any]] = None
        if result_for_trace is not None:
            summary = {
                "success": result_for_trace.get("success"),
                "answer_summary": result_for_trace.get("answer_summary"),
                "rounds": result_for_trace.get("rounds"),
                "elapsed_ms": result_for_trace.get("elapsed_ms"),
                "error": result_for_trace.get("error"),
            }
        trace.end(output=summary, error=trace_error)
        _langfuse.set_active(None)
        # Pair end_run with the begin_run above. Must come AFTER the
        # langfuse teardown so a misbehaving enforcer's on_run_end
        # can't strand the trace state.
        end_run(
            _harness_c_token, _harness_e_token,
            _harness_enforcer, constraints,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_decision(
    decision: SynthesizerDecision, round_count: int,
) -> dict[str, Any]:
    """Lift a :class:`SynthesizerDecision` into a JSON-friendly dict."""
    return {
        "round": round_count,
        # Alias so consumers that group / index by ``round_index``
        # (matching the per-specialist envelope) don't have to learn
        # two different field names.
        "round_index": round_count,
        "converged": decision.converged,
        "invoke_next": [dict(t) for t in decision.invoke_next],
        "final_answer": (
            dict(decision.final_answer)
            if decision.final_answer is not None else None
        ),
        "pruning_notes": decision.pruning_notes,
        "reasoning": decision.reasoning,
        "raw_assistant_text": decision.raw_assistant_text,
        "token_usage": dict(decision.token_usage),
        "latency_ms": decision.latency_ms,
        "attempts": decision.attempts,
        "error": decision.error,
    }


def _early_exit(
    *,
    error: str,
    model: str,
    roster: Sequence[str],
    pipeline: str,
    elapsed_ms: int = 0,
) -> dict[str, Any]:
    """Produce a result dict for a swarm that never started."""
    return {
        "success": False,
        "answer": None,
        "answer_summary": None,
        "final_answer": None,
        "memo_id": None,
        "memory_id": None,
        "rounds": 0,
        "common_thread_length": 0,
        "common_thread_summary": [],
        "token_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "tool_calls": 0,
        },
        "elapsed_ms": elapsed_ms,
        "error": error,
        "pipeline": pipeline,
        "model": model,
        "roster": list(roster),
        "specialist_outputs": [],
        "synthesizer_rounds": [],
    }


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MODEL",
    "DEFAULT_PROFILE",
    "DEFAULT_PRUNING_INTERVAL",
    "INVESTMENT_ANALYST_ROSTER",
    "MODEL_PROFILES",
    "resolve_active_profile",
    "run",
]
