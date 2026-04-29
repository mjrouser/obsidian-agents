#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from obsidian_intake_agent.automation_failures import write_automation_failure_note
from obsidian_intake_agent.processors.meeting_processor import Config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write an Obsidian note for an automation failure.")
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    parser.add_argument("--job", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--stderr-log", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.load(Path(args.config))
    note = write_automation_failure_note(
        config,
        job_name=args.job,
        exit_code=args.exit_code,
        stderr_log_path=Path(args.stderr_log),
    )
    print(f"automation_failure_note: {note.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
