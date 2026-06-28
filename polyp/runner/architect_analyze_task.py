"""``polyp.runner.architect_analyze_task`` -- ANALYZE phase.

Ported from ``architect_analyze_task.sh``. Runs on the CPU orchestrator
after the GPU worker finishes a job; the watcher already claimed
the row (status=analyzing).

- spawns an architect Claude session that reviews deviations, results,
  and records the verdict via ``cbq verdict ... --commit``;
- on wrapper failure: ``cbq analyze-failed`` (one retry via the
  executed queue, then parked as ``analyze_stuck``);
- logs uploaded to ``s3://${EXP_S3_BUCKET}/cb-queue/architect-logs/``.

Invoke as ``python -m polyp.runner.architect_analyze_task <id>``.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from reef.agents.headless_claude import HeadlessClaude, parse_stream_json_log

from polyp.runner.common import (
    TaskInfo,
    append_log,
    cbq,
    cbq_field,
    cbq_touch,
    cwd,
    env_int,
    git,
    git_reset_to_main,
    git_show_file,
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
EXPERIMENTS_DIR = os.environ.get("EXPERIMENTS_DIR", "ml/eval/experiments")
RESULTS_ROOT = EXPERIMENTS_DIR.rstrip("/") + "/results"


_RESPIN_PATTERN = re.compile(r"submitted:\s+([0-9][0-9.]*-[a-z0-9-]+)")


def build_analyze_prompt(
    *,
    task_info: TaskInfo,
    task_doc: str,
    attempt: int,
    max_attempts: int,
    prev_tail: str,
    prev_rc: int,
) -> str:
    tag = task_info.tag
    branch = task_info.branch
    results_dir = task_info.results_dir
    parts: list[str] = []
    parts.append(_ANALYZE_PROMPT_HEAD.format(
        tag=tag, id=task_info.id, branch=branch, results_dir=results_dir,
        experiments_dir=EXPERIMENTS_DIR,
    ))
    parts.append(f"\n---EXECUTED TASK {tag} (worker report included)---\n")
    parts.append(task_doc)
    parts.append("\n")

    if attempt > 1:
        cause = f"exited with code {prev_rc}"
        if prev_rc == 99:
            cause = (
                f"exited cleanly but the queue row for {tag} is still "
                f"'analyzing' — your job is to actually run the cbq verdict "
                f"command, not just describe it"
            )
        parts.append("\n---RETRY {a} of {m}---\n".format(a=attempt, m=max_attempts))
        parts.append(
            f"Your previous attempt {cause}. The checkout was reset to "
            f"origin/main.\n\n"
            f"Previous attempt's last output:\n"
            f"```\n{prev_tail or '(no assistant text captured)'}\n```\n"
        )

    parts.append("\n")
    parts.append(
        "---PRE-GATHERED ARTIFACTS (embedded by the launch wrapper; branch "
        "already fetched)---\n"
    )
    parts.append(f"\n===== git log origin/main..origin/{branch} --oneline =====\n")
    log_res = git("log", f"origin/main..origin/{branch}", "--oneline",
                  cwd=task_info.workdir)
    log_lines = (log_res.stdout or log_res.stderr or "").splitlines()[:40]
    parts.append("\n".join(log_lines))
    parts.append("\n")
    for path, cap, header in (
        (f"{results_dir}/deviations.md", 12000, "deviations.md"),
        (f"{results_dir}/run.spec", 4000, "run.spec"),
        (f"{results_dir}/results.json", 16000, "results.json (first 16000 bytes)"),
    ):
        parts.append(f"\n===== origin/{branch}:{header} =====\n")
        body = git_show_file(f"origin/{branch}", path, cwd=task_info.workdir)
        if body is None:
            parts.append(f"(git show failed for {path})")
        else:
            parts.append(body[:cap])
            if len(body) > cap:
                parts.append(
                    "\n...[TRUNCATED — git show the file for per-sample detail]"
                )
        parts.append("\n")
    parts.append(f"\n===== {EXPERIMENTS_DIR}/constraints.md (current main) =====\n")
    cons_path = task_info.workdir / EXPERIMENTS_DIR / "constraints.md"
    try:
        parts.append(cons_path.read_text()[:30000])
    except OSError:
        parts.append("(constraints.md unreadable)")
    return "".join(parts)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("Usage: python -m polyp.runner.architect_analyze_task <experiment-id>",
              file=sys.stderr)
        return 2
    id_ = argv[0]

    os.environ.setdefault("CBQ_ACTOR", f"architect:{os.uname().nodename}")
    _refresh_oauth_token_env()

    max_attempts = env_int("MAX_ATTEMPTS", 2)
    attempt_timeout_sec = env_int("ATTEMPT_TIMEOUT_SEC", 1800)

    s3_prefix = os.environ.get("EXP_S3_PREFIX", S3_PREFIX_DEFAULT)
    task = resolve_task(
        id_, workdir=WORKDIR, log_dir=LOG_DIR, index_path=INDEX_PATH,
        s3_prefix=s3_prefix, results_root=RESULTS_ROOT, phase="analyze",
    )
    slack_hook = route_slack_hook(task.kind)

    task_doc_proc = cbq("show", id_, "--markdown")
    if task_doc_proc.returncode != 0 or not (task_doc_proc.stdout or "").strip():
        log_line(f"ERROR: no markdown for {id_}", index_path=task.index_path)
        return 2
    task_doc = task_doc_proc.stdout

    log_line(f"ANALYZE-START {task.tag} {task.run_id}", index_path=task.index_path)

    driver = HeadlessClaude(
        model="claude-sonnet-4-6",
        max_turns=80,
        timeout_s=attempt_timeout_sec,
        kill_after_s=60,
        permission_mode="acceptEdits",
        skip_permissions=True,
        cwd=task.workdir,
        oauth_token_path=Path.home() / ".oat",
        langfuse_pipeline="architect-analyze",
    )

    total_start = time.time()
    final_rc = 1
    attempts_used = 0
    prev_rc = 0
    prev_tail = ""
    recorded = ""

    with cwd(task.workdir):
        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            append_log(task.log_path, f"===== ATTEMPT {attempt} START =====")
            log_line(
                f"ANALYZE-ATTEMPT {attempt}/{max_attempts} {task.tag}",
                index_path=task.index_path,
            )
            cbq_touch(id_)
            git_reset_to_main(cwd=task.workdir)
            git("fetch", "origin",
                f"+refs/heads/{task.branch}:refs/remotes/origin/{task.branch}",
                "--quiet", cwd=task.workdir)

            prompt = build_analyze_prompt(
                task_info=task, task_doc=task_doc, attempt=attempt,
                max_attempts=max_attempts, prev_tail=prev_tail, prev_rc=prev_rc,
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
                    f"ANALYZE-ATTEMPT-TIMEOUT {task.tag} attempt={attempt} "
                    f"after {attempt_timeout_sec}s (retry if attempts remain)",
                    index_path=task.index_path,
                )
            append_log(
                task.log_path,
                f"===== ATTEMPT {attempt} END (rc={rc}, {attempt_dur}s) =====",
            )

            status = cbq_field(id_, "status")
            if status in ("done", "falsified", "blocked"):
                recorded = status
                final_rc = 0
                break
            prev_rc = 99 if rc == 0 else rc
            prev_tail = result.tail(3500)

    total_dur = int(time.time() - total_start)

    s3_uploaded = s3_cp(task.log_path, task.s3_log_url)
    s3_url = task.s3_log_url if s3_uploaded else "(s3 upload failed)"

    # Detect any respin id the analyzer announced ("submitted: NNN-slug").
    _, assistant_text = parse_stream_json_log(task.log_path)
    respin_match = None
    for m in _RESPIN_PATTERN.finditer(assistant_text or ""):
        respin_match = m.group(1)
    respin = respin_match or "none"

    if final_rc == 0:
        log_line(
            f"ANALYZE-OK {task.tag} {task.run_id} verdict={recorded} "
            f"respin={respin} total_dur={total_dur}s",
            index_path=task.index_path,
        )
        emoji, detail = _verdict_emoji_detail(recorded, task)
        slack_post(
            f"{emoji} *architect-box* *ID:* `{task.tag}` analyzed → {recorded}\n"
            f"task: `{task.slug}`\n"
            f"respin: {respin}\n"
            f"{detail}\n"
            f"branch: <https://github.com/Coral-Bricks-AI/www/tree/{task.branch}|"
            f"`{task.branch}`>\n"
            f"total: {total_dur}s · log: `{s3_url}`",
            hook_url=slack_hook,
        )
        return 0

    log_line(
        f"ANALYZE-FAIL {task.tag} {task.run_id} "
        f"attempts={attempts_used}/{max_attempts} total_dur={total_dur}s",
        index_path=task.index_path,
    )
    last_tail = (assistant_text or "")[-800:]
    res = cbq("analyze-failed", id_,
              "--error", f"analyze wrapper rc={final_rc}")
    outcome_msg = (res.stdout or res.stderr or "cbq analyze-failed errored")
    log_line(outcome_msg.strip(), index_path=task.index_path)
    refile_note = (
        f"PARKED as analyze_stuck — GPU data intact on `{task.branch}`; "
        f"a human can verdict from the execution report, or "
        f"`cbq requeue {task.id}` for a fresh analyze round"
        if "analyze_stuck" in outcome_msg
        else "returned to executed for one more analyze round"
    )

    slack_post(
        f":warning: *architect-box* *ID:* `{task.tag}` ANALYZE phase failed "
        f"after {attempts_used}/{max_attempts} attempts — {refile_note}\n"
        f"task: `{task.slug}`\n"
        f"log: `{s3_url}` (also `~/architect/logs/{task.run_id}.jsonl` on "
        f"`{task.host}`)\n"
        f"last attempt tail:\n```\n{last_tail or '(no assistant text)'}\n```",
        hook_url=slack_hook,
    )
    return 1


def _verdict_emoji_detail(verdict: str, task: TaskInfo) -> tuple[str, str]:
    if verdict == "done":
        return ":white_check_mark:", f"recorded: `done/{task.id}-{task.slug}.task`"
    if verdict == "falsified":
        return (
            ":no_entry_sign:",
            f"recorded: `falsified/{task.id}-{task.slug}.task` "
            f"(hypothesis refuted)",
        )
    if verdict == "blocked":
        cnst = cbq_field(task.id, "blocked_on") or "?"
        return (
            ":construction:",
            f"blocked on `{cnst}` — no archive record; resurrect via "
            f"`cbq list --blocked-on {cnst}`",
        )
    return ":grey_question:", f"recorded: {verdict}"


def _refresh_oauth_token_env() -> None:
    oat = Path.home() / ".oat"
    if not oat.exists():
        return
    try:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oat.read_text().strip()
    except OSError:
        pass


_ANALYZE_PROMPT_HEAD = """You are the ARCHITECT (analyze phase) for the experiments pipeline, on the CPU orchestrator box, inside Coral-Bricks-AI/www on main. The GPU worker finished executing experiment {tag}; its execution report is in the task document below. Your job: review what ACTUALLY happened, record the verdict in the queue database via cbq, extract lessons, and decide what is next.

