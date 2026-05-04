from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from obsidian_intake_agent.automation_failures import (
    write_automation_failure_note,
    write_automation_failure_note_to_dir,
)
from obsidian_intake_agent.config import Config


class AutomationFailureNoteTests(unittest.TestCase):
    def test_writes_failure_note_under_system_error_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            log = Path(tmp_dir) / "weekly-wrap.stderr.log"
            log.write_text("Traceback\nTimeoutError: Codex CLI timed out\n", encoding="utf-8")
            config = _config(vault)

            note = write_automation_failure_note(
                config,
                job_name="weekly-wrap",
                exit_code=1,
                stderr_log_path=log,
                occurred_at=datetime(2026, 4, 29, 12, 15, tzinfo=UTC),
            )

            self.assertEqual(note.path.parent, vault / "_System" / "Agent Errors")
            self.assertEqual(note.path.name, "2026-04-29 121500 - weekly-wrap failed.md")
            self.assertEqual(
                note.latest_path, vault / "_System" / "Agent Errors" / "ACTION NEEDED - Latest Automation Failure.md"
            )
            text = note.path.read_text(encoding="utf-8")
            self.assertIn("# Automation failure: weekly-wrap", text)
            self.assertIn("ACTION NEEDED: This automation failed.", text)
            self.assertIn("- Exit code: `1`", text)
            self.assertIn("## Summary\n\nTimeoutError: Codex CLI timed out", text)
            self.assertIn("TimeoutError: Codex CLI timed out", text)
            latest_text = note.latest_path.read_text(encoding="utf-8")
            self.assertIn("# ACTION NEEDED: Latest automation failure", latest_text)
            self.assertIn(f"- Full failure note: `{note.path}`", latest_text)

    def test_missing_stderr_log_is_reported_in_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            missing_log = Path(tmp_dir) / "missing.stderr.log"
            config = _config(vault)

            note = write_automation_failure_note(
                config,
                job_name="intake watcher",
                exit_code=2,
                stderr_log_path=missing_log,
                occurred_at=datetime(2026, 4, 29, 9, 0, tzinfo=UTC),
            )

            text = note.path.read_text(encoding="utf-8")
            self.assertIn("Stderr log was not found", text)
            self.assertEqual(note.path.name, "2026-04-29 090000 - intake-watcher failed.md")

    def test_repeated_failure_notes_do_not_overwrite_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            log = Path(tmp_dir) / "weekly-wrap.stderr.log"
            log.write_text("failure\n", encoding="utf-8")
            config = _config(vault)
            occurred_at = datetime(2026, 4, 29, 12, 15, tzinfo=UTC)

            first = write_automation_failure_note(
                config,
                job_name="weekly-wrap",
                exit_code=1,
                stderr_log_path=log,
                occurred_at=occurred_at,
            )
            second = write_automation_failure_note(
                config,
                job_name="weekly-wrap",
                exit_code=1,
                stderr_log_path=log,
                occurred_at=occurred_at,
            )

            self.assertNotEqual(first.path, second.path)
            self.assertEqual(second.path.name, "2026-04-29 121500 - weekly-wrap failed-2.md")
            self.assertEqual(first.latest_path, second.latest_path)
            latest_text = second.latest_path.read_text(encoding="utf-8")
            self.assertIn(f"- Full failure note: `{second.path}`", latest_text)
            self.assertNotIn(f"- Full failure note: `{first.path}`", latest_text)

    def test_writes_failure_note_to_explicit_directory_with_extra_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            note_dir = Path(tmp_dir) / "automation-failures"
            log = Path(tmp_dir) / "weekly-briefing.stderr.log"
            log.write_text("FileNotFoundError: missing config.yaml\n", encoding="utf-8")

            note = write_automation_failure_note_to_dir(
                note_dir,
                job_name="weekly-briefing",
                exit_code=1,
                stderr_log_path=log,
                occurred_at=datetime(2026, 5, 4, 9, 0, tzinfo=UTC),
                extra_details=[
                    "Config path could not be loaded: `/repo/config.yaml`",
                    "This fallback note was written outside the vault because automation config was unavailable.",
                ],
            )

            self.assertEqual(note.path.parent, note_dir)
            self.assertEqual(note.latest_path.parent, note_dir)
            text = note.path.read_text(encoding="utf-8")
            self.assertIn("## Automation context", text)
            self.assertIn("Config path could not be loaded: `/repo/config.yaml`", text)
            self.assertIn("FileNotFoundError: missing config.yaml", text)


def _config(vault: Path) -> Config:
    return Config(
        vault_path=vault,
        intake_dir="00_Intake",
        meetings_dir="01_Meetings",
        actions_dir="07_Actions",
        archive_intake_dir="z_Archive/Intake",
        templates_dir="Templates",
        owner_filter="Matthew",
        dry_run=False,
        automation_error_dir="_System/Agent Errors",
    )


if __name__ == "__main__":
    unittest.main()
