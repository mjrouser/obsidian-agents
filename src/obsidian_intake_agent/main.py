from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .config import Config
from .graph_auth import GraphAuthProvider, GraphTokenResult
from .meetings import (
    ChainedMeetingArtifactDiscoveryClient,
    GraphMeetingFallbackSummaryClient,
    GraphOutlookMeetingDiscoveryClient,
    GraphTranscriptDiscoveryClient,
    GraphTranscriptDownloadClient,
    LocalIntakeTranscriptDiscoveryClient,
    MeetingArtifactDiscoveryClient,
    MeetingSyncGraphTimeoutError,
    UnconfiguredOutlookMeetingDiscoveryClient,
    attach_transcript_to_bundle,
    build_bundle_processing_plan,
    build_transcript_sync_plan,
    execute_bundle_processing_plan,
    render_bundle_execution_result,
    render_bundle_processing_plan,
    render_bundle_write_result,
    render_transcript_sync_plan,
    write_planned_bundle_notes,
)
from .processors.meeting_processor import MeetingProcessor
from .processors.web_clip_processor import WebClipProcessor
from .utils.git import auto_commit_repo
from .web_clips.bookmarklet import render_bookmarklet
from .web_clips.capture_server import run_capture_server
from .weekly import generate_weekly_snapshot

