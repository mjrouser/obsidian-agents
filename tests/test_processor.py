from __future__ import annotations

from datetime import datetime
from pathlib import Path
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from obsidian_intake_agent.processors.meeting_processor import (
    Config,
    MeetingProcessor,
    normalize_meeting_metadata,
)
from obsidian_intake_agent.processors.md_reader import extract_markdown_action_items


class MeetingProcessorTests(unittest.TestCase):
    def test_skips_inbox_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            inbox = intake_dir / "INBOX.md"
            inbox.write_text("Inbox contents\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))

            summary = processor.process_all_unprocessed()

            self.assertEqual(summary.processed_files, 0)
            self.assertEqual(summary.skipped_files, 1)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())

    def test_skips_placeholder_template_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            template_note = intake_dir / "YYYY-MM-DD - Teams - <meeting title>.md"
            template_note.write_text("Template contents\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))

            summary = processor.process_all_unprocessed()

            self.assertEqual(summary.processed_files, 0)
            self.assertEqual(summary.skipped_files, 1)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())

    def test_skips_already_processed_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            processed_note = intake_dir / "weekly-sync.md"
            processed_note.write_text(
                "STATUS: PROCESSED — see [[01_Meetings/2026-03-11 - Unknown - weekly-sync.md]]\nBody\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False))

            summary = processor.process_all_unprocessed()

            self.assertEqual(summary.processed_files, 0)
            self.assertEqual(summary.skipped_files, 1)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())

    def test_force_reprocesses_already_processed_file(self) -> None:
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
            ts = datetime(2026, 3, 12, 10, 0, 0).timestamp()
            os.utime(processed_note, (ts, ts))

            processor = MeetingProcessor(_config(vault, dry_run=False))

            result = processor.process_file(processed_note, force=True)

            self.assertTrue(result.processed)
            self.assertTrue((vault / "01_Meetings" / "2026-03-12 - Unknown - weekly-sync.md").exists())
            actions_text = (vault / "07_Actions" / "2026-03-09.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] complete Codex setup by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 [[2026-03-12 - Unknown - weekly-sync.md]]",
                actions_text,
            )

    def test_force_reprocess_keeps_single_processed_marker(self) -> None:
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
            ts = datetime(2026, 3, 12, 10, 0, 0).timestamp()
            os.utime(processed_note, (ts, ts))

            processor = MeetingProcessor(_config(vault, dry_run=False))

            processor.process_file(processed_note, force=True)

            updated_text = processed_note.read_text(encoding="utf-8")
            self.assertEqual(updated_text.count("STATUS: PROCESSED"), 1)
            self.assertIn(
                "STATUS: PROCESSED — see [[01_Meetings/2026-03-12 - Unknown - weekly-sync.md]]",
                updated_text,
            )

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
            self.assertTrue(meeting_path.exists())
            self.assertTrue(actions_path.exists())
            self.assertIn(
                "STATUS: PROCESSED — see [[01_Meetings/2026-03-11 - Unknown - weekly-sync.md]]",
                source.read_text(encoding="utf-8"),
            )
            meeting_text = meeting_path.read_text(encoding="utf-8")
            self.assertIn("# 2026-03-11 - Unknown - weekly-sync", meeting_text)
            self.assertIn("- Intake File: [[00_Intake/weekly-sync.md]]", meeting_text)
            actions_text = actions_path.read_text(encoding="utf-8")
            self.assertIn("# Actions — Week of 2026-03-09", actions_text)
            self.assertIn(
                "- [ ] complete Codex setup by Friday. (Owner: Matthew Rouser) — Source: 2026-03-11 [[2026-03-11 - Unknown - weekly-sync.md]]",
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
                "- [ ] complete Codex setup by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 [[2026-03-12 - Unknown - weekly-sync.md]]",
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
            processor.process_file(source, force=True)
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

            self.assertEqual(summary.processed_files, 1)
            self.assertEqual(source.read_text(encoding="utf-8"), raw_vtt)

            meeting_path = vault / "01_Meetings" / "2026-03-12 - Teams - Platform Sync.md"
            actions_path = vault / "07_Actions" / "2026-03-09.md"
            sidecar_path = intake_dir / "2026-03-12 - Teams - Platform Sync (intake).md"

            self.assertTrue(meeting_path.exists())
            self.assertTrue(actions_path.exists())
            self.assertTrue(sidecar_path.exists())

            meeting_text = meeting_path.read_text(encoding="utf-8")
            self.assertIn("# 2026-03-12 - Teams - Platform Sync", meeting_text)
            self.assertIn("- Intake File: [[00_Intake/2026-03-12 - Teams - Platform Sync.vtt]]", meeting_text)
            self.assertIn("## Decisions", meeting_text)
            self.assertIn("Move forward with rollout", meeting_text)
            self.assertIn("## Risks", meeting_text)
            self.assertIn("Timeline is tight", meeting_text)
            self.assertIn("## Open Questions", meeting_text)
            self.assertIn("Do we need legal review?", meeting_text)

            sidecar_text = sidecar_path.read_text(encoding="utf-8")
            self.assertIn("STATUS: PROCESSED — see [[2026-03-12 - Teams - Platform Sync.md]]", sidecar_text)
            self.assertIn("[2026-03-12 - Teams - Platform Sync.vtt](2026-03-12 - Teams - Platform Sync.vtt)", sidecar_text)

            actions_text = actions_path.read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] complete Codex setup by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 [[2026-03-12 - Teams - Platform Sync.md]]",
                actions_text,
            )

    def test_processed_vtt_is_skipped_on_second_run_via_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-12 - Teams - Platform Sync.vtt"
            source.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nAction: Matthew will ship it.\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False, llm_provider="none"))
            first_summary = processor.process_all_unprocessed()
            second_summary = processor.process_all_unprocessed()

            self.assertEqual(first_summary.processed_files, 1)
            self.assertEqual(second_summary.processed_files, 0)
            self.assertEqual(second_summary.skipped_files, 2)

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

    def test_normalize_meeting_metadata_uses_filename_date_and_source(self) -> None:
        path = Path("/tmp/2026-03-12 - Teams - Platform Sync.md")

        metadata = normalize_meeting_metadata(path)

        self.assertEqual(metadata.date, "2026-03-12")
        self.assertEqual(metadata.source, "Teams")
        self.assertEqual(metadata.title, "Platform Sync")
        self.assertEqual(
            metadata.canonical_basename,
            "2026-03-12 - Teams - Platform Sync.md",
        )

    def test_normalize_meeting_metadata_detects_copilot(self) -> None:
        path = Path("/tmp/2026-03-12 - Copilot - Project Review.md")

        metadata = normalize_meeting_metadata(path)

        self.assertEqual(metadata.date, "2026-03-12")
        self.assertEqual(metadata.source, "Copilot")
        self.assertEqual(metadata.title, "Project Review")

    def test_normalize_meeting_metadata_falls_back_to_mtime_for_unknown_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "weekly-sync.md"
            path.write_text("Agenda\n", encoding="utf-8")
            ts = datetime(2026, 3, 12, 9, 0, 0).timestamp()
            os.utime(path, (ts, ts))

            metadata = normalize_meeting_metadata(path)

            self.assertEqual(metadata.date, "2026-03-12")
            self.assertEqual(metadata.source, "Unknown")
            self.assertEqual(metadata.title, "weekly-sync")
            self.assertEqual(
                metadata.canonical_basename,
                "2026-03-12 - Unknown - weekly-sync.md",
            )

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
            processor.process_file(source, force=True)

            meeting_text = (
                vault / "01_Meetings" / "2026-03-12 - Teams - Platform Sync.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(
                meeting_text.count("[[00_Intake/Raw Transcripts/2026-03-12 - Teams - Platform Sync.md]]"),
                1,
            )

    def test_extracts_action_lines_with_owner(self) -> None:
        items = extract_markdown_action_items(
            "Action: Matthew will complete Codex setup by Friday.\n"
            "action: Alex will send the notes.\n"
        )

        self.assertEqual(items[0].owner, "Matthew")
        self.assertEqual(items[0].text, "complete Codex setup by Friday.")
        self.assertEqual(items[1].owner, "Alex")
        self.assertEqual(items[1].text, "send the notes.")

    def test_extracts_checkbox_actions_as_unassigned(self) -> None:
        items = extract_markdown_action_items(
            "- [ ] Share rollout notes\n"
            "- [ ] Matthew should review docs\n"
        )

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
                "### Action Items and Next Steps\n\n"
                "- Matthew will focus on the model section.\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            actions_text = (vault / "07_Actions" / "2026-03-16.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] focus on the model section. (Owner: Matthew Rouser) — Source: 2026-03-18 [[2026-03-18 - Teams - Slalom Lower Cost Delivery Model.md]]",
                actions_text,
            )

    def test_vtt_heuristic_extracts_owner_will_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            source = intake_dir / "2026-03-12 - Teams - Platform Sync.vtt"
            source.write_text(
                "WEBVTT\n\n"
                "00:00:00.000 --> 00:00:02.000\n"
                "Matthew will ship the update by Friday.\n",
                encoding="utf-8",
            )

            processor = MeetingProcessor(_config(vault, dry_run=False, llm_provider="none"))

            result = processor.process_file(source)

            self.assertTrue(result.processed)
            actions_text = (vault / "07_Actions" / "2026-03-09.md").read_text(encoding="utf-8")
            self.assertIn(
                "- [ ] ship the update by Friday. (Owner: Matthew Rouser) — Source: 2026-03-12 [[2026-03-12 - Teams - Platform Sync.md]]",
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


def _config(
    vault: Path,
    *,
    dry_run: bool,
    include_unassigned: bool = False,
    llm_provider: str = "codex_cli",
    action_owner_aliases: dict[str, list[str]] | None = None,
) -> Config:
    return Config(
        vault_path=vault,
        intake_dir="00_Intake",
        meetings_dir="01_Meetings",
        actions_dir="07_Actions",
        archive_intake_dir="_Archive/Intake",
        templates_dir="Templates",
        owner_filter="Matthew",
        dry_run=dry_run,
        include_unassigned=include_unassigned,
        llm_provider=llm_provider,
        action_owner_aliases=action_owner_aliases
        or {
            "Matthew Rouser": [
                "Matthew Rouser",
                "Matthew",
                "Matt Rouser",
                "Matt",
                "matthew.rouser",
            ]
        },
    )
