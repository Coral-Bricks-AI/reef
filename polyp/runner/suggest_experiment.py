"""``polyp.runner.suggest_experiment`` -- queue the next experiment when idle.

Ported from ``suggest_experiment.sh``. Fired by the architect watcher
when iterators are quiet, nothing is enqueued, and the ready queue is
below low-watermark. Designs ONE experiment, submits it via
``cbq submit --origin auto-suggest``, and reports on Slack.

Invoke as ``python -m polyp.runner.suggest_experiment``.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from reef.agents.headless_claude import HeadlessClaude

from polyp.runner.common import (
    cbq,
    cwd,
    git_reset_to_main,
    iso_now,
    slack_post,
)


WORKDIR = Path(os.environ.get(
    "WORKDIR", str(Path.home() / "architect" / "workdir" / "www")
)).expanduser()
SUGGEST_LOG = Path(os.environ.get(
    "SUGGEST_LOG", str(Path.home() / "architect" / "auto-suggest.log")
)).expanduser()

_SUBMITTED_PATTERN = re.compile(r"submitted:\s+([0-9][0-9.]*-[a-z0-9-]+)")


def _machine_desc(machine: str) -> str:
    if machine == "h100":
        return (
            "H100-class (this kind's worker owns 4x H100 80GB on the shared "
            "P5 box, so a spec may request up to 4 GPUs — large models and "
            "batches are fine)"
        )
    return (
        "A10-class (1x NVIDIA A10G 23GB, g5.xlarge) — a single small GPU; "
        "size models and batches to fit it"
    )


def _kind_desc(kind: str) -> str:
    if kind == "loadtest":
        return (
            "drives an already-running inference server (throughput/latency "
            "sweeps); it does not load models in-process"
        )
    if kind == "finetune":
        return "training runs"
    return (
        "in-process model evals (load model, run benchmark, write results.json)"
    )


def build_suggest_prompt(*, submit_kind: str, machine: str,
                         machine_desc: str, kind_desc: str,
                         workdir: Path) -> str:
    """Assemble the auto-suggest prompt + portfolio-state embedding."""
    parts: list[str] = [_SUGGEST_PROMPT_HEAD, "\n"]
    parts.append(
        "\nTARGET KIND (assigned by the watcher; each kind maps to one GPU "
        "worker, and this kind's worker is live)\n\n"
        f"- kind={submit_kind} — {kind_desc}\n"
        f"- The worker serving this kind currently runs on machine={machine}: "
        f"{machine_desc}\n"
        f"- Design the experiment to fit this kind's execution model and that "
        f"hardware budget. Submit with exactly: --kind {submit_kind} "
        f"--machine {machine}\n"
        "- A task submitted under the wrong kind lands on a worker that "
        "can't run it.\n"
        "- constraints.md was accumulated mostly on A10-class hardware: "
        "VRAM/OOM ceilings do not transfer across machine classes, but "
        "tooling/process lessons do.\n"
    )
    parts.append("\n")
    parts.append(
        "---PRE-GATHERED PORTFOLIO STATE (collected by the launch wrapper at "
        f"{iso_now()})---\n"
    )

    list_proc = cbq("list", "--json")
    parts.append("\n===== cbq list --json (live + parked) =====\n")
    parts.append((list_proc.stdout or list_proc.stderr or "")[:8000])

    blocked_proc = cbq("list", "--status", "blocked", "--json")
    parts.append("\n\n===== cbq list --status blocked --json =====\n")
    parts.append((blocked_proc.stdout or blocked_proc.stderr or "")[:4000])

    done_dir = workdir / "ml" / "eval" / "experiments" / "done"
    falsified_dir = workdir / "ml" / "eval" / "experiments" / "falsified"
    done_entries = _newest_first(done_dir)
    falsified_entries = _newest_first(falsified_dir)

    parts.append("\n\n===== ml/eval/experiments/done/ (newest first) =====\n")
    parts.append("\n".join(p.name for p in done_entries[:50]))
    parts.append("\n\n===== ml/eval/experiments/falsified/ (newest first) =====\n")
    parts.append("\n".join(p.name for p in falsified_entries[:50]))

    parts.append("\n\n===== 3 most recent done entries (each truncated to "
                 "6000 bytes) =====\n")
    for entry in done_entries[:3]:
        parts.append(f"\n--- {entry} ---\n")
        try:
            parts.append(entry.read_text(errors="replace")[:6000])
        except OSError:
            parts.append("(unreadable)")
        parts.append("\n")

    for header, path in (
        ("constraints.md",
         workdir / "ml" / "eval" / "experiments" / "constraints.md"),
        ("README.md",
         workdir / "ml" / "eval" / "experiments" / "README.md"),
        ("lib/README.md",
         workdir / "ml" / "eval" / "experiments" / "lib" / "README.md"),
    ):
        parts.append(f"\n===== ml/eval/experiments/{header} =====\n")
        try:
            parts.append(path.read_text(errors="replace")[:30000])
        except OSError:
            parts.append("(unreadable)")
        parts.append("\n")

    return "".join(parts)


def _newest_first(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    try:
        entries = [p for p in directory.iterdir() if p.is_file()]
    except OSError:
        return []
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return entries


def main(argv: Optional[list[str]] = None) -> int:
    SUGGEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CBQ_ACTOR", f"architect:{socket.gethostname()}")
    _refresh_oauth_token_env()

    suggest_kind = (os.environ.get("SUGGEST_KIND") or "").strip()
    suggest_machine = (os.environ.get("SUGGEST_MACHINE") or "a10").strip()
    submit_kind = suggest_kind or "research"

    machine_desc = _machine_desc(suggest_machine)
    kind_desc = _kind_desc(submit_kind)

    with open(SUGGEST_LOG, "a", buffering=1) as f:
        f.write(
            f"[{iso_now()}] auto-suggest starting "
            f"(kind={submit_kind} machine={suggest_machine})\n"
        )

    with cwd(WORKDIR):
        git_reset_to_main(cwd=WORKDIR)
        prompt = build_suggest_prompt(
            submit_kind=submit_kind, machine=suggest_machine,
            machine_desc=machine_desc, kind_desc=kind_desc, workdir=WORKDIR,
        )
        # The suggester writes plain text, not stream-json; the
        # ``--output-format text`` mode in the original. Use a thinner
        # subprocess path here since we don't need trajectory parsing
        # for this short-lived session.
        argv_cmd = [
            "claude", "-p", prompt,
            "--model", "claude-sonnet-4-6",
            "--permission-mode", "acceptEdits",
            "--dangerously-skip-permissions",
            "--max-turns", "40",
            "--output-format", "text",
        ]
        with open(SUGGEST_LOG, "a", buffering=1) as f:
            result = subprocess.run(
                argv_cmd, text=True, stdout=f, stderr=subprocess.STDOUT,
                env=os.environ,
            )
        rc = result.returncode

    with open(SUGGEST_LOG, "a", buffering=1) as f:
        f.write(f"[{iso_now()}] auto-suggest claude exited rc={rc}\n")

    new_task = _extract_last_submission(SUGGEST_LOG, tail_lines=50)
    if new_task:
        new_id = new_task.split("-", 1)[0]
        with open(SUGGEST_LOG, "a", buffering=1) as f:
            f.write(f"[{iso_now()}] auto-suggest submitted: {new_task}\n")
        slack_post(
            f":robot_face: *auto-suggest*: queued `{new_task}` "
            f"[kind={submit_kind} machine={suggest_machine}] — veto within "
            f"~30s with `cbq cancel {new_id}`"
        )
    else:
        with open(SUGGEST_LOG, "a", buffering=1) as f:
            f.write(f"[{iso_now()}] auto-suggest produced no submission\n")
    return 0


def _extract_last_submission(log_path: Path, *, tail_lines: int) -> Optional[str]:
    try:
        with open(log_path, "r") as f:
            lines = f.readlines()
    except OSError:
        return None
    last_match: Optional[str] = None
    for line in lines[-tail_lines:]:
        m = _SUBMITTED_PATTERN.search(line)
        if m:
            last_match = m.group(1)
    return last_match


def _refresh_oauth_token_env() -> None:
    oat = Path.home() / ".oat"
    if not oat.exists():
        return
    try:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oat.read_text().strip()
    except OSError:
        pass


_SUGGEST_PROMPT_HEAD = """You are the experiments GENERATOR. You fire only when all iteration has gone quiet: the per-experiment iterators (who handle the immediate next increment after each run) either closed their lines or have nothing pending. Unlike them, you see the WHOLE portfolio, and you have two moves — pick whichever is more valuable right now:

a. OPEN A NEW LINE: a different mechanism, model family, task family, or question — informed by everything in done/, falsified/, and the '## Line closed' reasons.
b. CONTINUE AN EXISTING LINE THE ITERATORS MISSED: a valuable angle on a prior experiment that never got queued — an open question an iterator overlooked, a cross-line comparison nobody ran, or a closed line whose '## Line closed' reasoning no longer holds (new constraints knowledge, a contradicting later result). If you reopen a closed line, your task's Goal section must quote the close reason and say why it no longer applies.

Either way: propose exactly ONE experiment and submit it to the queue.

You are inside the Coral-Bricks-AI/www repo on the main branch. The queue lives in Postgres, driven by the cbq CLI (on PATH):
- cbq list --json                          -> everything live or parked right now
- cbq history --grep '<regex>'             -> search prior verdicts (done + falsified)
- cbq list --status blocked --json         -> experiments waiting on a constraint to lift
- cbq submit <file> --origin auto-suggest [--parent NNNN]   -> queue your proposal; the ID is allocated for you and printed

DESIGN PROCESS (the inputs for steps 1-4 — README, constraints, done/falsified listings with the most recent entries, lib README, live queue — are EMBEDDED at the END of this prompt; start from those copies and open files only for older entries or anything marked truncated)