WEB_CLIPPER_TOKEN_ENV_VAR = "OBSIDIAN_WEB_CLIPPER_TOKEN"


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

    graph_parser = subparsers.add_parser("graph", help="Authenticate and inspect Microsoft Graph access.")
    graph_subparsers = graph_parser.add_subparsers(dest="graph_command", required=True)
    graph_subparsers.add_parser("login", help="Start Microsoft Graph device-code login and cache the token.")
    graph_subparsers.add_parser("status", help="Show Microsoft Graph auth configuration and cache status.")
    graph_subparsers.add_parser("logout", help="Remove the local Microsoft Graph token cache.")

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
        help="Write planned intake bundle notes and Outlook metadata sidecars into 00_Intake/bundles without downloading artifacts.",
    )
    sync_parser.add_argument(
        "--download-transcripts",
        action="store_true",
        dest="download_transcripts",
        help="Download available Teams transcript files into 00_Intake/bundles/raw_transcripts and write matching bundle notes in 00_Intake/bundles.",
    )
    process_bundles_parser = meetings_subparsers.add_parser(
        "process-bundles",
        help="Dry-run which synced meeting bundles in 00_Intake/bundles are ready for the existing intake processor.",
    )
    process_bundles_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print which bundle handoffs are ready without processing any files.",
    )
    process_bundles_parser.add_argument(
        "--execute",
        action="store_true",
        dest="execute",
        help="Process ready meeting bundle handoffs through the existing intake processor.",
    )
    process_bundles_parser.add_argument(
        "--validation",
        action="store_true",
        dest="validation",
        help="Write processed meeting notes and Matthew-owned actions into 99_Test Notes instead of production lanes.",
    )
    attach_parser = meetings_subparsers.add_parser(
        "attach-transcript",
        help="Attach a local transcript file to an existing synced meeting bundle.",
    )
    attach_parser.add_argument("--event-id", required=True, help="Outlook event ID from the meeting bundle metadata.")
    attach_parser.add_argument("--file", required=True, help="Local .vtt, .md, or .docx transcript file to attach.")

    web_clips_parser = subparsers.add_parser("web-clips", help="Capture and process web clips.")
    web_clips_subparsers = web_clips_parser.add_subparsers(dest="web_clips_command", required=True)
    bookmarklet_parser = web_clips_subparsers.add_parser(
        "bookmarklet",
        help="Print a bookmarklet for capturing web clips.",
    )
    bookmarklet_parser.add_argument(
        "--token",
        help=f"Shared token sent by the bookmarklet to the local capture server. Defaults to {WEB_CLIPPER_TOKEN_ENV_VAR}.",
    )
    serve_parser = web_clips_subparsers.add_parser(
        "serve",
        help="Run the local web clip capture server.",
    )
    serve_parser.add_argument(
        "--token",
        help=f"Shared token required for capture requests. Defaults to {WEB_CLIPPER_TOKEN_ENV_VAR}.",
    )
    process_web_clips_parser = web_clips_subparsers.add_parser(
        "process",
        help="Process raw web clip captures into reference notes.",
    )
    process_web_clips_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        dest="dry_run",
        help="Print intended web clip processing changes without writing files.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    _warn_if_not_using_repo_venv()
    parser = build_parser()
    args = parser.parse_args(argv)

    config = Config.load(Path(args.config))
    processor: MeetingProcessor | None = None

    def get_processor() -> MeetingProcessor:
        nonlocal processor
        if processor is None:
            processor = MeetingProcessor(
                config,
                meeting_discovery_client=_build_meeting_discovery_client(config),
            )
        return processor

    if args.command == "run":
        if not args.once:
            parser.error("`obsidian-agent run` currently requires `--once`.")
        summary = get_processor().process_all_unprocessed()
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

        watch_intake(get_processor())
        return 0

    if args.command == "process":
        result = get_processor().process_file(Path(args.path), force=args.force, dry_run=args.dry_run)
        if result.canonical_note_path is not None:
            print(f"canonical_output_file: {result.canonical_note_path}")
        for warning in result.processing_warnings:
            print(f"processing_warning: {warning}")
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

    if args.command == "graph":
        provider = GraphAuthProvider(config)
        if args.graph_command == "status":
            for line in provider.status().lines():
                print(line)
            return 0
        if args.graph_command == "login":
            login_result = provider.login(print)
            print(f"graph_login_succeeded: {'yes' if login_result.succeeded else 'no'}")
            if login_result.username:
                print(f"graph_login_account: {login_result.username}")
            print(f"graph_login_message: {login_result.message}")
            return 0 if login_result.succeeded else 1
        if args.graph_command == "logout":
            logout_result = provider.logout()
            print(f"graph_logout_removed: {'yes' if logout_result.removed else 'no'}")
            print(f"graph_logout_cache_path: {logout_result.cache_path}")
            print(f"graph_logout_message: {logout_result.message}")
            return 0

    if args.command == "meetings":
        if args.meetings_command == "sync-transcripts":
            selected_modes = sum(
                1 for selected in (args.dry_run, args.write_bundles, args.download_transcripts) if selected
            )
            if selected_modes != 1:
                parser.error(
                    "`obsidian-agent meetings sync-transcripts` requires exactly one of "
                    "`--dry-run`, `--write-bundles`, or `--download-transcripts`."
                )
            token_result = _resolve_graph_access_token_result(config)
            if _graph_auth_requires_manual_action(token_result):
                _print_graph_auth_required(token_result)
                return 2
            since = date.fromisoformat(args.since)
            try:
                sync_plan = build_transcript_sync_plan(
                    client=_build_meeting_discovery_client(config),
                    artifact_discovery_client=_build_meeting_artifact_discovery_client(
                        config,
                        download_transcripts=args.download_transcripts,
                    ),
                    since=since,
                    intake_root=config.vault_path / config.intake_dir,
                )
            except MeetingSyncGraphTimeoutError as exc:
                _print_meeting_sync_graph_timeout(exc)
                return 1
            mode = (
                "download-transcripts"
                if args.download_transcripts
                else ("write-bundles" if args.write_bundles else "dry-run")
            )
            print(render_transcript_sync_plan(sync_plan, mode=mode))
            if args.write_bundles or args.download_transcripts:
                print(render_bundle_write_result(write_planned_bundle_notes(sync_plan)))
            return 0
        if args.meetings_command == "process-bundles":
            if args.dry_run == args.execute:
                parser.error(
                    "`obsidian-agent meetings process-bundles` requires exactly one of `--dry-run` or `--execute`."
                )
            bundle_processor = MeetingProcessor(
                config,
                output_mode="validation" if args.validation else "normal",
                meeting_discovery_client=_build_meeting_discovery_client(config),
            )
            bundle_plan = build_bundle_processing_plan(
                intake_root=config.vault_path / config.intake_dir / "bundles",
                processor=bundle_processor,
            )
            if args.dry_run:
                print(render_bundle_processing_plan(bundle_plan))
                return 0
            execution_result = execute_bundle_processing_plan(bundle_plan, processor=bundle_processor)
            print(render_bundle_execution_result(execution_result))
            if execution_result.processed_count > 0:
                source_label = _vault_commit_source_label(
                    [
                        item.plan_item.metadata.processor_handoff.preferred_input_path.name
                        for item in execution_result.items
                        if item.status == "processed"
                        and item.plan_item.metadata.processor_handoff.preferred_input_path is not None
                    ]
                )
                _maybe_auto_commit(config, vault_source_name=source_label)
            return 0
        if args.meetings_command == "attach-transcript":
            attach_result = attach_transcript_to_bundle(
                bundle_root=config.vault_path / config.intake_dir / "bundles",
                event_id=args.event_id,
                file_path=Path(args.file),
            )
            print(f"meeting_bundle_transcript_attached: {attach_result.attached_path}")
            print(f"meeting_bundle_metadata_updated: {attach_result.metadata_path}")
            print(f"meeting_bundle_preferred_source: {attach_result.source_name}")
            return 0

    if args.command == "web-clips":
        if args.web_clips_command == "bookmarklet":
            token = _resolve_web_clipper_token(parser, args)
            print(
                render_bookmarklet(
                    host=config.web_clips_capture_host,
                    port=config.web_clips_capture_port,
                    token=token,
                )
            )
            return 0
        if args.web_clips_command == "serve":
            token = _resolve_web_clipper_token(parser, args)
            intake_dir = config.vault_path / config.web_clips_intake_dir

            def process_capture(raw_path: Path):
                return WebClipProcessor(config).process_file(raw_path, dry_run=False)

            def on_processed(result: object) -> None:
                if getattr(result, "written_reference_notes", 0) > 0 or getattr(result, "archived_raw_captures", 0) > 0:
                    _maybe_auto_commit(config, vault_source_name="web clips")

            print(f"web_clips_server_url: http://{config.web_clips_capture_host}:{config.web_clips_capture_port}")
            print(f"web_clips_intake_dir: {intake_dir}")
            run_capture_server(
                host=config.web_clips_capture_host,
                port=config.web_clips_capture_port,
                intake_dir=intake_dir,
                token=token,
                vault_path=config.vault_path,
                process_capture=process_capture,
                on_processed=on_processed,
            )
            return 0
        if args.web_clips_command == "process":
            effective_dry_run = config.dry_run if args.dry_run is None else args.dry_run
            web_clip_summary = WebClipProcessor(config).process_all(dry_run=args.dry_run)
            print(f"web_clips_processed_files: {web_clip_summary.processed_files}")
            print(f"web_clips_written_reference_notes: {web_clip_summary.written_reference_notes}")
            print(f"web_clips_archived_raw_captures: {web_clip_summary.archived_raw_captures}")
            if web_clip_summary.processed_files > 0 and not effective_dry_run:
                _maybe_auto_commit(config, vault_source_name="web clips")
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
        if status.state == "failed" and status.detail:
            print(f"git auto-commit vault detail: {status.detail}")
    else:
        print("git auto-commit vault: skipped (disabled)")

    if config.git_auto_commit_project:
        print("git auto-commit project: skipped (manual review required; commit project changes outside the agent)")
    else:
        print("git auto-commit project: skipped (disabled)")


