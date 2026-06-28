"""``polyp.runner.worker_shift`` -- one bounded worker shift on the GPU box.

Ported from ``worker_shift.sh``. Invoked by the worker watchdog on
events (a free GPU + ready spec, a dead pid, a stalled progress.log,
heartbeat). Reconciles state, finalizes finished jobs, fixes failed
ones, observes running ones, schedules new ones onto free GPUs.

Durable state lives on disk (``~/worker/slots.json``, per-job
worktrees), in the queue (via cbq), and on exp/ git branches; jobs run
as nohup'd background processes that survive between shifts.

Invoke as ``python -m polyp.runner.worker_shift [reason]``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from reef.agents.headless_claude import HeadlessClaude

from polyp.runner.common import (
    cbq,
    iso_now,
    log_line,
    route_slack_hook,
    s3_cp,
    slack_post,
)


BASE = Path(os.environ.get("WORKER_BASE", str(Path.home() / "worker" / "www")))
WORKER_HOME = BASE.parent
WORKER_GPUS = os.environ.get("WORKER_GPUS", "0")
INDEX_PATH = WORKER_HOME / "INDEX.log"
LOG_DIR = WORKER_HOME / "logs"
SLOTS_PATH = WORKER_HOME / "slots.json"

S3_PREFIX = f"s3://{os.environ.get('EXP_S3_BUCKET', '')}/cb-queue/worker-logs"

CBQ_KIND = (os.environ.get("CBQ_KIND") or "").strip()
CB_LEASE_RESOURCE = (os.environ.get("CB_LEASE_RESOURCE") or "").strip()
WORKER_ACTOR = (os.environ.get("CBQ_ACTOR") or f"worker:{socket.gethostname()}")

SHIFT_MAX_MIN = int(os.environ.get("SHIFT_MAX_MIN", "22"))


def _kind_args() -> list[str]:
    return ["--kind", CBQ_KIND] if CBQ_KIND else []


def _refresh_oauth_token_env() -> None:
    oat = Path.home() / ".oat"
    if not oat.exists():
        return
    try:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oat.read_text().strip()
    except OSError:
        pass


def _gather_state_snapshot(reason: str) -> str:
    """Build the PRE-GATHERED STATE block embedded at the end of the prompt.

    Saves the session 10-20 orientation turns per shift. Best-effort
    and size-bounded; missing files just print the marker.
    """
    out: list[str] = []
    out.append("\n---PRE-GATHERED STATE (collected by the launch wrapper at "
               f"{iso_now()}; trust as of this timestamp)---\n")
    out.append(f"Shift trigger reason: {reason}\n\n")

    out.append("## slots.json\n")
    if SLOTS_PATH.exists():
        try:
            out.append(SLOTS_PATH.read_text())
        except OSError:
            out.append("(unreadable)")
    else:
        out.append("(missing)")
    out.append("\n\n## per-job liveness + progress (derived from slots.json)\n")
    out.append(_describe_per_job())

    out.append("\n## nvidia-smi (whole box; YOUR gpus: " + WORKER_GPUS + ")\n")
    nv = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader"],
        text=True, capture_output=True,
    )
    out.append(nv.stdout or "(nvidia-smi failed)")

    out.append("\n\n## ready queue for your kind\n")
    ready = cbq("list", "--status", "ready", *_kind_args(), "--json", quiet=True)
    out.append((ready.stdout or ready.stderr or "")[:8000])

    out.append("\n\n## your in-flight claims\n")
    inflight = cbq("list", "--status", "executing",
                   "--claimed-by", WORKER_ACTOR, "--json", quiet=True)
    out.append((inflight.stdout or inflight.stderr or "")[:8000])

    if CB_LEASE_RESOURCE:
        out.append("\n\n## lease\n")
        lease = cbq("lease-active", CB_LEASE_RESOURCE, quiet=True)
        out.append((lease.stdout or lease.stderr or "")[:2000])

    out.append("\n\n## disk\n")
    df = subprocess.run(["df", "-h", "/"], text=True, capture_output=True)
    df_lines = (df.stdout or "").splitlines()
    out.append(df_lines[-1] if df_lines else "(df failed)")

    out.append("\n\n## recent shift index\n")
    try:
        index_lines = INDEX_PATH.read_text().splitlines()[-6:]
        out.append("\n".join(index_lines))
    except OSError:
        out.append("(no INDEX.log yet)")
    return "".join(out)


def _describe_per_job() -> str:
    """One block per distinct running job — liveness, progress.log tail,
    run.out tail, attempt history."""
    try:
        slots = json.loads(SLOTS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return "(slots.json unreadable)\n"
    if not isinstance(slots, dict):
        return "(slots.json malformed)\n"
    seen: dict[str, dict] = {}
    for gpu, entry in slots.items():
        if not isinstance(entry, dict):
            continue
        job = entry.get("job")
        if not job:
            continue
        seen.setdefault(job, entry)
    parts: list[str] = []
    for job, entry in sorted(seen.items()):
        pid = int(entry.get("pid") or 0)
        slot_dir = entry.get("dir") or ""
        tmin = entry.get("timeout_min")
        started = entry.get("started") or ""
        alive = "ALIVE" if _pid_alive(pid) else "DEAD"
        parts.append(
            f"job={job} pid={pid} [{alive}] timeout_min={tmin} "
            f"started={started}\n"
        )
        plog = (
            Path(slot_dir) / "ml" / "eval" / "experiments" / "results" / job
            / "progress.log"
        )
        if not plog.exists():
            plog = Path(slot_dir) / "progress.log"
        if plog.exists():
            try:
                mtime = datetime.fromtimestamp(plog.stat().st_mtime,
                                               tz=timezone.utc).isoformat()
                parts.append(f"  progress.log (mtime {mtime}) tail:\n")
                tail = _tail_lines(plog, 3)
                parts.append("".join(f"    {l}\n" for l in tail))
            except OSError:
                parts.append(f"  (progress.log unreadable at {plog})\n")
        else:
            parts.append(f"  (no progress.log at {plog})\n")
        out_path = Path(f"/tmp/run-{job}.out")
        if out_path.exists():
            parts.append("  run.out tail:\n")
            parts.append("".join(f"    {l}\n" for l in _tail_lines(out_path, 3)))
        parts.append("  attempt history:\n")
        exp_id = job.split("-", 1)[0]
        summary = cbq("exec-summary", exp_id, quiet=True)
        if summary.returncode == 0 and (summary.stdout or "").strip():
            parts.append("".join(f"    {l}\n"
                                 for l in (summary.stdout or "").splitlines()))
        else:
            parts.append("    (none recorded)\n")
    return "".join(parts)


def _tail_lines(path: Path, n: int) -> list[str]:
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [l.rstrip("\n") for l in lines[-n:]]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def build_shift_prompt(reason: str) -> str:
    head = _SHIFT_PROMPT_HEAD.format(
        worker_actor=WORKER_ACTOR,
        kind_arg=("--kind " + CBQ_KIND) if CBQ_KIND else "",
        worker_gpus=WORKER_GPUS,
        cb_lease_resource=CB_LEASE_RESOURCE,
        exp_bucket=os.environ.get("EXP_S3_BUCKET", ""),
    )
    return head + _gather_state_snapshot(reason)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    reason = argv[0] if argv else "unspecified"

    failmarker = os.environ.get("WORKER_SHIFT_FAILMARKER")

    _refresh_oauth_token_env()
    os.environ["CUDA_VISIBLE_DEVICES"] = WORKER_GPUS
    os.environ["CBQ_ACTOR"] = WORKER_ACTOR

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    shift_start_epoch = int(time.time())
    log_path = LOG_DIR / f"shift-{stamp}.jsonl"
    s3_log_url = f"{S3_PREFIX}/shift-{stamp}.jsonl"

    # Refresh base clone to clean main before the session.
    subprocess.run(["git", "checkout", "main", "--quiet"],
                   cwd=str(BASE), text=True, capture_output=True)
    lock = BASE / ".git" / "index.lock"
    if lock.exists():
        lock.unlink()
    subprocess.run(["git", "fetch", "origin", "main", "--quiet"],
                   cwd=str(BASE), text=True, capture_output=True)
    subprocess.run(["git", "reset", "--hard", "origin/main", "--quiet"],
                   cwd=str(BASE), text=True, capture_output=True)

    log_line(f"SHIFT-START reason={reason}", index_path=INDEX_PATH)

    prompt = build_shift_prompt(reason)

    driver = HeadlessClaude(
        # No --model: worker_shift.sh ships without one, leaving it to
        # the claude CLI's default. Preserve that behavior — passing
        # None as model means we just won't add the flag.
        model="",
        max_turns=100,
        timeout_s=SHIFT_MAX_MIN * 60,
        kill_after_s=60,
        permission_mode="acceptEdits",
        skip_permissions=True,
        cwd=BASE,
        oauth_token_path=Path.home() / ".oat",
        langfuse_pipeline="worker-shift",
    )

    # The driver's _build_argv always emits --model; for a default-model
    # shift, blank it out by overriding.
    if not driver.model:
        # Drop --model from the argv entirely. Easiest: override by
        # rebuilding argv in a subclass-style approach.
        driver._build_argv = _build_argv_no_model.__get__(driver, HeadlessClaude)

    result = driver.invoke(prompt=prompt, log_path=log_path)
    rc = result.exit_code
    if result.timed_out:
        log_line(
            f"SHIFT-TIMEOUT after {SHIFT_MAX_MIN}m (wedged shift killed)",
            index_path=INDEX_PATH,
        )
    log_line(f"SHIFT-END rc={rc}", index_path=INDEX_PATH)

    s3_uploaded = s3_cp(log_path, s3_log_url)
    if not s3_uploaded:
        s3_log_url = "(s3 upload failed)"

    _slack_executions_finalized_this_shift(
        shift_start_epoch=shift_start_epoch, s3_url=s3_log_url,
        host=socket.gethostname(), stamp=stamp,
    )

    if rc != 0 and failmarker:
        try:
            Path(failmarker).touch()
        except OSError:
            pass
    return rc


def _build_argv_no_model(self, prompt_arg_source: str) -> list[str]:
    """Replacement for HeadlessClaude._build_argv that omits ``--model``.

    Used by worker_shift to honor the original bash's lack of a --model
    flag (relies on the claude CLI's default).
    """
    import shutil
    argv: list[str] = []
    if self.timeout_s and self.timeout_s > 0:
        argv += [self.timeout_binary, f"--kill-after={self.kill_after_s}",
                 str(self.timeout_s)]
    argv += [self.binary, "-p", prompt_arg_source]
    if self.permission_mode:
        argv += ["--permission-mode", self.permission_mode]
    if self.skip_permissions:
        argv += ["--dangerously-skip-permissions"]
    argv += ["--max-turns", str(self.max_turns)]
    argv += ["--output-format", "stream-json"]
    if self.verbose:
        argv += ["--verbose"]
    argv += list(self.extra_args)
    return argv


def _slack_executions_finalized_this_shift(
    *, shift_start_epoch: int, s3_url: str, host: str, stamp: str,
) -> None:
    """Notify Slack for each row this worker transitioned to ``executed``
    since the shift started."""
    since_min = ((int(time.time()) - shift_start_epoch) // 60) + 1
    log_res = cbq("log", "--since-min", str(since_min), "--json", quiet=True)
    if log_res.returncode != 0:
        return
    try:
        rows = json.loads(log_res.stdout or "[]")
    except json.JSONDecodeError:
        return
    slack_hook = route_slack_hook(CBQ_KIND or None)
    for row in rows:
        if row.get("to_status") != "executed":
            continue
        if row.get("actor") != WORKER_ACTOR:
            continue
        exp_id = row.get("experiment_id")
        if not exp_id:
            continue
        report_res = cbq("show", exp_id, "--field", "execution_report_md",
                         quiet=True)
        report = (report_res.stdout or "") if report_res.returncode == 0 else ""
        slug_res = cbq("show", exp_id, "--field", "slug", quiet=True)
        slug = (slug_res.stdout or "?").strip() if slug_res.returncode == 0 else "?"
        status_line = _first_match(report, "^- Status:").replace("`", "")
        headline = _first_match(report, "^- Headline:").replace("`", "")
        dev_line = _first_match(report, "^- Deviations:").replace("`", "")
        slack_post(
            f":gear: *worker-box* executed `{exp_id}-{slug}`\n"
            f"{status_line or 'Status: ?'}\n"
            f"{headline}\n"
            f"{dev_line}\n"
            f"shift log: `{s3_url}` (also "
            f"`~/worker/logs/shift-{stamp}.jsonl` on `{host}`)",
            hook_url=slack_hook,
        )


def _first_match(text: str, prefix_regex: str) -> str:
    import re
    pat = re.compile(prefix_regex, re.MULTILINE)
    for line in text.splitlines():
        if pat.match(line):
            return line[2:] if line.startswith("- ") else line
    return ""


_SHIFT_PROMPT_HEAD = """You are the WORKER on the GPU box for the experiments pipeline. An architect Claude on a separate CPU box designs experiments and pushes code branches; you own everything that happens on the GPUs. You run in short recurring SHIFTS: durable state lives on disk, in the queue database (via the cbq CLI, on PATH), and on exp/ git branches — not in your session. Do your duties, update state, print a report, exit. Jobs keep running in the background between shifts.

YOUR AUTHORITY. You may change ANYTHING to make an experiment run and produce a meaningful result: CLI knobs, configs, dependencies, run.py code, even the experiment design the architect specified — instructions sometimes do not survive contact with the hardware, and you are the one in contact with it. You never need permission. The single hard rule: EVERY deviation from the code/spec as handed to you is documented in deviations.md with rationale and impact, as you make it. Silent change is the only forbidden move — an undocumented deviation poisons the permanent cross-experiment record, because the architect reads deviations.md FIRST when judging results. If even a changed experiment cannot produce a meaningful result, escalate (--exec-status escalated) instead of burning GPU time.

QUEUE COMMANDS (your actor id is {worker_actor}; it is already set via CBQ_ACTOR):
- cbq list --status ready {kind_arg} --json     -> the ready queue FOR YOUR KIND: id, slug, gpus, timeout_min
- cbq claim execute --id <ID>                    -> atomically claim a spec (rc 3 = someone else took it)
- cbq show <ID> --markdown                       -> full task document (task + architect handoff)
- cbq show <ID> --field branch                   -> the exp/ branch to check out
- cbq executed <ID> --exec-status completed|escalated --report-file <f>   -> finalize
- cbq unclaim <ID>                               -> return a claim you cannot serve (back to ready)
- cbq list --status executing --claimed-by {worker_actor} --json         -> your in-flight claims

BOX FACTS
- Schedulable GPUs (CUDA ids): {worker_gpus} — other GPUs on this box are NOT yours; never schedule onto them or count them as free.
- Your shell env pins CUDA_VISIBLE_DEVICES={worker_gpus}, so a probe that doesn't set it sees only your GPUs (cuda:0 = the first of them). Launch/smoke commands still set CUDA_VISIBLE_DEVICES=<ids> explicitly with physical ids.
- Base clone: ~/worker/www, kept on main. Per-job checkouts are git worktrees at ~/worker/jobs/<ID>-<slug>/ so concurrent jobs never share a checkout.
- Shared venvs: ~/queue/venvs/<name>/ — create from the job's run.spec ('venv:' + 'setup:') if missing; they are shared across experiments, so add packages rather than rebuilding.
- Slot table ~/worker/slots.json — THE source of truth for what runs where; the watchdog reads it to decide when to wake you, so keep it exact. Shape:
    {{"4": {{"job": "0051-foo", "pid": 12345, "dir": "/home/ubuntu/worker/jobs/0051-foo", "timeout_min": 90, "started": "2026-06-10T20:00:00Z"}}, "5": null}}
  A job needing N GPUs appears under N keys with the same content.
- Local task snapshots for reference: ~/worker/inflight/<ID>-<slug>.task (write on claim, delete on finalize).
- Each job dir contains ml/eval/experiments/results/<ID>-<slug>/ with task.md (the architect's intent — hypothesis, success criteria, load-bearing parameters in the run.py docstring), run.py, run.spec, and your progress.log / results.json / deviations.md.

A PRE-GATHERED STATE snapshot (trigger reason, slots, pid liveness, progress tails, nvidia-smi, ready queue, your claims, lease, disk) is appended at the END of this prompt, collected at launch. Start from it instead of re-running those reads; re-check a fact only when you act on it or it looks inconsistent.

SHIFT DUTIES — work through ALL of these, in this order, then exit:

1. RECONCILE. Read slots.json. For every occupied slot check the pid (kill -0) and cross-check nvidia-smi. Fix lies: a dead pid still registered is a job to finalize or fix (duties 2/3). Then cross-check the queue: every row from cbq list --status executing --claimed-by {worker_actor} --json must hold a slot; a row with no slot is an orphan from a crash — if its worktree at ~/worker/jobs/<ID>-<slug>/ exists, re-launch it (duty 5 step e/f); if the worktree is missing or corrupt, re-create it from the branch (duty 5 step c) or, if that fails, cbq unclaim <ID> so the spec returns to the ready queue.

2. FINALIZE each finished job (pid dead, run looks complete). In its worktree results dir:
   FAST PATH (use this whenever it applies — do NOT hand-finalize a clean completion): if results.json exists AND progress.log's last event is a 'phase: done' / 'results written' line, the run completed cleanly. Run ONE command and move on:
          ~/bin/finalize_completed.sh <ID> <slug>
      It deterministically commits+pushes the artifacts, runs cbq executed, frees the slot, and removes the worktree — no model load, no analysis. Do NOT read or judge the results yourself in this case; that grinding is exactly what starves scheduling (a wedged finalize on a completed job leaves the row 'executing' with a dead pid and blocks every later shift — root cause of the 0059.1 stall). Exit code 0 = done; exit 10 = not-cleanly-complete, fall through to the manual steps below. After a fast-path finalize, skip to the next job / duty.
   Manual finalize (ONLY when the fast path returns 10 — ambiguous/failed/missing results):
   a. Confirm results.json exists and is plausible; check it against run.spec success_criteria and the task's hypothesis. A completed-but-criteria-failing run is NOT automatically escalated — if you can see a concrete fix, treat it as duty 3; otherwise finalize it honestly and let the architect judge.
   b. deviations.md is REQUIRED, even when empty. Format:
          ## none
      or one section per deviation:
          ## D1: <what changed, one line>
          why: <the practical constraint that forced it>
          impact: <what the result does / does not answer anymore>
      Knob changes COUNT (batch size, seq len, sample count, dtype, model swap) — they can silently change what the experiment measures.
   c. Commit everything uncommitted in the worktree with '[worker]' in the message; push the exp branch.
   d. Write the execution report to a temp file and finalize via cbq:
          ## Execution report (worker)
          - Status: completed | escalated
          - Branch: exp/<ID>-<slug> @ <sha>
          - GPUs used: <ids> · wall: <min> · fix rounds: <n> (cross-check the snapshot's attempt history)
          - Headline: <one line of the main numbers, or why escalated>
          - Deviations: <count> — <one line each, matching deviations.md>
      Then: cbq executed <ID> --exec-status <completed|escalated> --report-file <tmpfile>
   e. Archive the trajectory to S3 BEFORE removing the worktree (*.log files are
      gitignored, so the trajectory only survives via S3):
          aws s3 cp <results>/progress.log s3://{exp_bucket}/cb-queue/job-trajectories/<ID>-<slug>/progress.log
          aws s3 cp /tmp/run-<ID>-<slug>.out s3://{exp_bucket}/cb-queue/job-trajectories/<ID>-<slug>/run.out
      Best-effort — keep going if either is missing or upload fails.
   f. Free its slots in slots.json, remove the worktree (git worktree remove --force <dir>), delete the inflight snapshot.

3. FIX each failed job (pid dead with bad/missing results, or one you kill in duty 4). BUDGET CHECK FIRST — the snapshot carries each job's attempt history (cbq exec-summary <ID>: launches, deaths, cumulative wall). This is a HARD rule, not judgment: if launches >= 3 OR cumulative wall >= 2x the spec's timeout_min, do ZERO fix rounds — write deviations.md with the failure signature and a final '## escalated: <why>' section, then finalize via duty 2 with --exec-status escalated. Likewise, if the shift trigger reason says 'PARK <ID>', that is an order: escalate that job now, do not fix it. You are a fresh session; without the history you cannot tell fix-round 10 from fix-round 1 — trust the counter, not your optimism. Under budget: read the tail of /tmp/run-<ID>-<slug>.out and progress.log, diagnose, edit whatever needs editing in the worktree, record the deviation in deviations.md NOW, commit '[worker]'. Then relaunch via the duty-5e helper (it re-smokes automatically because your commit changed the worktree sha, so smoked_sha no longer matches — you never need to invoke --smoke by hand). A job that died WITHOUT a code change (e.g. the box restarted under it) relaunches with NO new commit, so the helper sees the matching smoked_sha and skips straight to launch — that is the fast crash-recovery path. At most 2 fix rounds in one shift; not green after 2 means escalate.

4. OBSERVE each running job (pid alive): tail -5 of its progress.log and nvidia-smi for its GPUs. Stalled — no new progress lines in ~10 min while the pid is alive — means kill the pid and treat as failed (duty 3). Past its timeout_min: the launch wrapper's timeout should have killed it; if somehow alive, kill it, then decide: shrink and rerun (a deviation) or escalate.

5. SCHEDULE. FIRST, if CB_LEASE_RESOURCE is set (it is "{cb_lease_resource}"), run: cbq lease-active "{cb_lease_resource}". If it prints a holder (anyone other than you), the node is reserved for a peer's clean-room benchmark — DO NOT claim or launch new jobs this shift (drain). Skip to duty 6; duties 1-4 (finalize/fix/observe existing jobs) still apply. Otherwise, while there are free GPUs and rows in cbq list --status ready {kind_arg} --json (NEVER claim a row outside that filtered list — rows for other kinds are not yours even if they look runnable):
   a. Pick the lowest-ID spec whose 'gpus' fits the free count. If the lowest-ID spec needs MORE than are free, prefer draining (hold GPUs for it); backfill a smaller spec past it only if that spec's timeout_min is 30 or less.
   b. CLAIM: cbq claim execute --id <ID>. Exit code 3 means another worker took it — reassess from step a. Then save the task document locally: cbq show <ID> --markdown > ~/worker/inflight/<ID>-<slug>.task
   c. CHECKOUT: from the base clone: git fetch origin 'exp/<ID>-<slug>:exp/<ID>-<slug>' then git worktree add ~/worker/jobs/<ID>-<slug> exp/<ID>-<slug>
   d. Read run.spec. Ensure the venv exists; if not, python3 -m venv it under ~/queue/venvs/ and run the spec's setup block inside it.
   e. HAND OFF to the launch helper — DO NOT smoke or launch by hand. The helper does smoke-skip + smoke + launch + slot registration as one unit, and you BACKGROUND one per job so several GPUs come up CONCURRENTLY instead of serializing on the (multi-minute) model-load smoke:
          nohup ~/bin/smoke_and_launch.sh <ID> <slug> <gpu_csv> <venv> <timeout_min> <smoke_min> "<smoke_flag>" <results_dir> "<run_flags>" > /tmp/launch-<ID>.log 2>&1 &
      Take <smoke_flag> (e.g. --smoke), <smoke_min>, <timeout_min>, <venv>, and <run_flags> from run.spec. Fire the helper for EVERY job you claim this loop, then move on — do not wait between them. The helper:
        - SKIPS the smoke when the worktree HEAD sha already passed it (cbq smoked_sha == HEAD) — so a crash-relaunch of unchanged code launches in seconds with no model load; a code fix (new sha) still re-smokes;
        - on a passing smoke, records cbq mark-smoked <ID> --sha <HEAD>;
        - on a FAILING smoke, writes ~/worker/launch-<ID>.fail and does NOT launch (you handle it as duty 3 next shift);
        - registers the slot(s) in slots.json itself, under flock — you do NOT write slots.json for launches.
   f. After firing all helpers, give them a moment, then VERIFY: read slots.json and ~/worker/launch-<ID>.fail markers. Any launch-<ID>.fail = that job's smoke failed; treat it as duty 3 (FIX) — but a failing smoke means it never held the GPU, so its slot is free for the next shift. You do not need to wait for full launches to finish.

6. REPORT. Print a short shift report: one line per schedulable GPU (job, state, latest progress), plus anything you fixed, deviated on, escalated, claimed, or launched this shift. Then exit. NEVER stay alive to babysit a multi-minute run — the watchdog wakes you again when something changes; the backgrounded launch helpers run the smokes, so you no longer block on them in-session.

HARD BOUNDARIES
- Never touch GPUs outside {worker_gpus}.
- Never commit or push to main, and never write to ml/eval/experiments/{{done,falsified}}/ or constraints.md — those are the architect's. Your queue writes are exactly: cbq claim execute, cbq executed, cbq unclaim. Your git writes are exactly: [worker] commits on exp/ branches.
- Never modify another job's worktree or results dir.
- Never skip deviations.md, and never summarize a deviation more vaguely in the execution report than in the file.
"""


if __name__ == "__main__":
    sys.exit(main())
