#!/usr/bin/env bash
# Worker-side watchdog for the split architect/worker pipeline (Postgres
# queue). Runs on the GPU box. Does NO scheduling itself — the worker Claude
# session (worker_shift.sh) owns placement, fixing, and finalization. This loop
# only detects EVENTS and fires a shift, so no Claude tokens burn while jobs
# are happily running:
#
#   - ready rows in cb_queue AND at least one slot is free
#   - a registered job's pid is dead (needs finalize or fix)
#   - a running job's progress.log has stalled (> STALL_SEC without writes)
#   - an executing row claimed by this host holds no slot entry (orphan from a crash)
#   - heartbeat: jobs are running and the last shift was > HEARTBEAT_SEC ago
#
# State it reads (written by the worker shift): ~/worker/slots.json
set -uo pipefail
export PATH="$HOME/bin:$PATH"

BASE=~/worker/www
POLL_SEC=${POLL_SEC:-60}
HEARTBEAT_SEC=${HEARTBEAT_SEC:-600}
STALL_SEC=${STALL_SEC:-900}
WORKER_GPUS="${WORKER_GPUS:-0}"
export WORKER_GPUS
SLOTS=~/worker/slots.json
LAST_SHIFT_FILE=~/worker/.last_shift
# Watcher-owned per-retry bookkeeping (sessions never touch these):
#   SEEN     — {"<job>": pid} for which a 'launch' exec-event was recorded and
#              whose death has not yet been recorded (dedups events across polls)
#   SUPPRESS — {"<job>": until_epoch} dead-trigger suppression after a
#              crash-loop park shift has been fired
SEEN=~/worker/.exec_seen.json
SUPPRESS=~/worker/.dead_suppress.json
CRASH_LOOP_DEATHS=${CRASH_LOOP_DEATHS:-3}      # deaths within 1h => park, stop fixing
SUPPRESS_SEC=${SUPPRESS_SEC:-1800}
MAX_BACKOFF_SEC=${MAX_BACKOFF_SEC:-1800}
CBQ=${CBQ:-"$HOME/bin/cbq"}
# Honor a kind-distinct actor from the env (set on a shared box so two
# co-tenant workers don't collide on the same hostname); else the legacy id.
WORKER_ACTOR="${CBQ_ACTOR:-worker:$(hostname)}"
export CBQ_ACTOR="$WORKER_ACTOR"
# Kind + lease (both optional; unset => legacy any-kind, no clean-room drain).
# Each kind maps to one GPU worker; the worker sees and claims its kind only.
CBQ_KIND="${CBQ_KIND:-}"
KIND_ARG=""; [ -n "$CBQ_KIND" ] && KIND_ARG="--kind $CBQ_KIND"
# Hardware class this worker reports in its heartbeat (a10 | h100). NOT a
# routing filter — the architect uses it to size auto-suggested specs for
# whatever box currently serves this kind. The P5 tenants set h100.
CBQ_MACHINE="${CBQ_MACHINE:-a10}"
export CBQ_MACHINE
CB_LEASE_RESOURCE="${CB_LEASE_RESOURCE:-}"
# Concurrent shifts: a shift session is multi-minute and used to serialize the
# main loop, so any wedge stalled scheduling for the next ~22m. Now the watcher
# backgrounds shift sessions and fires up to MAX_INFLIGHT_SHIFTS at a time. cbq
# claims are atomic, slots.json writes are flock'd in smoke_and_launch.sh, so
# concurrent shifts cannot corrupt placement state. Backoff is computed from
# the count of .failed markers within the recent failure window.
#
# Default: one shift per schedulable GPU (floor 1, cap 4). A 1-GPU box gains
# nothing from concurrent shifts — they can only collide on its sole slot, and
# the non-flock'd writes inside a Claude shift's Bash commands (the slot wipe
# at 2026-06-14 19:35 root-caused here) lose the race. Multi-GPU boxes still
# parallelize up to GPU count so the multi-minute model-load smokes overlap.
_n_gpus=$(printf %s "$WORKER_GPUS" | awk -F, '{print NF}')
[ "${_n_gpus:-0}" -lt 1 ] && _n_gpus=1
[ "$_n_gpus" -gt 4 ] && _n_gpus=4
MAX_INFLIGHT_SHIFTS=${MAX_INFLIGHT_SHIFTS:-$_n_gpus}
FAIL_WINDOW_MIN=${FAIL_WINDOW_MIN:-5}
INFLIGHT_DIR=~/worker/inflight_shifts

