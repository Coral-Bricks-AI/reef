#!/usr/bin/env bash
# Usage: architect_code_task.sh <experiment-id>
# CODE phase of the split architect/worker pipeline (Postgres queue). Runs on
# the CPU-only orchestrator box — no GPU here, no execution. The row is already
# claimed (status=coding) by the watcher. See runner/README.md.
#
#   - Spawns an architect Claude session that writes run.py + run.spec on a
#     fresh exp/NNNN-<slug> branch and pushes the branch (code only, no runs)
#   - On success: `cbq ready <id>` records the handoff; the GPU worker claims
#     it from the ready queue
#   - On final failure: `cbq code-failed <id>` PARKS the row (no auto-retry —
#     the in-phase attempts below already retry with failure context; a parked
#     task needs a CHANGED task, resubmitted by a human), then a short Claude
#     session extracts any durable box/toolchain constraint into constraints.md
#   - Logs uploaded to s3://${EXP_S3_BUCKET}/cb-queue/architect-logs/
set -uo pipefail
# plan token: read fresh at every launch; ~/.oat is written by the monitor agent
export CLAUDE_CODE_OAUTH_TOKEN="$(cat ~/.oat)"
ID="$1"
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}
# Per-ATTEMPT wall cap (NOT cumulative): 30 min/attempt, up to MAX_ATTEMPTS, so a
# wedged session is killed (rc=124) while remaining attempts get new dice. Each
# attempt also calls `cbq touch` (below) to refresh claimed_at, so a live
# multi-attempt wrapper is never reaped — the reap window only covers one attempt.
# (Earlier this was a cumulative budget that mis-parked #0061.5.1.2.1.)
ATTEMPT_TIMEOUT_SEC=${ATTEMPT_TIMEOUT_SEC:-1800}

WORKDIR=${WORKDIR:-~/architect/workdir/www}
S3_PREFIX="s3://${EXP_S3_BUCKET}/cb-queue/architect-logs"
CBQ=${CBQ:-"$HOME/bin/cbq"}
export CBQ_ACTOR=${CBQ_ACTOR:-"architect:$(hostname)"}

# Route by kind: loadtest -> #infra-bench, else default (fallback to default).
KIND=$("$CBQ" show "$ID" --field kind 2>/dev/null || echo research)
SLACK_HOOK="${SLACK_WEBHOOK_URL:-}"
[ "$KIND" = loadtest ] && [ -n "${SLACK_INFRA_BENCH_WEBHOOK_URL:-}" ] && SLACK_HOOK="$SLACK_INFRA_BENCH_WEBHOOK_URL"

slack_post() {
  local hook="${SLACK_HOOK:-${SLACK_WEBHOOK_URL:-}}"
  [ -z "$hook" ] && return 0
  curl -sS -X POST -H 'Content-type: application/json' \
    --data "$(jq -nc --arg t "$1" '{text:$t}')" \
    "$hook" >/dev/null 2>&1 || true
}
extract_attempt_tail() {
  awk -v start="===== ATTEMPT $1 START =====" -v end="===== ATTEMPT $1 END =====" '
    $0 ~ start {flag=1; next}
    $0 ~ end {flag=0}
    flag' "$LOG" 2>/dev/null | \
    jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text' 2>/dev/null | \
    tail -c 3500
}

SHORT=$("$CBQ" show "$ID" --field slug) || { echo "[$(date -Is)] ERROR: no row for ${ID}"; exit 2; }
TASK=$(mktemp /tmp/code-task-${ID}.XXXXXX.md)
"$CBQ" show "$ID" --field task_md > "$TASK" || { echo "[$(date -Is)] ERROR: no task body for ${ID}"; exit 2; }
TAG="#${ID}"

STAMP=$(date +%Y%m%d-%H%M%S)
RUN_ID="${ID}-${SHORT}-${STAMP}"
LOG=~/architect/logs/${RUN_ID}.jsonl
INDEX=~/architect/INDEX.log
BRANCH="exp/${ID}-${SHORT}"
HOST=$(hostname)
S3_LOG_URL="${S3_PREFIX}/${RUN_ID}.jsonl"
RESULTS_DIR="ml/eval/experiments/results/${ID}-${SHORT}"

