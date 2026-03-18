#!/usr/bin/env bash
set -euo pipefail

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

if command -v python3 >/dev/null 2>&1; then
  echo "[OK] python3: $(python3 --version)"
else
  echo "[FAIL] python3 is missing"
  exit 1
fi

echo "[OK] check script completed"
