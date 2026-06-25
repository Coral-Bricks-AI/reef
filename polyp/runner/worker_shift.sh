#!/usr/bin/env bash
# Usage: worker_shift.sh [reason]
# One worker "shift" on the GPU box: a bounded Claude session that reconciles
# state, finalizes finished jobs, fixes failed ones, observes running ones, and
# schedules new ones onto free GPUs. Invoked by worker_watcher.sh on events —
# never long-lived. Durable state lives in ~/worker/slots.json, the per-job
# worktrees, the cb_queue database (via cbq), and the exp/ branches; jobs run
# as nohup'd background processes that survive between shifts.
set -uo pipefail
# plan token: read fresh at every launch; ~/.oat is written by the monitor agent
export CLAUDE_CODE_OAUTH_TOKEN="$(cat ~/.oat)"
REASON="${1:-unspecified}"

BASE=~/worker/www
WORKER_GPUS="${WORKER_GPUS:-0}"
# Pin the whole shift (and every probe it spawns) to this worker's GPUs: an
# ad-hoc python that never sets CUDA_VISIBLE_DEVICES otherwise lands on the
# box's GPU 0, which on a shared box belongs to a peer worker. Per-launch
# CUDA_VISIBLE_DEVICES=<ids> overrides still work and still use physical ids.
export CUDA_VISIBLE_DEVICES="$WORKER_GPUS"
S3_PREFIX="s3://${EXP_S3_BUCKET}/cb-queue/worker-logs"
STAMP=$(date +%Y%m%d-%H%M%S)
SHIFT_START_EPOCH=$(date +%s)
LOG=~/worker/logs/shift-${STAMP}.jsonl
INDEX=~/worker/INDEX.log
HOST=$(hostname)
S3_LOG_URL="${S3_PREFIX}/shift-${STAMP}.jsonl"
CBQ=${CBQ:-"$HOME/bin/cbq"}
WORKER_ACTOR="${CBQ_ACTOR:-worker:${HOST}}"
export CBQ_ACTOR="$WORKER_ACTOR"
# Kind + lease (optional; unset => legacy any-kind, no clean-room drain).
# Each kind maps to one GPU worker; this shift sees and claims its kind only.
# A row's `machine` is a sizing attribute (what hardware the architect
# designed for), not a claim filter.
CBQ_KIND="${CBQ_KIND:-}"
KIND_ARG=""; [ -n "$CBQ_KIND" ] && KIND_ARG="--kind $CBQ_KIND"
CB_LEASE_RESOURCE="${CB_LEASE_RESOURCE:-}"

# Route by kind: a loadtest worker posts to #infra-bench, else the default
# channel (fallback to default if the infra hook is unset).
SLACK_HOOK="${SLACK_WEBHOOK_URL:-}"
[ "$CBQ_KIND" = loadtest ] && [ -n "${SLACK_INFRA_BENCH_WEBHOOK_URL:-}" ] && SLACK_HOOK="$SLACK_INFRA_BENCH_WEBHOOK_URL"

slack_post() {
  local hook="${SLACK_HOOK:-${SLACK_WEBHOOK_URL:-}}"
  [ -z "$hook" ] && return 0
  curl -sS -X POST -H 'Content-type: application/json' \
    --data "$(jq -nc --arg t "$1" '{text:$t}')" \
    "$hook" >/dev/null 2>&1 || true
}

cd "$BASE"
git checkout main --quiet 2>/dev/null || true
[ -f "$BASE/.git/index.lock" ] && rm -f "$BASE/.git/index.lock"
git fetch origin main --quiet 2>/dev/null || true
git reset --hard origin/main --quiet 2>/dev/null

echo "[$(date -Is)] SHIFT-START reason=${REASON}" | tee -a "$INDEX"

# Top-level heredoc into a tempfile (see git history of run_task.sh for why).
# Unquoted HDR so ${WORKER_GPUS} etc. interpolate: NO backticks in the body,
# and every literal shell '$' is escaped as \$.
PROMPT_FILE=$(mktemp /tmp/worker-shift-prompt.XXXXXX.md)
cat > "$PROMPT_FILE" <<HDR
You are the WORKER on the GPU box for the experiments pipeline. An architect Claude on a separate CPU box designs experiments and pushes code branches; you own everything that happens on the GPUs. You run in short recurring SHIFTS: durable state lives on disk, in the queue database (via the cbq CLI, on PATH), and on exp/ git branches — not in your session. Do your duties, update state, print a report, exit. Jobs keep running in the background between shifts.

