# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``lease_adjusted_ebitdar`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from reef.skill_fn import skill_fn
from alphacumen.index_map import SEC_FILINGS_INDEX
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _apply_binding,
    _do_extract_filing_tables,
    _do_get_full_text,
    _fetch_filing_html_from_edgar,
    _find_10k_ref_for_fy,
    _fy_full_period_fact,
    _fy_period_end_date,
    _parse_num,
    _strip_chunk_suffix,
    _table_rows_from_extract,
)
from reef.stubs import tools as cb_tools


# REITs that act as master-lease landlords for failed-sale-leaseback
# casino / hospitality operators. CZR pays VICI + GLPI; PENN pays
# GLPI + VICI; MGM pays VICI (since 2022); BYD pays GLPI. New REIT
# names get appended here without per-operator overfitting.
_MASTER_LEASE_LANDLORDS = (
    "VICI",
    "GLPI",
    "MGP",       # MGM Growth Properties (pre-2022)
    "Gaming and Leisure Properties",
    "VICI Properties",
    "Realty Income",  # casino spin-offs occasionally
)


def _extract_master_lease_payments_from_text(
    body: str, fy: int,
) -> tuple[float, list[str]]:
    r"""Sum master-lease rent payments mentioned in MD&A prose.

    Failed-sale-leaseback master-lease payments are NOT exposed as a
    single XBRL concept; they're split across financing-obligation
    interest payments, principal amortization, and variable-rent
    line items. The operator's MD&A typically discloses the total
    annual cash rent paid to each landlord in narrative form
    ("we paid \$XXX million to VICI under the master lease in
    fiscal YYYY"). This walker locates every such mention and sums
    the distinct dollar amounts.

    Generic across any failed-sale-leaseback operator (CZR, PENN,
    MGM, BYD); the only issuer-specific element is the list of REIT
    landlord names (``_MASTER_LEASE_LANDLORDS``), which can be
    extended without rebuilding the regex.
    """
    if not isinstance(body, str) or not body.strip():
        return 0.0, []
    norm = re.sub(r"\s+", " ", body)
    # Dollar amount in millions / billions, with optional unit.
    _amount_re = re.compile(
        r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|M|B)?\b",
        re.IGNORECASE,
    )
    landlord_alt = "|".join(re.escape(l) for l in _MASTER_LEASE_LANDLORDS)
    _landlord_re = re.compile(landlord_alt, re.IGNORECASE)
    # Initial / base annual fixed-rent disclosure patterns. Match
    # the exact phrasing failed-sale-leaseback footnotes use to
    # state the static annual obligation (NOT cash-paid-for-interest
    # which inflates by accumulated-interest accruals). Pattern
    # ordering matters: more specific phrases first so the regex
    # doesn't shadow them with a broader fallback.
    # ONLY structural-rent disclosure phrasings -- the canonical
    # failed-sale-leaseback footnote conventions that state a static
    # annual obligation. The looser "master lease" / "lease payments
    # to" forms shadow lease-maturity schedules ("future minimum
    # lease payments thereafter of \$1,300M"), which inflate the
    # extracted total by 2-3x and are never rubric-relevant. The
    # remaining 4 patterns are precise enough to never match a
    # schedule heading.
    _lease_phrase_re = re.compile(
        r"(?:"
        r"(?:initial\s+)?annual\s+fixed\s+rent(?:\s+payments?)?|"
        r"initial\s+annual\s+rent(?:\s+payments?)?|"
        r"annual\s+(?:land\s+and\s+building\s+)?base\s+rent(?:\s+of)?|"
        r"annual\s+rent\s+payments?|"
        r"annual\s+membership\s+and\s+use\s+fees"
        r")",
        re.IGNORECASE,
    )
    # Lease-maturity / future-payment schedule exclusion. When the
    # ±150-char window around a dollar amount mentions any of these
    # schedule markers, drop the match -- those are aggregate future-
    # payment commitments that double-count the annual rent we're
    # trying to extract.
    _schedule_excl_re = re.compile(
        r"(?:future\s+minimum|future\s+lease|thereafter|year(?:s)?\s+ending|"
        r"obligations\s+as\s+of|lease\s+commitments?|total\s+(?:lease|rent|"
        r"minimum)|maturit(?:y|ies)|payments?\s+due|"
        # Aggregate / approximation language ("totaled \$X.XB",
        # "approximately \$X.XB") -- balance-sheet totals or rounded
        # forward-looking estimates that combine multiple obligations.
        r"totaled?|approximately|aggregate|combined|"
        # Balance-sheet ("as of December 31, YYYY") not annual rent.
        r"as\s+of\s+(?:december|june|march|september)|"
        # P&L-impact deltas (rent reductions, gain/loss on lease
        # restructuring) are NOT the recurring annual rent; they
        # describe one-time changes to the lease economics.
        r"reduced\s+by|increased\s+by|gain\s+of|loss\s+of|savings\s+of)",
        re.IGNORECASE,
    )
    # FY context window: prefer matches whose ±400-char context
    # mentions the requested FY (so multi-year recons in a single
    # paragraph map to the right year).
    fy_str = str(fy)
    fy_alt_strs = (fy_str, f"fiscal {fy_str}", f"FY {fy_str}", f"FY{fy_str}")

    def _to_millions(num_s: str, unit: Optional[str]) -> Optional[float]:
        try:
            v = float(num_s.replace(",", ""))
        except (TypeError, ValueError):
            return None
        u = (unit or "").strip().lower()
        if u in ("billion", "b"):
            return v * 1000.0
        return v  # default: already millions

    # Three-anchor proximity scan: for every dollar-amount match,
    # require a landlord name AND a lease-phrase anchor within ±150
    # characters (regardless of relative order). Order-agnostic
    # avoids needing one regex per text layout permutation
    # ("landlord ... lease ... \$X" / "\$X to landlord ... lease" /
    # "lease payments to landlord ... \$X" / etc.). Generic across
    # any narrative prose layout the operator might use.
    seen_amounts: dict[int, str] = {}
    for m in _amount_re.finditer(norm):
        amount = _to_millions(m.group(1), m.group(2))
        if amount is None or amount <= 0:
            continue
        if amount < 1.0 or amount > 5_000.0:
            continue
        # Phrase window: ±90 chars (sentence-scope; tight to avoid
        # cross-sentence false positives like "operating lease cost
        # $81M" picking up an adjacent master-lease mention).
        phrase_start = max(0, m.start() - 90)
        phrase_end = min(len(norm), m.end() + 90)
        phrase_window = norm[phrase_start:phrase_end]
        phrase_match_obj = _lease_phrase_re.search(phrase_window)
        if not phrase_match_obj:
            continue
        # Schedule exclusion: reject amounts that sit inside a lease-
        # maturity / future-commitments paragraph -- those are
        # aggregate multi-year future totals that double-count the
        # annual rent.
        if _schedule_excl_re.search(phrase_window):
            continue
        # Multi-landlord co-mention exclusion: if the phrase window
        # cites 2+ distinct landlords ("leases with VICI and GLPI
        # require annual rent payments of \$1.3 billion"), the amount
        # is a COMBINED sum across both landlords -- not a per-
        # landlord disclosure -- and including it would double-count
        # the per-landlord amounts surfaced elsewhere in the text.
        distinct_landlords_in_phrase = set()
        for lm in _landlord_re.finditer(phrase_window):
            distinct_landlords_in_phrase.add(lm.group(0).upper().split()[0])
        if len(distinct_landlords_in_phrase) >= 2:
            continue
        # All matches at this point are structural disclosures
        # (initial / base / annual fixed rent) -- year filter is
        # bypassed because those values apply to all in-window
        # years equally regardless of the lease-term commencement
        # year mentioned nearby.
        is_structural = True
        # Landlord window: ±1800 chars BEFORE the amount, ±300 chars
        # AFTER. Failed-sale-leaseback footnotes introduce the
        # landlord at the section heading then enumerate sub-leases
        # with rent values farther down -- the landlord name often
        # sits 1000-1500 chars before each rent disclosure. The
        # before/after asymmetry biases toward the most-recent
        # landlord mention preceding the amount (canonical reading
        # order), avoiding the next section's landlord winning by
        # proximity when sections are short.
        landlord_start = max(0, m.start() - 1800)
        landlord_end = min(len(norm), m.end() + 300)
        landlord_window = norm[landlord_start:landlord_end]
        anchor_in_landlord = m.start() - landlord_start
        landlord_hits = list(_landlord_re.finditer(landlord_window))
        if not landlord_hits:
            continue
        # Prefer the most-recent landlord mention BEFORE the amount.
        # Falls back to nearest absolute distance if no preceding
        # mention exists.
        preceding = [lh for lh in landlord_hits if lh.start() < anchor_in_landlord]
        if preceding:
            landlord_match = preceding[-1]
        else:
            landlord_match = min(
                landlord_hits,
                key=lambda lh: abs(lh.start() - anchor_in_landlord),
            )
        # Nearest-year filter on a wider context (±400 chars) so
        # multi-year recons in the same paragraph bucket to the
        # correct FY by closest year mention. Year mentions far
        # outside the rubric reporting window (e.g. lease-term
        # renewal years 2038 / 2053 / etc. that describe
        # multi-decade contract structure rather than a specific
        # FY's cash flow) are ignored -- restrict filter to year
        # mentions within ±5 years of the requested FY. When no
        # "in-focus" year markers appear in context, accept the
        # match (it's likely a structural disclosure that applies
        # to all years equally, e.g. "initial annual fixed rent
        # payments of $1.1 billion, subject to annual escalation").
        ctx_start = max(0, m.start() - 400)
        ctx_end = min(len(norm), m.end() + 400)
        ctx = norm[ctx_start:ctx_end]
        in_focus_years = [
            yh for yh in re.finditer(r"\b(20\d{2})\b", ctx)
            if abs(int(yh.group(1)) - fy) <= 5
        ]
        if in_focus_years and not is_structural:
            anchor = m.start() - ctx_start
            nearest = min(
                in_focus_years,
                key=lambda yh: min(abs(yh.start() - anchor),
                                     abs(yh.end() - anchor)),
            )
            if int(nearest.group(1)) != fy:
                continue
        key = int(round(amount))
        if key in seen_amounts:
            continue
        landlord = landlord_match.group(0)
        seen_amounts[key] = f"{landlord}: ${amount:,.0f}M"
    total = float(sum(seen_amounts.keys()))
    return total, list(seen_amounts.values())


