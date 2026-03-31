#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from obsidian_intake_agent.main import main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate or update the weekly Obsidian snapshot note.")
    parser.add_argument("mode", choices=["briefing", "wrap"])
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    parser.add_argument("--date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cli_args = ["--config", args.config, "weekly", args.mode]
    if args.date:
        cli_args.extend(["--date", args.date])
    if args.dry_run:
        cli_args.append("--dry-run")
    return main(cli_args)


if __name__ == "__main__":
    raise SystemExit(run())