YOUR AUTHORITY. You may change ANYTHING to make an experiment run and produce a meaningful result: CLI knobs, configs, dependencies, run.py code, even the experiment design the architect specified — instructions sometimes do not survive contact with the hardware, and you are the one in contact with it. You never need permission. The single hard rule: EVERY deviation from the code/spec as handed to you is documented in deviations.md with rationale and impact, as you make it. Silent change is the only forbidden move — an undocumented deviation poisons the permanent cross-experiment record, because the architect reads deviations.md FIRST when judging results. If even a changed experiment cannot produce a meaningful result, escalate (--exec-status escalated) instead of burning GPU time.

QUEUE COMMANDS (your actor id is ${WORKER_ACTOR}; it is already set via CBQ_ACTOR):
- cbq list --status ready ${KIND_ARG} --json     -> the ready queue FOR YOUR KIND: id, slug, gpus, timeout_min
- cbq claim execute --id <ID>                    -> atomically claim a spec (rc 3 = someone else took it)
- cbq show <ID> --markdown                       -> full task document (task + architect handoff)
- cbq show <ID> --field branch                   -> the exp/ branch to check out
- cbq executed <ID> --exec-status completed|escalated --report-file <f>   -> finalize
- cbq unclaim <ID>                               -> return a claim you cannot serve (back to ready)
- cbq list --status executing --claimed-by ${WORKER_ACTOR} --json         -> your in-flight claims

BOX FACTS
- Schedulable GPUs (CUDA ids): ${WORKER_GPUS} — other GPUs on this box are NOT yours; never schedule onto them or count them as free.
- Your shell env pins CUDA_VISIBLE_DEVICES=${WORKER_GPUS}, so a probe that doesn't set it sees only your GPUs (cuda:0 = the first of them). Launch/smoke commands still set CUDA_VISIBLE_DEVICES=<ids> explicitly with physical ids.
- Base clone: ~/worker/www, kept on main. Per-job checkouts are git worktrees at ~/worker/jobs/<ID>-<slug>/ so concurrent jobs never share a checkout.
- Shared venvs: ~/queue/venvs/<name>/ — create from the job's run.spec ('venv:' + 'setup:') if missing; they are shared across experiments, so add packages rather than rebuilding.
- Slot table ~/worker/slots.json — THE source of truth for what runs where; the watchdog reads it to decide when to wake you, so keep it exact. Shape:
    {"4": {"job": "0051-foo", "pid": 12345, "dir": "/home/ubuntu/worker/jobs/0051-foo", "timeout_min": 90, "started": "2026-06-10T20:00:00Z"}, "5": null}
  A job needing N GPUs appears under N keys with the same content.
