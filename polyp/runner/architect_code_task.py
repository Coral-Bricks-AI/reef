"""``polyp.runner.architect_code_task`` -- CODE phase of the architect pipeline.

Ported from ``architect_code_task.sh``. Runs on the CPU-only orchestrator
box after the watcher claims a row (status=coding):

- spawn an architect Claude session that writes ``run.py`` + ``run.spec``
  on a fresh ``exp/<id>-<slug>`` branch and pushes the branch (code only,
  no runs);
- on success: ``cbq ready <id>`` records the handoff;
- on final failure: ``cbq code-failed <id>`` parks the row, then a short
  Claude session extracts any durable box/toolchain constraint into
  ``constraints.md``.

Logs are uploaded to ``s3://${EXP_S3_BUCKET}/cb-queue/architect-logs/``.
Invoke as ``python -m polyp.runner.architect_code_task <id>``.
"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from reef.agents.headless_claude import HeadlessClaude, TIMEOUT_EXIT_CODE

from polyp.runner.common import (
    TaskInfo,
    append_log,
    cbq,
    cbq_field,
    cbq_touch,
    cwd,
    env_int,
    git,
    git_remote_branch_exists,
    git_remote_branch_sha,
    git_reset_to_main,
    git_show_file,
    iso_now,
    log_line,
    resolve_task,
    route_slack_hook,
    s3_cp,
    slack_post,
)


WORKDIR = Path(os.environ.get(
    "WORKDIR", str(Path.home() / "architect" / "workdir" / "www")
)).expanduser()
LOG_DIR = Path(os.environ.get(
    "ARCHITECT_LOG_DIR", str(Path.home() / "architect" / "logs")
)).expanduser()
INDEX_PATH = Path(os.environ.get(
    "ARCHITECT_INDEX", str(Path.home() / "architect" / "INDEX.log")
)).expanduser()
S3_PREFIX_DEFAULT = (
    f"s3://{os.environ.get('EXP_S3_BUCKET', '')}/cb-queue/architect-logs"
)
RESULTS_ROOT = os.environ.get(
    "EXPERIMENTS_DIR", "ml/eval/experiments"
).rstrip("/") + "/results"


def build_code_prompt(
    *,
    task_info: TaskInfo,
    task_body: str,
    attempt: int,
    max_attempts: int,
    prev_tail: str,
    prev_rc: int,
) -> str:
    """Assemble the architect-code prompt.

    Embeds the READ FIRST reference docs so the session starts working
    instead of re-opening the same three files every run; appends
    previous-attempt context on retries.
    """
    branch = task_info.branch
    results_dir = task_info.results_dir
    tag = task_info.tag

    parts: list[str] = []
    parts.append(_CODE_PROMPT_HEAD.format(
        branch=branch, results_dir=results_dir,
    ))
    parts.append(f"\n---TASK {tag}---\n")
    parts.append(task_body)
    parts.append("\n")

    if attempt > 1:
        cause = f"exited with code {prev_rc}"
        if prev_rc == 99:
            cause = (
                "exited cleanly but did not push the branch — your job is to "
                "actually commit + push, not just describe what you would do"
            )
        elif prev_rc == 98:
            cause = (
                f"pushed the branch but it is missing run.py and/or run.spec "
                f"in {results_dir}/ — both deliverables are mandatory"
            )
        parts.append("\n---RETRY {a} of {m}---\n".format(a=attempt, m=max_attempts))
        parts.append(
            f"Your previous attempt {cause}. The branch '{branch}' was reset "
            f"to origin/main; you are starting from a clean state.\n\n"
            f"DEBUG MODE: read the previous attempt's output carefully. "
            f"Diagnose the root cause. Fix it. Do not just repeat the same "
            f"steps.\n\n"
            f"Previous attempt's last output:\n"
            f"```\n{prev_tail or '(no assistant text captured)'}\n```\n"
        )

    parts.append("\n")
    parts.append(
        "---PRE-GATHERED REFERENCE (embedded by the launch wrapper: "
        "the READ FIRST 1-3 files, from current origin/main)---\n"
    )
    for ref in (
        "ml/eval/experiments/constraints.md",
        "ml/eval/experiments/README.md",
        "ml/eval/experiments/lib/README.md",
    ):
        parts.append(f"\n===== {ref} =====\n")
        body = _read_capped(task_info.workdir / ref, cap=30000)
        parts.append(body or "(unreadable)")
        parts.append("\n")
    return "".join(parts)


def build_constraint_prompt(*, task_info: TaskInfo, attempts_used: int,
                            note_body: str) -> str:
    return _CONSTRAINT_PROMPT_HEAD.format(
        tag=task_info.tag,
        slug=task_info.slug,
        attempts_used=attempts_used,
    ) + "\n---FAILURE RECORD---\n" + note_body


def _read_capped(path: Path, *, cap: int) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(cap + 1)
    except OSError:
        return ""
    if len(data) > cap:
        return data[:cap].decode("utf-8", errors="replace") + (
            "\n...[TRUNCATED — open the file for the rest]"
        )
    return data.decode("utf-8", errors="replace")


def attempt_deliverables_ok(
    *, branch: str, results_dir: str, log_path: Path, cwd_path: Path,
) -> tuple[bool, bool]:
    """Return ``(branch_pushed, deliverables_present)``."""
    if not git_remote_branch_exists(branch, cwd=cwd_path):
        return False, False
    # Refetch the branch explicitly (shallow clones only follow main by default).
    git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        "--quiet", cwd=cwd_path)
    deliverables = (
        git_show_file(f"origin/{branch}", f"{results_dir}/run.py", cwd=cwd_path)
        is not None
        and git_show_file(f"origin/{branch}", f"{results_dir}/run.spec",
                          cwd=cwd_path) is not None
    )
    return True, deliverables


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("Usage: python -m polyp.runner.architect_code_task <experiment-id>",
              file=sys.stderr)
        return 2
    id_ = argv[0]

    actor = os.environ.setdefault(
        "CBQ_ACTOR", f"architect:{os.uname().nodename}"
    )
    _refresh_oauth_token_env()

    max_attempts = env_int("MAX_ATTEMPTS", 3)
    attempt_timeout_sec = env_int("ATTEMPT_TIMEOUT_SEC", 1800)

    s3_prefix = os.environ.get("EXP_S3_PREFIX", S3_PREFIX_DEFAULT)
    task = resolve_task(
        id_, workdir=WORKDIR, log_dir=LOG_DIR, index_path=INDEX_PATH,
        s3_prefix=s3_prefix, results_root=RESULTS_ROOT,
    )
    # CODE phase always uses fresh ``exp/<id>-<slug>``; the cbq row may
    # not have a branch yet.
    task = TaskInfo(
        id=task.id, slug=task.slug, kind=task.kind, tag=task.tag,
        branch=f"exp/{task.id}-{task.slug}", workdir=task.workdir,
        results_dir=task.results_dir, run_id=task.run_id,
        log_path=task.log_path, s3_log_url=task.s3_log_url,
        index_path=task.index_path, host=task.host,
    )

    slack_hook = route_slack_hook(task.kind)

    task_body = cbq_field(id_, "task_md")
    if task_body is None:
        log_line(f"ERROR: no task body for {id_}", index_path=task.index_path)
        return 2

    log_line(f"CODE-START {task.tag} {task.run_id}", index_path=task.index_path)

    driver = HeadlessClaude(
        model="claude-sonnet-4-6",
        max_turns=150,
        timeout_s=attempt_timeout_sec,
        kill_after_s=60,
        permission_mode="acceptEdits",
        skip_permissions=True,
        cwd=task.workdir,
        oauth_token_path=Path.home() / ".oat",
        langfuse_pipeline="architect-code",
    )

    total_start = time.time()
    final_rc = 1
    attempts_used = 0
    attempt_summaries: list[str] = []
    prev_rc = 0
    prev_tail = ""

    with cwd(task.workdir):
        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            append_log(task.log_path, f"===== ATTEMPT {attempt} START =====")
            log_line(
                f"CODE-ATTEMPT {attempt}/{max_attempts} {task.tag}",
                index_path=task.index_path,
            )

            cbq_touch(id_)
            git_reset_to_main(cwd=task.workdir)
            # Delete the local + remote branch from any prior attempt.
            git("branch", "-D", task.branch, cwd=task.workdir)
            git("push", "origin", "--delete", task.branch, cwd=task.workdir)
            git("checkout", "-b", task.branch, cwd=task.workdir)

            results_path = task.workdir / task.results_dir
            results_path.mkdir(parents=True, exist_ok=True)
            (results_path / "task.md").write_text(task_body)

            prompt = build_code_prompt(
                task_info=task,
                task_body=task_body,
                attempt=attempt,
                max_attempts=max_attempts,
                prev_tail=prev_tail,
                prev_rc=prev_rc,
            )

            attempt_start = time.time()
            result = driver.invoke(
                prompt=prompt,
                log_path=task.log_path,
                attempt_label=str(attempt),
            )
            attempt_dur = int(time.time() - attempt_start)
            rc = result.exit_code
            if result.timed_out:
                log_line(
                    f"CODE-ATTEMPT-TIMEOUT {task.tag} attempt={attempt} "
                    f"after {attempt_timeout_sec}s (retry if attempts remain)",
                    index_path=task.index_path,
                )

            pushed, deliverables = attempt_deliverables_ok(
                branch=task.branch, results_dir=task.results_dir,
                log_path=task.log_path, cwd_path=task.workdir,
            )
            attempt_summaries.append(
                f"attempt {attempt}: rc={rc} "
                f"pushed={'yes' if pushed else 'no'} "
                f"deliverables={'yes' if deliverables else 'no'} "
                f"dur={attempt_dur}s"
            )
            append_log(
                task.log_path,
                f"===== ATTEMPT {attempt} END (rc={rc}, "
                f"pushed={'yes' if pushed else 'no'}, "
                f"deliverables={'yes' if deliverables else 'no'}, "
                f"{attempt_dur}s) =====",
            )

            if rc == 0 and deliverables:
                final_rc = 0
                break
            if rc == 0 and not pushed:
                prev_rc = 99
            elif rc == 0 and not deliverables:
                prev_rc = 98
            else:
                prev_rc = rc
            prev_tail = result.tail(3500)

    total_dur = int(time.time() - total_start)

    s3_uploaded = s3_cp(task.log_path, task.s3_log_url)
    s3_url = task.s3_log_url if s3_uploaded else "(s3 upload failed)"

    sha = git_remote_branch_sha(task.branch, cwd=task.workdir) or ""

    if final_rc == 0:
        return _on_success(
            task=task, attempts_used=attempts_used, max_attempts=max_attempts,
            total_dur=total_dur, sha=sha, s3_url=s3_url, slack_hook=slack_hook,
        )
    return _on_failure(
        task=task, driver=driver, attempts_used=attempts_used,
        max_attempts=max_attempts, total_dur=total_dur, final_rc=final_rc,
        attempt_summaries=attempt_summaries, s3_url=s3_url,
        slack_hook=slack_hook,
    )


def _on_success(
    *,
    task: TaskInfo,
    attempts_used: int,
    max_attempts: int,
    total_dur: int,
    sha: str,
    s3_url: str,
    slack_hook: Optional[str],
) -> int:
    spec_body = git_show_file(
        f"origin/{task.branch}", f"{task.results_dir}/run.spec",
        cwd=task.workdir,
    ) or ""
    spec_gpus = _spec_field(spec_body, "gpus") or "1"
    spec_timeout = _spec_field(spec_body, "timeout_min") or "90"

    log_line(
        f"CODE-OK {task.tag} {task.run_id} attempts={attempts_used}/{max_attempts} "
        f"branch={task.branch} total_dur={total_dur}s",
        index_path=task.index_path,
    )

    res = cbq(
        "ready", task.id,
        "--branch", task.branch,
        "--sha", sha or "unknown",
        "--gpus", spec_gpus,
        "--timeout-min", spec_timeout,
        "--log-url", s3_url,
        "--attempts", str(attempts_used),
    )
    if res.returncode != 0:
        log_line(
            f"WARN: cbq ready failed for {task.tag} — row left in coding; reap "
            f"will requeue",
            index_path=task.index_path,
        )
    else:
        slack_post(
            f":package: *architect-box* *ID:* `{task.tag}` coded → ready\n"
            f"task: `{task.slug}`\n"
            f"branch: <https://github.com/Coral-Bricks-AI/www/tree/{task.branch}|"
            f"`{task.branch}`> @ `{sha}` · gpus: {spec_gpus} · "
            f"timeout: {spec_timeout}m\n"
            f"total: {total_dur}s · attempts: {attempts_used}\n"
            f"log: `{s3_url}` (also `~/architect/logs/{task.run_id}.jsonl` on "
            f"`{task.host}`)",
            hook_url=slack_hook,
        )
    return 0


def _on_failure(
    *,
    task: TaskInfo,
    driver: HeadlessClaude,
    attempts_used: int,
    max_attempts: int,
    total_dur: int,
    final_rc: int,
    attempt_summaries: list[str],
    s3_url: str,
    slack_hook: Optional[str],
) -> int:
    log_line(
        f"CODE-FAIL {task.tag} {task.run_id} "
        f"attempts={attempts_used}/{max_attempts} rc={final_rc} "
        f"total_dur={total_dur}s",
        index_path=task.index_path,
    )
    # Re-parse the last attempt's tail from the log; the driver-returned
    # value is from inside the loop but we may have written more since.
    from reef.agents.headless_claude import parse_stream_json_log
    _, assistant_text = parse_stream_json_log(
        task.log_path, attempt_label=str(attempts_used),
    )
    last_tail = assistant_text[-800:] if assistant_text else ""

    note_path = Path(tempfile.mkstemp(prefix=f"code-fail-{task.id}.",
                                      suffix=".md")[1])
    note_lines = [
        "## Code-phase failure summary (auto-appended by architect runner)",
        "",
        f"- Run ID: `{task.run_id}`",
        "- Phase: CODE (architect box, no execution attempted)",
        f"- Attempts: {attempts_used}/{max_attempts}",
        f"- Total wall: {total_dur}s",
        f"- Final rc: {final_rc}",
        f"- Log (S3): `{s3_url}`",
        "",
        "### Per-attempt",
    ]
    note_lines.extend(f"- {line}" for line in attempt_summaries)
    note_lines += [
        "",
        "### Last attempt tail",
        "```",
        last_tail or "(no assistant text)",
        "```",
    ]
    note_path.write_text("\n".join(note_lines))

    res = cbq(
        "code-failed", task.id,
        "--note-file", str(note_path),
        "--log-url", s3_url,
        "--attempts", str(attempts_used),
        "--error", f"code phase rc={final_rc}",
    )
    if res.returncode != 0:
        log_line(f"WARN: cbq code-failed failed for {task.tag}",
                 index_path=task.index_path)

    # Constraint extraction pass.
    with cwd(task.workdir):
        git_reset_to_main(cwd=task.workdir)
        constraint_prompt = build_constraint_prompt(
            task_info=task, attempts_used=attempts_used,
            note_body=note_path.read_text(),
        )
        constraint_driver = HeadlessClaude(
            model="claude-sonnet-4-6",
            max_turns=15,
            timeout_s=300,
            kill_after_s=60,
            permission_mode="acceptEdits",
            skip_permissions=True,
            cwd=task.workdir,
            oauth_token_path=Path.home() / ".oat",
            langfuse_pipeline="architect-constraint-extract",
        )
        constraint_driver.invoke(
            prompt=constraint_prompt,
            log_path=task.log_path,
            attempt_label="constraint",
        )
        s3_cp(task.log_path, task.s3_log_url)

    summary_lines = "\n".join(attempt_summaries)
    slack_post(
        f":x: *architect-box* *ID:* `{task.tag}` CODE phase FAILED after "
        f"{attempts_used}/{max_attempts} attempts — PARKED (code_failed)\n"
        f"task: `{task.slug}`\n"
        f"total: {total_dur}s · final rc: {final_rc}\n"
        f"{summary_lines}\n"
        f"no auto-retry: the in-phase attempts already retried with failure "
        f"context. Respin with a CHANGED task: "
        f"`cbq show {task.id} --field failure_md`, edit, "
        f"`cbq submit <file> --parent {task.id.split('.', 1)[0]}`.\n"
        f"log: `{s3_url}`\n"
        f"last attempt tail:\n```\n{last_tail or '(no assistant text)'}\n```",
        hook_url=slack_hook,
    )
    return 1


def _refresh_oauth_token_env() -> None:
    """Mirror the bash ``export CLAUDE_CODE_OAUTH_TOKEN="$(cat ~/.oat)"`` line.

    Allows callers (or systemd) that don't set the env var directly to
    still authenticate Claude Code. The HeadlessClaude driver also reads
    ``~/.oat`` per invocation; this just keeps the parent process env
    in sync for any other tool that needs it.
    """
    oat = Path.home() / ".oat"
    if not oat.exists():
        return
    try:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oat.read_text().strip()
    except OSError:
        pass


def _spec_field(spec_body: str, key: str) -> Optional[str]:
    """Pull a scalar field from a YAML run.spec via regex (no YAML dep)."""
    import re
    pat = re.compile(rf"^{re.escape(key)}:\s*([0-9]+)", re.MULTILINE)
    m = pat.search(spec_body)
    return m.group(1) if m else None


# Prompt templates kept at module bottom so the control-flow above reads
# top-down. Triple-quoted, unformatted by design — the .format() call
# fills in branch / results_dir / etc.
_CODE_PROMPT_HEAD = """You are the ARCHITECT for the experiments pipeline, running on a CPU-only orchestrator box. There is NO GPU here. You are inside Coral-Bricks-AI/www on a fresh branch '{branch}'. Your job is to PREPARE the experiment described below so a separate GPU worker can execute it without you: write the code and the run spec, push the branch. Do NOT attempt to execute the experiment, load models, start inference servers, or install GPU/CUDA wheels on this box.

