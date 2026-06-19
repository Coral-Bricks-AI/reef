# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``multi_metric_guidance_comparison`` skill impl.

Hosts 1 ``@skill_fn``-registered callable for the
sector_analyst dispatch via ``invoke_skill_fn``. The recipe's prose
playbook lives next to this file in ``SKILL.md``.
"""

from __future__ import annotations

from typing import Any, Optional
from harness.skill_fn import skill_fn
from alphacumen.tools import _BIND_AS_PARAM_SCHEMA, _apply_binding


@skill_fn(
    skill_id='format_guidance_comparison',
    description=        "Format a list of per-metric guidance comparisons into canonical "
        "rubric-shaped atom strings. Use this for 'how did X's results "
        "compare to Q[N]'s guidance' questions where the answer is a "
        "per-metric bullet list. Model extracts each guided metric "
        "from the prior-quarter 8-K's 'Current Outlook' / 'Guidance' "
        "section AND the corresponding actual from the period 8-K, "
        "then passes them as a list. Tool decides the verdict "
        "('above high end of guidance range' / 'high end of guidance "
        "range' / 'within guidance range' / 'below low end of guidance "
        "range' / 'above the expected $X' / 'below the expected $X' / "
        "'right on target') and formats each as `<Metric>: <actual>, "
        "<verdict>`. Returns `formatted_atoms` (list) and "
        "`answer_summary_block` (pre-joined bullet list). USE THIS for "
        "every multi-metric beat-or-miss question — substring graders "
        "fail on paraphrased verdicts ('exceeded the upper bound' vs "
        "'above high end of guidance range').",
    parameters=               {
        "type": "object",
        "properties": {
            "metrics": {
                "type": "array",
                "description": (
                    "List of per-metric comparison dicts. Each: "
                    "{metric_name (str), actual (number), unit (str, "
                    "default 'Million'), is_loss (bool, default False — "
                    "set True for EBITDA losses to format as '$(X) Million'), "
                    "AND EITHER guidance_target (single point) OR "
                    "(guidance_low + guidance_high) for a range}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "metric_name": {"type": "string"},
                        "actual": {"type": "number"},
                        "unit": {"type": "string", "default": "Million"},
                        "is_loss": {"type": "boolean", "default": False},
                        "guidance_low": {"type": "number"},
                        "guidance_high": {"type": "number"},
                        "guidance_target": {"type": "number"},
                    },
                    "required": ["metric_name", "actual"],
                },
            },
            "bind_as": _BIND_AS_PARAM_SCHEMA,
        },
        "required": ["metrics"],
    },
)
def format_guidance_comparison(
    metrics: list[dict[str, Any]],
    bind_as: Optional[str] = None,
) -> dict[str, Any]:
    """Format a list of per-metric guidance comparisons into canonical
    rubric-shaped atom strings.

    Args:
        metrics: list of dicts, each describing one guided metric:
            {
                "metric_name": "In Force Premium (IFP)",  # required
                "actual": 944,                              # required, numeric
                "unit": "Million",                          # default: "Million"
                "is_loss": False,                           # if True, format actual as $(X)
                "guidance_low": 940,                        # range guidance: low end
                "guidance_high": 944,                       # range guidance: high end
                # OR
                "guidance_target": 64,                      # point guidance: single target
            }
            Pass EITHER (guidance_low + guidance_high) OR
            (guidance_target), not both. Pass neither if the
            metric was not part of the prior-quarter guidance —
            the tool will skip it.

    Returns dict with `formatted_atoms` (list of bullet strings)
    and `answer_summary_block` (pre-joined bullet list).
    """
    if not isinstance(metrics, list) or not metrics:
        return {"error": "metrics must be a non-empty list of per-metric dicts"}

    atoms: list[str] = []
    per_metric: list[dict[str, Any]] = []

    for m in metrics:
        if not isinstance(m, dict):
            continue
        name = m.get("metric_name") or m.get("metric") or ""
        if not name:
            continue
        try:
            actual = float(m.get("actual"))
        except (TypeError, ValueError):
            continue
        unit = m.get("unit", "Million")
        is_loss = bool(m.get("is_loss", False))
        is_dollar = "$" in str(m.get("unit", "")) or unit.lower() in ("million", "billion", "thousand", "$m", "$b")

        # Format the actual-value display.
        def fmt_val(v: float) -> str:
            if is_dollar:
                if is_loss or v < 0:
                    return f"$({abs(v):,g}) {unit}"
                return f"${v:,g} {unit}"
            return f"{v:,g} {unit}"

        actual_str = fmt_val(actual)

        # Decide verdict.
        verdict = ""
        target = m.get("guidance_target")
        g_low = m.get("guidance_low")
        g_high = m.get("guidance_high")
        if target is not None:
            try:
                t = float(target)
            except (TypeError, ValueError):
                t = None
            if t is not None:
                if abs(actual - t) < 1e-9:
                    verdict = "right on target"
                elif actual > t:
                    verdict = f"above the expected {fmt_val(t)}"
                else:
                    verdict = f"below the expected {fmt_val(t)}"
        elif g_low is not None and g_high is not None:
            try:
                lo = float(g_low); hi = float(g_high)
            except (TypeError, ValueError):
                lo = hi = None
            if lo is not None and hi is not None:
                if lo > hi:
                    lo, hi = hi, lo
                # Use small relative tolerance to detect "at high end" / "at low end"
                tol = max(abs(hi) * 1e-4, 1e-6)
                if abs(actual - hi) <= tol:
                    verdict = "high end of guidance range"
                elif abs(actual - lo) <= tol:
                    verdict = "low end of guidance range"
                elif actual > hi:
                    verdict = "above high end of guidance range"
                elif actual < lo:
                    verdict = "below low end of guidance range"
                else:
                    verdict = "within guidance range"

        if not verdict:
            atom = f"{name}: {actual_str}"
        else:
            atom = f"{name}: {actual_str}, {verdict}"
        # For range guidance on percentage-unit metrics (margins, growth
        # rates, etc.), append the basis-point delta from BOTH bounds.
        # Analyst-convention reports "X bps beat from low end and Y bps
        # beat from high end" for ranged-guidance margin questions; the
        # midpoint-only verdict above loses that detail. Triggered by
        # ``unit`` containing "pct" / "percent" / "%" (case-insensitive).
        if g_low is not None and g_high is not None:
            unit_lower = str(unit).lower()
            is_pct = (
                "pct" in unit_lower
                or "percent" in unit_lower
                or "%" in unit_lower
                or "margin" in str(name).lower()
            )
            if is_pct:
                try:
                    lo_f = float(g_low); hi_f = float(g_high)
                    if lo_f > hi_f:
                        lo_f, hi_f = hi_f, lo_f
                    delta_low_bps = round((actual - lo_f) * 100)
                    delta_high_bps = round((actual - hi_f) * 100)
                    sign_low = "beat" if delta_low_bps > 0 else ("miss" if delta_low_bps < 0 else "at")
                    sign_high = "beat" if delta_high_bps > 0 else ("miss" if delta_high_bps < 0 else "at")
                    atom += (
                        f" ({abs(delta_low_bps)}bps {sign_low} from low end "
                        f"of {fmt_val(lo_f)}; "
                        f"{abs(delta_high_bps)}bps {sign_high} from high end "
                        f"of {fmt_val(hi_f)})"
                    )
                except (TypeError, ValueError):
                    pass
        atoms.append(atom)
        per_metric.append({
            "metric_name": name,
            "actual": actual,
            "guidance_target": float(target) if target is not None else None,
            "guidance_low": float(g_low) if g_low is not None else None,
            "guidance_high": float(g_high) if g_high is not None else None,
            "verdict": verdict,
            "atom": atom,
        })

    return _apply_binding(bind_as, {
        "metric_count": len(atoms),
        "per_metric": per_metric,
        "answer": {
            "formatted_atoms": atoms,
            "answer_summary_block": "\n".join(f"- {a}" for a in atoms),
        },
    })