QUEUE COMMANDS AVAILABLE (the 'cbq' CLI is on PATH; the row for {tag} is claimed by you, status 'analyzing'):
- cbq verdict {id} done --review-file <f> --commit
- cbq verdict {id} falsified --review-file <f> --commit
- cbq verdict {id} blocked --constraint C-<n> --review-file <f>
- cbq submit <task-file> --parent {id} --origin respin     (enqueue a follow-up; the id is allocated for you)
- cbq history --grep '<regex>'                              (search prior verdicts)
The database row is AUTHORITATIVE. 'done' and 'falsified' render an archive file (<project>/experiments/done/ or falsified/) and commit it to main together with any constraints.md edits in your checkout — that is what --commit does. 'blocked' writes NO archive file by design.

THE WORKER HAS FULL AUTHORITY TO DEVIATE. It may have changed knobs, code, or the experiment design itself — legitimately, because it is the one in contact with the hardware. Your review of those deviations comes BEFORE you read any metric. Judge whether the original instructions ever made sense under practical constraints, not whether the worker was obedient.

STEP 0 — FETCH: already done by the launch wrapper (origin/{branch} is up to date) and the key artifacts — branch git log, deviations.md, run.spec, results.json, constraints.md — are EMBEDDED at the END of this prompt. Start from those copies. All artifacts live at {results_dir}/ on that branch; for anything not embedded or marked truncated (run.py, commit diffs, per-sample detail), read via git show origin/{branch}:<path> (do not check the branch out; stay on main).

