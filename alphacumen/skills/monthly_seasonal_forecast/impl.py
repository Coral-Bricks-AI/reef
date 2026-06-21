# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``monthly_seasonal_forecast`` skill impl.

Hosts 2 ``@skill_fn``-registered callables for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from reef.skill_fn import skill_fn
from alphacumen.tools import (
    _BIND_AS_PARAM_SCHEMA,
    _MONTHLY_REVENUE_PATTERNS,
    _MONTH_NAMES,
    _apply_binding,
    _fetch_filing_html_from_edgar,
    _list_sec_filings,
    _month_name_to_num,
)


@skill_fn(
    skill_id='monthly_seasonal_forecast',
    description=        "Format a quarterly seasonal-revenue forecast into canonical "
        "rubric-shaped atom strings. Use for issuers that publish "
        "monthly revenue as 6-K filings (TSM, UMC, ASE, certain "
        "Korean/Japanese filers) and a question asks 'assuming "
        "normal [Month] seasonality, will X beat or miss Q[N] "
        "guidance'. Model fetches the relevant 6-Ks (Q[N-1] earnings "
        "6-K for the USD revenue guidance + history-year monthly "
        "revenue 6-Ks for the prior_month→target_month growth rates "
        "+ current-year prior_month 6-K for the most recent monthly "
        "actual and YTD cumulative), then passes the extracted values "
        "as args. Tool runs the seasonal math (average growth rate "
        "applied to most recent monthly actual, summed with YTD, "
        "compared to guidance midpoint) and formats 10 canonical atom "
        "strings ready to drop into answer_summary. Generalizes to any "
        "issuer + month pair (TSM Feb→Mar for Q1, but also UMC, JD, "
        "etc.; quarter-end month is whichever month you're forecasting).",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Issuer ticker, e.g. 'TSM'."},
            "target_year": {"type": "integer", "description": "Year being forecast, e.g. 2025."},
            "target_quarter": {"type": "integer", "description": "Quarter being forecast (1-4)."},
            "prior_month_name": {
                "type": "string",
                "description": "Most recent month with published revenue, e.g. 'February'.",
            },
            "target_month_name": {
                "type": "string",
                "description": "Month being forecast, e.g. 'March'.",
            },
            "guidance_usd_low": {
                "type": "number",
                "description": "Quarter guidance low end, in USD BILLIONS (e.g. 25.0).",
            },
            "guidance_usd_high": {
                "type": "number",
                "description": "Quarter guidance high end, in USD billions (e.g. 25.8).",
            },
            "fx_local_per_usd": {
                "type": "number",
                "description": (
                    "FX rate the issuer's own outlook uses for the guidance "
                    "(NOT spot). E.g. TSM FY2024 outlook used 32.8 NT$ per USD."
                ),
            },
            "history_growth_rates_pct": {
                "type": "array",
                "items": {"type": "number"},
                "description": (
                    "SIGNED prior_month→target_month growth rates in percentage "
                    "points for prior N years (e.g. [17.0, -10.9, 7.5] for "
                    "Y-3=2022 +17%, Y-2=2023 -10.9% (decrease), Y-1=2024 +7.5%)."
                ),
            },
            "history_years": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "Years matching history_growth_rates_pct entries (e.g. "
                    "[2022, 2023, 2024])."
                ),
            },
            "prior_month_actual_local": {
                "type": "number",
                "description": (
                    "Most recent month's actual revenue in local-currency "
                    "MILLIONS (e.g. 260009 for NT$260.01 billion)."
                ),
            },
            "ytd_cumulative_local": {
                "type": "number",
                "description": (
                    "Year-to-date cumulative revenue in local-currency "
                    "MILLIONS (Jan + Feb + ... through prior_month)."
                ),
            },
            "local_currency_symbol": {
                "type": "string",
                "default": "NT$",
                "description": "Symbol for the local currency (e.g. 'NT$', 'JPY', 'KRW').",
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": [
            "ticker", "target_year", "target_quarter",
            "prior_month_name", "target_month_name",
            "guidance_usd_low", "guidance_usd_high", "fx_local_per_usd",
            "history_growth_rates_pct", "history_years",
            "prior_month_actual_local", "ytd_cumulative_local",
        ],
    },
)
def format_seasonal_forecast(
    ticker: str,
    target_year: int,
    target_quarter: int,
    prior_month_name: str,
    target_month_name: str,
    guidance_usd_low: float,
    guidance_usd_high: float,
    fx_local_per_usd: float,
    history_growth_rates_pct: list[float],
    history_years: list[int],
    prior_month_actual_local: float,
    ytd_cumulative_local: float,
    local_currency_symbol: str = "NT$",
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Format a quarterly seasonality forecast into canonical atom strings.

    Args:
        ticker: Issuer ticker, e.g. "TSM".
        target_year: Year being forecast, e.g. 2025.
        target_quarter: 1-4.
        prior_month_name: e.g. "February" (the most recent month with
            published actual revenue).
        target_month_name: e.g. "March" (the month being forecast).
        guidance_usd_low, guidance_usd_high: Quarter guidance in
            USD BILLIONS (e.g. 25.0, 25.8).
        fx_local_per_usd: FX rate the issuer's outlook uses (e.g.
            32.8 NT$ per USD).
        history_growth_rates_pct: List of SIGNED prior_month →
            target_month growth rates (in percentage points) for the
            prior N years (e.g. [17.0, -10.9, 7.5]).
        history_years: List of years matching history_growth_rates_pct
            (e.g. [2022, 2023, 2024]).
        prior_month_actual_local: Most recent month's actual revenue
            in local-currency millions (e.g. 260009 for NT$260.01B).
        ytd_cumulative_local: Year-to-date cumulative revenue in
            local-currency millions (e.g. 553301 for Jan+Feb 2025).
        local_currency_symbol: Display symbol for local currency
            (default "NT$").

    Returns dict with `formatted_atoms` list and an
    `answer_summary_block` pre-joined with `- ` markers.
    """
    if not ticker or not isinstance(ticker, str):
        return {"error": "ticker is required"}
    try:
        target_year = int(target_year)
        target_quarter = int(target_quarter)
        guidance_usd_low = float(guidance_usd_low)
        guidance_usd_high = float(guidance_usd_high)
        fx_local_per_usd = float(fx_local_per_usd)
        prior_month_actual_local = float(prior_month_actual_local)
        ytd_cumulative_local = float(ytd_cumulative_local)
    except (TypeError, ValueError) as exc:
        return {"error": f"numeric inputs must be coercible: {exc!s}"}

    if not isinstance(history_growth_rates_pct, list) or not isinstance(history_years, list):
        return {"error": "history_growth_rates_pct and history_years must be lists"}
    if len(history_growth_rates_pct) != len(history_years):
        return {"error": "history_growth_rates_pct and history_years must be the same length"}
    if not history_growth_rates_pct:
        return {"error": "must supply at least one historical growth rate"}

    try:
        rates_pct = [float(r) for r in history_growth_rates_pct]
        years = [int(y) for y in history_years]
    except (TypeError, ValueError) as exc:
        return {"error": f"history list values must be numeric: {exc!s}"}

    # Compute.
    avg_growth_pct = sum(rates_pct) / len(rates_pct)
    avg_growth_decimal = avg_growth_pct / 100.0
    guidance_usd_mid = (guidance_usd_low + guidance_usd_high) / 2.0
    # NT$ guidance in millions: USD billion * 1000 = USD million; * fx → local million
    guidance_local_m = guidance_usd_mid * 1000.0 * fx_local_per_usd
    target_month_estimate_local = prior_month_actual_local * (1.0 + avg_growth_decimal)
    quarter_estimate_local = ytd_cumulative_local + target_month_estimate_local
    pct_beat_miss = (
        (quarter_estimate_local - guidance_local_m) / guidance_local_m * 100.0
        if guidance_local_m else 0.0
    )
    beat_or_miss = "Beat" if pct_beat_miss > 0 else "Miss" if pct_beat_miss < 0 else "In-Line"

    # Format.
    q = target_quarter
    y = target_year
    ccy = local_currency_symbol
    atoms: list[str] = []

    atoms.append(
        f"Q{q} {y} guidance (USD): ${guidance_usd_low:g}B to ${guidance_usd_high:g}B"
    )
    atoms.append(
        f"Q{q} {y} guidance ({ccy} at {fx_local_per_usd:g}): {guidance_local_m:,.0f}"
    )
    for yr, rate in zip(years, rates_pct):
        atoms.append(
            f"{prior_month_name} to {target_month_name} revenue growth rate {yr}: {rate:.1f}%"
        )
    atoms.append(
        f"Average {prior_month_name} to {target_month_name} growth rate: {avg_growth_pct:.1f}%"
    )
    atoms.append(
        f"{prior_month_name} {y} revenue ({ccy}): {prior_month_actual_local:,.0f}"
    )
    atoms.append(
        f"{target_month_name} {y} revenue (estimate, {ccy}): {target_month_estimate_local:,.0f}"
    )
    atoms.append(
        f"Q{q} {y} estimate: {quarter_estimate_local:,.0f}"
    )
    atoms.append(
        f"Q{q} {y}: {pct_beat_miss:+.1f}% {beat_or_miss}"
    )

    return _apply_binding(bind_as, {
        "ticker": ticker.strip().upper(),
        "target_year": target_year,
        "target_quarter": target_quarter,
        "inputs": {
            "guidance_usd_low": guidance_usd_low,
            "guidance_usd_high": guidance_usd_high,
            "fx_local_per_usd": fx_local_per_usd,
            "history_growth_rates_pct": rates_pct,
            "history_years": years,
            "prior_month_actual_local": prior_month_actual_local,
            "ytd_cumulative_local": ytd_cumulative_local,
        },
        "derived": {
            "avg_growth_pct": round(avg_growth_pct, 4),
            "guidance_local_m": round(guidance_local_m, 2),
            "target_month_estimate_local": round(target_month_estimate_local, 2),
            "quarter_estimate_local": round(quarter_estimate_local, 2),
            "pct_beat_miss": round(pct_beat_miss, 4),
            "verdict": beat_or_miss,
        },
        "answer": {
            "formatted_atoms": atoms,
            "answer_summary_block": "\n".join(f"- {a}" for a in atoms),
        },
    })


@skill_fn(
    skill_id='monthly_seasonal_forecast',
    description=        "Pull a foreign-private issuer's monthly revenue series across "
        "a multi-month window directly from SEC EDGAR via sec-api.io's "
        "filings endpoint, bypassing the local BM25 corpus. USE THIS "
        "for any monthly-seasonality question on an FPI that publishes "
        "monthly revenue as 6-K press releases (TSM, UMC, ASE, JD, "
        "etc.) — the local sec_filings_chunked corpus typically does not "
        "have these filings backfilled. Internally chains: (1) sec-api "
        "filings list for ticker + formType:6-K + filed_at window, "
        "(2) EDGAR direct HTML fetch of each filing's primary document, "
        "(3) regex extraction of the canonical 'Net revenue for [Month] "
        "[Year] was approximately [Currency][N] [million|billion]' "
        "string. Returns a chronologically ordered list of monthly "
        "revenue records (with currency, local-currency-millions "
        "value, source_ref) plus pre-computed month-over-month growth "
        "rates and a pre-composed `answer_summary_block`. Quote that "
        "block verbatim, then feed the extracted values into "
        "`format_seasonal_forecast` for the final canonical atoms.",
    parameters=               {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": (
                    "Foreign-private issuer ticker, e.g. one that "
                    "publishes monthly revenue as 6-K press releases. "
                    "Case-insensitive."
                ),
            },
            "fy_start_month": {
                "type": "string",
                "description": (
                    "First calendar month of the window in 'YYYY-MM' "
                    "form (e.g. '2022-01'). Filings filed in or before "
                    "this month are excluded; revenue records before "
                    "this month are dropped from the output."
                ),
            },
            "fy_end_month": {
                "type": "string",
                "description": (
                    "Last calendar month of the window in 'YYYY-MM' "
                    "form (e.g. '2025-03'). The tool widens the SEC "
                    "filing search by +1 month to catch press releases "
                    "for fy_end_month that are filed the following "
                    "month (typical FPI pattern)."
                ),
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["ticker", "fy_start_month", "fy_end_month"],
    },
)
def fetch_foreign_monthly_revenue(
    ticker: str,
    fy_start_month: str,
    fy_end_month: str,
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch monthly revenue 6-K filings for ``ticker`` between
    ``fy_start_month`` and ``fy_end_month`` (each "YYYY-MM") via
    sec-api.io filings endpoint + EDGAR direct fetch, and regex-extract
    each filing's reported monthly revenue value.

    Returns a chronologically ordered list of monthly revenue records
    plus pre-computed month-over-month growth rates and a canonical
    answer-summary block ready to quote.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return {"error": "ticker required"}

    def _parse_ym(s: str) -> Optional[tuple[int, int]]:
        s = (s or "").strip()
        if not s:
            return None
        # Accept YYYY-MM or YYYY-MM-DD.
        parts = s[:7].split("-")
        if len(parts) != 2:
            return None
        try:
            y, m = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if not (1 <= m <= 12):
            return None
        return y, m

    start_ym = _parse_ym(fy_start_month)
    end_ym = _parse_ym(fy_end_month)
    if not start_ym or not end_ym:
        return {"error": f"fy_start_month/fy_end_month must be YYYY-MM; got {fy_start_month!r}, {fy_end_month!r}"}

    # 6-K filings for a month are typically filed in the FOLLOWING
    # month (e.g. February revenue press-released ~March 8). Widen the
    # window by +1 month on the upper bound to catch the trailing
    # filing for fy_end_month.
    filed_gte = f"{start_ym[0]:04d}-{start_ym[1]:02d}-01"
    end_y, end_m = end_ym
    next_y, next_m = (end_y + 1, 1) if end_m == 12 else (end_y, end_m + 1)
    # Also +1 day to be inclusive on the bound.
    filed_lte = f"{next_y:04d}-{next_m:02d}-28"

    filings, err = _list_sec_filings(t, "6-K", filed_gte, filed_lte, max_results=250)
    if err:
        return {"ticker": t, "fy_start_month": fy_start_month,
                "fy_end_month": fy_end_month, "error": err}
    if not filings:
        return {
            "ticker": t, "fy_start_month": fy_start_month,
            "fy_end_month": fy_end_month,
            "error": (
                f"sec-api returned no 6-K filings for ticker={t} in "
                f"[{filed_gte}, {filed_lte}]. Issuer may not file 6-K "
                f"(US-domiciled issuers file 10-Q); verify the ticker."
            ),
            "filings_count": 0,
        }

    # Walk each filing, fetch the primary document, regex for monthly
    # revenue. Multiple regex patterns; first match wins per filing.
    records: list[dict[str, Any]] = []
    raw_filing_index: list[dict[str, Any]] = []
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415
    except ImportError:
        BeautifulSoup = None  # type: ignore[assignment]

    for f in filings:
        # sec-api's filings response uses ``linkToFilingDetails`` (the
        # primary document URL) for the document HTML. ``primaryDocumentUrl``
        # is NOT a field sec-api returns; check both for forward compat.
        url = (
            f.get("linkToFilingDetails")
            or f.get("primaryDocumentUrl")
            or f.get("linkToHtml")
            or ""
        )
        acc = f.get("accessionNo") or ""
        filed_at = (f.get("filedAt") or "")[:10]
        raw_filing_index.append({
            "accession": acc, "filed_at": filed_at, "url": url,
        })
        if not url:
            continue
        html, fetch_err = _fetch_filing_html_from_edgar(url)
        if not html:
            continue
        if BeautifulSoup is not None:
            try:
                text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
            except Exception:  # noqa: BLE001
                text = html
        else:
            text = html
        # Strip excess whitespace so the regex spans line breaks.
        text = re.sub(r"\s+", " ", text)[:60_000]

        match: Optional[re.Match[str]] = None
        which = -1
        for i, pat in enumerate(_MONTHLY_REVENUE_PATTERNS):
            m = pat.search(text)
            if m:
                match = m
                which = i
                break
        if not match:
            continue

        # Both patterns extract (month_name_part, year, currency, value, unit)
        # in groups 1-5. The match.group(1) carries "[Month]" or
        # "[Month] [Day]" — drop the day if present.
        month_str = match.group(1).strip()
        year_str = match.group(2).strip()
        currency = match.group(3).strip()
        value_str = match.group(4).strip()
        unit = match.group(5).lower()

        # First word of month_str = month name.
        month_name = month_str.split()[0] if month_str else ""
        month_num = _month_name_to_num(month_name)
        try:
            year_int = int(year_str)
            value_f = float(value_str.replace(",", ""))
        except ValueError:
            continue
        if month_num is None or year_int < 2000 or year_int > 2100:
            continue
        # Only keep records inside the asked window.
        if (year_int, month_num) < start_ym or (year_int, month_num) > end_ym:
            continue
        # Normalize to local-currency MILLIONS to match the canonical
        # 6-K reporting unit. "billion" → multiply by 1_000.
        revenue_millions = value_f * 1000.0 if unit.startswith("b") else value_f
        records.append({
            "year": year_int,
            "month": month_num,
            "year_month": f"{year_int:04d}-{month_num:02d}",
            "month_name": _MONTH_NAMES[month_num - 1].capitalize(),
            "revenue_local_millions": revenue_millions,
            "currency": currency,
            "raw_match": match.group(0)[:200],
            "regex_variant": which,
            "source_ref": f"sec:{acc}" if acc else "",
            "filed_at": filed_at,
        })

    # Deduplicate: keep first occurrence per (year, month) (regex hit
    # in the earliest filing for that month).
    seen: set[tuple[int, int]] = set()
    deduped: list[dict[str, Any]] = []
    for r in records:
        k = (r["year"], r["month"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    deduped.sort(key=lambda r: (r["year"], r["month"]))

    # Compute month-over-month growth rates within each year.
    mom_growth: list[dict[str, Any]] = []
    for i, r in enumerate(deduped):
        if i == 0:
            continue
        prior = deduped[i - 1]
        # Only compute MoM if same year and consecutive months.
        if prior["year"] == r["year"] and prior["month"] + 1 == r["month"]:
            if prior["revenue_local_millions"] > 0:
                gp = (r["revenue_local_millions"] / prior["revenue_local_millions"] - 1.0) * 100.0
                mom_growth.append({
                    "from_year_month": prior["year_month"],
                    "to_year_month": r["year_month"],
                    "growth_pct": round(gp, 2),
                })

    # Convenience: pre-compute prior_month → target_month growth-by-year
    # for the most common monthly-seasonality framing (e.g. Feb → Mar).
    # Caller can specify which months by inspecting `mom_growth`.

    answer_lines: list[str] = []
    answer_lines.append(
        f"**{t} monthly revenue {start_ym[0]}-{start_ym[1]:02d} to "
        f"{end_ym[0]}-{end_ym[1]:02d}** (from 6-K filings):"
    )
    for r in deduped:
        cur = r["currency"]
        v = r["revenue_local_millions"]
        if v >= 1_000:
            v_str = f"{cur}{v / 1_000:.2f}B"
        else:
            v_str = f"{cur}{v:,.0f}M"
        answer_lines.append(f"- {r['year_month']} ({r['month_name']}): {v_str}")
    if mom_growth:
        answer_lines.append("\n**Month-over-month growth rates:**")
        for g in mom_growth:
            answer_lines.append(
                f"- {g['from_year_month']} → {g['to_year_month']}: {g['growth_pct']:+.1f}%"
            )
    answer_summary_block = "\n".join(answer_lines)

    return _apply_binding(bind_as, {
        "ticker": t,
        "fy_start_month": fy_start_month,
        "fy_end_month": fy_end_month,
        "filings_count": len(filings),
        "parsed_count": len(deduped),
        "monthly_revenue": deduped,
        "month_over_month_growth": mom_growth,
        "filings_index": raw_filing_index[:50],
        "answer_summary_block": answer_summary_block,
    })
