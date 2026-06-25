#!/usr/bin/env bash
# Usage: architect_analyze_task.sh <experiment-id>
# ANALYZE phase of the split architect/worker pipeline (Postgres queue). Runs
# on the CPU-only orchestrator box after the GPU worker finishes a job. The row
# is already claimed (status=analyzing) by the watcher.
#
#   - Spawns an architect Claude session that reviews the worker's DEVIATIONS
#     FIRST (the worker has full authority to change anything, including the
#     experiment design — review what was actually tested before reading any
#     metric), then the metrics, then records the verdict via cbq:
#       cbq verdict <id> done       (completed + meaningful, possibly REFRAMED)
#       cbq verdict <id> falsified  (hypothesis tested and refuted)
#       cbq verdict <id> blocked    (untestable under a constraint — paired
#                                    write with a C-NNN bullet in constraints.md)
#     done/falsified render an archive file committed to main; blocked leaves
#     NO archive record (the row + constraints.md are the record).
#   - On wrapper failure: cbq analyze-failed — one retry via the executed
#     queue, then parked as analyze_stuck (GPU data intact on the branch).
#   - Logs uploaded to s3://${EXP_S3_BUCKET}/cb-queue/architect-logs/
set -uo pipefail
# plan token: read fresh at every launch; ~/.oat is written by the monitor agent
export CLAUDE_CODE_OAUTH_TOKEN="$(cat ~/.oat)"
ID="$1"
MAX_ATTEMPTS=${MAX_ATTEMPTS:-2}
# Per-ATTEMPT wall cap (NOT cumulative): 30 min/attempt. Each attempt also calls
# `cbq touch` (below) to refresh claimed_at, so a live multi-attempt wrapper is
# never reaped — the reap window only needs to cover one attempt.
ATTEMPT_TIMEOUT_SEC=${ATTEMPT_TIMEOUT_SEC:-1800}

WORKDIR=${WORKDIR:-~/architect/workdir/www}
S3_PREFIX="s3://${EXP_S3_BUCKET}/cb-queue/architect-logs"
CBQ=${CBQ:-"$HOME/bin/cbq"}
export CBQ_ACTOR=${CBQ_ACTOR:-"architect:$(hostname)"}

# Route notifications by kind: loadtest tasks -> #infra-bench, everything else
# -> the default channel. Falls back to default if the infra hook is unset.
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
TAG="#${ID}"
BRANCH=$("$CBQ" show "$ID" --field branch 2>/dev/null || echo "exp/${ID}-${SHORT}")
TASK_DOC=$(mktemp /tmp/analyze-doc-${ID}.XXXXXX.md)
"$CBQ" show "$ID" --markdown > "$TASK_DOC"

STAMP=$(date +%Y%m%d-%H%M%S)
RUN_ID="${ID}-${SHORT}-analyze-${STAMP}"
LOG=~/architect/logs/${RUN_ID}.jsonl
INDEX=~/architect/INDEX.log
HOST=$(hostname)
S3_LOG_URL="${S3_PREFIX}/${RUN_ID}.jsonl"
RESULTS_DIR="${EXPERIMENTS_DIR}/results/${ID}-${SHORT}"

echo "[$(date -Is)] ANALYZE-START ${TAG} ${RUN_ID}" | tee -a "$INDEX"

cd "$WORKDIR"

TOTAL_START=$(date +%s)
final_rc=1
attempts_used=0
prev_rc=0
recorded=""

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  attempts_used=$attempt
  echo "===== ATTEMPT ${attempt} START =====" >> "$LOG"
  echo "[$(date -Is)] ANALYZE-ATTEMPT ${attempt}/${MAX_ATTEMPTS} ${TAG}" | tee -a "$INDEX"

  # Per-attempt claim heartbeat (see code wrapper): keep claimed_at fresh so a
  # live multi-attempt session isn't reaped as an orphan.
  "$CBQ" touch "$ID" --quiet 2>/dev/null || true

  # analyze works on a clean main checkout (constraints.md edits + the archive
  # export commit to main via cbq verdict --commit)
  git checkout main --quiet >>"$LOG" 2>&1
  git fetch origin main --quiet >>"$LOG" 2>&1
  git reset --hard origin/main --quiet >>"$LOG" 2>&1
  # pre-fetch the exp branch so the session's STEP 0 is already done and the
  # artifacts can be embedded into the prompt below
  git fetch origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}" --quiet >>"$LOG" 2>&1 || true

  if [ "$attempt" -eq 1 ]; then
    RETRY_CTX=""
  else
    PREV_TAIL=$(extract_attempt_tail $((attempt - 1)))
    cause="exited with code ${prev_rc}"
    [ "$prev_rc" -eq 99 ] && cause="exited cleanly but the queue row for ${TAG} is still 'analyzing' — your job is to actually run the cbq verdict command, not just describe it"
    RETRY_CTX="

