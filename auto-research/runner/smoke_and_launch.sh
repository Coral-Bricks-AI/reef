#!/usr/bin/env bash
# smoke_and_launch.sh — deterministic per-GPU "skip-smoke-if-unchanged, smoke,
# launch, register slot" unit. The worker shift BACKGROUNDS one of these per
# free GPU so multiple jobs come up CONCURRENTLY instead of serializing on the
# blocking smoke (a 20B model load is ~3-4 min each). It also implements the
# smoke-skip memo: if the worktree HEAD SHA already passed --smoke for this row
# (cbq smoked_sha), the smoke is skipped and we launch straight away.
#
# This is intentionally NOT agentic — it is the one path that must be exactly
# right (GPU pinning, slot bookkeeping, smoke gating), so it is fixed bash the
# worker Claude calls rather than reproduces by hand each shift.
#
# The slot is held for the WHOLE lifetime (smoke included): the helper writes a
# provisional slot under its OWN pid before smoking, so a concurrent shift never
# sees the just-claimed row as a slot-less orphan and double-launches it. After
# launch the slot is swapped to the run pid; on smoke failure the slot is freed.
#
# Usage:
#   smoke_and_launch.sh <id> <slug> <gpu_csv> <venv> <timeout_min>
#                       <smoke_min> <smoke_flag> <results_dir> "<run_flags>"
#
# Contract:
#   - exit 0 + slot holds the run pid          => launched
#   - exit 0 + ~/worker/launch-<id>.skipped    => smoke skipped (sha match), launched
#   - exit 20 + ~/worker/launch-<id>.fail      => smoke FAILED; slot freed; NOT launched
#   - exit 2x                                  => setup error; NOT launched
set -uo pipefail

ID="$1"; SLUG="$2"; GPUS="$3"; VENV="$4"; TIMEOUT_MIN="$5"
SMOKE_MIN="$6"; SMOKE_FLAG="$7"; RESULTS_DIR="$8"; RUN_FLAGS="${9:-}"

CBQ="${CBQ:-$HOME/bin/cbq}"
SLOTS="$HOME/worker/slots.json"
LOCK="$HOME/worker/.slots.lock"
PY="$HOME/queue/venvs/$VENV/bin/python"
JOB="${ID}-${SLUG}"
WORKTREE_ROOT="$HOME/worker/jobs/${JOB}"
OUT="/tmp/run-${JOB}.out"
FAIL_MARK="$HOME/worker/launch-${ID}.fail"
SKIP_MARK="$HOME/worker/launch-${ID}.skipped"
rm -f "$FAIL_MARK" "$SKIP_MARK"

log() { echo "[$(date -Is)] smoke_and_launch[$JOB gpu=$GPUS]: $*"; }

# write_slot <pid> <phase> | free_slot — both under flock; parallel siblings
# touch the same slots.json, so every read-modify-write is serialized.
_slots_py() {
  "$PY" - "$SLOTS" "$@" <<'PYEOF'
import json, os, sys
slots_path, action = sys.argv[1], sys.argv[2]
try:    slots = json.load(open(slots_path))
except Exception: slots = {}
gpus = os.environ["GPUS"].split(",")
if action == "free":
    for g in gpus: slots[g] = None
else:  # write
    entry = {"job": os.environ["JOB"], "pid": int(sys.argv[3]),
             "dir": os.environ["DIR"], "timeout_min": int(os.environ["TIMEOUT_MIN"]),
             "started": os.environ["STARTED"], "phase": sys.argv[4]}
    for g in gpus: slots[g] = entry
json.dump(slots, sys.stdout, indent=1)
PYEOF
}
_slot_apply() {  # <action> [pid] [phase]
  ( flock 9
    tmp="$(mktemp)"
    if GPUS="$GPUS" JOB="$JOB" DIR="$WORKTREE_ROOT" TIMEOUT_MIN="$TIMEOUT_MIN" \
       STARTED="$STARTED" _slots_py "$@" > "$tmp" && [ -s "$tmp" ]; then
      mv "$tmp" "$SLOTS"
    else rm -f "$tmp"; fi
  ) 9>"$LOCK"
}
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

[ -x "$PY" ]            || { log "venv python missing: $PY"; exit 21; }
[ -d "$RESULTS_DIR" ]   || { log "results dir missing: $RESULTS_DIR"; exit 22; }
cd "$RESULTS_DIR"       || { log "cd failed: $RESULTS_DIR"; exit 22; }
# FORCE a writable HF_HOME. The worker env points HF_HOME at the read-only
# shared cache (/opt/hf-cache), but `datasets` needs to write its cache root —
# a ${HF_HOME:-...} default would (wrongly) inherit the read-only path and
# every dataset load dies with PermissionError. Models still resolve via
# cache_dir=/opt/hf-cache/hub inside run.py; FA3 kernels also cache here.
export HF_HOME="$HOME/worker/hf-home"
mkdir -p "$HF_HOME"
# The venv is NOT activated (we run $VENV/bin/python directly), so its bin dir is
# off PATH and vLLM's bare shell-outs (ninja for torch.compile/CUDA-graph, the
# vllm CLI, etc.) hit FileNotFoundError -> EngineCore dies rc=1. Put it on PATH
# for the smoke AND the launched run (both inherit this env). Also park config/
# cache dirs at writable paths (the tenant has no ~/.config). Root-caused on 0067.
export PATH="$HOME/queue/venvs/$VENV/bin:$PATH"
export MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/mpl}"
export VLLM_NO_USAGE_STATS="${VLLM_NO_USAGE_STATS:-1}"
export DO_NOT_TRACK="${DO_NOT_TRACK:-1}"

# Hold the slot under THIS helper's pid for the whole smoke (closes the
# claim->launch orphan window for a concurrent shift).
_slot_apply write "$$" smoking

# ── Smoke-skip memo ─────────────────────────────────────────────────────────
HEAD_SHA="$(git -C "$WORKTREE_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
SMOKED_SHA="$("$CBQ" show "$ID" --field smoked_sha 2>/dev/null || echo)"
if [ -n "$HEAD_SHA" ] && [ "$HEAD_SHA" != "unknown" ] && [ "$HEAD_SHA" = "$SMOKED_SHA" ]; then
  log "SKIP smoke — HEAD $HEAD_SHA already smoked"
  : > "$SKIP_MARK"
else
  log "SMOKE — HEAD=$HEAD_SHA smoked=${SMOKED_SHA:-<none>}"
  if ! CUDA_VISIBLE_DEVICES="$GPUS" timeout "${SMOKE_MIN}m" "$PY" -u run.py $SMOKE_FLAG > "$OUT.smoke" 2>&1; then
    log "SMOKE FAILED (see $OUT.smoke)"
    { echo "smoke failed at sha=$HEAD_SHA"; tail -20 "$OUT.smoke"; } > "$FAIL_MARK"
    _slot_apply free
    exit 20
  fi
  "$CBQ" mark-smoked "$ID" --sha "$HEAD_SHA" 2>/dev/null \
    && log "recorded smoked_sha=$HEAD_SHA" || log "WARN mark-smoked failed (non-fatal)"
fi

# ── Launch full run (nohup, detached), then swap the slot to the run pid ─────
nohup env CUDA_VISIBLE_DEVICES="$GPUS" HF_HOME="$HF_HOME" \
  timeout "${TIMEOUT_MIN}m" "$PY" -u run.py $RUN_FLAGS > "$OUT" 2>&1 &
PID=$!
_slot_apply write "$PID" running
log "LAUNCHED pid=$PID timeout=${TIMEOUT_MIN}m; slot(s) $GPUS -> running"
exit 0
