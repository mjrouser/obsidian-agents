from __future__ import annotations

import argparse
from pathlib import Path

from .processors.meeting_processor import Config, MeetingProcessor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obsidian-agent")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Process intake files.")
    run_parser.add_argument(
        "--once",
        action="store_true",
        help="Process current unprocessed intake files and exit.",
    )

    subparsers.add_parser("watch", help="Watch the intake directory for new files.")

    process_parser = subparsers.add_parser("process", help="Process a single file.")
    process_parser.add_argument("path", help="Path to intake file.")
    process_parser.add_argument(
        "--force",
        "--reprocess",
        action="store_true",
        dest="force",
        help="Reprocess an already-processed intake file instead of skipping it.",
    )
    process_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Simulate processing without writing meeting notes, action files, or intake markers.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = Config.load(Path(args.config))
    processor = MeetingProcessor(config)

    if args.command == "run":
        if not args.once:
            parser.error("`obsidian-agent run` currently requires `--once`.")
        summary = processor.process_all_unprocessed()
        print(f"processed_files: {summary.processed_files}")
        print(
            "skipped_files: "
            f"{summary.skipped_files} "
            f"(ignored basename: {summary.skipped_ignored_basename}, "
            f"already processed: {summary.skipped_already_processed}, "
            f"unsupported ext: {summary.skipped_unsupported_ext})"
        )
        print(f"written_meeting_notes: {summary.meeting_notes_written}")
        print(f"updated_actions_files: {summary.weekly_action_files_updated}")
        return 0

    if args.command == "watch":
        from .watcher import watch_intake

        watch_intake(processor)
        return 0

    if args.command == "process":
        result = processor.process_file(Path(args.path), force=args.force, dry_run=args.dry_run)
        if result.canonical_note_path is not None:
            print(f"canonical_output_file: {result.canonical_note_path}")
        return 0

    parser.error("Unknown command.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
