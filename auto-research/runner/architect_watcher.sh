#!/usr/bin/env bash
# Architect-side watcher for the split architect/worker pipeline (Postgres
# queue). Runs on the CPU-only orchestrator box. Polls cb_queue via cbq and
# dispatches:
#
#   executed rows -> architect_analyze_task.sh  (priority 1: reviewing worker
#       deviations + recording verdicts unblocks respins and feeds
#       constraints.md back into design)
#   enqueued rows -> architect_code_task.sh     (priority 2: writes run.py +
#       run.spec, promotes to ready for the GPU worker)
#
# The generator (suggest_experiment.sh) is PER (KIND, MACHINE): each kind
# (research / loadtest / finetune) maps to one GPU worker, and a group is live
# when a worker heartbeated recently (cbq workers). Demand is sized from those
# heartbeats: a group's GPU count (summed over its workers' gpus csv) sets its
# low-water at gpus+1 — enough live experiments (enqueued+coding+ready+
# executing) to occupy every GPU plus one buffered spare. For each live group
# whose iteration is quiet and whose live count is below that, the generator
# authors ONE new experiment, sized for the group's machine. Groups with no
# live worker get nothing, so a stopped box never accrues specs it can't
# execute.
#
# Claims are atomic DB row updates (`cbq claim`, FOR UPDATE SKIP LOCKED) —
# no lifecycle commits, no inflight/ snapshots. A phase session that crashes
# leaves its row claimed; `cbq reap` returns stale claims each poll.
set -uo pipefail
export PATH="$HOME/bin:$PATH"

WORKPOOL=~/architect
SUGGEST_LOG=~/architect/auto-suggest.log
export SUGGEST_LOG
POLL_SEC=${POLL_SEC:-30}
SUGGEST_INTERVAL_SEC=${SUGGEST_INTERVAL_SEC:-0}
# A kind counts as live when its worker heartbeated within this many minutes
# (the worker watchdog heartbeats every ~60s; 5 tolerates a few misses).
WORKER_ACTIVE_MIN=${WORKER_ACTIVE_MIN:-5}
# Kinds the GENERATOR must NOT auto-propose new top-level lines for (space-sep).
# loadtest is a finite, human-curated sweep plan (engines × quant × datasets in
# loadtest/tasks/), not an open-ended research line — auto-suggest would burn the
# rented Capacity Block on specs nobody asked for. ITERATION (analyze → next
# sub-id) is unaffected; this only gates brand-new lines. Set '' to allow all.
SUGGEST_EXCLUDE_KINDS=${SUGGEST_EXCLUDE_KINDS:-"loadtest swe-bench swe-bench-sweep"}
CBQ=${CBQ:-"$HOME/bin/cbq"}
export CBQ_ACTOR="architect:$(hostname)"
mkdir -p ~/architect/logs

# Role model:
#   - ITERATORS (analysts/fixers) are spawned PER COMPLETED GPU JOB: every
#     executed row gets its own fresh instance immediately (up to ANALYZE_MAX
#     concurrent), reviews the work, and either enqueues the next incremental
#     sub-ID experiment on that line or closes the line.
#   - CODE sessions run in a small parallel pool (CODE_SLOTS) turning enqueued
#     tasks into ready specs.
#   - The GENERATOR (suggest_experiment.sh) proposes brand-new top-level lines
#     of work, one (kind, machine) group at a time. It fires ONLY for groups
#     with a live worker heartbeat whose iteration is quiet and whose live
#     count is below their GPU count + 1 — new lines never compete with
#     iteration, and a kind whose box is offline gets nothing.
CODE_SLOTS=${CODE_SLOTS:-2}
ANALYZE_MAX=${ANALYZE_MAX:-8}

declare -A analyze_pids=()
declare -A code_pids=()
declare -A suggest_pids=()     # one in-flight generator per (kind, machine) group
declare -A excluded_logged=()  # SUGGEST_EXCLUDE_KINDS: log each skip once, not per poll

ensure_clone() {  # ensure_clone <dir>
  if [ ! -d "$1/.git" ]; then
    echo "[$(date -Is)] cloning phase workdir $1..."
    mkdir -p "$(dirname "$1")"
    gh repo clone Coral-Bricks-AI/www "$1" -- --depth 50 || return 2
  fi
  # shallow clones are single-branch; widen the refspec so exp/ branches can be
  # fetched into refs/remotes/origin/ (the phase runners depend on it)
  git -C "$1" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
}

free_idx() {  # free_idx <pids-assoc-name> <max> -> lowest index with no live pid
  local -n _pids=$1
  local max=$2 i p
  for ((i = 1; i <= max; i++)); do
    p="${_pids[$i]:-0}"
    if [ "$p" -eq 0 ] || ! kill -0 "$p" 2>/dev/null; then
      echo "$i"
      return 0
    fi
  done
  return 1
}

echo "[$(date -Is)] architect watcher up (pid=$$, poll=${POLL_SEC}s, low-water=per-group gpus+1, worker-active=${WORKER_ACTIVE_MIN}m)"

