from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .config import Config
from .meetings import (
    GraphOutlookMeetingDiscoveryClient,
    LocalIntakeTranscriptDiscoveryClient,
    UnconfiguredOutlookMeetingDiscoveryClient,
    build_bundle_processing_plan,
    build_transcript_sync_plan,
    render_bundle_processing_plan,
    render_bundle_write_result,
    render_transcript_sync_plan,
    write_planned_bundle_notes,
)
from .processors.meeting_processor import MeetingProcessor
from .utils.git import auto_commit_repo
from .weekly import generate_weekly_snapshot


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

    weekly_parser = subparsers.add_parser("weekly", help="Generate or update a weekly review note.")
    weekly_parser.add_argument(
        "mode",
        choices=["briefing", "wrap"],
        help="Which weekly review section to generate.",
    )
    weekly_parser.add_argument(
        "--date",
        dest="target_date",
        default=None,
        help="Target run date in YYYY-MM-DD format. Defaults to today.",
    )
    weekly_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Render the weekly note update without writing the weekly review file.",
    )

    meetings_parser = subparsers.add_parser("meetings", help="Discover and plan meeting artifact sync.")
    meetings_subparsers = meetings_parser.add_subparsers(dest="meetings_command", required=True)

    sync_parser = meetings_subparsers.add_parser(
        "sync-transcripts",
        help="Dry-run transcript sync planning for recently ended meetings.",
    )
    sync_parser.add_argument(
        "--since",
        required=True,
        help="Include meetings ending on or after this YYYY-MM-DD date.",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print the transcript sync plan without downloading or writing artifacts.",
    )
    sync_parser.add_argument(
        "--write-bundles",
        action="store_true",
        dest="write_bundles",
        help="Write planned intake bundle notes into the intake folder without downloading artifacts.",
    )
    process_bundles_parser = meetings_subparsers.add_parser(
        "process-bundles",
        help="Dry-run which synced meeting bundles are ready for the existing intake processor.",
    )
    process_bundles_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print which bundle handoffs are ready without processing any files.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    _warn_if_not_using_repo_venv()
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
        if not config.dry_run and summary.processed_files > 0:
            source_label = _vault_commit_source_label(summary.processed_sources or [])
            _maybe_auto_commit(config, vault_source_name=source_label)
        return 0

    if args.command == "watch":
        from .watcher import watch_intake

        watch_intake(processor)
        return 0

    if args.command == "process":
        result = processor.process_file(Path(args.path), force=args.force, dry_run=args.dry_run)
        if result.canonical_note_path is not None:
            print(f"canonical_output_file: {result.canonical_note_path}")
        if result.processed and not args.dry_run:
            _maybe_auto_commit(config, vault_source_name=Path(args.path).name)
        return 0

    if args.command == "weekly":
        target_date = None
        if args.target_date:
            target_date = __import__("datetime").date.fromisoformat(args.target_date)
        weekly_result = generate_weekly_snapshot(
            config,
            mode=args.mode,
            target_date=target_date,
            dry_run=args.dry_run,
        )
        action = "would_update" if args.dry_run else ("updated" if weekly_result.changed else "unchanged")
        print(f"weekly_review_{action}: {weekly_result.review_path}")
        if weekly_result.changed and not args.dry_run:
            _maybe_auto_commit(config, vault_source_name=weekly_result.review_path.name)
        return 0

    if args.command == "meetings":
        if args.meetings_command == "sync-transcripts":
            if args.dry_run == args.write_bundles:
                parser.error(
                    "`obsidian-agent meetings sync-transcripts` requires exactly one of `--dry-run` or `--write-bundles`."
                )
            since = date.fromisoformat(args.since)
            sync_plan = build_transcript_sync_plan(
                client=_build_meeting_discovery_client(config),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(
                    intake_root=config.vault_path / config.intake_dir,
                ),
                since=since,
                intake_root=config.vault_path / config.intake_dir,
            )
            mode = "write-bundles" if args.write_bundles else "dry-run"
            print(render_transcript_sync_plan(sync_plan, mode=mode))
            if args.write_bundles:
                print(render_bundle_write_result(write_planned_bundle_notes(sync_plan)))
            return 0
        if args.meetings_command == "process-bundles":
            if not args.dry_run:
                parser.error("`obsidian-agent meetings process-bundles` currently requires `--dry-run`.")
            bundle_plan = build_bundle_processing_plan(
                intake_root=config.vault_path / config.intake_dir,
                processor=processor,
            )
            print(render_bundle_processing_plan(bundle_plan))
            return 0

    parser.error("Unknown command.")
    return 1


def _maybe_auto_commit(config: Config, *, vault_source_name: str) -> None:
    if config.git_auto_commit_vault:
        status = auto_commit_repo(
            config.git_vault_repo_path or config.vault_path,
            f"auto: process transcript outputs for {vault_source_name}",
        )
        print(_format_git_status("vault", status.state, status.repo_path))
    else:
        print("git auto-commit vault: skipped (disabled)")

    if config.git_auto_commit_project:
        print("git auto-commit project: skipped (manual review required; commit project changes outside the agent)")
    else:
        print("git auto-commit project: skipped (disabled)")


def _format_git_status(repo_name: str, state: str, repo_path: Path) -> str:
    if state == "committed":
        return f"git auto-commit {repo_name}: committed ({repo_path})"
    if state == "no_changes":
        return f"git auto-commit {repo_name}: skipped (no changes)"
    if state == "not_git_repo":
        return f"git auto-commit {repo_name}: skipped (not a git repo: {repo_path})"
    return f"git auto-commit {repo_name}: skipped ({state})"


def _vault_commit_source_label(processed_sources: list[str]) -> str:
    if not processed_sources:
        return "batch intake processing"
    if len(processed_sources) == 1:
        return processed_sources[0]
    return "multiple intake files"


def _warn_if_not_using_repo_venv() -> None:
    executable = Path(sys.executable)
    repo_root = Path(__file__).resolve().parents[2]
    expected = repo_root / ".venv" / "bin" / "python"
    prefix = Path(sys.prefix)
    try:
        prefix.relative_to((repo_root / ".venv").resolve())
        return
    except ValueError:
        pass
    print(
        f"WARNING: expected repo virtualenv interpreter at {expected}, but running with {executable}",
        file=sys.stderr,
    )


def _build_meeting_discovery_client(
    config: Config,
) -> GraphOutlookMeetingDiscoveryClient | UnconfiguredOutlookMeetingDiscoveryClient:
    token = os.environ.get(config.outlook_graph_access_token_env, "").strip()
    if not token:
        return UnconfiguredOutlookMeetingDiscoveryClient()
    return GraphOutlookMeetingDiscoveryClient(
        access_token=token,
        api_base_url=config.outlook_graph_api_base_url,
    )


if __name__ == "__main__":
    raise SystemExit(main())
