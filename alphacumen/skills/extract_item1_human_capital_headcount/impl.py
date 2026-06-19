# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``extract_item1_human_capital_headcount`` skill impl.

One @skill_fn-registered callable. The recipe's prose playbook lives
next to this file in SKILL.md.

Walks each fiscal year's 10-K Item 1 "Human Capital" / "Employees"
subsection and reads the canonical FY-end headcount disclosure
("we employed approximately X employees as of <date>"). Returns
per-FY headcount + YoY %. Used for rubric atoms that grade a multi-FY
% workforce change at issuers that don't tag dei:EntityNumberOfEmployees.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Optional

from harness.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _fetch_filing_html_from_edgar,
    _fetch_filing_section_html,
    _filing_url_for_accession,
    _find_10k_ref_for_fy,
    _sec_api_filing_url_from_accession,
    _xbrl_extract_accession,
)


# Subsection headers commonly following Human Capital / Employees in
# Item 1 Business. Used as the upper-bound terminator when slicing
# the prose region for regex search.
_NEXT_SUBSECTION_HEADERS: tuple[str, ...] = (
    "Properties",
    "Item 1A",
    "Risk Factors",
    "Government Regulation",
    "Available Information",
    "Cybersecurity",
    "Environmental",
    "Intellectual Property",
)


# Header anchors that start the Human Capital / Employees subsection
# in Item 1 Business. Order matters only as a search list; we take
# the EARLIEST match.
_SUBSECTION_HEADERS: tuple[str, ...] = (
    "Human Capital Management",
    "Human Capital Resources",
    "Human Capital",
    "Our People",
    "Our Employees",
    "Employees",  # older 10-K convention
    "Workforce",
)


# Regex patterns that capture a headcount integer near an
# "employees" / "team members" / "workforce" noun. Each pattern
# captures (count) as group 1.
_HEADCOUNT_PATTERNS: tuple[re.Pattern, ...] = (
    # "we employed approximately X (full-time) (employees|people|team members)"
    re.compile(
        r"(?i)\b(?:we|our company|the company)\s+(?:had|employed)\s+"
        r"approximately\s+([\d,]+)\s+(?:full[-\s]time\s+)?"
        r"(?:employees|people|team\s+members|colleagues)\b",
    ),
    # "approximately X (full-time) employees" (no leading subject)
    re.compile(
        r"(?i)\bapproximately\s+([\d,]+)\s+(?:full[-\s]time\s+)?"
        r"(?:employees|people|team\s+members|colleagues)\b",
    ),
    # "X employees worldwide" / "X-person workforce"
    re.compile(
        r"(?i)\b([\d,]{4,})\s+(?:employees|people|team\s+members|colleagues)"
        r"\s+(?:worldwide|globally|in\s+total)",
    ),
    # "workforce of approximately X" / "global workforce of X"
    re.compile(
        r"(?i)\b(?:global\s+)?workforce\s+of\s+approximately\s+([\d,]+)\b",
    ),
)


# "as of <Month> <Day>, <Year>" near the headcount sentence. Used to
# verify the figure is an FY-end disclosure and to surface the
# anchor date in the answer.
_ASOF_DATE_RE = re.compile(
    r"(?i)\bas\s+of\s+("
    r"(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)"
    r"\s+\d{1,2},?\s+\d{4}"
    r")",
)


def _strip_html_to_text(html: str) -> str:
    """Minimal HTML-to-text stripper.

    Mirrors the helper used by extract_competition_section so both
    skills handle the same set of entity encodings consistently.
    """
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


def _fetch_item1_text(ref: str) -> str:
    """Pull Item 1 Business text via sec-api Extractor, EDGAR fallback.

    sec-api gives a clean item-level slice when it processes the
    filing; EDGAR direct-fetch returns the whole-document HTML and we
    strip it to text. Caching is handled by the underlying helpers.
    """
    if not ref:
        return ""
    accession = _xbrl_extract_accession(ref)
    if not accession:
        parts = ref.split(":")
        if len(parts) >= 2 and parts[0] == "sec":
            accession = parts[1]
    if not accession:
        return ""

    api_key = os.environ.get("SEC_API_KEY", "").strip()
    if api_key:
        filing_url, err = _filing_url_for_accession(accession, api_key)
        if filing_url and not err:
            html, err = _fetch_filing_section_html(filing_url, "1", api_key)
            if html and not err:
                text = _strip_html_to_text(html)
                if text and len(text) > 500:
                    return text

    # EDGAR direct-fetch fallback (whole-document HTML).
    primary_url, err = _sec_api_filing_url_from_accession(accession)
    if not primary_url or err:
        return ""
    html, err = _fetch_filing_html_from_edgar(primary_url)
    if not html or err:
        return ""
    return _strip_html_to_text(html)


