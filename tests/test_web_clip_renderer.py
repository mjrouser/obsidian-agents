from __future__ import annotations

import unittest
from datetime import datetime, timezone

import yaml

from obsidian_intake_agent.rendering.web_clip_renderer import (
    render_processed_web_clip_note,
    render_raw_web_clip_note,
)
from obsidian_intake_agent.web_clips.models import ProcessedWebClip, WebClipCapture, WebClipPassage


class WebClipRendererTests(unittest.TestCase):
    def test_render_raw_note_preserves_passages_and_source_metadata(self) -> None:
        capture = WebClipCapture(
            source_url="https://example.com/article",
            source_title="Article Title",
            captured_at=datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc),
            why="Use this for CRM adoption messaging.",
            passages=[
                WebClipPassage(text="First exact passage."),
                WebClipPassage(text="Second exact passage."),
            ],
        )

        rendered = render_raw_web_clip_note(capture)

        self.assertIn("type: web_clip_intake", rendered)
        self.assertIn("status: unprocessed", rendered)
        self.assertIn('source_url: "https://example.com/article"', rendered)
        self.assertIn("# Article Title", rendered)
        self.assertIn("Use this for CRM adoption messaging.", rendered)
        self.assertIn("> First exact passage.", rendered)
        self.assertIn("> Second exact passage.", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_render_processed_note_preserves_original_passages(self) -> None:
        processed = ProcessedWebClip(
            source_url="https://example.com/article",
            source_title="Article Title",
            captured_at="2026-05-18T14:30:00+00:00",
            why="Use this for CRM adoption messaging.",
            summary="This source explains adoption pressure.",
            passages=["First exact passage.", "Second exact passage."],
            topics=["adoption", "crm"],
            application="Use this for CRM adoption messaging.",
            related=[],
            intake_source="z_Archive/Intake/Web Clips/Article Title.md",
        )

        rendered = render_processed_web_clip_note(processed)

        self.assertIn("type: web_clip", rendered)
        self.assertIn('- "adoption"', rendered)
        self.assertIn("## Application", rendered)
        self.assertIn("Use this for CRM adoption messaging.", rendered)
        self.assertIn("> First exact passage.", rendered)
        self.assertIn("> Second exact passage.", rendered)
        self.assertIn("[[z_Archive/Intake/Web Clips/Article Title.md]]", rendered)

    def test_render_processed_note_quotes_topics_as_yaml_strings(self) -> None:
        topics = ["foo: bar", "foo # bar", "- starts list"]
        processed = ProcessedWebClip(
            source_url="https://example.com/article",
            source_title="Article Title",
            captured_at="2026-05-18T14:30:00+00:00",
            why="Use this for CRM adoption messaging.",
            summary="This source explains adoption pressure.",
            passages=["First exact passage."],
            topics=topics,
            application="Use this for CRM adoption messaging.",
            related=[],
            intake_source="z_Archive/Intake/Web Clips/Article Title.md",
        )

        rendered = render_processed_web_clip_note(processed)

        frontmatter = rendered.split("---\n", 2)[1]
        metadata = yaml.safe_load(frontmatter)
        self.assertEqual(topics, metadata["topics"])

    def test_render_processed_note_frontmatter_scalars_round_trip_control_characters(self) -> None:
        topics = ["foo\nbar", "foo: # bar"]
        related = ["context\nwith newline", "related: # item"]
        source_title = "Article\nTitle # with colon:"
        processed = ProcessedWebClip(
            source_url="https://example.com/article",
            source_title=source_title,
            captured_at="2026-05-18T14:30:00+00:00",
            why="Use this for CRM adoption messaging.",
            summary="This source explains adoption pressure.",
            passages=["First exact passage."],
            topics=topics,
            application="Use this for CRM adoption messaging.",
            related=related,
            intake_source="z_Archive/Intake/Web Clips/Article Title.md",
        )

        rendered = render_processed_web_clip_note(processed)

        frontmatter = rendered.split("---\n", 2)[1]
        metadata = yaml.safe_load(frontmatter)
        self.assertEqual(topics, metadata["topics"])
        self.assertEqual(related, metadata["related"])
        self.assertEqual(source_title, metadata["source_title"])
