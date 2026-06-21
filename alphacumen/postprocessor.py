# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``alphacumen.postprocessor`` -- the synthesis half of the split GP.

Once the planner converges, this runs **once**: it reads the full common
thread and writes the structured ``final_answer`` report.  Its prompt
(``postprocessor.md``) carries the production synthesizer's convergence
+ synthesis-quality rules verbatim, plus a hand-written final-answer
schema header.  There is no orchestration here and no skill loading.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Mapping, Optional, Sequence

from reef.react import format_common_thread, json_llm_call
from alphacumen.roster import _apply_tokens, _date_tokens, _resolve_today

logger = logging.getLogger(__name__)

# Matches "Coverage Gap Note:" / "Data Gap Note:" / "Coverage Note:"-style
# blocks the model sometimes emits once per specialist when retrieval
# returns nothing useful. When ~all specialists hit the same empty index
# window, the merged answer ends up with several near-identical blocks
# whose content rounds to "freshest evidence is dated <month>". Collapse
# down to one canonical block per (date-anchor, retrieval-source) pair.
_COVERAGE_GAP_BLOCK_RE = re.compile(
    r"(?im)^(?:\s*[\-\*•]?\s*)?(?:Coverage|Data|Index|Retrieval)\s+Gap\s+Note\s*:\s*[^\n]+(?:\n(?!\s*$).*)*",
)

_POSTPROC_LLM_TIMEOUT_S = 180.0
_POSTPROC_JSON_PARSE_RETRIES = 3

_SYSTEM_CACHE: dict[bool, tuple[str, str]] = {}


def _postprocessor_system_prompt(*, asof: Optional[str] = None) -> str:
    from alphacumen.planner import _variant_postprocessor

    today, is_backtest = _resolve_today(asof)
    cached = _SYSTEM_CACHE.get(is_backtest)
    if cached is not None and cached[0] == today:
        return cached[1]

    template = (
        resources.files("alphacumen.prompts")
        .joinpath("postprocessor.md")
        .read_text(encoding="utf-8")
    )
    # Slot pass first: backtest slot values embed ``{today}``, which
    # the date-token pass below renders against the resolved asof.
    rendered = _variant_postprocessor.apply(template, is_backtest=is_backtest)
    rendered = _apply_tokens(rendered, _date_tokens(asof))
    _SYSTEM_CACHE[is_backtest] = (today, rendered)
    return rendered


@dataclass
class PostProcessResult:
    """Outcome of the terminal synthesis call."""

    final_answer: dict[str, Any] = field(
        default_factory=lambda: {
            "answerable": False,
            "answer_summary": "",
            "confidence": "low",
        }
    )
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


def _sentinel(summary: str, *, raw: str, usage: dict[str, int],
             latency_ms: int, attempts: int, error: str) -> PostProcessResult:
    return PostProcessResult(
        final_answer={
            "answerable": False,
            "answer_summary": summary,
            "confidence": "low",
        },
        raw_assistant_text=raw,
        token_usage=usage,
        latency_ms=latency_ms,
        attempts=attempts,
        error=error,
    )


