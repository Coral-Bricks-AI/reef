#!/usr/bin/env bash
# Auto-suggest the next experiment when the queue is idle.
#
# Invoked by architect_watcher.sh when all iterators are idle, nothing is
# enqueued, and the ready queue is below its low-watermark. Spawns a short
# Claude session that:
#   1. reads ml/eval/experiments/{done,falsified,README,constraints,lib} for context
#   2. designs ONE next experiment as a .task file
#   3. submits it via `cbq submit --origin auto-suggest` (the ID is allocated
#      by the queue — no fetch/re-check/race dance)
#
# The watcher's next poll claims it normally; from there the lifecycle is
# identical to a user-submitted task. origin='auto-suggest' on the row is the
# marker. Veto with `cbq cancel <id>` before the watcher claims it (~30s).
set -uo pipefail
# plan token: read fresh at every launch; ~/.oat is written by the monitor agent
export CLAUDE_CODE_OAUTH_TOKEN="$(cat ~/.oat)"

WORKDIR=${WORKDIR:-~/architect/workdir/www}
LOG=${SUGGEST_LOG:-~/architect/auto-suggest.log}
mkdir -p "$(dirname "$LOG")"
HOST=$(hostname)
CBQ=${CBQ:-"$HOME/bin/cbq"}
export CBQ_ACTOR=${CBQ_ACTOR:-"architect:${HOST}"}

# Target kind, set by the watcher from live worker heartbeats; each kind maps
# to one GPU worker. SUGGEST_MACHINE is the hardware class that worker's
# heartbeat reported — it sizes the design and is stamped on the row, but
# routing is by kind alone. SUGGEST_KIND='' = legacy any-kind worker; submit
# into the default kind then.
SUGGEST_KIND="${SUGGEST_KIND:-}"
SUGGEST_MACHINE="${SUGGEST_MACHINE:-a10}"
SUBMIT_KIND="${SUGGEST_KIND:-research}"
case "$SUGGEST_MACHINE" in
  h100) MACHINE_DESC="H100-class (this kind's worker owns 4x H100 80GB on the shared P5 box, so a spec may request up to 4 GPUs — large models and batches are fine)" ;;
  *)    MACHINE_DESC="A10-class (1x NVIDIA A10G 23GB, g5.xlarge) — a single small GPU; size models and batches to fit it" ;;
esac
case "$SUBMIT_KIND" in
  loadtest) KIND_DESC="drives an already-running inference server (throughput/latency sweeps); it does not load models in-process" ;;
  finetune) KIND_DESC="training runs" ;;
  *)        KIND_DESC="in-process model evals (load model, run benchmark, write results.json)" ;;
esac

cd "$WORKDIR"
git fetch origin main --quiet 2>/dev/null
git reset --hard origin/main --quiet 2>/dev/null

slack_post() {
  [ -z "${SLACK_WEBHOOK_URL:-}" ] && return 0
  curl -sS -X POST -H 'Content-type: application/json' \
    --data "$(jq -nc --arg t "$1" '{text:$t}')" \
    "$SLACK_WEBHOOK_URL" >/dev/null 2>&1 || true
}

echo "[$(date -Is)] auto-suggest starting (kind=${SUBMIT_KIND} machine=${SUGGEST_MACHINE})" | tee -a "$LOG"

# Write the suggester prompt to a tempfile via a top-level quoted heredoc.
# Done this way rather than PROMPT=$(cat <<HDR ... HDR) because bash's $()
# parser still scans the heredoc body for keywords and parens — words like
# "do" / "done" inside the prose trip the parser. Top-level heredocs are
# parsed differently and pass anything verbatim.
PROMPT_FILE=$(mktemp /tmp/suggester-prompt.XXXXXX.md)
cat > "$PROMPT_FILE" <<'HDR'
You are the experiments GENERATOR. You fire only when all iteration has gone quiet: the per-experiment iterators (who handle the immediate next increment after each run) either closed their lines or have nothing pending. Unlike them, you see the WHOLE portfolio, and you have two moves — pick whichever is more valuable right now:

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
HDR