Working directory for this experiment: {results_dir}/
The original task file is already copied there as task.md.

READ FIRST, in order (items 1-3 are EMBEDDED at the END of this prompt — use the embedded copies; re-open a file only if its embed is marked truncated):
1. ml/eval/experiments/constraints.md — practical constraints of the GPU worker box, learned from prior runs. Design within them: specs that ignore them get overruled by the worker on the box.
2. ml/eval/experiments/README.md — required reporting schema ('Required reporting shape').
3. ml/eval/experiments/lib/README.md — battle-tested primitives (niah generators, attention helpers, GpuMonitor, results.write_results, progress.ProgressLog, and `exp_setup` which bundles every recurring HF-cache / LoRA-attach / dataset-load / mark-done fix into one import — use it). Import these instead of re-deriving them.
4. If the task builds on prior experiments (references like #NNNN, or asks to 'leverage prior experiments'), read the relevant entries in ml/eval/experiments/done/ and ml/eval/experiments/falsified/ (confirmed and refuted results respectively). 'cbq history --grep <regex>' searches both plus parked records.

DELIVERABLES — both files in {results_dir}/ on this branch:

1. run.py — the complete experiment.
   - Self-contained entrypoint the worker runs as: python run.py [flags]
   - The worker invokes run.py from the results dir, not the repo root. Any `from ml.eval.experiments.lib...` (or other `ml.*`) import requires inserting the repo root onto sys.path BEFORE the import line; without it the smoke fails fast with ModuleNotFoundError.
   - For repeatable setup (env, model load, dataset, score, mark-done), import helpers from your project's lib/ package rather than re-deriving each time. Every novel failure should land back into that lib so the next run inherits the fix.
   - MUST support a --smoke flag: an end-to-end pass finishing in under ~3 GPU-minutes (model load + 1-2 samples + a results-shaped write to smoke_results.json). The smoke is what catches import errors, OOM-at-load, path bugs and shape errors before the full run burns a GPU slot — make it exercise the real code path, not a stub.
   - Expose operational knobs as CLI flags with sane defaults (for example --batch-size, --seq-len, --n-samples, --gpu-mem-util) so the worker can tune without editing code.
   - Emit per-sample progress via lib.progress.ProgressLog to progress.log in the working directory.
   - Write final metrics with lib.results.write_results (validates the reporting schema, including accuracy_by_position for retrieval-style evals).
   - Top-of-file docstring states the experiment INTENT: hypothesis, success criteria, and which parameters are LOAD-BEARING for the comparison (the worker reads this before changing anything).

2. run.spec — YAML, the worker's operational contract:
     entry: run.py
     venv: sparse-attn            # shared venv name under ~/queue/venvs/ on the GPU box
     setup: |                     # idempotent shell; worker runs it if the venv or deps are missing
       pip install --index-url https://download.pytorch.org/whl/cu128 torch
       pip install transformers datasets
     gpus: 1                      # how MANY GPUs the run needs; the worker picks WHICH ones
     timeout_min: 90              # wall-clock budget for the full run
     smoke: '--smoke'             # flag string for the smoke pass; omit only if a smoke is truly impossible
     smoke_timeout_min: 5
     artifacts: [results.json, progress.log]
     success_criteria: >
       One or two sentences the worker can check before finalizing
       (e.g. 'sparse variant within 10pp of dense overall, by-position reported').
   Never name physical GPU ids anywhere in code or spec — the worker owns placement and sets CUDA_VISIBLE_DEVICES.

VALIDATE WHAT YOU CAN ON CPU: at minimum run python3 -m py_compile on run.py. If pure-python pieces (sample generators, scorers, aggregation) are cheap to unit-test here, do it. Anything that needs torch/CUDA is exactly what the --smoke pass surfaces on the GPU box — do not try it here.

THE WORKER MAY OVERRULE YOU. A worker Claude on the GPU box executes this with full authority to change anything — knobs, code, even the experiment design — when instructions do not survive practical constraints. Every change it makes is documented in deviations.md and reviewed by the architect afterwards. So: make intent explicit in the docstring, mark what is load-bearing, and design to constraints.md so the worker does not have to deviate.

When you finish:
1. git add {results_dir}/
2. git commit with a descriptive message including '#<id> [architect]'
3. git push -u origin '{branch}'
4. Print a 1-paragraph summary: what the experiment tests, key design choices, what the worker should expect.

Do NOT merge to main. Do NOT touch main at all — the runner records the handoff in the queue database itself. Do NOT touch other experiments' directories. Do NOT run any cbq state-changing command (claim/ready/verdict/...) — the runner owns queue state for this phase.
"""


_CONSTRAINT_PROMPT_HEAD = """You are the ARCHITECT doing a 2-minute post-mortem of a CODE-phase failure in the experiments pipeline. You are inside Coral-Bricks-AI/www on main. Experiment {tag} ({slug}) failed its code phase after {attempts_used} attempts.

Read the failure record below and ml/eval/experiments/constraints.md. Decide: does this failure reveal a DURABLE constraint of the GPU box, toolchain, or harness that is NOT already captured in constraints.md? (Examples: a library that cannot install, a CUDA/toolkit mismatch, a turn-budget pattern.) Transient flakes, task-specific bugs, and anything already covered do NOT qualify.

- If yes: append exactly ONE terse bullet to ml/eval/experiments/constraints.md under the right section, formatted '- C-<n>: <fact> (from {tag} code failure)' where <n> is one more than the highest existing C-number in the file (start at C-100 if none exist). Then git add, git commit -m 'constraint from code failure {tag} [architect]', git push origin main (on rejection: git pull --rebase and retry).
- If no: change nothing and print 'no durable constraint'.
"""


if __name__ == "__main__":
    sys.exit(main())