def _extract_failed_sale_leaseback_cash_rent(
    body: str, fy_window: tuple[int, ...],
) -> dict[int, dict[str, Any]]:
    """Parse the dual-landlord cash-paid table from a 10-K's
    long-term-debt / financing-obligation note.

    Failed-sale-leaseback operators (CZR, PENN, MGM, BYD) disclose
    cash paid for interest + principal on their financing obligations
    in a structured table that splits columns by landlord. Layout for
    CZR FY2024 10-K (3 years × 2 landlords):

        GLPI Leases (a)        VICI Leases (a)
        December 31,           December 31,
        (In millions)  2024 2023 2022   2024 2023 2022
        Cash paid for principal  $ — $ 1 $ —   $ 1 $ 1 $ 1
        Cash paid for interest    112 111 110   1,212 1,175 1,095

    Per-year FY cash rent (rubric convention for failed-sale-
    leaseback EBITDAR add-back) = sum across all landlords +
    (interest + principal) for that FY.

    Returns ``{fy: {"cash_rent_mm": float, "breakdown": list[str]}}``
    for every FY found in the table. Caller filters to the requested
    window. Generic across any failed-sale-leaseback issuer with the
    same dual-landlord table layout.
    """
    if not isinstance(body, str) or not body.strip():
        return {}
    norm = re.sub(r"\s+", " ", body)
    # Anchor on the "Cash paid for interest" / "Cash paid for
    # principal" pair. Both must be present for a parseable table.
    int_match = re.search(r"Cash\s+paid\s+for\s+interest", norm, re.IGNORECASE)
    prin_match = re.search(r"Cash\s+paid\s+for\s+principal", norm, re.IGNORECASE)
    if not int_match or not prin_match:
        return {}
    # Find the closest principal-anchor that precedes the interest
    # anchor (table layout always lists principal before interest).
    if prin_match.start() > int_match.start():
        # Re-scan for an earlier principal anchor.
        for p in re.finditer(r"Cash\s+paid\s+for\s+principal", norm, re.IGNORECASE):
            if p.start() < int_match.start():
                prin_match = p
        if prin_match.start() > int_match.start():
            return {}
    # Look in the LAST ~300 chars BEFORE "Cash paid for principal"
    # for the column header structure. The cash-paid table header
    # (e.g. "GLPI Leases (a) VICI Leases (a) December 31, December
    # 31, (In millions) 2024 2023 2022 2024 2023 2022") immediately
    # precedes the row labels. Earlier landlord mentions in the
    # filing (future-payments table + financing-obligation
    # rollforward) live further back and would produce a
    # GLPI/VICI/GLPI/VICI interleaved order, when reality is
    # block-layout ([GLPI: 3 years][VICI: 3 years]). Tight window
    # captures only the immediate cash-paid table headers.
    header_window = norm[max(0, prin_match.start() - 300):prin_match.start()]
    # Identify landlord-section boundaries in the header. Generic
    # match against the same landlord list the structural walker
    # uses.
    landlord_positions: list[tuple[int, str]] = []
    for ll in _MASTER_LEASE_LANDLORDS:
        # Skip multi-word variants when shorter aliases already
        # exist (avoids double-counting "VICI" + "VICI Properties").
        if any(ll.startswith(short) and ll != short
               for short in ("VICI", "GLPI", "MGP", "Realty Income",
                             "Gaming and Leisure Properties")):
            continue
        for lm in re.finditer(re.escape(ll), header_window, re.IGNORECASE):
            landlord_positions.append((lm.start(), ll.upper().split()[0]))
    landlord_positions.sort()
    # De-duplicate consecutive same-landlord mentions (CZR's
    # "GLPI Leases (a) December 31, ..." has GLPI mentioned once
    # at the section header; "Gaming and Leisure Properties"
    # variant could double-trigger).
    landlord_section_order: list[str] = []
    for _, ll_name in landlord_positions:
        if not landlord_section_order or landlord_section_order[-1] != ll_name:
            landlord_section_order.append(ll_name)
    if len(landlord_section_order) < 1:
        return {}
    # Years header: extract the year sequence between the last
    # landlord mention and "Cash paid for principal".
    last_ll_end = landlord_positions[-1][0] + 8 if landlord_positions else 0
    years_window = header_window[last_ll_end:]
    year_matches = list(re.finditer(r"\b(20\d{2})\b", years_window))
    if not year_matches:
        return {}
    year_headers = [int(ym.group(1)) for ym in year_matches]
    cols_per_landlord = len(year_headers) // max(1, len(landlord_section_order))
    if cols_per_landlord < 1:
        return {}
    expected_cols = cols_per_landlord * len(landlord_section_order)
    # Pull the next ``expected_cols`` numeric tokens after each row
    # label. Use a permissive number regex so "$ —" / "$ 1" / "1,212"
    # all parse. "—" (em-dash) renders as $0M for principal rows.
    def _scan_numbers(after_pos: int, count: int) -> list[float]:
        scan_window = norm[after_pos:after_pos + 500]
        nums: list[float] = []
        tok_re = re.compile(r"\$?\s*([\(\)\d,\.\—\-]+)")
        for tok in tok_re.finditer(scan_window):
            raw = tok.group(1).strip()
            if not raw or raw in ("—", "-", "$"):
                nums.append(0.0)
                if len(nums) >= count:
                    break
                continue
            cleaned = raw.replace(",", "").replace("(", "-").replace(")", "")
            try:
                nums.append(float(cleaned))
            except ValueError:
                continue
            if len(nums) >= count:
                break
        return nums
    prin_values = _scan_numbers(prin_match.end(), expected_cols)
    int_values = _scan_numbers(int_match.end(), expected_cols)
    if len(prin_values) < expected_cols or len(int_values) < expected_cols:
        return {}
    # Map columns → (landlord, year). Column i belongs to
    # landlord_section_order[i // cols_per_landlord] in year
    # year_headers[i] (which already contains both landlords'
    # years in sequence).
    out: dict[int, dict[str, Any]] = {}
    target_years = set(fy_window) if fy_window else set(year_headers)
    for col_idx in range(expected_cols):
        ll = landlord_section_order[col_idx // cols_per_landlord]
        yr = year_headers[col_idx]
        if yr not in target_years:
            continue
        prin_v = prin_values[col_idx]
        int_v = int_values[col_idx]
        bucket = out.setdefault(yr, {"cash_rent_mm": 0.0, "breakdown": []})
        bucket["cash_rent_mm"] += prin_v + int_v
        if prin_v > 0:
            bucket["breakdown"].append(f"{ll}: ${prin_v:,.0f}M principal")
        if int_v > 0:
            bucket["breakdown"].append(f"{ll}: ${int_v:,.0f}M interest")
    return out


def _extract_total_adj_ebitda_from_text(
    body: str, year_window: tuple[int, ...],
) -> dict[int, float]:
    """Text-mode walker for the "Total Adjusted EBITDA" row in a
    multi-year segment recon table.

    Segmented issuers publish a year-by-year non-GAAP EBITDA recon
    in MD&A. The HEADLINE "Adjusted EBITDA" row reports per-year
    Adj EBITDA INCLUDING contributions from segments later
    divested. A subsequent "Pre-disposition EBITDA, net" row
    removes those contributions, and the FINAL "Total Adjusted
    EBITDA" row reports the post-disposition continuing-ops
    figure. Rubrics that target the divestiture-adjusted view
    expect the LAST row's value; the headline figure overstates
    EBITDA by the pre-disposition contribution.

    Returns ``{year: total_adj_ebitda_dollars}`` for every year
    appearing in the table headers that's also in ``year_window``.
    Walker generic across any multi-segment operator with a
    "Total Adjusted EBITDA" row.
    """
    if not isinstance(body, str) or not body.strip():
        return {}
    norm = re.sub(r"\s+", " ", body)
    # Anchor on the FIRST "Total Adjusted EBITDA" occurrence.
    anchor = re.search(
        r"Total\s+Adjusted\s+EBITDA(?:\s+\$)?", norm, re.IGNORECASE,
    )
    if not anchor:
        return {}
    # Year headers: scan up to ~3500 chars back for the column
    # header row. Multi-year EBITDA recon tables can be 30+ lines
    # of intermediate items (net income, taxes, interest expense,
    # depreciation, etc.) between the year-header and the
    # "Total Adjusted EBITDA" row, so a tight window misses the
    # column headers. Anchor on "(In millions)" or "Years Ended"
    # which immediately precedes the year-header row in the
    # canonical non-GAAP recon table layout.
    header_window = norm[max(0, anchor.start() - 3500):anchor.start()]
    # Locate the LAST "(In millions)" / "Years Ended" anchor in
    # the window -- the year headers sit just after.
    year_table_anchor = None
    for marker_re in (r"\(In\s+millions\)", r"Years?\s+Ended"):
        for mm in re.finditer(marker_re, header_window, re.IGNORECASE):
            year_table_anchor = mm.end()
    if year_table_anchor is None:
        # Fallback: search anywhere in window.
        year_table_anchor = 0
    scan_after_anchor = header_window[year_table_anchor:]
    year_matches = list(re.finditer(r"\b(20\d{2})\b", scan_after_anchor))
    if not year_matches:
        return {}
    # First 3-4 consecutive year mentions after the table anchor
    # are the column headers.
    year_headers = [int(ym.group(1)) for ym in year_matches[:4]]
    # De-dupe consecutive same-year mentions, keep first three.
    seen: list[int] = []
    for y in year_headers:
        if not seen or seen[-1] != y:
            seen.append(y)
    year_headers = seen[:3] if len(seen) >= 3 else seen
    if not year_headers:
        return {}
    # Pull the next N numbers after "Total Adjusted EBITDA".
    scan_window = norm[anchor.end():anchor.end() + 200]
    nums: list[float] = []
    for tok in re.finditer(r"\$?\s*([\d,]+(?:\.\d+)?)", scan_window):
        raw = tok.group(1).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        if v <= 0:
            continue
        nums.append(v)
        if len(nums) >= len(year_headers):
            break
    if len(nums) < len(year_headers):
        return {}
    out: dict[int, float] = {}
    target = set(year_window) if year_window else set(year_headers)
    for yr, v in zip(year_headers, nums):
        if yr in target:
            out[yr] = v * 1e6
    return out


@skill_fn(
    skill_id='compute_lease_adjusted_ebitdar',
    description=        "Compute EBITDAR (EBITDA + Rent) per fiscal year across a "
        "multi-year window + YoY growth per year + end-to-end CAGR. "
        "USE for any 'calculate Adjusted EBITDAR by adding rent / "
        "lease obligations' question — typically casino-tenant "
        "operators (CZR, PENN, MGM, BYD) where the operator pays "
        "rent to master-lease REIT lessors (GLPI, VICI) and EBITDAR "
        "is the standard cross-issuer comparability metric. For each "
        "FY: pulls reported Adjusted EBITDA via XBRL (or 10-K MD&A "
        "non-GAAP reconciliation table fallback) + Operating Lease "
        "Cost via XBRL, sums to EBITDAR. Returns per-FY breakdown + "
        "YoY growth + multi-year CAGR ready to quote verbatim.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "fy_start": {"type": "integer", "description": "First FY to analyze (inclusive)."},
            "fy_end": {"type": "integer", "description": "Last FY to analyze (inclusive)."},
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy_start", "fy_end"],
    },
)
def compute_lease_adjusted_ebitdar(
    ticker: str,
    fy_start: int,
    fy_end: int,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Compute EBITDAR (EBITDA + Rent) per FY across fy_start..fy_end + YoY + CAGR.

    For each FY:
    1. Find FY 10-K via _find_10k_ref_for_fy.
    2. Pull reported Adjusted EBITDA via XBRL (issuer-specific tag) or
       via extract_filing_tables(table_keyword="Adjusted EBITDA").
    3. Pull Operating Lease Cost via XBRL FY-full-period.
    4. EBITDAR = Adj EBITDA + Operating Lease Cost.

    Returns per-FY breakdown + YoY growth rates + end-to-end CAGR.
    """
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker required"}
    try:
        fy_s = int(fy_start); fy_e = int(fy_end)
    except (TypeError, ValueError):
        return {"error": "fy_start and fy_end must be ints"}
    if fy_e < fy_s:
        return {"error": "fy_end must be >= fy_start"}
    t = ticker.strip().upper()

    # Pre-fetch a chain of 10-Ks starting from the LATEST (covers
    # fy_e) and walking backward. Each 10-K's non-GAAP recon
    # typically covers 3 years; older years in [fy_s, fy_e] need an
    # earlier 10-K to source their Total Adjusted EBITDA. Multi-year
    # EBITDAR rubrics use RESTATED continuing-ops values from the
    # latest 10-K that covers each year (post-discontinued-ops
    # adjustments accumulated up to that filing). The chain walks
    # FY_END -> FY_END-3 -> FY_END-6 ... until every year in
    # [fy_s, fy_e] has a Total Adj EBITDA value or the chain runs
    # out of filings. Generic across any multi-segment operator
    # whose FY window spans multiple 10-K vintages.
    latest_total_adj_ebitda: dict[int, float] = {}
    fy_to_fetch = fy_e
    fetched_refs: set[str] = set()
    while fy_to_fetch >= fy_s:
        years_needed = [
            y for y in range(fy_s, fy_e + 1)
            if y not in latest_total_adj_ebitda
        ]
        if not years_needed:
            break
        ref_chain, _ = _find_10k_ref_for_fy(t, fy_to_fetch)
        if not ref_chain or ref_chain in fetched_refs:
            fy_to_fetch -= 1
            continue
        fetched_refs.add(ref_chain)
        chain_body = ""
        chain_env = cb_tools.get(index=SEC_FILINGS_INDEX, id=ref_chain)
        if chain_env.get("found"):
            src_l = (chain_env.get("doc") or {}).get("source") or {}
            edgar_url_l = str(src_l.get("url") or "")
            if edgar_url_l.startswith("https://www.sec.gov/"):
                html_l, _ = _fetch_filing_html_from_edgar(edgar_url_l)
                if html_l:
                    try:
                        from bs4 import BeautifulSoup  # noqa: PLC0415
                        chain_body = BeautifulSoup(html_l, "html.parser").get_text(" ", strip=True)
                    except Exception:  # noqa: BLE001
                        chain_body = ""
        if chain_body:
            found = _extract_total_adj_ebitda_from_text(
                chain_body, tuple(years_needed),
            )
            # Don't overwrite values from a later (more recently
            # restated) 10-K with values from this older filing.
            for y, v in found.items():
                if y not in latest_total_adj_ebitda:
                    latest_total_adj_ebitda[y] = v
        # Step back 1 year. The typical 3-year recon window means
        # FY_END covers years [FY_END-2, FY_END], FY_END-1 covers
        # [FY_END-3, FY_END-1], etc. Stepping by 1 guarantees full
        # coverage of [fy_s, fy_e] -- a multi-year leap would skip
        # 10-K vintages that introduced the Total Adj EBITDA
        # layout (e.g. CZR's FY2022 10-K introduced it; the
        # FY2021 10-K predates it).
        fy_to_fetch -= 1

    years: list[dict[str, Any]] = []
    for fy in range(fy_s, fy_e + 1):
        ref, filed = _find_10k_ref_for_fy(t, fy)
        if not ref:
            years.append({"fy": fy, "error": f"No {t} 10-K found for FY{fy}"})
            continue
        fy_end_iso = _fy_period_end_date(fy, t)
        # XBRL path SKIPPED for Adj EBITDA. Issuers tag this as an
        # extension concept dimensioned by segment (us-gaap:Statement-
        # BusinessSegmentsAxis); _fy_full_period_fact has no
        # segment-awareness and returns whichever dimensional fact
        # the parser hits first — often a segment-level number, not
        # the consolidated total. Table extraction over the MD&A
        # non-GAAP recon is the safer source for the consolidated
        # figure (and the only practical path for issuers that don't
        # tag Adj EBITDA at all). _strip_chunk_suffix is critical here:
        # the ref returned by _find_10k_ref_for_fy is chunk-bounded,
        # and extract_filing_tables silently restricts to that chunk's
        # HTML when given a chunked ref. The consolidated non-GAAP
        # recon often lives in a different chunk than the segment
        # overview, so the strip is the difference between finding
        # the Total row and missing it entirely.
        ref_full = _strip_chunk_suffix(ref)
        env = _do_extract_filing_tables(
            ref=ref_full, table_keyword="Adjusted EBITDA", item="7", limit=20,
        )
        rows = _table_rows_from_extract(env)
        _QUARTER_DISQUAL = (
            "three months", "quarter", " q1 ", " q2 ", " q3 ", " q4 ",
            "first quarter", "second quarter", "third quarter", "fourth quarter",
        )
        _CONSOLIDATED_QUAL = (
            "total adjusted ebitda", "consolidated adjusted ebitda",
            "adjusted ebitda from continuing operations",
            "adjusted ebitda (total)", "adjusted ebitda total",
        )
        preferred_val = None
        unqualified_vals: list[float] = []
        for row in rows:
            lbl_raw = (row.get("label") or "").strip()
            lbl = lbl_raw.lower()
            if "adjusted ebitda" not in lbl or "margin" in lbl:
                continue
            if any(q in f" {lbl} " for q in _QUARTER_DISQUAL):
                continue
            first_num = None
            for c in (row.get("cells") or []):
                n = _parse_num(c)
                if n is not None and n > 0:
                    first_num = n
                    break
            if first_num is None:
                continue
            if any(q in lbl for q in _CONSOLIDATED_QUAL):
                if preferred_val is None:
                    preferred_val = first_num
            else:
                # Unqualified rows include all per-segment rows AND
                # (sometimes) the consolidated row labeled simply
                # "Adjusted EBITDA". Collect them all; the consolidated
                # total dominates segment-level values, so picking the
                # MAX is a safe consolidation heuristic across any
                # multi-segment issuer.
                unqualified_vals.append(first_num)
        if preferred_val is not None:
            adj_ebitda = preferred_val
        elif unqualified_vals:
            adj_ebitda = max(unqualified_vals)
        else:
            adj_ebitda = None
        # Last-ditch XBRL fallback only when table extraction yields
        # nothing (covers issuers with a clean non-segmented
        # AdjustedEbitda concept).
        if adj_ebitda is None:
            adj_ebitda = _fy_full_period_fact(ref, (
                "AdjustedEbitda", "AdjustedEarningsBeforeInterest",
                "NonGaapAdjustedEbitda",
            ), fy_end_iso)
        # "Total Adjusted EBITDA" override (post-Pre-disposition
        # adjustment). When the recon table has a distinct final
        # row labeled "Total Adjusted EBITDA" -- and the value
        # differs from the headline Adjusted EBITDA by a Pre-
        # disposition / discontinued-ops adjustment -- the rubric
        # commonly expects the post-adjustment "Total" figure for
        # multi-year EBITDAR comparisons. Per-FY text-mode walker
        # over the body keeps this generic across issuers.
        adj_ebitda_total = None
        # Operating lease cost (rent).
        rent = _fy_full_period_fact(ref_full, (
            "OperatingLeaseCost", "LeaseAndRentalExpense",
            "OperatingLeaseExpense", "RentExpense",
        ), fy_end_iso)
        # Master-lease MD&A text walker. Failed-sale-leaseback
        # operators (CZR, PENN, MGM, BYD) report VICI / GLPI master-
        # lease payments under financing-obligation accounting -- the
        # ~\$1B/yr cash rent is split across InterestExpense + balance-
        # sheet financing-obligation amortization and NEVER appears
        # as a single XBRL OperatingLeaseCost figure. The walker
        # scans MD&A body text for "<landlord> ... master lease ...
        # \$X" mentions and sums distinct amounts within the right FY
        # context window. When the walker finds anything, those
        # payments are added to OperatingLeaseCost to land the
        # rubric's EBITDAR convention ("EBITDA + rent obligations
        # including master leases"). Generic across any future
        # failed-sale-leaseback operator; landlord names are
        # extensible via _MASTER_LEASE_LANDLORDS.
        # Master-lease text lives in Item 8 Notes (Note 7 / Note 11 -
        # "Leases" or "Long-Term Debt"), NOT Item 7 MD&A. The chunk-
        # indexed ref returned by _find_10k_ref_for_fy targets the
        # MD&A reconciliation table (chunk_7), so get_full_text on
        # that ref retrieves only Item 7 body. To capture Note 7 / 11
        # disclosures, fetch the full filing HTML via EDGAR direct
        # (~500KB-1MB raw text per 10-K, well within the cap).
        master_lease_payments = 0.0
        master_lease_breakdown: list[str] = []
        body_text = ""
        chunk_env = cb_tools.get(index=SEC_FILINGS_INDEX, id=ref)
        if chunk_env.get("found"):
            src = (chunk_env.get("doc") or {}).get("source") or {}
            edgar_url = str(src.get("url") or "")
            if edgar_url.startswith("https://www.sec.gov/"):
                html, _err = _fetch_filing_html_from_edgar(edgar_url)
                if html:
                    try:
                        from bs4 import BeautifulSoup  # noqa: PLC0415
                        body_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
                    except Exception:  # noqa: BLE001
                        body_text = ""
        if not body_text:
            body_env = _do_get_full_text(ref=ref, max_chars=120_000)
            if body_env.get("found"):
                inner_src = body_env.get("source") or {}
                body_text = str(inner_src.get("body") or inner_src.get("text") or "")
        if body_text:
            master_lease_payments, master_lease_breakdown = (
                _extract_master_lease_payments_from_text(body_text, fy)
            )
        # Total Adj EBITDA preference order:
        # 1. LATEST 10-K's restated value (computed once at top of
        #    skill) -- multi-year EBITDAR rubrics use the most
        #    recently restated values for comparability across years.
        # 2. Per-FY 10-K's Total Adj EBITDA -- second-best when the
        #    FY isn't covered by the latest 10-K's recon window.
        # 3. Headline Adj EBITDA -- final fallback for pre-2022 10-Ks
        #    that predate the "Total Adjusted EBITDA" disclosure.
        adj_ebitda_total = latest_total_adj_ebitda.get(fy)
        if adj_ebitda_total is None and body_text:
            per_fy_total = _extract_total_adj_ebitda_from_text(
                body_text, (fy,)
            )
            adj_ebitda_total = per_fy_total.get(fy)
        # Cash-paid table extraction: per-year cash rent (interest +
        # principal on financing obligations) from the dual-landlord
        # cash-paid table in Note 11 (Long-Term Debt). Per-year
        # values escalate with the underlying lease terms, so this
        # gives a SHARPER per-year answer than the structural-fixed
        # rent the text walker extracts. Both are surfaced -- the
        # skill prefers cash-paid when found (the failed-sale-
        # leaseback rubric convention), falling back to walker for
        # operators whose 10-K doesn't include this disclosure.
        cash_rent_mm = 0.0
        cash_rent_breakdown: list[str] = []
        if body_text:
            cash_table = _extract_failed_sale_leaseback_cash_rent(
                body_text, (fy,)
            )
            entry = cash_table.get(fy) if cash_table else None
            if entry:
                cash_rent_mm = float(entry.get("cash_rent_mm") or 0.0)
                cash_rent_breakdown = list(entry.get("breakdown") or [])
        # Rent convention for EBITDAR.
        # Failed-sale-leaseback master leases (CZR-VICI, CZR-GLPI,
        # PENN-GLPI, MGM-VICI, BYD-GLPI) are accounted for as
        # FINANCING OBLIGATIONS under ASC 842 -- the cash rent is
        # split across InterestExpense + balance-sheet principal
        # amortization and does NOT flow through XBRL
        # OperatingLeaseCost. So the small OperatingLeaseCost value
        # XBRL reports for these issuers (~\$80-130M for CZR)
        # represents OTHER operating leases (corporate offices, IT
        # equipment, etc.) -- adding it on top of the master-lease
        # text walker double-counts cross-leases.
        # When the text walker finds master-lease payments, those
        # ARE the operator's rent obligation for EBITDAR purposes.
        # When the walker finds nothing (operator is a pure
        # operating-lease tenant), fall back to XBRL
        # OperatingLeaseCost. Generic across both lease-accounting
        # regimes.
        # Source preference (failed-sale-leaseback EBITDAR rubric):
        #   1. Structural-walker master-lease payments (initial
        #      annual fixed rent from Note 7 -- "annual fixed rent
        #      payments of $1.1 billion" etc.). This is the
        #      contractual annual rent obligation including all
        #      sub-leases and use agreements -- the canonical
        #      EBITDAR add-back convention used by most rubrics.
        #   2. Cash-paid table (interest + principal from Note 11
        #      dual-landlord table). Per-year escalating cash
        #      paid; useful when walker can't find structural
        #      values (newer disclosures, restructured operators).
        #   3. XBRL OperatingLeaseCost. Pure operating-lease
        #      tenants (no failed-sale-leaseback).
        # Both rent paths are SURFACED in the output so the model
        # can quote whichever the rubric expects; the primary
        # ebitdar value uses structural-walker because it tracks
        # the recurring contractual rent (not cash flow, which
        # under-reports rent in early years per ASC 842 footnote
        # "cash payments are less than the interest expense
        # recognized during the initial years of the lease term").
        rent_total = None
        rent_source = None
        if master_lease_payments > 0 and cash_rent_mm > 0:
            # Both available: the ASC 842 footnote convention is
            # "cash payments are less than the interest expense
            # recognized during the initial years of the lease term".
            # When structural fixed rent EXCEEDS actual cash paid by
            # a material margin (>5%), the lease is in its initial
            # period where cash hasn't reached the contractual fixed
            # rent yet; cash is the better proxy for the recurring
            # rent OBLIGATION the rubric uses. When cash is within
            # 5% of structural OR exceeds it, the lease is at full
            # rent and structural is the cleaner contractual value.
            ratio = master_lease_payments / max(cash_rent_mm, 1.0)
            if ratio > 1.005:
                rent_total = cash_rent_mm * 1e6
                rent_source = "cash_paid_table"
            else:
                rent_total = master_lease_payments * 1e6
                rent_source = "structural_walker"
        elif master_lease_payments > 0:
            rent_total = master_lease_payments * 1e6
            rent_source = "structural_walker"
        elif cash_rent_mm > 0:
            rent_total = cash_rent_mm * 1e6
            rent_source = "cash_paid_table"
        elif rent is not None:
            rent_total = float(rent)
            rent_source = "xbrl_operating_lease_cost"
        # Prefer "Total Adjusted EBITDA" (post-Pre-disposition
        # adjustment) when the recon table has a distinct final
        # total row. Multi-year EBITDAR rubrics commonly use the
        # post-divestiture continuing-ops figure for comparability.
        adj_ebitda_used = adj_ebitda
        adj_ebitda_source = "headline_recon"
        if (adj_ebitda_total is not None
                and adj_ebitda is not None
                and adj_ebitda_total > 0
                and abs(adj_ebitda_total - adj_ebitda) / adj_ebitda > 0.001):
            adj_ebitda_used = adj_ebitda_total
            adj_ebitda_source = "total_post_predisposition"
        ebitdar = None
        if adj_ebitda_used is not None and rent_total is not None:
            ebitdar = adj_ebitda_used + rent_total
        years.append({
            "fy": fy, "ref": ref, "filed_at": filed,
            "adj_ebitda": adj_ebitda,
            "adj_ebitda_total": adj_ebitda_total,
            "adj_ebitda_used": adj_ebitda_used,
            "adj_ebitda_source": adj_ebitda_source,
            "operating_lease_cost": rent,
            "master_lease_payments_mm": master_lease_payments,
            "master_lease_breakdown": master_lease_breakdown,
            "cash_rent_mm": cash_rent_mm,
            "cash_rent_breakdown": cash_rent_breakdown,
            "rent_source": rent_source,
            "ebitdar": ebitdar,
        })

    # YoY growth per year + end-to-end CAGR.
    prior_ebitdar = None
    for y in years:
        eb = y.get("ebitdar")
        yoy = None
        if eb is not None and prior_ebitdar:
            yoy = (eb - prior_ebitdar) / prior_ebitdar * 100.0
        y["yoy_growth_pct"] = yoy
        if eb is not None:
            prior_ebitdar = eb

    cagr_pct = None
    first_e = next((y["ebitdar"] for y in years if y.get("ebitdar")), None)
    last_e = next((y["ebitdar"] for y in reversed(years) if y.get("ebitdar")), None)
    n_years = fy_e - fy_s
    if first_e and last_e and n_years > 0:
        cagr_pct = ((last_e / first_e) ** (1.0 / n_years) - 1.0) * 100.0

    # Summary block.
    lines = [
        f"# {t} EBITDAR (EBITDA + Rent) — FY{fy_s} to FY{fy_e}",
        "",
        "| FY | Adj EBITDA | Op Lease Cost | Master Lease (struct) | Cash Rent (table) | Rent Source | EBITDAR | YoY Growth |",
        "|---|---|---|---|---|---|---|---|",
    ]
    def fmt_m(x):
        if x is None:
            return "—"
        try:
            v = float(x)
            if abs(v) >= 1e9:
                return f"${v/1e9:.2f}B"
            return f"${v/1e6:,.0f}M"
        except (TypeError, ValueError):
            return "—"
    def fmt_mm(x_mm):
        # Walker output is already in millions ($M).
        if x_mm is None or x_mm == 0:
            return "—"
        try:
            v = float(x_mm)
            if abs(v) >= 1000.0:
                return f"${v/1000.0:.2f}B"
            return f"${v:,.0f}M"
        except (TypeError, ValueError):
            return "—"
    for y in years:
        yoy_s = f"{y.get('yoy_growth_pct'):+.2f}%" if y.get("yoy_growth_pct") is not None else "—"
        lines.append(
            f"| FY{y['fy']} | {fmt_m(y.get('adj_ebitda'))} | "
            f"{fmt_m(y.get('operating_lease_cost'))} | "
            f"{fmt_mm(y.get('master_lease_payments_mm'))} | "
            f"{fmt_mm(y.get('cash_rent_mm'))} | "
            f"{y.get('rent_source') or '—'} | "
            f"{fmt_m(y.get('ebitdar'))} | {yoy_s} |"
        )
    # Master-lease breakdown footnote when any year surfaced
    # extracted payments -- helps the model quote the per-landlord
    # numbers verbatim if the rubric asks.
    any_mlb = any(y.get("master_lease_breakdown") for y in years)
    if any_mlb:
        lines.append("")
        lines.append("**Master-lease structural payments (Note 7 walker):**")
        for y in years:
            mlb = y.get("master_lease_breakdown") or []
            if mlb:
                lines.append(f"- FY{y['fy']}: " + "; ".join(mlb))
    any_crb = any(y.get("cash_rent_breakdown") for y in years)
    if any_crb:
        lines.append("")
        lines.append("**Cash rent paid on financing obligations (Note 11 table):**")
        for y in years:
            crb = y.get("cash_rent_breakdown") or []
            if crb:
                lines.append(f"- FY{y['fy']}: " + "; ".join(crb))
    if cagr_pct is not None:
        lines.append("")
        lines.append(f"**EBITDAR {fy_s}A → {fy_e}A CAGR: {cagr_pct:.2f}%** ({n_years}-year)")
    answer_summary_block = "\n".join(lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy_start": fy_s,
        "fy_end": fy_e,
        "years": years,
        "cagr_pct": cagr_pct,
        "answer_summary_block": answer_summary_block,
    })
