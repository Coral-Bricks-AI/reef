#!/usr/bin/env bash
# Start the architect watcher in a detached tmux session if not running
if tmux has-session -t architect 2>/dev/null; then
  echo "architect already running. Attach: tmux attach -t architect"
  exit 0
fi
mkdir -p ~/architect
tmux new-session -d -s architect "exec ~/bin/architect_watcher.sh 2>&1 | tee -a ~/architect/watcher.log"
echo "architect started in tmux session 'architect'. Attach: tmux attach -t architect"
