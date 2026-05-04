#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from obsidian_intake_agent.automation_failures import (
    write_automation_failure_note,
    write_automation_failure_note_to_dir,
)
from obsidian_intake_agent.config import Config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write an Obsidian note for an automation failure.")
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    parser.add_argument("--job", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--stderr-log", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config)
    stderr_log_path = Path(args.stderr_log)
    try:
        config = Config.load(config_path)
        note = write_automation_failure_note(
            config,
            job_name=args.job,
            exit_code=args.exit_code,
            stderr_log_path=stderr_log_path,
        )
        print(f"automation_failure_note: {note.path}")
        print(f"automation_failure_latest_note: {note.latest_path}")
    except Exception as exc:
        fallback_dir = stderr_log_path.resolve().parent / "automation-failures"
        note = write_automation_failure_note_to_dir(
            fallback_dir,
            job_name=args.job,
            exit_code=args.exit_code,
            stderr_log_path=stderr_log_path,
            extra_details=[
                f"Config path could not be loaded: `{config_path}`",
                f"Config load error: `{exc}`",
                "This fallback note was written outside the vault because automation config was unavailable.",
            ],
        )
        print(
            f"warning: wrote fallback automation failure note because config could not be loaded: {config_path}",
            file=sys.stderr,
        )
        print(f"automation_failure_note_fallback: {note.path}")
        print(f"automation_failure_latest_note_fallback: {note.latest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
