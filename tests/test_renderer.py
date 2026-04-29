from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from obsidian_intake_agent.rendering.meeting_renderer import (
    ActionRecord,
    format_obsidian_link,
    normalize_action_for_key,
    parse_existing_actions,
    render_actions_note,
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

    def test_renders_actions_note_with_link(self) -> None:
        rendered = render_actions_note(
            monday=date(2026, 3, 9),
            action_records=[
                ActionRecord(
                    text="Complete Codex setup by Friday.",
                    owner="Matthew Rouser",
                    source_date="2026-03-11",
                    source_note="2026-03-11 - Teams - Weekly Sync.md",
                )
            ],
            owner_aliases={"Matthew Rouser": ["Matthew", "Matt"]},
        )
        self.assertIn("# Actions — Week of 2026-03-09", rendered)
        self.assertIn("## This Week", rendered)
        self.assertIn("## Longer-Term / In Progress", rendered)
        self.assertIn("Complete Codex setup by Friday.", rendered)
        self.assertIn("[[2026-03-11 - Teams - Weekly Sync.md]]", rendered)
        self.assertIn("(Owner: Matthew Rouser)", rendered)

    def test_updates_existing_two_section_actions_note_without_touching_longer_term(self) -> None:
        existing = (
            "# Actions — Week of 2026-03-09\n\n"
            "## This Week\n\n"
            "- [ ] Existing this week item (Owner: Matthew Rouser) — Source: 2026-03-10 [[existing.md]]\n\n"
            "## Longer-Term / In Progress\n\n"
            "- [ ] Longer-term item (Owner: Matthew Rouser) — Source: 2026-03-08 [[long.md]]\n"
        )
        rendered = render_actions_note(
            monday=date(2026, 3, 9),
            action_records=[
                ActionRecord(
                    text="New this week item",
                    owner="Matthew Rouser",
                    source_date="2026-03-11",
                    source_note="2026-03-11 - Teams - Weekly Sync.md",
                )
            ],
            owner_aliases={"Matthew Rouser": ["Matthew", "Matt"]},
            existing_text=existing,
        )
        self.assertIn(
            "## This Week\n\n"
            "- [ ] Existing this week item (Owner: Matthew Rouser) — Source: 2026-03-10 [[existing.md]]\n"
            "- [ ] New this week item (Owner: Matthew Rouser) — Source: 2026-03-11 "
            "[[2026-03-11 - Teams - Weekly Sync.md]]\n\n"
            "## Longer-Term / In Progress",
            rendered,
        )
        self.assertIn(
            "## Longer-Term / In Progress\n\n"
            "- [ ] Longer-term item (Owner: Matthew Rouser) — Source: 2026-03-08 [[long.md]]\n",
            rendered,
        )

    def test_migrates_old_single_list_actions_note_into_this_week_section(self) -> None:
        existing = (
            "# Actions — Week of 2026-03-09\n\n"
            "## Open Actions\n\n"
            "- [ ] Existing item (Owner: Matthew Rouser) — Source: 2026-03-10 [[existing.md]]\n"
        )
        rendered = render_actions_note(
            monday=date(2026, 3, 9),
            action_records=[
                ActionRecord(
                    text="New migrated item",
                    owner="Matthew Rouser",
                    source_date="2026-03-11",
                    source_note="2026-03-11 - Teams - Weekly Sync.md",
                )
            ],
            owner_aliases={"Matthew Rouser": ["Matthew", "Matt"]},
            existing_text=existing,
        )
        self.assertNotIn("## Open Actions", rendered)
        self.assertIn(
            "## This Week\n\n"
            "- [ ] Existing item (Owner: Matthew Rouser) — Source: 2026-03-10 [[existing.md]]\n"
            "- [ ] New migrated item (Owner: Matthew Rouser) — Source: 2026-03-11 "
            "[[2026-03-11 - Teams - Weekly Sync.md]]\n\n"
            "## Longer-Term / In Progress\n",
            rendered,
        )

    def test_render_actions_note_is_idempotent_across_reruns(self) -> None:
        action_record = ActionRecord(
            text="Complete Codex setup by Friday.",
            owner="Matthew Rouser",
            source_date="2026-03-11",
            source_note="2026-03-11 - Teams - Weekly Sync.md",
        )
        first = render_actions_note(
            monday=date(2026, 3, 9),
            action_records=[action_record],
            owner_aliases={"Matthew Rouser": ["Matthew", "Matt"]},
        )
        second = render_actions_note(
            monday=date(2026, 3, 9),
            action_records=[action_record],
            owner_aliases={"Matthew Rouser": ["Matthew", "Matt"]},
            existing_text=first,
        )
        self.assertEqual(second, first)

    def test_parse_existing_actions_and_normalize_key_dedupes_variants(self) -> None:
        existing = (
            "- [x] Complete Codex setup by Friday (Owner: Matthew) - Source: 2026-02-28 "
            "[[2026-02-28 - Teams - Internal UserTesting Project Closeout and Retro.md]].\n"
        )
        records = parse_existing_actions(
            existing,
            owner_aliases={"Matthew Rouser": ["Matthew", "Matt"]},
        )
        new_record = ActionRecord(
            text="Complete Codex setup by Friday.",
            owner="Matthew Rouser",
            source_date="2026-02-28",
            source_note="2026-02-28 - Teams - Internal UserTesting Project Closeout and Retro.md",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(
            normalize_action_for_key(records[0], {"Matthew Rouser": ["Matthew", "Matt"]}),
            normalize_action_for_key(new_record, {"Matthew Rouser": ["Matthew", "Matt"]}),
        )


if __name__ == "__main__":
    unittest.main()