echo "[$(date -Is)] CODE-START ${TAG} ${RUN_ID}" | tee -a "$INDEX"

TOTAL_START=$(date +%s)
final_rc=1
attempts_used=0
declare -a attempt_summaries
prev_rc=0

cd "$WORKDIR"

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  attempts_used=$attempt
  echo "===== ATTEMPT ${attempt} START =====" >> "$LOG"
  echo "[$(date -Is)] CODE-ATTEMPT ${attempt}/${MAX_ATTEMPTS} ${TAG}" | tee -a "$INDEX"

  # Per-attempt claim heartbeat: refresh claimed_at so a live multi-attempt
  # wrapper is never reaped as an orphan (reap window covers one attempt only).
  "$CBQ" touch "$ID" --quiet 2>/dev/null || true

  # reset branch to clean main each attempt
  git fetch origin main --quiet >>"$LOG" 2>&1
  git checkout main --quiet >>"$LOG" 2>&1
  git reset --hard origin/main --quiet >>"$LOG" 2>&1
  git branch -D "$BRANCH" 2>/dev/null >>"$LOG" 2>&1 || true
  git push origin --delete "$BRANCH" 2>/dev/null >>"$LOG" 2>&1 || true
  git checkout -b "$BRANCH" >>"$LOG" 2>&1

  # echo the original task into the branch immediately, so it's preserved even if Claude flounders
  mkdir -p "$RESULTS_DIR"
  cp "$TASK" "$RESULTS_DIR/task.md"

  if [ "$attempt" -eq 1 ]; then
    RETRY_CTX=""
  else
    PREV_TAIL=$(extract_attempt_tail $((attempt - 1)))
    cause="exited with code ${prev_rc}"
    [ "$prev_rc" -eq 99 ] && cause="exited cleanly but did not push the branch — your job is to actually commit + push, not just describe what you would do"
    [ "$prev_rc" -eq 98 ] && cause="pushed the branch but it is missing run.py and/or run.spec in ${RESULTS_DIR}/ — both deliverables are mandatory"
    RETRY_CTX="

---RETRY ${attempt} of ${MAX_ATTEMPTS}---
Your previous attempt ${cause}. The branch '${BRANCH}' was reset to origin/main; you are starting from a clean state.

DEBUG MODE: read the previous attempt's output carefully. Diagnose the root cause. Fix it. Do not just repeat the same steps.

