from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from obsidian_intake_agent.config import Config
from obsidian_intake_agent.weekly import (
    _prompt_path_for_mode,
    _review_filename,
    build_weekly_source_bundle,
    generate_weekly_snapshot,
    run_codex_markdown,
)


class WeeklySnapshotTests(unittest.TestCase):
    def test_review_filename_uses_monday_date_and_mode(self) -> None:
        monday = date(2026, 3, 30)
        self.assertEqual(_review_filename(monday=monday, mode="briefing"), "2026-03-30 Weekly Briefing.md")
        self.assertEqual(_review_filename(monday=monday, mode="wrap"), "2026-03-30 Weekly Wrap.md")

    def test_build_source_bundle_includes_actions_previous_snapshot_and_recent_meetings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            (vault / "07_Actions").mkdir(parents=True)
            (vault / "09_Weekly Reviews").mkdir(parents=True)
            (vault / "01_Meetings").mkdir(parents=True)
            (vault / "07_Actions" / "2026-03-30.md").write_text("actions", encoding="utf-8")
            (vault / "09_Weekly Reviews" / "2026-03-23 Weekly Briefing.md").write_text("snapshot", encoding="utf-8")
            (vault / "01_Meetings" / "2026-03-28 - Teams - Sync.md").write_text("meeting", encoding="utf-8")

            config = _config(vault)
            bundle = build_weekly_source_bundle(
                config,
                monday=date(2026, 3, 30),
                run_date=date(2026, 4, 3),
                mode="briefing",
            )

            self.assertIn("2026-03-30.md", bundle)
            self.assertIn("2026-03-23 Weekly Briefing.md", bundle)
            self.assertIn("2026-03-28 - Teams - Sync.md", bundle)

    def test_generate_weekly_snapshot_writes_mode_specific_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            (vault / "07_Actions").mkdir(parents=True)
            config = _config(vault)

            with patch(
                "obsidian_intake_agent.weekly.run_codex_markdown", return_value="# Weekly Briefing"
            ) as codex_mock:
                result = generate_weekly_snapshot(
                    config,
                    mode="briefing",
                    target_date=date(2026, 3, 30),
                    dry_run=False,
                )

            self.assertTrue(result.changed)
            self.assertEqual(result.review_path.name, "2026-03-30 Weekly Briefing.md")
            self.assertTrue(result.review_path.exists())
            text = result.review_path.read_text(encoding="utf-8")
            self.assertIn("# Weekly Briefing", text)
            codex_mock.assert_called_once()

    def test_rerun_updates_same_mode_specific_file_without_creating_other_mode_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            (vault / "07_Actions").mkdir(parents=True)
            config = _config(vault)

            with patch("obsidian_intake_agent.weekly.run_codex_markdown", return_value="# Weekly Briefing V1"):
                first = generate_weekly_snapshot(config, mode="briefing", target_date=date(2026, 3, 30), dry_run=False)
            with patch("obsidian_intake_agent.weekly.run_codex_markdown", return_value="# Weekly Briefing V2"):
                second = generate_weekly_snapshot(config, mode="briefing", target_date=date(2026, 3, 30), dry_run=False)

            self.assertEqual(first.review_path, second.review_path)
            self.assertEqual(second.review_path.name, "2026-03-30 Weekly Briefing.md")
            self.assertEqual(second.review_path.read_text(encoding="utf-8"), "# Weekly Briefing V2\n")
            self.assertFalse((vault / "09_Weekly Reviews" / "2026-03-30 Weekly Wrap.md").exists())

    def test_non_dry_run_weekly_note_write_archives_older_dated_weekly_review_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            reviews = vault / "09_Weekly Reviews"
            (vault / "07_Actions").mkdir(parents=True)
            reviews.mkdir(parents=True)
            old_review = reviews / "2026-03-16 Weekly Briefing.md"
            kept_review = reviews / "2026-03-23 Weekly Briefing.md"
            old_review.write_text("old", encoding="utf-8")
            kept_review.write_text("kept", encoding="utf-8")
            config = _config(vault, archive_retention_count=2)

            with patch("obsidian_intake_agent.weekly.run_codex_markdown", return_value="# Weekly Briefing"):
                result = generate_weekly_snapshot(
                    config,
                    mode="briefing",
                    target_date=date(2026, 3, 30),
                    dry_run=False,
                )

            self.assertTrue(result.changed)
            self.assertTrue(result.review_path.exists())
            self.assertTrue(kept_review.exists())
            self.assertFalse(old_review.exists())
            self.assertEqual(
                (reviews / "Review & Wrap Archive" / old_review.name).read_text(encoding="utf-8"),
                "old",
            )

    def test_dry_run_weekly_generation_does_not_archive_older_weekly_review_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            reviews = vault / "09_Weekly Reviews"
            (vault / "07_Actions").mkdir(parents=True)
            reviews.mkdir(parents=True)
            old_review = reviews / "2026-03-16 Weekly Briefing.md"
            kept_review = reviews / "2026-03-23 Weekly Briefing.md"
            old_review.write_text("old", encoding="utf-8")
            kept_review.write_text("kept", encoding="utf-8")
            config = _config(vault, archive_retention_count=1)

            with patch("obsidian_intake_agent.weekly.run_codex_markdown", return_value="# Weekly Briefing"):
                result = generate_weekly_snapshot(
                    config,
                    mode="briefing",
                    target_date=date(2026, 3, 30),
                    dry_run=True,
                )

            self.assertTrue(result.changed)
            self.assertFalse(result.review_path.exists())
            self.assertTrue(old_review.exists())
            self.assertTrue(kept_review.exists())
            self.assertFalse((reviews / "Review & Wrap Archive").exists())

    def test_unchanged_weekly_generation_does_not_archive_older_weekly_review_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            reviews = vault / "09_Weekly Reviews"
            (vault / "07_Actions").mkdir(parents=True)
            reviews.mkdir(parents=True)
            unchanged_review = reviews / "2026-03-30 Weekly Briefing.md"
            old_review = reviews / "2026-03-16 Weekly Briefing.md"
            kept_review = reviews / "2026-03-23 Weekly Briefing.md"
            unchanged_review.write_text("# Weekly Briefing\n", encoding="utf-8")
            old_review.write_text("old", encoding="utf-8")
            kept_review.write_text("kept", encoding="utf-8")
            config = _config(vault, archive_retention_count=1)

            with patch("obsidian_intake_agent.weekly.run_codex_markdown", return_value="# Weekly Briefing"):
                result = generate_weekly_snapshot(
                    config,
                    mode="briefing",
                    target_date=date(2026, 3, 30),
                    dry_run=False,
                )

            self.assertFalse(result.changed)
            self.assertTrue(unchanged_review.exists())
            self.assertTrue(old_review.exists())
            self.assertTrue(kept_review.exists())
            self.assertFalse((reviews / "Review & Wrap Archive").exists())

    def test_wrap_prompt_path_uses_correct_spelling(self) -> None:
        self.assertEqual(_prompt_path_for_mode("wrap").name, "friday_weekly_wrap_prompt.md")
        self.assertTrue(_prompt_path_for_mode("wrap").exists())


