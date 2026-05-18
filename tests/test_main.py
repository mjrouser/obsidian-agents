from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from obsidian_intake_agent.config import Config
from obsidian_intake_agent.main import _build_meeting_artifact_discovery_client, _build_meeting_discovery_client, main
from obsidian_intake_agent.meetings import (
    AttachTranscriptResult,
    BundleExecutionResult,
    BundleExecutionResultItem,
    BundleMetadataRecord,
    BundleProcessingPlanItem,
    BundleProcessorHandoff,
    BundleWriteResult,
    ChainedMeetingArtifactDiscoveryClient,
    GraphOutlookMeetingDiscoveryClient,
    LocalIntakeTranscriptDiscoveryClient,
    TranscriptSyncPlan,
    UnconfiguredOutlookMeetingDiscoveryClient,
)
from obsidian_intake_agent.processors.meeting_processor import ProcessResult
from obsidian_intake_agent.utils.git import GitCommitStatus


class MainCliTests(unittest.TestCase):
    def test_run_once_prints_summary_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            (intake_dir / "weekly-sync.md").write_text(
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )
            config_path = repo / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        f'vault_path: "{vault}"',
                        'intake_dir: "00_Intake"',
                        'meetings_dir: "01_Meetings"',
                        'actions_dir: "07_Actions"',
                        'archive_intake_dir: "_Archive/Intake"',
                        'templates_dir: "Templates"',
                        'owner_filter: "Matthew"',
                        "dry_run: true",
                        "include_unassigned: false",
                        'llm_provider: "none"',
                        "codex_model: null",
                        'codex_exec_cmd: ["codex", "exec"]',
                        'extraction_mode: "draft"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(["--config", str(config_path), "run", "--once"])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("processed_files: 1", output)
            self.assertIn("written_meeting_notes: 1", output)

    def test_process_force_reprocesses_and_prints_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "weekly-sync.md"
            intake_file.write_text(
                "STATUS: PROCESSED — see [[01_Meetings/old-note.md]]\n"
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )
            config_path = repo / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        f'vault_path: "{vault}"',
                        'intake_dir: "00_Intake"',
                        'meetings_dir: "01_Meetings"',
                        'actions_dir: "07_Actions"',
                        'archive_intake_dir: "_Archive/Intake"',
                        'templates_dir: "Templates"',
                        'owner_filter: "Matthew"',
                        "dry_run: false",
                        "include_unassigned: false",
                        'llm_provider: "none"',
                        "codex_model: null",
                        'codex_exec_cmd: ["codex", "exec"]',
                        'extraction_mode: "draft"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(["--config", str(config_path), "process", "--force", str(intake_file)])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("Reprocessing (forced)", output)
            self.assertIn("canonical_output_file:", output)

    def test_process_dry_run_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "weekly-sync.md"
            intake_file.write_text(
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )
            config_path = repo / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        f'vault_path: "{vault}"',
                        'intake_dir: "00_Intake"',
                        'meetings_dir: "01_Meetings"',
                        'actions_dir: "07_Actions"',
                        'archive_intake_dir: "_Archive/Intake"',
                        'templates_dir: "Templates"',
                        'owner_filter: "Matthew"',
                        "dry_run: false",
                        "include_unassigned: false",
                        'llm_provider: "none"',
                        "codex_model: null",
                        'codex_exec_cmd: ["codex", "exec"]',
                        'extraction_mode: "draft"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(["--config", str(config_path), "process", "--dry-run", str(intake_file)])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("DRY RUN — would write:", output)
            self.assertIn("DRY RUN — would update:", output)
            self.assertIn("DRY RUN — would prepend processed marker", output)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())
            self.assertNotIn("STATUS: PROCESSED", intake_file.read_text(encoding="utf-8"))

    def test_process_dry_run_skips_both_auto_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "weekly-sync.md"
            intake_file.write_text("Action: Matthew will complete Codex setup by Friday.\n", encoding="utf-8")
            config_path = _write_config(repo, vault, git_auto_commit_vault=True, git_auto_commit_project=True)

            with patch("obsidian_intake_agent.main.auto_commit_repo") as commit_mock:
                exit_code = main(["--config", str(config_path), "process", "--dry-run", str(intake_file)])

            self.assertEqual(exit_code, 0)
            commit_mock.assert_not_called()

    def test_process_skips_auto_commits_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "weekly-sync.md"
            intake_file.write_text("Action: Matthew will complete Codex setup by Friday.\n", encoding="utf-8")
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.auto_commit_repo") as commit_mock,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(["--config", str(config_path), "process", str(intake_file)])

            self.assertEqual(exit_code, 0)
            commit_mock.assert_not_called()
            self.assertIn("git auto-commit vault: skipped (disabled)", stdout.getvalue())
            self.assertIn("git auto-commit project: skipped (disabled)", stdout.getvalue())

    def test_project_auto_commit_requires_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "weekly-sync.md"
            intake_file.write_text("Action: Matthew will complete Codex setup by Friday.\n", encoding="utf-8")
            config_path = _write_config(repo, vault, git_auto_commit_vault=True, git_auto_commit_project=True)

            with (
                patch(
                    "obsidian_intake_agent.main.auto_commit_repo",
                    return_value=GitCommitStatus(state="committed", repo_path=vault),
                ) as commit_mock,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(["--config", str(config_path), "process", str(intake_file)])

            self.assertEqual(exit_code, 0)
            commit_mock.assert_called_once()
            output = stdout.getvalue()
            self.assertIn("git auto-commit vault: committed", output)
            self.assertIn("git auto-commit project: skipped (manual review required", output)

    def test_skips_gracefully_when_repo_path_is_not_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "weekly-sync.md"
            intake_file.write_text("Action: Matthew will complete Codex setup by Friday.\n", encoding="utf-8")
            config_path = _write_config(repo, vault, git_auto_commit_vault=True)

            with (
                patch(
                    "obsidian_intake_agent.main.auto_commit_repo",
                    return_value=GitCommitStatus(state="not_git_repo", repo_path=vault),
                ) as commit_mock,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(["--config", str(config_path), "process", str(intake_file)])

            self.assertEqual(exit_code, 0)
            commit_mock.assert_called_once()
            self.assertIn("git auto-commit vault: skipped (not a git repo:", stdout.getvalue())

    def test_processing_failure_does_not_run_auto_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "weekly-sync.md"
            intake_file.write_text("Action: Matthew will complete Codex setup by Friday.\n", encoding="utf-8")
            config_path = _write_config(repo, vault, git_auto_commit_vault=True, git_auto_commit_project=True)

            with (
                patch("obsidian_intake_agent.main.auto_commit_repo") as commit_mock,
                patch("obsidian_intake_agent.main.MeetingProcessor.process_file", side_effect=RuntimeError("boom")),
                self.assertRaises(RuntimeError),
            ):
                main(["--config", str(config_path), "process", str(intake_file)])

            commit_mock.assert_not_called()

    def test_process_prints_processor_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "2026-05-13 - Teams - Long Meeting.vtt"
            intake_file.write_text("WEBVTT\n", encoding="utf-8")
            config_path = _write_config(repo, vault)

            with (
                patch(
                    "obsidian_intake_agent.main.MeetingProcessor.process_file",
                    return_value=ProcessResult(
                        processed=True,
                        canonical_note_path=vault / "01_Meetings" / "2026-05-13 - Teams - Long Meeting.md",
                        processing_warnings=["vtt_extraction_fallback reason=Codex CLI timed out"],
                    ),
                ),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(["--config", str(config_path), "process", str(intake_file)])

            self.assertEqual(exit_code, 0)
            self.assertIn("processing_warning: vtt_extraction_fallback reason=Codex CLI timed out", stdout.getvalue())

    def test_web_clips_bookmarklet_prints_javascript_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "web-clips",
                        "bookmarklet",
                        "--token",
                        "test-token",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.startswith("javascript:"))
            self.assertIn("http://127.0.0.1:8765/capture", output)
            self.assertIn("X-Obsidian-Web-Clipper-Token", output)
            self.assertIn("test-token", output)

    def test_web_clips_bookmarklet_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch.dict(os.environ, {}, clear=True),
                self.assertRaises(SystemExit) as exc,
            ):
                main(["--config", str(config_path), "web-clips", "bookmarklet"])

            self.assertEqual(exc.exception.code, 2)

    def test_web_clips_bookmarklet_uses_env_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch.dict(os.environ, {"OBSIDIAN_WEB_CLIPPER_TOKEN": "env-token"}, clear=False),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(["--config", str(config_path), "web-clips", "bookmarklet"])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.startswith("javascript:"))
            self.assertIn("X-Obsidian-Web-Clipper-Token", output)
            self.assertIn("env-token", output)

    def test_web_clips_process_prints_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"
            intake_dir.mkdir(parents=True)
            (intake_dir / "2026-05-18 - Example Article.md").write_text(
                "\n".join(
                    [
                        "---",
                        "type: web_clip_intake",
                        "status: unprocessed",
                        'captured_at: "2026-05-18T10:00:00+00:00"',
                        'source_url: "https://example.com/article"',
                        'source_title: "Example Article"',
                        "---",
                        "",
                        "# Example Article",
                        "",
                        "Source: https://example.com/article",
                        "",
                        "## Why This Matters",
                        "",
                        "Useful context for the web clip CLI.",
                        "",
                        "## Captured Passages",
                        "",
                        "> A concise captured passage.",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config_path = _write_config(repo, vault)

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(["--config", str(config_path), "web-clips", "process", "--dry-run"])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("DRY RUN: would write web clip reference note:", output)
            self.assertIn("DRY RUN: would archive web clip intake note:", output)
            self.assertIn("web_clips_processed_files: 1", output)
            self.assertIn("web_clips_written_reference_notes: 0", output)
            self.assertIn("web_clips_archived_raw_captures: 0", output)

    def test_web_clips_process_without_flag_preserves_config_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"
            intake_dir.mkdir(parents=True)
            raw_clip = intake_dir / "2026-05-18 - Example Article.md"
            raw_clip.write_text(
                "\n".join(
                    [
                        "---",
                        "type: web_clip_intake",
                        "status: unprocessed",
                        'captured_at: "2026-05-18T10:00:00+00:00"',
                        'source_url: "https://example.com/article"',
                        'source_title: "Example Article"',
                        "---",
                        "",
                        "# Example Article",
                        "",
                        "Source: https://example.com/article",
                        "",
                        "## Why This Matters",
                        "",
                        "Useful context for the web clip CLI.",
                        "",
                        "## Captured Passages",
                        "",
                        "> A concise captured passage.",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config_path = _write_config(repo, vault, dry_run=True, git_auto_commit_vault=True)

            with (
                patch("obsidian_intake_agent.main.auto_commit_repo") as commit_mock,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(["--config", str(config_path), "web-clips", "process"])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("DRY RUN: would write web clip reference note:", output)
            self.assertIn("DRY RUN: would archive web clip intake note:", output)
            self.assertIn("web_clips_processed_files: 1", output)
            self.assertIn("web_clips_written_reference_notes: 0", output)
            self.assertIn("web_clips_archived_raw_captures: 0", output)
            self.assertTrue(raw_clip.exists())
            self.assertFalse((vault / "10_References").exists())
            commit_mock.assert_not_called()

    def test_meetings_sync_transcripts_requires_exactly_one_mode_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with self.assertRaises(SystemExit) as exc:
                main(["--config", str(config_path), "meetings", "sync-transcripts", "--since", "2026-05-01"])

            self.assertEqual(exc.exception.code, 2)

            with self.assertRaises(SystemExit) as both_exc:
                main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "sync-transcripts",
                        "--since",
                        "2026-05-01",
                        "--dry-run",
                        "--write-bundles",
                    ]
                )

            self.assertEqual(both_exc.exception.code, 2)

            with self.assertRaises(SystemExit) as three_modes_exc:
                main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "sync-transcripts",
                        "--since",
                        "2026-05-01",
                        "--dry-run",
                        "--write-bundles",
                        "--download-transcripts",
                    ]
                )

            self.assertEqual(three_modes_exc.exception.code, 2)

    def test_meetings_process_bundles_requires_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with self.assertRaises(SystemExit) as exc:
                main(["--config", str(config_path), "meetings", "process-bundles"])

            self.assertEqual(exc.exception.code, 2)

            with self.assertRaises(SystemExit) as both_exc:
                main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "process-bundles",
                        "--dry-run",
                        "--execute",
                    ]
                )

            self.assertEqual(both_exc.exception.code, 2)

    def test_meetings_sync_transcripts_prints_dry_run_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "sync-transcripts",
                        "--since",
                        "2026-05-01",
                        "--dry-run",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("meeting_sync_mode: dry-run", output)
            self.assertIn("meeting_sync_since: 2026-05-01", output)
            self.assertIn("meeting_sync_provider: outlook_calendar", output)
            self.assertIn("meeting_sync_warning: Outlook calendar discovery is not configured yet;", output)

    def test_meetings_sync_transcripts_write_bundles_prints_write_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            fake_plan = TranscriptSyncPlan(
                since=date(2026, 5, 1),
                generated_at=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
                provider_label="stub_outlook_calendar",
                warning=None,
                items=(),
            )

            with (
                patch("obsidian_intake_agent.main.build_transcript_sync_plan", return_value=fake_plan),
                patch(
                    "obsidian_intake_agent.main.write_planned_bundle_notes",
                    return_value=BundleWriteResult(
                        written_bundle_note_paths=(
                            vault / "00_Intake" / "2026-05-04 - Teams - Platform Sync (bundle).md",
                        ),
                        written_metadata_paths=(
                            vault / "00_Intake" / "2026-05-04 - Teams - Platform Sync (outlook).json",
                        ),
                        written_identity_paths=(
                            vault / "00_Intake" / "_meeting_sync" / "identities" / "2026-05-04-f922f45f924d0fb4.json",
                        ),
                        skipped_existing_bundle_note_paths=(),
                        skipped_existing_metadata_paths=(),
                        skipped_existing_identity_paths=(),
                    ),
                ),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "sync-transcripts",
                        "--since",
                        "2026-05-01",
                        "--write-bundles",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("meeting_sync_bundle_notes_written: 1", output)
            self.assertIn("meeting_sync_outlook_metadata_written: 1", output)
            self.assertIn("meeting_sync_identity_markers_written: 1", output)
            self.assertIn("bundle_note_written:", output)
            self.assertIn("outlook_metadata_written:", output)
            self.assertIn("meeting_identity_written:", output)

    def test_meetings_sync_transcripts_download_transcripts_prints_write_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            fake_plan = TranscriptSyncPlan(
                since=date(2026, 5, 1),
                generated_at=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
                provider_label="stub_outlook_calendar",
                warning=None,
                items=(),
            )

            with (
                patch("obsidian_intake_agent.main.build_transcript_sync_plan", return_value=fake_plan),
                patch(
                    "obsidian_intake_agent.main.write_planned_bundle_notes",
                    return_value=BundleWriteResult(
                        written_bundle_note_paths=(),
                        written_metadata_paths=(),
                        written_identity_paths=(),
                        skipped_existing_bundle_note_paths=(),
                        skipped_existing_metadata_paths=(),
                        skipped_existing_identity_paths=(),
                    ),
                ) as write_mock,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "sync-transcripts",
                        "--since",
                        "2026-05-01",
                        "--download-transcripts",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            write_mock.assert_called_once_with(fake_plan)
            self.assertIn("meeting_sync_mode: download-transcripts", output)
            self.assertIn("meeting_sync_bundle_notes_written: 0", output)

    def test_meetings_process_bundles_prints_dry_run_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            bundle_root.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            (bundle_root / "2026-05-04 - Teams - Platform Sync (outlook).json").write_text(
                "\n".join(
                    [
                        "{",
                        '  "outlook_event_id": "evt-1",',
                        '  "subject": "Platform Sync",',
                        '  "processor_handoff": {',
                        '    "preferred_input_path": null,',
                        '    "preferred_input_source_name": null',
                        "  },",
                        '  "artifacts": [',
                        '    {"source_name": "Outlook calendar metadata", "status": "available", "detail": "metadata", "matched_paths": []}',
                        "  ]",
                        "}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "process-bundles",
                        "--dry-run",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("meeting_bundle_process_mode: dry-run", output)
            self.assertIn(f"meeting_bundle_process_bundle_root: {bundle_root}", output)
            self.assertIn("meeting_bundle_process_candidates: 1", output)
            self.assertIn("meeting_bundle_process_blocked: 1", output)
            self.assertIn(
                "reason: Bundle is still calendar-only, so there is no processor-ready transcript artifact yet.",
                output,
            )

    def test_meetings_process_bundles_uses_bundle_staging_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            (vault / "00_Intake").mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.build_bundle_processing_plan") as plan_mock,
                patch(
                    "obsidian_intake_agent.main.render_bundle_processing_plan",
                    return_value="meeting_bundle_process_mode: dry-run",
                ),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "process-bundles",
                        "--dry-run",
                    ]
                )

            self.assertEqual(exit_code, 0)
            plan_mock.assert_called_once()
            self.assertEqual(
                plan_mock.call_args.kwargs["intake_root"],
                vault / "00_Intake" / "bundles",
            )

    def test_meetings_process_bundles_execute_prints_execution_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            ready_input = intake_root / "2026-05-04 - Teams - Platform Sync.vtt"
            fake_result = BundleExecutionResult(
                executed_at=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
                bundle_root=intake_root,
                items=(
                    BundleExecutionResultItem(
                        status="processed",
                        plan_item=BundleProcessingPlanItem(
                            decision="ready",
                            metadata=BundleMetadataRecord(
                                metadata_path=intake_root / "2026-05-04 - Teams - Platform Sync (outlook).json",
                                bundle_note_path=intake_root / "2026-05-04 - Teams - Platform Sync (bundle).md",
                                processed_marker_path=None,
                                event_id="evt-1",
                                subject="Platform Sync",
                                start_at=datetime.fromisoformat("2026-05-04T13:00:00+00:00"),
                                teams_meeting_id=None,
                                identity_key=None,
                                source_type="outlook_calendar_metadata",
                                processor_handoff=BundleProcessorHandoff(
                                    preferred_input_path=ready_input,
                                    preferred_input_source_name="Teams .vtt transcript",
                                ),
                                artifacts=(),
                            ),
                            reasons=(
                                "Bundle has a processor-ready local artifact and can be handed to the existing intake processor.",
                            ),
                        ),
                        reasons=("Bundle preferred input was processed successfully.",),
                        canonical_note_path=vault / "01_Meetings" / "2026-05-04 - Teams - Platform Sync.md",
                    ),
                ),
            )

            with (
                patch("obsidian_intake_agent.main.build_bundle_processing_plan") as plan_mock,
                patch(
                    "obsidian_intake_agent.main.execute_bundle_processing_plan", return_value=fake_result
                ) as execute_mock,
                patch("obsidian_intake_agent.main.auto_commit_repo") as commit_mock,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "process-bundles",
                        "--execute",
                    ]
                )

            self.assertEqual(exit_code, 0)
            plan_mock.assert_called_once()
            execute_mock.assert_called_once()
            commit_mock.assert_not_called()
            output = stdout.getvalue()
            self.assertIn("meeting_bundle_process_mode: execute", output)
            self.assertIn("meeting_bundle_process_processed: 1", output)
            self.assertIn("canonical_output_file:", output)

    def test_meetings_process_bundles_execute_validation_mode_prints_test_lane_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            bundle_root.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            preferred_input = bundle_root / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
            preferred_input.parent.mkdir(parents=True)
            preferred_input.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nAction: Matthew will confirm follow-up\n",
                encoding="utf-8",
            )
            (bundle_root / "2026-05-04 - Teams - Platform Sync (outlook).json").write_text(
                "\n".join(
                    [
                        "{",
                        '  "outlook_event_id": "evt-validation",',
                        '  "subject": "Platform Sync",',
                        '  "processor_handoff": {',
                        f'    "preferred_input_path": "{preferred_input}",',
                        '    "preferred_input_source_name": "Teams .vtt transcript"',
                        "  },",
                        '  "artifacts": [',
                        '    {"source_name": "Teams .vtt transcript", "status": "available", "detail": "downloaded", "matched_paths": []}',
                        "  ]",
                        "}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "process-bundles",
                        "--execute",
                        "--validation",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("meeting_bundle_process_output_mode: validation", output)
            self.assertIn("99_Test Notes/Meetings", output)
            self.assertIn("99_Test Notes/Actions", output)
            self.assertFalse((vault / "01_Meetings" / "2026-05-04 - Teams - Platform Sync.md").exists())
            self.assertFalse((vault / "07_Actions" / "2026-05-04.md").exists())

    def test_meetings_attach_transcript_prints_attached_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            source = repo / "download.vtt"
            source.write_text("WEBVTT\n", encoding="utf-8")
            result = AttachTranscriptResult(
                metadata_path=vault / "00_Intake" / "bundles" / "delivery-review (outlook).json",
                attached_path=vault / "00_Intake" / "bundles" / "raw_transcripts" / "download.vtt",
                source_name="Manual / semi-manual intake",
            )

            with (
                patch("obsidian_intake_agent.main.attach_transcript_to_bundle", return_value=result) as attach_mock,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "attach-transcript",
                        "--event-id",
                        "evt-attach",
                        "--file",
                        str(source),
                    ]
                )

            self.assertEqual(exit_code, 0)
            attach_mock.assert_called_once_with(
                bundle_root=vault / "00_Intake" / "bundles",
                event_id="evt-attach",
                file_path=source,
            )
            output = stdout.getvalue()
            self.assertIn("meeting_bundle_transcript_attached:", output)
            self.assertIn("meeting_bundle_metadata_updated:", output)
            self.assertIn("meeting_bundle_preferred_source:", output)

    def test_build_meeting_discovery_client_uses_graph_when_token_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with patch.dict(os.environ, {"OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN": "token-value"}, clear=False):
                client = _build_meeting_discovery_client(config)

            self.assertIsInstance(client, GraphOutlookMeetingDiscoveryClient)

    def test_build_meeting_discovery_client_falls_back_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with patch.dict(os.environ, {}, clear=True):
                client = _build_meeting_discovery_client(config)

            self.assertIsInstance(client, UnconfiguredOutlookMeetingDiscoveryClient)

    def test_build_meeting_discovery_client_uses_cached_graph_auth_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
            ):
                provider_class.return_value.get_access_token.return_value = _FakeTokenResult("cached-token")
                client = _build_meeting_discovery_client(config)

            self.assertIsInstance(client, GraphOutlookMeetingDiscoveryClient)

    def test_build_meeting_discovery_client_falls_back_when_cached_auth_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
            ):
                provider_class.return_value.get_access_token.return_value = _FakeTokenResult(None)
                client = _build_meeting_discovery_client(config)

            self.assertIsInstance(client, UnconfiguredOutlookMeetingDiscoveryClient)

    def test_build_meeting_artifact_discovery_client_uses_local_only_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with patch.dict(os.environ, {}, clear=True):
                client = _build_meeting_artifact_discovery_client(config)

            self.assertIsInstance(client, LocalIntakeTranscriptDiscoveryClient)

    def test_build_meeting_artifact_discovery_client_uses_cached_graph_auth_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
            ):
                provider_class.return_value.get_access_token.return_value = _FakeTokenResult("cached-token")
                client = _build_meeting_artifact_discovery_client(config)

            self.assertIsInstance(client, ChainedMeetingArtifactDiscoveryClient)

    def test_build_meeting_artifact_discovery_client_chains_graph_and_local_with_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with patch.dict(os.environ, {"OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN": "token-value"}, clear=False):
                client = _build_meeting_artifact_discovery_client(config)

            self.assertIsInstance(client, ChainedMeetingArtifactDiscoveryClient)

    def test_build_meeting_artifact_discovery_client_can_download_transcripts_with_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with patch.dict(os.environ, {"OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN": "token-value"}, clear=False):
                client = _build_meeting_artifact_discovery_client(config, download_transcripts=True)

            self.assertIsInstance(client, ChainedMeetingArtifactDiscoveryClient)

    def test_meetings_sync_transcripts_exits_nonzero_when_configured_auth_requires_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("obsidian_intake_agent.main.build_transcript_sync_plan") as sync_plan_mock,
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                provider_class.return_value.get_access_token.return_value = _FakeTokenResult(
                    None,
                    source="cache_miss",
                    message="Graph auth is configured, but no cached token is available or refreshable.",
                    recovery_steps=(
                        ".venv/bin/obsidian-agent graph status",
                        ".venv/bin/obsidian-agent graph login",
                    ),
                )

                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "sync-transcripts",
                        "--since",
                        "2026-05-01",
                        "--dry-run",
                    ]
                )

            self.assertEqual(exit_code, 2)
            sync_plan_mock.assert_not_called()
            self.assertIn("graph_auth_required: yes", stderr.getvalue())
            self.assertIn(".venv/bin/obsidian-agent graph login", stderr.getvalue())


