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

notify_failure() {
  if [ "${AUTOMATION_FAILURE_NOTIFICATIONS:-1}" = "0" ]; then
    return 0
  fi
  if ! command -v osascript >/dev/null 2>&1; then
    return 0
  fi

  osascript \
    -e 'on run argv' \
    -e 'display notification ("Exit code " & item 2 of argv & ". Check the latest failure note.") with title "Obsidian Agent failed" subtitle (item 1 of argv)' \
    -e 'end run' \
    "$JOB_NAME" "$STATUS" \
    >> "$STDOUT_LOG" 2>> "$STDERR_LOG" || true
}

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
  notify_failure
fi

exit "$STATUS"