- Local task snapshots for reference: ~/worker/inflight/<ID>-<slug>.task (write on claim, delete on finalize).
- Each job dir contains ml/eval/experiments/results/<ID>-<slug>/ with task.md (the architect's intent — hypothesis, success criteria, load-bearing parameters in the run.py docstring), run.py, run.spec, and your progress.log / results.json / deviations.md.

A PRE-GATHERED STATE snapshot (trigger reason, slots, pid liveness, progress tails, nvidia-smi, ready queue, your claims, lease, disk) is appended at the END of this prompt, collected at launch. Start from it instead of re-running those reads; re-check a fact only when you act on it or it looks inconsistent.

SHIFT DUTIES — work through ALL of these, in this order, then exit:

1. RECONCILE. Read slots.json. For every occupied slot check the pid (kill -0) and cross-check nvidia-smi. Fix lies: a dead pid still registered is a job to finalize or fix (duties 2/3). Then cross-check the queue: every row from cbq list --status executing --claimed-by ${WORKER_ACTOR} --json must hold a slot; a row with no slot is an orphan from a crash — if its worktree at ~/worker/jobs/<ID>-<slug>/ exists, re-launch it (duty 5 step e/f); if the worktree is missing or corrupt, re-create it from the branch (duty 5 step c) or, if that fails, cbq unclaim <ID> so the spec returns to the ready queue.

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
          aws s3 cp <results>/progress.log s3://${EXP_S3_BUCKET}/cb-queue/job-trajectories/<ID>-<slug>/progress.log
          aws s3 cp /tmp/run-<ID>-<slug>.out s3://${EXP_S3_BUCKET}/cb-queue/job-trajectories/<ID>-<slug>/run.out
      Best-effort — keep going if either is missing or upload fails.
   f. Free its slots in slots.json, remove the worktree (git worktree remove --force <dir>), delete the inflight snapshot.

3. FIX each failed job (pid dead with bad/missing results, or one you kill in duty 4). BUDGET CHECK FIRST — the snapshot carries each job's attempt history (cbq exec-summary <ID>: launches, deaths, cumulative wall). This is a HARD rule, not judgment: if launches >= 3 OR cumulative wall >= 2x the spec's timeout_min, do ZERO fix rounds — write deviations.md with the failure signature and a final '## escalated: <why>' section, then finalize via duty 2 with --exec-status escalated. Likewise, if the shift trigger reason says 'PARK <ID>', that is an order: escalate that job now, do not fix it. You are a fresh session; without the history you cannot tell fix-round 10 from fix-round 1 — trust the counter, not your optimism. Under budget: read the tail of /tmp/run-<ID>-<slug>.out and progress.log, diagnose, edit whatever needs editing in the worktree, record the deviation in deviations.md NOW, commit '[worker]'. Then relaunch via the duty-5e helper (it re-smokes automatically because your commit changed the worktree sha, so smoked_sha no longer matches — you never need to invoke --smoke by hand). A job that died WITHOUT a code change (e.g. the box restarted under it) relaunches with NO new commit, so the helper sees the matching smoked_sha and skips straight to launch — that is the fast crash-recovery path. At most 2 fix rounds in one shift; not green after 2 means escalate.

4. OBSERVE each running job (pid alive): tail -5 of its progress.log and nvidia-smi for its GPUs. Stalled — no new progress lines in ~10 min while the pid is alive — means kill the pid and treat as failed (duty 3). Past its timeout_min: the launch wrapper's timeout should have killed it; if somehow alive, kill it, then decide: shrink and rerun (a deviation) or escalate.

5. SCHEDULE. FIRST, if CB_LEASE_RESOURCE is set (it is "${CB_LEASE_RESOURCE}"), run: cbq lease-active "${CB_LEASE_RESOURCE}". If it prints a holder (anyone other than you), the node is reserved for a peer's clean-room benchmark — DO NOT claim or launch new jobs this shift (drain). Skip to duty 6; duties 1-4 (finalize/fix/observe existing jobs) still apply. Otherwise, while there are free GPUs and rows in cbq list --status ready ${KIND_ARG} --json (NEVER claim a row outside that filtered list — rows for other kinds are not yours even if they look runnable):
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
- Never touch GPUs outside ${WORKER_GPUS}.
- Never commit or push to main, and never write to ml/eval/experiments/{done,falsified}/ or constraints.md — those are the architect's. Your queue writes are exactly: cbq claim execute, cbq executed, cbq unclaim. Your git writes are exactly: [worker] commits on exp/ branches.
- Never modify another job's worktree or results dir.
- Never skip deviations.md, and never summarize a deviation more vaguely in the execution report than in the file.
HDR

# Pre-gathered state snapshot: saves the session 10-20 orientation turns per
# shift. Appended after the heredoc so file contents never pass through
# heredoc interpolation. Every read is best-effort and size-bounded.
{
  echo ""
  echo "---PRE-GATHERED STATE (collected by the launch wrapper at $(date -Is); trust as of this timestamp)---"
  echo "Shift trigger reason: ${REASON}"
  echo ""
  echo "## slots.json"
  cat ~/worker/slots.json 2>/dev/null || echo "(missing)"
  echo ""
  echo "## per-job liveness + progress (derived from slots.json)"
  jq -r 'to_entries[] | select(.value != null) | [.key, .value.job, (.value.pid|tostring), .value.dir, (.value.timeout_min|tostring), .value.started] | @tsv' \
      ~/worker/slots.json 2>/dev/null | sort -u -t$'\t' -k2,2 \
    | while IFS=$'\t' read -r gpu job pid dir tmin started; do
      alive=DEAD; kill -0 "$pid" 2>/dev/null && alive=ALIVE
      echo "job=$job pid=$pid [$alive] timeout_min=$tmin started=$started"
      # dir is documented as the worktree root but sometimes holds the
      # results dir itself — accept either
      pl="$dir/ml/eval/experiments/results/$job/progress.log"
      [ -f "$pl" ] || pl="$dir/progress.log"
      if [ -f "$pl" ]; then
        echo "  progress.log (mtime $(date -r "$pl" -Is 2>/dev/null)) tail:"
        tail -3 "$pl" 2>/dev/null | sed 's/^/    /'
      else
        echo "  (no progress.log at $pl)"
      fi
      out="/tmp/run-$job.out"
      [ -f "$out" ] && { echo "  run.out tail:"; tail -3 "$out" 2>/dev/null | sed 's/^/    /'; }
      echo "  attempt history:"
      "$CBQ" exec-summary "${job%%-*}" 2>/dev/null | sed 's/^/    /' || echo "    (none recorded)"
    done
  echo ""
  echo "## nvidia-smi (whole box; YOUR gpus: ${WORKER_GPUS})"
  nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo "(nvidia-smi failed)"
  echo ""
  echo "## ready queue for your kind"
  "$CBQ" list --status ready $KIND_ARG --json 2>&1 | head -c 8000
  echo ""
  echo "## your in-flight claims"
  "$CBQ" list --status executing --claimed-by "$WORKER_ACTOR" --json 2>&1 | head -c 8000
  if [ -n "$CB_LEASE_RESOURCE" ]; then
    echo ""
    echo "## lease"
    "$CBQ" lease-active "$CB_LEASE_RESOURCE" 2>&1 | head -c 2000
  fi
  echo ""
  echo "## disk"
  df -h / 2>/dev/null | tail -1
  echo ""
  echo "## recent shift index"
  tail -6 "$INDEX" 2>/dev/null
} >> "$PROMPT_FILE"

PROMPT=$(cat "$PROMPT_FILE")
rm -f "$PROMPT_FILE"

# Wall-clock backstop: a shift must never run unbounded. A wedged shift (e.g. an
# agentic session grinding on one duty) otherwise holds a slot/orphan and starves
# scheduling forever (root cause of the 0059.1 stall). A legit shift — including
# one synchronous model-load smoke — fits well under this; on timeout the wrapper
# exits non-zero, the watchdog backs off and fires a fresh shift, and the next
# shift's reconcile re-adopts any in-flight work. SHIFT_MAX_MIN overridable in env.
timeout "${SHIFT_MAX_MIN:-22}m" claude -p "$PROMPT" \
  --permission-mode acceptEdits \
  --dangerously-skip-permissions \
  --max-turns 100 \
  --output-format stream-json --verbose \
  >> "$LOG" 2>&1
RC=$?
[ "$RC" -eq 124 ] && echo "[$(date -Is)] SHIFT-TIMEOUT after ${SHIFT_MAX_MIN:-22}m (wedged shift killed)" | tee -a "$INDEX"
echo "[$(date -Is)] SHIFT-END rc=${RC}" | tee -a "$INDEX"

aws s3 cp "$LOG" "$S3_LOG_URL" --quiet 2>/dev/null || S3_LOG_URL="(s3 upload failed)"

# slack-report any executions this shift finalized: executed-transitions by
# this worker since the shift started, straight from the transitions log
SINCE_MIN=$(( ( $(date +%s) - SHIFT_START_EPOCH ) / 60 + 1 ))
"$CBQ" log --since-min "$SINCE_MIN" --json 2>/dev/null \
  | jq -rc --arg actor "$WORKER_ACTOR" \
      '.[] | select(.to_status == "executed" and .actor == $actor) | .experiment_id' \
  | while read -r exp_id; do
      [ -z "$exp_id" ] && continue
      report=$("$CBQ" show "$exp_id" --field execution_report_md 2>/dev/null || echo "")
      # Strip backticks: these fields are interpolated into the double-quoted
      # slack_post argument below, where a markdown-code backtick triggers
      # runtime command substitution — crashing the shift (nonzero exit) and,
      # via the watcher's failure/restart path, tearing down running GPU jobs.
      status_line=$(echo "$report" | grep -m1 -E '^- Status:' | sed 's/^- //' | tr -d '`')
      headline=$(echo "$report" | grep -m1 -E '^- Headline:' | sed 's/^- //' | tr -d '`')
      dev_line=$(echo "$report" | grep -m1 -E '^- Deviations:' | sed 's/^- //' | tr -d '`')
      slug=$("$CBQ" show "$exp_id" --field slug 2>/dev/null || echo "?")
      slack_post ":gear: *worker-box* executed \`${exp_id}-${slug}\`
${status_line:-Status: ?}
${headline:-}
${dev_line:-}
shift log: \`${S3_LOG_URL}\` (also \`~/worker/logs/shift-${STAMP}.jsonl\` on \`${HOST}\`)"
    done

exit $RC