class GraphCliTests(unittest.TestCase):
    def test_graph_status_prints_status_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                provider = provider_class.return_value
                provider.status.return_value.lines.return_value = [
                    "graph_auth_configured: yes",
                    "graph_auth_silent_token: available",
                ]

                exit_code = main(["--config", str(config_path), "graph", "status"])

        self.assertEqual(exit_code, 0)
        self.assertIn("graph_auth_configured: yes", stdout.getvalue())
        self.assertIn("graph_auth_silent_token: available", stdout.getvalue())

    def test_graph_login_returns_zero_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                provider = provider_class.return_value
                provider.login.side_effect = lambda output: _FakeLoginResult(True, "matthew@example.com", "ok")

                exit_code = main(["--config", str(config_path), "graph", "login"])

        self.assertEqual(exit_code, 0)
        self.assertIn("graph_login_succeeded: yes", stdout.getvalue())
        self.assertIn("graph_login_account: matthew@example.com", stdout.getvalue())

    def test_graph_login_returns_one_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                provider = provider_class.return_value
                provider.login.side_effect = lambda output: _FakeLoginResult(False, None, "missing config")

                exit_code = main(["--config", str(config_path), "graph", "login"])

        self.assertEqual(exit_code, 1)
        self.assertIn("graph_login_succeeded: no", stdout.getvalue())
        self.assertIn("graph_login_message: missing config", stdout.getvalue())

    def test_graph_logout_prints_removed_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                provider = provider_class.return_value
                provider.logout.return_value = _FakeLogoutResult(True, Path("/tmp/cache.json"), "removed")

                exit_code = main(["--config", str(config_path), "graph", "logout"])

        self.assertEqual(exit_code, 0)
        self.assertIn("graph_logout_removed: yes", stdout.getvalue())
        self.assertIn("graph_logout_cache_path: /tmp/cache.json", stdout.getvalue())


