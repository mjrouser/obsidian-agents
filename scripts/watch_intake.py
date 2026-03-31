#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from obsidian_intake_agent.main import main


if __name__ == "__main__":
    raise SystemExit(main(["--config", str(REPO_ROOT / "config.yaml"), "watch"]))
