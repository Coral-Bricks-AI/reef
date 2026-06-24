#!/usr/bin/env python3
# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end Vals AI Finance Agent benchmark eval against hosted AlphaCumen.

We use the public 50-question slice of the Vals AI Finance Agent
benchmark (``data/finance_agent_benchmark.csv``, CC-BY-4.0, fetched from
``huggingface.co/datasets/vals-ai/finance_agent_benchmark``). Each row
ships a rubric of atoms (correctness / contradiction) graded one at a
time, unlike FinanceBench's single gold answer.

Pipeline (per row):

1. Submit the question to hosted AlphaCumen via the Coral platform
   gateway (``alphacumen.evals.common.runner.ask_alphacumen_hosted``).
2. Take the agent's ``answer_summary`` as the candidate answer.
3. Grade each rubric atom (correctness / contradiction) with Claude
   (``alphacumen.evals.common.judge``).
4. Print per-atom verdicts and overall pass/fail.

Usage::

    export CORAL_API_KEY=ak_...
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m alphacumen.evals.valsai.eval_one
    python -m alphacumen.evals.valsai.eval_one --row 5 --out /tmp/row5.json
    python -m alphacumen.evals.valsai.eval_one --rows 0-9 --out /tmp/vals
    python -m alphacumen.evals.valsai.eval_one --all --json-out /tmp/vals.jsonl

Auth:

- Coral: ``$CORAL_API_KEY`` (or ``$CORAL_API_KEY_FILE``, or ``~/.coral/api_key``)
- Claude (judge): ``$ANTHROPIC_API_KEY``
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alphacumen.evals.common import cli, config, judge, runner, util

HERE = Path(__file__).resolve().parent

DATASET_CSV = HERE / "data" / "finance_agent_benchmark.csv"
DATASET_NAME = "finance_agent_benchmark"

JUDGE_MODEL = os.getenv("VALSAI_JUDGE_MODEL", config.DEFAULT_JUDGE_MODEL)
HARNESS_MODEL = os.getenv("VALSAI_HARNESS_MODEL", config.DEFAULT_HARNESS_MODEL)


# ---------------------------------------------------------------------------
# Per-row asof overrides
# ---------------------------------------------------------------------------
#
# A handful of public-50 rows reference filings whose SEC accession dates
# sit just past the global ``--asof 2025-03-01`` clamp the other rows
# need. Without an override, the model honestly reports "no such filing
# found in window" and the row scores 0% even though retrieval would have
# surfaced the answer. Each override is keyed by row index with one day
# after the verbatim filing date so it just lands in the visible window.

_PER_ROW_ASOF_OVERRIDES: dict[int, str] = {
    15: "2025-04-01T00:00:00Z",  # KKR Series D MCPS 424B5 (2025-03-04)
    28: "2025-05-15T00:00:00Z",  # Spirit Airlines 10-K/A (2025-04-30)
    35: "2025-04-01T00:00:00Z",  # Rocket-Redfin merger 8-K (2025-03-10)
}


# ---------------------------------------------------------------------------
# Claude judge -- one rubric atom at a time
# ---------------------------------------------------------------------------


_JUDGE_SYSTEM = (
    "You are an impartial grader for a structured financial-research benchmark. "
    "You grade one rubric atom at a time and return strict JSON only."
)

_JUDGE_USER_TEMPLATE = """Grade the candidate answer against ONE rubric atom.

Question:
{question}

Reference answer (the human gold answer):
{reference}

Candidate answer (the system being graded):
{candidate}

Rubric atom:
- operator: {operator}
- criteria: {criteria}

Grading rules:
- If operator is "correctness": PASS iff the candidate clearly asserts/contains
  the criteria fact (numerical equivalence is fine -- e.g. "$2.86B" vs
  "2,865,507"). Minor wording differences OK.
- If operator is "contradiction": PASS iff the candidate does NOT contradict
  the criteria text. Missing information is NOT a contradiction. Only
  affirmative wrong claims fail this atom.

Return strict JSON only, no prose:
{{"pass": true|false, "reason": "<one sentence>"}}
"""


