import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obsidian_intake_agent.meetings.occurrence_assignment import OccurrenceWindow
from obsidian_intake_agent.meetings.recap_provenance import create_recap_provenance, read_recap_provenance
from obsidian_intake_agent.meetings.transcript_provenance import provenance_path


class RecapProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bundles/fallbacks/recap.md"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"Original recap")
        self.target = OccurrenceWindow(
            "event", "series", datetime(2026, 10, 2, 16, tzinfo=UTC), datetime(2026, 10, 2, 16, 25, tzinfo=UTC)
        )
        self.payload = create_recap_provenance(
            content=self.path.read_bytes(),
            target=self.target,
            context=(self.target,),
            insight={"id": "insight", "createdDateTime": "2026-10-02T16:03:00Z"},
            online_meeting_id="online",
        )
        self.save()

    def save(self):
        provenance_path(self.path).write_text(json.dumps(self.payload))

    def test_valid_cached_pair_and_system_alias(self):
        self.assertEqual(read_recap_provenance(self.path, self.target), self.payload)

    def test_missing_sidecar_or_mutated_source_fails(self):
        provenance_path(self.path).unlink()
        with self.assertRaises(ValueError):
            read_recap_provenance(self.path, self.target)
        self.save()
        self.path.write_bytes(b"Edited")
        with self.assertRaises(ValueError):
            read_recap_provenance(self.path, self.target)

    def test_unknown_policy_and_old_insight_fail(self):
        for key, value in (
            ("policy_version", 99),
            ("occurrence_context", []),
            ("insight", {"id": "old", "createdDateTime": "2026-07-22T16:03:00Z"}),
        ):
            previous = self.payload[key]
            self.payload[key] = value
            self.save()
            with self.subTest(key=key), self.assertRaises(ValueError):
                read_recap_provenance(self.path, self.target)
            self.payload[key] = previous

    def test_rescheduled_or_new_overlap_fails(self):
        shifted = replace(self.target, scheduled_start=self.target.scheduled_start + timedelta(days=1))
        with self.assertRaises(ValueError):
            read_recap_provenance(self.path, shifted)
        other = replace(self.target, occurrence_id="other")
        with self.assertRaises(ValueError):
            read_recap_provenance(self.path, self.target, (self.target, other))

    def test_symlink_source_and_sidecar_fail(self):
        for path in (self.path, provenance_path(self.path)):
            with self.subTest(path=path):
                original = path.read_bytes()
                backup = path.with_suffix(".backup")
                backup.write_bytes(original)
                path.unlink()
                path.symlink_to(backup)
                with self.assertRaises(ValueError):
                    read_recap_provenance(self.path, self.target)
                path.unlink()
                path.write_bytes(original)
