# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``extract_competition_section`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the sector_analyst
dispatch via ``invoke_skill_fn``. The recipe's prose playbook lives
next to this file in ``SKILL.md``.

Walks the issuer's Item 1 Business "Competition" subsection via the
sec-api Extractor (preferred) with an EDGAR direct-fetch fallback
for issuers where the Extractor doesn't slice item-level prose. The
walker is intentionally text-only; named competitors and specific
language are preserved verbatim so narrative atoms grade against
the issuer's actual disclosure.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from harness.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _fetch_filing_html_from_edgar,
    _find_10k_ref_for_fy,
    _sec_api_filing_url_from_accession,
)


# Subsection headers that typically follow Competition in Item 1
# Business. Anchoring on any of these terminates the Competition
# region. Order matters only as a search list; we take the EARLIEST
# match after the Competition anchor.
_NEXT_SUBSECTION_HEADERS: tuple[str, ...] = (
    "Research and Development",
    "Manufacturing",
    "Sales and Distribution",
    "Sales and Marketing",
    "Marketing and Sales",
    "Marketing, Sales",
    "Government Regulation",
    "Regulation",
    "Intellectual Property",
    "Seasonality",
    "Suppliers",
    "Customers",
    "Employees",
    "Human Capital",
    "Properties",
    "Environmental",
    "Backlog",
    "Available Information",
    "Cybersecurity",
    "Item 1A",
    "Risk Factors",
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


def _extract_competition_subsection(text: str) -> Optional[str]:
    """Find the "Competition" subsection prose in Item 1 Business text.

    Anchors on the standalone "Competition" header (a word followed
    by sentence content, not a passing mention inside another
    sentence). Returns the text from the header to the next
    recognized subsection header, or ``None`` when no anchor
    matches. The returned prose preserves the issuer's verbatim
    language; named competitors and specific phrasing carry through.
    """
    if not text:
        return None
    # The Competition header most reliably appears as a capitalized
    # token that begins a body sentence. Issuers split into two
    # observed layouts:
    #
    # 1. "Competition <body>" -- header inline with body, body
    #    starts with a paragraph-opener Capital word ("The market
    #    for...", "Our business...", "We compete...", "In our
    #    industry...").
    #
    # 2. "Competition Competition <body>" -- header word repeats as
    #    the heading + body start, with the body itself opening
    #    with "Competition in" / "Competition for" / "Competition
    #    is".
    #
    # The lookahead list covers both, plus a tail of lowercase
    # function-word body openers ("in", "for", "is", "from",
    # "among") that appear after the double-header. We accept any
    # "Competition" followed by either case so subsequent
    # subsection extraction still terminates correctly on the next
    # known Item 1 subsection header.
    header_re = re.compile(
        r"\bCompetition\s+(?=(?:The|Our|We|In|Although|While|"
        r"Many|There|This|Competition|in|for|is|from|among)\b)",
    )
    m = header_re.search(text)
    if not m:
        return None
    start = m.end()
    # Find the earliest next-subsection header AFTER the Competition
    # anchor. The Item 1A Risk Factors header is the hard upper
    # bound -- we always stop there even if no other header matches.
    end = len(text)
    for header in _NEXT_SUBSECTION_HEADERS:
        h_m = re.search(
            r"\b" + re.escape(header) + r"\b",
            text[start:],
        )
        if h_m and h_m.start() + start < end:
            end = h_m.start() + start
    # Cap at 8000 chars so an unexpected layout doesn't dump the
    # entire 10-K body. The canonical Competition subsection runs
    # 500-4000 chars; 8000 is generous headroom.
    if end - start > 8000:
        end = start + 8000
    prose = text[start:end].strip()
    # Drop trailing partial-sentence fragments (the next-header
    # boundary sometimes lands mid-paragraph). Truncate to the last
    # period followed by whitespace + capital letter.
    last_sentence_end = list(re.finditer(r"\.\s+(?=[A-Z])", prose))
    if last_sentence_end and len(prose) - last_sentence_end[-1].end() < 200:
        prose = prose[: last_sentence_end[-1].end()].strip()
    return prose if len(prose) >= 100 else None


def _fetch_item1_text(ref: str) -> str:
    """Pull Item 1 Business prose text via EDGAR direct-fetch.

    The sec-api Extractor is optimised for tables and routinely
    returns only partial / table-shaped snippets of Item 1 prose,
    which caused at least one issuer's Competition subsection to
    come back truncated mid-sentence in 0.0.538. EDGAR direct-fetch
    returns the FULL filing HTML, which we strip to plain text --
    enough surface for the Competition anchor + next-subsection
    terminator to slice the canonical subsection cleanly. Costs
    one extra round-trip per call (cached by ref).
    """
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


@skill_fn(
    skill_id="extract_competition_section",
    description=(
        "Pull the issuer's Item 1 Business 'Competition' subsection "
        "prose verbatim from the latest FY 10-K (or 20-F). Returns "
        "the full subsection text + the original 10-K ref. USE for "
        "any 'who does <ticker> see as competitors', 'how does "
        "<ticker> characterize its competitive landscape', or 'which "
        "of A vs B provides more detailed competitive disclosure' "
        "question. For multi-issuer comparisons, call ONCE PER "
        "TICKER and quote each issuer's section verbatim in the "
        "answer -- asymmetric retrieval (full prose for one ticker, "
        "thin snippet for the other) is the canonical failure mode "
        "this skill prevents. Generic across any issuer with a "
        "standard SEC Item 1 layout."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "fy": {
                "type": "integer",
                "description": (
                    "Fiscal year of the 10-K (or 20-F) to pull "
                    "from. Use the latest reported FY whose annual "
                    "report is filed at the question's asof."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy"],
    },
)
def extract_competition_section(
    ticker: str,
    fy: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Pull the Item 1 Business 'Competition' subsection verbatim.
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

    item1_text = _fetch_item1_text(ref)
    if not item1_text:
        return {
            "ticker": t,
            "fy": fy_int,
            "ref": ref,
            "filed_at": filed,
            "error": (
                "could not retrieve Item 1 Business text "
                f"for {t} FY{fy_int} 10-K (ref={ref})"
            ),
        }

    competition_text = _extract_competition_subsection(item1_text)
    if not competition_text:
        return {
            "ticker": t,
            "fy": fy_int,
            "ref": ref,
            "filed_at": filed,
            "item1_text_chars": len(item1_text),
            "error": (
                f"Competition subsection anchor not found in {t} "
                f"FY{fy_int} 10-K Item 1 ({len(item1_text):,} chars). "
                "The issuer may have an atypical Item 1 layout; "
                "fall back to `get_full_text(ref=...)` on the 10-K "
                "and scan for competitor names directly."
            ),
        }

    lines = [
        f"# {t} Item 1 Business — Competition subsection (FY{fy_int})",
        "",
        f"10-K ref: `{ref}` (filed {filed or 'unknown'})",
        f"Subsection length: {len(competition_text):,} chars.",
        "",
        "## Verbatim Competition disclosure",
        "",
        "> " + competition_text.replace("\n", "\n> "),
        "",
        "## How to use this text",
        "",
        "Quote the verbatim disclosure above in the per-issuer "
        "competitive-landscape section of the final answer. The "
        "issuer's own language (named competitors, characterization "
        "of substitute entertainment, scale-of-competitors framing) "
        "is what narrative atoms grade against. Do NOT paraphrase "
        "into a summary -- the rubric atoms grade verbatim phrasing.",
    ]
    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy": fy_int,
        "ref": ref,
        "filed_at": filed,
        "competition_text": competition_text,
        "competition_text_chars": len(competition_text),
        "answer_summary_block": answer_summary_block,
    })