STEP 1 — DEVIATIONS FIRST. Read deviations.md before anything else. Then cross-check it against reality: git log origin/main..origin/{branch} --oneline and inspect the diffs of commits tagged [worker]. If the report and the diff disagree, trust the diff and note the gap. For EACH deviation decide:
  - ACCEPT: the result still answers the original question.
  - REFRAME: the result answers a DIFFERENT question than the task claimed. The permanent record must state what was ACTUALLY tested — rewrite the headline claim and the hypothesis verdict accordingly. Never file a result under the original claim when the condition changed (a shrunk seq-len, a swapped model, a changed sampling regime all change the question).
  - RESPIN: the deviation gutted the experiment; a follow-up is needed that respects the constraint the worker hit. Submit it with --parent {id}.
A missing deviations.md when [worker] commits exist is itself a finding — reconstruct the deviations from the diff and say so in your review.

STEP 2 — only now read results.json. Interpret against the hypothesis and the run.spec success_criteria, AS REFRAMED by step 1. Sanity-check per_sample and the by-position/by-axis breakdowns for the failure modes the experiments README warns about (bimodal collapse hidden by averages, model emitting first-token-and-stop, etc.).

STEP 3 — CONSTRAINTS. Any deviation or failure caused by a wrong architect-side assumption becomes ONE terse bullet appended to {experiments_dir}/constraints.md. That file is read at design time by the next code phase and the auto-suggester — it is how the pipeline stops issuing specs the box cannot honor. Do not duplicate bullets that are already there. New bullets that a 'blocked' verdict will reference MUST carry a C-<n> id: '- C-<n>: <fact>' where <n> is one more than the highest existing C-number (start at C-100 if none exist). Edit the file in this checkout; do NOT commit it yourself — cbq verdict --commit picks it up in the same commit as the archive record (the paired write).

