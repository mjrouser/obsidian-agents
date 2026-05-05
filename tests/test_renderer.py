from __future__ import annotations

import unittest
from pathlib import Path

from obsidian_intake_agent.rendering.meeting_renderer import (
    format_obsidian_link,
    render_extracted_meeting_note,
    render_meeting_front_matter,
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
        self.assertTrue(rendered.startswith("---\n"))
        self.assertIn('date: "2026-03-11"', rendered)
        self.assertIn('source: "Teams"', rendered)
        self.assertIn("participants: []", rendered)
        self.assertIn('owner_filter: "Matthew"', rendered)
        self.assertIn('intake_file: "00_Intake/2026-03-11 - Teams - Weekly Sync.md"', rendered)
        self.assertIn("# 2026-03-11 - Teams - Weekly Sync", rendered)
        self.assertIn("- Source: Teams", rendered)
        self.assertIn("- Intake File: [[00_Intake/2026-03-11 - Teams - Weekly Sync.md]]", rendered)
        self.assertIn("- Owner Filter: Matthew", rendered)
        self.assertIn("Agenda item", rendered)

    def test_renders_extracted_meeting_front_matter_with_context(self) -> None:
        rendered = render_extracted_meeting_note(
            heading="2026-03-12 - Teams - Platform Sync",
            intake_file=Path("_Archive/Intake/2026-03-12 - Teams - Platform Sync.vtt"),
            extracted={
                "date": "2026-03-12",
                "source": "Teams",
                "title": "Platform Sync",
                "context": "The team reviewed the rollout path.",
                "participants": ["Matthew Rouser", "Ada Lovelace"],
                "attendance_confidence": "confirmed",
                "summary_bullets": ["Reviewed rollout status."],
                "sources_used": ["Teams transcript", "Outlook calendar metadata"],
                "source_limitations": ["Meeting chat was not available."],
            },
            context={
                "organizer": "Ada Lovelace",
                "attendees": ["Matthew Rouser", "Ada Lovelace", "Grace Hopper"],
                "outlook_event_id": "event-123",
                "teams_meeting_id": "meeting-456",
                "transcript_id": "transcript-789",
            },
        )

        self.assertIn('participants: ["Matthew Rouser", "Ada Lovelace"]', rendered)
        self.assertIn('organizer: "Ada Lovelace"', rendered)
        self.assertIn('attendees: ["Matthew Rouser", "Ada Lovelace", "Grace Hopper"]', rendered)
        self.assertIn('outlook_event_id: "event-123"', rendered)
        self.assertIn('teams_meeting_id: "meeting-456"', rendered)
        self.assertIn('transcript_id: "transcript-789"', rendered)
        self.assertIn("## Context\n\nThe team reviewed the rollout path.", rendered)
        self.assertIn("## Participants\n\n- Matthew Rouser\n- Ada Lovelace", rendered)
        self.assertIn("## Source\n\n- Attendance Confidence: confirmed", rendered)
        self.assertIn("- Source Used: Teams transcript", rendered)
        self.assertIn("- Source Used: Outlook calendar metadata", rendered)
        self.assertIn("- Limitation: Meeting chat was not available.", rendered)

    def test_front_matter_escapes_yaml_values(self) -> None:
        rendered = render_meeting_front_matter(
            date="2026-03-12",
            source="Teams",
            title='Planning: "Q2"',
            intake_file=Path("00_Intake/Planning.md"),
            participants=["Matthew: Rouser"],
        )

        self.assertIn('title: "Planning: \\"Q2\\""', rendered)
        self.assertIn('participants: ["Matthew: Rouser"]', rendered)

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
