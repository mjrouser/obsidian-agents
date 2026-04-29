#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
export PYTHON_BIN
exec bash "$REPO_ROOT/scripts/run_automation_command.sh" \
  "weekly-wrap" \
  "logs/weekly-wrap.stdout.log" \
  "logs/weekly-wrap.stderr.log" \
  "$PYTHON_BIN" scripts/generate_weekly_summary.py wrap
