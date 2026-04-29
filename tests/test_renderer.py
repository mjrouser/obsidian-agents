from __future__ import annotations

import unittest
from pathlib import Path

from obsidian_intake_agent.rendering.meeting_renderer import (
    format_obsidian_link,
    render_meeting_note,
)


class MeetingRendererTests(unittest.TestCase):
    def test_renders_meeting_note(self) -> None:
        rendered = render_meeting_note(
            heading="2026-03-11 - Teams - Weekly Sync",
            intake_file=Path("00_Intake/2026-03-11 - Teams - Weekly Sync.md"),
            owner_filter="Matthew",
            normalized_body="Agenda item\nAction item",
            meeting_date="2026-03-11",
            meeting_source="Teams",
            meeting_title="Weekly Sync",
        )
        self.assertIn("# 2026-03-11 - Teams - Weekly Sync", rendered)
        self.assertIn("- Source: Teams", rendered)
        self.assertIn("- Intake File: [[00_Intake/2026-03-11 - Teams - Weekly Sync.md]]", rendered)
        self.assertIn("- Owner Filter: Matthew", rendered)
        self.assertIn("Agenda item", rendered)

    def test_format_obsidian_link_is_idempotent_and_uses_forward_slashes(self) -> None:
        self.assertEqual(
            format_obsidian_link(Path("00_Intake/Raw Transcripts/file.vtt")),
            "[[00_Intake/Raw Transcripts/file.vtt]]",
        )
        self.assertEqual(
            format_obsidian_link("[[00_Intake/Raw Transcripts/file.vtt]]"),
            "[[00_Intake/Raw Transcripts/file.vtt]]",
        )


if __name__ == "__main__":
    unittest.main()