def postprocess(
    *,
    model: str,
    common_thread: Sequence[Mapping[str, Any]],
    temperature: float = 0.2,
    max_tokens: int = 6_144,
    asof: Optional[str] = None,
    system_prompt_override: Optional[str] = None,
) -> PostProcessResult:
    """Run the terminal synthesis call.  Always returns -- never raises."""
    system_prompt = (
        system_prompt_override
        if system_prompt_override is not None
        else _postprocessor_system_prompt(asof=asof)
    )
    formatted = format_common_thread(common_thread)
    user_message = (
        f"=== COMMON THREAD ===\n{formatted}\n===\n\n"
        "Synthesize the findings above into the final report. Reply with one "
        "JSON object matching the final_answer schema."
    )

    result = json_llm_call(
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_s=_POSTPROC_LLM_TIMEOUT_S,
        max_retries=_POSTPROC_JSON_PARSE_RETRIES,
        log_label="postprocessor",
    )

    if result.error:
        summary = (
            f"Synthesis LLM call failed after retries: {result.error}"[:500]
            if result.error == "llm_chat_error"
            else f"Synthesis failed to produce structured JSON after "
                 f"{_POSTPROC_JSON_PARSE_RETRIES} attempts."
        )
        return _sentinel(
            summary,
            raw=result.raw,
            usage=result.token_usage,
            latency_ms=result.latency_ms,
            attempts=result.attempts,
            error=result.error,
        )

    parsed = result.parsed
    assert parsed is not None  # error=None guarantees parsed is set

    final_answer = parsed
    if (
        isinstance(parsed.get("final_answer"), Mapping)
        and "answer_summary" not in parsed
    ):
        final_answer = dict(parsed["final_answer"])

    # Collapse duplicate coverage-gap notes: multiple specialists hitting
    # the same retrieval shortcoming (stale scraped index + sparse GDELT
    # entity coverage in our Tesla repro) each emit their own block, and
    # the synthesizer concatenates them without dedup. Keep only the
    # first occurrence so the user-facing answer shows one note instead
    # of three.
    final_answer = dict(final_answer)
    summary = final_answer.get("answer_summary")
    if isinstance(summary, str):
        final_answer["answer_summary"] = _dedupe_coverage_gap_blocks(summary)

    return PostProcessResult(
        final_answer=final_answer,
        raw_assistant_text=result.raw,
        token_usage=result.token_usage,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
    )


def _dedupe_coverage_gap_blocks(text: str) -> str:
    """Remove second-and-later ``Coverage Gap Note:``-style paragraphs.

    Conservative: only drops blocks whose normalized content (lower-case,
    whitespace-collapsed, punctuation-stripped) is *substantially*
    similar to a block we've already kept. Different gap notes that
    cite different sources / dates / corpora are preserved.
    """
    matches = list(_COVERAGE_GAP_BLOCK_RE.finditer(text))
    if len(matches) < 2:
        return text

    # "Frame" tokens that anchor a coverage-gap claim. Two blocks are
    # treated as the same claim when they share ≥ 2 frame tokens AND
    # raw token Jaccard ≥ 0.25 (loose, because the model paraphrases
    # the same fact with different citations / wording across
    # specialists). Empirically this collapses the Tesla repro pair
    # (35% Jaccard) without merging genuinely distinct gap notes that
    # cite different missing sources.
    _FRAME_TOKENS = {
        "coverage", "gap", "note", "freshest", "retrieved", "evidence",
        "dated", "stale", "missing", "corpus", "surfaced", "tool",
        "returned", "absent", "search", "index",
    }

    def _normalize(s: str) -> str:
        s = re.sub(r"\s+", " ", s.lower())
        s = re.sub(r"[^a-z0-9 ]", "", s)
        return s.strip()

    kept_norms: list[str] = []
    drop_spans: list[tuple[int, int]] = []
    for m in matches:
        norm = _normalize(m.group(0))
        tokens = set(norm.split())
        if not tokens:
            continue
        is_dup = False
        for existing in kept_norms:
            other = set(existing.split())
            if not other:
                continue
            shared_frame = len(tokens & other & _FRAME_TOKENS)
            jaccard = len(tokens & other) / len(tokens | other)
            if shared_frame >= 2 and jaccard >= 0.25:
                is_dup = True
                break
        if is_dup:
            drop_spans.append((m.start(), m.end()))
        else:
            kept_norms.append(norm)

    if not drop_spans:
        return text

    # Splice in reverse so earlier indices stay valid.
    out = text
    for start, end in reversed(drop_spans):
        # Eat one trailing blank line so we don't leave a double break.
        end_extended = end
        while end_extended < len(out) and out[end_extended] == "\n":
            end_extended += 1
            if end_extended - end >= 2:
                break
        out = out[:start] + out[end_extended:]
    return out.rstrip() + ("\n" if text.endswith("\n") else "")


__all__ = ["PostProcessResult", "postprocess"]
