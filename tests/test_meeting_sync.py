from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError

from obsidian_intake_agent.meetings import (
    BundleWriteResult,
    GraphOutlookMeetingDiscoveryClient,
    MeetingDiscoverySnapshot,
    OutlookMeetingCandidate,
    UnconfiguredOutlookMeetingDiscoveryClient,
    build_transcript_sync_plan,
    render_bundle_write_result,
    render_intake_bundle_note,
    render_transcript_sync_plan,
    write_planned_bundle_notes,
)


class TranscriptSyncPlannerTests(unittest.TestCase):
    def test_skips_canceled_declined_all_day_and_focus_without_meeting_content(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(event_id="cancelled", subject="Cancelled", is_cancelled=True),
                    _meeting(event_id="declined", subject="Declined", response_status="declined"),
                    _meeting(event_id="all-day", subject="All Day", is_all_day=True),
                    _meeting(event_id="focus", subject="Focus Time", event_type="focus"),
                )
            ),
            since=date(2026, 5, 1),
            now=now,
        )

        self.assertEqual(plan.candidate_count, 4)
        self.assertEqual(plan.process_count, 0)
        self.assertEqual(plan.skip_count, 4)
        self.assertEqual(plan.items[0].reasons, ("Skipped canceled meeting.",))
        self.assertEqual(plan.items[1].reasons, ("Skipped declined event because no meeting content was detected.",))
        self.assertEqual(plan.items[2].reasons, ("Skipped all-day event because no meeting content was detected.",))
        self.assertEqual(plan.items[3].reasons, ("Skipped focus block because no meeting content was detected.",))

    def test_processes_teams_meeting_and_extracts_teams_meeting_id(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_YWJjMTIz%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            since=date(2026, 5, 1),
            now=now,
        )

        self.assertEqual(plan.process_count, 1)
        self.assertEqual(plan.items[0].decision, "process")
        self.assertEqual(plan.items[0].bundle.teams_meeting_id, "19:meeting_YWJjMTIz@thread.v2")
        self.assertEqual(plan.items[0].bundle.available_sources(), ["Outlook calendar metadata"])
        self.assertIsNone(plan.items[0].intake_bundle_note)
        self.assertIn(
            "Dry-run only: transcript, chat, recap, and bundle-note writes are not implemented yet.",
            plan.items[0].reasons,
        )

    def test_keeps_declined_focus_and_all_day_meetings_when_teams_content_exists(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        join_url = "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_keep%40thread.v2/0?context=%7B%7D"
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="declined",
                        subject="Declined but recorded",
                        response_status="declined",
                        join_url=join_url,
                    ),
                    _meeting(event_id="all-day", subject="All day offsite", is_all_day=True, join_url=join_url),
                    _meeting(event_id="focus", subject="Focus Time", event_type="focus", join_url=join_url),
                )
            ),
            since=date(2026, 5, 1),
            now=now,
        )

        self.assertEqual(plan.process_count, 3)
        self.assertEqual(plan.skip_count, 0)

    def test_unconfigured_client_renders_clear_warning(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=UnconfiguredOutlookMeetingDiscoveryClient(),
            since=date(2026, 5, 1),
            now=now,
        )

        rendered = render_transcript_sync_plan(plan)
        self.assertIn("meeting_sync_warning: Outlook calendar discovery is not configured yet;", rendered)
        self.assertIn("meeting_sync_candidates: 0", rendered)

    def test_builds_planned_intake_bundle_note_when_intake_root_is_available(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform / Sync: Weekly",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                        organizer="Casey",
                    ),
                )
            ),
            since=date(2026, 5, 1),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=now,
        )

        bundle_note = plan.items[0].intake_bundle_note
        self.assertIsNotNone(bundle_note)
        assert bundle_note is not None
        self.assertEqual(
            bundle_note.path,
            Path("/tmp/vault/00_Intake/2026-05-04 - Teams - Platform - Sync- Weekly (bundle).md"),
        )
        self.assertEqual(bundle_note.attendance_confidence, "calendar_invite_only")
        self.assertIn("Known from calendar invite; attendance not guaranteed.", bundle_note.source_limitations)
        self.assertIn('outlook_event_id: "evt-1"', bundle_note.content)
        self.assertIn("- Organizer: Casey", bundle_note.content)
        self.assertIn("- Source Used: Outlook calendar metadata", bundle_note.content)

    def test_rendered_plan_includes_bundle_path_and_transparency(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            since=date(2026, 5, 1),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=now,
        )

        rendered = render_transcript_sync_plan(plan)
        self.assertIn(
            "would_write_bundle: /tmp/vault/00_Intake/2026-05-04 - Teams - Platform Sync (bundle).md", rendered
        )
        self.assertIn("bundle_attendance_confidence: calendar_invite_only", rendered)
        self.assertIn("bundle_source_limitation: Known from calendar invite; attendance not guaranteed.", rendered)

    def test_graph_client_parses_outlook_events_into_meeting_candidates(self) -> None:
        client = GraphOutlookMeetingDiscoveryClient(
            access_token="token",
            fetch_json=lambda url, token: {
                "value": [
                    {
                        "id": "evt-123",
                        "subject": "Platform Sync",
                        "start": {"dateTime": "2026-05-04T13:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-05-04T13:30:00", "timeZone": "UTC"},
                        "isCancelled": False,
                        "isAllDay": False,
                        "showAs": "busy",
                        "responseStatus": {"response": "accepted"},
                        "onlineMeetingProvider": "teamsForBusiness",
                        "onlineMeeting": {
                            "joinUrl": (
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D"
                            )
                        },
                        "bodyPreview": "Join here",
                        "categories": ["Client"],
                        "organizer": {"emailAddress": {"name": "Casey"}},
                        "type": "singleInstance",
                    }
                ]
            },
        )

        snapshot = client.list_recently_ended_meetings(
            since=date(2026, 5, 1),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        self.assertEqual(snapshot.provider_label, "graph_outlook_calendar")
        self.assertIsNone(snapshot.warning)
        self.assertEqual(len(snapshot.meetings), 1)
        meeting = snapshot.meetings[0]
        self.assertEqual(meeting.event_id, "evt-123")
        self.assertEqual(meeting.organizer, "Casey")
        self.assertTrue(meeting.is_teams_meeting())
        self.assertEqual(meeting.teams_meeting_id(), "19:meeting_graph123@thread.v2")

    def test_graph_client_returns_permission_warning(self) -> None:
        client = GraphOutlookMeetingDiscoveryClient(
            access_token="token",
            fetch_json=lambda url, token: (_ for _ in ()).throw(_SyntheticHTTPError(url, 403, "Forbidden")),
        )

        snapshot = client.list_recently_ended_meetings(
            since=date(2026, 5, 1),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        self.assertEqual(snapshot.meetings, ())
        self.assertIn("HTTP 403", snapshot.warning or "")

    def test_render_intake_bundle_note_renders_expected_sections(self) -> None:
        meeting = _meeting(
            event_id="evt-9",
            subject="Delivery Review",
            join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_delivery%40thread.v2/0?context=%7B%7D",
            online_meeting_provider="teamsForBusiness",
            organizer="Morgan",
        )
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            since=date(2026, 5, 1),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )
        bundle_note = plan.items[0].intake_bundle_note
        assert bundle_note is not None

        rendered = render_intake_bundle_note(
            meeting=meeting,
            bundle=plan.items[0].bundle,
            attendance_confidence=bundle_note.attendance_confidence,
            sources_used=bundle_note.sources_used,
            source_limitations=bundle_note.source_limitations,
        )

        self.assertTrue(rendered.startswith("---\n"))
        self.assertIn('intake_kind: "meeting_source_bundle"', rendered)
        self.assertIn("# 2026-05-04 - Teams - Delivery Review (bundle)", rendered)
        self.assertIn("## Source", rendered)
        self.assertIn("## Artifact Plan", rendered)

    def test_write_planned_bundle_notes_writes_processable_notes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                        _meeting(event_id="evt-2", subject="Cancelled", is_cancelled=True),
                    )
                ),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=now,
            )

            first_result = write_planned_bundle_notes(plan)
            second_result = write_planned_bundle_notes(plan)

            self.assertEqual(first_result.written_count, 1)
            self.assertEqual(first_result.skipped_existing_count, 0)
            self.assertTrue((intake_root / "2026-05-04 - Teams - Platform Sync (bundle).md").exists())
            self.assertEqual(second_result.written_count, 0)
            self.assertEqual(second_result.skipped_existing_count, 1)

    def test_render_bundle_write_result_lists_written_and_skipped_paths(self) -> None:
        result = BundleWriteResult(
            written_paths=(Path("/tmp/a.md"),),
            skipped_existing_paths=(Path("/tmp/b.md"),),
        )

        rendered = render_bundle_write_result(result)

        self.assertIn("meeting_sync_bundle_notes_written: 1", rendered)
        self.assertIn("meeting_sync_bundle_notes_skipped_existing: 1", rendered)
        self.assertIn("bundle_note_written: /tmp/a.md", rendered)
        self.assertIn("bundle_note_skipped_existing: /tmp/b.md", rendered)


