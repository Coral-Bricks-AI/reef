#!/usr/bin/env bash
# Compat shim: real impl in polyp/runner/suggest_experiment.py.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
exec "${POLYP_PYTHON:-python3}" -m polyp.runner.suggest_experiment "$@"
