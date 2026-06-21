# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``extract_accounting_estimate_change_disclosure`` skill impl.

Walks a specific 10-Q / 10-K's Notes-to-Financial-Statements text for
the verbatim management-rationale passage announcing a change in
accounting estimate. Generic across any US-listed issuer; the
rationale lives in the Notes, not in MD&A "Critical Accounting
Estimates" boilerplate.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _fetch_filing_html_from_edgar,
    _find_filing_ref_for_asof,
    _sec_api_filing_url_from_accession,
    _xbrl_extract_accession,
)


# Phrases that typically introduce or close a rationale for an
# accounting-estimate change. The walker looks for these in the
# 800-char window around the change-topic anchor to capture the
# rationale-bearing sentence even when it doesn't sit immediately
# adjacent to the topic noun phrase.
_RATIONALE_ANCHOR_PHRASES: tuple[str, ...] = (
    "due to",
    "based on",
    "result of",
    "reflecting",
    "reflects",
    "attributed to",
    "driven by",
    "as a result of",
    "consistent with",
    "in light of",
    "consistent with our experience",
    "given",
    "to align",
    "to better reflect",
    "to reflect",
    "stemming from",
    "owing to",
    "longer useful lives are",
    "shorter useful lives are",
    "this change in estimate",
    "we determined",
    "we revised",
    "we completed",
    "completed our most recent",
    "after completing",
    "after our review",
    "we have revised",
)


# Standard Note 1 / Significant Accounting Policies anchors. The
# walker prefers a change-topic match inside one of these named
# sections over a match elsewhere in the filing.
_NOTE_SECTION_ANCHORS: tuple[str, ...] = (
    "Summary of Significant Accounting Policies",
    "Significant Accounting Policies",
    "Description of Business and Accounting Policies",
    "Accounting Policies",
    "Recent Accounting",
    "Use of Estimates",
    "Property and Equipment",
    "Change in Accounting Estimate",
    "Changes in Accounting Estimates",
    "Critical Accounting Estimates",
)


def _strip_html_to_text(html: str) -> str:
    """HTML-to-text helper mirroring extract_competition_section."""
    if not html:
        return ""
    no_scripts = re.sub(
        r"<(?:script|style)[^>]*>.*?</(?:script|style)>",
        " ", html, flags=re.I | re.DOTALL,
    )
    no_tags = re.sub(r"<[^>]+>", " ", no_scripts)
    no_entities = (
        no_tags.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#160;", " ")
        .replace("&#8217;", "'")
        .replace("&#8220;", '"')
        .replace("&#8221;", '"')
        .replace("&#8211;", "-")
        .replace("&#8212;", "--")
        .replace("&#36;", "$")
    )
    return re.sub(r"\s+", " ", no_entities).strip()


def _fetch_filing_text(ref: str) -> str:
    """Pull the full filing text from EDGAR + strip to plain text."""
    if not ref:
        return ""
    accession = _xbrl_extract_accession(ref)
    if not accession:
        parts = ref.split(":")
        if len(parts) >= 2 and parts[0] == "sec":
            accession = parts[1]
    if not accession:
        return ""
    primary_url, err = _sec_api_filing_url_from_accession(accession)
    if not primary_url or err:
        return ""
    html, err = _fetch_filing_html_from_edgar(primary_url)
    if not html or err:
        return ""
    return _strip_html_to_text(html)


def _find_change_topic_passages(text: str, change_topic: str) -> list[tuple[int, str]]:
    """Return up to 6 candidate passages where the change_topic appears.

    Each passage is a 600-char window centered on the topic occurrence
    in the filing text. The window is wide enough to capture the
    rationale sentence + the change announcement sentence even when
    they're separated by a metric-quantification sentence.
    """
    if not text or not change_topic:
        return []
    topic = change_topic.strip()
    if not topic:
        return []
    out: list[tuple[int, str]] = []
    # Allow whitespace-collapsed matches and case-insensitive search.
    safe_topic = re.escape(topic).replace(r"\ ", r"\s+")
    pat = re.compile(r"(?i)" + safe_topic)
    for m in pat.finditer(text):
        start = max(0, m.start() - 300)
        end = min(len(text), m.end() + 300)
        out.append((m.start(), text[start:end]))
        if len(out) >= 6:
            break
    return out


def _score_passage(passage: str) -> int:
    """Score a candidate passage by the count of rationale-anchor matches.

    Higher score = more likely to contain the verbatim management
    rationale the rubric grades against. Boosts when the passage also
    overlaps with a Notes-section anchor (Significant Accounting
    Policies, etc.).
    """
    if not passage:
        return 0
    p = passage.lower()
    score = sum(1 for a in _RATIONALE_ANCHOR_PHRASES if a in p)
    for section in _NOTE_SECTION_ANCHORS:
        if section.lower() in p:
            score += 2
    # Penalise passages that look like Item 7 MD&A boilerplate. The
    # "Critical Accounting Estimates" header itself is fine; the
    # generic "actual results could differ materially" line is not.
    if "actual results could differ materially" in p:
        score -= 2
    if "estimates and assumptions" in p and "policies" not in p:
        score -= 1
    return score


