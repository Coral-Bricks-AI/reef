# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen.roster`` -- finance specialist roster + prompt loading.

Each specialist is a ``SpecialistConfig`` bundle (key, label, persona
prompt file, tools, max_steps, ...). :func:`alphacumen.swarm.run` loads
:data:`INVESTMENT_ANALYST_ROSTER`, fans out one
:func:`reef.react.run_react` per specialist in parallel, and hands
the JSON-shaped outputs to the planner/postprocessor for synthesis.

This is the **finance impl** of the specialist concept; the framework
side (:mod:`reef`) doesn't define a generic SpecialistConfig
yet -- once a non-finance harness needs to share orchestration code
with AlphaCumen, the data-only fields of SpecialistConfig lift to a
framework Protocol and the finance-specific bits (the sector_analyst
seed branch, the alphacumen prompt loaders) stay here.

Personas live as ``.md`` files under
``alphacumen/prompts/`` and are loaded via
:func:`importlib.resources` so they ship inside the wheel. The legacy
code embedded persona text in Python string literals; keeping them as
data-files lets the Console render them verbatim and makes prompt
iteration a non-code-review change.

Moved from ``alphacumen.roster`` during the reef/alphacumen split;
``alphacumen.roster`` is now a back-compat shim that re-exports the
public surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from typing import Optional, Sequence

from alphacumen import tools as ac_tools
from alphacumen.tools import Tool


_PROMPT_PACKAGE = "alphacumen.prompts"


def _load_sector_seed() -> str:
    """Read the slim sector seed prompt.

    The seed used to live next to its on-demand skills folder under
    ``alphacumen.sector``. After the reef/alphacumen split it ships
    inside ``alphacumen.prompts`` alongside the other finance
    persona prompts -- one home for everything the model reads at
    system-prompt-build time, while the skill data dirs sit next to
    each other under ``alphacumen``.
    """
    return resources.files(_PROMPT_PACKAGE).joinpath(
        "specialist_sector_seed.md"
    ).read_text(encoding="utf-8")


def augment_sector_instruction(instruction: str) -> str:
    """Attach ``=== LOADED SKILLS ===`` block to a sector instruction.

    Picks the top-k matching skills via the loader's keyword-overlap
    heuristic over the dispatch instruction. The planner stays at the
    layer of choosing which specialist + intent; identifying the right
    sector skill is the sector_analyst's responsibility (it also has
    the full skill index in its seed prompt + Hard rule 0 to scan and
    invoke_skill_fn into any skill it identifies). The pre-load via
    keyword overlap is an optimization that surfaces the skill body
    so sector_analyst doesn't have to cold-call invoke_skill_fn.
    """
    from alphacumen.skill_registry import (
        load_skills, render_loaded, suggest_ids,
    )
    skills = load_skills()
    ids = suggest_ids(instruction, top_k=4, min_overlap=2, skills=skills)
    if not ids:
        return instruction
    block = render_loaded(ids, skills=skills)
    if not block:
        return instruction
    return f"{instruction}\n\n{block}"


def _load_prompt(name: str) -> str:
    """Read a prompt template shipped under ``alphacumen/prompts/<name>``.

    Uses :func:`importlib.resources.files` so the wheel-installed
    package finds its data correctly (the manifest's
    ``[tool.setuptools].package-data`` entry must include
    ``alphacumen/prompts/*.md``). Falls back to a clear FileNotFoundError
    message when the prompt doesn't ship -- caught at swarm-startup
    time, never silently masked.
    """
    return resources.files(_PROMPT_PACKAGE).joinpath(name).read_text(
        encoding="utf-8"
    )


def _resolve_today(asof: Optional[str]) -> tuple[str, bool]:
    """Return ``(formatted_date, is_backtest)``.

    When ``asof`` is set (an ISO-8601 UTC timestamp from the platform
    gateway's per-run ``mode='backtest'`` plumbing) the date is pinned
    to that point in time and the second element flags backtest mode
    so the prompt template can switch its scaffold accordingly.

    When ``asof`` is ``None`` we fall back to wallclock UTC and run
    in live framing.
    """
    dt, is_backtest = _resolve_today_dt(asof)
    return dt.strftime("%B %d, %Y"), is_backtest


