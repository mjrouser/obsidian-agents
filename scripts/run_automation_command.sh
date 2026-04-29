#!/bin/bash
set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "usage: run_automation_command.sh <job-name> <stdout-log> <stderr-log> <command> [args...]" >&2
  exit 2
fi

JOB_NAME="$1"
STDOUT_LOG="$2"
STDERR_LOG="$3"
shift 3

mkdir -p "$(dirname "$STDOUT_LOG")" "$(dirname "$STDERR_LOG")"

set +e
STATUS=0
"$@" >> "$STDOUT_LOG" 2>> "$STDERR_LOG" &
CHILD_PID=$!

terminate_child() {
  kill "$CHILD_PID" 2>/dev/null || true
}

trap terminate_child TERM INT
wait "$CHILD_PID"
STATUS=$?
trap - TERM INT
set -e

if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 143 ]; then
  PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
  "$PYTHON_BIN" scripts/write_automation_failure_note.py \
    --job "$JOB_NAME" \
    --exit-code "$STATUS" \
    --stderr-log "$STDERR_LOG" \
    >> "$STDOUT_LOG" 2>> "$STDERR_LOG" || true
fi

exit "$STATUS"
