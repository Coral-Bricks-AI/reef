#!/usr/bin/env bash
# worker_reconcile.sh [--dry-run]
#
# DETERMINISTIC reconcile + schedule, run by the watcher (worker_watcher.sh)
# at the top of every poll, BEFORE it decides whether to wake the agentic
# shift. It pulls the mechanical 90% of scheduling OFF the agent's critical
# path — the agent (worker_shift.sh) is a multi-minute `claude -p` session with
# a hard 22-min wall-clock timeout, and when it wedges it holds the scheduling
# turn and starves every free GPU (root-caused on 0059.1, and again on
# 2026-06-13 when 4 H100s sat idle behind two wedged shifts).
#
# Two phases:
#
#   PHASE A — FINALIZE / HEAL SLOTS (always on). For each slot whose pid is DEAD,
#     call the deterministic finalize_completed.sh. It self-checks the UNAMBIGUOUS
#     completion signal (results.json + progress.log 'done'); on a clean run it
#     commits/pushes artifacts, runs `cbq executed`, frees the slot, and removes
#     the worktree — in seconds, no model load, no LLM. A dead-but-not-clean job
#     (a crash, or a user kill) is the harder case: the experiment-record side
#     (fix vs escalate, deviations.md, cbq executed) is the shift's job, but the
#     SLOT must still be freed here, because slots.json is what the dashboard and
#     the watcher's scheduling read for "GPU available". Leaving a dead pid in
#     slots.json makes the dashboard lie about the box state and starves the
#     watcher's launch trigger until the next Claude shift unwedges. So we free
#     the slot under the shared flock, leave the worktree + the cbq executing
#     claim untouched, and let the next shift handle the orphan (the watcher
#     already fires on "orphaned executing claim with no slot").
#
#   PHASE B — SCHEDULE (opt-in: WORKER_DET_SCHED=1). While GPUs are free and
#     ready specs exist, claim + check out + launch the ones whose SHARED venv
#     already exists, via smoke_and_launch.sh — same lowest-id / hold-for-big /
#     backfill-<=30min policy the shift uses (duty 5a). Anything that needs a
#     venv BUILD, a code FIX, or whose smoke FAILS is deferred to the agentic
#     shift (unclaimed back to ready). The agent stays in the loop for the hard
#     10%; the easy launches no longer wait on it.
#
# SAFETY. Safe to run concurrently with a shift: claims are atomic (`cbq claim`,
# rc 3 = already taken), every slots.json write goes through the SAME flock the
# helpers use, smoke_and_launch holds the slot under its own pid before the
# smoke (no orphan window), and finalize_completed is idempotent. In --dry-run
# nothing is claimed/launched/finalized — it only logs the decisions, so you can
# validate the policy against live state before arming Phase B.
#
# Env (inherited from the watcher / /etc/worker-box.env):
#   WORKER_GPUS, CBQ, CBQ_KIND, CB_LEASE_RESOURCE, WORKER_DET_SCHED
set -uo pipefail
export PATH="$HOME/bin:$PATH"

DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
LBL=""; [ "$DRY" = 1 ] && LBL="/dry"

BASE="${WORKER_BASE:-$HOME/worker/www}"
SLOTS="${SLOTS:-$HOME/worker/slots.json}"
LOCK="$HOME/worker/.slots.lock"
CBQ="${CBQ:-$HOME/bin/cbq}"
QVENVS="$HOME/queue/venvs"
JOBS="$HOME/worker/jobs"
INFLIGHT="$HOME/worker/inflight"
CBQ_KIND="${CBQ_KIND:-}"
KIND_ARG=(); [ -n "$CBQ_KIND" ] && KIND_ARG=(--kind "$CBQ_KIND")
CB_LEASE_RESOURCE="${CB_LEASE_RESOURCE:-}"
DET_SCHED="${WORKER_DET_SCHED:-0}"
# `cbq stop` sets stop_requested_at on an executing row; we honor it by SIGTERM
# (then SIGKILL after STOP_GRACE_SEC if the pid is still alive). The dead pid
# then falls into the dead-not-clean heal below and the slot is freed.
STOP_GRACE_SEC="${STOP_GRACE_SEC:-10}"
WORKER_ACTOR_FILTER="${CBQ_ACTOR:-}"

log() { echo "[$(date -Is)] reconcile${LBL}: $*"; }

