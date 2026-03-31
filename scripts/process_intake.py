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
    parser = argparse.ArgumentParser(description="Thin wrapper around the existing obsidian-agent intake processor.")
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    parser.add_argument("--path", default=None, help="Specific intake file to process.")
    parser.add_argument("--force", action="store_true", help="Reprocess an already-processed intake file.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate processing without writing.")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cli_args = ["--config", args.config]
    if args.path:
        cli_args.extend(["process", args.path])
        if args.force:
            cli_args.insert(len(cli_args), "--force")
        if args.dry_run:
            cli_args.insert(len(cli_args), "--dry-run")
    else:
        cli_args.extend(["run", "--once"])
    return main(cli_args)


if __name__ == "__main__":
    raise SystemExit(run())
