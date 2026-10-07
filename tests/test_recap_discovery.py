import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from obsidian_intake_agent.meetings import (
    build_bundle_processing_plan,
    build_transcript_sync_plan,
    execute_bundle_processing_plan,
    write_planned_bundle_notes,
)
from obsidian_intake_agent.meetings.sync import (
    GraphMeetingFallbackSummaryClient,
    MeetingDiscoverySnapshot,
    OutlookMeetingCandidate,
)
from tests.test_meeting_process_bundles import _processor_for_vault


class RecapDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.meeting = OutlookMeetingCandidate(
            "october",
            "Recurring",
            datetime(2026, 10, 2, 16, tzinfo=UTC),
            datetime(2026, 10, 2, 16, 25, tzinfo=UTC),
            join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_test%40thread.v2/0",
        )
        self.requests = []
        self.pages = {
            "https://graph.example/v1.0/copilot/users/user/onlineMeetings/online/aiInsights": {
                "value": [{"id": "old", "createdDateTime": "2026-07-22T16:00:00Z"}]
            }
        }

    def fetch(self, url, token):
        self.requests.append(url)
        if "/me/onlineMeetings?" in url:
            return {"value": [{"id": "online"}]}
        return self.pages[url]

    def client(self):
        return GraphMeetingFallbackSummaryClient(
            access_token="fake",
            intake_root=self.root,
            api_base_url="https://graph.example/v1.0",
            user_id="user",
            fetch_json=self.fetch,
        )

    def test_old_recap_not_downloaded_or_written(self):
        # With explicit user_id, meeting resolution uses /users/user/onlineMeetings.
        client = self.client()
        client._graph_user_path = "/me"
        artifact = client.discover_artifacts(meeting=self.meeting)[0]
        self.assertEqual(artifact.status, "missing")
        self.assertFalse(any(url.endswith("/old") for url in self.requests))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_second_page_selected_and_chat_not_fetched(self):
        first = next(iter(self.pages))
        self.pages[first]["@odata.nextLink"] = first + "?page=2"
        selected = {"id": "new", "createdDateTime": "2026-10-02T16:03:00Z"}
        self.pages[first + "?page=2"] = {"value": [selected]}
        self.pages[first + "/new"] = {**selected, "meetingNotes": [{"text": "Current discussion"}]}
        client = self.client()
        client._graph_user_path = "/me"
        artifact = client.discover_artifacts(meeting=self.meeting)[0]
        self.assertEqual(artifact.status, "available")
        self.assertIn("Current discussion", artifact.planned_content)
        self.assertEqual(artifact.recap_provenance["insight"]["id"], "new")
        self.assertFalse(any("/chats/" in url for url in self.requests))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unsafe_or_incomplete_pagination_never_selects(self):
        first = next(iter(self.pages))
        for link in (
            "https://evil.example/steal",
            first,
            "http://graph.example/next",
            "https://user@graph.example/next",
            "https://graph.example:444/next",
            42,
        ):
            with self.subTest(link=link):
                self.pages[first] = {
                    "value": [{"id": "new", "createdDateTime": "2026-10-02T16:03:00Z"}],
                    "@odata.nextLink": link,
                }
                self.requests.clear()
                client = self.client()
                client._graph_user_path = "/me"
                artifact = client.discover_artifacts(meeting=self.meeting)[0]
                self.assertEqual(artifact.status, "not_attempted")
                self.assertEqual(len(self.requests), 2)

    def test_cached_file_without_provenance_is_preserved(self):
        path = self.root / "bundles/fallbacks/2026-10-02 - Teams - Recurring (fallback).md"
        path.parent.mkdir(parents=True)
        path.write_text("Old source")
        artifact = self.client().discover_artifacts(meeting=self.meeting)[0]
        self.assertEqual(artifact.status, "not_attempted")
        self.assertEqual(path.read_text(), "Old source")
        self.assertEqual(self.requests, [])

    def test_old_recap_end_to_end_stays_pending_and_has_no_actions_on_rerun(self):
        processor = _processor_for_vault(self.root)
        client = self.client()
        client._graph_user_path = "/me"
        client._intake_root = self.root / "00_Intake"
        discovery = SimpleNamespace(
            list_recently_ended_meetings=lambda **kwargs: MeetingDiscoverySnapshot(
                meetings=(self.meeting,), provider_label="synthetic"
            )
        )
        for _ in range(2):
            plan = build_transcript_sync_plan(
                client=discovery,
                artifact_discovery_client=client,
                since=date(2026, 10, 2),
                intake_root=client._intake_root,
                now=self.meeting.end_at + timedelta(hours=2),
            )
            self.assertIsNone(plan.items[0].intake_bundle_note.processor_input_path)
            write_planned_bundle_notes(plan)
            bundle_plan = build_bundle_processing_plan(intake_root=client._intake_root / "bundles", processor=processor)
            self.assertEqual(bundle_plan.ready_count, 0)
            execute_bundle_processing_plan(bundle_plan, processor=processor)
        self.assertEqual(list((self.root / "01_Meetings").glob("*.md")), [])
        self.assertEqual(list((self.root / "07_Actions").glob("*.md")), [])
