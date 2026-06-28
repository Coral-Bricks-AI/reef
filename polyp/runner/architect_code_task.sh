#!/usr/bin/env bash
# Compat shim: the real implementation moved to polyp/runner/architect_code_task.py.
# Kept so systemd units / watcher scripts / direct invocations of the old path
# keep working through the migration.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
exec "${POLYP_PYTHON:-python3}" -m polyp.runner.architect_code_task "$@"
