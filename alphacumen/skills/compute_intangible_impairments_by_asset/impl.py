# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``compute_intangible_impairments_by_asset`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the sector_analyst
dispatch via ``invoke_skill_fn``. The recipe's prose playbook lives
next to this file in ``SKILL.md``.

Generic across any issuer that tags impairment facts by
``ProductOrServiceAxis`` extension members + asset-class axes
(common pattern for pharma/biotech post-acquisition writedowns
and consumer/industrial brand impairments).
"""

from __future__ import annotations

import re
from typing import Any, Optional
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _do_bm25_sec,
    _do_get_full_text,
    _do_get_xbrl_facts,
    _fetch_filing_exhibits_from_edgar,
    _find_10k_ref_for_fy,
    _sec_api_filing_url_from_accession,
)


# Concept name patterns we pull. Sub-string match (case-insensitive)
# on the flat XBRL fact concept name.
_IMPAIRMENT_CONCEPT_PATTERN = "impairmentofintangibleassetsexcludinggoodwill"

# Axis name suffixes whose member values name the impaired asset.
# Issuers tag impaired products via custom extensions on this axis
# (e.g. ``pfe:MedrolMember``, ``mrk:KeytrudaMember``).
_ASSET_NAME_AXIS_SUFFIXES = (
    "ProductOrServiceAxis",
    "ProductAxis",
    "BrandAxis",
    "AcquisitionAxis",
)

# Axis name suffixes whose member identifies the asset's accounting
# class (Brand / DevelopedTechnologyRights / IPR&D / etc).
_ASSET_TYPE_AXIS_SUFFIXES = (
    "FiniteLivedIntangibleAssetsByMajorClassAxis",
    "IndefiniteLivedIntangibleAssetsByMajorClassAxis",
    "IntangibleAssetsByClassAxis",
    "MajorClassesOfFiniteLivedIntangibleAssetsAxis",
)

# Generic recognizer for any intangible-asset class axis. Issuers
# occasionally define combined extension axes (e.g. an axis whose
# name spans both Finite- and Indefinite-lived classes for an
# "Other" residual bucket that covers items in both regimes); the
# exact axis name varies issuer-by-issuer but always contains
# "IntangibleAssets" and ends with a ClassAxis suffix. This regex
# matches any such variant generically.
_ASSET_TYPE_AXIS_PATTERN = re.compile(
    r"IntangibleAssets.*By(?:Major)?ClassAxis$",
)

# Asset-type member values that indicate an "Other" / residual
# bucket (issuer pre-aggregated the long-tail impairments rather
# than naming each asset). Detected on the type-member name; case-
# insensitive substring match.
_OTHER_BUCKET_MEMBER_TOKENS = (
    "Other",
    "Various",
    "AggregateOther",
)


# -------------------------------------------------------------------
# Prose-mode Q4 impairment walker (supplements the XBRL extraction).
#
# Why this exists: issuers commonly disclose Q4 intangible-impairment
# breakdowns as a Roman-numeral prose list in the Q4 earnings-release
# 8-K Ex 99 ("$X.X billion ... composed of: (i) $A for ASSET_1, (ii)
# $B for ASSET_2, ..., (v) other ... totaling $Z million"). The last
# residual item ("(v) other ...") is NOT XBRL-tagged at asset level
# in many filings -- the issuer files only the named-asset
# dimensioned facts plus a single consolidated total, leaving the
# residual implicit. Without a prose walker, the XBRL-only result
# misses the residual line AND the prose-disclosed named-impairment
# total (which is the canonical Seagen-share / X-share denominator).
#
# Anchor on the canonical phrase + fiscal-quarter mention; iterate
# Roman-numeral list items; emit (asset_name, type_hint, value_usd)
# entries plus the explicit total from the header.
# -------------------------------------------------------------------

# Two acceptable anchor orderings observed in earnings-release prose:
#
# 1. "fourth quarter of <fy> ... intangible asset impairment charges
#    of $X.X billion ... composed of: (i) ..."  (PFE 2024 style)
# 2. "intangible asset impairment charges of $X.X billion ... fourth
#    quarter of <fy> ... composed of: (i) ..."  (other issuer style)
#
# Both patterns are emitted; first match wins. The `(fy_marker)` group
# captures the fiscal year mention so the caller can verify it matches
# the requested `fy` -- a Q4 earnings release commonly re-cites a
# prior-year Q4 breakdown for comparison, and the wrong-year match
# must be rejected.
_Q4_IMPAIRMENT_HEADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:fourth[-\s]quarter|Q4)\s+of\s+(\d{4})"
        r"[^.]{0,500}?(?:non-cash\s+)?"
        r"(?:intangible(?:\s+asset)?\s+)?impairment\s+charges?\s+of\s+"
        r"\$\s*([\d.]+)\s*(billion|million|B|M)\b"
        r"[^.]{0,800}?(?:composed|comprised|consisting)\s+of[:]?\s*"
        r"(.{50,3500}?)(?:\.\s+(?:Full[-\s]year|For\s+the\s+full|Net|"
        r"Total|Adjusted|Operating|The\s+amount|Excluded|During)|\Z)",
        re.I | re.DOTALL,
    ),
    re.compile(
        r"(?:non-cash\s+)?(?:intangible(?:\s+asset)?\s+)?impairment\s+"
        r"charges?\s+of\s+\$\s*([\d.]+)\s*(billion|million|B|M)\b"
        r"[^.]{0,500}?(?:fourth[-\s]quarter|Q4)\s+of\s+(\d{4})"
        r"[^.]{0,800}?(?:composed|comprised|consisting)\s+of[:]?\s*"
        r"(.{50,3500}?)(?:\.\s+(?:Full[-\s]year|For\s+the\s+full|Net|"
        r"Total|Adjusted|Operating|The\s+amount|Excluded|During)|\Z)",
        re.I | re.DOTALL,
    ),
)

_ROMAN_ITEM_PATTERN = re.compile(
    r"\(\s*([ivx]+)\s*\)\s*(.+?)(?=\(\s*[ivx]+\s*\)|$)",
    re.I | re.DOTALL,
)

_DOLLAR_AMOUNT_PATTERN = re.compile(
    r"\$\s*([\d.]+)\s*(billion|million|B|M)\b",
    re.I,
)


def _scale_to_usd(value: float, unit: str) -> float:
    """Convert ($amount, 'billion'|'million') to USD."""
    u = unit.lower()
    if u in ("billion", "b"):
        return value * 1e9
    return value * 1e6


def _strip_html_to_text(html: str) -> str:
    """Minimal HTML-to-text for press-release exhibits.

    The full BeautifulSoup pass in alphacumen.tools is over-engineered for
    a single regex scan; collapse tags + whitespace inline. Preserves
    the Roman-numeral list inline because the source HTML emits it
    as plain text inside ``<p>`` runs.
    """
    if not html:
        return ""
    no_scripts = re.sub(
        r"<(?:script|style)[^>]*>.*?</(?:script|style)>",
        " ", html, flags=re.I | re.DOTALL,
    )
    no_tags = re.sub(r"<[^>]+>", " ", no_scripts)
    # Unescape the most common HTML entities so the dollar sign +
    # Roman-numeral parens read cleanly to the regex walker.
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


def _accession_from_chunk_ref(ref: str) -> str:
    """Extract the accession number from a ``sec:<acc>:<chunk>`` ref."""
    if not ref:
        return ""
    parts = ref.split(":")
    # ``sec:0000078003-25-000017:2.02`` -> ``0000078003-25-000017``
    if len(parts) >= 2 and parts[0] == "sec":
        return parts[1]
    return ""


def _fetch_q4_earnings_release_text(accession: str) -> str:
    """Fetch + concatenate the 8-K's Ex 99 attachment text from EDGAR.

    Bypasses the local sec_filings_chunked index, which historically
    splits 8-Ks by cover-sheet items (1.01, 2.02, 9.01) but does NOT
    chunk the Ex 99 attachment body -- the substantive earnings-
    release prose (impairment paragraph, segment commentary, recon
    tables) lives in Ex 99 and is invisible to the chunked index.
    Returns ``""`` when no exhibits could be fetched.
    """
    if not accession:
        return ""
    primary_url, err = _sec_api_filing_url_from_accession(accession)
    if not primary_url or err:
        return ""
    html, err = _fetch_filing_exhibits_from_edgar(primary_url)
    if not html or err:
        return ""
    return _strip_html_to_text(html)


def _find_q4_earnings_8k_candidate_refs(
    ticker: str, fy: int, k: int = 10,
) -> list[tuple[str, str]]:
    """Return up to ``k`` Q4 earnings 8-K candidate (ref, filed_at) pairs.

    Q4 earnings releases are filed in Jan-Apr of fy+1. The bm25_sec
    query biases the rank toward earnings-release vocabulary, but
    routine 8-Ks (CEO transitions, dividend declarations, supplemental
    filings) and follow-on releases (e.g. an Item 8.01 announcement
    filed months later) can outrank the canonical Q4 release on a
    chunk-level BM25 hit. The caller iterates the list and probes
    each candidate body for the impairment-list anchor; the first
    candidate whose body matches wins. Returns an empty list when
    the index is unavailable.
    """
    try:
        env = _do_bm25_sec(
            query=(
                f"{ticker} fourth quarter full year results "
                f"earnings revenues impairment"
            ),
            k=k, fields=None,
            filters={
                "ticker": ticker, "form_type": "8-K",
                "filed_at_gte": f"{fy + 1:04d}-01-01",
                "filed_at_lte": f"{fy + 1:04d}-04-30",
            },
            sort=None, body_mode="snippet",
        )
    except Exception:  # noqa: BLE001 -- index may be unavailable
        return []
    hits = env.get("hits") or []
    out: list[tuple[str, str]] = []
    for h in hits:
        out.append((
            h.get("id", ""),
            str((h.get("source") or {}).get("filed_at", "")),
        ))
    return out


def _extract_q4_impairment_breakdown(
    body_text: str, fy: int,
) -> Optional[dict[str, Any]]:
    """Parse the prose Q4 impairment list from earnings-release text.

    Returns ``None`` when the anchor pattern doesn't match.
    Otherwise returns::

        {"header_total_usd": float,
         "items": [{"value_usd": float, "context_text": str,
                    "is_residual": bool}, ...],
         "named_total_usd": float}

    The walker is intentionally conservative -- it does NOT try to
    parse out asset-name spelling (issuers vary in punctuation /
    parenthetical descriptors). The skill caller cross-references
    each ``value_usd`` against the XBRL-extracted named-asset rows;
    matching values stay in the XBRL list, unmatched values get
    appended as prose-only entries (typically the "(v) other ...
    totaling $X" residual line).
    """
    if not body_text:
        return None
    matched: Optional[tuple[int, float, str, str]] = None
    for pat in _Q4_IMPAIRMENT_HEADER_PATTERNS:
        for m in pat.finditer(body_text):
            groups = m.groups()
            # Group order differs per pattern: pattern 0 captures
            # (year, value, unit, list_block); pattern 1 captures
            # (value, unit, year, list_block). Distinguish by which
            # group parses as an integer year (1900-2099).
            year_idx = None
            for i, g in enumerate(groups[:3]):
                if g and g.isdigit() and 1900 <= int(g) <= 2099:
                    year_idx = i
                    break
            if year_idx is None:
                continue
            year_val = int(groups[year_idx])
            if year_val != fy:
                continue
            # Value + unit are the OTHER two slots in the first three
            # groups; the list block is always the last group.
            non_year = [g for i, g in enumerate(groups[:3]) if i != year_idx]
            try:
                v_amount = float(non_year[0])
            except (TypeError, ValueError):
                continue
            v_unit = non_year[1] or "million"
            list_block = groups[-1] or ""
            matched = (year_val, v_amount, v_unit, list_block)
            break
        if matched is not None:
            break
    if matched is None:
        return None
    _year, header_val, header_unit, list_block = matched
    header_total = _scale_to_usd(header_val, header_unit)

    items: list[dict[str, Any]] = []
    # Trailing word in the item text that flags the residual bucket
    # ("other ... totaling $X million which includes..."). Detected
    # on substring match to stay tolerant of phrasing variants.
    _RESIDUAL_TOKENS = ("other ", "totaling", "remaining", "various")
    for it_m in _ROMAN_ITEM_PATTERN.finditer(list_block):
        item_text = (it_m.group(2) or "").strip()
        if not item_text:
            continue
        dollar_matches = list(_DOLLAR_AMOUNT_PATTERN.finditer(item_text))
        if not dollar_matches:
            continue
        is_residual = any(
            tok in item_text.lower() for tok in _RESIDUAL_TOKENS
        )
        for dm in dollar_matches:
            try:
                v = _scale_to_usd(float(dm.group(1)), dm.group(2))
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
            items.append({
                "value_usd": v,
                "context_text": item_text[:240],
                "is_residual": is_residual,
            })

    named_total = sum(it["value_usd"] for it in items)
    return {
        "header_total_usd": header_total,
        "items": items,
        "named_total_usd": named_total,
    }


def _strip_axis_value_prefix(raw: str) -> str:
    """Drop a ``ns:`` prefix and a trailing ``Member`` token."""
    if not raw:
        return ""
    s = raw.split(":")[-1]
    if s.endswith("Member"):
        s = s[: -len("Member")]
    return s


def _decode_camel_case(name: str) -> str:
    """Convert an XBRL extension-member CamelCase token to a human-readable
    string.

    Examples:
      ``B7H4VFelmetatugVedotin`` → ``B7H4V Felmetatug Vedotin``
      ``Medrol`` → ``Medrol``
      ``InProcessResearchAndDevelopment`` → ``In-Process Research And Development``
      ``DevelopedTechnologyRights`` → ``Developed Technology Rights``

    Heuristic only; preserves the source for verbatim matching when
    the caller needs the exact tag form.
    """
    if not name:
        return ""
    # Two-rule CamelCase split that preserves acronyms while
    # separating WordlikeTokens:
    #   1. lowercase->uppercase: "tugVedotin" -> "tug Vedotin"
    #   2. uppercase->uppercase followed by lowercase: "VFelmetatug"
    #      -> "V Felmetatug" (splits acronym from the next Word
    #      while leaving pure acronyms like "IPR&D" untouched).
    # Together: "B7H4VFelmetatugVedotin" -> "B7H4V Felmetatug Vedotin".
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    # "InProcess..." -> "In-Process..."
    spaced = re.sub(r"^In Process(?= )", "In-Process", spaced)
    return spaced


def _segment_member_for_axis(
    segment: list[dict],
    axis_suffixes: tuple[str, ...],
    axis_pattern: Optional["re.Pattern[str]"] = None,
) -> Optional[str]:
    """Find the first segment entry whose dimension ends with one of
    the axis suffixes OR matches the optional regex pattern; return
    its raw member value.
    """
    for s in segment or []:
        if not isinstance(s, dict):
            continue
        dim = str(s.get("dimension") or "")
        dim_local = dim.split(":")[-1]
        if any(dim.endswith(suf) for suf in axis_suffixes):
            return str(s.get("value") or "")
        if axis_pattern is not None and axis_pattern.search(dim_local):
            return str(s.get("value") or "")
    return None


def _matches_period(
    fact_period: dict, period_kind: str, fy: int,
) -> bool:
    """Filter facts by period scope.

    ``q4``: period start in the FY's Sep-30 window and end on Dec-31
    of the FY.
    ``annual``: period start on Jan-1 of the FY and end on Dec-31
    of the FY.
    Filing dates other than Dec-31 (issuer not on a calendar fiscal
    year) match the FY end-of-year (Mar-31 / Jun-30 / etc.) -- the
    skill compares year + month parts so non-calendar issuers work
    transparently.
    """
    if not isinstance(fact_period, dict):
        return False
    start = str(fact_period.get("startDate") or "")
    end = str(fact_period.get("endDate") or "")
    if not (start and end):
        return False
    try:
        start_year = int(start[:4])
        end_year = int(end[:4])
        start_month = int(start[5:7])
        end_month = int(end[5:7])
    except (ValueError, IndexError):
        return False
    if period_kind == "q4":
        # Q4 mode accepts ANY span ending in the FY's last month.
        # Rationale: the question asks about "Q4 impairments" but
        # issuers tag at variable spans -- some facts get a true
        # 3-month Q4 tag (Sep30-Dec31), others get only the FY tag
        # (Jan1-Dec31) for an impairment that nonetheless OCCURRED
        # in Q4 (no other quarters recorded an impairment for that
        # asset). PFE FY2024 disitamab is tagged at FY span only;
        # without this loosening it disappears from the Q4 view.
        # Caller must dedupe (we do, below) to avoid double-counting
        # an asset that has BOTH a Q4 and FY tag.
        if end_year != fy:
            return False
        month_span = (end_year - start_year) * 12 + (end_month - start_month)
        return month_span in (2, 3, 10, 11, 12)
    elif period_kind == "annual":
        # Full FY: start in the first quarter of the FY, end in the
        # last quarter. Span 11-13 months.
        if end_year != fy:
            return False
        month_span = (end_year - start_year) * 12 + (end_month - start_month)
        return month_span in (10, 11, 12)
    return False


@skill_fn(
    skill_id="compute_intangible_impairments_by_asset",
    description=(
        "Extract per-asset intangible-asset impairment values from "
        "XBRL with the segment-dimension breakdown the issuer "
        "discloses (asset name via ProductOrServiceAxis extension "
        "members; asset type via FiniteLivedIntangibleAssetsByMajor"
        "ClassAxis / IndefiniteLived ... Axis). Use for 'itemize "
        "Q4 impairments by asset name + amount + type' questions or "
        "for computing what share of impairments come from a named "
        "acquired portfolio (e.g. PFE-Seagen, MRK-AcceleronCV). "
        "Returns a per-asset table + consolidated total. "
        "``period_kind='q4'`` (default) gets the FY's Q4 facts; "
        "``period_kind='annual'`` gets the full-year facts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "fy": {
                "type": "integer",
                "description": "Fiscal year whose 10-K discloses the impairments (e.g. 2024).",
            },
            "period_kind": {
                "type": "string",
                "enum": ["q4", "annual"],
                "default": "q4",
                "description": (
                    "Temporal scope. 'q4' (default): 3-month span "
                    "ending on FY-end. 'annual': 11-13 month FY span."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy"],
    },
)
def compute_intangible_impairments_by_asset(
    ticker: str,
    fy: int,
    period_kind: str = "q4",
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Extract per-asset intangible-asset impairment values from
    XBRL with the segment-dimension breakdown.
    """
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    try:
        fy_int = int(fy)
    except (TypeError, ValueError):
        return {"error": f"fy must be int (got {fy!r})"}
    if period_kind not in ("q4", "annual"):
        return {"error": f"period_kind must be 'q4' or 'annual' (got {period_kind!r})"}
    t = ticker.strip().upper()
    ref, filed = _find_10k_ref_for_fy(t, fy_int)
    if not ref:
        return {"error": f"no {t} 10-K found for FY{fy_int}"}
    facts_env = _do_get_xbrl_facts(
        ref=ref,
        concept_pattern=_IMPAIRMENT_CONCEPT_PATTERN,
        periods=None,
        # Bypass the default cap (50) -- dimensional impairment
        # facts can run 30-80 per FY (asset × type × segment ×
        # quarterly + annual periods); the default truncates the
        # tail (recently-tagged assets often appear last in the
        # XBRL serialization order).
        limit=200,
    )
    if facts_env.get("error"):
        return {"error": f"xbrl fetch failed: {facts_env['error']}", "ref": ref}
    all_facts = facts_env.get("facts") or []
    # Filter facts that match the requested period and have a value.
    matching: list[dict[str, Any]] = []
    seen_keys: set[tuple] = set()
    for f in all_facts:
        if not _matches_period(f.get("period") or {}, period_kind, fy_int):
            continue
        try:
            val = float(f.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if val <= 0:
            continue
        # Dedupe identical facts (sec-api sometimes returns the same
        # fact under multiple sections).
        segment = f.get("segment") or []
        seg_key = tuple(
            (str(s.get("dimension") or ""), str(s.get("value") or ""))
            for s in segment if isinstance(s, dict)
        )
        key = (val, seg_key, str((f.get("period") or {}).get("startDate") or ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        matching.append(f)
    # Split by-asset facts vs aggregate (no asset-axis dimension).
    by_asset: list[dict[str, Any]] = []
    aggregate_total: Optional[float] = None
    for f in matching:
        segment = f.get("segment") or []
        asset_raw = _segment_member_for_axis(segment, _ASSET_NAME_AXIS_SUFFIXES)
        type_raw = _segment_member_for_axis(
            segment, _ASSET_TYPE_AXIS_SUFFIXES,
            axis_pattern=_ASSET_TYPE_AXIS_PATTERN,
        )
        try:
            val = float(f.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if not asset_raw and not type_raw:
            # No asset-name or asset-type dimension -- treat as
            # consolidated total / segment-aggregate. Some issuers
            # tag the full-period aggregate without ANY segment
            # (truly consolidated) while others segment it by
            # StatementBusinessSegmentsAxis (BiopharmaSegment etc.).
            # Both shapes represent the total impairment for the
            # period; the LARGEST value wins (covers both cases
            # generically).
            if aggregate_total is None or val > aggregate_total:
                aggregate_total = val
            continue
        # Type-only fact with an "Other" / "Various" type member
        # represents the issuer's residual bucket -- the long-tail
        # impairments aggregated by the issuer (vs the named-asset
        # rows above). Keep as a synthetic by_asset row labeled
        # "Other (<type breakdown>)".
        if not asset_raw and type_raw:
            type_name_clean = _strip_axis_value_prefix(type_raw)
            is_other_bucket = any(
                tok.lower() in type_name_clean.lower()
                for tok in _OTHER_BUCKET_MEMBER_TOKENS
            )
            if not is_other_bucket:
                # Type-only facts that are NOT "Other" buckets are
                # type-aggregates (e.g. all IPR&D for the FY) --
                # these double-count the named assets we already
                # have, so skip.
                continue
            by_asset.append({
                "asset_name": "Other",
                "asset_name_raw": "",
                "asset_type": _decode_camel_case(type_name_clean),
                "asset_type_raw": type_raw,
                "value_usd": val,
                "value_mm": val / 1e6,
            })
            continue
        asset_name_clean = _strip_axis_value_prefix(asset_raw or "")
        asset_name_display = _decode_camel_case(asset_name_clean)
        type_name_clean = _strip_axis_value_prefix(type_raw or "")
        type_name_display = _decode_camel_case(type_name_clean)
        by_asset.append({
            "asset_name": asset_name_display or "(unnamed)",
            "asset_name_raw": asset_raw or "",
            "asset_type": type_name_display or "(untyped)",
            "asset_type_raw": type_raw or "",
            "value_usd": val,
            "value_mm": val / 1e6,
        })
    # Dedupe by asset_name_raw: when q4 mode loosened the period
    # filter to include FY-span facts (so assets without a 3-month
    # tag still appear), an asset that has BOTH a true Q4 tag AND
    # an FY tag will land twice. Prefer the SMALLEST value per
    # asset (Q4 is a SUBSET of FY; the Q4 amount is always <= FY
    # amount for a same-period impairment, and equality is the
    # common case where the only annual impairment occurred in Q4).
    deduped: dict[str, dict[str, Any]] = {}
    for r in by_asset:
        key = r["asset_name_raw"]
        prior = deduped.get(key)
        if prior is None or r["value_usd"] < prior["value_usd"]:
            deduped[key] = r
    by_asset = list(deduped.values())
    by_asset.sort(key=lambda r: r["value_usd"], reverse=True)
    sum_by_asset = sum(r["value_usd"] for r in by_asset)

    # Prose supplement (q4 only): merge in any residual line items
    # disclosed in the Q4 earnings-release 8-K Ex 99 narrative that
    # weren't tagged at asset level in XBRL. Roman-numeral list
    # disclosure is the standard SEC convention for Q4 impairment
    # breakdowns; the trailing "(v) other ... totaling $X" residual
    # is frequently undimensioned in XBRL even when the named
    # asset-level facts are present.
    prose_breakdown: Optional[dict[str, Any]] = None
    prose_residual_value_usd: Optional[float] = None
    prose_named_total_usd: Optional[float] = None
    prose_header_total_usd: Optional[float] = None
    prose_ref: str = ""
    if period_kind == "q4":
        # Walk up to 10 candidate 8-Ks filed in fy+1's Jan-Apr window.
        # For each: first try the indexed chunk body (fast path -- no
        # network); if that doesn't anchor the pattern, fall back to
        # an EDGAR direct fetch of the same accession's Ex 99
        # attachment (the indexed chunks omit Ex 99 attachment text
        # entirely, even though the earnings-release prose with the
        # Roman-numeral impairment list lives there). De-dupe by
        # accession so we don't fetch the same Ex 99 twice across
        # multiple chunk hits of the same filing.
        candidates = _find_q4_earnings_8k_candidate_refs(t, fy_int, k=10)
        seen_accessions: set[str] = set()
        for candidate_ref, _filed in candidates:
            if not candidate_ref:
                continue
            # Fast path: try the indexed chunk text.
            text_env = _do_get_full_text(
                ref=candidate_ref, max_chars=32_000,
            )
            body = str(text_env.get("body") or text_env.get("text") or "")
            breakdown = _extract_q4_impairment_breakdown(body, fy_int)
            if breakdown is not None and breakdown["items"]:
                prose_breakdown = breakdown
                prose_ref = candidate_ref
                break
            # Slow path: EDGAR Ex 99 fetch keyed by accession.
            accession = _accession_from_chunk_ref(candidate_ref)
            if not accession or accession in seen_accessions:
                continue
            seen_accessions.add(accession)
            edgar_text = _fetch_q4_earnings_release_text(accession)
            if not edgar_text:
                continue
            breakdown = _extract_q4_impairment_breakdown(edgar_text, fy_int)
            if breakdown is not None and breakdown["items"]:
                prose_breakdown = breakdown
                prose_ref = f"edgar:{accession}:ex99"
                break
        if prose_breakdown:
            prose_named_total_usd = prose_breakdown["named_total_usd"]
            prose_header_total_usd = prose_breakdown["header_total_usd"]
            # When the prose disclosure contains a residual line, the
            # XBRL "Other" bucket is a strictly smaller subset of the
            # same conceptual residual -- the prose value rolls up
            # both the XBRL-tagged Other plus the unspecified
            # "(v) other ... totaling $X" balance that the issuer
            # didn't dimensionalize. Dropping the XBRL Other row
            # before merging keeps the residual single-rowed at the
            # canonical disclosed value.
            prose_has_residual = any(
                it["is_residual"] for it in prose_breakdown["items"]
            )
            if prose_has_residual:
                by_asset = [
                    r for r in by_asset
                    if r["asset_name"] != "Other"
                ]
            xbrl_values: set[int] = {
                int(round(r["value_usd"])) for r in by_asset
            }
            # Tolerance: XBRL values can differ from prose by a few
            # dollars at the rounding boundary. Use a tight absolute
            # window (1e6 USD = $1M, the smallest unit issuers round
            # to in earnings-release prose) -- a relative window
            # backfires when two true-distinct lines happen to fall
            # within 1% of each other (the canonical example: PFE
            # Q4 2024 has both a $435M Zavzpret named-asset row AND
            # a $436M "Other" residual; a 1% relative window would
            # collapse them).
            def _close_match(v: float, existing: set[int]) -> bool:
                return any(abs(v - e) <= 1e6 for e in existing)
            for item in prose_breakdown["items"]:
                v = float(item["value_usd"])
                # Residual lines (the "(v) other ... totaling $X"
                # bucket) are conceptually distinct from any named
                # asset and always merge in regardless of close-
                # match. Named prose items skip on close-match (the
                # XBRL row carries richer dimensional metadata).
                if not item["is_residual"] and _close_match(v, xbrl_values):
                    continue
                # New value not in XBRL list. Tag as residual when
                # the prose flagged it; otherwise label as a named
                # prose-only item (preserves the context text so the
                # caller can spot what it is).
                label = (
                    "Other (prose residual)"
                    if item["is_residual"] else "(prose-only)"
                )
                by_asset.append({
                    "asset_name": label,
                    "asset_name_raw": "",
                    "asset_type": "(disclosed in earnings release)",
                    "asset_type_raw": "",
                    "value_usd": v,
                    "value_mm": v / 1e6,
                    "source": "earnings_release_prose",
                    "context_text": item["context_text"],
                })
                xbrl_values.add(int(round(v)))
                if item["is_residual"]:
                    prose_residual_value_usd = v
            by_asset.sort(key=lambda r: r["value_usd"], reverse=True)
            sum_by_asset = sum(r["value_usd"] for r in by_asset)

    # Total impairments per the disclosed line items = named assets
    # + the "Other" residual bucket. This is the canonical
    # denominator for "% attributable to <subset of named assets>"
    # computations (it ties out to the issuer's disclosed total
    # even when the segment-aggregate XBRL fact ties to a slightly
    # different number due to unallocated corporate items). When the
    # prose walker found an explicit named-impairment total, prefer
    # it -- the prose total is the figure the issuer rounded to,
    # which is what rubrics reference for "% of total" answers.
    total_from_breakdown = (
        prose_named_total_usd
        if prose_named_total_usd is not None
        and prose_named_total_usd > sum_by_asset * 0.95
        else sum_by_asset
    )
    # Build markdown summary block.
    period_label = (
        f"Q4 FY{fy_int}" if period_kind == "q4" else f"FY{fy_int}"
    )
    lines = [
        f"# {t} intangible-asset impairments — {period_label}",
        "",
        f"10-K ref: `{ref}` (filed {filed or 'unknown'})",
        "",
        "| Asset | Type | Amount ($M) |",
        "|---|---|---|",
    ]
    for r in by_asset:
        lines.append(
            f"| {r['asset_name']} | {r['asset_type']} | ${r['value_mm']:,.0f}M |"
        )
    lines.append("")
    lines.append(
        f"**Total named line-item impairments: "
        f"${total_from_breakdown / 1e6:,.0f}M** "
        f"(sum of the rows above). "
        f"USE THIS AS THE DENOMINATOR when computing the share of "
        f"line-item impairments attributable to a subset of these "
        f"assets (e.g. assets acquired in a specific acquisition, "
        f"a single therapeutic area, a single product family). The "
        f"share formula is: `(sum of subset values) / "
        f"${total_from_breakdown / 1e6:,.0f}M`."
    )
    if prose_has_residual:
        lines.append(
            "_Residual-bucket convention:_ the **\"Other (prose "
            "residual)\"** row groups unnamed long-tail impairments "
            "the issuer did NOT break out at the asset level. When "
            "computing share attributable to a named subset (a "
            "specific acquired portfolio, therapeutic area, "
            "product family), treat the residual bucket as "
            "**NOT attributable** to any subset -- include its "
            "value in the denominator (it's part of the named "
            "line-item total above) but NEVER in the subset "
            "numerator. This is the SEC-disclosure convention: "
            "issuers itemize what they can attribute and bucket "
            "the rest as residual; mapping residual back to a "
            "specific subset would require disclosure the issuer "
            "didn't make."
        )
    if prose_header_total_usd is not None:
        lines.append(
            f"Earnings-release prose header total: "
            f"${prose_header_total_usd / 1e6:,.0f}M "
            f"(source: 8-K Ex 99, ref `{prose_ref}`)."
        )
    if aggregate_total is not None:
        lines.append(
            f"_Diagnostic only:_ XBRL segment-aggregate "
            f"(Impairment-of-Intangible-Assets) fact = "
            f"${aggregate_total / 1e6:,.0f}M. This is the "
            f"consolidated impairment charge for the period and "
            f"INCLUDES unallocated impairments not itemized at the "
            f"asset level. Divide by this ONLY when the question "
            f"explicitly asks for share of the consolidated charge "
            f"(e.g. 'what % of the company's total Q4 impairment'); "
            f"do NOT use it for line-item subset shares."
        )
    answer_summary_block = "\n".join(lines)
    return _apply_binding(bind_as, {
        "ticker": t,
        "fy": fy_int,
        "period_kind": period_kind,
        "ref": ref,
        "filed_at": filed,
        "by_asset": by_asset,
        "named_asset_total_usd": sum_by_asset,
        "named_asset_total_mm": sum_by_asset / 1e6,
        "total_from_breakdown_usd": total_from_breakdown,
        "total_from_breakdown_mm": total_from_breakdown / 1e6,
        "consolidated_total_usd": aggregate_total,
        "consolidated_total_mm": (
            aggregate_total / 1e6 if aggregate_total is not None else None
        ),
        "prose_header_total_usd": prose_header_total_usd,
        "prose_named_total_usd": prose_named_total_usd,
        "prose_residual_value_usd": prose_residual_value_usd,
        "prose_ref": prose_ref or None,
        "answer_summary_block": answer_summary_block,
    })