# Free every slot whose entry's job matches $1, under the shared flock. Mirrors
# the pattern in finalize_completed.sh so reconcile and the helpers serialize
# read-modify-writes against slots.json the same way.
free_slot_for_job() {  # <job>
  ( flock 9
    tmp="$(mktemp)"
    JOB="$1" python3 - "$SLOTS" > "$tmp" <<'PYEOF'
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
}

[ -s "$SLOTS" ] || { log "no slots.json yet — nothing to do"; exit 0; }

# ── PRE-PHASE — honor stop-intent on executing jobs ──────────────────────────
# Polls `cbq stop-pending` for executing rows this actor owns that have a stop
# request, looks each one up in slots.json, and SIGTERMs the pid. A short grace
# window covers clean-shutdown / CUDA-release; if the pid is still alive after
# that, SIGKILL. Finalization (cbq executed, deviations.md, etc.) is left to
# the shift — same contract as a crash; the user-stop just looks like a death
# the watcher records via its normal exec-event 'death' write.
honor_stop_intent() {
  local actor_arg=()
  [ -n "$WORKER_ACTOR_FILTER" ] && actor_arg=(--claimed-by "$WORKER_ACTOR_FILTER")
  local ids
  ids=$("$CBQ" stop-pending "${actor_arg[@]}" --json 2>/dev/null) || return 0
  [ -n "$ids" ] && [ "$ids" != "[]" ] || return 0
  local id job pid
  while read -r id; do
    [ -n "$id" ] || continue
    job=$(jq -r --arg i "$id" \
      '[to_entries[] | select(.value != null and (.value.job // "" | startswith($i + "-"))) | .value.job] | first // empty' \
      "$SLOTS")
    [ -n "$job" ] || { log "stop-pending ${id}: no live slot — leave for shift (orphan path)"; continue; }
    pid=$(jq -r --arg j "$job" \
      '[to_entries[] | select(.value.job == $j) | .value.pid] | first // empty' "$SLOTS")
    [ -n "$pid" ] || continue
    if ! kill -0 "$pid" 2>/dev/null; then
      log "stop-pending ${id}: pid ${pid} already dead — heal path will free slot"
      continue
    fi
    if [ "$DRY" = 1 ]; then
      log "WOULD SIGTERM pid=${pid} for stop-requested ${id} (${job}); grace=${STOP_GRACE_SEC}s then SIGKILL"
      continue
    fi
    log "honoring stop request for ${id}: SIGTERM pid=${pid} (job=${job})"
    # SIGTERM the timeout wrapper AND its python child — `timeout` propagates,
    # but a process group kill is more reliable when the child is in its own pgid.
    local pgid; pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    [ -n "$pgid" ] && kill -TERM "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    local waited=0
    while [ "$waited" -lt "$STOP_GRACE_SEC" ] && kill -0 "$pid" 2>/dev/null; do
      sleep 1; waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      log "stop-pending ${id}: still alive after ${STOP_GRACE_SEC}s — SIGKILL pid=${pid}"
      [ -n "$pgid" ] && kill -KILL "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
  done < <(jq -r '.[]' <<<"$ids" 2>/dev/null)
}

# ── PHASE A — finalize cleanly-completed dead jobs ───────────────────────────
finalize_dead_clean() {
  local job pid id slug rc res
  while IFS=$'\t' read -r job pid; do
    [ -n "$job" ] || continue
    kill -0 "$pid" 2>/dev/null && continue           # still running — leave it
    id="${job%%-*}"; slug="${job#*-}"
    res="$JOBS/$job/ml/eval/experiments/results/$job"
    if [ "$DRY" = 1 ]; then
      if [ -f "$res/results.json" ] \
         && grep -qE '"phase": *"done"|results written|"msg": *"done"' "$res/progress.log" 2>/dev/null; then
        log "WOULD finalize (clean completion): $job"
      else
        log "WOULD free slot (dead, not cleanly complete — crash/user-kill); leave worktree+claim for shift: $job"
      fi
      continue
    fi
    "$HOME/bin/finalize_completed.sh" "$id" "$slug" >/dev/null 2>&1; rc=$?
    case "$rc" in
      0)  log "finalized clean completion: $job (slot freed, worktree removed)";;
      10) free_slot_for_job "$job"
          log "freed slot (dead, not cleanly complete — crash/user-kill); worktree+claim left for shift: $job";;
      *)  free_slot_for_job "$job"
          log "finalize_completed rc=$rc for $job — slot freed, worktree+claim left for shift";;
    esac
  done < <(jq -r '[to_entries[] | select(.value != null)
                    | {job: .value.job, pid: (.value.pid // 0)}]
                  | unique_by(.job)[] | [.job, (.pid|tostring)] | @tsv' "$SLOTS" 2>/dev/null)
}

# ── PHASE B — deterministic schedule of ready specs onto free GPUs ────────────
free_gpu_ids() { jq -r 'to_entries | map(select(.value == null) | .key) | sort_by(tonumber) | .[]' "$SLOTS" 2>/dev/null; }
spec_scalar()  { awk -v k="$1" 'index($0, k":")==1 { sub("^"k":[ \t]*", ""); print; exit }' "$2" 2>/dev/null; }

# Echo the run flags from run.spec (key 'run_flags' or 'args'); empty if none.
# Returns 2 if the value is a YAML folded/block scalar (starts with > or |, or
# the value sits on following lines) — those need real YAML parsing, so the
# caller DEFERS the spec to the agentic shift rather than risk launching with
# wrong/empty flags. Only the unambiguous single-line scalar is handled here.
spec_runflags() {  # <spec>
  local line val
  line=$(awk 'index($0,"run_flags:")==1 || index($0,"args:")==1 { print; exit }' "$1" 2>/dev/null)
  [ -n "$line" ] || { printf ''; return 0; }          # no flags key — fine
  val=${line#*:}
  val=$(printf '%s' "$val" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
  case "$val" in ""|">"*|"|"*) return 2;; esac          # folded/block — defer
  printf '%s' "$(printf '%s' "$val" | sed -E "s/^'(.*)'$/\\1/; s/^\"(.*)\"$/\\1/")"
}

# Return a claimed row to ready and drop its worktree — used on every deferral
# so a spec we can't serve deterministically goes back for the shift to handle.
defer() {  # <id> <job>
  local id="$1" job="$2"
  "$CBQ" unclaim "$id" >/dev/null 2>&1 || true
  git -C "$BASE" worktree remove --force "$JOBS/$job" 2>/dev/null || true
  rm -f "$INFLIGHT/${job}.task" 2>/dev/null || true
}

schedule() {
  # Live scheduling is opt-in; a dry-run always previews the decisions (it
  # claims/launches nothing) so the policy can be validated before arming.
  if [ "$DET_SCHED" != 1 ] && [ "$DRY" != 1 ]; then
    log "Phase B disabled (WORKER_DET_SCHED!=1) — schedule deferred to shift"
    return 0
  fi
  if [ -n "$CB_LEASE_RESOURCE" ]; then
    local holder; holder="$("$CBQ" lease-active "$CB_LEASE_RESOURCE" 2>/dev/null | head -1)"
    [ -n "$holder" ] && { log "draining (node leased by $holder) — not scheduling"; return 0; }
  fi

  local -a FREE; mapfile -t FREE < <(free_gpu_ids)
  local nfree=${#FREE[@]}
  [ "$nfree" -gt 0 ] || return 0

  # NEVER claim a spec whose id already occupies a slot. The DB and slot table
  # can drift — a row gets unclaimed/reaped back to 'ready' while its detached
  # process keeps running (observed on 0060.1.1/0060.1.2, 2026-06-13). Claiming
  # such a row would launch a SECOND copy of the same experiment onto another
  # GPU, corrupting the shared worktree/checkpoints. Seed the exclude set from
  # the slots; in --dry-run we also add each previewed claim so the preview is
  # realistic (live mode drops the row from 'ready' via the claim itself).
  declare -A EXCL
  local sid
  while read -r sid; do [ -n "$sid" ] && EXCL["$sid"]=1; done \
    < <(jq -r '[to_entries[] | select(.value != null) | (.value.job | split("-")[0])] | unique | .[]' "$SLOTS" 2>/dev/null)

  while [ "$nfree" -gt 0 ]; do
    local ready; ready="$("$CBQ" list --status ready "${KIND_ARG[@]}" --json 2>/dev/null)" || break
    [ -n "$ready" ] || break
    local exjson='[]'
    [ "${#EXCL[@]}" -gt 0 ] && exjson=$(printf '%s\n' "${!EXCL[@]}" | jq -R . | jq -cs .)

    # Eligible = ready rows whose id is not already in a slot / not yet previewed.
    # Placement policy (mirrors shift duty 5a): take the lowest-id (the query
    # sorts by ord) eligible spec if it fits; otherwise HOLD GPUs for it and
    # only backfill a smaller eligible spec past it when it is short (<=30 min).
    local pick; pick=$(jq -c --argjson f "$nfree" --argjson ex "$exjson" '
      [ .[] | select(.id as $i | ($ex | index($i)) | not) ] as $elig
      | ($elig[0] // empty) as $lo
      | if ($lo | not) then empty
        elif (($lo.gpus // 1) <= $f) then $lo
        else ([ $elig[] | select((.gpus // 1) <= $f and (.timeout_min // 999) <= 30) ][0] // empty) end' <<<"$ready")
    [ -n "$pick" ] && [ "$pick" != "null" ] || {
      log "nothing schedulable: no eligible ready spec fits ${nfree} free GPU(s) (excludes in-slot ids)"; break; }

    local id slug g job
    id=$(jq -r '.id' <<<"$pick"); slug=$(jq -r '.slug' <<<"$pick"); g=$(jq -r '.gpus // 1' <<<"$pick")
    job="${id}-${slug}"
    local gpu_csv; gpu_csv=$(IFS=,; echo "${FREE[*]:0:$g}")

    if [ "$DRY" = 1 ]; then
      log "WOULD claim+launch ${job} (gpus=${g}) onto GPU(s) [${gpu_csv}]"
      EXCL["$id"]=1; FREE=("${FREE[@]:$g}"); nfree=$((nfree - g)); continue
    fi

    # Atomic claim. rc 3 => a racing worker took it; re-list and reassess.
    if ! "$CBQ" claim execute --id "$id" "${KIND_ARG[@]}" >/dev/null 2>&1; then
      log "claim race for ${id} — re-listing"; continue
    fi
    EXCL["$id"]=1
    mkdir -p "$INFLIGHT"
    "$CBQ" show "$id" --markdown > "$INFLIGHT/${job}.task" 2>/dev/null || true

    # Check out the exp/ branch as a worktree (skip if a stale dir already exists).
    local wt="$JOBS/$job" br="exp/${job}"
    if [ ! -d "$wt" ]; then
      git -C "$BASE" fetch origin "${br}:${br}" --quiet 2>/dev/null || true
      if ! git -C "$BASE" worktree add "$wt" "$br" --quiet 2>/dev/null; then
        log "worktree add failed for ${job} — defer to shift"; defer "$id" "$job"; break
      fi
    fi

    local res="$wt/ml/eval/experiments/results/$job" spec
    spec="$res/run.spec"
    [ -f "$spec" ] || { log "run.spec missing for ${job} — defer to shift"; defer "$id" "$job"; break; }

    local venv tmin smin smoke
    venv=$(spec_scalar venv "$spec")
    tmin=$(spec_scalar timeout_min "$spec"); : "${tmin:=90}"
    smin=$(spec_scalar smoke_timeout_min "$spec"); : "${smin:=10}"
    smoke=$(spec_scalar smoke "$spec" | sed -E "s/^['\"]//; s/['\"]$//")

    # Deterministic launch requires a pre-existing SHARED venv and an explicit
    # smoke flag. A missing venv (needs `setup:` build) or a smoke-less spec is
    # the shift's job — defer so we never run a full job as its own "smoke".
    if [ -z "$venv" ] || [ ! -x "$QVENVS/$venv/bin/python" ]; then
      log "venv '${venv:-<none>}' absent for ${job} — defer to shift (venv build)"; defer "$id" "$job"; break
    fi
    if [ -z "$smoke" ]; then
      log "no smoke flag in run.spec for ${job} — defer to shift"; defer "$id" "$job"; break
    fi
    local rflags
    if ! rflags=$(spec_runflags "$spec"); then
      log "run flags are a folded/multi-line scalar in run.spec for ${job} — defer to shift"
      defer "$id" "$job"; break
    fi

    log "launch ${job} gpus=${gpu_csv} venv=${venv} timeout=${tmin}m smoke='${smoke}' smoke_min=${smin}m run_flags='${rflags}'"
    nohup "$HOME/bin/smoke_and_launch.sh" "$id" "$slug" "$gpu_csv" "$venv" "$tmin" "$smin" "$smoke" "$res" "$rflags" \
      > "/tmp/launch-${id}.log" 2>&1 &
    FREE=("${FREE[@]:$g}"); nfree=$((nfree - g))
  done
}

# Sourcing guard: `WORKER_RECONCILE_LIB=1 source worker_reconcile.sh` loads the
# functions (spec parsing, placement) WITHOUT running a pass — used by tests.
[ "${WORKER_RECONCILE_LIB:-}" = 1 ] && return 0

honor_stop_intent
finalize_dead_clean
schedule
exit 0
