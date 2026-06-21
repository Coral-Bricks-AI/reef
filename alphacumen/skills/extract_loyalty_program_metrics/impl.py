# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``extract_loyalty_program_metrics`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the sector_analyst
dispatch via ``invoke_skill_fn``. The recipe's prose playbook lives
next to this file in ``SKILL.md``.

Walks the issuer's 10-K body for loyalty-program member-share
disclosure (X% of room nights / check-ins / stays attributable to
members). EDGAR direct-fetch ensures we see the full filing prose
(the chunked index splits Item 1 / MD&A across boundaries, and
BM25 routinely returns retrieval-noise hits rather than the
canonical penetration paragraph).

Generic across US-listed hospitality issuers; no ticker-specific or
rubric-keyed phrasing baked in.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _fetch_filing_html_from_edgar,
    _find_10k_ref_for_fy,
    _sec_api_filing_url_from_accession,
)


def _strip_html_to_text(html: str) -> str:
    """Minimal HTML-to-text stripper for prose extraction."""
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
        .replace("&#8217;", "'")
        .replace("&#8211;", "-")
        .replace("&#8212;", "--")
        .replace("&#8220;", '"')
        .replace("&#8221;", '"')
        .replace("&#36;", "$")
    )
    return re.sub(r"\s+", " ", no_entities).strip()


def _fetch_10k_text(ref: str) -> str:
    """Pull full 10-K body text via EDGAR direct-fetch."""
    if not ref:
        return ""
    accession = ""
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


# Loyalty-membership context tokens. Anchor the disclosure
# paragraph on any of these — they identify the loyalty-program
# context so we don't pick up unrelated room-night-percentage
# mentions. Two flavours:
#
# 1. Brand names (Bonvoy, Wyndham Rewards, World of Hyatt, IHG One
#    Rewards, Hilton Honors, Choice Privileges, Best Western
#    Rewards). The disclosure paragraph almost always names the
#    program; a window centered on the brand name catches the
#    paragraph regardless of how the issuer phrases the member-
#    share figure.
# 2. Loyalty-program / loyalty-members fallbacks for issuers that
#    don't use a distinct brand name in the disclosure paragraph.
_LOYALTY_CONTEXT_TOKENS = (
    # Brand-name anchors (preferred -- specific to the loyalty
    # program). Includes both the proper-cased brand and the
    # generic Rewards / Honors / Privileges suffix variants.
    "Marriott Bonvoy",
    "Bonvoy",
    "Wyndham Rewards",
    "World of Hyatt",
    "IHG One Rewards",
    "IHG Rewards Club",
    "IHG Rewards",
    "Hilton Honors",
    "Choice Privileges",
    "Best Western Rewards",
    "Radisson Rewards",
    "Hyatt Privé",
    "MGM Rewards",
    "Caesars Rewards",
    # Generic-noun fallbacks. Looser, used when the issuer doesn't
    # name a distinct brand in the disclosure paragraph (or the
    # brand appears in a footnote section). Lowercase variants
    # cover MD&A prose; capitalised variants cover headers /
    # section titles.
    "Loyalty Program",
    "loyalty program",
    "loyalty members",
    "rewards members",
    "Rewards members",
    "rewards member",
    "Rewards member",
    "loyalty member",
    "Loyalty member",
    # Guest-loyalty-program phrase the SEC standard 10-K layout
    # uses for many hospitality issuers' Item 1 subsection
    # heading.
    "guest loyalty program",
    "Guest Loyalty Program",
)


# Percentage pattern. Captures the integer % AND the optional
# modifier ("approximately", "over", "nearly", "around"). Skipping
# decimal percentages for now -- hospitality issuers consistently
# report integer values for member-share. Including decimal would
# require a tighter context filter to avoid picking up adjacent
# margin/RevPAR ratios that share the percentage shape.
_PCT_RE = re.compile(
    r"(?:(?P<modifier>approximately|over|nearly|around|about)\s+)?"
    # The digit is bounded on the LEFT by \b (so we don't match
    # the trailing two digits of a 3+ digit number like "122
    # million"). On the RIGHT, "%" needs no boundary -- punctuation
    # bounds it naturally -- while "percent" gets a \b because the
    # word can be followed by space, comma, or period.
    r"\b(?P<pct>\d{1,2})\s*(?:%|percent\b)",
    re.I,
)