def _resolve_today_dt(asof: Optional[str]) -> tuple[datetime, bool]:
    """Return ``(parsed_datetime, is_backtest)``.

    Internal helper that exposes the parsed datetime so callers can
    derive year / yyyymmdd / etc. tokens from a single source of truth.
    """
    if asof is not None and asof.strip():
        s = asof.strip()
        normalised = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            return datetime.now(timezone.utc), False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed, True
    return datetime.now(timezone.utc), False


def _date_tokens(asof: Optional[str]) -> dict[str, str]:
    """Compute the full set of date-token substitutions for prompts.

    Persona files use these in example queries (e.g. ``"Q1 {today_year}
    deliveries"``) so live and backtest runs render the same template
    with year strings anchored to the current asof rather than a
    hardcoded literal that contradicts ``{today}``.
    """
    dt, _ = _resolve_today_dt(asof)
    year = dt.year
    return {
        "{today}": dt.strftime("%B %d, %Y"),
        "{today_year}": str(year),
        "{today_year_minus_1}": str(year - 1),
        "{today_year_minus_2}": str(year - 2),
        "{today_yyyymmdd}": dt.strftime("%Y%m%d"),
        "{today_iso}": dt.strftime("%Y-%m-%d"),
    }


def _apply_tokens(text: str, tokens: dict[str, str]) -> str:
    for k, v in tokens.items():
        text = text.replace(k, v)
    return text


def _build_base_instruction(
    *, tool_budget: int, asof: Optional[str] = None,
) -> str:
    _, is_backtest = _resolve_today_dt(asof)
    template = _load_prompt(
        "base_backtest.md" if is_backtest else "base.md"
    )
    rendered = _apply_tokens(template, _date_tokens(asof))
    return rendered.replace("{tool_budget}", str(tool_budget))


