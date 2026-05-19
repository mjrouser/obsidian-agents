#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STDOUT_LOG="logs/web-clipper.stdout.log"
STDERR_LOG="logs/web-clipper.stderr.log"
mkdir -p logs

ENV_FILE="${OBSIDIAN_WEB_CLIPPER_ENV_FILE:-$HOME/.config/obsidian-agents/web-clipper.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [ -z "${OBSIDIAN_WEB_CLIPPER_TOKEN:-}" ]; then
  printf '%s\n' "OBSIDIAN_WEB_CLIPPER_TOKEN is required. Create $ENV_FILE or set OBSIDIAN_WEB_CLIPPER_TOKEN before loading the LaunchAgent." \
    | tee -a "$STDERR_LOG" >&2
  exit 2
fi

PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
export PYTHON_BIN
exec bash "$REPO_ROOT/scripts/run_automation_command.sh" \
  "web-clipper" \
  "$STDOUT_LOG" \
  "$STDERR_LOG" \
  "$REPO_ROOT/.venv/bin/obsidian-agent" web-clips serve