Previous attempt's last output:
\`\`\`
${PREV_TAIL:-(no assistant text captured)}
\`\`\`"
  fi

  # Build the prompt via a top-level heredoc into a tempfile, then read.
  # Top-level heredoc rather than PROMPT=$(cat <<HDR ... HDR) — bash's $()
  # parser counts parens/keywords inside the heredoc body and silently
  # produces an empty result when confused (see git history of run_task.sh).
  # Unquoted HDR: no backticks or unescaped dollar-literals in the body.
  PROMPT_FILE=$(mktemp /tmp/architect-code-prompt.XXXXXX.md)
  cat > "$PROMPT_FILE" <<HDR
You are the ARCHITECT for the experiments pipeline, running on a CPU-only orchestrator box. There is NO GPU here. You are inside Coral-Bricks-AI/www on a fresh branch '${BRANCH}'. Your job is to PREPARE the experiment described below so a separate GPU worker can execute it without you: write the code and the run spec, push the branch. Do NOT attempt to execute the experiment, load models, start inference servers, or install GPU/CUDA wheels on this box.

Working directory for this experiment: ${RESULTS_DIR}/
The original task file is already copied there as task.md.

READ FIRST, in order (items 1-3 are EMBEDDED at the END of this prompt — use the embedded copies; re-open a file only if its embed is marked truncated):
1. ml/eval/experiments/constraints.md — practical constraints of the GPU worker box, learned from prior runs. Design within them: specs that ignore them get overruled by the worker on the box.
2. ml/eval/experiments/README.md — required reporting schema ('Required reporting shape').
3. ml/eval/experiments/lib/README.md — battle-tested primitives (niah generators, attention helpers, GpuMonitor, results.write_results, progress.ProgressLog, and `exp_setup` which bundles every recurring HF-cache / LoRA-attach / dataset-load / mark-done fix into one import — use it). Import these instead of re-deriving them.
4. If the task builds on prior experiments (references like #NNNN, or asks to 'leverage prior experiments'), read the relevant entries in ml/eval/experiments/done/ and ml/eval/experiments/falsified/ (confirmed and refuted results respectively). 'cbq history --grep <regex>' searches both plus parked records.

DELIVERABLES — both files in ${RESULTS_DIR}/ on this branch:

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
1. git add ${RESULTS_DIR}/
2. git commit with a descriptive message including '${TAG} [architect]'
3. git push -u origin '${BRANCH}'
4. Print a 1-paragraph summary: what the experiment tests, key design choices, what the worker should expect.

Do NOT merge to main. Do NOT touch main at all — the runner records the handoff in the queue database itself. Do NOT touch other experiments' directories. Do NOT run any cbq state-changing command (claim/ready/verdict/...) — the runner owns queue state for this phase.

---TASK ${TAG}---
HDR
  cat "$TASK" >> "$PROMPT_FILE"
  echo "" >> "$PROMPT_FILE"
  echo "$RETRY_CTX" >> "$PROMPT_FILE"
  # READ FIRST items 1-3, embedded so the session does not spend turns
  # re-opening the same three files every run.
  {
    echo ""
    echo "---PRE-GATHERED REFERENCE (embedded by the launch wrapper: the READ FIRST 1-3 files, from current origin/main)---"
    for ref in ml/eval/experiments/constraints.md ml/eval/experiments/README.md ml/eval/experiments/lib/README.md; do
      echo ""
      echo "===== $ref ====="
      head -c 30000 "$ref" 2>/dev/null || echo "(unreadable)"
      [ "$(wc -c < "$ref" 2>/dev/null || echo 0)" -gt 30000 ] && echo "...[TRUNCATED — open the file for the rest]"
    done
  } >> "$PROMPT_FILE"
  PROMPT=$(cat "$PROMPT_FILE")
  rm -f "$PROMPT_FILE"

  # Per-attempt wall cap: a hung session (rate-limit stall, network wedge) is
  # killed at the cap so it can't run unbounded, but the next attempt still gets
  # a fresh budget and new dice. rc=124 = timed out.
  ATTEMPT_START=$(date +%s)
  timeout --kill-after=60 "$ATTEMPT_TIMEOUT_SEC" claude -p "$PROMPT" \
    --model claude-sonnet-4-6 \
    --permission-mode acceptEdits \
    --dangerously-skip-permissions \
    --max-turns 150 \
    --output-format stream-json --verbose \
    >> "$LOG" 2>&1
  rc=$?
  [ "$rc" -eq 124 ] && echo "[$(date -Is)] CODE-ATTEMPT-TIMEOUT ${TAG} attempt=${attempt} after ${ATTEMPT_TIMEOUT_SEC}s (retry if attempts remain)" | tee -a "$INDEX"
  ATTEMPT_END=$(date +%s)
  ATTEMPT_DUR=$((ATTEMPT_END - ATTEMPT_START))

  # success = clean exit + branch pushed + both deliverables present on the remote branch
  attempt_pushed="no"
  attempt_complete="no"
  if git ls-remote --heads origin "$BRANCH" 2>/dev/null | grep -q "$BRANCH"; then
    attempt_pushed="yes"
    # explicit refspec: shallow clones are single-branch, so a bare
    # `git fetch origin <branch>` would only update FETCH_HEAD
    git fetch origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}" --quiet >>"$LOG" 2>&1
    if git show "origin/${BRANCH}:${RESULTS_DIR}/run.py" >/dev/null 2>&1 \
    && git show "origin/${BRANCH}:${RESULTS_DIR}/run.spec" >/dev/null 2>&1; then
      attempt_complete="yes"
    fi
  fi
  attempt_summaries+=("attempt ${attempt}: rc=${rc} pushed=${attempt_pushed} deliverables=${attempt_complete} dur=${ATTEMPT_DUR}s")
  echo "===== ATTEMPT ${attempt} END (rc=${rc}, pushed=${attempt_pushed}, deliverables=${attempt_complete}, ${ATTEMPT_DUR}s) =====" >> "$LOG"

  if [ "$rc" -eq 0 ] && [ "$attempt_complete" = "yes" ]; then
    final_rc=0
    break
  fi
  if [ "$rc" -eq 0 ] && [ "$attempt_pushed" = "no" ]; then
    prev_rc=99
  elif [ "$rc" -eq 0 ] && [ "$attempt_complete" = "no" ]; then
    prev_rc=98
  else
    prev_rc=$rc
  fi
done

TOTAL_END=$(date +%s)
TOTAL_DUR=$((TOTAL_END - TOTAL_START))

aws s3 cp "$LOG" "$S3_LOG_URL" --quiet 2>/dev/null && echo "log -> $S3_LOG_URL" || S3_LOG_URL="(s3 upload failed)"

SHA=""
if git ls-remote --heads origin "$BRANCH" 2>/dev/null | grep -q "$BRANCH"; then
  SHA=$(git ls-remote --heads origin "$BRANCH" 2>/dev/null | awk '{print substr($1,1,7)}')
fi

SUMMARY_LINES=$(printf '%s\n' "${attempt_summaries[@]}")

if [ "$final_rc" -eq 0 ]; then
  # read gpus requirement out of the spec for the handoff record
  SPEC_GPUS=$(git show "origin/${BRANCH}:${RESULTS_DIR}/run.spec" 2>/dev/null | sed -n 's/^gpus:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
  SPEC_TIMEOUT=$(git show "origin/${BRANCH}:${RESULTS_DIR}/run.spec" 2>/dev/null | sed -n 's/^timeout_min:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)

  echo "[$(date -Is)] CODE-OK ${TAG} ${RUN_ID} attempts=${attempts_used}/${MAX_ATTEMPTS} branch=${BRANCH} total_dur=${TOTAL_DUR}s" | tee -a "$INDEX"

  if "$CBQ" ready "$ID" --branch "$BRANCH" --sha "${SHA:-unknown}" \
       --gpus "${SPEC_GPUS:-1}" --timeout-min "${SPEC_TIMEOUT:-90}" \
       --log-url "$S3_LOG_URL" --attempts "$attempts_used"; then
    slack_post ":package: *architect-box* *ID:* \`${TAG}\` coded → ready
task: \`${SHORT}\`
branch: <https://github.com/Coral-Bricks-AI/www/tree/${BRANCH}|\`${BRANCH}\`> @ \`${SHA}\` · gpus: ${SPEC_GPUS:-1} · timeout: ${SPEC_TIMEOUT:-?}m
total: ${TOTAL_DUR}s · attempts: ${attempts_used}
log: \`${S3_LOG_URL}\` (also \`~/architect/logs/${RUN_ID}.jsonl\` on \`${HOST}\`)"
  else
    echo "[$(date -Is)] WARN: cbq ready failed for ${TAG} — row left in coding; reap will requeue" | tee -a "$INDEX"
  fi
  rm -f "$TASK"
  exit 0
else
  echo "[$(date -Is)] CODE-FAIL ${TAG} ${RUN_ID} attempts=${attempts_used}/${MAX_ATTEMPTS} rc=${final_rc} total_dur=${TOTAL_DUR}s" | tee -a "$INDEX"
  LAST_TAIL=$(extract_attempt_tail "$attempts_used" | tail -c 800)

  NOTE_FILE=$(mktemp /tmp/code-fail-${ID}.XXXXXX.md)
  {
    echo "## Code-phase failure summary (auto-appended by architect runner)"
    echo
    echo "- Run ID: \`${RUN_ID}\`"
    echo "- Phase: CODE (architect box, no execution attempted)"
    echo "- Attempts: ${attempts_used}/${MAX_ATTEMPTS}"
    echo "- Total wall: ${TOTAL_DUR}s"
    echo "- Final rc: ${final_rc}"
    echo "- Log (S3): \`${S3_LOG_URL}\`"
    echo
    echo "### Per-attempt"
    for line in "${attempt_summaries[@]}"; do echo "- $line"; done
    echo
    echo "### Last attempt tail"
    echo "\`\`\`"
    echo "${LAST_TAIL:-(no assistant text)}"
    echo "\`\`\`"
  } > "$NOTE_FILE"
  "$CBQ" code-failed "$ID" --note-file "$NOTE_FILE" --log-url "$S3_LOG_URL" \
    --attempts "$attempts_used" --error "code phase rc=${final_rc}" \
    || echo "[$(date -Is)] WARN: cbq code-failed failed for ${TAG}" | tee -a "$INDEX"

  # Constraint extraction: a code failure that reveals a durable box/toolchain
  # constraint must land in constraints.md BEFORE the record stops being looked
  # at — otherwise the next design walks into the same wall.
  git checkout main --quiet 2>/dev/null
  git fetch origin main --quiet 2>/dev/null
  git reset --hard origin/main --quiet 2>/dev/null
  CONSTRAINT_PROMPT_FILE=$(mktemp /tmp/constraint-prompt.XXXXXX.md)
  cat > "$CONSTRAINT_PROMPT_FILE" <<HDR
You are the ARCHITECT doing a 2-minute post-mortem of a CODE-phase failure in the experiments pipeline. You are inside Coral-Bricks-AI/www on main. Experiment ${TAG} (${SHORT}) failed its code phase after ${attempts_used} attempts.

Read the failure record below and ml/eval/experiments/constraints.md. Decide: does this failure reveal a DURABLE constraint of the GPU box, toolchain, or harness that is NOT already captured in constraints.md? (Examples: a library that cannot install, a CUDA/toolkit mismatch, a turn-budget pattern.) Transient flakes, task-specific bugs, and anything already covered do NOT qualify.

- If yes: append exactly ONE terse bullet to ml/eval/experiments/constraints.md under the right section, formatted '- C-<n>: <fact> (from ${TAG} code failure)' where <n> is one more than the highest existing C-number in the file (start at C-100 if none exist). Then git add, git commit -m 'constraint from code failure ${TAG} [architect]', git push origin main (on rejection: git pull --rebase and retry).
- If no: change nothing and print 'no durable constraint'.

---FAILURE RECORD---
HDR
  cat "$NOTE_FILE" >> "$CONSTRAINT_PROMPT_FILE"
  CONSTRAINT_PROMPT=$(cat "$CONSTRAINT_PROMPT_FILE")
  rm -f "$CONSTRAINT_PROMPT_FILE"
  timeout --kill-after=60 300 claude -p "$CONSTRAINT_PROMPT" \
    --model claude-sonnet-4-6 \
    --permission-mode acceptEdits \
    --dangerously-skip-permissions \
    --max-turns 15 \
    --output-format stream-json --verbose \
    >> "$LOG" 2>&1 || true
  aws s3 cp "$LOG" "$S3_LOG_URL" --quiet 2>/dev/null || true

  slack_post ":x: *architect-box* *ID:* \`${TAG}\` CODE phase FAILED after ${attempts_used}/${MAX_ATTEMPTS} attempts — PARKED (code_failed)
task: \`${SHORT}\`
total: ${TOTAL_DUR}s · final rc: ${final_rc}
${SUMMARY_LINES}
no auto-retry: the in-phase attempts already retried with failure context. Respin with a CHANGED task: \`cbq show ${ID} --field failure_md\`, edit, \`cbq submit <file> --parent ${ID%%.*}\`.
log: \`${S3_LOG_URL}\`
last attempt tail:
\`\`\`
${LAST_TAIL:-(no assistant text)}
\`\`\`"
  rm -f "$TASK" "$NOTE_FILE"
  exit 1
fi
