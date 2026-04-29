#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"

if command -v git >/dev/null 2>&1; then
  echo "[OK] git: $(git --version)"
else
  echo "[FAIL] git is missing"
  exit 1
fi

if command -v rg >/dev/null 2>&1; then
  echo "[OK] rg: $(rg --version | head -n 1)"
else
  echo "[WARN] rg missing"
fi

if [ -x "$PYTHON_BIN" ]; then
  echo "[OK] .venv python: $("$PYTHON_BIN" --version)"
else
  echo "[FAIL] expected virtualenv interpreter missing: $PYTHON_BIN"
  exit 1
fi

if [ -f "$REPO_ROOT/config.example.yaml" ]; then
  echo "[OK] example config present"
else
  echo "[FAIL] expected config.example.yaml"
  exit 1
fi

if git -C "$REPO_ROOT" ls-files --error-unmatch config.yaml >/dev/null 2>&1; then
  echo "[FAIL] config.yaml must remain local and untracked"
  exit 1
else
  echo "[OK] config.yaml is untracked"
fi

echo "[OK] check script completed"