# Metric keywords -- the thing the percentage measures. Room nights
# is the dominant hospitality metric; check-ins, stays, occupied
# rooms, bookings, and room revenue are alternative wordings used
# by different issuers.
_METRIC_KEYWORDS = (
    "hotel room nights",
    "room nights",
    "check-ins",
    "check ins",
    "checkins",
    "stays",
    "occupied rooms",
    "bookings",
    "room revenue",
    "system-wide rooms",
)


# Geography keywords. Captures the regional scope of the percentage.
_GEO_KEYWORDS = (
    ("U.S.", "U.S."),
    ("United States", "United States"),
    ("U.S", "U.S."),
    ("US ", "U.S."),
    ("North America", "North America"),
    ("Canada", "Canada"),
    ("domestic", "U.S."),
    ("global", "global"),
    ("globally", "global"),
    ("worldwide", "global"),
    ("internationally", "international"),
    ("international", "international"),
    ("system-wide", "system-wide"),
)


def _find_geography_label(window: str) -> str:
    """Return canonical geography label from a text window."""
    win_lc = window.lower()
    for raw, canonical in _GEO_KEYWORDS:
        if raw.lower() in win_lc:
            return canonical
    return ""


def _find_metric_label(window: str) -> str:
    """Return canonical metric label from a text window."""
    win_lc = window.lower()
    for kw in _METRIC_KEYWORDS:
        if kw in win_lc:
            return kw
    return ""


