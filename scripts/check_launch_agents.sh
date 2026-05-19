#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

labels=(
  "com.obsidian.agent.intake-watcher"
  "com.obsidian.agent.weekly-briefing"
  "com.obsidian.agent.weekly-wrap"
  "com.obsidian.agent.web-clipper"
)

for label in "${labels[@]}"; do
  if launchctl list | grep -q "$label"; then
    echo "loaded: $label"
  else
    echo "missing: $label"
  fi
done

echo
for log_name in \
  "intake-watcher.stdout.log" \
  "intake-watcher.stderr.log" \
  "weekly-briefing.stdout.log" \
  "weekly-briefing.stderr.log" \
  "weekly-wrap.stdout.log" \
  "weekly-wrap.stderr.log" \
  "web-clipper.stdout.log" \
  "web-clipper.stderr.log"
do
  if [ -f "logs/$log_name" ]; then
    echo "log present: logs/$log_name"
  else
    echo "log missing: logs/$log_name"
  fi
done
