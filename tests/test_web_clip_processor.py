from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import config
from obsidian_intake_agent.processors.web_clip_processor import WebClipProcessor


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
