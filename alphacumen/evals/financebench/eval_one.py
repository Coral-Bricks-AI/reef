#!/usr/bin/env python3
# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end FinanceBench eval against hosted AlphaCumen.

FinanceBench (Islam et al. 2023, Patronus AI -- "FinanceBench: A New
Benchmark for Financial Question Answering") is a RAG-over-10-Ks
benchmark. We use the public 150-question open-source slice
(``data/financebench_open_source.jsonl``, CC-BY-NC-4.0, fetched from
``github.com/patronus-ai/financebench``).

Each FinanceBench row has a single gold answer plus justification. The
grader is one Claude call per row that returns a single verdict
(``correct`` / ``incorrect`` / ``refused``), mirroring FinanceBench's
own correct/incorrect/refused taxonomy.

Pipeline (per row):

1. Submit the question to hosted AlphaCumen via the Coral platform
   gateway (``alphacumen.evals.common.runner.ask_alphacumen_hosted``).
2. Take the agent's ``answer_summary`` as the candidate answer.
3. Grade the candidate against the gold answer with Claude
   (``alphacumen.evals.common.judge``).
4. Print the verdict and overall pass/fail.

Usage::

    export CORAL_API_KEY=ak_...
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m alphacumen.evals.financebench.eval_one
    python -m alphacumen.evals.financebench.eval_one --row 5 --out /tmp/row5.json
    python -m alphacumen.evals.financebench.eval_one --rows 0-49 --out /tmp/fb
    python -m alphacumen.evals.financebench.eval_one --all --json-out /tmp/fb.jsonl

Auth:

- Coral: ``$CORAL_API_KEY`` (or ``$CORAL_API_KEY_FILE``, or ``~/.coral/api_key``)
- Claude (judge): ``$ANTHROPIC_API_KEY``

Gateway URL defaults to the public Coral gateway; override with
``$CORAL_PLATFORM_URL``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alphacumen.evals.common import cli, config, judge, runner, util

HERE = Path(__file__).resolve().parent

DATASET_JSONL = HERE / "data" / "financebench_open_source.jsonl"
DATASET_NAME = "financebench_open_source"

JUDGE_MODEL = os.getenv("FINANCEBENCH_JUDGE_MODEL", config.DEFAULT_JUDGE_MODEL)
HARNESS_MODEL = os.getenv("FINANCEBENCH_HARNESS_MODEL", config.DEFAULT_HARNESS_MODEL)


# ---------------------------------------------------------------------------
# Claude judge -- single verdict per row
# ---------------------------------------------------------------------------


_JUDGE_SYSTEM = (
    "You are an impartial grader for the FinanceBench financial-QA benchmark. "
    "You compare a candidate answer to the human gold answer for one question "
    "and return strict JSON only."
)

_JUDGE_USER_TEMPLATE = """Grade the candidate answer for ONE FinanceBench question.

Question:
{question}

Gold answer (the human reference):
{reference}

Gold justification (how the reference was derived; for your context only):
{justification}

Candidate answer (the system being graded):
{candidate}

Grading rules:
- Verdict "correct": the candidate clearly conveys the same answer as the
  gold answer. Numerical equivalence is fine -- accept reasonable rounding,
  unit restatements ("$1,577M" vs "1577.00" vs "$1.577 billion"), and minor
  wording differences. For yes/no questions the candidate must reach the
  same conclusion (extra supporting detail beyond the gold is fine; an
  affirmatively different conclusion or a materially wrong number is not).
- Verdict "refused": the candidate declines to answer or says it cannot
  find / does not have the information (no substantive answer attempted).
- Verdict "incorrect": the candidate attempts an answer but it conflicts
  with the gold answer (wrong number, wrong conclusion, wrong entity, etc.).

Return strict JSON only, no prose:
{{"verdict": "correct"|"incorrect"|"refused", "reason": "<one sentence>"}}
"""


_VALID_VERDICTS = ("correct", "incorrect", "refused")


def grade_answer(
    question: str,
    reference: str,
    justification: str,
    candidate: str,
) -> dict[str, Any]:
    """Grade ``candidate`` against the gold answer. Returns the verdict dict."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Export it before running."
        )
    user = _JUDGE_USER_TEMPLATE.format(
        question=question,
        reference=reference,
        justification=justification or "(none provided)",
        candidate=candidate,
    )
    raw = judge.judge_with_retry(
        api_key, JUDGE_MODEL, _JUDGE_SYSTEM, user, verdict_key="verdict"
    )
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in _VALID_VERDICTS:
        verdict = "correct" if verdict.startswith("correct") else "incorrect"
    out = {"verdict": verdict, "reason": raw.get("reason", "")}
    reason_short = str(out["reason"]).replace("\n", " ")[:100]
    print(f"  [judge] verdict={verdict.upper()}: {reason_short}")
    return out


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def _load_dataset() -> list[dict[str, Any]]:
    if not DATASET_JSONL.exists():
        raise FileNotFoundError(
            f"dataset not found: {DATASET_JSONL}. "
            f"Re-download from github.com/patronus-ai/financebench "
            f"(data/financebench_open_source.jsonl)."
        )
    rows: list[dict[str, Any]] = []
    with DATASET_JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_row(row_index: int) -> dict[str, Any]:
    rows = _load_dataset()
    if row_index >= len(rows):
        raise IndexError(f"row {row_index} out of range; dataset has {len(rows)} rows")
    return rows[row_index]


def _row_count() -> int:
    return len(_load_dataset())


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
    fb_id = row.get("financebench_id") or f"row_{row_index}"
    question = row["question"]
    reference = row["answer"]
    justification = row.get("justification") or ""
    qtype = row.get("question_type") or ""
    company = row.get("company") or ""
    doc_name = row.get("doc_name") or ""
    print(f"\n=== Row {row_index} | {fb_id} | {qtype} | {company} / {doc_name} ===")
    print(f"Q: {question}")
    print(f"Gold A:\n{reference}\n")

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
        candidate, _full_result, run_rec = runner.ask_alphacumen_hosted(
            coral_question,
            model=harness_model,
            api_key=coral_api_key,
            gateway_url=coral_gateway_url,
            pipeline_package=args.pipeline_package,
        )
        coral_elapsed_s = round(time.time() - t_coral, 2)
        print(f"[coral] elapsed={coral_elapsed_s}s")

    print(f"\n[candidate answer ({len(candidate)} chars)]\n{candidate}\n")

    print(f"[step 2/2] grading with Claude ({JUDGE_MODEL}) ...")
    t_judge = time.time()
    verdict = grade_answer(question, reference, justification, candidate)
    judge_elapsed_s = round(time.time() - t_judge, 2)

    is_correct = verdict["verdict"] == "correct"
    is_refused = verdict["verdict"] == "refused"

    print(f"\n=== Row {row_index} verdict ===")
    print(f"verdict: {verdict['verdict']}")
    print(f"reason:  {verdict['reason']}")
    print(f"correct: {is_correct}")

    request_id = util.pick_field(run_rec, "request_id")
    pipeline_slug = util.pick_field(run_rec, "pipeline_slug")

    summary = {
        "verdict": verdict["verdict"],
        "correct": is_correct,
        "refused": is_refused,
        "judge_model": JUDGE_MODEL,
        "harness_model": harness_model,
    }

    envelope = {
        "row_index": row_index,
        "dataset": DATASET_NAME,
        "financebench_id": fb_id,
        "question": question,
        "reference": reference,
        "justification": justification,
        "question_type": qtype,
        "company": company,
        "doc_name": doc_name,
        "candidate": candidate,
        "verdict": verdict,
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
                     help="Row index (0-149). Default: 0 when no other selector is given.")
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
                             f"Defaults to $FINANCEBENCH_HARNESS_MODEL or {HARNESS_MODEL!r}.")
    parser.add_argument("--pipeline-package", type=str,
                        default=config.PIPELINE_PACKAGE_DEFAULT,
                        help="Pipeline package spec forwarded to the gateway. "
                             f"Default {config.PIPELINE_PACKAGE_DEFAULT!r}. Pin to an exact "
                             "version (e.g. 'cb-ia==0.0.420') for repeatable runs.")
    parser.add_argument("--query-suffix", type=str, default="",
                        help="Optional text appended to the question before sending. "
                             "Useful for anchoring the dataset's reference filing. "
                             "The judge sees the original question.")
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
        except Exception as exc:  # noqa: BLE001 -- log and continue per-row
            print(f"[error] row {ri} crashed: {exc!r}", file=sys.stderr)
            summaries.append((ri, "errored", None))
            continue

        v = envelope["summary"]["verdict"]
        status = "correct" if v == "correct" else ("refused" if v == "refused" else "incorrect")
        summaries.append((ri, status, envelope))

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
        n_correct = sum(1 for _, s, _ in summaries if s == "correct")
        n_incorrect = sum(1 for _, s, _ in summaries if s == "incorrect")
        n_refused = sum(1 for _, s, _ in summaries if s == "refused")
        n_errored = sum(1 for _, s, _ in summaries if s == "errored")
        n_attempted = n_correct + n_incorrect + n_refused
        acc = (n_correct / n_attempted) if n_attempted else 0.0
        print("\n=== Sweep summary ===")
        print(f"rows requested: {len(summaries)}")
        print(f"  correct:   {n_correct}/{n_attempted} attempted  ({acc:.1%})")
        print(f"  incorrect: {n_incorrect}/{n_attempted} attempted")
        print(f"  refused:   {n_refused}/{n_attempted} attempted")
        print(f"  errored:   {n_errored}")
        for ri, s, env in summaries:
            tag = s.upper()
            if env is not None:
                print(f"  row {ri}: {tag}  ({env.get('financebench_id')})")
            else:
                print(f"  row {ri}: {tag}")

    if len(summaries) == 1:
        ri, s, env = summaries[0]
        if s == "errored" or env is None:
            return 2
        return 0 if env["summary"]["correct"] else 1

    any_not_correct = any(s in ("incorrect", "refused", "errored") for _, s, _ in summaries)
    return 1 if any_not_correct else 0


if __name__ == "__main__":
    sys.exit(main())