@dataclass(frozen=True)
class SpecialistConfig:
    """One specialist's static configuration.

    Frozen: instances are shared across runs and cached at the
    module level, so any per-run state belongs in
    :class:`reef.react.Trajectory`, not here.

    ``persona_prompt`` and ``supplement`` are the two halves of the
    specialist's system message: the persona sets identity / mandate
    (loaded from a separate file so it can be edited as prose), the
    supplement lists the operational tool playbook (also a file).
    The runtime concatenates ``base + persona + supplement`` into
    one system message.
    """

    key: str
    label: str
    prompt_file: str
    tools: tuple[Tool, ...]
    max_steps: int = 6
    # Output-token cap for the specialist's chat completion. The
    # provider default (4096) is fine for short personas, but
    # specialists with large prompt scaffolds (sector_analyst's tool
    # playbook is ~390 lines) routinely emit 4k tokens of internal
    # reasoning before getting to a tool_call structure, then hit the
    # cap with empty `final_message_content` and zero tool calls --
    # observed in practice on row 5 of the Vals AI eval. Raising the
    # cap for those specialists eliminates the silent-truncation
    # failure mode. Per-call cost scales linearly with `max_tokens`
    # only when the model actually fills the budget, which is rare,
    # so the cost penalty for raising it is small.
    max_tokens: int = 4096
    # Must-retrieve gate. When set to N >= 1 the specialist's ReAct
    # loop refuses to accept a no-tool-call assistant message as the
    # final answer until N tool dispatches have happened. See
    # :func:`reef.react.run_react`'s ``min_tool_calls_before_final``
    # docstring for the failure mode this prevents (Vals AI row 5
    # vc_analyst quant-extract: model emitted confident answer with
    # zero tool calls, fabricated citations from training memory).
    # Personas whose entire job is "extract a figure from a fresh
    # corpus you have no priors on" (news_quant_analyst) set this
    # to 1; narrative / analysis personas leave it at 0 because they
    # legitimately can answer some prompts from prior context
    # (clarifying / re-phrasing turns from the GP).
    min_tool_calls_before_final: int = 0
    # Temperature passed to ``run_react``. The runtime default is 0.2
    # for creative-narrative balance. For numeric-heavy personas
    # (sector_analyst — quotes verbatim XBRL / table figures + skill
    # ``answer_summary_block`` strings), lower temperature reduces
    # paraphrasing variance on the final answer composition step,
    # which improves rubric-atom match rates on Beat-or-Miss / KPI
    # / multi-issuer-ratio questions. Narrative-only personas
    # (vc_analyst) keep 0.2.
    temperature: float = 0.2

    def system_prompt(self, *, asof: Optional[str] = None) -> str:
        """Return the full system message for this specialist.

        ``asof`` (optional ISO-8601 UTC) pins the rendered ``{today}``
        date and switches the base scaffold to the backtest framing
        ("reasoning at a simulated point in time" rather than
        "operating in real-time"). Live runs leave it unset.

        For ``sector_analyst`` the body is the slim seed at
        ``alphacumen/sector/specialist_sector_seed.md`` -- the legacy
        monolithic ``alphacumen/prompts/specialist_sector.md`` was retired
        once the seed + on-demand skills layout in
        :mod:`alphacumen.skills` reached parity. The per-recipe
        skill bodies attach to each round's user_message in
        :func:`augment_sector_instruction`, keyed off the dispatch
        instruction.
        """
        base = _build_base_instruction(
            tool_budget=self.max_steps, asof=asof,
        )
        if self.key == "sector_analyst":
            prompt_body = _load_sector_seed()
            prompt_body = _apply_tokens(prompt_body, _date_tokens(asof))
            from alphacumen.skill_registry import load_skills, render_index
            prompt_body = prompt_body.replace(
                "{skill_index}", render_index(load_skills())
            )
        else:
            prompt_body = _load_prompt(self.prompt_file)
            # Persona files reference ``{tool_budget}`` plus the date
            # tokens (``{today_year}``, ``{today_year_minus_1}``, ...)
            # so example queries don't bake in a literal year that
            # contradicts ``{today}`` under backtest framing.
            # ``{today}`` itself is also exposed in the body now -- the
            # base block pins the canonical reference, but persona
            # examples sometimes need to echo the same date in their
            # own scaffolding (e.g. SEC filing-window suggestions).
            prompt_body = _apply_tokens(prompt_body, _date_tokens(asof))
        prompt_body = prompt_body.replace(
            "{tool_budget}", str(self.max_steps)
        )
        return f"{base}\n\n---\n\n{prompt_body}"