class _FakeLoginResult:
    def __init__(self, succeeded: bool, username: str | None, message: str) -> None:
        self.succeeded = succeeded
        self.username = username
        self.message = message


class _FakeLogoutResult:
    def __init__(self, removed: bool, cache_path: Path, message: str) -> None:
        self.removed = removed
        self.cache_path = cache_path
        self.message = message


class _FakeTokenResult:
    def __init__(
        self,
        access_token: str | None,
        *,
        source: str = "cache",
        message: str = "fake token result",
        recovery_steps: tuple[str, ...] = (),
    ) -> None:
        self.access_token = access_token
        self.available = access_token is not None
        self.source = source
        self.message = message
        self.recovery_steps = recovery_steps


def _write_config(
    repo: Path,
    vault: Path,
    *,
    dry_run: bool = False,
    git_auto_commit_vault: bool = False,
    git_auto_commit_project: bool = False,
) -> Path:
    config_path = repo / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f'vault_path: "{vault}"',
                'intake_dir: "00_Intake"',
                'meetings_dir: "01_Meetings"',
                'actions_dir: "07_Actions"',
                'archive_intake_dir: "_Archive/Intake"',
                'templates_dir: "Templates"',
                'owner_filter: "Matthew"',
                f"dry_run: {'true' if dry_run else 'false'}",
                "include_unassigned: false",
                'llm_provider: "none"',
                "codex_model: null",
                'codex_exec_cmd: ["codex", "exec"]',
                'extraction_mode: "draft"',
                f"git_auto_commit_vault: {'true' if git_auto_commit_vault else 'false'}",
                f"git_auto_commit_project: {'true' if git_auto_commit_project else 'false'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


if __name__ == "__main__":
    unittest.main()