---RETRY ${attempt} of ${MAX_ATTEMPTS}---
Your previous attempt ${cause}. The checkout was reset to origin/main.

Previous attempt's last output:
\`\`\`
${PREV_TAIL:-(no assistant text captured)}
\`\`\`"
  fi

  # Top-level heredoc into a tempfile (see git history of run_task.sh for why).
  # Unquoted HDR: no backticks or unescaped dollar-literals in the body.
  PROMPT_FILE=$(mktemp /tmp/architect-analyze-prompt.XXXXXX.md)
  cat > "$PROMPT_FILE" <<HDR
You are the ARCHITECT (analyze phase) for the experiments pipeline, on the CPU orchestrator box, inside Coral-Bricks-AI/www on main. The GPU worker finished executing experiment ${TAG}; its execution report is in the task document below. Your job: review what ACTUALLY happened, record the verdict in the queue database via cbq, extract lessons, and decide what is next.

QUEUE COMMANDS AVAILABLE (the 'cbq' CLI is on PATH; the row for ${TAG} is claimed by you, status 'analyzing'):
- cbq verdict ${ID} done --review-file <f> --commit
- cbq verdict ${ID} falsified --review-file <f> --commit
- cbq verdict ${ID} blocked --constraint C-<n> --review-file <f>
- cbq submit <task-file> --parent ${ID} --origin respin     (enqueue a follow-up; the id is allocated for you)
- cbq history --grep '<regex>'                              (search prior verdicts)
The database row is AUTHORITATIVE. 'done' and 'falsified' render an archive file (<project>/experiments/done/ or falsified/) and commit it to main together with any constraints.md edits in your checkout — that is what --commit does. 'blocked' writes NO archive file by design.

THE WORKER HAS FULL AUTHORITY TO DEVIATE. It may have changed knobs, code, or the experiment design itself — legitimately, because it is the one in contact with the hardware. Your review of those deviations comes BEFORE you read any metric. Judge whether the original instructions ever made sense under practical constraints, not whether the worker was obedient.

STEP 0 — FETCH: already done by the launch wrapper (origin/${BRANCH} is up to date) and the key artifacts — branch git log, deviations.md, run.spec, results.json, constraints.md — are EMBEDDED at the END of this prompt. Start from those copies. All artifacts live at ${RESULTS_DIR}/ on that branch; for anything not embedded or marked truncated (run.py, commit diffs, per-sample detail), read via git show origin/${BRANCH}:<path> (do not check the branch out; stay on main).

STEP 1 — DEVIATIONS FIRST. Read deviations.md before anything else. Then cross-check it against reality: git log origin/main..origin/${BRANCH} --oneline and inspect the diffs of commits tagged [worker]. If the report and the diff disagree, trust the diff and note the gap. For EACH deviation decide:
  - ACCEPT: the result still answers the original question.
  - REFRAME: the result answers a DIFFERENT question than the task claimed. The permanent record must state what was ACTUALLY tested — rewrite the headline claim and the hypothesis verdict accordingly. Never file a result under the original claim when the condition changed (a shrunk seq-len, a swapped model, a changed sampling regime all change the question).
  - RESPIN: the deviation gutted the experiment; a follow-up is needed that respects the constraint the worker hit. Submit it with --parent ${ID}.
A missing deviations.md when [worker] commits exist is itself a finding — reconstruct the deviations from the diff and say so in your review.

STEP 2 — only now read results.json. Interpret against the hypothesis and the run.spec success_criteria, AS REFRAMED by step 1. Sanity-check per_sample and the by-position/by-axis breakdowns for the failure modes the experiments README warns about (bimodal collapse hidden by averages, model emitting first-token-and-stop, etc.).

STEP 3 — CONSTRAINTS. Any deviation or failure caused by a wrong architect-side assumption becomes ONE terse bullet appended to ${EXPERIMENTS_DIR}/constraints.md. That file is read at design time by the next code phase and the auto-suggester — it is how the pipeline stops issuing specs the box cannot honor. Do not duplicate bullets that are already there. New bullets that a 'blocked' verdict will reference MUST carry a C-<n> id: '- C-<n>: <fact>' where <n> is one more than the highest existing C-number (start at C-100 if none exist). Edit the file in this checkout; do NOT commit it yourself — cbq verdict --commit picks it up in the same commit as the archive record (the paired write).

STEP 4 — LIB PROMOTION (successful experiments only). Scan the branch for NEW reusable, non-experiment-specific utilities. Promote clean versions to ${EXPERIMENTS_DIR}/lib/ on main, update lib/README.md, commit and push that separately yourself. Skip if nothing reusable.

STEP 5 — VERDICT. Write your review to a temp file (e.g. /tmp/review-${ID}.md): a '## Architect review' section containing per-deviation verdicts (ACCEPT/REFRAME/RESPIN with one line of reasoning each), the reframed claim if any, headline numbers, and your interpretation. Then record EXACTLY ONE verdict:
  - Status completed and the result is meaningful (possibly reframed) and CONFIRMS or extends the hypothesis: cbq verdict ${ID} done --review-file <f> --commit
  - The hypothesis was TESTED AND REFUTED (a real negative result — this is valuable, it is what stops future designs from re-running the idea): cbq verdict ${ID} falsified --review-file <f> --commit
  - The question could NOT be tested under a practical constraint (escalated runs usually land here): FIRST append the C-<n> constraint bullet to constraints.md and commit+push it yourself with message 'constraint C-<n> from ${TAG} [architect]', THEN: cbq verdict ${ID} blocked --constraint C-<n> --review-file <f>. The experiment stays resurrectable: when the constraint lifts, 'cbq list --blocked-on C-<n>' finds it. Do NOT file untestable work as falsified — that poisons the negative-knowledge archive with claims that were never tested.
  Distinguish carefully: 'falsified' means the experiment RAN and the answer was no. 'blocked' means the experiment could not run as designed. A REFRAMEd partial result that did test something goes to done/ or falsified/ on its merits.

STEP 6 — ITERATE OR CLOSE — mandatory; decide explicitly every time, in addition to the verdict. You are the ITERATOR for this line of work: brand-new lines come from the idle-time generator, never from you; you always build on the existing experiment.
  ITERATE (the default): write exactly ONE follow-up task file and submit it: cbq submit /tmp/respin-${ID}.task --parent ${ID} --origin respin. It must build directly on THIS result: vary one knob, chase the specific open question this run raised, or fix what blocked it — within constraints.md. Submit it QUICKLY: the GPUs idle when iterators dawdle, so a focused increment now beats a polished proposal later.
  CLOSE THE LINE: if nothing further on this line is worth GPU time (question answered, direction falsified, diminishing returns, constraint wall), submit NOTHING and include a '## Line closed' section in your review file stating the reason and what evidence would justify reopening it.

STEP 7 — print a 3-sentence summary: verdict, the key deviations and your ruling on them, and your iterate-or-close decision with the new id if you iterated (cbq submit prints it).

Do NOT modify anything on the exp branch. Do NOT touch other experiments' directories. Do NOT run cbq claim/unclaim/requeue — the watcher owns those.

---EXECUTED TASK ${TAG} (worker report included)---
HDR
  cat "$TASK_DOC" >> "$PROMPT_FILE"
  echo "" >> "$PROMPT_FILE"
  echo "$RETRY_CTX" >> "$PROMPT_FILE"
  # STEP 0 artifacts, embedded so the session starts reviewing instead of
  # fetching. Best-effort and size-bounded; a missing artifact prints its
  # git error, which is itself signal for the review.
  {
    echo ""
    echo "---PRE-GATHERED ARTIFACTS (embedded by the launch wrapper; branch already fetched)---"
    echo ""
    echo "===== git log origin/main..origin/${BRANCH} --oneline ====="
    git log "origin/main..origin/${BRANCH}" --oneline 2>&1 | head -40
    echo ""
    echo "===== origin/${BRANCH}:${RESULTS_DIR}/deviations.md ====="
    git show "origin/${BRANCH}:${RESULTS_DIR}/deviations.md" 2>&1 | head -c 12000
    echo ""
    echo "===== origin/${BRANCH}:${RESULTS_DIR}/run.spec ====="
    git show "origin/${BRANCH}:${RESULTS_DIR}/run.spec" 2>&1 | head -c 4000
    echo ""
    echo "===== origin/${BRANCH}:${RESULTS_DIR}/results.json (first 16000 bytes) ====="
    git show "origin/${BRANCH}:${RESULTS_DIR}/results.json" 2>&1 | head -c 16000
    [ "$(git show "origin/${BRANCH}:${RESULTS_DIR}/results.json" 2>/dev/null | wc -c)" -gt 16000 ] && echo "...[TRUNCATED — git show the file for per-sample detail]"
    echo ""
    echo "===== ${EXPERIMENTS_DIR}/constraints.md (current main) ====="
    head -c 30000 ${EXPERIMENTS_DIR}/constraints.md 2>/dev/null
  } >> "$PROMPT_FILE"
  PROMPT=$(cat "$PROMPT_FILE")
  rm -f "$PROMPT_FILE"

  # Per-attempt wall cap: a hung session is killed at the cap so it can't run
  # unbounded, but the next attempt still gets a fresh budget. rc=124 = timed out.
  ATTEMPT_START=$(date +%s)
  timeout --kill-after=60 "$ATTEMPT_TIMEOUT_SEC" claude -p "$PROMPT" \
    --model claude-sonnet-4-6 \
    --permission-mode acceptEdits \
    --dangerously-skip-permissions \
    --max-turns 80 \
    --output-format stream-json --verbose \
    >> "$LOG" 2>&1
  rc=$?
  [ "$rc" -eq 124 ] && echo "[$(date -Is)] ANALYZE-ATTEMPT-TIMEOUT ${TAG} attempt=${attempt} after ${ATTEMPT_TIMEOUT_SEC}s (retry if attempts remain)" | tee -a "$INDEX"
  ATTEMPT_END=$(date +%s)
  echo "===== ATTEMPT ${attempt} END (rc=${rc}, $((ATTEMPT_END - ATTEMPT_START))s)  =====" >> "$LOG"

  # success = the row reached a terminal verdict
  STATUS=$("$CBQ" show "$ID" --field status 2>/dev/null || echo "")
  case "$STATUS" in
    done|falsified|blocked)
      recorded="$STATUS"
      final_rc=0
      break
      ;;
  esac
  prev_rc=$rc
  [ "$rc" -eq 0 ] && prev_rc=99
done

TOTAL_END=$(date +%s)
TOTAL_DUR=$((TOTAL_END - TOTAL_START))

aws s3 cp "$LOG" "$S3_LOG_URL" --quiet 2>/dev/null && echo "log -> $S3_LOG_URL" || S3_LOG_URL="(s3 upload failed)"

# respins show up in the log as cbq submit output
RESPIN=$(jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text' "$LOG" 2>/dev/null \
  | grep -oE 'submitted: [0-9][0-9.]*-[a-z0-9-]+' | tail -1 | sed 's/^submitted: //')

if [ "$final_rc" -eq 0 ]; then
  echo "[$(date -Is)] ANALYZE-OK ${TAG} ${RUN_ID} verdict=${recorded} respin=${RESPIN:-none} total_dur=${TOTAL_DUR}s" | tee -a "$INDEX"
  case "$recorded" in
    done)      EMOJI=":white_check_mark:"; DETAIL="recorded: \`done/${ID}-${SHORT}.task\`" ;;
    falsified) EMOJI=":no_entry_sign:";    DETAIL="recorded: \`falsified/${ID}-${SHORT}.task\` (hypothesis refuted)" ;;
    blocked)   EMOJI=":construction:"
               CNST=$("$CBQ" show "$ID" --field blocked_on 2>/dev/null || echo "?")
               DETAIL="blocked on \`${CNST}\` — no archive record; resurrect via \`cbq list --blocked-on ${CNST}\`" ;;
  esac
  slack_post "${EMOJI} *architect-box* *ID:* \`${TAG}\` analyzed → ${recorded}
task: \`${SHORT}\`
respin: ${RESPIN:-none}
${DETAIL}
branch: <https://github.com/Coral-Bricks-AI/www/tree/${BRANCH}|\`${BRANCH}\`>
total: ${TOTAL_DUR}s · log: \`${S3_LOG_URL}\`"
  rm -f "$TASK_DOC"
  exit 0
else
  echo "[$(date -Is)] ANALYZE-FAIL ${TAG} ${RUN_ID} attempts=${attempts_used}/${MAX_ATTEMPTS} total_dur=${TOTAL_DUR}s" | tee -a "$INDEX"
  LAST_TAIL=$(extract_attempt_tail "$attempts_used" | tail -c 800)

  # one retry via the executed queue, then parked as analyze_stuck — the GPU
  # data is intact on the branch either way; never re-queue to the start
  OUTCOME=$("$CBQ" analyze-failed "$ID" --error "analyze wrapper rc=${final_rc}" 2>&1 || echo "cbq analyze-failed errored")
  echo "[$(date -Is)] ${OUTCOME}" | tee -a "$INDEX"
  if echo "$OUTCOME" | grep -q "analyze_stuck"; then
    REFILE_NOTE="PARKED as analyze_stuck — GPU data intact on \`${BRANCH}\`; a human can verdict from the execution report, or \`cbq requeue ${ID}\` for a fresh analyze round"
  else
    REFILE_NOTE="returned to executed for one more analyze round"
  fi

  slack_post ":warning: *architect-box* *ID:* \`${TAG}\` ANALYZE phase failed after ${attempts_used}/${MAX_ATTEMPTS} attempts — ${REFILE_NOTE}
task: \`${SHORT}\`
log: \`${S3_LOG_URL}\` (also \`~/architect/logs/${RUN_ID}.jsonl\` on \`${HOST}\`)
last attempt tail:
\`\`\`
${LAST_TAIL:-(no assistant text)}
\`\`\`"
  rm -f "$TASK_DOC"
  exit 1
fi
