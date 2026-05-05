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
    MeetingArtifact,
    MeetingAttendee,
    MeetingDiscoverySnapshot,
    OutlookMeetingCandidate,
    UnconfiguredOutlookMeetingDiscoveryClient,
    build_transcript_sync_plan,
    render_bundle_write_result,
    render_intake_bundle_note,
    render_meeting_identity_sidecar,
    render_outlook_metadata_sidecar,
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
            "Transcript, chat, and recap retrieval are not implemented yet; current sync planning is metadata-first.",
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
                        response_status="accepted",
                        attendees=(
                            MeetingAttendee(
                                name="Jordan",
                                email="jordan@example.com",
                                role="required",
                                response_status="accepted",
                            ),
                        ),
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
        self.assertEqual(
            bundle_note.metadata_path,
            Path("/tmp/vault/00_Intake/2026-05-04 - Teams - Platform - Sync- Weekly (outlook).json"),
        )
        self.assertEqual(bundle_note.identity_path.parent, Path("/tmp/vault/00_Intake/_meeting_sync/identities"))
        self.assertEqual(bundle_note.identity_path.suffix, ".json")
        self.assertEqual(bundle_note.attendance_confidence, "calendar_invite_only")
        self.assertIn("Known from calendar invite; attendance not guaranteed.", bundle_note.source_limitations)
        self.assertIn('outlook_event_id: "evt-1"', bundle_note.content)
        self.assertIn("- Organizer: Casey", bundle_note.content)
        self.assertIn("- Your Response: accepted", bundle_note.content)
        self.assertIn(
            "- Join URL: https://teams.microsoft.com/l/meetup-join/19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D",
            bundle_note.content,
        )
        self.assertIn("  - Jordan <jordan@example.com> (required, accepted)", bundle_note.content)
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
        self.assertIn(
            "would_write_outlook_metadata: /tmp/vault/00_Intake/2026-05-04 - Teams - Platform Sync (outlook).json",
            rendered,
        )
        bundle_note = plan.items[0].intake_bundle_note
        assert bundle_note is not None
        self.assertIn(f"would_write_meeting_identity: {bundle_note.identity_path}", rendered)
        self.assertIn("bundle_attendance_confidence: calendar_invite_only", rendered)
        self.assertIn("bundle_source_limitation: Known from calendar invite; attendance not guaranteed.", rendered)

    def test_rendered_plan_includes_processable_gap_summary(self) -> None:
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
            intake_root=Path("/tmp/vault/00_Intake"),
            now=now,
        )

        rendered = render_transcript_sync_plan(plan)

        self.assertIn("meeting_sync_processable_missing_vtt: 1", rendered)
        self.assertIn("meeting_sync_processable_missing_transcript_text: 1", rendered)
        self.assertIn("meeting_sync_processable_missing_chat: 1", rendered)
        self.assertIn("meeting_sync_processable_missing_recap: 1", rendered)
        self.assertIn("meeting_sync_processable_calendar_only: 1", rendered)
        self.assertIn("meeting_sync_processable_vtt_available: 0", rendered)
        self.assertIn("meeting_sync_processable_vtt_not_attempted: 1", rendered)
        self.assertIn("meeting_sync_processable_chat_permission_blocked: 0", rendered)

    def test_rendered_plan_includes_source_status_breakdown_and_limitations(self) -> None:
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
                        discovered_artifacts=(
                            MeetingArtifact(
                                source_name="Teams .vtt transcript",
                                status="missing",
                                detail="No transcript file was published for this meeting.",
                            ),
                            MeetingArtifact(
                                source_name="Teams transcript text",
                                status="available",
                                detail="Transcript text was discovered from a future Teams sync source.",
                            ),
                            MeetingArtifact(
                                source_name="Teams meeting chat",
                                status="permission_blocked",
                                detail="Graph chat message access is not granted.",
                            ),
                        ),
                    ),
                )
            ),
            since=date(2026, 5, 1),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=now,
        )

        rendered = render_transcript_sync_plan(plan)
        bundle_note = plan.items[0].intake_bundle_note
        assert bundle_note is not None

        self.assertIn("meeting_sync_processable_vtt_missing: 1", rendered)
        self.assertIn("meeting_sync_processable_transcript_text_available: 1", rendered)
        self.assertIn("meeting_sync_processable_chat_permission_blocked: 1", rendered)
        self.assertIn("meeting_sync_processable_recap_not_attempted: 1", rendered)
        self.assertIn(
            "source_pending: Teams .vtt transcript=missing (No transcript file was published for this meeting.)",
            rendered,
        )
        self.assertIn(
            "source_pending: Teams meeting chat=permission_blocked (Graph chat message access is not granted.)",
            rendered,
        )
        self.assertEqual(bundle_note.sources_used, ("Teams transcript text", "Outlook calendar metadata"))
        self.assertIn("Teams .vtt transcript was not available.", bundle_note.source_limitations)
        self.assertIn("Permission blocked retrieval of Teams meeting chat.", bundle_note.source_limitations)
        self.assertIn("Copilot recap / AI summary was not retrieved yet.", bundle_note.source_limitations)

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
                        "attendees": [
                            {
                                "type": "required",
                                "status": {"response": "accepted"},
                                "emailAddress": {"name": "Morgan", "address": "morgan@example.com"},
                            },
                            {
                                "type": "optional",
                                "status": {"response": "tentativelyAccepted"},
                                "emailAddress": {"name": "Jordan", "address": "jordan@example.com"},
                            },
                        ],
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
        self.assertEqual(meeting.response_status, "accepted")
        self.assertTrue(meeting.is_teams_meeting())
        self.assertEqual(meeting.teams_meeting_id(), "19:meeting_graph123@thread.v2")
        self.assertEqual(
            meeting.attendees,
            (
                MeetingAttendee(
                    name="Morgan",
                    email="morgan@example.com",
                    role="required",
                    response_status="accepted",
                ),
                MeetingAttendee(
                    name="Jordan",
                    email="jordan@example.com",
                    role="optional",
                    response_status="tentativelyAccepted",
                ),
            ),
        )

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
            attendees=(
                MeetingAttendee(
                    name="Priya",
                    email="priya@example.com",
                    role="required",
                    response_status="accepted",
                ),
            ),
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
        self.assertIn("- Attendees:", rendered)
        self.assertIn("  - Priya <priya@example.com> (required, accepted)", rendered)
        self.assertIn("## Source", rendered)
        self.assertIn("## Artifact Plan", rendered)

    def test_render_outlook_metadata_sidecar_renders_expected_json(self) -> None:
        meeting = _meeting(
            event_id="evt-9",
            subject="Delivery Review",
            join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_delivery%40thread.v2/0?context=%7B%7D",
            online_meeting_provider="teamsForBusiness",
            organizer="Morgan",
            response_status="accepted",
            attendees=(
                MeetingAttendee(
                    name="Priya",
                    email="priya@example.com",
                    role="required",
                    response_status="accepted",
                ),
            ),
        )
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            since=date(2026, 5, 1),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        rendered = render_outlook_metadata_sidecar(meeting=meeting, bundle=plan.items[0].bundle)

        self.assertIn('"source_type": "outlook_calendar_metadata"', rendered)
        self.assertIn('"outlook_event_id": "evt-9"', rendered)
        self.assertIn('"teams_meeting_id": "19:meeting_delivery@thread.v2"', rendered)
        self.assertIn('"email": "priya@example.com"', rendered)

    def test_render_meeting_identity_sidecar_renders_expected_json(self) -> None:
        meeting = _meeting(
            event_id="evt-9",
            subject="Delivery Review",
            join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_delivery%40thread.v2/0?context=%7B%7D",
            online_meeting_provider="teamsForBusiness",
        )
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            since=date(2026, 5, 1),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )
        bundle_note = plan.items[0].intake_bundle_note
        assert bundle_note is not None

        rendered = render_meeting_identity_sidecar(
            meeting=meeting,
            bundle=plan.items[0].bundle,
            bundle_note_path=bundle_note.path,
            metadata_path=bundle_note.metadata_path,
        )

        self.assertIn('"source_type": "meeting_sync_identity"', rendered)
        self.assertIn('"outlook_event_id": "evt-9"', rendered)
        self.assertIn('"teams_meeting_id": "19:meeting_delivery@thread.v2"', rendered)
        self.assertIn(
            '"bundle_note_path": "/tmp/vault/00_Intake/2026-05-04 - Teams - Delivery Review (bundle).md"', rendered
        )

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
            self.assertTrue((intake_root / "2026-05-04 - Teams - Platform Sync (outlook).json").exists())
            identity_path = plan.items[0].intake_bundle_note.identity_path
            self.assertTrue(identity_path.exists())
            self.assertEqual(second_result.written_count, 0)
            self.assertEqual(second_result.skipped_existing_count, 1)
            self.assertEqual(len(first_result.written_metadata_paths), 1)
            self.assertEqual(len(second_result.skipped_existing_metadata_paths), 1)
            self.assertEqual(len(first_result.written_identity_paths), 1)
            self.assertEqual(len(second_result.skipped_existing_identity_paths), 1)

    def test_planning_skips_meeting_when_identity_marker_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            planning_probe = build_transcript_sync_plan(
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
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            existing_identity = planning_probe.items[0].intake_bundle_note.identity_path
            existing_identity.parent.mkdir(parents=True)
            existing_identity.write_text("{}\n", encoding="utf-8")

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
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(plan.process_count, 0)
            self.assertEqual(plan.skip_count, 1)
            self.assertEqual(plan.items[0].decision, "skip")
            self.assertEqual(plan.items[0].intake_bundle_note.identity_path, existing_identity)
            self.assertEqual(
                plan.items[0].reasons,
                ("Skipped meeting because a meeting identity marker already exists.",),
            )

            rendered = render_transcript_sync_plan(plan)
            self.assertIn("meeting_sync_would_process: 0", rendered)
            self.assertIn("meeting_sync_would_skip: 1", rendered)
            self.assertIn("reason: Skipped meeting because a meeting identity marker already exists.", rendered)

    def test_planning_allows_identity_backfill_when_bundle_and_metadata_exist_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            existing_bundle = intake_root / "2026-05-04 - Teams - Platform Sync (bundle).md"
            existing_bundle.write_text("existing bundle\n", encoding="utf-8")
            existing_metadata = intake_root / "2026-05-04 - Teams - Platform Sync (outlook).json"
            existing_metadata.write_text("{}\n", encoding="utf-8")

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
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(plan.process_count, 1)
            self.assertEqual(plan.skip_count, 0)
            self.assertEqual(plan.items[0].decision, "process")

            result = write_planned_bundle_notes(plan)
            self.assertEqual(result.written_count, 0)
            self.assertEqual(len(result.written_metadata_paths), 0)
            self.assertEqual(result.skipped_existing_count, 1)
            self.assertEqual(len(result.written_identity_paths), 1)
            identity_path = plan.items[0].intake_bundle_note.identity_path
            self.assertTrue(identity_path.exists())

    def test_render_bundle_write_result_lists_written_and_skipped_paths(self) -> None:
        result = BundleWriteResult(
            written_bundle_note_paths=(Path("/tmp/a.md"),),
            written_metadata_paths=(Path("/tmp/a.json"),),
            written_identity_paths=(Path("/tmp/a.identity.json"),),
            skipped_existing_bundle_note_paths=(Path("/tmp/b.md"),),
            skipped_existing_metadata_paths=(Path("/tmp/b.json"),),
            skipped_existing_identity_paths=(Path("/tmp/b.identity.json"),),
        )

        rendered = render_bundle_write_result(result)

        self.assertIn("meeting_sync_bundle_notes_written: 1", rendered)
        self.assertIn("meeting_sync_bundle_notes_skipped_existing: 1", rendered)
        self.assertIn("meeting_sync_outlook_metadata_written: 1", rendered)
        self.assertIn("meeting_sync_outlook_metadata_skipped_existing: 1", rendered)
        self.assertIn("meeting_sync_identity_markers_written: 1", rendered)
        self.assertIn("meeting_sync_identity_markers_skipped_existing: 1", rendered)
        self.assertIn("bundle_note_written: /tmp/a.md", rendered)
        self.assertIn("bundle_note_skipped_existing: /tmp/b.md", rendered)
        self.assertIn("outlook_metadata_written: /tmp/a.json", rendered)
        self.assertIn("outlook_metadata_skipped_existing: /tmp/b.json", rendered)
        self.assertIn("meeting_identity_written: /tmp/a.identity.json", rendered)
        self.assertIn("meeting_identity_skipped_existing: /tmp/b.identity.json", rendered)


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
    attendees: tuple[MeetingAttendee, ...] = (),
    response_status: str | None = None,
    is_cancelled: bool = False,
    is_all_day: bool = False,
    event_type: str | None = None,
    join_url: str | None = None,
    online_meeting_provider: str | None = None,
    discovered_artifacts: tuple[MeetingArtifact, ...] = (),
) -> OutlookMeetingCandidate:
    return OutlookMeetingCandidate(
        event_id=event_id,
        subject=subject,
        start_at=datetime.fromisoformat("2026-05-04T13:00:00+00:00"),
        end_at=datetime.fromisoformat("2026-05-04T13:30:00+00:00"),
        organizer=organizer,
        attendees=attendees,
        response_status=response_status,
        is_cancelled=is_cancelled,
        is_all_day=is_all_day,
        event_type=event_type,
        join_url=join_url,
        online_meeting_provider=online_meeting_provider,
        discovered_artifacts=discovered_artifacts,
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
