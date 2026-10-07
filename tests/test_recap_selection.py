import unittest
from datetime import UTC, datetime

from obsidian_intake_agent.meetings.occurrence_assignment import OccurrenceWindow
from obsidian_intake_agent.meetings.recap_selection import select_recap, validate_detail


class RecapSelectionTests(unittest.TestCase):
    def setUp(self):
        self.target = OccurrenceWindow(
            "current", "series", datetime(2026, 10, 2, 16, tzinfo=UTC), datetime(2026, 10, 2, 16, 25, tzinfo=UTC)
        )

    def choose(self, rows, context=None):
        return select_recap(rows, self.target, context or (self.target,))

    def test_boundaries_offsets_and_order(self):
        for stamp in ("2026-10-02T15:45:00Z", "2026-10-02T16:55:00Z", "2026-10-02T12:03:00-04:00"):
            with self.subTest(stamp=stamp):
                current = {"id": "current", "createdDateTime": stamp}
                old = {"id": "old", "createdDateTime": "2026-07-22T16:00:00Z"}
                for rows in ([old, current], [current, old], [current, current]):
                    self.assertEqual(self.choose(rows).selected["id"], "current")

    def test_invalid_source_metadata(self):
        for row in (
            {"id": "x"},
            {"id": "x", "createdDateTime": "bad"},
            {"id": "x", "createdDateTime": "2026-10-02T16:00:00"},
            {"createdDateTime": "2026-10-02T16:00:00Z"},
            {"id": "x", "createdDateTime": "2026-10-02T16:00:00Z", "endDateTime": "2026-10-01T16:00:00Z"},
        ):
            with self.subTest(row=row):
                self.assertIsNone(self.choose([row]).selected)

    def test_overlapping_candidate_blocks_other_unique_candidate(self):
        other = OccurrenceWindow(
            "neighbor", "series", datetime(2026, 10, 2, 17, tzinfo=UTC), datetime(2026, 10, 2, 17, 25, tzinfo=UTC)
        )
        result = self.choose(
            [
                {"id": "unique", "createdDateTime": "2026-10-02T16:00:00Z"},
                {"id": "overlap", "createdDateTime": "2026-10-02T16:50:00Z"},
            ],
            (self.target, other),
        )
        self.assertIsNone(result.selected)
        self.assertEqual(result.reason, "ambiguous_recap_assignment")
        self.assertIn("neighbor", result.conflicts)

    def test_multiple_or_conflicting_records_not_guessed(self):
        a = {"id": "a", "createdDateTime": "2026-10-02T16:00:00Z"}
        self.assertEqual(self.choose([a, {**a, "id": "b"}]).reason, "multiple_matching_recaps")
        self.assertEqual(
            self.choose([a, {**a, "createdDateTime": "2026-10-02T16:01:00Z"}]).reason, "conflicting_recap_metadata"
        )
        self.assertEqual(self.choose([a, {"id": "a"}]).reason, "conflicting_recap_metadata")

    def test_detail_must_agree_and_contain_content(self):
        a = {"id": "a", "createdDateTime": "2026-10-02T16:00:00+00:00"}
        for detail in ({**a, "id": "b"}, {"id": "a"}, a, {**a, "createdDateTime": "2026-07-22T16:00:00Z"}):
            with self.subTest(detail=detail), self.assertRaises(ValueError):
                validate_detail(a, detail)
        self.assertEqual(validate_detail(a, {**a, "meetingNotes": [{"text": "Current"}]}), a)

    def test_old_series_recap_cannot_match_new_occurrence(self):
        occurrence = OccurrenceWindow(
            "october", "series", datetime(2026, 10, 2, 16, tzinfo=UTC), datetime(2026, 10, 2, 16, 25, tzinfo=UTC)
        )
        result = select_recap([{"id": "july", "createdDateTime": "2026-07-22T16:05:00Z"}], occurrence, (occurrence,))
        self.assertIsNone(result.selected)
        self.assertEqual(result.reason, "no_matching_recap")
