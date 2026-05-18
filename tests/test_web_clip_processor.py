from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from obsidian_intake_agent.processors.web_clip_processor import WebClipProcessor
from tests.helpers import config


class WebClipProcessorTests(unittest.TestCase):
    def test_dry_run_reports_planned_outputs_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=True)

            self.assertEqual(result.processed_files, 1)
            self.assertFalse((vault / "10_References" / "Web Clips").exists())
            self.assertTrue(raw.exists())

    def test_process_writes_reference_note_and_archives_raw_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=False)

            self.assertEqual(result.processed_files, 1)
            output = vault / "10_References" / "Web Clips" / "2026-05-18 - Article.md"
            archived = vault / "_Archive" / "Intake" / "Web Clips" / "2026-05-18 - Article.md"
            self.assertTrue(output.exists())
            self.assertTrue(archived.exists())
            self.assertFalse(raw.exists())
            text = output.read_text(encoding="utf-8")
            self.assertIn("type: web_clip", text)
            self.assertIn("> First exact passage.", text)
            self.assertIn("[[_Archive/Intake/Web Clips/2026-05-18 - Article.md]]", text)

    def test_rerun_is_idempotent_when_raw_capture_was_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            first = processor.process_all(dry_run=False)
            second = processor.process_all(dry_run=False)

            self.assertEqual(first.processed_files, 1)
            self.assertEqual(second.processed_files, 0)

    def test_process_preserves_existing_outputs_with_unique_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            reference = vault / "10_References" / "Web Clips" / "2026-05-18 - Article.md"
            archive = vault / "_Archive" / "Intake" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            reference.parent.mkdir(parents=True)
            archive.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            reference.write_text("existing reference", encoding="utf-8")
            archive.write_text("existing archive", encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=False)

            unique_reference = vault / "10_References" / "Web Clips" / "2026-05-18 - Article 2.md"
            unique_archive = vault / "_Archive" / "Intake" / "Web Clips" / "2026-05-18 - Article 2.md"
            self.assertEqual(result.processed_files, 1)
            self.assertEqual(reference.read_text(encoding="utf-8"), "existing reference")
            self.assertEqual(archive.read_text(encoding="utf-8"), "existing archive")
            self.assertTrue(unique_reference.exists())
            self.assertTrue(unique_archive.exists())
            self.assertFalse(raw.exists())
            self.assertIn(
                "[[_Archive/Intake/Web Clips/2026-05-18 - Article 2.md]]",
                unique_reference.read_text(encoding="utf-8"),
            )

    def test_process_archives_raw_capture_when_matching_reference_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            reference = vault / "10_References" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            reference.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            reference.write_text(
                "existing processed note\n[[_Archive/Intake/Web Clips/2026-05-18 - Article.md]]\n",
                encoding="utf-8",
            )
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=False)

            archive = vault / "_Archive" / "Intake" / "Web Clips" / "2026-05-18 - Article.md"
            self.assertEqual(result.processed_files, 1)
            self.assertEqual(result.written_reference_notes, 0)
            self.assertEqual(result.archived_raw_captures, 1)
            self.assertTrue(archive.exists())
            self.assertFalse((vault / "10_References" / "Web Clips" / "2026-05-18 - Article 2.md").exists())

    def test_process_recovers_partial_collision_reference_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            references = vault / "10_References" / "Web Clips"
            archives = vault / "_Archive" / "Intake" / "Web Clips"
            raw.parent.mkdir(parents=True)
            references.mkdir(parents=True)
            archives.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            (references / "2026-05-18 - Article.md").write_text("existing reference", encoding="utf-8")
            (archives / "2026-05-18 - Article.md").write_text("existing archive", encoding="utf-8")
            (references / "2026-05-18 - Article 2.md").write_text(
                "partial processed note\n[[_Archive/Intake/Web Clips/2026-05-18 - Article 2.md]]\n",
                encoding="utf-8",
            )
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=False)

            self.assertEqual(result.processed_files, 1)
            self.assertEqual(result.written_reference_notes, 0)
            self.assertEqual(result.archived_raw_captures, 1)
            self.assertTrue((archives / "2026-05-18 - Article 2.md").exists())
            self.assertFalse((references / "2026-05-18 - Article 3.md").exists())
            self.assertFalse(raw.exists())

    def test_process_rejects_outputs_outside_vault_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            outside = Path(tmp_dir) / "outside"
            processor = WebClipProcessor(replace(config(vault, dry_run=False), web_clips_references_dir="../outside"))

            with self.assertRaisesRegex(ValueError, "outside the vault"):
                processor.process_all(dry_run=False)

            self.assertTrue(raw.exists())
            self.assertFalse(outside.exists())

    def test_process_rejects_intake_path_outside_vault_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            outside = Path(tmp_dir) / "outside"
            raw = outside / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            processor = WebClipProcessor(replace(config(vault, dry_run=False), web_clips_intake_dir="../outside"))

            with self.assertRaisesRegex(ValueError, "outside the vault"):
                processor.process_all(dry_run=False)

            self.assertTrue(raw.exists())

    def test_process_skips_symlinked_raw_clip_outside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            outside = Path(tmp_dir) / "outside"
            outside_raw = outside / "outside.md"
            outside_raw.parent.mkdir(parents=True)
            outside_raw.write_text(_raw_note(), encoding="utf-8")
            intake = vault / "00_Intake" / "Web Clips"
            intake.mkdir(parents=True)
            try:
                (intake / "2026-05-18 - Article.md").symlink_to(outside_raw)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=False)

            self.assertEqual(result.processed_files, 0)
            self.assertTrue(outside_raw.exists())
            self.assertFalse((vault / "10_References" / "Web Clips").exists())

    def test_process_skips_symlinked_raw_clip_inside_vault_without_moving_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            target = vault / "00_Intake" / "Other" / "target.md"
            intake = vault / "00_Intake" / "Web Clips"
            target.parent.mkdir(parents=True)
            intake.mkdir(parents=True)
            target.write_text(_raw_note(), encoding="utf-8")
            symlink = intake / "2026-05-18 - Article.md"
            try:
                symlink.symlink_to(target)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            processor = WebClipProcessor(config(vault, dry_run=False))

            first = processor.process_all(dry_run=False)
            second = processor.process_all(dry_run=False)

            self.assertEqual(first.processed_files, 0)
            self.assertEqual(second.processed_files, 0)
            self.assertTrue(target.exists())
            self.assertTrue(symlink.exists())
            self.assertFalse((vault / "10_References" / "Web Clips").exists())

    def test_process_skips_malformed_frontmatter_without_blocking_later_clips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake = vault / "00_Intake" / "Web Clips"
            intake.mkdir(parents=True)
            malformed = intake / "2026-05-18 - Broken.md"
            valid = intake / "2026-05-18 - Article.md"
            malformed.write_text("---\ntype: web_clip_intake\nstatus: unprocessed\n", encoding="utf-8")
            valid.write_text(_raw_note(), encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=False)

            self.assertEqual(result.processed_files, 1)
            self.assertTrue(malformed.exists())
            self.assertTrue((vault / "10_References" / "Web Clips" / "2026-05-18 - Article.md").exists())


def _raw_note() -> str:
    return """---
type: web_clip_intake
status: unprocessed
captured_at: "2026-05-18T14:30:00+00:00"
source_url: "https://example.com/article"
source_title: "Article"
---

# Article

Source: https://example.com/article

## Why This Matters

Use this for CRM adoption messaging.

## Captured Passages

> First exact passage.

> Second exact passage.
"""