def _extract_loyalty_metrics(text: str) -> list[dict[str, Any]]:
    """Find all loyalty-member-share percentage disclosures.

    Returns one entry per (percentage, geography, metric) triple,
    each with the verbatim source sentence. Walks every
    loyalty-context match; for each, scans ±200 chars for a
    percentage; for each percentage, identifies the nearest metric +
    geography label. Deduplicates by (pct, geo, metric) so the same
    figure isn't double-emitted when the disclosure appears
    multiple times in the filing.
    """
    if not text:
        return []

    # Find every loyalty-context match with surrounding window
    # (±400 chars on each side). Percentages of loyalty-program
    # member share routinely fall WITHIN this window of the
    # context anchor.
    candidate_windows: list[tuple[int, str]] = []
    seen_starts: set[int] = set()
    for token in _LOYALTY_CONTEXT_TOKENS:
        for m in re.finditer(re.escape(token), text, re.I):
            anchor = m.start()
            # De-dupe overlapping anchors -- if two tokens hit
            # within 50 chars, treat as one window.
            if any(abs(anchor - s) < 50 for s in seen_starts):
                continue
            seen_starts.add(anchor)
            win_start = max(0, anchor - 400)
            win_end = min(len(text), anchor + 400)
            candidate_windows.append((anchor, text[win_start:win_end]))

    # Growth-context tokens we use to reject percentages that
    # describe growth / year-over-year change rather than member
    # share. "grew X%", "X% growth", "X% in 2025", "X% since
    # 2022" are common false positives in the loyalty-disclosure
    # paragraph and adjacent prose.
    _GROWTH_TOKENS_BEFORE = ("grew ", "growth ", "increased ", "rose ", "up ", "decline ", "decreased ")
    _GROWTH_TOKENS_AFTER = (" in 20", " since ", " growth", " yoy", " year-over-year")

    triples: dict[tuple[int, str, str], dict[str, Any]] = {}
    for anchor, window in candidate_windows:
        for pm in _PCT_RE.finditer(window):
            try:
                pct = int(pm.group("pct"))
            except (TypeError, ValueError):
                continue
            # Filter improbable values. Hospitality loyalty member
            # shares range ~10-95%. Outside this band the
            # percentage almost certainly refers to something else
            # (effective tax rate, growth rate, comp ratio).
            if pct < 10 or pct > 95:
                continue
            # Reject growth-context percentages ("grew X%",
            # "X% growth", "X% in 2025", "X% since 2022").
            before_15 = window[max(0, pm.start() - 25):pm.start()].lower()
            after_15 = window[pm.end():min(len(window), pm.end() + 25)].lower()
            if any(t in before_15 for t in _GROWTH_TOKENS_BEFORE):
                continue
            if any(t in after_15 for t in _GROWTH_TOKENS_AFTER):
                continue
            modifier = (pm.group("modifier") or "").lower()
            # Scope geo + metric search to ±150 chars around the
            # percentage. Looking BEFORE the percentage catches the
            # shared-sentence metric -- e.g. "of <metric> at our
            # hotels <geo-A> and over <pct>% in the <geo-B>" has
            # the metric word before the second percentage but the
            # next-occurrence-of-metric only further downstream.
            # Backward + forward window lets the second percentage
            # inherit the metric from the first.
            local_start = max(0, pm.start() - 150)
            local_end = min(len(window), pm.end() + 150)
            local_window = window[local_start:local_end]
            metric = _find_metric_label(local_window)
            # For geo: scope to the AFTER-window (the geo of a
            # percentage is the scope clause that follows it --
            # "<pct>% of <metric> at our <geo> hotels"), BOUNDED at
            # the next percentage marker. Without this bound, a
            # sentence with two percentages ("<X>% of <metric>
            # <geo-A> and <Y>% in the <geo-B>") assigns the geo-B
            # label to BOTH percentages because the second
            # percentage's geo appears in the first's forward
            # window.
            after_window_raw = window[pm.end():local_end]
            next_pct_in_after = re.search(
                r"\d{1,2}\s*%", after_window_raw,
            )
            if next_pct_in_after:
                after_window = after_window_raw[:next_pct_in_after.start()]
            else:
                after_window = after_window_raw
            geo = _find_geography_label(after_window)
            if not metric:
                continue
            # Extract verbatim source sentence around the
            # percentage (the half-sentence preceding + the
            # percentage's clause). Bound by sentence-end
            # punctuation OR ±250 chars.
            sent_start = max(0, pm.start() - 250)
            sent_end = min(len(window), pm.end() + 250)
            # Snap sentence-start to the most recent period+space
            # before the percentage.
            preceding = window[sent_start:pm.start()]
            last_period = max(
                preceding.rfind(". "),
                preceding.rfind(".\n"),
            )
            if last_period >= 0:
                sent_start = sent_start + last_period + 2
            # Snap sentence-end to the next period+space after the
            # percentage.
            following = window[pm.end():sent_end]
            next_period = following.find(". ")
            if next_period >= 0:
                sent_end = pm.end() + next_period + 1
            source_sentence = window[sent_start:sent_end].strip()
            # Dedupe by (pct, geo). A single (pct, geo) combo
            # should map to ONE metric -- issuers don't disclose
            # the same figure twice for different metrics. Without
            # this dedupe, a sentence scanned from multiple
            # candidate windows can emit (pct, geo, metric-A) and
            # (pct, geo, metric-B) for the same underlying
            # disclosure.
            key = (pct, geo)
            if key in triples:
                continue
            triples[key] = {
                "percentage": pct,
                "modifier": modifier or None,
                "geography": geo or "(unspecified)",
                "metric": metric,
                "source_sentence": source_sentence,
            }
    return list(triples.values())


