from __future__ import annotations

import unittest

from obsidian_intake_agent.processors.meeting_metadata import MeetingMetadata
from obsidian_intake_agent.processors.vtt_extractor import (
    action_items_from_extracted,
    heuristic_extract_meeting_data,
    normalize_extracted_meeting_data,
)


class VttExtractorTests(unittest.TestCase):
    def test_heuristic_extracts_owner_will_action(self) -> None:
        extracted = heuristic_extract_meeting_data(
            "Matthew will ship the update by Friday.",
            _metadata(),
        )

        self.assertEqual(
            extracted["action_items"],
            [{"text": "ship the update by Friday.", "owner": "Matthew", "due": None}],
        )

    def test_action_items_from_extracted_ignores_empty_items(self) -> None:
        extracted = {
            "action_items": [
                {"text": "", "owner": "Matthew"},
                {"text": "Share rollout notes.", "owner": "Matthew", "due": "Friday"},
                "not a dict",
            ]
        }

        items = action_items_from_extracted(extracted)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "Share rollout notes.")
        self.assertEqual(items[0].owner, "Matthew")
        self.assertEqual(items[0].due, "Friday")

    def test_normalize_extracted_meeting_data_applies_metadata_defaults(self) -> None:
        normalized = normalize_extracted_meeting_data({"title": "", "action_items": None}, _metadata())

        self.assertEqual(normalized["title"], "Platform Sync")
        self.assertEqual(normalized["date"], "2026-03-12")
        self.assertEqual(normalized["source"], "Teams")
        self.assertEqual(normalized["action_items"], [])


def _metadata() -> MeetingMetadata:
    return MeetingMetadata(
        date="2026-03-12",
        source="Teams",
        title="Platform Sync",
        canonical_basename="2026-03-12 - Teams - Platform Sync.md",
    )


if __name__ == "__main__":
    unittest.main()
