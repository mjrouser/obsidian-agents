from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from obsidian_intake_agent.processors.md_reader import extract_markdown_action_items, parse_action_text
from obsidian_intake_agent.processors.meeting_processor import MeetingProcessor
from tests.helpers import config as _config


class MeetingProcessorTests(unittest.TestCase):
    def test_processes_markdown_and_updates_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "weekly-sync.md"
            source.write_text(
                "Agenda\n\nAction: Matthew will complete Codex setup by Friday.\n- [ ] Share rollout notes\n",
                encoding="utf-8",
            )

            ts = datetime(2026, 3, 11, 10, 0, 0).timestamp()
            os.utime(source, (ts, ts))

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_all_unprocessed()

            meeting_path = vault / "01_Meetings" / "2026-03-11 - Unknown - weekly-sync.md"
            actions_path = vault / "07_Actions" / "2026-03-09.md"
            archived_path = vault / "_Archive" / "Intake" / "weekly-sync.md"
            self.assertTrue(meeting_path.exists())
            self.assertTrue(actions_path.exists())
            self.assertFalse(source.exists())
            self.assertTrue(archived_path.exists())
            self.assertIn(
                "STATUS: PROCESSED — see [[01_Meetings/2026-03-11 - Unknown - weekly-sync.md]]",
                archived_path.read_text(encoding="utf-8"),
            )
            meeting_text = meeting_path.read_text(encoding="utf-8")
            self.assertIn("# 2026-03-11 - Unknown - weekly-sync", meeting_text)
            self.assertIn("- Intake File: [[_Archive/Intake/weekly-sync.md]]", meeting_text)
            actions_text = actions_path.read_text(encoding="utf-8")
            self.assertIn("# Actions — Week of 2026-03-09", actions_text)
            self.assertIn(
                "- [ ] complete Codex setup by Friday. (Owner: Matthew Rouser) — Source: 2026-03-11 "
                "[[2026-03-11 - Unknown - weekly-sync.md]]",
                actions_text,
            )
            self.assertNotIn("Review action items", actions_text)
            self.assertNotIn("Share rollout notes", actions_text)

    def test_owner_aliases_route_to_canonical_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "weekly-sync.md"
            source.write_text(
                "Action: Matthew Rouser will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )
            ts = datetime(2026, 3, 12, 10, 0, 0).timestamp()
            os.utime(source, (ts, ts))

            processor = MeetingProcessor(
                _config(
                    vault,
                    dry_run=False,
                    action_owner_aliases={
                        "Matthew Rouser": [
                            "Matthew Rouser",
                            "Matthew",
                            "Matt Rouser",
                            "Matt",
                            "matthew.rouser",
                        ]
                    },
                )
            )

            processor.process_all_unprocessed()

            actions_text = (vault / "07_Actions" / "2026-03-09.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] complete Codex setup by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 "
                "[[2026-03-12 - Unknown - weekly-sync.md]]",
                actions_text,
            )

    def test_reprocessing_same_meeting_does_not_duplicate_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-02-28 - Teams - Internal UserTesting Project Closeout and Retro.md"
            source.write_text(
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_file(source)
            actions_path = vault / "07_Actions" / "2026-02-23.md"
            first = actions_path.read_text(encoding="utf-8")
            processor.process_file(vault / "_Archive" / "Intake" / source.name, force=True)
            second = actions_path.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertEqual(second.count("complete Codex setup by Friday"), 1)

    def test_existing_action_variants_do_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            actions_dir = vault / "07_Actions"
            actions_dir.mkdir(parents=True)
            (actions_dir / "2026-02-23.md").write_text(
                "# Actions — Week of 2026-02-23\n\n## Open Actions\n"
                "- [x] complete Codex setup by Friday (Owner: Matthew) - Source: 2026-02-28 "
                "[[2026-02-28 - Teams - Internal UserTesting Project Closeout and Retro.md]].\n",
                encoding="utf-8",
            )
            source = intake_dir / "2026-02-28 - Teams - Internal UserTesting Project Closeout and Retro.md"
            source.write_text(
                "Action: Matthew Rouser will complete   Codex setup by Friday.\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_file(source)

            actions_text = (actions_dir / "2026-02-23.md").read_text(encoding="utf-8")
            self.assertEqual(actions_text.casefold().count("complete codex setup by friday"), 1)

    def test_new_actions_file_uses_this_week_and_longer_term_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-12 - Teams - Platform Sync.md"
            source.write_text("Action: Matthew will ship the update by Friday.\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_all_unprocessed()

            actions_text = (vault / "07_Actions" / "2026-03-09.md").read_text(encoding="utf-8")
            self.assertIn("# Actions — Week of 2026-03-09", actions_text)
            self.assertIn("## This Week", actions_text)
            self.assertIn("## Carry Over Items", actions_text)
            self.assertIn("## Longer-Term / In Progress", actions_text)
            self.assertIn(
                "## This Week\n\n"
                "- [ ] ship the update by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 "
                "[[2026-03-12 - Teams - Platform Sync.md]]\n\n"
                "## Carry Over Items\n\n"
                "## Longer-Term / In Progress",
                actions_text,
            )

    def test_existing_longer_term_section_is_preserved_when_adding_new_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            actions_dir = vault / "07_Actions"
            actions_dir.mkdir(parents=True)
            (actions_dir / "2026-03-09.md").write_text(
                "# Actions — Week of 2026-03-09\n\n"
                "## This Week\n\n"
                "- [ ] Existing item (Owner: Matthew Rouser) — Source: 2026-03-10 [[existing.md]]\n\n"
                "## Carry Over Items\n\n"
                "- [ ] Preserve this carry-over item (Owner: Matthew Rouser) — Source: 2026-03-03 [[carry.md]]\n\n"
                "## Longer-Term / In Progress\n\n"
                "- [ ] Preserve this longer-term item (Owner: Matthew Rouser) — Source: 2026-03-08 [[long.md]]\n",
                encoding="utf-8",
            )
            source = intake_dir / "2026-03-12 - Teams - Platform Sync.md"
            source.write_text("Action: Matthew will ship the update by Friday.\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_all_unprocessed()

            actions_text = (actions_dir / "2026-03-09.md").read_text(encoding="utf-8")
            self.assertIn(
                "## This Week\n\n"
                "- [ ] Existing item (Owner: Matthew Rouser) — Source: 2026-03-10 [[existing.md]]\n"
                "- [ ] ship the update by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 "
                "[[2026-03-12 - Teams - Platform Sync.md]]\n\n"
                "## Carry Over Items",
                actions_text,
            )
            self.assertIn(
                "- [ ] Preserve this carry-over item (Owner: Matthew Rouser) — Source: 2026-03-03 [[carry.md]]",
                actions_text,
            )
            self.assertIn(
                "## Longer-Term / In Progress",
                actions_text,
            )
            self.assertIn(
                "- [ ] Preserve this longer-term item (Owner: Matthew Rouser) — Source: 2026-03-08 [[long.md]]",
                actions_text,
            )

    def test_new_actions_file_carries_forward_unfinished_previous_week_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            actions_dir = vault / "07_Actions"
            actions_dir.mkdir(parents=True)
            (actions_dir / "2026-03-09.md").write_text(
                "# Actions — Week of 2026-03-09\n\n"
                "## This Week\n\n"
                "- [ ] Carry this forward (Owner: Matthew Rouser) — Source: 2026-03-10 [[carry.md]]\n"
                "- [x] Completed item (Owner: Matthew Rouser) — Source: 2026-03-10 [[done.md]]\n\n"
                "## Carry Over Items\n\n"
                "- [ ] Existing carry-over item (Owner: Matthew Rouser) — Source: 2026-03-03 [[older.md]]\n\n"
                "## Longer-Term / In Progress\n\n"
                "- [ ] Longer-term item (Owner: Matthew Rouser) — Source: 2026-03-08 [[long.md]]\n",
                encoding="utf-8",
            )
            source = intake_dir / "2026-03-18 - Teams - Platform Sync.md"
            source.write_text("Action: Matthew will ship the update by Friday.\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_all_unprocessed()

            actions_text = (actions_dir / "2026-03-16.md").read_text(encoding="utf-8")
            self.assertIn(
                "## This Week\n\n"
                "- [ ] ship the update by Friday. (Owner: Matthew Rouser) — Source: 2026-03-18 "
                "[[2026-03-18 - Teams - Platform Sync.md]]\n\n"
                "## Carry Over Items\n\n"
                "- [ ] Carry this forward (Owner: Matthew Rouser) — Source: 2026-03-10 [[carry.md]]\n"
                "- [ ] Existing carry-over item (Owner: Matthew Rouser) — Source: 2026-03-03 [[older.md]]\n"
                "- [ ] Longer-term item (Owner: Matthew Rouser) — Source: 2026-03-08 [[long.md]]\n\n"
                "## Longer-Term / In Progress\n",
                actions_text,
            )
            self.assertNotIn("Completed item", actions_text)

    def test_archives_older_action_notes_after_action_note_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            actions_dir = vault / "07_Actions"
            intake_dir.mkdir(parents=True)
            actions_dir.mkdir(parents=True)
            for name in [
                "2026-05-11.md",
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-20.md",
                "2026-04-13.md",
            ]:
                (actions_dir / name).write_text(f"# Actions {name}\n", encoding="utf-8")
            source = intake_dir / "2026-05-12 - Teams - Sync.md"
            source.write_text("Action: Matthew will follow up with Alex.\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            self.assertFalse((actions_dir / "2026-04-13.md").exists())
            self.assertTrue((actions_dir / "Actions Archive" / "2026-04-13.md").exists())
            self.assertTrue((actions_dir / "2026-05-11.md").exists())

    def test_batch_retention_waits_until_carry_over_sources_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            actions_dir = vault / "07_Actions"
            intake_dir.mkdir(parents=True)
            actions_dir.mkdir(parents=True)
            for name in [
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-14.md",
            ]:
                (actions_dir / name).write_text(f"# Actions — Week of {name.removesuffix('.md')}\n", encoding="utf-8")
            (actions_dir / "2026-04-13.md").write_text(
                "# Actions — Week of 2026-04-13\n\n"
                "## This Week\n\n"
                "- [ ] Preserve this carry-over context (Owner: Matthew Rouser) — Source: 2026-04-13 [[source.md]]\n\n"
                "## Carry Over Items\n\n"
                "## Longer-Term / In Progress\n",
                encoding="utf-8",
            )
            first_source = intake_dir / "a-newer-week.md"
            first_source.write_text("Action: Matthew will update the May plan.\n", encoding="utf-8")
            first_ts = datetime(2026, 5, 12, 10, 0, 0).timestamp()
            os.utime(first_source, (first_ts, first_ts))
            second_source = intake_dir / "b-carry-over-week.md"
            second_source.write_text("Action: Matthew will update the April plan.\n", encoding="utf-8")
            second_ts = datetime(2026, 4, 21, 10, 0, 0).timestamp()
            os.utime(second_source, (second_ts, second_ts))

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_all_unprocessed()

            april_actions_text = (actions_dir / "2026-04-20.md").read_text(encoding="utf-8")
            self.assertIn("Preserve this carry-over context", april_actions_text)
            self.assertFalse((actions_dir / "2026-04-13.md").exists())
            self.assertTrue((actions_dir / "Actions Archive" / "2026-04-13.md").exists())

    def test_dry_run_does_not_archive_older_action_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            actions_dir = vault / "07_Actions"
            intake_dir.mkdir(parents=True)
            actions_dir.mkdir(parents=True)
            for name in [
                "2026-05-11.md",
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-20.md",
                "2026-04-13.md",
            ]:
                (actions_dir / name).write_text(f"# Actions {name}\n", encoding="utf-8")
            source = intake_dir / "2026-05-12 - Teams - Sync.md"
            source.write_text("Action: Matthew will follow up with Alex.\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=True))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            self.assertTrue((actions_dir / "2026-04-13.md").exists())
            self.assertFalse((actions_dir / "Actions Archive").exists())

    def test_validation_mode_action_write_does_not_archive_action_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            production_actions_dir = vault / "07_Actions"
            validation_actions_dir = vault / "99_Test Notes" / "Actions"
            intake_dir.mkdir(parents=True)
            production_actions_dir.mkdir(parents=True)
            validation_actions_dir.mkdir(parents=True)
            action_note_names = [
                "2026-05-11.md",
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-20.md",
                "2026-04-13.md",
            ]
            for name in action_note_names:
                (production_actions_dir / name).write_text(f"# Production Actions {name}\n", encoding="utf-8")
                (validation_actions_dir / name).write_text(f"# Validation Actions {name}\n", encoding="utf-8")
            source = intake_dir / "2026-05-12 - Teams - Sync.md"
            source.write_text("Action: Matthew will follow up with Alex.\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False), output_mode="validation")

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            self.assertTrue((production_actions_dir / "2026-04-13.md").exists())
            self.assertFalse((production_actions_dir / "Actions Archive").exists())
            self.assertTrue((validation_actions_dir / "2026-04-13.md").exists())
            self.assertFalse((validation_actions_dir / "Actions Archive").exists())

    def test_processes_vtt_without_modifying_raw_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-12 - Teams - Platform Sync.vtt"
            raw_vtt = (
                "WEBVTT\n\n"
                "00:00:00.000 --> 00:00:02.000\n"
                "Intro discussion\n\n"
                "00:00:02.000 --> 00:00:04.000\n"
                "Decision: Move forward with rollout\n\n"
                "00:00:04.000 --> 00:00:06.000\n"
                "Risk: Timeline is tight\n\n"
                "00:00:06.000 --> 00:00:08.000\n"
                "Question: Do we need legal review?\n\n"
                "00:00:08.000 --> 00:00:10.000\n"
                "Action: Matthew will complete Codex setup by Friday.\n"
            )
            source.write_text(raw_vtt, encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False, llm_provider="none"))

            summary = processor.process_all_unprocessed()
            archived_vtt_path = vault / "_Archive" / "Intake" / "2026-03-12 - Teams - Platform Sync.vtt"

            self.assertEqual(summary.processed_files, 1)
            self.assertEqual(archived_vtt_path.read_text(encoding="utf-8"), raw_vtt)

            meeting_path = vault / "01_Meetings" / "2026-03-12 - Teams - Platform Sync.md"
            actions_path = vault / "07_Actions" / "2026-03-09.md"
            sidecar_path = intake_dir / "2026-03-12 - Teams - Platform Sync (intake).md"

            self.assertTrue(meeting_path.exists())
            self.assertTrue(actions_path.exists())
            self.assertTrue(sidecar_path.exists())
            self.assertFalse(source.exists())
            self.assertTrue(archived_vtt_path.exists())

            meeting_text = meeting_path.read_text(encoding="utf-8")
            self.assertTrue(meeting_text.startswith("---\n"))
            self.assertIn('date: "2026-03-12"', meeting_text)
            self.assertIn('source: "Teams"', meeting_text)
            self.assertIn('title: "Platform Sync"', meeting_text)
            self.assertIn("participants: []", meeting_text)
            self.assertIn('intake_file: "_Archive/Intake/2026-03-12 - Teams - Platform Sync.vtt"', meeting_text)
            self.assertIn("# 2026-03-12 - Teams - Platform Sync", meeting_text)
            self.assertIn(
                "- Intake File: [[_Archive/Intake/2026-03-12 - Teams - Platform Sync.vtt]]",
                meeting_text,
            )
            self.assertIn("## Decisions", meeting_text)
            self.assertIn("Move forward with rollout", meeting_text)
            self.assertIn("## Risks", meeting_text)
            self.assertIn("Timeline is tight", meeting_text)
            self.assertIn("## Open Questions", meeting_text)
            self.assertIn("Do we need legal review?", meeting_text)

            sidecar_text = sidecar_path.read_text(encoding="utf-8")
            self.assertIn("STATUS: PROCESSED — see [[2026-03-12 - Teams - Platform Sync.md]]", sidecar_text)
            self.assertIn(
                "[../_Archive/Intake/2026-03-12 - Teams - Platform Sync.vtt]"
                "(../_Archive/Intake/2026-03-12 - Teams - Platform Sync.vtt)",
                sidecar_text,
            )

            actions_text = actions_path.read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] complete Codex setup by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 "
                "[[2026-03-12 - Teams - Platform Sync.md]]",
                actions_text,
            )

    def test_process_vtt_file_validation_mode_writes_meeting_note_under_test_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            source = vault / "00_Intake" / "2026-03-12 - Teams - weekly-sync.vtt"
            source.parent.mkdir(parents=True)
            source.write_text(
                "WEBVTT\n\n00:00.000 --> 00:02.000\nAction: Matthew to ship recap\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False, llm_provider="none"), output_mode="validation")

            result = processor.process_file(source)

            expected_note = vault / "99_Test Notes" / "Meetings" / "2026-03-12 - Teams - weekly-sync.md"
            self.assertTrue(result.processed)
            self.assertEqual(result.canonical_note_path, expected_note)
            self.assertTrue(expected_note.exists())
            self.assertFalse((vault / "01_Meetings" / "2026-03-12 - Teams - weekly-sync.md").exists())
            self.assertIn("validation_mode: true", expected_note.read_text(encoding="utf-8"))

    def test_process_markdown_validation_mode_routes_actions_to_test_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            source = vault / "00_Intake" / "2026-03-12 - Unknown - weekly-sync.md"
            source.parent.mkdir(parents=True)
            source.write_text("# Notes\nAction: Matthew to review action items\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False), output_mode="validation")

            result = processor.process_file(source)

            expected_actions = vault / "99_Test Notes" / "Actions" / "2026-03-09.md"
            self.assertEqual(result.actions_file_path, expected_actions)
            self.assertTrue(expected_actions.exists())
            self.assertFalse((vault / "07_Actions" / "2026-03-09.md").exists())

    def test_meeting_date_2026_03_12_rolls_to_monday_2026_03_09(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-12 - Teams - Platform Sync.md"
            source.write_text("Action: Matthew will ship the update.\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_all_unprocessed()

            actions_path = vault / "07_Actions" / "2026-03-09.md"
            self.assertTrue(actions_path.exists())
            self.assertIn("# Actions — Week of 2026-03-09", actions_path.read_text(encoding="utf-8"))

    def test_dry_run_prints_planned_changes_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "weekly-sync.md"
            source.write_text("Agenda\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=True))

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                processor.process_file(source)

            output = stdout.getvalue()
            self.assertIn("DRY RUN — would write:", output)
            self.assertNotIn("DRY RUN — would update:", output)
            self.assertIn("DRY RUN — would prepend processed marker", output)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertNotIn("STATUS: PROCESSED", source.read_text(encoding="utf-8"))

    def test_dry_run_override_does_not_create_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "weekly-sync.md"
            source.write_text("Action: Matthew will complete Codex setup by Friday.\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = processor.process_file(source, dry_run=True)

            self.assertTrue(result.processed)
            self.assertIn("DRY RUN — would write:", stdout.getvalue())
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())
            self.assertNotIn("STATUS: PROCESSED", source.read_text(encoding="utf-8"))

    def test_dry_run_force_reprocess_does_not_duplicate_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            processed_note = intake_dir / "weekly-sync.md"
            processed_note.write_text(
                "STATUS: PROCESSED — see [[01_Meetings/old-note.md]]\n"
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False))

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = processor.process_file(processed_note, force=True, dry_run=True)

            self.assertTrue(result.processed)
            self.assertIn("Reprocessing (forced)", stdout.getvalue())
            self.assertIn("DRY RUN — would prepend processed marker", stdout.getvalue())
            updated_text = processed_note.read_text(encoding="utf-8")
            self.assertEqual(updated_text.count("STATUS: PROCESSED"), 1)
            self.assertIn("STATUS: PROCESSED — see [[01_Meetings/old-note.md]]", updated_text)

    def test_reprocessing_meeting_note_keeps_single_intake_wikilink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Raw Transcripts"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-12 - Teams - Platform Sync.md"
            source.write_text("Action: Matthew will ship the update.\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_file(source)
            processor.process_file(vault / "_Archive" / "Intake" / "Raw Transcripts" / source.name, force=True)

            meeting_text = (vault / "01_Meetings" / "2026-03-12 - Teams - Platform Sync.md").read_text(encoding="utf-8")
            self.assertEqual(
                meeting_text.count("[[_Archive/Intake/Raw Transcripts/2026-03-12 - Teams - Platform Sync.md]]"),
                1,
            )

    def test_extracts_action_lines_with_owner(self) -> None:
        items = extract_markdown_action_items(
            "Action: Matthew will complete Codex setup by Friday.\naction: Alex will send the notes.\n"
        )

        self.assertEqual(items[0].owner, "Matthew")
        self.assertEqual(items[0].text, "complete Codex setup by Friday.")
        self.assertEqual(items[1].owner, "Alex")
        self.assertEqual(items[1].text, "send the notes.")

    def test_extracts_owner_to_action_lines(self) -> None:
        items = extract_markdown_action_items(
            "Action: Matthew to draft the SOW\nAction: Matt to send the deck by Friday\nAction: Daniel to share links\n"
        )

        self.assertEqual(items[0].owner, "Matthew")
        self.assertEqual(items[0].text, "draft the SOW")
        self.assertEqual(items[1].owner, "Matt")
        self.assertEqual(items[1].text, "send the deck by Friday")
        self.assertEqual(items[2].owner, "Daniel")
        self.assertEqual(items[2].text, "share links")

    def test_extracts_owner_label_action_lines(self) -> None:
        items = extract_markdown_action_items(
            "Action: Matthew: draft SOW for Louisiana Pacific\n"
            "Action: Matt - follow up with Brian\n"
            "Action: Assigned to Matthew: draft SOW\n"
            "Action: Owner: Matthew - follow up with legal\n"
        )

        self.assertEqual(items[0].owner, "Matthew")
        self.assertEqual(items[0].text, "draft SOW for Louisiana Pacific")
        self.assertEqual(items[1].owner, "Matt")
        self.assertEqual(items[1].text, "follow up with Brian")
        self.assertEqual(items[2].owner, "Matthew")
        self.assertEqual(items[2].text, "draft SOW")
        self.assertEqual(items[3].owner, "Matthew")
        self.assertEqual(items[3].text, "follow up with legal")

    def test_extracts_checkbox_actions_as_unassigned(self) -> None:
        items = extract_markdown_action_items("- [ ] Share rollout notes\n- [ ] Matthew should review docs\n")

        self.assertIsNone(items[0].owner)
        self.assertEqual(items[0].text, "Share rollout notes")
        self.assertIsNone(items[1].owner)
        self.assertEqual(items[1].text, "Matthew should review docs")

    def test_extracts_action_section_bullets_with_owner(self) -> None:
        items = extract_markdown_action_items(
            "### Action Items and Next Steps\n\n"
            "- Davis will refine the executive summary.\n"
            "- Matthew will focus on the model section.\n"
            "### Additional Notes\n"
            "- Matthew discussed prior collateral.\n"
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].owner, "Davis")
        self.assertEqual(items[1].owner, "Matthew")
        self.assertEqual(items[1].text, "focus on the model section.")

    def test_extracts_parenthetical_owner_task_in_action_section(self) -> None:
        items = extract_markdown_action_items(
            "### Action Items and Next Steps\n\n"
            "- Draft SOW for Louisiana Pacific (Matthew)\n"
            "- Send revised proposal (Matt)\n"
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].owner, "Matthew")
        self.assertEqual(items[0].text, "Draft SOW for Louisiana Pacific")
        self.assertEqual(items[1].owner, "Matt")
        self.assertEqual(items[1].text, "Send revised proposal")

    def test_action_section_bullet_parenthetical_owner_does_not_keep_leading_dash(self) -> None:
        items = extract_markdown_action_items("### Action Items and Next Steps\n\n- Draft SOW (Matthew)\n")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].owner, "Matthew")
        self.assertEqual(items[0].text, "Draft SOW")
        self.assertFalse(items[0].text.startswith("-"))

    def test_extracts_standalone_parenthetical_owner_line(self) -> None:
        parsed = parse_action_text("Draft SOW (Matthew)")
        self.assertEqual(parsed.owner, "Matthew")
        self.assertEqual(parsed.text, "Draft SOW")

        items = extract_markdown_action_items("Draft SOW (Matthew)\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].owner, "Matthew")
        self.assertEqual(items[0].text, "Draft SOW")

    def test_extracts_standalone_explicit_assignment_formats(self) -> None:
        items = extract_markdown_action_items(
            "Matthew to draft the SOW\nMatthew: draft the SOW\nAssigned to Matthew: draft the SOW\n"
        )

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].owner, "Matthew")
        self.assertEqual(items[0].text, "draft the SOW")
        self.assertEqual(items[1].owner, "Matthew")
        self.assertEqual(items[1].text, "draft the SOW")
        self.assertEqual(items[2].owner, "Matthew")
        self.assertEqual(items[2].text, "draft the SOW")

    def test_extracts_explicit_non_checkbox_bullet_outside_action_section(self) -> None:
        items = extract_markdown_action_items("- Matt to review changes Raymond made to the staffing model\n")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].owner, "Matt")
        self.assertEqual(items[0].text, "review changes Raymond made to the staffing model")

    def test_extracts_coordinated_owner_to_action_lines(self) -> None:
        items = extract_markdown_action_items(
            "Jeremy and Bruno to confirm Afreen’s assignment to Resi and update notes accordingly.\n"
            "Julie and Jeremy to discuss Senski’s resource needs and possible halftime arrangement.\n"
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].owner, "Jeremy and Bruno")
        self.assertEqual(items[0].text, "confirm Afreen’s assignment to Resi and update notes accordingly.")
        self.assertEqual(items[1].owner, "Julie and Jeremy")
        self.assertEqual(items[1].text, "discuss Senski’s resource needs and possible halftime arrangement.")

    def test_extracts_trailing_owner_will_clause_without_false_owner(self) -> None:
        items = extract_markdown_action_items(
            "### Action Items and Next Steps\n\n"
            "- Daniel requested all people leaders to encourage their teams to complete Workday training "
            "and the AI survey by the required date; Daniel will share links.\n"
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].owner, "Daniel")
        self.assertEqual(items[0].text, "share links.")

    def test_does_not_extract_ambiguous_or_non_owner_formats(self) -> None:
        items = extract_markdown_action_items(
            "Action: Draft SOW (v2)\n"
            "Action: Pricing review (high priority)\n"
            "Action: Notes: Louisiana Pacific discussion\n"
            "Action: Context: Matthew mentioned the deck\n"
            "Action: Matthew should draft the SOW\n"
            "Action: We need Matthew to draft the SOW\n"
            "Action: Please send the deck, Matthew\n"
            "Ordinary prose sentence that should not become an action.\n"
        )

        self.assertIsNone(items[0].owner)
        self.assertEqual(items[0].text, "Draft SOW (v2)")
        self.assertIsNone(items[1].owner)
        self.assertEqual(items[1].text, "Pricing review (high priority)")
        self.assertIsNone(items[2].owner)
        self.assertEqual(items[2].text, "Notes: Louisiana Pacific discussion")
        self.assertIsNone(items[3].owner)
        self.assertEqual(items[3].text, "Context: Matthew mentioned the deck")
        self.assertIsNone(items[4].owner)
        self.assertEqual(items[4].text, "Matthew should draft the SOW")
        self.assertIsNone(items[5].owner)
        self.assertEqual(items[5].text, "We need Matthew to draft the SOW")
        self.assertIsNone(items[6].owner)
        self.assertEqual(items[6].text, "Please send the deck, Matthew")

    def test_does_not_extract_ambiguous_standalone_formats(self) -> None:
        items = extract_markdown_action_items(
            "Draft SOW (v2)\nNotes: Louisiana Pacific discussion\nMatthew should draft the SOW\n"
        )

        self.assertEqual(items, [])

    def test_does_not_extract_non_action_coordinated_owner_lines(self) -> None:
        items = extract_markdown_action_items(
            "Jeremy and Bruno discussed staffing.\n"
            "Jeremy and Bruno should confirm staffing.\n"
            "Jeremy and Bruno were on the call.\n"
        )

        self.assertEqual(items, [])

    def test_does_not_extract_vague_non_checkbox_bullets(self) -> None:
        items = extract_markdown_action_items(
            "- Matt should review changes\n- Notes about staffing model\n- Discussion of Raymond’s edits\n"
        )

        self.assertEqual(items, [])

    def test_includes_unassigned_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "weekly-sync.md"
            source.write_text("- [ ] Share rollout notes\n", encoding="utf-8")
            ts = datetime(2026, 3, 11, 10, 0, 0).timestamp()
            os.utime(source, (ts, ts))

            processor = MeetingProcessor(_config(vault, dry_run=False, include_unassigned=True))

            processor.process_all_unprocessed()

            actions_text = (vault / "07_Actions" / "2026-03-09.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] Share rollout notes — Source: 2026-03-11 [[2026-03-11 - Unknown - weekly-sync.md]]",
                actions_text,
            )

    def test_markdown_action_section_routes_matthew_owned_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Raw Transcripts"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-18 - Teams - Slalom Lower Cost Delivery Model.md"
            source.write_text(
                "### Action Items and Next Steps\n\n- Matthew will focus on the model section.\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            actions_text = (vault / "07_Actions" / "2026-03-16.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] focus on the model section. (Owner: Matthew Rouser) — Source: 2026-03-18 "
                "[[2026-03-18 - Teams - Slalom Lower Cost Delivery Model.md]]",
                actions_text,
            )

    def test_markdown_parenthetical_owner_routes_matthew_owned_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-16 - Teams - E&O Weekly Practice Sync.md"
            source.write_text(
                "### Action Items and Next Steps\n\n- Draft SOW for Louisiana Pacific (Matthew)\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            actions_text = (vault / "07_Actions" / "2026-03-16.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] Draft SOW for Louisiana Pacific (Owner: Matthew Rouser) — Source: 2026-03-16 "
                "[[2026-03-16 - Teams - E&O Weekly Practice Sync.md]]",
                actions_text,
            )

    def test_markdown_coordinated_and_parenthetical_actions_only_route_matthew(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-16 - Teams - E&O Weekly Practice Sync.md"
            source.write_text(
                "### Action Items and Next Steps\n\n"
                "- Jeremy and Bruno to confirm Afreen’s assignment to Resi and update notes accordingly.\n"
                "- Julie and Jeremy to discuss Senski’s resource needs and possible halftime arrangement.\n"
                "- Draft SOW for Louisiana Pacific (Matthew)\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            actions_text = (vault / "07_Actions" / "2026-03-16.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] Draft SOW for Louisiana Pacific (Owner: Matthew Rouser) — Source: 2026-03-16 "
                "[[2026-03-16 - Teams - E&O Weekly Practice Sync.md]]",
                actions_text,
            )
            self.assertNotIn("Afreen’s assignment to Resi", actions_text)
            self.assertNotIn("Senski’s resource needs", actions_text)

            processor.process_file(vault / "_Archive" / "Intake" / source.name, force=True)
            actions_text_again = (vault / "07_Actions" / "2026-03-16.md").read_text(encoding="utf-8")
            self.assertEqual(actions_text_again, actions_text)

    def test_non_checkbox_bullet_outside_action_section_routes_matthew_owned_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-16 - Teams - TDA CRM and GMS SOW Check-in.md"
            source.write_text(
                "### Meeting Recap\n\n- Matt to review changes Raymond made to the staffing model\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            actions_text = (vault / "07_Actions" / "2026-03-16.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] review changes Raymond made to the staffing model (Owner: Matthew Rouser) — Source: "
                "2026-03-16 [[2026-03-16 - Teams - TDA CRM and GMS SOW Check-in.md]]",
                actions_text,
            )

    def test_vtt_heuristic_extracts_owner_will_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-12 - Teams - Platform Sync.vtt"
            source.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nMatthew will ship the update by Friday.\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False, llm_provider="none"))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            actions_text = (vault / "07_Actions" / "2026-03-09.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] ship the update by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 "
                "[[2026-03-12 - Teams - Platform Sync.md]]",
                actions_text,
            )

    def test_no_weekly_actions_file_is_created_when_no_actions_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "weekly-sync.md"
            source.write_text("Agenda only\n", encoding="utf-8")
            ts = datetime(2026, 3, 11, 10, 0, 0).timestamp()
            os.utime(source, (ts, ts))

            processor = MeetingProcessor(_config(vault, dry_run=False))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            self.assertIsNone(result.actions_file_path)
            self.assertFalse((vault / "07_Actions" / "2026-03-09.md").exists())


if __name__ == "__main__":
    unittest.main()