@skill_fn(
    skill_id="extract_loyalty_program_metrics",
    description=(
        "Pull the issuer's loyalty-program member-share metrics "
        "(US / global / regional percentages of room nights / "
        "check-ins / stays attributable to members) from the latest "
        "FY 10-K body. Returns each percentage with its geography "
        "label + metric label + verbatim source sentence. USE for "
        "any 'what percentage of room nights / check-ins come from "
        "loyalty members' question, multi-issuer hospitality-"
        "loyalty comparisons (any pair of US-listed hotel issuers), "
        "or 'which company is better positioned to translate loyalty "
        "strength into durable economic advantage' questions. Call "
        "ONCE PER TICKER. Generic across US-listed hospitality "
        "issuers; no ticker-specific values baked in."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "fy": {
                "type": "integer",
                "description": (
                    "Fiscal year of the 10-K to pull from. Use the "
                    "latest reported FY whose annual report is "
                    "filed at the question's asof."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy"],
    },
)
def extract_loyalty_program_metrics(
    ticker: str,
    fy: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Extract loyalty-program member-share percentages from 10-K
    prose.
    """
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": f"fy must be int (got {fy!r})"}

    t = ticker.strip().upper()
    ref, filed = _find_10k_ref_for_fy(t, fy_int)
    if not ref:
        return {"error": f"no {t} 10-K found for FY{fy_int}"}

    text = _fetch_10k_text(ref)
    if not text:
        return {
            "ticker": t,
            "fy": fy_int,
            "ref": ref,
            "filed_at": filed,
            "error": (
                f"could not retrieve {t} FY{fy_int} 10-K text "
                f"(ref={ref})"
            ),
        }

    metrics = _extract_loyalty_metrics(text)
    if not metrics:
        return {
            "ticker": t,
            "fy": fy_int,
            "ref": ref,
            "filed_at": filed,
            "metrics": [],
            "filing_text_chars": len(text),
            "error": (
                f"No loyalty-program member-share percentage "
                f"disclosure matched the standard hospitality "
                f"pattern in {t} FY{fy_int} 10-K. The issuer may "
                f"not disclose member share at all, or may use an "
                f"atypical phrasing not covered by the loyalty-"
                f"context tokens. Fall back to `get_full_text("
                f"ref={ref!r})` and scan for loyalty / rewards / "
                f"Bonvoy / Hyatt / IHG One language directly."
            ),
        }

    # Sort: U.S. / North America first (rubric convention), then
    # global, then others.
    geo_order = {
        "U.S.": 0, "North America": 1, "Canada": 2,
        "global": 3, "system-wide": 4, "international": 5,
    }
    metrics.sort(
        key=lambda r: (geo_order.get(r["geography"], 9), -r["percentage"]),
    )

    lines = [
        f"# {t} loyalty-program member-share metrics (FY{fy_int})",
        "",
        f"10-K ref: `{ref}` (filed {filed or 'unknown'})",
        "",
        "| Geography | Metric | Percentage | Modifier |",
        "|---|---|---|---|",
    ]
    for r in metrics:
        mod = r["modifier"] or "—"
        lines.append(
            f"| {r['geography']} | {r['metric']} | "
            f"**{r['percentage']}%** | {mod} |"
        )
    lines.append("")
    lines.append("## Verbatim source sentences")
    lines.append("")
    for r in metrics:
        mod_text = f"{r['modifier']} " if r['modifier'] else ""
        lines.append(
            f"- **{mod_text}{r['percentage']}%** "
            f"({r['geography']} {r['metric']}): "
            f"\"{r['source_sentence']}\""
        )
    lines.append("")
    lines.append(
        "## How to use this output"
    )
    lines.append("")
    lines.append(
        "Quote the table above + the verbatim source sentence for "
        "each metric in the per-issuer section of the answer. For "
        "multi-issuer comparisons, the rubric grades both the "
        "individual percentage figures AND the comparative "
        "conclusion (which issuer has stronger loyalty penetration). "
        "Stating both issuers' US + global percentages side-by-side "
        "is the canonical disclosure pattern."
    )

    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy": fy_int,
        "ref": ref,
        "filed_at": filed,
        "metrics": metrics,
        "metrics_count": len(metrics),
        "answer_summary_block": answer_summary_block,
    })