SPECIALIST_CONFIGS: dict[str, SpecialistConfig] = {
    "stock_analyst": SpecialistConfig(
        key="stock_analyst",
        label="Stock Analyst (price, technicals, macro)",
        prompt_file="specialist_stock.md",
        tools=ac_tools.STOCK_ANALYST_TOOLS,
        max_steps=6,
        # Raised to 6144: stock_analyst routinely fans out to 3
        # tickers × multiple tools (compute_technicals,
        # compute_options_stats, get_full_text on 8-Ks,
        # get_reddit_sentiment), then assembles a per-ticker
        # markdown table answer. Hit 4096 cap with truncated JSON
        # on Vals AI row 5 (the tail of `answer_summary` was cut
        # mid-sentence, breaking `extract_json_payload` and
        # forcing success=False even though the prose was usable).
        max_tokens=6144,
    ),
    "sector_analyst": SpecialistConfig(
        key="sector_analyst",
        label="Sector Analyst (filings, graph, fundamentals)",
        prompt_file="specialist_sector.md",
        # Slim roster: universal workhorses + the ``invoke_skill_fn``
        # dispatcher + every 1:1 recipe tool not yet migrated to a
        # folder-shaped skill. The full legacy roster
        # (``SECTOR_ANALYST_TOOLS``) is kept exported for external
        # gdelt experiments but no longer drives the sector_analyst's
        # own swarm path.
        tools=ac_tools.SECTOR_ANALYST_TOOLS_SLIM,
        # Multi-layer trend questions (per-period series + at least
        # one derived metric + an MD&A attribution sentence) need
        # enough tool-call rounds to do skill-load + filing retrieve
        # + MD&A drill-in + render in a single trajectory. At 12
        # steps the specialist could produce each layer
        # individually but not all in one response. Bumped to 20.
        max_steps=20,
        # Output budget. ~390-line tool-playbook prompt + base
        # instruction = ~12k input tokens, and the specialist needs
        # room to (a) lay out its multi-step retrieval plan, (b)
        # emit several tool_calls per turn, and (c) write a
        # multi-section markdown `answer_summary` quoting verbatim
        # filing values. At the 4096 default the model was hitting
        # the cap on planning alone and never emitting any
        # tool_calls. 16384 gives generous headroom for trend +
        # decomposition + attribution answers.
        max_tokens=16384,
        # Must-retrieve gate. Required because the specialist was
        # observed to occasionally emit a confident "no retrievable
        # data found" final answer WITHOUT making any tool call — the
        # answer hallucinated specific bm25_sec queries it never ran
        # ("bm25_sec search returned 0 results"). The runtime ReAct
        # loop now refuses to accept a no-tool-call final answer; the
        # specialist must actually search before claiming search
        # results. If retrieval genuinely fails, the model can report
        # that AFTER at least one real call.
        min_tool_calls_before_final=1,
        # REVERTED back to default 0.2.
        #
        # I tried 0.0 to reduce numeric-atom rendering variance
        # (rows 5 + 32 had sometimes-paraphrased tool output).
        # Empirically temp=0 made things worse: at 0.0 the model
        # deterministically collapsed to a "narrative + methodology"
        # answer path that skipped tool calls entirely. Two re-smokes
        # of each affected row at temp=0.0 returned 0-25% partial
        # vs 75-100% at temp=0.2 (variance-dependent). The lower
        # temperature locked the model into a single (wrong) framing
        # rather than letting the must-retrieve gate + skill index
        # pull it toward the tool-call path.
        #
        # Leaving the SpecialistConfig.temperature plumbing in
        # place — it's the right knob; this persona just wants 0.2.
        # temperature=0.2,  # (default — uncomment to override)
    ),
    "vc_analyst": SpecialistConfig(
        key="vc_analyst",
        label="VC Analyst (growth, TAM, long-tail web)",
        prompt_file="specialist_vc.md",
        tools=ac_tools.VC_ANALYST_TOOLS,
        # Raised from 4 to 6: get_full_text for scraped articles needs
        # room for BM25 + get_full_text + vector + RRF + synthesis.
        # The per-entity decomposition issue is now mitigated by the
        # synthesizer's multi-entity breadth rule.
        max_steps=6,
    ),
    "risk_analyst": SpecialistConfig(
        key="risk_analyst",
        label="Risk Analyst (tail risk, ecosystem, regime)",
        prompt_file="specialist_risk.md",
        tools=ac_tools.RISK_ANALYST_TOOLS,
        max_steps=6,
    ),
    "news_quant_analyst": SpecialistConfig(
        # Narrow figure-extraction-from-news persona, separate from
        # vc_analyst (Mary Meeker / qualitative narrative). Created
        # because the prior approach of bolting a "QUANT-EXTRACT
        # MODE" onto vc_analyst's persona had a structural bias
        # problem: ~250 lines of "you do narrative TAM analysis,
        # not legacy quarterly numbers" framing dominated the
        # model's behavior even when the GP explicitly routed it a
        # quant-extract dispatch. The model would skip retrieval
        # entirely and emit a confident answer from training memory
        # (Vals AI row 5 on cb-ia 0.0.149: tool_calls=0 + 4220 chars
        # of fabricated Reuters/Bloomberg citations with the wrong
        # AMZN capex figure). A dedicated short-prompt persona +
        # the must-retrieve runtime gate together fix the failure.
        key="news_quant_analyst",
        label="News Quant Analyst (figure extraction from news)",
        prompt_file="specialist_news_quant.md",
        # Same tools as vc_analyst -- we want access to the same
        # scraped-articles + GDELT corpora -- but a different
        # persona prompt so the model doesn't inherit the
        # narrative framing.
        tools=ac_tools.VC_ANALYST_TOOLS,
        # 6 steps: 1 BM25 + 1 vector + 1 optional RRF + 1 synthesis
        # leaves 2 steps of headroom for an extra search if the
        # first pair returned nothing useful.
        max_steps=6,
        # Same 6144 cap as vc_analyst -- the prompt is shorter but
        # the model still emits a structured per-ticker payload
        # which can run long for 3+ entity questions.
        max_tokens=6144,
        # The must-retrieve gate. With this set to 1 the runtime
        # ReAct loop (reef.react.run_react) refuses to accept a
        # no-tool-call assistant message as the final answer until
        # at least 1 tool dispatch has happened. Capped coercion
        # turns inside run_react prevent a stubborn model from
        # burning the entire step budget on coercion no-ops; if it
        # truly refuses, the swarm-level enforcement still
        # discards the payload as ungrounded.
        min_tool_calls_before_final=1,
    ),
}