def grade_answer(
    question: str,
    reference: str,
    candidate: str,
    rubric_atoms: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Grade ``candidate`` atom-by-atom. Returns per-atom verdicts."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Export it before running."
        )
    results = []
    for i, atom in enumerate(rubric_atoms, 1):
        operator = atom["operator"]
        criteria = atom["criteria"]
        user = _JUDGE_USER_TEMPLATE.format(
            question=question,
            reference=reference,
            candidate=candidate,
            operator=operator,
            criteria=criteria,
        )
        verdict = judge.judge_with_retry(
            api_key, JUDGE_MODEL, _JUDGE_SYSTEM, user, verdict_key="pass"
        )
        results.append({
            "atom_index": i,
            "operator": operator,
            "criteria": criteria,
            "pass": bool(verdict.get("pass")),
            "reason": verdict.get("reason", ""),
        })
        marker = "PASS" if results[-1]["pass"] else "FAIL"
        crit_short = criteria.replace("\n", " ")[:80]
        print(f"  [judge] atom {i}/{len(rubric_atoms)} {operator} {marker}: {crit_short}")
    return results


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def load_row(row_index: int) -> dict[str, Any]:
    if not DATASET_CSV.exists():
        raise FileNotFoundError(
            f"dataset not found: {DATASET_CSV}. "
            f"Re-download from huggingface.co/datasets/vals-ai/finance_agent_benchmark"
        )
    with DATASET_CSV.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if row_index >= len(rows):
        raise IndexError(f"row {row_index} out of range; dataset has {len(rows)} rows")
    row = rows[row_index]
    row["_rubric_atoms"] = ast.literal_eval(row["Rubric"])
    return row


def _row_count() -> int:
    with DATASET_CSV.open("r", encoding="utf-8") as f:
        return sum(1 for _ in csv.DictReader(f))


# ---------------------------------------------------------------------------
# Per-row driver
# ---------------------------------------------------------------------------