def _best_rationale_passage(text: str, change_topic: str) -> Optional[str]:
    """Pick the highest-scoring passage containing the change_topic."""
    candidates = _find_change_topic_passages(text, change_topic)
    if not candidates:
        return None
    scored = [(p, _score_passage(p)) for _, p in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    top_passage, top_score = scored[0]
    if top_score <= 0:
        # No rationale anchors at all — return the first occurrence so
        # the caller still sees the issuer's wording, but mark it as
        # low-confidence.
        return candidates[0][1]
    return top_passage


@skill_fn(
    skill_id="extract_accounting_estimate_change_disclosure",
    description=(
        "Pull the verbatim Notes-to-Financial-Statements passage "
        "from a specific 10-Q / 10-K where the issuer announces + "
        "justifies a change in accounting estimate (useful-life "
        "revision, amortization-period change, allowance "
        "methodology shift, segment reclassification, etc.). The "
        "rationale lives in the Notes, NOT in Item 7 MD&A "
        "boilerplate; rubric atoms grading a verbatim management "
        "rationale ('the change is attributed to / due to / "
        "reflects ...') fail when the model paraphrases ('management "
        "believed conditions had improved'). This walker bypasses "
        "BM25 keyword ranking and reads the filing's full Notes "
        "text directly via EDGAR. USE for any rubric atom that "
        "grades verbatim management-disclosed rationale for an "
        "estimate change. Generic across any US-listed issuer."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "period_end_iso": {
                "type": "string",
                "description": (
                    "ISO-8601 date of the quarter-end (or fiscal-"
                    "year-end) when the change was first disclosed. "
                    "The walker resolves this to the matching "
                    "10-Q or 10-K filing via the standard asof-"
                    "filing resolver."
                ),
            },
            "fiscal_year": {
                "type": "integer",
                "description": (
                    "Fiscal year of the filing. Used together with "
                    "period_end_iso to disambiguate when an issuer "
                    "files multiple periods around the same date."
                ),
            },
            "change_topic": {
                "type": "string",
                "description": (
                    "Short noun phrase identifying the estimate "
                    "that changed (e.g. 'useful life', 'discount "
                    "rate', 'allowance methodology', 'amortization "
                    "period for developed technology'). The walker "
                    "anchors on this phrase to find the rationale "
                    "passage."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "period_end_iso", "fiscal_year", "change_topic"],
    },
)
def extract_accounting_estimate_change_disclosure(
    ticker: str,
    period_end_iso: str,
    fiscal_year: int,
    change_topic: str,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Verbatim rationale passage for an accounting-estimate change."""
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    if not period_end_iso or not isinstance(period_end_iso, str):
        return {"error": "period_end_iso required (ISO-8601)"}
    if not change_topic or not isinstance(change_topic, str):
        return {"error": "change_topic required"}
    try:
        fy = int(fiscal_year)
    except (TypeError, ValueError):
        return {"error": "fiscal_year must be int"}

    t = ticker.strip().upper()
    ref, filed = _find_filing_ref_for_asof(t, fy, period_end_iso)
    if not ref:
        return {
            "ticker": t,
            "period_end_iso": period_end_iso,
            "fiscal_year": fy,
            "change_topic": change_topic,
            "error": (
                f"no filing found for {t} fy={fy} period_end="
                f"{period_end_iso}"
            ),
        }

    filing_text = _fetch_filing_text(ref)
    if not filing_text:
        return {
            "ticker": t,
            "period_end_iso": period_end_iso,
            "fiscal_year": fy,
            "ref": ref,
            "filed_at": filed,
            "change_topic": change_topic,
            "error": "filing text fetch / strip failed",
        }

    passage = _best_rationale_passage(filing_text, change_topic)
    if not passage:
        return {
            "ticker": t,
            "period_end_iso": period_end_iso,
            "fiscal_year": fy,
            "ref": ref,
            "filed_at": filed,
            "filing_chars": len(filing_text),
            "change_topic": change_topic,
            "error": (
                f"change_topic {change_topic!r} not found in filing "
                f"text ({len(filing_text):,} chars). Topic phrasing "
                f"may differ from the filing's wording."
            ),
        }

    lines = [
        f"# {t} accounting-estimate change — {change_topic}",
        "",
        f"Filing ref: `{ref}` (filed {filed or 'unknown'})",
        f"Period end: {period_end_iso}, FY{fy}.",
        "",
        "## Verbatim disclosure (Notes to Financial Statements)",
        "",
        "> " + passage.replace("\n", "\n> "),
        "",
        "## How to use this passage",
        "",
        "Quote the verbatim passage above in the final answer's "
        "section discussing the estimate change. The issuer's "
        "specific rationale phrasing is what the rubric grades "
        "against — paraphrasing into a summary drops the verbatim "
        "language and fails the atom. Include the source filing "
        "ref so the answer's provenance is clear.",
    ]
    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "period_end_iso": period_end_iso,
        "fiscal_year": fy,
        "change_topic": change_topic,
        "ref": ref,
        "filed_at": filed,
        "passage": passage,
        "passage_chars": len(passage),
        "answer_summary_block": answer_summary_block,
    })
