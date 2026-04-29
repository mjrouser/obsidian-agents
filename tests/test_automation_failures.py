from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from obsidian_intake_agent.automation_failures import write_automation_failure_note
from obsidian_intake_agent.processors.meeting_processor import Config


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
            text = note.path.read_text(encoding="utf-8")
            self.assertIn("# Automation failure: weekly-wrap", text)
            self.assertIn("- Exit code: `1`", text)
            self.assertIn("TimeoutError: Codex CLI timed out", text)

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
