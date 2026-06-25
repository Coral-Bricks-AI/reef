#!/usr/bin/env bash
# finalize_completed.sh <id> <slug> — DETERMINISTIC finalize for a job that ran
# to completion. Exists because the agentic worker shift can wedge on the
# finalize duty (judgment + commit + push + cbq executed), and a single wedged
# finalize starves scheduling: the row stays 'executing' with a dead pid, its
# slot stays occupied, and every subsequent shift re-grinds the same job instead
# of claiming ready work (root-caused on 0059.1, 2026-06-13).
#
# This path is NON-agentic on purpose. It only acts on an UNAMBIGUOUS completion
# signal (results.json present AND progress.log's last event is phase=done /
# "results written"); otherwise it exits 10 and the shift falls back to manual
# handling. The common case — a clean completed run — finalizes in seconds with
# no model load and no LLM judgment.
#
# Exit codes: 0 finalized; 10 not-cleanly-complete (caller handles); 2x setup error.
set -uo pipefail

ID="${1:?usage: finalize_completed.sh <id> <slug>}"
SLUG="${2:?usage: finalize_completed.sh <id> <slug>}"
JOB="${ID}-${SLUG}"
CBQ="${CBQ:-$HOME/bin/cbq}"
WT="$HOME/worker/jobs/${JOB}"
RES="$WT/ml/eval/experiments/results/${JOB}"
SLOTS="$HOME/worker/slots.json"
LOCK="$HOME/worker/.slots.lock"
log() { echo "[$(date -Is)] finalize_completed[$JOB]: $*"; }

[ -d "$RES" ] || { log "results dir missing: $RES"; exit 22; }
[ -f "$RES/results.json" ] || { log "no results.json — not cleanly complete"; exit 10; }
# last meaningful progress event must signal completion
LAST="$(grep -E '"phase": *"done"|results written|"msg": *"done"' "$RES/progress.log" 2>/dev/null | tail -1)"
[ -n "$LAST" ] || { log "progress.log has no done/results-written event — not cleanly complete"; exit 10; }

cd "$WT" || { log "cd worktree failed"; exit 22; }

# deviations.md is required by the analyze phase; a clean auto-finalize records none.
[ -f "$RES/deviations.md" ] || printf '## none\n(auto-finalized by finalize_completed.sh: completed run, no worker deviations recorded)\n' > "$RES/deviations.md"

# commit + push any uncommitted artifacts (idempotent — "nothing to commit" is fine)
git add "ml/eval/experiments/results/${JOB}" 2>/dev/null || true
git commit -q -m "[worker] ${ID}: finalize completed run (auto)" 2>/dev/null || true
BR="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
timeout 90 git push -q origin "HEAD:${BR}" 2>&1 | grep -vE '^remote:|pull/new' || true
SHA="$(git rev-parse HEAD 2>/dev/null)"

# headline straight from the job's own done event (no analysis)
HEAD_LINE="$(echo "$LAST" | sed 's/[[:space:]]\+/ /g' | cut -c1-400)"
REP="$(mktemp /tmp/finrep-${ID}.XXXXXX.md)"
{
  echo "## Execution report (worker)"
  echo "- Status: completed"
  echo "- Branch: ${BR} @ ${SHA}"
  echo "- GPUs used: (auto-finalize) · finalized by finalize_completed.sh"
  echo "- Headline: ${HEAD_LINE}"
  echo "- Deviations: 0 (auto-finalize of a cleanly-completed run)"
} > "$REP"

if "$CBQ" executed "$ID" --exec-status completed --report-file "$REP" 2>&1 | grep -q "executed"; then
  log "cbq executed OK"
else
  # already finalized by a racing shift, or wrong state — not fatal; still free the slot
  log "cbq executed did not transition (already finalized or wrong state)"
fi
rm -f "$REP"

# Archive the trajectory to S3 — progress.log + /tmp/run-${JOB}.out aren't
# in git (www/.gitignore excludes *.log), so without this step the training
# trajectory dies with the worker box (capacity-block teardown, terminate).
# results.json / smoke_results.json / deviations.md / adapter dir are already
# committed above. Best-effort: an S3 failure must NOT block the finalize.
S3_TRAJ="s3://${EXP_S3_BUCKET}/cb-queue/job-trajectories/${JOB}"
[ -f "$RES/progress.log" ] && \
  aws s3 cp "$RES/progress.log" "$S3_TRAJ/progress.log" --quiet 2>/dev/null \
  && log "trajectory -> $S3_TRAJ/progress.log" \
  || log "progress.log S3 upload skipped (missing or failed)"
RUNOUT="/tmp/run-${JOB}.out"
[ -f "$RUNOUT" ] && \
  aws s3 cp "$RUNOUT" "$S3_TRAJ/run.out" --quiet 2>/dev/null \
  && log "run.out -> $S3_TRAJ/run.out" \
  || log "run.out S3 upload skipped (missing or failed)"

# free any slot holding this job, under the shared lock
( flock 9
  tmp="$(mktemp)"
  JOB="$JOB" python3 - "$SLOTS" > "$tmp" <<'PYEOF'
import json, os, sys
try: s = json.load(open(sys.argv[1]))
except Exception: s = {}
job = os.environ["JOB"]
for g, v in list(s.items()):
    if v and v.get("job") == job:
        s[g] = None
json.dump(s, sys.stdout, indent=1)
PYEOF
  [ -s "$tmp" ] && mv "$tmp" "$SLOTS" || rm -f "$tmp"
) 9>"$LOCK"

git worktree remove --force "$WT" 2>/dev/null || true
rm -f "$HOME/worker/inflight/${JOB}.task" 2>/dev/null || true
log "finalized + slot freed + worktree removed"
exit 0