while true; do
  # crashed phase sessions leave rows claimed; return them to their queues
  "$CBQ" reap --quiet >/dev/null 2>&1 || true

  if ! counts=$("$CBQ" counts --json 2>/dev/null); then
    echo "[$(date -Is)] cbq counts failed (db unreachable?); retrying in ${POLL_SEC}s"
    sleep "$POLL_SEC"
    continue
  fi
  n_enqueued=$(jq -r '.enqueued' <<<"$counts")
  n_executed=$(jq -r '.executed' <<<"$counts")

  # ---- spawn one iterator per completed GPU job ----
  while [ "$n_executed" -gt 0 ]; do
    idx=$(free_idx analyze_pids "$ANALYZE_MAX") || {
      echo "[$(date -Is)] ANALYZE_MAX=${ANALYZE_MAX} iterators busy; remaining executed rows wait for the next poll"
      break
    }
    id=$("$CBQ" claim analyze --kind-not-in swe-bench,swe-bench-sweep 2>/dev/null) || break
    [ -z "$id" ] && break
    dir="$WORKPOOL/work-analyze-${idx}/www"
    if ! ensure_clone "$dir"; then
      "$CBQ" unclaim "$id" --note "phase clone failed" || true
      break
    fi
    echo "[$(date -Is)] iterator[$idx] spawned for: $id"
    ( WORKDIR="$dir" ~/bin/architect_analyze_task.sh "$id" || true ) &
    analyze_pids[$idx]=$!
    n_executed=$((n_executed - 1))
  done

  # ---- code pool (turn enqueued tasks into ready specs) ----
  while [ "$n_enqueued" -gt 0 ]; do
    idx=$(free_idx code_pids "$CODE_SLOTS") || break
    id=$("$CBQ" claim code --kind-not-in swe-bench,swe-bench-sweep 2>/dev/null) || break
    [ -z "$id" ] && break
    dir="$WORKPOOL/work-code-${idx}/www"
    if ! ensure_clone "$dir"; then
      "$CBQ" unclaim "$id" --note "phase clone failed" || true
      break
    fi
    echo "[$(date -Is)] coder[$idx] spawned for: $id"
    ( WORKDIR="$dir" ~/bin/architect_code_task.sh "$id" || true ) &
    code_pids[$idx]=$!
    n_enqueued=$((n_enqueued - 1))
  done

  # ---- generator: per (kind, machine) group with a live worker ----
  # Groups come from worker heartbeats, so the architect only authors work a
  # box is actually around to execute. Each group's demand target is its GPU
  # count + 1 (gpus csv summed over the group's live workers; a heartbeat
  # without gpus counts as 1 so its kind still gets work): enough live
  # experiments to occupy every GPU plus one buffered spare. Live counts
  # include executing — an experiment on a GPU still holds it. Per-group DB
  # counts (fetched fresh, after this poll's claims) subsume the old pid-based
  # gating: a live coder session is a 'coding' row, a live iterator an
  # 'analyzing' row. kind='' means a legacy any-kind worker: counts are then
  # filtered by machine alone.
  groups=$("$CBQ" workers --active-min "$WORKER_ACTIVE_MIN" --json 2>/dev/null \
    | jq -r 'group_by([.kind, .machine]) | .[]
        | [(.[0].kind // "any"), .[0].machine,
           ([.[] | ((.gpus // "") | split(",") | map(select(length > 0)) | length)
                 | if . == 0 then 1 else . end] | add)]
        | join("|")' 2>/dev/null)
  for entry in $groups; do
    IFS='|' read -r kind machine gpu_count <<<"$entry"
    case " $SUGGEST_EXCLUDE_KINDS " in
      *" $kind "*)
        if [ -z "${excluded_logged[$kind]:-}" ]; then
          echo "[$(date -Is)] kind ${kind} excluded from auto-suggest (SUGGEST_EXCLUDE_KINDS; logged once per watcher start)"
          excluded_logged[$kind]=1
        fi
        continue ;;
    esac
    kind_arg=""
    [ "$kind" != "any" ] && kind_arg="--kind $kind"
    low_water=$((gpu_count + 1))
    group_counts=$("$CBQ" counts $kind_arg --machine "$machine" --json 2>/dev/null) || continue
    live=$(jq -r '.enqueued + .coding + .ready + .executing' <<<"$group_counts")
    iterating=$(jq -r '.executed + .analyzing' <<<"$group_counts")
    if [ "$iterating" -gt 0 ] || [ "$live" -ge "$low_water" ]; then
      continue  # group busy iterating, or enough work for its GPUs already
    fi
    # Skip if a generator for this group is already in flight (backgrounded
    # below — a slow Claude session must not block the analyze/code pools).
    key="${kind}/${machine}"
    prev_pid="${suggest_pids[$key]:-0}"
    if [ "$prev_pid" -ne 0 ] && kill -0 "$prev_pid" 2>/dev/null; then
      continue
    fi
    now=$(date +%s)
    last=0
    stamp="$WORKPOOL/.last_suggest.${kind}.${machine}"
    [ -f "$stamp" ] && last=$(cat "$stamp" 2>/dev/null || echo 0)
    if [ $((now - last)) -ge "$SUGGEST_INTERVAL_SEC" ] && [ -x ~/bin/suggest_experiment.sh ]; then
      echo "[$(date -Is)] ${kind}/${machine} quiet, live=${live} < ${low_water} (${gpu_count} gpus + 1); firing generator"
      if ensure_clone "$WORKPOOL/workdir/www"; then
        # Stamp BEFORE backgrounding so the next poll's SUGGEST_INTERVAL_SEC
        # gate sees the fire time (not the completion time).
        echo "$now" > "$stamp"
        ( WORKDIR="$WORKPOOL/workdir/www" SUGGEST_KIND="${kind#any}" SUGGEST_MACHINE="$machine" \
            ~/bin/suggest_experiment.sh \
            || echo "[$(date -Is)] generator failed (non-fatal)" ) &
        suggest_pids[$key]=$!
      fi
    fi
  done
  sleep "$POLL_SEC"
done
