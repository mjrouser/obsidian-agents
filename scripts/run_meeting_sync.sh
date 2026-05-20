#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs logs/locks
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
AGENT_BIN="$REPO_ROOT/.venv/bin/obsidian-agent"
export PYTHON_BIN
CHILD_PID=""

LOCK_FILE="$REPO_ROOT/logs/locks/meeting-sync.lockfile"
terminate_child() {
  local signal="$1"
  if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
    terminate_tree "$CHILD_PID" "$signal"
    wait "$CHILD_PID" 2>/dev/null || true
  fi
}
terminate_tree() {
  local pid="$1"
  local signal="$2"
  local child_pid
  for child_pid in $(pgrep -P "$pid" 2>/dev/null || true); do
    terminate_tree "$child_pid" "$signal"
  done
  kill "-$signal" "$pid" 2>/dev/null || true
}
handle_term() {
  terminate_child TERM
  exit 143
}
handle_int() {
  terminate_child INT
  exit 130
}
trap handle_term TERM
trap handle_int INT

SINCE="$("$PYTHON_BIN" -c 'from datetime import date, timedelta; print((date.today() - timedelta(days=7)).isoformat())')"

lockf -t 0 "$LOCK_FILE" \
  bash "$REPO_ROOT/scripts/run_automation_command.sh" \
  "meeting-sync" \
  "logs/meeting-sync.stdout.log" \
  "logs/meeting-sync.stderr.log" \
  bash -c '"$1" meetings sync-transcripts --since "$2" --download-transcripts && "$1" meetings process-bundles --execute' \
  _ "$AGENT_BIN" "$SINCE" \
  2>/dev/null &
CHILD_PID=$!

set +e
wait "$CHILD_PID"
STATUS=$?
set -e
CHILD_PID=""
if [ "$STATUS" -eq 75 ]; then
  echo "meeting-sync: previous run still active; skipping this tick" | tee -a logs/meeting-sync.stdout.log
  exit 0
fi
exit "$STATUS"
