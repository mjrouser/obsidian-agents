from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obsidian_intake_agent.processors.intake_state import IntakeState, strip_leading_status_marker


class IntakeStateTests(unittest.TestCase):
    def test_skip_reason_ignores_draft_and_placeholder_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake = vault / "00_Intake"
            intake.mkdir(parents=True)
            state = IntakeState(intake_path=intake, archive_path=vault / "_Archive" / "Intake")

            ignored_names = [
                "INBOX.md",
                "Untitled.md",
                "Untitled 2.md",
                "YYYY-MM-DD - Teams - <meeting title>.md",
            ]
            for name in ignored_names:
                path = intake / name
                path.write_text("Draft\n", encoding="utf-8")

                self.assertEqual(state.skip_reason(path), "ignored basename")
                self.assertFalse(state.should_process(path))

    def test_skip_reason_detects_unsupported_and_processed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake = vault / "00_Intake"
            intake.mkdir(parents=True)
            state = IntakeState(intake_path=intake, archive_path=vault / "_Archive" / "Intake")
            unsupported = intake / "image.png"
            unsupported.write_text("not a meeting\n", encoding="utf-8")
            processed = intake / "weekly-sync.md"
            processed.write_text("STATUS: PROCESSED - see [[note.md]]\nBody\n", encoding="utf-8")

            self.assertEqual(state.skip_reason(unsupported), "unsupported ext")
            self.assertEqual(state.skip_reason(processed), "already processed")

    def test_vtt_sidecar_marks_raw_vtt_as_already_processed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake = vault / "00_Intake"
            intake.mkdir(parents=True)
            state = IntakeState(intake_path=intake, archive_path=vault / "_Archive" / "Intake")
            raw_vtt = intake / "2026-03-12 - Teams - Platform Sync.vtt"
            raw_vtt.write_text("WEBVTT\n", encoding="utf-8")
            sidecar = intake / "2026-03-12 - Teams - Platform Sync (intake).md"
            sidecar.write_text("STATUS: PROCESSED - see [[2026-03-12 - Teams - Platform Sync.md]]\n", encoding="utf-8")

            self.assertEqual(state.vtt_sidecar_path(raw_vtt), sidecar)
            self.assertTrue(state.vtt_has_processed_sidecar(raw_vtt))
            self.assertEqual(state.skip_reason(raw_vtt), "already processed")

    def test_archive_destination_preserves_intake_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake = vault / "00_Intake"
            archive = vault / "_Archive" / "Intake"
            source = intake / "Raw Transcripts" / "weekly-sync.md"
            source.parent.mkdir(parents=True)
            source.write_text("Body\n", encoding="utf-8")
            state = IntakeState(intake_path=intake, archive_path=archive)

            self.assertEqual(state.archive_destination(source), archive / "Raw Transcripts" / "weekly-sync.md")

    def test_strip_leading_status_marker_removes_only_leading_processed_markers(self) -> None:
        text = (
            "STATUS: PROCESSED - see [[old.md]]\n"
            "STATUS: PROCESSED - see [[new.md]]\n"
            "Agenda\n"
            "STATUS: PROCESSED should remain in body\n"
        )

        self.assertEqual(
            strip_leading_status_marker(text),
            "Agenda\nSTATUS: PROCESSED should remain in body",
        )


if __name__ == "__main__":
    unittest.main()