1. Read ml/eval/experiments/README.md fully, especially the "Lessons captured from prior experiments" section.
2. Read ml/eval/experiments/constraints.md — the practical constraints of the GPU worker box, maintained from real deviations. Anything you propose outside them will get overruled on the box.
3. List ml/eval/experiments/done/ and ml/eval/experiments/falsified/. Read the most recent 2-3 done entries and any falsified entries to understand the current state of the research. falsified/ records are tested-and-refuted hypotheses — do not re-propose them; blocked rows (cbq list --status blocked) are UNTESTED designs waiting on a constraint, not negatives.
4. Read ml/eval/experiments/lib/README.md so you know what primitives are already available.
5. Pick ONE next experiment that:
   - Tests a hypothesis the prior experiments raised but did not answer
   - Is feasible under ml/eval/experiments/constraints.md
   - Will complete in 30-45 min wall time
   - Has a clean accuracy/latency delta the user can read in a single glance
   - Includes some flavor of block attention if the prior work was about block attention [user stated focus]
   - Does NOT duplicate something already in done/ or falsified/ (check cbq history --grep too)
   - Does NOT exceed the budget that prior experiments fit within

6. Write the experiment as a markdown .task file with the same shape as recent done entries: yaml config block + sections for Goal, Method, Hypothesis. Save it to /tmp/suggest.task (NOT inside the repo).

7. Record the move and rationale in the task's Goal section: "new direction because ..." or "continuing #X because the iterators missed ...".

8. Submit it WITH THE FLAGS from the TARGET KIND section below:
   - New line:                cbq submit /tmp/suggest.task --slug <short-slug> --origin auto-suggest --kind <kind> --machine <machine>
   - Continuing line #NNNN:   cbq submit /tmp/suggest.task --slug <short-slug> --origin auto-suggest --kind <kind> --machine <machine> --parent NNNN
   The slug is hyphenated lowercase, describes the variable being tested, max ~40 chars. cbq prints 'submitted: <id>-<slug>' — include that line verbatim in your output.

9. Print a 2-3 sentence summary: what you proposed, why you chose it now, what specific question it answers.

DESIGN PRINCIPLES the user has been explicit about

- Block attention is the recurring theme. Every experiment should include some flavor of block attention or block-pattern manipulation.
- Position breakdown is required. results.json must report accuracy_by_position [or analog] per variant. Use lib.results.write_results which validates this.
- Pure SSM is not interesting. Hybrid attention plus SSM models are interesting.
- Sink+recent and fixed sliding window have been thoroughly characterized; the user said "the sink+recent experiments are not useful, we already have the learnings." Skip another budget-sweep or block-size-sweep of those.
- The interesting open questions as of the latest done entries are around adaptive selection: per-layer routing degradation [#0005 finding], per-head selection, training-free scorer alternatives, scoring functions [#0007 ablation territory], hybrid architectures, larger context lengths.

GUARDRAILS

- Propose EXACTLY ONE experiment, not a sweep of 5.
- Do NOT touch any files in the repo. Do NOT modify done/, falsified/, lib/, runner/, or the README. Your only queue write is the single cbq submit.
- If you cannot identify a useful next experiment [research is genuinely done for now], do not submit anything; just print "no suggestion: <one-line reason>" and exit cleanly.

You have at most 40 turns. Be focused.
"""


if __name__ == "__main__":
    sys.exit(main())