@dataclass(slots=True)
class _StubMeetingDiscoveryClient:
    meetings: tuple[OutlookMeetingCandidate, ...]

    def list_recently_ended_meetings(
        self,
        *,
        since: date,
        now: datetime,
    ) -> MeetingDiscoverySnapshot:
        del since, now
        return MeetingDiscoverySnapshot(
            meetings=self.meetings,
            provider_label="stub_outlook_calendar",
        )


def _meeting(
    *,
    event_id: str,
    subject: str,
    organizer: str | None = None,
    response_status: str | None = None,
    is_cancelled: bool = False,
    is_all_day: bool = False,
    event_type: str | None = None,
    join_url: str | None = None,
    online_meeting_provider: str | None = None,
) -> OutlookMeetingCandidate:
    return OutlookMeetingCandidate(
        event_id=event_id,
        subject=subject,
        start_at=datetime.fromisoformat("2026-05-04T13:00:00+00:00"),
        end_at=datetime.fromisoformat("2026-05-04T13:30:00+00:00"),
        organizer=organizer,
        response_status=response_status,
        is_cancelled=is_cancelled,
        is_all_day=is_all_day,
        event_type=event_type,
        join_url=join_url,
        online_meeting_provider=online_meeting_provider,
    )


if __name__ == "__main__":
    unittest.main()


class _SyntheticHTTPError(HTTPError):
    def __init__(self, url: str, code: int, message: str) -> None:
        self.url = url
        self.code = code
        self.msg = message
        self.hdrs = None
        self.fp = None
