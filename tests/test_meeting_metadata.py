from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from obsidian_intake_agent.processors.meeting_metadata import normalize_meeting_metadata


class MeetingMetadataTests(unittest.TestCase):
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

    def test_normalize_meeting_metadata_accepts_equals_in_filename_date(self) -> None:
        path = Path("/tmp/2026=05-11 - Teams - Slalom Kickoff Week Monday.vtt")

        metadata = normalize_meeting_metadata(path)

        self.assertEqual(metadata.date, "2026-05-11")
        self.assertEqual(metadata.source, "Teams")
        self.assertEqual(metadata.title, "Slalom Kickoff Week Monday")
        self.assertEqual(
            metadata.canonical_basename,
            "2026-05-11 - Teams - Slalom Kickoff Week Monday.md",
        )

    def test_normalize_meeting_metadata_detects_source_with_missing_title_space(self) -> None:
        path = Path("/tmp/2026-05-12 - Teams -Slalom Kickoff Week Tuesday.vtt")

        metadata = normalize_meeting_metadata(path)

        self.assertEqual(metadata.date, "2026-05-12")
        self.assertEqual(metadata.source, "Teams")
        self.assertEqual(metadata.title, "Slalom Kickoff Week Tuesday")

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


if __name__ == "__main__":
    unittest.main()
