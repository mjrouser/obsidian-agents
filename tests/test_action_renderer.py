from __future__ import annotations

import unittest
from datetime import date

from obsidian_intake_agent.rendering.action_renderer import (
    ActionRecord,
    normalize_action_for_key,
    parse_existing_actions,
    parse_incomplete_actions,
    render_actions_note,
)


class ActionRendererTests(unittest.TestCase):
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
        self.assertIn("## Carry Over Items", rendered)
        self.assertIn("## Longer-Term / In Progress", rendered)
        self.assertIn("Complete Codex setup by Friday.", rendered)
        self.assertIn("[[2026-03-11 - Teams - Weekly Sync.md]]", rendered)
        self.assertIn("(Owner: Matthew Rouser)", rendered)

    def test_updates_existing_two_section_actions_note_without_touching_longer_term(self) -> None:
        existing = (
            "# Actions — Week of 2026-03-09\n\n"
            "## This Week\n\n"
            "- [ ] Existing this week item (Owner: Matthew Rouser) — Source: 2026-03-10 [[existing.md]]\n\n"
            "## Carry Over Items\n\n"
            "- [ ] Existing carry-over item (Owner: Matthew Rouser) — Source: 2026-03-03 [[carry.md]]\n\n"
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
            "## Carry Over Items",
            rendered,
        )
        self.assertIn(
            "## Carry Over Items\n\n"
            "- [ ] Existing carry-over item (Owner: Matthew Rouser) — Source: 2026-03-03 [[carry.md]]\n\n"
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
            "## Carry Over Items\n\n"
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

    def test_new_actions_note_places_carry_over_between_this_week_and_longer_term(self) -> None:
        rendered = render_actions_note(
            monday=date(2026, 3, 16),
            action_records=[
                ActionRecord(
                    text="New this week item",
                    owner="Matthew Rouser",
                    source_date="2026-03-18",
                    source_note="2026-03-18 - Teams - Weekly Sync.md",
                )
            ],
            carry_over_records=[
                ActionRecord(
                    text="Carry this forward",
                    owner="Matthew Rouser",
                    source_date="2026-03-11",
                    source_note="2026-03-11 - Teams - Weekly Sync.md",
                )
            ],
            owner_aliases={"Matthew Rouser": ["Matthew", "Matt"]},
        )
        self.assertIn(
            "## This Week\n\n"
            "- [ ] New this week item (Owner: Matthew Rouser) — Source: 2026-03-18 "
            "[[2026-03-18 - Teams - Weekly Sync.md]]\n\n"
            "## Carry Over Items\n\n"
            "- [ ] Carry this forward (Owner: Matthew Rouser) — Source: 2026-03-11 "
            "[[2026-03-11 - Teams - Weekly Sync.md]]\n\n"
            "## Longer-Term / In Progress\n",
            rendered,
        )

    def test_parse_incomplete_actions_ignores_completed_items(self) -> None:
        existing = (
            "- [ ] Carry this forward (Owner: Matthew Rouser) — Source: 2026-03-10 [[carry.md]]\n"
            "- [x] Done item (Owner: Matthew Rouser) — Source: 2026-03-09 [[done.md]]\n"
        )
        records = parse_incomplete_actions(existing, {"Matthew Rouser": ["Matthew", "Matt"]})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].text, "Carry this forward")


if __name__ == "__main__":
    unittest.main()
