from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from obsidian_intake_agent.config import Config
from obsidian_intake_agent.main import _build_meeting_discovery_client, main
from obsidian_intake_agent.meetings import (
    BundleWriteResult,
    GraphOutlookMeetingDiscoveryClient,
    TranscriptSyncPlan,
    UnconfiguredOutlookMeetingDiscoveryClient,
)
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

    def test_meetings_sync_transcripts_prints_dry_run_summary(self) -> None:
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
                        skipped_existing_bundle_note_paths=(),
                        skipped_existing_metadata_paths=(),
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
            self.assertIn("bundle_note_written:", output)
            self.assertIn("outlook_metadata_written:", output)

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


def _write_config(
    repo: Path,
    vault: Path,
    *,
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
                "dry_run: false",
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