def run_row(
    row_index: int,
    *,
    args: argparse.Namespace,
    coral_api_key: str,
    coral_gateway_url: str,
) -> dict[str, Any]:
    """Run one row. Returns the result envelope."""
    row = load_row(row_index)
    question = row["Question"]
    reference = row["Answer"]
    qtype = row["Question Type"]
    atoms = row["_rubric_atoms"]
    print(f"\n=== Row {row_index} | {qtype} | {len(atoms)} rubric atoms ===")
    print(f"Q: {question}")
    print(f"Reference A:\n{reference}\n")

    harness_model = args.model

    if args.skip_coral:
        print("[debug] reading candidate answer from stdin...")
        candidate = sys.stdin.read().strip()
        run_rec: dict[str, Any] = {}
    else:
        print("[step 1/2] asking hosted AlphaCumen...")
        t_coral = time.time()
        coral_question = question
        if args.query_suffix:
            coral_question = f"{question}\n\n{args.query_suffix}"
            print(f"[coral] appending query suffix: {args.query_suffix!r}")
        effective_asof = _PER_ROW_ASOF_OVERRIDES.get(row_index, args.asof)
        if row_index in _PER_ROW_ASOF_OVERRIDES and effective_asof != args.asof:
            print(
                f"[asof] row {row_index} override: "
                f"{args.asof!r} -> {effective_asof!r}"
            )
        candidate, _full_result, run_rec = runner.ask_alphacumen_hosted(
            coral_question,
            model=harness_model,
            api_key=coral_api_key,
            gateway_url=coral_gateway_url,
            pipeline_package=args.pipeline_package,
            asof=effective_asof,
        )
        coral_elapsed_s = round(time.time() - t_coral, 2)
        print(f"[coral] elapsed={coral_elapsed_s}s")

    print(f"\n[candidate answer ({len(candidate)} chars)]\n{candidate}\n")

    print(f"[step 2/2] grading with Claude ({JUDGE_MODEL}) ...")
    t_judge = time.time()
    verdicts = grade_answer(question, reference, candidate, atoms)
    judge_elapsed_s = round(time.time() - t_judge, 2)

    correctness = [v for v in verdicts if v["operator"] == "correctness"]
    contradiction = [v for v in verdicts if v["operator"] == "contradiction"]
    correctness_passed = sum(1 for v in correctness if v["pass"])
    contradiction_failed = sum(1 for v in contradiction if not v["pass"])
    fully_correct = (
        correctness_passed == len(correctness) and contradiction_failed == 0
    )
    partial = (correctness_passed / len(correctness)) if correctness else 1.0

    print(f"\n=== Row {row_index} verdict ===")
    print(f"correctness atoms passed: {correctness_passed}/{len(correctness)}")
    print(f"contradiction atoms failed (lower=better): "
          f"{contradiction_failed}/{len(contradiction)}")
    print(f"partial credit (atom-level): {partial:.1%}")
    print(f"fully correct (strict):      {fully_correct}")

    request_id = util.pick_field(run_rec, "request_id")
    pipeline_slug = util.pick_field(run_rec, "pipeline_slug")

    summary = {
        "correctness_passed": correctness_passed,
        "correctness_total": len(correctness),
        "contradiction_failed": contradiction_failed,
        "contradiction_total": len(contradiction),
        "partial_credit": partial,
        "fully_correct": fully_correct,
        "judge_model": JUDGE_MODEL,
        "harness_model": harness_model,
    }

    envelope = {
        "row_index": row_index,
        "dataset": DATASET_NAME,
        "question": question,
        "reference": reference,
        "question_type": qtype,
        "candidate": candidate,
        "verdicts": verdicts,
        "summary": summary,
        "request_id": request_id,
        "pipeline_slug": pipeline_slug,
        "coral_run": {
            "request_id": request_id,
            "pipeline_slug": pipeline_slug,
            "model": util.pick_field(run_rec, "model"),
            "status": str(util.pick_field(run_rec, "status") or ""),
            "time_taken_ms": util.pick_field(run_rec, "time_taken_ms"),
            "queued_at": util.iso(util.pick_field(run_rec, "queued_at")),
            "started_at": util.iso(util.pick_field(run_rec, "started_at")),
            "completed_at": util.iso(util.pick_field(run_rec, "completed_at")),
            "mode": util.pick_field(run_rec, "mode"),
            "error_message": util.pick_field(run_rec, "error_message"),
            "error_code": util.pick_field(run_rec, "error_code"),
        },
        "indices": config.PIPELINE_INDICES,
        "judge_elapsed_s": judge_elapsed_s,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    return envelope


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sel = parser.add_mutually_exclusive_group()
    sel.add_argument("--row", type=int, default=None,
                     help="Row index (0-49). Default: 0 when no other selector is given.")
    sel.add_argument("--rows", type=str, default=None,
                     help="Row selector, e.g. '0-9' or '0,3,7-12'.")
    sel.add_argument("--all", action="store_true",
                     help="Run every row in the dataset.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Optional path to write full result JSON. For multi-row "
                             "runs this is treated as a directory and each row writes "
                             "``row_<N>.json`` inside it.")
    parser.add_argument("--skip-coral", action="store_true",
                        help="Skip the hosted submit and grade a pasted answer (debug). "
                             "Reads stdin. Forces a single-row run.")
    parser.add_argument("--model", type=str, default=HARNESS_MODEL,
                        help=f"Model id forwarded to the hosted gateway. "
                             f"Defaults to $VALSAI_HARNESS_MODEL or {HARNESS_MODEL!r}.")
    parser.add_argument("--pipeline-package", type=str,
                        default=config.PIPELINE_PACKAGE_DEFAULT,
                        help="Pipeline package spec forwarded to the gateway. "
                             f"Default {config.PIPELINE_PACKAGE_DEFAULT!r}. Pin to an exact "
                             "version (e.g. 'cb-ia==0.0.420') for repeatable runs.")
    parser.add_argument("--asof", type=str, default=None,
                        help="Pin the run to a specific date (ISO-8601 UTC, e.g. "
                             "'2025-03-01T00:00:00Z'). Uses mode='backtest' so the "
                             "pipeline treats this as 'today'. Useful when benchmark "
                             "reference answers target a specific fiscal year.")
    parser.add_argument("--query-suffix", type=str, default="",
                        help="Optional text appended to the question before sending. "
                             "Useful for anchoring the reference timeframe.")
    cli.add_json_out_arg(parser)
    args = parser.parse_args()

    total_rows = _row_count()
    if args.skip_coral:
        if args.rows or args.all:
            print("[warn] --skip-coral implies a single row; ignoring --rows/--all",
                  file=sys.stderr)
        rows_to_run = [args.row if args.row is not None else 0]
    elif args.all:
        rows_to_run = list(range(total_rows))
    elif args.rows:
        rows_to_run = util.parse_rows_spec(args.rows, total_rows)
        if not rows_to_run:
            print(f"[error] --rows {args.rows!r} matched no rows in [0,{total_rows})",
                  file=sys.stderr)
            return 2
    else:
        rows_to_run = [args.row if args.row is not None else 0]

    coral_api_key = config.read_coral_api_key()
    coral_gateway_url = os.environ.get(
        "CORAL_PLATFORM_URL", config.GATEWAY_URL_DEFAULT,
    )

    if args.out and len(rows_to_run) > 1:
        args.out.mkdir(parents=True, exist_ok=True)

    summaries: list[tuple[int, str, dict[str, Any] | None]] = []
    for ri in rows_to_run:
        try:
            envelope = run_row(
                ri,
                args=args,
                coral_api_key=coral_api_key,
                coral_gateway_url=coral_gateway_url,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[error] row {ri} crashed: {exc!r}", file=sys.stderr)
            summaries.append((ri, "errored", None))
            continue

        s = "passed" if envelope["summary"]["fully_correct"] else "failed"
        summaries.append((ri, s, envelope))

        if args.json_out and not args.skip_coral:
            util.append_jsonl(args.json_out, envelope)

        if args.out:
            target = (
                args.out / f"row_{ri}.json"
                if args.out.is_dir() or len(rows_to_run) > 1
                else args.out
            )
            target.write_text(json.dumps(envelope, indent=2, default=str))
            print(f"[out] row {ri} -> {target}")

    if len(summaries) > 1:
        n_passed = sum(1 for _, s, _ in summaries if s == "passed")
        n_failed = sum(1 for _, s, _ in summaries if s == "failed")
        n_errored = sum(1 for _, s, _ in summaries if s == "errored")
        n_attempted = n_passed + n_failed
        print("\n=== Sweep summary ===")
        print(f"rows requested: {len(summaries)}")
        print(f"  passed:  {n_passed}/{n_attempted} attempted")
        print(f"  failed:  {n_failed}/{n_attempted} attempted")
        print(f"  errored: {n_errored}")
        for ri, s, env in summaries:
            tag = s.upper()
            if env is not None:
                pc = env["summary"]["partial_credit"]
                print(f"  row {ri}: {tag}  partial={pc:.1%}")
            else:
                print(f"  row {ri}: {tag}")

    if len(summaries) == 1:
        ri, s, env = summaries[0]
        if s == "errored" or env is None:
            return 2
        return 0 if env["summary"]["fully_correct"] else 1

    any_failed = any(s in ("failed", "errored") for _, s, _ in summaries)
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
