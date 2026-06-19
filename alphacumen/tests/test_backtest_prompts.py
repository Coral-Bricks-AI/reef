# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Backtest-mode prompt rendering tests.

Covers the alphacumen side of the backtest leak guarantee: when
:func:`alphacumen.swarm.run` is called with ``asof=<iso>``, both the
specialist base prompt and the planner seed prompt render against
that asof and switch to the backtest scaffold (which explicitly
tells the model "anything after this date is unknown to you"). Live
runs render against wallclock and use the live scaffold.
"""

from __future__ import annotations

from datetime import datetime, timezone

from alphacumen.roster import (
    INVESTMENT_ANALYST_ROSTER,
    SPECIALIST_CONFIGS,
    _build_base_instruction,
    _resolve_today,
)
from alphacumen.swarm import _planner_system_prompt, _PLANNER_SEED_CACHE


# ---------------------------------------------------------------- _resolve_today


def test_resolve_today_live_returns_wallclock() -> None:
    today, is_backtest = _resolve_today(None)
    assert is_backtest is False
    # Sanity: the formatted date should parse back as a real date.
    assert datetime.strptime(today, "%B %d, %Y").year >= 2024


def test_resolve_today_backtest_pins_to_iso() -> None:
    today, is_backtest = _resolve_today("2024-03-01T00:00:00+00:00")
    assert is_backtest is True
    assert today == "March 01, 2024"


def test_resolve_today_accepts_z_suffix() -> None:
    today, is_backtest = _resolve_today("2024-06-15T12:34:56Z")
    assert is_backtest is True
    assert today == "June 15, 2024"


def test_resolve_today_naive_iso_assumed_utc() -> None:
    today, is_backtest = _resolve_today("2024-12-25T00:00:00")
    assert is_backtest is True
    assert today == "December 25, 2024"


def test_resolve_today_garbage_falls_back_to_live() -> None:
    today, is_backtest = _resolve_today("not-a-date")
    # Defensive fallback: we don't crash the run; the kernel clamp is
    # still authoritative on retrieval correctness.
    assert is_backtest is False
    assert datetime.strptime(today, "%B %d, %Y") is not None


# ---------------------------------------------------------------- specialist base prompt


def test_specialist_base_uses_live_scaffold_when_asof_unset() -> None:
    text = _build_base_instruction(tool_budget=6, asof=None)
    assert "operating in" in text and "real-time" in text
    # Live scaffold does NOT carry the simulated-date framing.
    assert "simulated point in time" not in text
    assert "simulated date" not in text


def test_specialist_base_uses_backtest_scaffold_when_asof_set() -> None:
    text = _build_base_instruction(
        tool_budget=6, asof="2024-03-01T00:00:00Z",
    )
    assert "March 01, 2024" in text
    assert "simulated point in time" in text
    # The lookahead guard line must be present in the backtest scaffold.
    assert "Lookahead" in text or "lookahead" in text or "LOOKAHEAD" in text
    # And explicit "anything after this date is unknown to you".
    assert "unknown to you" in text


def test_specialist_base_backtest_says_no_real_time() -> None:
    """The backtest scaffold must NOT keep the live-mode 'operating in
    real-time' phrasing -- that phrase confuses the model on a replay."""
    text = _build_base_instruction(
        tool_budget=6, asof="2024-03-01T00:00:00Z",
    )
    assert "operating in\nreal-time" not in text


def test_specialist_system_prompt_threads_asof() -> None:
    cfg = SPECIALIST_CONFIGS["sector_analyst"]
    live = cfg.system_prompt()
    backtest = cfg.system_prompt(asof="2024-03-01T00:00:00Z")
    assert "March 01, 2024" in backtest
    assert "March 01, 2024" not in live
    # Both should contain the persona body (sanity).
    assert "sector_analyst" not in live  # prompt body has no role echo
    assert len(live) > 200 and len(backtest) > 200


# ---------------------------------------------------------------- planner seed prompt


def _clear_planner_cache() -> None:
    """Reset the per-(roster, is_backtest, date) seed cache so each
    test renders from disk instead of returning a sibling test's
    cached entry."""
    _PLANNER_SEED_CACHE.clear()


def test_planner_seed_live_renders_wallclock() -> None:
    _clear_planner_cache()
    text = _planner_system_prompt(INVESTMENT_ANALYST_ROSTER)
    # Live scaffold does NOT carry the no-foresight framing.
    assert "no foresight" not in text
    assert "post-" not in text or "post-{today}" not in text


def test_planner_seed_backtest_renders_asof() -> None:
    _clear_planner_cache()
    text = _planner_system_prompt(
        INVESTMENT_ANALYST_ROSTER,
        asof="2024-03-01T00:00:00Z",
    )
    assert "March 01, 2024" in text
    # The lookahead-guard phrasing the planner needs to police itself.
    assert "no foresight" in text
    assert (
        "later revealed" in text
        or "later become" in text
        or "Looking back" in text
        or "looking back" in text
    )
    # Planner instruction template should bake the asof into the
    # specialist-instruction example.
    assert "≤" in text or "<=" in text or "as of March 01, 2024" in text


def test_planner_seed_cache_keys_separate_live_and_backtest() -> None:
    """A live run and a backtest run for the same roster must NOT
    share a cached seed -- the cache key includes is_backtest."""
    _clear_planner_cache()
    live_a = _planner_system_prompt(INVESTMENT_ANALYST_ROSTER)
    backtest_a = _planner_system_prompt(
        INVESTMENT_ANALYST_ROSTER, asof="2024-03-01T00:00:00Z",
    )
    live_b = _planner_system_prompt(INVESTMENT_ANALYST_ROSTER)
    backtest_b = _planner_system_prompt(
        INVESTMENT_ANALYST_ROSTER, asof="2024-03-01T00:00:00Z",
    )
    assert live_a == live_b  # cache hit for live
    assert backtest_a == backtest_b  # cache hit for backtest
    assert live_a != backtest_a  # different scaffold


def test_planner_seed_different_asof_renders_different_dates() -> None:
    _clear_planner_cache()
    march = _planner_system_prompt(
        INVESTMENT_ANALYST_ROSTER, asof="2024-03-01T00:00:00Z",
    )
    june = _planner_system_prompt(
        INVESTMENT_ANALYST_ROSTER, asof="2024-06-15T00:00:00Z",
    )
    assert "March 01, 2024" in march
    assert "June 15, 2024" in june
    assert march != june
