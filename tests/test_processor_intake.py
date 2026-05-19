from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from obsidian_intake_agent.processors.meeting_processor import MeetingProcessor
from tests.helpers import config


class MeetingProcessorIntakeTests(unittest.TestCase):
    def test_skips_inbox_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            inbox = intake_dir / "INBOX.md"
            inbox.write_text("Inbox contents\n", encoding="utf-8")

            processor = MeetingProcessor(config(vault, dry_run=False))

            summary = processor.process_all_unprocessed()

            self.assertEqual(summary.processed_files, 0)
            self.assertEqual(summary.skipped_files, 1)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())

    def test_skips_placeholder_template_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            template_note = intake_dir / "YYYY-MM-DD - Teams - <meeting title>.md"
            template_note.write_text("Template contents\n", encoding="utf-8")

            processor = MeetingProcessor(config(vault, dry_run=False))

            summary = processor.process_all_unprocessed()

            self.assertEqual(summary.processed_files, 0)
            self.assertEqual(summary.skipped_files, 1)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())

    def test_skips_draft_untitled_notes_until_renamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)

            for basename in ("Untitled.md", "Untitled 2.md"):
                draft_note = intake_dir / basename
                draft_note.write_text("Pasted summary\n", encoding="utf-8")

            processor = MeetingProcessor(config(vault, dry_run=False))

            summary = processor.process_all_unprocessed()

            self.assertEqual(summary.processed_files, 0)
            self.assertEqual(summary.skipped_files, 2)
            self.assertEqual(summary.skipped_ignored_basename, 2)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())

    def test_skips_already_processed_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            processed_note = intake_dir / "weekly-sync.md"
            processed_note.write_text(
                "STATUS: PROCESSED — see [[01_Meetings/2026-03-11 - Unknown - weekly-sync.md]]\nBody\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(config(vault, dry_run=False))

            summary = processor.process_all_unprocessed()

            self.assertEqual(summary.processed_files, 0)
            self.assertEqual(summary.skipped_files, 1)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())

    def test_force_reprocesses_already_processed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            processed_note = intake_dir / "weekly-sync.md"
            processed_note.write_text(
                "STATUS: PROCESSED — see [[01_Meetings/old-note.md]]\n"
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )
            ts = datetime(2026, 3, 12, 10, 0, 0).timestamp()
            os.utime(processed_note, (ts, ts))

            processor = MeetingProcessor(config(vault, dry_run=False))

            result = processor.process_file(processed_note, force=True)

            self.assertTrue(result.processed)
            self.assertTrue((vault / "01_Meetings" / "2026-03-12 - Unknown - weekly-sync.md").exists())
            actions_text = (vault / "07_Actions" / "2026-03-09.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] complete Codex setup by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 "
                "[[2026-03-12 - Unknown - weekly-sync.md]]",
                actions_text,
            )

    def test_force_reprocess_keeps_single_processed_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            processed_note = intake_dir / "weekly-sync.md"
            processed_note.write_text(
                "STATUS: PROCESSED — see [[01_Meetings/old-note.md]]\n"
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )
            ts = datetime(2026, 3, 12, 10, 0, 0).timestamp()
            os.utime(processed_note, (ts, ts))

            processor = MeetingProcessor(config(vault, dry_run=False))

            processor.process_file(processed_note, force=True)

            updated_text = (vault / "_Archive" / "Intake" / "weekly-sync.md").read_text(encoding="utf-8")
            self.assertEqual(updated_text.count("STATUS: PROCESSED"), 1)
            self.assertIn(
                "STATUS: PROCESSED — see [[01_Meetings/2026-03-12 - Unknown - weekly-sync.md]]",
                updated_text,
            )

    def test_processed_vtt_is_skipped_on_second_run_via_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-12 - Teams - Platform Sync.vtt"
            source.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nAction: Matthew will ship it.\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(config(vault, dry_run=False, llm_provider="none"))
            first_summary = processor.process_all_unprocessed()
            source.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nAction: Matthew will ship it.\n",
                encoding="utf-8",
            )
            second_summary = processor.process_all_unprocessed()

            self.assertEqual(first_summary.processed_files, 1)
            self.assertTrue(
                (vault / "00_Intake" / "Intake Notes" / "2026-03-12 - Teams - Platform Sync (intake).md").exists()
            )
            self.assertFalse((vault / "00_Intake" / "2026-03-12 - Teams - Platform Sync (intake).md").exists())
            self.assertEqual(second_summary.processed_files, 0)
            self.assertEqual(second_summary.skipped_files, 1)


if __name__ == "__main__":
    unittest.main()