SPECIALIST_BRIEFS: dict[str, str] = {
    "stock_analyst": (
        "Equity price action, OHLCV bars, technicals, options "
        "positioning, macro benchmarks, SEC filing metadata, Reddit / "
        "retail sentiment."
    ),
    "sector_analyst": (
        "SEC filing bodies (10-K / 10-Q / 8-K full text), quantitative "
        "financials, web news. For 8-K earnings whose first-pass search "
        "misses, widens the date window by ~30 days earlier on its own."
    ),
    "vc_analyst": (
        "Growth metrics, TAM, startup signals, long-tail web research, "
        "and the desk's standing competitive-context analyst (gets "
        "invoked for any stock / sector question to surface the "
        "competitive landscape). Cannot read SEC filing bodies. NOT "
        "for figure extraction: route specific reported-metric "
        "questions ('what was X's capex in Y') to news_quant_analyst "
        "instead -- vc_analyst's narrative-framed prompt makes it "
        "prone to hallucinate figures from training memory."
    ),
    "risk_analyst": (
        "Tail risk via the GDELT event stream, regulatory / macro "
        "correlation, web news. Maps macro stress to the right series "
        "(federal_funds for liquidity, unemployment + treasury_10y "
        "for recession risk, cpi / inflation for inflation shock, "
        "brent for commodity stress). Cannot read SEC filings or graph "
        "metadata."
    ),
    "news_quant_analyst": (
        "NARROW figure-extractor: pulls specific reported numerical "
        "figures (capex, revenue, EPS, headcount, deal value, KPI) "
        "for a named company + period from the scraped-articles + "
        "GDELT corpora. Quotes the dollar amount verbatim with "
        "source URL and date. Use IN PARALLEL with sector_analyst on "
        "reported-metric questions where the figure may live in news / "
        "transcripts rather than the SEC body (e.g. firms that don't "
        "publish an explicit annual capex line in their 8-K but whose "
        "figure is widely covered in analyst notes). MUST issue at "
        "least one search call before answering -- the runtime "
        "enforces this. Cannot read SEC filings, do narrative "
        "analysis, or do competitive context (vc_analyst handles "
        "the narrative side)."
    ),
}
"""One-line summary of each specialist's domain + tool surface.

Rendered into the orchestrator system prompt's ``{roster_brief}``
block by :mod:`reef.synthesizer (legacy, removed)`. Kept here -- next to the
:data:`SPECIALIST_CONFIGS` they describe -- so adding a specialist is
a single file edit instead of a cross-module dance.
"""


INVESTMENT_ANALYST_ROSTER: tuple[str, ...] = (
    "stock_analyst",
    "sector_analyst",
    "vc_analyst",
    "risk_analyst",
    "news_quant_analyst",
)


def get_specialist(key: str) -> SpecialistConfig:
    """Look up a specialist config; raises :class:`KeyError` on miss."""
    return SPECIALIST_CONFIGS[key]


def specialists_for(roster: Sequence[str]) -> tuple[SpecialistConfig, ...]:
    """Resolve a list of specialist keys into their configs."""
    return tuple(get_specialist(k) for k in roster)


__all__ = [
    "INVESTMENT_ANALYST_ROSTER",
    "SPECIALIST_BRIEFS",
    "SPECIALIST_CONFIGS",
    "SpecialistConfig",
    "augment_sector_instruction",
    "get_specialist",
    "specialists_for",
]