STEP 4 — LIB PROMOTION (successful experiments only). Scan the branch for NEW reusable, non-experiment-specific utilities. Promote clean versions to {experiments_dir}/lib/ on main, update lib/README.md, commit and push that separately yourself. Skip if nothing reusable.

STEP 5 — VERDICT. Write your review to a temp file (e.g. /tmp/review-{id}.md): a '## Architect review' section containing per-deviation verdicts (ACCEPT/REFRAME/RESPIN with one line of reasoning each), the reframed claim if any, headline numbers, and your interpretation. Then record EXACTLY ONE verdict:
  - Status completed and the result is meaningful (possibly reframed) and CONFIRMS or extends the hypothesis: cbq verdict {id} done --review-file <f> --commit
  - The hypothesis was TESTED AND REFUTED (a real negative result — this is valuable, it is what stops future designs from re-running the idea): cbq verdict {id} falsified --review-file <f> --commit
  - The question could NOT be tested under a practical constraint (escalated runs usually land here): FIRST append the C-<n> constraint bullet to constraints.md and commit+push it yourself with message 'constraint C-<n> from {tag} [architect]', THEN: cbq verdict {id} blocked --constraint C-<n> --review-file <f>. The experiment stays resurrectable: when the constraint lifts, 'cbq list --blocked-on C-<n>' finds it. Do NOT file untestable work as falsified — that poisons the negative-knowledge archive with claims that were never tested.
  Distinguish carefully: 'falsified' means the experiment RAN and the answer was no. 'blocked' means the experiment could not run as designed. A REFRAMEd partial result that did test something goes to done/ or falsified/ on its merits.

STEP 6 — ITERATE OR CLOSE — mandatory; decide explicitly every time, in addition to the verdict. You are the ITERATOR for this line of work: brand-new lines come from the idle-time generator, never from you; you always build on the existing experiment.
  ITERATE (the default): write exactly ONE follow-up task file and submit it: cbq submit /tmp/respin-{id}.task --parent {id} --origin respin. It must build directly on THIS result: vary one knob, chase the specific open question this run raised, or fix what blocked it — within constraints.md. Submit it QUICKLY: the GPUs idle when iterators dawdle, so a focused increment now beats a polished proposal later.
  CLOSE THE LINE: if nothing further on this line is worth GPU time (question answered, direction falsified, diminishing returns, constraint wall), submit NOTHING and include a '## Line closed' section in your review file stating the reason and what evidence would justify reopening it.

STEP 7 — print a 3-sentence summary: verdict, the key deviations and your ruling on them, and your iterate-or-close decision with the new id if you iterated (cbq submit prints it).

Do NOT modify anything on the exp branch. Do NOT touch other experiments' directories. Do NOT run cbq claim/unclaim/requeue — the watcher owns those.
"""


if __name__ == "__main__":
    sys.exit(main())
