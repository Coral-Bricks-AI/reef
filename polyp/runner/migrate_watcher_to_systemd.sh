#!/usr/bin/env bash
# One-shot cutover on architect-box: tmux-hosted architect watcher -> supervised
# systemd unit (architect-box.service, Type=simple/Restart=always/KillMode=process).
#
# Safe ordering: freeze the old watcher loop (SIGSTOP: no new claims; already-
# running phase sessions are separate processes and unaffected) -> wait for
# in-flight code/analyze wrappers to finish -> remove the old tmux session
# (loadtest-arch session on the shared tmux server is untouched) -> start the
# supervised service. Prereq: the new unit is installed + daemon-reloaded.
#
# Run ON the box:  ~/bin/migrate_watcher_to_systemd.sh
set -u

WATCHER_PID=$(pgrep -f "bash /home/ubuntu/bin/architect_watcher.sh" | head -1)
if [ -z "$WATCHER_PID" ]; then
  echo "no watcher process found; starting service directly"
  sudo systemctl enable --now architect-box
  exit 0
fi

echo "[1/4] freezing watcher pid $WATCHER_PID (no new claims; children unaffected)"
kill -STOP "$WATCHER_PID"

echo "[2/4] waiting for in-flight code/analyze sessions to finish (poll 30s, max 45m)"
for i in $(seq 1 90); do
  LIVE=$(pgrep -fc "architect_(code|analyze)_task.sh" || true)
  [ "${LIVE:-0}" -eq 0 ] && break
  echo "  t+$((i*30))s: $LIVE session(s) still running"
  sleep 30
done
LIVE=$(pgrep -fc "architect_(code|analyze)_task.sh" || true)
if [ "${LIVE:-0}" -ne 0 ]; then
  echo "WARN: $LIVE session(s) still alive after 45m; proceeding (their claims self-heal via reap in <=90m)"
fi

echo "[3/4] killing frozen watcher + tmux session 'architect' (loadtest-arch untouched)"
kill -9 "$WATCHER_PID" 2>/dev/null
tmux kill-session -t architect 2>/dev/null

echo "[4/4] starting supervised service"
sudo systemctl enable --now architect-box
sleep 3
systemctl is-active architect-box && pgrep -af architect_watcher.sh
echo "DONE — live view: journalctl -fu architect-box  or  tail -f ~/architect/watcher.log"