class WeeklyCodexTests(unittest.TestCase):
    def test_run_codex_markdown_uses_configured_exec_cmd(self) -> None:
        with patch("obsidian_intake_agent.weekly.run_codex_stdout", return_value="# Weekly Briefing\n") as run_mock:
            rendered = run_codex_markdown("prompt", model="gpt-5", exec_cmd=["custom-codex", "exec"])

        self.assertEqual(rendered, "# Weekly Briefing\n")
        run_mock.assert_called_once_with(
            "prompt",
            model="gpt-5",
            exec_cmd=["custom-codex", "exec"],
            timeout_seconds=None,
            output_kind="markdown",
        )

    def test_run_codex_markdown_raises_clear_error_on_timeout(self) -> None:
        with (
            patch(
                "obsidian_intake_agent.weekly.run_codex_stdout",
                side_effect=TimeoutError("Codex CLI timed out after 30 seconds while returning markdown."),
            ),
            self.assertRaisesRegex(
                TimeoutError,
                "Codex CLI timed out after 30 seconds while returning markdown",
            ),
        ):
            run_codex_markdown("prompt", model=None, exec_cmd=["codex", "exec"], timeout_seconds=30)


def _config(vault: Path, *, archive_retention_count: int = 4) -> Config:
    return Config(
        vault_path=vault,
        intake_dir="00_Intake",
        meetings_dir="01_Meetings",
        actions_dir="07_Actions",
        archive_intake_dir="z_Archive/Intake",
        templates_dir="Templates",
        owner_filter="Matthew",
        dry_run=False,
        llm_provider="codex_cli",
        codex_model=None,
        codex_exec_cmd=["codex", "exec"],
        weekly_reviews_dir="09_Weekly Reviews",
        weekly_reviews_archive_dir="Review & Wrap Archive",
        archive_retention_count=archive_retention_count,
        watcher_settle_seconds=1,
        watcher_stable_seconds=0,
        automation_log_dir=str(vault / "logs"),
    )