# Kind assignment, expanded here (the main heredoc is quoted). The suggester
# designs FOR this kind's worker and must submit with these exact flags.
{
  echo ""
  echo "TARGET KIND (assigned by the watcher; each kind maps to one GPU worker, and this kind's worker is live)"
  echo ""
  echo "- kind=${SUBMIT_KIND} — ${KIND_DESC}"
  echo "- The worker serving this kind currently runs on machine=${SUGGEST_MACHINE}: ${MACHINE_DESC}"
  echo "- Design the experiment to fit this kind's execution model and that hardware budget. Submit with exactly: --kind ${SUBMIT_KIND} --machine ${SUGGEST_MACHINE}"
  echo "- A task submitted under the wrong kind lands on a worker that can't run it."
  echo "- constraints.md was accumulated mostly on A10-class hardware: VRAM/OOM ceilings do not transfer across machine classes, but tooling/process lessons do."
} >> "$PROMPT_FILE"

# Portfolio state, embedded so the session spends its turns designing rather
# than listing and reading. Best-effort and size-bounded.
{
  echo ""
  echo "---PRE-GATHERED PORTFOLIO STATE (collected by the launch wrapper at $(date -Is))---"
  echo ""
  echo "===== cbq list --json (live + parked) ====="
  "$CBQ" list --json 2>&1 | head -c 8000
  echo ""
  echo "===== cbq list --status blocked --json ====="
  "$CBQ" list --status blocked --json 2>&1 | head -c 4000
  echo ""
  echo "===== ml/eval/experiments/done/ (newest first) ====="
  ls -t ml/eval/experiments/done/ 2>/dev/null | head -50
  echo ""
  echo "===== ml/eval/experiments/falsified/ (newest first) ====="
  ls -t ml/eval/experiments/falsified/ 2>/dev/null | head -50
  echo ""
  echo "===== 3 most recent done entries (each truncated to 6000 bytes) ====="
  for f in $(ls -t ml/eval/experiments/done/* 2>/dev/null | head -3); do
    echo ""
    echo "--- $f ---"
    head -c 6000 "$f"
    echo ""
  done
  echo ""
  echo "===== ml/eval/experiments/constraints.md ====="
  head -c 30000 ml/eval/experiments/constraints.md 2>/dev/null
  echo ""
  echo "===== ml/eval/experiments/README.md ====="
  head -c 30000 ml/eval/experiments/README.md 2>/dev/null
  echo ""
  echo "===== ml/eval/experiments/lib/README.md ====="
  head -c 30000 ml/eval/experiments/lib/README.md 2>/dev/null
} >> "$PROMPT_FILE"

PROMPT=$(cat "$PROMPT_FILE")
rm -f "$PROMPT_FILE"

# Run the suggester with a tight turn budget
claude -p "$PROMPT" \
  --model claude-sonnet-4-6 \
  --permission-mode acceptEdits \
  --dangerously-skip-permissions \
  --max-turns 40 \
  --output-format text \
  >> "$LOG" 2>&1
RC=$?
echo "[$(date -Is)] auto-suggest claude exited rc=$RC" | tee -a "$LOG"

# Detect what (if anything) it actually submitted
NEW_TASK=$(tail -50 "$LOG" | grep -oE 'submitted: [0-9][0-9.]*-[a-z0-9-]+' | tail -1 | sed 's/^submitted: //')

if [ -n "$NEW_TASK" ]; then
  NEW_ID=${NEW_TASK%%-*}
  echo "[$(date -Is)] auto-suggest submitted: $NEW_TASK" | tee -a "$LOG"
  slack_post ":robot_face: *auto-suggest*: queued \`${NEW_TASK}\` [kind=${SUBMIT_KIND} machine=${SUGGEST_MACHINE}] — veto within ~30s with \`cbq cancel ${NEW_ID}\`"
else
  echo "[$(date -Is)] auto-suggest produced no submission" | tee -a "$LOG"
  # quiet: do not slack-spam on no-suggest
fi
