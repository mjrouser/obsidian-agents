from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from obsidian_intake_agent.web_clips.bookmarklet import render_bookmarklet
from obsidian_intake_agent.web_clips.capture_server import (
    MAX_FIELD_CHARS,
    MAX_PASSAGES,
    MAX_REQUEST_BYTES,
    capture_payload_to_note,
    is_valid_capture_token,
    parse_content_length,
    run_capture_server,
    sanitize_clip_filename,
)


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

    def test_capture_payload_rejects_field_over_limit(self) -> None:
        payload = {
            "source_url": "https://example.com/article",
            "source_title": "Article Title",
            "captured_at": "2026-05-18T14:30:00+00:00",
            "why": "x" * (MAX_FIELD_CHARS + 1),
            "passages": ["Useful passage."],
        }

        with self.assertRaisesRegex(ValueError, "why is too large"):
            capture_payload_to_note(payload, intake_dir=Path("/tmp/not-used"))

    def test_capture_payload_rejects_too_many_passages(self) -> None:
        payload = {
            "source_url": "https://example.com/article",
            "source_title": "Article Title",
            "captured_at": "2026-05-18T14:30:00+00:00",
            "why": "Use this.",
            "passages": ["Useful passage."] * (MAX_PASSAGES + 1),
        }

        with self.assertRaisesRegex(ValueError, "too many passages"):
            capture_payload_to_note(payload, intake_dir=Path("/tmp/not-used"))

    def test_parse_content_length_accepts_positive_bounded_value(self) -> None:
        self.assertEqual(parse_content_length(str(MAX_REQUEST_BYTES)), MAX_REQUEST_BYTES)

    def test_parse_content_length_rejects_missing_or_malformed_values(self) -> None:
        for value in (None, "", "abc", "1.5"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Content-Length"):
                    parse_content_length(value)

    def test_parse_content_length_rejects_non_positive_values(self) -> None:
        for value in ("0", "-1"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Content-Length"):
                    parse_content_length(value)

    def test_parse_content_length_rejects_large_requests(self) -> None:
        with self.assertRaisesRegex(ValueError, "request is too large"):
            parse_content_length(str(MAX_REQUEST_BYTES + 1))

    def test_sanitize_clip_filename_truncates_long_titles(self) -> None:
        captured_at = datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc)

        name = sanitize_clip_filename("x" * 300, captured_at)

        self.assertEqual(len(name.removeprefix("2026-05-18 - ").removesuffix(".md")), 120)

    def test_capture_payload_to_note_uses_unique_name_for_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_dir = Path(tmp_dir) / "00_Intake" / "Web Clips"
            payload = {
                "source_url": "https://example.com/article",
                "source_title": "Article Title",
                "captured_at": "2026-05-18T14:30:00+00:00",
                "why": "Use this.",
                "passages": ["Useful passage."],
            }

            first = capture_payload_to_note(payload, intake_dir=intake_dir)
            second = capture_payload_to_note(payload, intake_dir=intake_dir)

            self.assertNotEqual(first, second)
            self.assertEqual(second.name, "2026-05-18 - Article Title 2.md")

    def test_bookmarklet_targets_configured_endpoint(self) -> None:
        bookmarklet = render_bookmarklet(host="127.0.0.1", port=8765, token="secret-token")

        self.assertTrue(bookmarklet.startswith("javascript:"))
        self.assertIn("http://127.0.0.1:8765/capture", bookmarklet)
        self.assertIn("Add passage", bookmarklet)

    def test_bookmarklet_sends_token_header_when_configured(self) -> None:
        bookmarklet = render_bookmarklet(host="127.0.0.1", port=8765, token="secret-token")

        self.assertIn("X-Obsidian-Web-Clipper-Token", bookmarklet)
        self.assertIn("secret-token", bookmarklet)

    def test_bookmarklet_rejects_blank_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "token is required"):
            render_bookmarklet(host="127.0.0.1", port=8765, token="")

    def test_capture_token_validation_requires_match(self) -> None:
        self.assertTrue(is_valid_capture_token({"X-Obsidian-Web-Clipper-Token": "secret-token"}, "secret-token"))
        self.assertFalse(is_valid_capture_token({"X-Obsidian-Web-Clipper-Token": "wrong-token"}, "secret-token"))
        self.assertFalse(is_valid_capture_token({}, "secret-token"))

    def test_capture_token_validation_rejects_blank_configured_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "token is required"):
            is_valid_capture_token({"X-Obsidian-Web-Clipper-Token": "secret-token"}, "")

    def test_capture_server_rejects_blank_token_before_starting(self) -> None:
        with self.assertRaisesRegex(ValueError, "token is required"):
            run_capture_server(host="127.0.0.1", port=8765, intake_dir=Path("/tmp/not-used"), token="")
