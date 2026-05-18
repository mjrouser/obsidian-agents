from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from obsidian_intake_agent.web_clips.bookmarklet import render_bookmarklet
from obsidian_intake_agent.web_clips.capture_server import capture_payload_to_note, sanitize_clip_filename


class WebClipCaptureServerTests(unittest.TestCase):
    def test_sanitize_clip_filename_keeps_safe_title_and_date(self) -> None:
        captured_at = datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc)

        name = sanitize_clip_filename("A/B: Useful * Article?", captured_at)

        self.assertEqual(name, "2026-05-18 - A-B Useful Article.md")

    def test_capture_payload_to_note_writes_raw_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_dir = Path(tmp_dir) / "00_Intake" / "Web Clips"
            payload = {
                "source_url": "https://example.com/article",
                "source_title": "Article Title",
                "captured_at": "2026-05-18T14:30:00+00:00",
                "why": "Use this for CRM adoption messaging.",
                "passages": ["First exact passage.", "Second exact passage."],
            }

            written = capture_payload_to_note(payload, intake_dir=intake_dir)

            self.assertTrue(written.exists())
            text = written.read_text(encoding="utf-8")
            self.assertIn("type: web_clip_intake", text)
            self.assertIn("> First exact passage.", text)
            self.assertIn("> Second exact passage.", text)

    def test_capture_payload_rejects_blank_passages(self) -> None:
        payload = {
            "source_url": "https://example.com/article",
            "source_title": "Article Title",
            "captured_at": "2026-05-18T14:30:00+00:00",
            "why": "Use this.",
            "passages": [" "],
        }

        with self.assertRaisesRegex(ValueError, "passages cannot be blank"):
            capture_payload_to_note(payload, intake_dir=Path("/tmp/not-used"))

    def test_bookmarklet_targets_configured_endpoint(self) -> None:
        bookmarklet = render_bookmarklet(host="127.0.0.1", port=8765)

        self.assertTrue(bookmarklet.startswith("javascript:"))
        self.assertIn("http://127.0.0.1:8765/capture", bookmarklet)
        self.assertIn("Add passage", bookmarklet)