def _slice_human_capital_subsection(item1_text: str) -> str:
    """Cut the Human Capital / Employees subsection out of Item 1.

    Anchors on the earliest matching subsection header, then runs
    until the next-subsection terminator or 8000 chars (whichever
    comes first). Returns the prose slice; empty string when no
    header matches.
    """
    if not item1_text:
        return ""
    earliest_start = -1
    for header in _SUBSECTION_HEADERS:
        m = re.search(
            r"\b" + re.escape(header) + r"\b",
            item1_text,
        )
        if m and (earliest_start < 0 or m.start() < earliest_start):
            earliest_start = m.start()
    if earliest_start < 0:
        return ""
    end = len(item1_text)
    for header in _NEXT_SUBSECTION_HEADERS:
        m = re.search(
            r"\b" + re.escape(header) + r"\b",
            item1_text[earliest_start + 1:],
        )
        if m and (m.start() + earliest_start + 1) < end:
            end = m.start() + earliest_start + 1
    if end - earliest_start > 8000:
        end = earliest_start + 8000
    return item1_text[earliest_start:end].strip()


def _extract_headcount_from_subsection(prose: str) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Find the canonical FY-end headcount sentence.

    Returns (headcount_int, sentence_quote, asof_date_str). All
    fields are None when no pattern matches. Sentence_quote is the
    verbatim ~250-char window around the captured number so the
    answer can show the filing's exact phrasing.
    """
    if not prose:
        return None, None, None
    best: tuple[int, str, Optional[str]] | None = None
    for pat in _HEADCOUNT_PATTERNS:
        for m in pat.finditer(prose):
            raw = m.group(1)
            try:
                n = int(raw.replace(",", ""))
            except ValueError:
                continue
            # Filter out unrelated small integers (e.g. "approximately
            # 30 patents"). FY-end workforce counts at US public
            # issuers are typically >= 500.
            if n < 500:
                continue
            # Capture a 250-char window around the match for the
            # answer's verbatim sentence quote.
            win_start = max(0, m.start() - 120)
            win_end = min(len(prose), m.end() + 130)
            window = prose[win_start:win_end].strip()
            # Date capture: search the same window for an "as of
            # <Month Day, Year>" anchor.
            asof = None
            dm = _ASOF_DATE_RE.search(window)
            if dm:
                asof = dm.group(1).strip()
            cand = (n, window, asof)
            # Prefer the candidate with a captured as-of date (it's
            # the canonical FY-end disclosure). Otherwise keep the
            # first match.
            if best is None:
                best = cand
            elif best[2] is None and asof is not None:
                best = cand
    if best is None:
        return None, None, None
    return best[0], best[1], best[2]


@skill_fn(
    skill_id="extract_item1_human_capital_headcount",
    description=(
        "Walk each fiscal year's 10-K Item 1 Human Capital / "
        "Employees subsection and read the canonical end-of-FY "
        "headcount disclosure (the \"we employed approximately X "
        "employees as of <date>\" sentence). Returns a per-FY series "
        "of (FY, headcount, sentence_quote, as_of_date) entries + "
        "per-FY YoY % deltas. USE for any year-over-year or "
        "multi-FY headcount / employee / workforce trend question "
        "at a US-listed issuer. Generic across any issuer with the "
        "standard Item 1 \"Human Capital\" / \"Employees\" layout. "
        "Particularly useful when the issuer doesn't tag "
        "dei:EntityNumberOfEmployees in XBRL (a meaningful share of "
        "large-caps don't), which is the common reason ad-hoc "
        "XBRL-driven retrieval comes back empty."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "fy_start": {
                "type": "integer",
                "description": (
                    "First fiscal year of the headcount series "
                    "(inclusive)."
                ),
            },
            "fy_end": {
                "type": "integer",
                "description": (
                    "Last fiscal year of the headcount series "
                    "(inclusive). For a single-YoY question, pass "
                    "fy_start = FY-prior, fy_end = FY-current."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy_start", "fy_end"],
    },
)
def extract_item1_human_capital_headcount(
    ticker: str,
    fy_start: int,
    fy_end: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Per-FY Item 1 headcount + YoY % over an inclusive fiscal-year range."""
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    try:
        fy_s = int(fy_start)
        fy_e = int(fy_end)
    except (TypeError, ValueError):
        return {"error": "fy_start and fy_end must be ints"}
    if fy_e < fy_s:
        return {"error": "fy_end must be >= fy_start"}

    t = ticker.strip().upper()

    series: list[dict[str, Any]] = []
    for fy in range(fy_s, fy_e + 1):
        ref, filed = _find_10k_ref_for_fy(t, fy)
        if not ref:
            series.append({
                "fy": fy,
                "error": f"no {t} 10-K found for FY{fy}",
            })
            continue
        item1_text = _fetch_item1_text(ref)
        if not item1_text:
            series.append({
                "fy": fy,
                "ref": ref,
                "filed_at": filed,
                "error": "Item 1 text fetch failed",
            })
            continue
        subsection = _slice_human_capital_subsection(item1_text)
        if not subsection:
            series.append({
                "fy": fy,
                "ref": ref,
                "filed_at": filed,
                "item1_chars": len(item1_text),
                "error": (
                    "Human Capital / Employees subsection anchor "
                    "not found in Item 1; issuer may use atypical "
                    "layout."
                ),
            })
            continue
        headcount, sentence_quote, asof = _extract_headcount_from_subsection(subsection)
        if headcount is None:
            series.append({
                "fy": fy,
                "ref": ref,
                "filed_at": filed,
                "subsection_chars": len(subsection),
                "error": (
                    "Headcount pattern did not match in the Human "
                    "Capital / Employees subsection; issuer may "
                    "phrase the workforce count atypically."
                ),
            })
            continue
        series.append({
            "fy": fy,
            "ref": ref,
            "filed_at": filed,
            "headcount": headcount,
            "as_of_date": asof,
            "sentence_quote": sentence_quote,
        })

    # YoY % deltas. Computed between consecutive series entries
    # where both have a numeric headcount.
    yoy: list[dict[str, Any]] = []
    for prev, cur in zip(series, series[1:]):
        if (
            isinstance(prev.get("headcount"), int)
            and isinstance(cur.get("headcount"), int)
            and prev["headcount"] > 0
        ):
            delta_pct = (cur["headcount"] - prev["headcount"]) / prev["headcount"] * 100.0
            yoy.append({
                "from_fy": prev["fy"],
                "to_fy": cur["fy"],
                "from_count": prev["headcount"],
                "to_count": cur["headcount"],
                "yoy_pct": round(delta_pct, 4),
            })

    # Answer summary block.
    lines = [
        f"# {t} headcount — FY{fy_s} to FY{fy_e}",
        "",
        f"Sourced from each FY's 10-K Item 1 Human Capital / "
        f"Employees subsection (verbatim prose).",
        "",
        "## Per-FY headcount",
        "",
        "| FY | Headcount | As of | Source |",
        "|---|---|---|---|",
    ]
    for entry in series:
        fy_n = entry.get("fy")
        if "error" in entry:
            lines.append(
                f"| FY{fy_n} | _n/a_ | _n/a_ | {entry['error'][:80]} |"
            )
            continue
        hc = entry.get("headcount")
        asof = entry.get("as_of_date") or "_not in prose_"
        ref = entry.get("ref", "")
        lines.append(
            f"| FY{fy_n} | {hc:,} | {asof} | `{ref}` |"
        )

    if yoy:
        lines.extend([
            "",
            "## Year-over-year % delta",
            "",
            "| From FY | To FY | From count | To count | YoY % |",
            "|---|---|---|---|---|",
        ])
        for d in yoy:
            sign = "+" if d["yoy_pct"] >= 0 else ""
            lines.append(
                f"| FY{d['from_fy']} | FY{d['to_fy']} | "
                f"{d['from_count']:,} | {d['to_count']:,} | "
                f"{sign}{d['yoy_pct']:.2f}% |"
            )

    # Verbatim filing sentences for atom-level grounding. Quoting
    # these in the final answer preserves the issuer's exact phrasing
    # for narrative atoms that grade against verbatim disclosure.
    verbatim_blocks = [
        entry for entry in series
        if entry.get("sentence_quote")
    ]
    if verbatim_blocks:
        lines.extend([
            "",
            "## Verbatim Item 1 sentences",
            "",
            "Quote each sentence as-is in the per-FY discussion:",
            "",
        ])
        for entry in verbatim_blocks:
            fy_n = entry["fy"]
            lines.append(f"**FY{fy_n}:**")
            lines.append("")
            lines.append("> " + entry["sentence_quote"])
            lines.append("")

    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy_start": fy_s,
        "fy_end": fy_e,
        "series": series,
        "yoy": yoy,
        "answer_summary_block": answer_summary_block,
    })
