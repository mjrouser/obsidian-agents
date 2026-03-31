#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
exec "$PYTHON_BIN" scripts/generate_weekly_summary.py wrap >> logs/weekly-wrap.stdout.log 2>> logs/weekly-wrap.stderr.log