mkdir -p ~/worker/{logs,inflight,jobs} "$INFLIGHT_DIR"

# Count live shift PIDs; GC pidfiles whose process has exited.
count_inflight() {
  local n=0 f p
  for f in "$INFLIGHT_DIR"/*.pid; do
    [ -e "$f" ] || continue
    p=$(cat "$f" 2>/dev/null)
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then n=$((n+1)); else rm -f "$f"; fi
  done
  echo "$n"
}
[ -s "$SEEN" ] || echo '{}' > "$SEEN"
[ -s "$SUPPRESS" ] || echo '{}' > "$SUPPRESS"

jqset() {  # jqset <file> <key> <value-or-null>: atomic single-key update
  local f="$1" k="$2" v="$3" tmp
  tmp=$(mktemp) || return 1
  if [ "$v" = null ]; then jq --arg k "$k" 'del(.[$k])' "$f" > "$tmp" 2>/dev/null
  else jq --arg k "$k" --argjson v "$v" '.[$k] = $v' "$f" > "$tmp" 2>/dev/null; fi \
    && mv "$tmp" "$f" || rm -f "$tmp"
}

[ -f "$BASE/.git/index.lock" ] && rm -f "$BASE/.git/index.lock"

if [ ! -d "$BASE/.git" ]; then
  echo "[$(date -Is)] cloning Coral-Bricks-AI/www..."
  gh repo clone Coral-Bricks-AI/www "$BASE" -- --depth 50 || exit 2
fi
# shallow clones are single-branch; widen the refspec so exp/ branches can be
# fetched into refs/remotes/origin/ (shift sessions depend on it)
git -C "$BASE" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'

# init the slot table: every schedulable GPU free
if [ ! -f "$SLOTS" ]; then
  jq -n --arg g "$WORKER_GPUS" '[$g | split(",")[] | {(.): null}] | add' > "$SLOTS"
  echo "[$(date -Is)] initialized slot table for GPUs ${WORKER_GPUS}"
fi

echo "[$(date -Is)] worker watchdog up (pid=$$, gpus=${WORKER_GPUS}, poll=${POLL_SEC}s, heartbeat=${HEARTBEAT_SEC}s, stall=${STALL_SEC}s)"

# Kind heartbeat: tells the architect's generator this kind has a live worker
# (and on what hardware), so it keeps authoring experiments for it. A
# background subloop (not a step of the main loop) because the main loop
# blocks for the whole duration of a synchronous shift — a long shift would
# otherwise make a busy kind look dead to the architect.
(
  while true; do
    "$CBQ" heartbeat $KIND_ARG --machine "$CBQ_MACHINE" --gpus "$WORKER_GPUS" >/dev/null 2>&1 || true
    sleep "${HEARTBEAT_EVERY_SEC:-60}"
  done
) &
HEARTBEAT_PID=$!
trap 'kill "$HEARTBEAT_PID" 2>/dev/null' EXIT

while true; do
  cd "$BASE"
  # Self-heal ~/.git-credentials if a prior shift truncated it (observed
  # 2026-06-14: a Claude shift session zero'd the file mid-run, blocking every
  # subsequent `git fetch` on a stuck username prompt for ~1h until the watcher
  # was manually unwedged). With the box's IAM role granted GetSecretValue on
  # prod/github/deploy-token, the watcher restores the file deterministically;
  # if the role lacks that permission the call fails and we proceed as before
  # (the next fetch will hang — fix the IAM, do not work around it here).
  CREDS="$HOME/.git-credentials"
  if [ ! -s "$CREDS" ]; then
    TOK=$(AWS_DEFAULT_REGION=us-east-1 aws secretsmanager get-secret-value \
            --secret-id prod/github/deploy-token --query SecretString \
            --output text 2>/dev/null | jq -r .GITHUB_TOKEN 2>/dev/null)
    if [ -n "$TOK" ] && [ "$TOK" != "null" ]; then
      umask 077
      printf 'https://x-access-token:%s@github.com\n' "$TOK" > "$CREDS"
      chmod 600 "$CREDS"
      echo "[$(date -Is)] self-heal: restored ~/.git-credentials from secrets manager"
    else
      echo "[$(date -Is)] self-heal SKIPPED: cannot read prod/github/deploy-token (IAM?); next git fetch will hang"
    fi
  fi
  # ALSO strip the broken host-specific credential helper that `gh auth setup-git`
  # re-installs whenever a Claude shift session touches gh tooling (observed
  # 2026-06-15 right after the file-self-heal landed). That config block:
  #   [credential "https://github.com"]
  #     helper =
  #     helper = !/usr/bin/gh auth git-credential
  # OVERRIDES the global `credential.helper = store` for github.com URLs and
  # routes auth through Ubuntu 22.04's broken `gh auth git-credential` (gh 2.4.0),
  # which fails the credential-helper handshake → every `git fetch` hangs on a
  # username prompt even though ~/.git-credentials is intact. Idempotent strip:
  # the unset-all is a noop when the section is absent.
  if git config --global --get "credential.https://github.com.helper" >/dev/null 2>&1; then
    git config --global --unset-all "credential.https://github.com.helper" 2>/dev/null || true
    git config --global --remove-section "credential.https://github.com" 2>/dev/null || true
    echo "[$(date -Is)] self-heal: stripped broken credential.https://github.com.helper (gh auth setup-git override)"
  fi
  # the base clone stays current for worktree checkouts of exp/ branches
  git checkout main --quiet 2>/dev/null
  [ -f "$BASE/.git/index.lock" ] && rm -f "$BASE/.git/index.lock"
  git fetch origin main --quiet 2>/dev/null || true
  git reset --hard origin/main --quiet 2>/dev/null || true

  if ! counts=$("$CBQ" counts $KIND_ARG --json 2>/dev/null); then
    echo "[$(date -Is)] cbq counts failed (db unreachable?); retrying in ${POLL_SEC}s"
    sleep "$POLL_SEC"
    continue
  fi
  n_ready=$(jq -r '.ready' <<<"$counts")

  # Clean-room drain: if a peer holds an exclusive node lease, do NOT launch
  # new work this poll (finalize/fix/observe still fire). Empty => free to run.
  drain=""
  if [ -n "$CB_LEASE_RESOURCE" ]; then
    holder=$("$CBQ" lease-active "$CB_LEASE_RESOURCE" 2>/dev/null | head -1)
    [ -n "$holder" ] && drain="$holder"
  fi

  # Deterministic reconcile + schedule BEFORE waking the agent. Phase A frees
  # the GPU of any cleanly-completed dead job (finalize_completed.sh) the
  # instant it finishes; Phase B (opt-in WORKER_DET_SCHED=1) launches ready
  # specs whose shared venv already exists. Real failures, venv builds, and
  # smoke failures are left to fall through to the agentic shift below. Both
  # phases are idempotent + lock-safe, so this is harmless even mid-shift.
  if [ -x "$HOME/bin/worker_reconcile.sh" ]; then
    "$HOME/bin/worker_reconcile.sh" >> ~/worker/reconcile.log 2>&1 \
      || echo "[$(date -Is)] worker_reconcile.sh exited nonzero (non-fatal)"
    # Phase B may have claimed ready specs; refresh the count so the launch
    # trigger below doesn't fire a shift for work reconcile already placed.
    if counts=$("$CBQ" counts $KIND_ARG --json 2>/dev/null); then
      n_ready=$(jq -r '.ready' <<<"$counts")
    fi
  fi

  free=$(jq '[to_entries[] | select(.value == null)] | length' "$SLOTS" 2>/dev/null || echo 0)

  dead=0; stalled=0; running=0; park_reasons=""
  now=$(date +%s)
  # distinct running jobs (a 2-GPU job occupies 2 slots)
  for job in $(jq -r '[to_entries[] | select(.value != null) | .value.job] | unique | .[]' "$SLOTS" 2>/dev/null); do
    pid=$(jq -r --arg j "$job" '[to_entries[] | select(.value.job == $j) | .value.pid] | first' "$SLOTS")
    dir=$(jq -r --arg j "$job" '[to_entries[] | select(.value.job == $j) | .value.dir] | first' "$SLOTS")
    started=$(jq -r --arg j "$job" '[to_entries[] | select(.value.job == $j) | .value.started] | first // empty' "$SLOTS")
    exp_id="${job%%-*}"
    started_epoch=$(date -d "$started" +%s 2>/dev/null || echo "$now")
    seen_pid=$(jq -r --arg j "$job" '.[$j] // 0' "$SEEN")
    if kill -0 "$pid" 2>/dev/null; then
      running=$((running + 1))
      # per-retry memory: record each launch exactly once per (job, pid)
      if [ "$seen_pid" != "$pid" ]; then
        "$CBQ" exec-event "$exp_id" --kind launch --pid "$pid" >/dev/null 2>&1 || true
        jqset "$SEEN" "$job" "$pid"
      fi
      plog="$dir/ml/eval/experiments/results/$job/progress.log"
      [ -f "$plog" ] || plog="$dir/progress.log"
      if [ -f "$plog" ]; then
        [ -n "$(find "$plog" -mmin "+$((STALL_SEC / 60))" 2>/dev/null)" ] && stalled=$((stalled + 1))
      elif [ $((now - started_epoch)) -ge "$STALL_SEC" ]; then
        # alive but never produced a progress.log: hung at startup — without
        # this branch such a job is invisible to stall detection forever
        stalled=$((stalled + 1))
      fi
    else
      # record the death exactly once, with attempt lifetime + failure tail
      if [ "$seen_pid" = "$pid" ]; then
        wall=$((now - started_epoch)); [ "$wall" -lt 0 ] && wall=0
        note=$(tail -c 300 "/tmp/run-$job.out" 2>/dev/null | tr '\n\t' '  ')
        "$CBQ" exec-event "$exp_id" --kind death --pid "$pid" --wall-sec "$wall" \
          ${note:+--note "$note"} >/dev/null 2>&1 || true
        jqset "$SEEN" "$job" null
        # crash-loop debounce: N deaths inside an hour => one park shift, then
        # suppress this job's dead-trigger so it cannot re-fire fix shifts
        d1h=$("$CBQ" exec-summary "$exp_id" --json 2>/dev/null | jq -r '.deaths_1h // 0')
        if [ "${d1h:-0}" -ge "$CRASH_LOOP_DEATHS" ]; then
          park_reasons="${park_reasons}PARK ${exp_id}: crash loop, ${d1h} deaths in 1h (cbq exec-summary ${exp_id}) — escalate, do not fix; "
          jqset "$SUPPRESS" "$job" $((now + SUPPRESS_SEC))
        fi
      fi
      sup_until=$(jq -r --arg j "$job" '.[$j] // 0' "$SUPPRESS")
      if [ "$now" -lt "${sup_until:-0}" ] && [ -z "$park_reasons" ]; then
        : # suppressed: a park shift already fired for this crash-looper
      else
        dead=$((dead + 1))
      fi
    fi
  done
  # prune bookkeeping for jobs no longer in slots (finalized/removed)
  for stale in $(jq -r --slurpfile s "$SLOTS" \
      'keys[] as $k | select(([$s[0] | to_entries[] | select(.value != null) | .value.job] | index($k)) == null) | $k' \
      "$SEEN" 2>/dev/null); do jqset "$SEEN" "$stale" null; done

  # executing rows claimed by this host whose job holds no slot = orphans
  orphans=0
  for job_id in $(
    "$CBQ" list --status executing --claimed-by "$WORKER_ACTOR" --json 2>/dev/null \
      | jq -r '.[].id' 2>/dev/null
  ); do
    in_slots=$(jq -r --arg j "$job_id" \
      '[to_entries[] | select(.value.job != null) | select(.value.job | startswith($j + "-"))] | length' \
      "$SLOTS" 2>/dev/null || echo 0)
    [ "$in_slots" -eq 0 ] && orphans=$((orphans + 1))
  done

  now=$(date +%s)
  last_shift=0
  [ -f "$LAST_SHIFT_FILE" ] && last_shift=$(cat "$LAST_SHIFT_FILE" 2>/dev/null || echo 0)

  reasons=""
  [ "$dead" -gt 0 ]    && reasons="${reasons}${dead} job(s) finished or died; "
  [ "$stalled" -gt 0 ] && reasons="${reasons}${stalled} job(s) stalled >$((STALL_SEC / 60))min; "
  [ "$orphans" -gt 0 ] && reasons="${reasons}${orphans} orphaned executing claim(s) with no slot; "
  # Launch trigger — suppressed while draining for a peer's clean-room lease.
  if [ -z "$drain" ]; then
    [ "$n_ready" -gt 0 ] && [ "$free" -gt 0 ] && reasons="${reasons}${n_ready} ready spec(s) with ${free} free GPU(s); "
  elif [ "$n_ready" -gt 0 ] && [ "$free" -gt 0 ]; then
    echo "[$(date -Is)] draining: node leased by ${drain}; holding ${n_ready} ready spec(s) off ${free} free GPU(s)"
  fi
  [ "$running" -gt 0 ] && [ $((now - last_shift)) -ge "$HEARTBEAT_SEC" ] && reasons="${reasons}heartbeat observation (${running} running); "

  # park instructions go first so the session cannot miss them
  reasons="${park_reasons}${reasons}"

  if [ -n "$reasons" ]; then
    inflight=$(count_inflight)
    if [ "$inflight" -ge "$MAX_INFLIGHT_SHIFTS" ]; then
      echo "[$(date -Is)] skip shift (inflight=${inflight}/${MAX_INFLIGHT_SHIFTS}): ${reasons}"
    else
      pf="$INFLIGHT_DIR/$(date +%s)-$$-$RANDOM.pid"
      ( ~/bin/worker_shift.sh "$reasons"; rc=$?; rm -f "$pf"; [ "$rc" -ne 0 ] && touch "${pf%.pid}.failed" ) &
      shift_pid=$!
      echo "$shift_pid" > "$pf"
      echo "[$(date -Is)] fired shift pid=${shift_pid} (inflight=$((inflight + 1))/${MAX_INFLIGHT_SHIFTS}): ${reasons}"
      date +%s > "$LAST_SHIFT_FILE"
    fi
  fi

  # exponential backoff on shift failures within the recent window: without
  # this, a shift that dies instantly (disk full, bad token) re-fires every
  # poll — observed at 56 wakes/hour for 5 hours on 2026-06-12. Counts .failed
  # markers in the last FAIL_WINDOW_MIN minutes (concurrent shifts mean we
  # can't use a 'consecutive failures' counter anymore).
  fail_n=$(find "$INFLIGHT_DIR" -name '*.failed' -mmin "-${FAIL_WINDOW_MIN}" 2>/dev/null | wc -l)
  sleep_for="$POLL_SEC"
  if [ "$fail_n" -gt 0 ]; then
    sleep_for=$((POLL_SEC * (2 ** (fail_n < 5 ? fail_n : 5))))
    [ "$sleep_for" -gt "$MAX_BACKOFF_SEC" ] && sleep_for="$MAX_BACKOFF_SEC"
    echo "[$(date -Is)] backing off ${sleep_for}s after ${fail_n} shift failure(s) in last ${FAIL_WINDOW_MIN}m"
  fi
  find "$INFLIGHT_DIR" -name '*.failed' -mmin "+60" -delete 2>/dev/null || true
  sleep "$sleep_for"
done
