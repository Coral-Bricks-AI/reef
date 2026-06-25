#!/usr/bin/env bash
# Start the worker watchdog in a detached tmux session if not running
if tmux has-session -t worker 2>/dev/null; then
  echo "worker already running. Attach: tmux attach -t worker"
  exit 0
fi
mkdir -p ~/worker
tmux new-session -d -s worker "exec ~/bin/worker_watcher.sh 2>&1 | tee -a ~/worker/watcher.log"
echo "worker started in tmux session 'worker'. Attach: tmux attach -t worker"
