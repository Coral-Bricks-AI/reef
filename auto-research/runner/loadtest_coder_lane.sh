#!/usr/bin/env bash
# loadtest_coder_lane.sh — a dedicated, single-slot CODE/ANALYZE lane for
# kind=loadtest, run as a SECOND tmux session on the architect-box box alongside
# the main architect_watcher.sh.
#
# Why: the main watcher's code pool (CODE_SLOTS, no --kind filter) is shared
# across research/loadtest/finetune, so a burst of finetune/research codings can
# leave loadtest tasks enqueued while the rented H100 box sits idle. This lane
# gives loadtest its own coder so it never waits behind other kinds.
#
# Why it does not starve other workloads:
#   1. It claims ONLY `--kind loadtest` (cbq claim ... --kind loadtest), so it is
#      structurally incapable of taking a finetune (or research) row.
#   2. It runs at most ONE phase session at a time (sequential), nice'd to +10,
#      so on the shared t3.large the main watcher's finetune sessions (nice 0)
#      always win CPU contention.
#   3. By draining loadtest here, it FREES the main pool's slots to spend more
#      time on finetune/research — net less contention for primary workload, not more.
#
# Stateless like the main watcher: claims are atomic DB ops (FOR UPDATE SKIP
# LOCKED), so racing the main watcher for a loadtest row is safe (one wins). The
# main watcher's `cbq reap` each poll also recovers this lane's crashed claims.
set -uo pipefail
export PATH="$HOME/bin:$PATH"

POLL_SEC=${POLL_SEC:-30}
CBQ=${CBQ:-"$HOME/bin/cbq"}
LANE_NICE=${LANE_NICE:-10}
WP=~/architect/lane-loadtest
export CBQ_ACTOR="architect-loadtest:$(hostname)"
mkdir -p "$WP"

ensure_clone() {  # ensure_clone <dir>
  if [ ! -d "$1/.git" ]; then
    echo "[$(date -Is)] cloning lane workdir $1..."
    mkdir -p "$(dirname "$1")"
    gh repo clone Coral-Bricks-AI/www "$1" -- --depth 50 || return 2
  fi
  git -C "$1" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
}

# run_phase <code|analyze> <task-script> — claim ONE loadtest row for this phase
# and run it to completion (foreground => 1-at-a-time). Returns 0 if it ran.
run_phase() {
  local phase="$1" script="$2" id dir
  id=$("$CBQ" claim "$phase" --kind loadtest 2>/dev/null) || return 1
  [ -z "$id" ] && return 1
  dir="$WP/work-${phase}/www"
  if ! ensure_clone "$dir"; then
    "$CBQ" unclaim "$id" --note "lane clone failed" || true
    return 1
  fi
  echo "[$(date -Is)] lane[$phase] running: $id"
  WORKDIR="$dir" nice -n "$LANE_NICE" "$HOME/bin/$script" "$id" || true
  return 0
}

echo "[$(date -Is)] loadtest coder lane up (pid=$$, poll=${POLL_SEC}s, kind=loadtest, 1 slot, nice=+${LANE_NICE})"
while true; do
  ran=0
  # analyze first (unblocks respins/closes lines), then code (enqueued -> ready).
  run_phase analyze architect_analyze_task.sh && ran=1
  run_phase code    architect_code_task.sh    && ran=1
  [ "$ran" -eq 0 ] && sleep "$POLL_SEC"
done
