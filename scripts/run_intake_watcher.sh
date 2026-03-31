#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
exec "$PYTHON_BIN" scripts/watch_intake.py >> logs/intake-watcher.stdout.log 2>> logs/intake-watcher.stderr.log