def _resolve_web_clipper_token(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    token = args.token or os.environ.get(WEB_CLIPPER_TOKEN_ENV_VAR)
    if not token:
        parser.error(
            f"`obsidian-agent web-clips {args.web_clips_command}` requires --token or {WEB_CLIPPER_TOKEN_ENV_VAR}."
        )
    return token


def _format_git_status(repo_name: str, state: str, repo_path: Path) -> str:
    if state == "committed":
        return f"git auto-commit {repo_name}: committed ({repo_path})"
    if state == "no_changes":
        return f"git auto-commit {repo_name}: skipped (no changes)"
    if state == "not_git_repo":
        return f"git auto-commit {repo_name}: skipped (not a git repo: {repo_path})"
    if state == "failed":
        return f"git auto-commit {repo_name}: warning (commit failed in {repo_path})"
    return f"git auto-commit {repo_name}: skipped ({state})"


def _vault_commit_source_label(processed_sources: list[str]) -> str:
    if not processed_sources:
        return "batch intake processing"
    if len(processed_sources) == 1:
        return processed_sources[0]
    return "multiple intake files"


def _resolve_graph_access_token_result(config: Config) -> GraphTokenResult:
    return GraphAuthProvider(config).get_access_token()


def _resolve_graph_access_token(config: Config) -> str | None:
    return _resolve_graph_access_token_result(config).access_token


def _graph_auth_requires_manual_action(token_result: GraphTokenResult) -> bool:
    return token_result.source == "cache_miss" and not token_result.available


def _print_graph_auth_required(token_result: GraphTokenResult) -> None:
    print("graph_auth_required: yes", file=sys.stderr)
    print(
        f"graph_auth_message: {token_result.message}",
        file=sys.stderr,
    )
    if token_result.recovery_steps:
        print("graph_auth_recovery_steps:", file=sys.stderr)
        for step in token_result.recovery_steps:
            print(f"  {step}", file=sys.stderr)


def _print_meeting_sync_graph_timeout(exc: MeetingSyncGraphTimeoutError) -> None:
    print("meeting_sync_error: graph_timeout", file=sys.stderr)
    print(f"meeting_sync_error_detail: {exc}", file=sys.stderr)
    print(
        "meeting_sync_next_step: The next scheduled run will retry automatically. "
        "If this repeats, run `obsidian-agent graph status` and consider increasing the Graph timeout.",
        file=sys.stderr,
    )


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
    token = _resolve_graph_access_token(config)
    if not token:
        return UnconfiguredOutlookMeetingDiscoveryClient()
    return GraphOutlookMeetingDiscoveryClient(
        access_token=token,
        api_base_url=config.outlook_graph_api_base_url,
    )


def _build_meeting_artifact_discovery_client(
    config: Config,
    *,
    download_transcripts: bool = False,
) -> MeetingArtifactDiscoveryClient:
    local_client = LocalIntakeTranscriptDiscoveryClient(
        intake_root=config.vault_path / config.intake_dir,
    )
    token = _resolve_graph_access_token(config)
    if not token:
        return local_client
    if download_transcripts:
        return ChainedMeetingArtifactDiscoveryClient(
            GraphTranscriptDownloadClient(
                access_token=token,
                intake_root=config.vault_path / config.intake_dir,
                api_base_url=config.outlook_graph_api_base_url,
            ),
            GraphMeetingFallbackSummaryClient(
                access_token=token,
                intake_root=config.vault_path / config.intake_dir,
                api_base_url=config.outlook_graph_api_base_url,
            ),
            local_client,
        )
    return ChainedMeetingArtifactDiscoveryClient(
        GraphTranscriptDiscoveryClient(
            access_token=token,
            api_base_url=config.outlook_graph_api_base_url,
        ),
        GraphMeetingFallbackSummaryClient(
            access_token=token,
            intake_root=config.vault_path / config.intake_dir,
            api_base_url=config.outlook_graph_api_base_url,
        ),
        local_client,
    )


if __name__ == "__main__":
    raise SystemExit(main())
