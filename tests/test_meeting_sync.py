from __future__ import annotations

import io
import json
import tempfile
import unittest
from base64 import urlsafe_b64encode
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from obsidian_intake_agent.meetings import (
    BundleWriteResult,
    ChainedMeetingArtifactDiscoveryClient,
    GraphMeetingFallbackSummaryClient,
    GraphOutlookMeetingDiscoveryClient,
    GraphTranscriptDiscoveryClient,
    GraphTranscriptDownloadClient,
    LocalIntakeTranscriptDiscoveryClient,
    MeetingArtifact,
    MeetingAttendee,
    MeetingDiscoverySnapshot,
    MeetingSourceBundle,
    OccurrenceWindow,
    OutlookMeetingCandidate,
    SelectedTranscriptDiagnostic,
    TranscriptDiagnostics,
    TranscriptSyncPlan,
    TranscriptSyncPlanItem,
    UnconfiguredOutlookMeetingDiscoveryClient,
    build_bundle_processing_plan,
    build_transcript_sync_plan,
    execute_bundle_processing_plan,
    render_bundle_write_result,
    render_intake_bundle_note,
    render_meeting_identity_sidecar,
    render_outlook_metadata_sidecar,
    render_transcript_sync_plan,
    transcript_provenance,
    write_planned_bundle_notes,
)
from obsidian_intake_agent.meetings import process_bundles as process_bundles_module
from obsidian_intake_agent.meetings import sync as meeting_sync_module
from obsidian_intake_agent.processors.meeting_processor import ProcessResult


class TranscriptSyncPlannerTests(unittest.TestCase):
    def test_exact_duplicate_transcript_records_select_only_once(self) -> None:
        meeting = meeting_sync_module._meetings_with_occurrence_context((_recurring_meeting(),))[0]
        record = meeting_sync_module.GraphTranscriptRecord(
            transcript_id="duplicate-segment",
            created_at=datetime.fromisoformat("2026-07-17T17:05:00+00:00"),
            created_at_missing=False,
        )

        selected, conflicts = meeting_sync_module._select_transcripts_for_occurrence(
            (record, record),
            meeting,
        )

        self.assertEqual(tuple(item.transcript_id for item in selected), ("duplicate-segment",))
        self.assertEqual(conflicts, ())

    def test_conflicting_timestamp_duplicates_are_not_selected_for_any_occurrence(self) -> None:
        first = _recurring_meeting()
        second = replace(
            first,
            event_id="evt-recurring-next",
            start_at=datetime.fromisoformat("2026-07-18T17:00:00+00:00"),
            end_at=datetime.fromisoformat("2026-07-18T17:25:00+00:00"),
        )
        meetings = meeting_sync_module._meetings_with_occurrence_context((first, second))
        conflicting_records = (
            meeting_sync_module.GraphTranscriptRecord(
                transcript_id="conflicting-segment",
                created_at=datetime.fromisoformat("2026-07-17T17:05:00+00:00"),
                created_at_missing=False,
            ),
            meeting_sync_module.GraphTranscriptRecord(
                transcript_id="conflicting-segment",
                created_at=datetime.fromisoformat("2026-07-18T17:05:00+00:00"),
                created_at_missing=False,
            ),
        )

        for meeting in meetings:
            with self.subTest(event_id=meeting.event_id):
                selected, conflicts = meeting_sync_module._select_transcripts_for_occurrence(
                    conflicting_records,
                    meeting,
                )
                self.assertEqual(selected, ())
                self.assertEqual(conflicts, ())

    def test_exact_duplicate_snapshot_meetings_are_collapsed_before_planning(self) -> None:
        meeting = _meeting(
            event_id="duplicate-event",
            subject="Platform Sync",
            response_status="accepted",
        )

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting, meeting)),
            since=date(2026, 5, 4),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        self.assertEqual(plan.candidate_count, 1)

    def test_conflicting_duplicate_snapshot_event_ids_are_rejected_without_echoing_id(self) -> None:
        event_id = "sensitive-event-id"
        meeting = _meeting(
            event_id=event_id,
            subject="Platform Sync",
            response_status="accepted",
        )
        conflicting = replace(
            meeting,
            end_at=datetime.fromisoformat("2026-05-04T13:45:00+00:00"),
        )

        with self.assertRaisesRegex(ValueError, "conflicting definitions") as exc_info:
            build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting, conflicting)),
                since=date(2026, 5, 4),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

        self.assertNotIn(event_id, str(exc_info.exception))

    def test_occurrence_with_unique_and_ambiguous_transcripts_renders_conflict_without_any_selection(self) -> None:
        join_url = "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D"
        first = OutlookMeetingCandidate(
            event_id="occurrence-first",
            subject="Recurring Platform Sync",
            start_at=datetime.fromisoformat("2026-07-17T17:00:00+00:00"),
            end_at=datetime.fromisoformat("2026-07-17T17:25:00+00:00"),
            response_status="accepted",
            join_url=join_url,
            online_meeting_provider="teamsForBusiness",
            series_master_id="series-master",
        )
        second = OutlookMeetingCandidate(
            event_id="occurrence-second",
            subject="Recurring Platform Sync",
            start_at=datetime.fromisoformat("2026-07-17T17:30:00+00:00"),
            end_at=datetime.fromisoformat("2026-07-17T17:55:00+00:00"),
            response_status="accepted",
            join_url=join_url,
            online_meeting_provider="teamsForBusiness",
            series_master_id="series-master",
        )

        def fetch_json(url: str, token: str) -> dict[str, object]:
            del token
            if url.endswith("/transcripts"):
                return {
                    "value": [
                        {
                            "id": "unique-current-segment",
                            "createdDateTime": "2026-07-17T17:00:00Z",
                        },
                        {
                            "id": "ambiguous-segment",
                            "createdDateTime": "2026-07-17T17:35:00Z",
                        },
                    ]
                }
            return {"value": [{"id": "opaque-meeting-id"}]}

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(first, second)),
            artifact_discovery_client=GraphTranscriptDiscoveryClient(
                access_token="token",
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
            ),
            since=date(2026, 7, 17),
            now=datetime.fromisoformat("2026-07-17T19:00:00+00:00"),
        )

        self.assertEqual(plan.items[0].decision, "process")
        transcript_artifact = plan.items[0].bundle.artifact("Teams transcript text")
        assert transcript_artifact is not None
        assert transcript_artifact.diagnostics is not None
        self.assertEqual(transcript_artifact.diagnostics.selected_transcripts, ())
        rendered = render_transcript_sync_plan(plan)
        self.assertIn("meeting_sync_occurrence_errors: 1", rendered)
        self.assertEqual(
            rendered.count("meeting_sync_occurrence_error: ambiguous_transcript_assignment"),
            1,
        )
        self.assertIn(
            "meeting_sync_occurrence_error: ambiguous_transcript_assignment",
            rendered,
        )
        self.assertIn("candidate_transcript_id: ambiguous-segment", rendered)
        self.assertIn(
            "candidate_transcript_created_at: 2026-07-17T17:35:00+00:00",
            rendered,
        )
        self.assertIn(
            "scheduled_window: 2026-07-17T17:00:00+00:00 to 2026-07-17T17:25:00+00:00",
            rendered,
        )
        self.assertIn(
            "selection_window: 2026-07-17T16:45:00+00:00 to 2026-07-17T17:55:00+00:00",
            rendered,
        )
        self.assertIn(
            "scheduled_window: 2026-07-17T17:30:00+00:00 to 2026-07-17T17:55:00+00:00",
            rendered,
        )
        self.assertIn(
            "selection_window: 2026-07-17T17:15:00+00:00 to 2026-07-17T18:25:00+00:00",
            rendered,
        )
        self.assertIn("occurrence_id: occurrence-first", rendered)
        self.assertIn("occurrence_id: occurrence-second", rendered)
        self.assertNotIn("selected_transcript_id: ambiguous-segment", rendered)
        self.assertNotIn("selected_transcript_id: unique-current-segment", rendered)

    def test_dry_run_renders_safe_structured_transcript_diagnostics(self) -> None:
        meeting = _recurring_meeting()
        diagnostics = TranscriptDiagnostics(
            candidate_count=3,
            selected_transcripts=(
                SelectedTranscriptDiagnostic("segment-2", datetime.fromisoformat("2026-07-17T17:20:00+00:00")),
                SelectedTranscriptDiagnostic("segment-1", datetime.fromisoformat("2026-07-17T17:05:00+00:00")),
            ),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="downloaded",
        )
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            artifact_discovery_client=_StubArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        "Teams .vtt transcript",
                        "available",
                        "Downloaded transcript.",
                        diagnostics=diagnostics,
                    ),
                )
            ),
            since=date(2026, 7, 17),
            now=datetime.fromisoformat("2026-07-17T18:00:00+00:00"),
        )

        rendered = render_transcript_sync_plan(plan)

        self.assertIn("  transcript_candidates: 3", rendered)
        self.assertLess(
            rendered.index("selected_transcript_id: segment-1"), rendered.index("selected_transcript_id: segment-2")
        )
        self.assertIn("  selected_transcript_created_at: 2026-07-17T17:05:00+00:00", rendered)
        self.assertIn(
            "  selected_for_occurrence: 2026-07-17T17:00:00+00:00 to 2026-07-17T17:25:00+00:00",
            rendered,
        )
        self.assertIn("  local_transcript_action: downloaded", rendered)

    def test_later_artifact_ambiguity_is_rendered_after_selected_diagnostics(self) -> None:
        meeting = replace(
            _recurring_meeting(),
            occurrence_context=(
                OccurrenceWindow(
                    occurrence_id="occurrence-first",
                    series_id="series",
                    scheduled_start=datetime.fromisoformat("2026-07-17T17:00:00+00:00"),
                    scheduled_end=datetime.fromisoformat("2026-07-17T17:25:00+00:00"),
                ),
                OccurrenceWindow(
                    occurrence_id="occurrence-second",
                    series_id="series",
                    scheduled_start=datetime.fromisoformat("2026-07-17T17:30:00+00:00"),
                    scheduled_end=datetime.fromisoformat("2026-07-17T17:55:00+00:00"),
                ),
            ),
        )
        meeting = replace(meeting, event_id="occurrence-first")
        selected_diagnostics = TranscriptDiagnostics(
            candidate_count=1,
            selected_transcripts=(
                SelectedTranscriptDiagnostic(
                    "selected-segment",
                    datetime.fromisoformat("2026-07-17T17:05:00+00:00"),
                ),
            ),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="validated",
        )
        ambiguous_diagnostics = TranscriptDiagnostics(
            candidate_count=1,
            selected_transcripts=(),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="none",
            assignment_conflicts=(
                meeting_sync_module.AssignmentConflict(
                    "later-ambiguous-segment",
                    datetime.fromisoformat("2026-07-17T17:35:00+00:00"),
                    ("occurrence-first", "occurrence-second"),
                ),
            ),
        )
        bundle = MeetingSourceBundle(
            meeting=meeting,
            artifacts=(
                MeetingArtifact("Teams .vtt transcript", "available", diagnostics=selected_diagnostics),
                MeetingArtifact("Teams transcript text", "missing", diagnostics=ambiguous_diagnostics),
            ),
            teams_meeting_id=meeting.teams_meeting_id(),
        )
        item = TranscriptSyncPlanItem(
            decision="process",
            meeting=meeting,
            bundle=bundle,
            reasons=(),
            intake_bundle_note=None,
        )
        plan = TranscriptSyncPlan(
            since=date(2026, 7, 17),
            generated_at=datetime.fromisoformat("2026-07-17T19:00:00+00:00"),
            provider_label="test",
            warning=None,
            items=(item,),
        )

        self.assertEqual(plan.occurrence_error_count, 1)
        self.assertTrue(plan.has_ambiguous_transcript_assignments)
        self.assertEqual(plan.occurrence_errors[0].transcript_id, "later-ambiguous-segment")
        rendered = render_transcript_sync_plan(plan)
        self.assertEqual(rendered.count("selected_transcript_id: selected-segment"), 1)
        self.assertEqual(
            rendered.count("meeting_sync_occurrence_error: ambiguous_transcript_assignment"),
            1,
        )
        self.assertIn("candidate_transcript_id: later-ambiguous-segment", rendered)
        self.assertIn(
            "candidate_transcript_created_at: 2026-07-17T17:35:00+00:00",
            rendered,
        )
        self.assertIn("occurrence_id: occurrence-first", rendered)
        self.assertIn("occurrence_id: occurrence-second", rendered)

    def test_fetch_graph_json_omits_outlook_timezone_preference_by_default(self) -> None:
        captured: dict[str, object] = {}

        class _FakeResponse:
            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def read(self) -> bytes:
                return json.dumps({"value": []}).encode("utf-8")

        def _fake_urlopen(request, *, timeout=None):
            captured["prefer"] = request.headers.get("Prefer")
            captured["timeout"] = timeout
            return _FakeResponse()

        with patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen):
            payload = meeting_sync_module._fetch_graph_json(
                "https://graph.microsoft.com/v1.0/me/onlineMeetings",
                "token",
            )

        self.assertEqual(payload, {"value": []})
        self.assertIsNone(captured["prefer"])
        self.assertEqual(captured["timeout"], meeting_sync_module.GRAPH_REQUEST_TIMEOUT_SECONDS)

    def test_fetch_graph_json_can_request_outlook_timezone_preference(self) -> None:
        captured: dict[str, object] = {}

        class _FakeResponse:
            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def read(self) -> bytes:
                return json.dumps({"value": []}).encode("utf-8")

        def _fake_urlopen(request, *, timeout=None):
            captured["prefer"] = request.headers.get("Prefer")
            captured["timeout"] = timeout
            return _FakeResponse()

        with patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen):
            payload = meeting_sync_module._fetch_graph_json(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                "token",
                prefer_outlook_timezone=True,
            )

        self.assertEqual(payload, {"value": []})
        self.assertEqual(captured["prefer"], 'outlook.timezone="UTC"')
        self.assertEqual(captured["timeout"], meeting_sync_module.GRAPH_REQUEST_TIMEOUT_SECONDS)

    def test_fetch_graph_json_wraps_timeout_error(self) -> None:
        def _fake_urlopen(request, *, timeout=None):
            del request, timeout
            raise TimeoutError("The read operation timed out")

        with (
            patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen),
            self.assertRaises(meeting_sync_module.MeetingSyncGraphTimeoutError) as exc,
        ):
            meeting_sync_module._fetch_graph_json(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                "token",
            )

        self.assertEqual(exc.exception.operation, "Microsoft Graph JSON request")
        self.assertEqual(exc.exception.timeout_seconds, meeting_sync_module.GRAPH_REQUEST_TIMEOUT_SECONDS)
        self.assertIn("Microsoft Graph did not respond within 30 seconds", str(exc.exception))

    def test_fetch_graph_json_wraps_urlerror_timeout_reason(self) -> None:
        def _fake_urlopen(request, *, timeout=None):
            del request, timeout
            raise URLError(TimeoutError("timed out"))

        with (
            patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen),
            self.assertRaises(meeting_sync_module.MeetingSyncGraphTimeoutError),
        ):
            meeting_sync_module._fetch_graph_json(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                "token",
            )

    def test_fetch_graph_json_retries_timeout_once_before_succeeding(self) -> None:
        attempts = 0

        class _FakeResponse:
            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def read(self) -> bytes:
                return json.dumps({"value": ["ok"]}).encode("utf-8")

        def _fake_urlopen(request, *, timeout=None):
            del request, timeout
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("The read operation timed out")
            return _FakeResponse()

        with patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen):
            payload = meeting_sync_module._fetch_graph_json(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                "token",
            )

        self.assertEqual(attempts, 2)
        self.assertEqual(payload, {"value": ["ok"]})

    def test_fetch_graph_json_preserves_non_timeout_urlerror(self) -> None:
        def _fake_urlopen(request, *, timeout=None):
            del request, timeout
            raise URLError("temporary DNS failure")

        with (
            patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen),
            self.assertRaises(URLError),
        ):
            meeting_sync_module._fetch_graph_json(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                "token",
            )

    def test_fetch_graph_bytes_uses_timeout(self) -> None:
        captured: dict[str, object] = {}

        class _FakeResponse:
            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def read(self) -> bytes:
                return b"WEBVTT\n"

        def _fake_urlopen(request, *, timeout=None):
            captured["accept"] = request.headers.get("Accept")
            captured["timeout"] = timeout
            return _FakeResponse()

        with patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen):
            content = meeting_sync_module._fetch_graph_bytes(
                "https://graph.microsoft.com/v1.0/me/onlineMeetings/transcripts/content",
                "token",
            )

        self.assertEqual(content, b"WEBVTT\n")
        self.assertEqual(captured["accept"], "text/vtt")
        self.assertEqual(captured["timeout"], meeting_sync_module.GRAPH_REQUEST_TIMEOUT_SECONDS)

    def test_fetch_graph_bytes_wraps_timeout_error(self) -> None:
        def _fake_urlopen(request, *, timeout=None):
            del request, timeout
            raise TimeoutError("The read operation timed out")

        with (
            patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen),
            self.assertRaises(meeting_sync_module.MeetingSyncGraphTimeoutError) as exc,
        ):
            meeting_sync_module._fetch_graph_bytes(
                "https://graph.microsoft.com/v1.0/me/onlineMeetings/transcripts/content",
                "token",
            )

        self.assertEqual(exc.exception.operation, "Microsoft Graph content request")
        self.assertEqual(exc.exception.timeout_seconds, meeting_sync_module.GRAPH_REQUEST_TIMEOUT_SECONDS)

    def test_skips_canceled_declined_all_day_and_focus_without_meeting_content(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(event_id="cancelled", subject="Cancelled", is_cancelled=True),
                    _meeting(event_id="declined", subject="Declined", response_status="declined"),
                    _meeting(event_id="all-day", subject="All Day", response_status="accepted", is_all_day=True),
                    _meeting(event_id="focus", subject="Focus Time", response_status="accepted", event_type="focus"),
                )
            ),
            since=date(2026, 5, 1),
            now=now,
        )

        self.assertEqual(plan.candidate_count, 4)
        self.assertEqual(plan.process_count, 0)
        self.assertEqual(plan.skip_count, 4)
        self.assertEqual(plan.items[0].reasons, ("Skipped canceled meeting.",))
        self.assertEqual(plan.items[1].reasons, ("Skipped event because response status was declined.",))
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
                        response_status="accepted",
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
            "Bundle output preserves artifact retrieval status for transcript, chat, and recap sources.",
            plan.items[0].reasons,
        )

    def test_skips_unresponded_meeting_even_when_teams_join_url_exists(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="notResponded",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_unresponded%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            since=date(2026, 5, 1),
            now=now,
        )

        self.assertEqual(plan.process_count, 0)
        self.assertEqual(plan.skip_count, 1)
        self.assertEqual(plan.items[0].decision, "skip")
        self.assertEqual(plan.items[0].reasons, ("Skipped event because response status was notResponded.",))

    def test_skips_office_hours_subject_even_when_teams_meeting_exists(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Office Hours - Platform",
                        response_status="accepted",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_officehours%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            since=date(2026, 5, 1),
            now=now,
        )

        self.assertEqual(plan.process_count, 0)
        self.assertEqual(plan.skip_count, 1)
        self.assertEqual(plan.items[0].decision, "skip")
        self.assertEqual(plan.items[0].reasons, ("Skipped low-signal office-hours event.",))

    def test_skipped_v1_ineligible_meeting_does_not_trigger_artifact_discovery(self) -> None:
        discovery_client = _RecordingArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams transcript text",
                    status="available",
                    detail="Should never be discovered for skipped meetings.",
                    matched_paths=(Path("/tmp/should-not-run.md"),),
                ),
            )
        )

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Office Hours - Platform",
                        response_status="accepted",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_officehours%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            artifact_discovery_client=discovery_client,
            since=date(2026, 5, 1),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        self.assertEqual(discovery_client.calls, 0)
        self.assertEqual(plan.items[0].decision, "skip")
        self.assertEqual(plan.items[0].reasons, ("Skipped low-signal office-hours event.",))

    def test_non_teams_meeting_does_not_trigger_artifact_discovery(self) -> None:
        discovery_client = _RecordingArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams transcript text",
                    status="available",
                    detail="Should never be discovered for non-Teams meetings.",
                    matched_paths=(Path("/tmp/should-not-run.md"),),
                ),
            )
        )

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Status Update",
                        response_status="accepted",
                    ),
                )
            ),
            artifact_discovery_client=discovery_client,
            since=date(2026, 5, 1),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        self.assertEqual(discovery_client.calls, 0)
        self.assertEqual(plan.items[0].decision, "skip")
        self.assertEqual(
            plan.items[0].reasons,
            ("Skipped event because Outlook metadata did not identify a Teams meeting.",),
        )

    def test_skips_declined_but_keeps_focus_and_all_day_meetings_when_teams_content_exists(self) -> None:
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
                    _meeting(
                        event_id="all-day",
                        subject="All day offsite",
                        response_status="accepted",
                        is_all_day=True,
                        join_url=join_url,
                    ),
                    _meeting(
                        event_id="focus",
                        subject="Focus Time",
                        response_status="accepted",
                        event_type="focus",
                        join_url=join_url,
                    ),
                )
            ),
            since=date(2026, 5, 1),
            now=now,
        )

        self.assertEqual(plan.process_count, 2)
        self.assertEqual(plan.skip_count, 1)
        self.assertEqual(plan.items[0].decision, "skip")
        self.assertEqual(plan.items[0].reasons, ("Skipped event because response status was declined.",))
        self.assertEqual(plan.items[1].decision, "process")
        self.assertEqual(plan.items[2].decision, "process")

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
            Path("/tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Platform - Sync- Weekly (bundle).md"),
        )
        self.assertEqual(
            bundle_note.metadata_path,
            Path("/tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Platform - Sync- Weekly (outlook).json"),
        )
        self.assertEqual(
            bundle_note.identity_path.parent,
            Path("/tmp/vault/00_Intake/bundles/_meeting_sync/identities"),
        )
        self.assertEqual(bundle_note.identity_path.suffix, ".json")
        self.assertIsNone(bundle_note.processor_input_path)
        self.assertIsNone(bundle_note.processor_input_source_name)
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

    def test_bundle_paths_live_under_machine_managed_bundles_subtree(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        meeting = _meeting(
            event_id="evt-1",
            subject="Platform Sync",
            response_status="accepted",
            join_url=("https://teams.microsoft.com/l/meetup-join/19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"),
            online_meeting_provider="teamsForBusiness",
        )
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            since=date(2026, 5, 1),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=now,
        )

        bundle_note = plan.items[0].intake_bundle_note
        assert bundle_note is not None

        self.assertEqual(
            bundle_note.path,
            Path("/tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Platform Sync (bundle).md"),
        )
        self.assertEqual(
            bundle_note.metadata_path,
            Path("/tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Platform Sync (outlook).json"),
        )
        self.assertEqual(
            bundle_note.identity_path.parent,
            Path("/tmp/vault/00_Intake/bundles/_meeting_sync/identities"),
        )
        self.assertEqual(
            meeting_sync_module._transcript_relative_path(meeting),
            Path("bundles/raw_transcripts/2026-05-04 - Teams - Platform Sync.vtt"),
        )
        self.assertEqual(
            meeting_sync_module._transcript_text_relative_path(meeting),
            Path("bundles/raw_transcripts/2026-05-04 - Teams - Platform Sync.md"),
        )
        self.assertEqual(
            meeting_sync_module._fallback_summary_relative_path(meeting),
            Path("bundles/fallbacks/2026-05-04 - Teams - Platform Sync (fallback).md"),
        )

    def test_rendered_plan_includes_bundle_path_and_transparency(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
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
            "would_write_bundle: /tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Platform Sync (bundle).md",
            rendered,
        )
        self.assertIn(
            "would_write_outlook_metadata: /tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Platform Sync (outlook).json",
            rendered,
        )
        bundle_note = plan.items[0].intake_bundle_note
        assert bundle_note is not None
        self.assertIn(f"would_write_meeting_identity: {bundle_note.identity_path}", rendered)
        self.assertIn("bundle_attendance_confidence: calendar_invite_only", rendered)
        self.assertIn("bundle_source_limitation: Known from calendar invite; attendance not guaranteed.", rendered)
        self.assertNotIn("would_process_intake_file:", rendered)

    def test_rendered_plan_includes_processable_gap_summary(self) -> None:
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
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
                        response_status="accepted",
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
        self.assertIn(
            "Teams .vtt transcript was not available: No transcript file was published for this meeting.",
            bundle_note.source_limitations,
        )
        self.assertIn(
            "Permission blocked retrieval of Teams meeting chat: Graph chat message access is not granted.",
            bundle_note.source_limitations,
        )
        self.assertIn(
            "Copilot recap / AI summary was not retrieved yet: Discovery-only dry run; artifact retrieval is deferred.",
            bundle_note.source_limitations,
        )

    def test_local_transcript_discovery_marks_matching_vtt_as_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "bundles" / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            rendered = render_transcript_sync_plan(plan)
            bundle_note = plan.items[0].intake_bundle_note
            assert bundle_note is not None

            self.assertEqual(plan.items[0].bundle.source_status("Teams .vtt transcript"), "available")
            self.assertIn("meeting_sync_processable_vtt_available: 1", rendered)
            self.assertEqual(
                plan.items[0].bundle.artifact_paths("Teams .vtt transcript"),
                (transcript_path,),
            )
            self.assertIn(f"source_available: Teams .vtt transcript ({transcript_path})", rendered)
            self.assertEqual(
                bundle_note.sources_used,
                ("Teams .vtt transcript", "Outlook calendar metadata"),
            )
            self.assertEqual(bundle_note.processor_input_path, transcript_path)
            self.assertEqual(bundle_note.processor_input_source_name, "Teams .vtt transcript")
            self.assertNotIn("Teams .vtt transcript was not retrieved yet.", bundle_note.source_limitations)
            self.assertIn(f"- Matched Path: `{transcript_path}`", bundle_note.content)
            self.assertIn(f"- Preferred Input: `{transcript_path}`", bundle_note.content)
            self.assertIn("- Preferred Source: Teams .vtt transcript", bundle_note.content)

    def test_local_transcript_discovery_reports_same_date_candidates_when_exact_match_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            candidate_path = intake_root / "2026-05-04 - Teams - Platform Planning.vtt"
            candidate_path.write_text("WEBVTT\n", encoding="utf-8")

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            artifact = plan.items[0].bundle.artifact("Teams .vtt transcript")
            assert artifact is not None

            self.assertEqual(artifact.status, "missing")
            self.assertEqual(artifact.matched_paths, ())
            self.assertIn("Expected local transcript stem: 2026-05-04 - Teams - Platform Sync", artifact.detail or "")
            self.assertIn("Same-date local candidate(s):", artifact.detail or "")
            self.assertIn(str(candidate_path), artifact.detail or "")
            self.assertIn("Candidates are suggestions only and were not selected automatically.", artifact.detail or "")

    def test_local_transcript_discovery_reports_expected_stem_when_no_candidates_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            artifact = plan.items[0].bundle.artifact("Teams .vtt transcript")
            assert artifact is not None

            self.assertEqual(artifact.status, "missing")
            self.assertIn("Expected local transcript stem: 2026-05-04 - Teams - Platform Sync", artifact.detail or "")
            self.assertIn("no same-date local transcript candidates found", artifact.detail or "")

    def test_local_transcript_discovery_ignores_meeting_sync_machine_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            machine_path = intake_root / "bundles" / "_meeting_sync" / "2026-05-04 - Teams - Platform Planning.vtt"
            machine_path.parent.mkdir(parents=True)
            machine_path.write_text("WEBVTT\n", encoding="utf-8")

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            artifact = plan.items[0].bundle.artifact("Teams .vtt transcript")
            assert artifact is not None

            self.assertEqual(artifact.status, "missing")
            self.assertNotIn(str(machine_path), artifact.detail or "")
            self.assertIn("no same-date local transcript candidates found", artifact.detail or "")

    def test_local_transcript_discovery_marks_matching_markdown_transcript_as_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "2026-05-04 - Teams - Platform Sync.md"
            transcript_path.write_text("Transcript text\n", encoding="utf-8")

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            rendered = render_transcript_sync_plan(plan)
            bundle_note = plan.items[0].intake_bundle_note
            assert bundle_note is not None

            self.assertEqual(plan.items[0].bundle.source_status("Teams transcript text"), "available")
            self.assertEqual(plan.items[0].bundle.source_status("Teams .vtt transcript"), "missing")
            self.assertEqual(
                plan.items[0].bundle.artifact_paths("Teams transcript text"),
                (transcript_path,),
            )
            self.assertIn("meeting_sync_processable_transcript_text_available: 1", rendered)
            self.assertIn("meeting_sync_processable_vtt_missing: 1", rendered)
            self.assertIn(f"- Matched Path: `{transcript_path}`", bundle_note.content)
            self.assertEqual(bundle_note.processor_input_path, transcript_path)
            self.assertEqual(bundle_note.processor_input_source_name, "Teams transcript text")
            self.assertIn(f"- Preferred Input: `{transcript_path}`", bundle_note.content)
            self.assertIn("- Preferred Source: Teams transcript text", bundle_note.content)

    def test_duplicate_transcript_text_matches_prefer_machine_managed_bundle_path_for_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            legacy_path = intake_root / "2026-05-04 - Teams - Platform Sync.md"
            bundle_path = intake_root / "bundles" / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.md"
            legacy_path.write_text("Legacy transcript text\n", encoding="utf-8")
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            bundle_path.write_text("Managed transcript text\n", encoding="utf-8")

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            bundle_note = plan.items[0].intake_bundle_note
            assert bundle_note is not None

            self.assertEqual(
                plan.items[0].bundle.artifact_paths("Teams transcript text"),
                (bundle_path, legacy_path),
            )
            self.assertEqual(bundle_note.processor_input_path, bundle_path)
            self.assertEqual(bundle_note.processor_input_source_name, "Teams transcript text")
            self.assertIn(f"- Preferred Input: `{bundle_path}`", bundle_note.content)

    def test_chained_artifact_discovery_allows_later_clients_to_override_sources(self) -> None:
        graph_client = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams transcript text",
                    status="available",
                    detail="Graph transcript metadata available.",
                ),
            )
        )
        local_client = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams transcript text",
                    status="available",
                    detail="Matched local intake artifact.",
                    matched_paths=(Path("/tmp/2026-05-04 - Teams - Platform Sync.md"),),
                ),
            )
        )

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            artifact_discovery_client=ChainedMeetingArtifactDiscoveryClient(graph_client, local_client),
            since=date(2026, 5, 1),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        self.assertEqual(
            plan.items[0].bundle.artifact_paths("Teams transcript text"),
            (Path("/tmp/2026-05-04 - Teams - Platform Sync.md"),),
        )
        bundle_note = plan.items[0].intake_bundle_note
        assert bundle_note is not None
        self.assertEqual(bundle_note.processor_input_source_name, "Teams transcript text")

    def test_chained_artifact_discovery_preserves_permission_blocked_over_later_missing(self) -> None:
        graph_client = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="permission_blocked",
                    detail="Graph transcript content download failed with HTTP 403.",
                ),
            )
        )
        local_client = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="missing",
                    detail="No local .vtt intake file matched the meeting date/title.",
                ),
            )
        )

        artifacts = ChainedMeetingArtifactDiscoveryClient(graph_client, local_client).discover_artifacts(
            meeting=_meeting(event_id="evt-1", subject="Platform Sync")
        )

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].source_name, "Teams .vtt transcript")
        self.assertEqual(artifacts[0].status, "permission_blocked")
        self.assertEqual(artifacts[0].detail, "Graph transcript content download failed with HTTP 403.")

    def test_chained_artifact_discovery_preserves_graph_missing_detail_over_later_local_missing(self) -> None:
        graph_client = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="missing",
                    detail="Graph transcript discovery returned no transcript records.",
                ),
            )
        )
        local_client = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="missing",
                    detail="No local .vtt intake file matched the meeting date/title.",
                ),
            )
        )

        artifacts = ChainedMeetingArtifactDiscoveryClient(graph_client, local_client).discover_artifacts(
            meeting=_meeting(event_id="evt-1", subject="Platform Sync")
        )

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].source_name, "Teams .vtt transcript")
        self.assertEqual(artifacts[0].status, "missing")
        self.assertEqual(artifacts[0].detail, "Graph transcript discovery returned no transcript records.")

    def test_chained_artifact_discovery_does_not_readmit_unverifiable_vtt(self) -> None:
        meeting = _recurring_meeting()
        stale_path = Path("/vault/2026-07-17 - Teams - Recurring Platform Sync.vtt")
        diagnostics = TranscriptDiagnostics(
            candidate_count=2,
            selected_transcripts=(),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="none",
        )
        downloader = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="missing",
                    detail="No transcript record matched this occurrence.",
                    diagnostics=diagnostics,
                ),
            )
        )
        local_client = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="available",
                    detail="Matched local intake artifact.",
                    matched_paths=(stale_path,),
                ),
            )
        )

        artifacts = ChainedMeetingArtifactDiscoveryClient(downloader, local_client).discover_artifacts(meeting=meeting)

        self.assertEqual(artifacts[0].status, "missing")
        self.assertEqual(artifacts[0].matched_paths, ())
        self.assertEqual(artifacts[0].diagnostics, diagnostics)

    def test_chained_artifact_discovery_does_not_readmit_vtt_after_downloader_discovery_failure(self) -> None:
        stale_path = Path("/vault/2026-07-17 - Teams - Recurring Platform Sync.vtt")
        downloader_artifact = MeetingArtifact(
            source_name="Teams .vtt transcript",
            status="not_attempted",
            detail="Graph transcript discovery failed: timeout.",
        )
        local_artifact = MeetingArtifact(
            source_name="Teams .vtt transcript",
            status="available",
            detail="Matched local intake artifact.",
            matched_paths=(stale_path,),
        )

        artifacts = ChainedMeetingArtifactDiscoveryClient(
            _StubArtifactDiscoveryClient(artifacts=(downloader_artifact,)),
            _StubArtifactDiscoveryClient(artifacts=(local_artifact,)),
        ).discover_artifacts(meeting=_recurring_meeting())

        self.assertEqual(artifacts, (downloader_artifact,))

    def test_chained_artifact_discovery_preserves_verified_vtt_diagnostics(self) -> None:
        meeting = _recurring_meeting()
        transcript_path = Path("/vault/2026-07-17 - Teams - Recurring Platform Sync.vtt")
        diagnostics = TranscriptDiagnostics(
            candidate_count=2,
            selected_transcripts=(
                SelectedTranscriptDiagnostic("july-17", datetime.fromisoformat("2026-07-17T17:25:00+00:00")),
            ),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="stale_preserved_and_replaced",
        )
        downloader_artifact = MeetingArtifact(
            source_name="Teams .vtt transcript",
            status="available",
            detail="Replaced stale Graph transcript after occurrence validation.",
            matched_paths=(transcript_path,),
            diagnostics=diagnostics,
        )
        local_artifact = MeetingArtifact(
            source_name="Teams .vtt transcript",
            status="available",
            detail="Matched local intake artifact.",
            matched_paths=(transcript_path,),
        )

        artifacts = ChainedMeetingArtifactDiscoveryClient(
            _StubArtifactDiscoveryClient(artifacts=(downloader_artifact,)),
            _StubArtifactDiscoveryClient(artifacts=(local_artifact,)),
        ).discover_artifacts(meeting=meeting)

        self.assertEqual(artifacts, (downloader_artifact,))

    def test_chained_artifact_discovery_preserves_graph_not_attempted_detail_over_later_local_missing(self) -> None:
        graph_client = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="not_attempted",
                    detail="Graph transcript discovery failed: timeout.",
                ),
            )
        )
        local_client = _StubArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="missing",
                    detail="No local .vtt intake file matched the meeting date/title.",
                ),
            )
        )

        artifacts = ChainedMeetingArtifactDiscoveryClient(graph_client, local_client).discover_artifacts(
            meeting=_meeting(event_id="evt-1", subject="Platform Sync")
        )

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].source_name, "Teams .vtt transcript")
        self.assertEqual(artifacts[0].status, "not_attempted")
        self.assertEqual(artifacts[0].detail, "Graph transcript discovery failed: timeout.")

    def test_default_chain_allows_recap_fallback_when_graph_only_has_transcript_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True, exist_ok=True)
            meeting = _meeting(
                event_id="evt-1",
                subject="Platform Sync",
                response_status="accepted",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
            requested_urls: list[str] = []

            def fetch_json(url: str, token: str) -> dict[str, object]:
                requested_urls.append(url)
                self.assertEqual(token, "token")
                if url.endswith(
                    "/me/onlineMeetings?%24filter=JoinWebUrl+eq+%27https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253Ameeting_graph123%2540thread.v2%2F0%3Fcontext%3D%257B%257D%27"
                ):
                    return {"value": [{"id": "opaque-meeting-id"}]}
                if url.endswith("/me?$select=id"):
                    return {"id": "user-123"}
                if url.endswith("/copilot/users/user-123/onlineMeetings/opaque-meeting-id/aiInsights"):
                    return {"value": [{"id": "insight-1"}]}
                if url.endswith("/copilot/users/user-123/onlineMeetings/opaque-meeting-id/aiInsights/insight-1"):
                    return {
                        "id": "insight-1",
                        "meetingNotes": [
                            {"title": "Summary", "text": "Metadata-only transcript should not suppress fallback."}
                        ],
                    }
                if url.endswith("/me/onlineMeetings/opaque-meeting-id?$select=chatInfo"):
                    return {"chatInfo": {"threadId": "19:meeting_graph123@thread.v2"}}
                if url.endswith(
                    "/chats/19:meeting_graph123@thread.v2/messages?%24top=50&%24orderby=createdDateTime+desc"
                ):
                    return {
                        "value": [
                            {
                                "from": {"user": {"displayName": "Priya"}},
                                "body": {"content": "<p>Fallback should still capture this context.</p>"},
                            }
                        ]
                    }
                self.fail(f"Unexpected Graph URL: {url}")

            artifact_client = ChainedMeetingArtifactDiscoveryClient(
                _StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            source_name="Teams transcript text",
                            status="available",
                            detail="Graph transcript metadata exists, but no local processor-ready file was downloaded.",
                        ),
                    )
                ),
                GraphMeetingFallbackSummaryClient(
                    access_token="token",
                    intake_root=intake_root,
                    api_base_url="https://graph.example/v1.0",
                    fetch_json=fetch_json,
                ),
            )

            artifacts = artifact_client.discover_artifacts(meeting=meeting)
            artifact_by_name = {artifact.source_name: artifact for artifact in artifacts}
            fallback_path = intake_root / meeting_sync_module._fallback_summary_relative_path(meeting)

            self.assertEqual(artifact_by_name["Teams transcript text"].status, "available")
            self.assertEqual(artifact_by_name["Copilot recap / AI summary"].status, "available")
            self.assertEqual(artifact_by_name["Copilot recap / AI summary"].matched_paths, (fallback_path,))
            self.assertEqual(artifact_by_name["Teams meeting chat"].status, "available")
            self.assertIsNotNone(artifact_by_name["Copilot recap / AI summary"].planned_content)
            self.assertFalse(fallback_path.exists())
            self.assertGreaterEqual(len(requested_urls), 5)

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
                        "seriesMasterId": "series-master-123",
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
        self.assertEqual(meeting.series_master_id, "series-master-123")
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

    def test_graph_calendar_discovery_propagates_graph_timeout_for_cli_handling(self) -> None:
        def _fake_fetch_json(url: str, token: str) -> dict[str, object]:
            del url, token
            raise meeting_sync_module.MeetingSyncGraphTimeoutError(
                operation="Microsoft Graph JSON request",
                timeout_seconds=meeting_sync_module.GRAPH_REQUEST_TIMEOUT_SECONDS,
            )

        client = GraphOutlookMeetingDiscoveryClient(access_token="token", fetch_json=_fake_fetch_json)

        with self.assertRaises(meeting_sync_module.MeetingSyncGraphTimeoutError):
            client.list_recently_ended_meetings(
                since=date(2026, 5, 1),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

    def test_graph_client_uses_event_body_for_teams_join_url_when_online_meeting_is_missing(self) -> None:
        requested_urls: list[str] = []

        def fetch_json(url: str, token: str) -> dict[str, object]:
            requested_urls.append(url)
            self.assertEqual(token, "token")
            return {
                "value": [
                    {
                        "id": "evt-body-link",
                        "subject": "Body Link Sync",
                        "start": {"dateTime": "2026-05-04T13:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-05-04T13:30:00", "timeZone": "UTC"},
                        "isCancelled": False,
                        "isAllDay": False,
                        "onlineMeetingProvider": "unknown",
                        "onlineMeeting": None,
                        "body": {
                            "content": (
                                "Microsoft Teams meeting "
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_body123%40thread.v2/0?context=%7B%7D"
                            ),
                        },
                        "bodyPreview": "Microsoft Teams meeting",
                        "type": "singleInstance",
                    }
                ]
            }

        client = GraphOutlookMeetingDiscoveryClient(
            access_token="token",
            api_base_url="https://graph.example/v1.0",
            fetch_json=fetch_json,
        )

        snapshot = client.list_recently_ended_meetings(
            since=date(2026, 5, 1),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        self.assertIn("body", requested_urls[0])
        self.assertEqual(len(snapshot.meetings), 1)
        meeting = snapshot.meetings[0]
        self.assertTrue(meeting.is_teams_meeting())
        self.assertEqual(meeting.teams_meeting_id(), "19:meeting_body123@thread.v2")

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

    def test_graph_client_warns_before_calendar_call_when_token_lacks_calendar_scope(self) -> None:
        def encode_jwt_part(payload: dict[str, object]) -> str:
            encoded = urlsafe_b64encode(json.dumps(payload).encode()).decode()
            return encoded.rstrip("=")

        token = ".".join(
            (
                encode_jwt_part({"alg": "none"}),
                encode_jwt_part({"aud": "https://graph.microsoft.com/", "scp": "User.Read"}),
                "signature",
            )
        )

        def fetch_json(url: str, token: str) -> dict[str, object]:
            self.fail("Graph calendar endpoint should not be called when token scopes are known to be insufficient.")

        client = GraphOutlookMeetingDiscoveryClient(
            access_token=token,
            fetch_json=fetch_json,
        )

        snapshot = client.list_recently_ended_meetings(
            since=date(2026, 5, 1),
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        self.assertEqual(snapshot.meetings, ())
        self.assertIn("Graph token is missing delegated Microsoft Graph calendar permission", snapshot.warning or "")
        self.assertIn("Calendars.Read", snapshot.warning or "")

    def test_graph_transcript_discovery_marks_transcript_metadata_available(self) -> None:
        requested_urls: list[str] = []

        def fetch_json(url: str, token: str) -> dict[str, object]:
            requested_urls.append(url)
            self.assertEqual(token, "token")
            if len(requested_urls) == 1:
                return {"value": [{"id": "opaque-meeting-id"}]}
            return {
                "value": [
                    {"id": "transcript-1", "createdDateTime": "2026-05-04T13:05:00Z"},
                    {"id": "transcript-2", "createdDateTime": "2026-05-04T13:25:00Z"},
                ]
            }

        client = GraphTranscriptDiscoveryClient(
            access_token="token", api_base_url="https://graph.example/v1.0", fetch_json=fetch_json
        )
        artifacts = client.discover_artifacts(
            meeting=_meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
        )

        self.assertEqual(
            requested_urls,
            [
                "https://graph.example/v1.0/me/onlineMeetings?%24filter=JoinWebUrl+eq+%27"
                "https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253Ameeting_graph123%2540thread.v2%2F0"
                "%3Fcontext%3D%257B%257D%27",
                "https://graph.example/v1.0/me/onlineMeetings/opaque-meeting-id/transcripts",
            ],
        )
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].source_name, "Teams transcript text")
        self.assertEqual(artifacts[0].status, "available")
        self.assertIn("transcript-1, transcript-2", artifacts[0].detail or "")
        self.assertEqual(artifacts[0].matched_paths, ())
        self.assertEqual(
            artifacts[0].diagnostics,
            TranscriptDiagnostics(
                candidate_count=2,
                selected_transcripts=(
                    SelectedTranscriptDiagnostic("transcript-1", datetime.fromisoformat("2026-05-04T13:05:00+00:00")),
                    SelectedTranscriptDiagnostic("transcript-2", datetime.fromisoformat("2026-05-04T13:25:00+00:00")),
                ),
                occurrence_start_at=datetime.fromisoformat("2026-05-04T13:00:00+00:00"),
                occurrence_end_at=datetime.fromisoformat("2026-05-04T13:30:00+00:00"),
                local_action="none",
            ),
        )

    def test_graph_transcript_discovery_selects_only_current_recurring_occurrence(self) -> None:
        json_call_count = 0

        def fetch_json(url: str, token: str) -> dict[str, object]:
            nonlocal json_call_count
            json_call_count += 1
            if json_call_count == 1:
                return {"value": [{"id": "opaque-meeting-id"}]}
            return {
                "value": [
                    {"id": "july-8", "createdDateTime": "2026-07-08T17:25:00Z"},
                    {"id": "july-17", "createdDateTime": "2026-07-17T17:25:00Z"},
                ]
            }

        artifacts = GraphTranscriptDiscoveryClient(
            access_token="token",
            api_base_url="https://graph.example/v1.0",
            fetch_json=fetch_json,
        ).discover_artifacts(meeting=_recurring_meeting())

        self.assertEqual(artifacts[0].status, "available")
        self.assertIn("july-17", artifacts[0].detail or "")
        self.assertNotIn("july-8", artifacts[0].detail or "")
        assert artifacts[0].diagnostics is not None
        self.assertEqual(artifacts[0].diagnostics.candidate_count, 2)
        self.assertEqual(
            artifacts[0].diagnostics.selected_transcripts,
            (SelectedTranscriptDiagnostic("july-17", datetime.fromisoformat("2026-07-17T17:25:00+00:00")),),
        )

    def test_graph_transcript_discovery_marks_missing_when_no_record_matches_occurrence(self) -> None:
        json_call_count = 0

        def fetch_json(url: str, token: str) -> dict[str, object]:
            nonlocal json_call_count
            json_call_count += 1
            if json_call_count == 1:
                return {"value": [{"id": "opaque-meeting-id"}]}
            return {"value": [{"id": "july-8", "createdDateTime": "2026-07-08T17:25:00Z"}]}

        artifacts = GraphTranscriptDiscoveryClient(
            access_token="token",
            api_base_url="https://graph.example/v1.0",
            fetch_json=fetch_json,
        ).discover_artifacts(meeting=_recurring_meeting())

        self.assertEqual(artifacts[0].status, "missing")
        self.assertEqual(
            artifacts[0].detail,
            "Graph transcript discovery returned no transcript records for this meeting occurrence.",
        )
        assert artifacts[0].diagnostics is not None
        self.assertEqual(artifacts[0].diagnostics.candidate_count, 1)
        self.assertEqual(artifacts[0].diagnostics.selected_transcripts, ())

    def test_chained_discovery_preserves_graph_diagnostics_on_existing_local_transcript(self) -> None:
        meeting = _recurring_meeting()
        local_path = Path("/vault/2026-07-17 - Teams - Recurring Platform Sync.md")
        local_artifact = MeetingArtifact(
            "Teams transcript text",
            "available",
            "Matched local transcript.",
            matched_paths=(local_path,),
        )
        graph_diagnostics = TranscriptDiagnostics(
            candidate_count=2,
            selected_transcripts=(
                SelectedTranscriptDiagnostic("july-17", datetime.fromisoformat("2026-07-17T17:25:00+00:00")),
            ),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="none",
        )
        chain = ChainedMeetingArtifactDiscoveryClient(
            _StubArtifactDiscoveryClient(artifacts=(local_artifact,)),
            _StubArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        "Teams transcript text",
                        "available",
                        "Graph metadata available.",
                        diagnostics=graph_diagnostics,
                    ),
                )
            ),
        )

        artifacts = chain.discover_artifacts(meeting=meeting)

        self.assertEqual(artifacts[0].matched_paths, (local_path,))
        self.assertEqual(artifacts[0].diagnostics, graph_diagnostics)

    def test_chained_discovery_carries_graph_diagnostics_to_later_local_transcript(self) -> None:
        meeting = _recurring_meeting()
        local_path = Path("/vault/2026-07-17 - Teams - Recurring Platform Sync.md")
        graph_diagnostics = TranscriptDiagnostics(
            candidate_count=2,
            selected_transcripts=(
                SelectedTranscriptDiagnostic("july-17", datetime.fromisoformat("2026-07-17T17:25:00+00:00")),
            ),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="none",
        )
        graph_artifact = MeetingArtifact(
            "Teams transcript text",
            "available",
            "Graph metadata available.",
            diagnostics=graph_diagnostics,
        )
        local_artifact = MeetingArtifact(
            "Teams transcript text",
            "available",
            "Matched local transcript.",
            matched_paths=(local_path,),
        )

        artifacts = ChainedMeetingArtifactDiscoveryClient(
            _StubArtifactDiscoveryClient(artifacts=(graph_artifact,)),
            _StubArtifactDiscoveryClient(artifacts=(local_artifact,)),
        ).discover_artifacts(meeting=meeting)

        self.assertEqual(artifacts[0].matched_paths, (local_path,))
        self.assertEqual(artifacts[0].diagnostics, graph_diagnostics)

    def test_dry_run_renders_diagnostics_from_transcript_metadata_artifact(self) -> None:
        meeting = _recurring_meeting()
        diagnostics = TranscriptDiagnostics(
            candidate_count=2,
            selected_transcripts=(
                SelectedTranscriptDiagnostic("july-17", datetime.fromisoformat("2026-07-17T17:25:00+00:00")),
            ),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="none",
        )
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            artifact_discovery_client=_StubArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        "Teams transcript text",
                        "available",
                        "Graph metadata available.",
                        diagnostics=diagnostics,
                    ),
                )
            ),
            since=date(2026, 7, 17),
            now=datetime.fromisoformat("2026-07-17T18:00:00+00:00"),
        )

        rendered = render_transcript_sync_plan(plan)

        self.assertIn("  transcript_candidates: 2", rendered)
        self.assertIn("  selected_transcript_id: july-17", rendered)
        self.assertIn("  selected_transcript_created_at: 2026-07-17T17:25:00+00:00", rendered)

    def test_graph_transcript_discovery_marks_empty_transcript_list_missing(self) -> None:
        requested_urls: list[str] = []

        def fetch_json(url: str, token: str) -> dict[str, object]:
            requested_urls.append(url)
            if len(requested_urls) == 1:
                return {"value": [{"id": "opaque-meeting-id"}]}
            return {"value": []}

        client = GraphTranscriptDiscoveryClient(
            access_token="token",
            api_base_url="https://graph.example/v1.0",
            fetch_json=fetch_json,
        )

        artifacts = client.discover_artifacts(
            meeting=_meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
        )

        self.assertEqual(
            requested_urls,
            [
                "https://graph.example/v1.0/me/onlineMeetings?%24filter=JoinWebUrl+eq+%27"
                "https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253Ameeting_graph123%2540thread.v2%2F0"
                "%3Fcontext%3D%257B%257D%27",
                "https://graph.example/v1.0/me/onlineMeetings/opaque-meeting-id/transcripts",
            ],
        )
        self.assertEqual(artifacts[0].source_name, "Teams transcript text")
        self.assertEqual(artifacts[0].status, "missing")
        self.assertEqual(artifacts[0].detail, "Graph transcript discovery returned no transcript records.")

    def test_graph_transcript_discovery_marks_404_missing(self) -> None:
        client = GraphTranscriptDiscoveryClient(
            access_token="token",
            fetch_json=lambda url, token: (_ for _ in ()).throw(_SyntheticHTTPError(url, 404, "Not Found")),
        )

        artifacts = client.discover_artifacts(
            meeting=_meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
        )

        self.assertEqual(artifacts[0].source_name, "Teams transcript text")
        self.assertEqual(artifacts[0].status, "missing")
        self.assertEqual(artifacts[0].detail, "Graph did not find transcripts for this Teams meeting.")

    def test_graph_transcript_discovery_marks_permission_errors_blocked(self) -> None:
        client = GraphTranscriptDiscoveryClient(
            access_token="token",
            fetch_json=lambda url, token: (_ for _ in ()).throw(_SyntheticHTTPError(url, 403, "Forbidden")),
        )

        artifacts = client.discover_artifacts(
            meeting=_meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
        )

        self.assertEqual(artifacts[0].source_name, "Teams transcript text")
        self.assertEqual(artifacts[0].status, "permission_blocked")
        self.assertEqual(artifacts[0].detail, "Graph transcript discovery failed with HTTP 403.")

    def test_graph_transcript_discovery_marks_bad_payload_not_attempted(self) -> None:
        client = GraphTranscriptDiscoveryClient(
            access_token="token",
            fetch_json=lambda url, token: {"unexpected": []},
        )

        artifacts = client.discover_artifacts(
            meeting=_meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
        )

        self.assertEqual(artifacts[0].source_name, "Teams transcript text")
        self.assertEqual(artifacts[0].status, "not_attempted")
        self.assertIn("Graph online meeting lookup response did not contain a value list.", artifacts[0].detail or "")

    def test_graph_transcript_discovery_does_not_attempt_without_teams_meeting_id(self) -> None:
        client = GraphTranscriptDiscoveryClient(
            access_token="token",
            fetch_json=lambda url, token: self.fail("fetch_json should not be called without a Teams meeting ID"),
        )

        artifacts = client.discover_artifacts(meeting=_meeting(event_id="evt-1", subject="Platform Sync"))

        self.assertEqual(artifacts[0].source_name, "Teams transcript text")
        self.assertEqual(artifacts[0].status, "not_attempted")
        self.assertEqual(artifacts[0].detail, "Outlook metadata did not include a Teams join URL.")

    def test_graph_transcript_discovery_marks_missing_when_online_meeting_lookup_finds_nothing(self) -> None:
        requested_urls: list[str] = []

        def fetch_json(url: str, token: str) -> dict[str, object]:
            requested_urls.append(url)
            return {"value": []}

        client = GraphTranscriptDiscoveryClient(
            access_token="token",
            api_base_url="https://graph.example/v1.0",
            fetch_json=fetch_json,
        )

        artifacts = client.discover_artifacts(
            meeting=_meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/meet/217922761958715?p=ytyUGXxNGsma23IKHs",
                online_meeting_provider="teamsForBusiness",
            )
        )

        self.assertEqual(
            requested_urls,
            [
                "https://graph.example/v1.0/me/onlineMeetings?%24filter=JoinWebUrl+eq+%27"
                "https%3A%2F%2Fteams.microsoft.com%2Fmeet%2F217922761958715%3Fp%3DytyUGXxNGsma23IKHs%27"
            ],
        )
        self.assertEqual(artifacts[0].status, "missing")
        self.assertEqual(artifacts[0].detail, "Graph did not find an online meeting record for this Outlook event.")

    def test_graph_transcript_download_writes_vtt_and_marks_processor_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            requested_json_urls: list[str] = []
            requested_content_urls: list[str] = []

            def fetch_json(url: str, token: str) -> dict[str, object]:
                requested_json_urls.append(url)
                self.assertEqual(token, "token")
                if len(requested_json_urls) == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {
                    "value": [
                        {
                            "id": "transcript 1",
                            "createdDateTime": "2026-05-04T13:05:00Z",
                        }
                    ]
                }

            def fetch_bytes(url: str, token: str) -> bytes:
                requested_content_urls.append(url)
                self.assertEqual(token, "token")
                return b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Morgan>Hello</v>"

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            )
            meeting = _meeting(
                event_id="evt-1",
                subject="Platform Sync",
                response_status="accepted",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
            artifacts = client.discover_artifacts(meeting=meeting)
            transcript_path = intake_root / "bundles" / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"

            self.assertEqual(
                requested_json_urls,
                [
                    "https://graph.example/v1.0/me/onlineMeetings?%24filter=JoinWebUrl+eq+%27"
                    "https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253Ameeting_graph123%2540thread.v2%2F0"
                    "%3Fcontext%3D%257B%257D%27",
                    "https://graph.example/v1.0/me/onlineMeetings/opaque-meeting-id/transcripts",
                ],
            )
            self.assertEqual(
                requested_content_urls,
                [
                    "https://graph.example/v1.0/me/onlineMeetings/"
                    "opaque-meeting-id/transcripts/transcript%201/content?%24format=text%2Fvtt"
                ],
            )
            self.assertEqual(artifacts[0].source_name, "Teams .vtt transcript")
            self.assertEqual(artifacts[0].status, "available")
            self.assertEqual(artifacts[0].matched_paths, (transcript_path,))
            self.assertEqual(artifacts[0].occurrence_validated_paths, (transcript_path,))
            self.assertIsNotNone(
                transcript_provenance.matching_provenance(
                    transcript_path,
                    event_id=meeting.event_id,
                    occurrence_start_at=meeting.start_at,
                    occurrence_end_at=meeting.end_at,
                )
            )
            self.assertFalse((intake_root / "2026-05-04 - Teams - Platform Sync.vtt").exists())
            self.assertEqual(
                transcript_path.read_text(encoding="utf-8"),
                "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Morgan>Hello</v>\n",
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=client,
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            bundle_note = plan.items[0].intake_bundle_note
            assert bundle_note is not None
            self.assertEqual(bundle_note.processor_input_path, transcript_path)
            self.assertEqual(bundle_note.processor_input_source_name, "Teams .vtt transcript")

    def test_graph_transcript_download_selects_record_for_current_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            requested_json_urls: list[str] = []
            requested_content_urls: list[str] = []

            def fetch_json(url: str, token: str) -> dict[str, object]:
                requested_json_urls.append(url)
                self.assertEqual(token, "token")
                if len(requested_json_urls) == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {
                    "value": [
                        {"id": "july-8", "createdDateTime": "2026-07-08T17:25:00Z"},
                        {"id": "july-17", "createdDateTime": "2026-07-17T17:25:00Z"},
                    ]
                }

            def fetch_bytes(url: str, token: str) -> bytes:
                requested_content_urls.append(url)
                self.assertEqual(token, "token")
                if "/july-17/" in url:
                    return b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCurrent occurrence"
                return b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nOlder occurrence"

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            )
            meeting = OutlookMeetingCandidate(
                event_id="evt-recurring",
                subject="Recurring Platform Sync",
                start_at=datetime.fromisoformat("2026-07-17T17:00:00+00:00"),
                end_at=datetime.fromisoformat("2026-07-17T17:25:00+00:00"),
                response_status="accepted",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )

            artifacts = client.discover_artifacts(meeting=meeting)
            transcript_path = (
                intake_root / "bundles" / "raw_transcripts" / "2026-07-17 - Teams - Recurring Platform Sync.vtt"
            )

            self.assertEqual(
                requested_content_urls,
                [
                    "https://graph.example/v1.0/me/onlineMeetings/opaque-meeting-id/"
                    "transcripts/july-17/content?%24format=text%2Fvtt"
                ],
            )
            self.assertEqual(artifacts[0].status, "available")
            self.assertEqual(
                transcript_path.read_text(encoding="utf-8"),
                "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCurrent occurrence\n",
            )

    def test_graph_transcript_download_selects_current_occurrence_from_later_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            requested_json_urls: list[str] = []
            requested_content_urls: list[str] = []
            page_two_url = "https://graph.example/v1.0/transcripts?page=2"

            def fetch_json(url: str, token: str) -> dict[str, object]:
                requested_json_urls.append(url)
                self.assertEqual(token, "token")
                if len(requested_json_urls) == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                if url == page_two_url:
                    return {"value": [{"id": "july-17", "createdDateTime": "2026-07-17T17:25:00Z"}]}
                return {
                    "value": [{"id": "july-8", "createdDateTime": "2026-07-08T17:25:00Z"}],
                    "@odata.nextLink": page_two_url,
                }

            def fetch_bytes(url: str, token: str) -> bytes:
                requested_content_urls.append(url)
                self.assertEqual(token, "token")
                return b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCurrent occurrence"

            artifacts = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=Path(tmp_dir) / "00_Intake",
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            ).discover_artifacts(meeting=_recurring_meeting())

            self.assertIn(page_two_url, requested_json_urls)
            self.assertEqual(len(requested_content_urls), 1)
            self.assertIn("/transcripts/july-17/content?", requested_content_urls[0])
            self.assertEqual(artifacts[0].status, "available")
            assert artifacts[0].diagnostics is not None
            self.assertEqual(artifacts[0].diagnostics.candidate_count, 2)

    def test_graph_transcript_download_replaces_unproven_stale_managed_vtt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            transcript_path = (
                intake_root / "bundles" / "raw_transcripts" / "2026-07-17 - Teams - Recurring Platform Sync.vtt"
            )
            old_content = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nOLD\n"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_bytes(old_content)
            json_call_count = 0

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                self.assertEqual(token, "token")
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {
                    "value": [
                        {"id": "july-8", "createdDateTime": "2026-07-08T17:25:00Z"},
                        {"id": "july-17", "createdDateTime": "2026-07-17T17:25:00Z"},
                        {"id": "july-24", "createdDateTime": "2026-07-24T17:25:00Z"},
                    ]
                }

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=lambda url, token: b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNEW",
            )
            meeting = OutlookMeetingCandidate(
                event_id="evt-recurring",
                subject="Recurring Platform Sync",
                start_at=datetime.fromisoformat("2026-07-17T17:00:00+00:00"),
                end_at=datetime.fromisoformat("2026-07-17T17:25:00+00:00"),
                response_status="accepted",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )

            artifacts = client.discover_artifacts(meeting=meeting)

            old_hash = meeting_sync_module.hashlib.sha256(old_content).hexdigest()
            archive_path = (
                intake_root
                / "bundles"
                / "fallbacks"
                / "stale_transcripts"
                / f"{transcript_path.stem}-{old_hash[:8]}.vtt"
            )
            provenance_path = transcript_path.with_suffix(".vtt.provenance.json")
            self.assertEqual(
                transcript_path.read_bytes(),
                b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNEW\n",
            )
            self.assertEqual(archive_path.read_bytes(), old_content)
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(provenance),
                {
                    "schema_version",
                    "event_id",
                    "occurrence_start_at",
                    "occurrence_end_at",
                    "transcripts",
                    "content_sha256",
                    "downloaded_at",
                },
            )
            self.assertEqual(provenance["schema_version"], 1)
            self.assertEqual(provenance["event_id"], "evt-recurring")
            self.assertEqual(provenance["occurrence_start_at"], "2026-07-17T17:00:00+00:00")
            self.assertEqual(provenance["occurrence_end_at"], "2026-07-17T17:25:00+00:00")
            self.assertEqual(provenance["transcripts"], [{"id": "july-17", "created_at": "2026-07-17T17:25:00+00:00"}])
            self.assertEqual(
                provenance["content_sha256"],
                meeting_sync_module.hashlib.sha256(transcript_path.read_bytes()).hexdigest(),
            )
            self.assertIsInstance(provenance["downloaded_at"], str)
            self.assertEqual(artifacts[0].status, "available")
            self.assertEqual(artifacts[0].matched_paths, (transcript_path,))
            assert artifacts[0].diagnostics is not None
            self.assertEqual(artifacts[0].diagnostics.candidate_count, 3)
            self.assertEqual(
                [item.transcript_id for item in artifacts[0].diagnostics.selected_transcripts],
                ["july-17"],
            )

            rerun_artifacts = client.discover_artifacts(meeting=meeting)

            self.assertEqual(json_call_count, 2)
            self.assertEqual(list(archive_path.parent.glob("*.vtt")), [archive_path])
            self.assertIn("Validated", rerun_artifacts[0].detail or "")
            assert rerun_artifacts[0].diagnostics is not None
            self.assertEqual(rerun_artifacts[0].diagnostics.candidate_count, 1)

            rerun_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(artifacts=rerun_artifacts),
                since=date(2026, 7, 17),
                now=datetime.fromisoformat("2026-07-17T18:00:00+00:00"),
            )
            self.assertIn("  transcript_candidates: 1", render_transcript_sync_plan(rerun_plan))
            metadata = json.loads(render_outlook_metadata_sidecar(meeting=meeting, bundle=rerun_plan.items[0].bundle))
            self.assertEqual(metadata["artifacts"][0]["transcript_diagnostics"]["candidate_count"], 1)

    def test_graph_transcript_download_short_circuits_with_matching_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _recurring_meeting()
            transcript_path = intake_root / meeting_sync_module._transcript_relative_path(meeting)
            content = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCURRENT\n"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_bytes(content)
            transcript_provenance.write_provenance(
                transcript_path,
                event_id=meeting.event_id,
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
                transcripts=[{"id": "july-17", "created_at": "2026-07-17T17:25:00+00:00"}],
                content=content,
            )

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                fetch_json=lambda url, token: self.fail("valid provenance must short-circuit Graph discovery"),
                fetch_bytes=lambda url, token: self.fail("valid provenance must short-circuit content download"),
            )

            artifacts = client.discover_artifacts(meeting=meeting)

            self.assertEqual(artifacts[0].status, "available")
            self.assertEqual(artifacts[0].matched_paths, (transcript_path,))
            self.assertIn("july-17", artifacts[0].detail or "")
            self.assertEqual(
                artifacts[0].diagnostics,
                TranscriptDiagnostics(
                    candidate_count=1,
                    selected_transcripts=(
                        SelectedTranscriptDiagnostic("july-17", datetime.fromisoformat("2026-07-17T17:25:00+00:00")),
                    ),
                    occurrence_start_at=meeting.start_at,
                    occurrence_end_at=meeting.end_at,
                    local_action="validated",
                ),
            )

    def test_graph_transcript_provenance_fast_path_rejects_new_overlapping_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            first = _recurring_meeting()
            second = replace(
                first,
                event_id="evt-recurring-overlap",
                start_at=datetime.fromisoformat("2026-07-17T17:30:00+00:00"),
                end_at=datetime.fromisoformat("2026-07-17T17:55:00+00:00"),
            )
            contextual_first = meeting_sync_module._meetings_with_occurrence_context((first, second))[0]
            transcript_path = intake_root / meeting_sync_module._transcript_relative_path(first)
            content = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCURRENT\n"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_bytes(content)
            transcript_provenance.write_provenance(
                transcript_path,
                event_id=first.event_id,
                occurrence_start_at=first.start_at,
                occurrence_end_at=first.end_at,
                transcripts=[{"id": "overlapping-segment", "created_at": "2026-07-17T17:25:00+00:00"}],
                content=content,
            )
            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                fetch_json=lambda _url, _token: self.fail("validated provenance must not trigger Graph discovery"),
                fetch_bytes=lambda _url, _token: self.fail("validated provenance must not trigger content download"),
            )

            artifacts = client.discover_artifacts(meeting=contextual_first)

            self.assertEqual(artifacts[0].status, "missing")
            self.assertEqual(artifacts[0].occurrence_validated_paths, ())
            assert artifacts[0].diagnostics is not None
            self.assertEqual(artifacts[0].diagnostics.selected_transcripts, ())
            self.assertEqual(len(artifacts[0].diagnostics.assignment_conflicts), 1)
            self.assertEqual(
                artifacts[0].diagnostics.assignment_conflicts[0].occurrence_ids,
                (first.event_id, second.event_id),
            )

            marker_path = meeting_sync_module._meeting_identity_path(first, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-07-18T17:25:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(first,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(artifacts=artifacts),
                since=date(2026, 7, 17),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-07-17T18:00:00+00:00"),
            )

            self.assertEqual(plan.items[0].decision, "skip")
            self.assertIsNone(plan.items[0].intake_bundle_note)
            self.assertIn(
                "meeting_sync_occurrence_error: ambiguous_transcript_assignment",
                render_transcript_sync_plan(plan),
            )

    def test_graph_transcript_text_provenance_fast_path_rejects_new_overlapping_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            first = _recurring_meeting()
            second = replace(
                first,
                event_id="evt-recurring-overlap",
                start_at=datetime.fromisoformat("2026-07-17T17:30:00+00:00"),
                end_at=datetime.fromisoformat("2026-07-17T17:55:00+00:00"),
            )
            contextual_first = meeting_sync_module._meetings_with_occurrence_context((first, second))[0]
            transcript_path = intake_root / meeting_sync_module._transcript_text_relative_path(first)
            content = b"# Transcript\n\nOverlapping segment.\n"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_bytes(content)
            transcript_provenance.write_provenance(
                transcript_path,
                event_id=first.event_id,
                occurrence_start_at=first.start_at,
                occurrence_end_at=first.end_at,
                transcripts=[{"id": "overlapping-segment", "created_at": "2026-07-17T17:25:00+00:00"}],
                content=content,
            )
            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                fetch_json=lambda _url, _token: self.fail("validated provenance must not trigger Graph discovery"),
                fetch_bytes=lambda _url, _token: self.fail("validated provenance must not trigger content download"),
            )

            artifacts = client.discover_artifacts(meeting=contextual_first)
            transcript_artifact = artifacts[1]

            self.assertEqual(transcript_artifact.status, "missing")
            self.assertEqual(transcript_artifact.occurrence_validated_paths, ())
            assert transcript_artifact.diagnostics is not None
            self.assertEqual(transcript_artifact.diagnostics.selected_transcripts, ())
            self.assertEqual(len(transcript_artifact.diagnostics.assignment_conflicts), 1)
            self.assertEqual(
                transcript_artifact.diagnostics.assignment_conflicts[0].occurrence_ids,
                (first.event_id, second.event_id),
            )

            marker_path = meeting_sync_module._meeting_identity_path(first, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-07-18T17:25:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(first,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(artifacts=artifacts),
                since=date(2026, 7, 17),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-07-17T18:00:00+00:00"),
            )

            self.assertEqual(plan.items[0].decision, "skip")
            self.assertIsNone(plan.items[0].intake_bundle_note)
            self.assertIn(
                "meeting_sync_occurrence_error: ambiguous_transcript_assignment",
                render_transcript_sync_plan(plan),
            )

    def test_graph_transcript_text_provenance_without_timestamp_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _recurring_meeting()
            transcript_path = intake_root / meeting_sync_module._transcript_text_relative_path(meeting)
            content = b"# Transcript\n\nMissing timestamp.\n"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_bytes(content)
            transcript_provenance.write_provenance(
                transcript_path,
                event_id=meeting.event_id,
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
                transcripts=[{"id": "missing-timestamp", "created_at": None}],
                content=content,
            )
            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                fetch_json=lambda _url, _token: self.fail("validated provenance must not trigger Graph discovery"),
                fetch_bytes=lambda _url, _token: self.fail("validated provenance must not trigger content download"),
            )

            transcript_artifact = client.discover_artifacts(meeting=meeting)[1]

            self.assertEqual(transcript_artifact.status, "missing")
            self.assertEqual(transcript_artifact.matched_paths, ())
            self.assertEqual(transcript_artifact.occurrence_validated_paths, ())
            assert transcript_artifact.diagnostics is not None
            self.assertEqual(transcript_artifact.diagnostics.candidate_count, 1)
            self.assertEqual(transcript_artifact.diagnostics.selected_transcripts, ())

    def test_graph_transcript_repair_orders_archive_before_target_before_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _recurring_meeting()
            transcript_path = intake_root / meeting_sync_module._transcript_relative_path(meeting)
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_bytes(b"WEBVTT\n\nOLD\n")
            json_call_count = 0
            events: list[str] = []
            real_archive = transcript_provenance.archive_stale_transcript
            real_atomic_write = transcript_provenance.atomic_write_bytes
            real_write_provenance = transcript_provenance.write_provenance

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {"value": [{"id": "july-17", "createdDateTime": "2026-07-17T17:25:00Z"}]}

            def archive(path: Path, *, archive_dir: Path, content: bytes) -> Path:
                events.append("archive")
                return real_archive(path, archive_dir=archive_dir, content=content)

            def atomic_write(path: Path, content: bytes) -> None:
                events.append("target")
                real_atomic_write(path, content)

            def write_provenance(
                path: Path,
                *,
                event_id: str,
                occurrence_start_at: datetime,
                occurrence_end_at: datetime,
                transcripts: Sequence[Mapping[str, object]],
                content: bytes,
            ) -> None:
                events.append("provenance")
                real_write_provenance(
                    path,
                    event_id=event_id,
                    occurrence_start_at=occurrence_start_at,
                    occurrence_end_at=occurrence_end_at,
                    transcripts=transcripts,
                    content=content,
                )

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=lambda url, token: b"WEBVTT\n\nNEW\n",
            )

            with (
                patch.object(meeting_sync_module, "archive_stale_transcript", side_effect=archive),
                patch.object(meeting_sync_module, "atomic_write_bytes", side_effect=atomic_write),
                patch.object(meeting_sync_module, "write_provenance", side_effect=write_provenance),
            ):
                client.discover_artifacts(meeting=meeting)

            self.assertEqual(events, ["archive", "target", "provenance"])

    def test_graph_transcript_download_backfills_provenance_for_matching_legacy_vtt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _recurring_meeting()
            transcript_path = intake_root / meeting_sync_module._transcript_relative_path(meeting)
            content = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCURRENT\n"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_bytes(content)
            json_call_count = 0

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {"value": [{"id": "july-17", "createdDateTime": "2026-07-17T17:25:00Z"}]}

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=lambda url, token: content.rstrip(b"\n"),
            )

            artifacts = client.discover_artifacts(meeting=meeting)

            self.assertEqual(transcript_path.read_bytes(), content)
            self.assertTrue(transcript_provenance.provenance_path(transcript_path).is_file())
            self.assertFalse((intake_root / "bundles" / "fallbacks" / "stale_transcripts").exists())
            self.assertIn("Backfilled", artifacts[0].detail or "")
            assert artifacts[0].diagnostics is not None
            self.assertEqual(artifacts[0].diagnostics.local_action, "provenance_backfilled")

    def test_graph_transcript_download_preserves_unvalidated_vtt_when_occurrence_has_no_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _recurring_meeting()
            transcript_path = intake_root / meeting_sync_module._transcript_relative_path(meeting)
            old_content = b"WEBVTT\n\nOLD\n"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_bytes(old_content)
            json_call_count = 0

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {"value": [{"id": "july-8", "createdDateTime": "2026-07-08T17:25:00Z"}]}

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=lambda url, token: self.fail("nonmatching transcript must not download"),
            )

            artifacts = client.discover_artifacts(meeting=meeting)

            self.assertEqual(transcript_path.read_bytes(), old_content)
            self.assertEqual([artifact.status for artifact in artifacts], ["missing", "missing"])
            self.assertTrue(all(not artifact.matched_paths for artifact in artifacts))
            self.assertFalse(transcript_provenance.provenance_path(transcript_path).exists())

    def test_graph_transcript_download_repairs_tampered_vtt_after_provenance_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _recurring_meeting()
            transcript_path = intake_root / meeting_sync_module._transcript_relative_path(meeting)
            expected_content = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCURRENT\n"
            tampered_content = b"WEBVTT\n\nTAMPERED\n"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_bytes(expected_content)
            transcript_provenance.write_provenance(
                transcript_path,
                event_id=meeting.event_id,
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
                transcripts=[{"id": "july-17", "created_at": "2026-07-17T17:25:00+00:00"}],
                content=expected_content,
            )
            transcript_path.write_bytes(tampered_content)
            json_call_count = 0

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {"value": [{"id": "july-17", "createdDateTime": "2026-07-17T17:25:00Z"}]}

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=lambda url, token: expected_content,
            )

            artifacts = client.discover_artifacts(meeting=meeting)

            tampered_hash = meeting_sync_module.hashlib.sha256(tampered_content).hexdigest()[:8]
            archive_path = (
                intake_root
                / "bundles"
                / "fallbacks"
                / "stale_transcripts"
                / f"{transcript_path.stem}-{tampered_hash}.vtt"
            )
            self.assertEqual(transcript_path.read_bytes(), expected_content)
            self.assertEqual(archive_path.read_bytes(), tampered_content)
            self.assertIn("Replaced stale", artifacts[0].detail or "")
            assert artifacts[0].diagnostics is not None
            self.assertEqual(artifacts[0].diagnostics.local_action, "stale_preserved_and_replaced")

    def test_graph_transcript_download_merges_current_occurrence_segments_chronologically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            requested_content_urls: list[str] = []
            json_call_count = 0

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                self.assertEqual(token, "token")
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {
                    "value": [
                        {"id": "segment-2", "createdDateTime": "2026-07-17T17:20:00Z"},
                        {"id": "segment-1", "createdDateTime": "2026-07-17T17:05:00Z"},
                    ]
                }

            def fetch_bytes(url: str, token: str) -> bytes:
                requested_content_urls.append(url)
                self.assertEqual(token, "token")
                if "/segment-1/" in url:
                    return b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nFirst segment\n"
                return b"WEBVTT\n\n00:00:03.000 --> 00:00:04.000\nSecond segment\n"

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            )
            meeting = OutlookMeetingCandidate(
                event_id="evt-recurring",
                subject="Recurring Platform Sync",
                start_at=datetime.fromisoformat("2026-07-17T17:00:00+00:00"),
                end_at=datetime.fromisoformat("2026-07-17T17:25:00+00:00"),
                response_status="accepted",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )

            artifacts = client.discover_artifacts(meeting=meeting)
            transcript_path = (
                intake_root / "bundles" / "raw_transcripts" / "2026-07-17 - Teams - Recurring Platform Sync.vtt"
            )
            transcript_content = transcript_path.read_text(encoding="utf-8")

            self.assertEqual(
                requested_content_urls,
                [
                    "https://graph.example/v1.0/me/onlineMeetings/opaque-meeting-id/"
                    "transcripts/segment-1/content?%24format=text%2Fvtt",
                    "https://graph.example/v1.0/me/onlineMeetings/opaque-meeting-id/"
                    "transcripts/segment-2/content?%24format=text%2Fvtt",
                ],
            )
            self.assertEqual(artifacts[0].status, "available")
            self.assertEqual(transcript_content.count("WEBVTT"), 1)
            self.assertLess(transcript_content.index("First segment"), transcript_content.index("Second segment"))
            assert artifacts[0].diagnostics is not None
            self.assertEqual(artifacts[0].diagnostics.candidate_count, 2)
            self.assertEqual(
                [item.transcript_id for item in artifacts[0].diagnostics.selected_transcripts],
                ["segment-1", "segment-2"],
            )
            self.assertEqual(artifacts[0].diagnostics.local_action, "downloaded")

    def test_merged_vtt_bytes_strips_signature_header_text_and_metadata(self) -> None:
        content = meeting_sync_module._merged_vtt_bytes(
            [
                b"\xef\xbb\xbfWEBVTT - Segment 1\nKind: captions\nLanguage: en\n\n"
                b"00:00:01.000 --> 00:00:02.000\nFirst segment\n",
                b"WEBVTT\t- Segment 2\nKind: captions\nLanguage: en\n\n00:00:03.000 --> 00:00:04.000\nSecond segment\n",
            ]
        )

        self.assertEqual(
            content,
            b"WEBVTT\n\n"
            b"00:00:01.000 --> 00:00:02.000\nFirst segment\n\n"
            b"00:00:03.000 --> 00:00:04.000\nSecond segment\n",
        )

    def test_graph_transcript_download_returns_missing_when_no_timestamp_matches_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_call_count = 0
            requested_content_urls: list[str] = []

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                self.assertEqual(token, "token")
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {"value": [{"id": "older-record", "createdDateTime": "2026-04-27T13:30:00Z"}]}

            def fetch_bytes(url: str, token: str) -> bytes:
                requested_content_urls.append(url)
                return b"WEBVTT\n"

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=Path(tmp_dir) / "00_Intake",
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            )

            artifacts = client.discover_artifacts(
                meeting=_meeting(
                    event_id="evt-1",
                    subject="Platform Sync",
                    join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                    online_meeting_provider="teamsForBusiness",
                )
            )

            self.assertEqual(requested_content_urls, [])
            self.assertEqual([artifact.status for artifact in artifacts], ["missing", "missing"])
            self.assertEqual(
                artifacts[0].diagnostics,
                TranscriptDiagnostics(
                    candidate_count=1,
                    selected_transcripts=(),
                    occurrence_start_at=datetime.fromisoformat("2026-05-04T13:00:00+00:00"),
                    occurrence_end_at=datetime.fromisoformat("2026-05-04T13:30:00+00:00"),
                    local_action="none",
                ),
            )

    def test_graph_transcript_download_returns_missing_for_multiple_timestamp_free_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_call_count = 0
            requested_content_urls: list[str] = []

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                self.assertEqual(token, "token")
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {"value": [{"id": "ambiguous-1"}, {"id": "ambiguous-2"}]}

            def fetch_bytes(url: str, token: str) -> bytes:
                requested_content_urls.append(url)
                return b"WEBVTT\n"

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=Path(tmp_dir) / "00_Intake",
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            )

            artifacts = client.discover_artifacts(
                meeting=_meeting(
                    event_id="evt-1",
                    subject="Platform Sync",
                    join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                    online_meeting_provider="teamsForBusiness",
                )
            )

            self.assertEqual(requested_content_urls, [])
            self.assertEqual([artifact.status for artifact in artifacts], ["missing", "missing"])

    def test_graph_transcript_download_rejects_timestamp_free_record_among_nonmatching_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_call_count = 0
            requested_content_urls: list[str] = []

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                self.assertEqual(token, "token")
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {
                    "value": [
                        {"id": "older-record", "createdDateTime": "2026-04-27T13:30:00Z"},
                        {"id": "timestamp-free"},
                    ]
                }

            def fetch_bytes(url: str, token: str) -> bytes:
                requested_content_urls.append(url)
                self.assertEqual(token, "token")
                return b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCompatible record"

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=Path(tmp_dir) / "00_Intake",
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            )

            artifacts = client.discover_artifacts(
                meeting=_meeting(
                    event_id="evt-1",
                    subject="Platform Sync",
                    join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                    online_meeting_provider="teamsForBusiness",
                )
            )

            self.assertEqual(requested_content_urls, [])
            self.assertEqual([artifact.status for artifact in artifacts], ["missing", "missing"])

    def test_graph_transcript_download_excludes_malformed_timestamp_from_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_call_count = 0
            requested_content_urls: list[str] = []

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                self.assertEqual(token, "token")
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {
                    "value": [
                        {"id": "older-record", "createdDateTime": "2026-04-27T13:30:00Z"},
                        {"id": "malformed-record", "createdDateTime": "not-a-date"},
                    ]
                }

            def fetch_bytes(url: str, token: str) -> bytes:
                requested_content_urls.append(url)
                self.assertEqual(token, "token")
                return b"WEBVTT\n"

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=Path(tmp_dir) / "00_Intake",
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            )

            artifacts = client.discover_artifacts(
                meeting=_meeting(
                    event_id="evt-1",
                    subject="Platform Sync",
                    join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                    online_meeting_provider="teamsForBusiness",
                )
            )

            self.assertEqual(requested_content_urls, [])
            self.assertEqual([artifact.status for artifact in artifacts], ["missing", "missing"])

    def test_graph_transcript_download_rejects_single_timestamp_free_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_call_count = 0
            requested_content_urls: list[str] = []

            def fetch_json(url: str, token: str) -> dict[str, object]:
                nonlocal json_call_count
                json_call_count += 1
                self.assertEqual(token, "token")
                if json_call_count == 1:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                return {"value": [{"id": "timestamp-free"}]}

            def fetch_bytes(url: str, token: str) -> bytes:
                requested_content_urls.append(url)
                self.assertEqual(token, "token")
                return b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCompatible record"

            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=Path(tmp_dir) / "00_Intake",
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            )

            artifacts = client.discover_artifacts(
                meeting=_meeting(
                    event_id="evt-1",
                    subject="Platform Sync",
                    join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                    online_meeting_provider="teamsForBusiness",
                )
            )

            self.assertEqual(requested_content_urls, [])
            self.assertEqual([artifact.status for artifact in artifacts], ["missing", "missing"])

    def test_graph_transcript_download_preserves_opaque_online_meeting_id_path_characters(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        requested_json_urls: list[str] = []

        def fetch_json(url: str, token: str) -> dict[str, object]:
            requested_json_urls.append(url)
            self.assertEqual(token, "token")
            if len(requested_json_urls) == 1:
                return {
                    "value": [
                        {"id": "MSo1Yjk0ZTNiNC1lMWMyLTRmMGMtOGYyNi1lNTE4YTk5NjMyMjIqMCoqMTk6bWVldGluZ19hYmNAthread.v2"}
                    ]
                }
            return {"value": []}

        client = GraphTranscriptDownloadClient(
            access_token="token",
            intake_root=Path(tmp_dir.name) / "00_Intake",
            api_base_url="https://graph.example/v1.0",
            fetch_json=fetch_json,
            fetch_bytes=lambda url, token: self.fail("fetch_bytes should not be called when no transcripts exist"),
        )

        artifacts = client.discover_artifacts(
            meeting=_meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
        )

        self.assertEqual(
            requested_json_urls,
            [
                "https://graph.example/v1.0/me/onlineMeetings?%24filter=JoinWebUrl+eq+%27"
                "https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253Ameeting_graph123%2540thread.v2%2F0"
                "%3Fcontext%3D%257B%257D%27",
                "https://graph.example/v1.0/me/onlineMeetings/"
                "MSo1Yjk0ZTNiNC1lMWMyLTRmMGMtOGYyNi1lNTE4YTk5NjMyMjIqMCoqMTk6bWVldGluZ19hYmNAthread.v2/transcripts",
            ],
        )
        self.assertEqual(artifacts[0].status, "missing")
        self.assertEqual(artifacts[0].detail, "Graph transcript discovery returned no transcript records.")
        assert artifacts[0].diagnostics is not None
        self.assertEqual(artifacts[0].diagnostics.candidate_count, 0)
        self.assertEqual(artifacts[0].diagnostics.selected_transcripts, ())
        self.assertEqual(artifacts[0].diagnostics.local_action, "none")

    def test_graph_transcript_download_rejects_proven_vtt_without_assignment_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            transcript_path = intake_root / "bundles" / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            meeting = _meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
            transcript_provenance.write_provenance(
                transcript_path,
                event_id=meeting.event_id,
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
                transcripts=[{"id": "transcript-1", "created_at": None}],
                content=transcript_path.read_bytes(),
            )
            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                fetch_json=lambda url, token: self.fail("fetch_json should not be called for existing VTT"),
                fetch_bytes=lambda url, token: self.fail("fetch_bytes should not be called for existing VTT"),
            )

            artifacts = client.discover_artifacts(meeting=meeting)

            self.assertEqual(artifacts[0].source_name, "Teams .vtt transcript")
            self.assertEqual(artifacts[0].status, "missing")
            self.assertEqual(artifacts[0].matched_paths, ())
            self.assertEqual(artifacts[0].occurrence_validated_paths, ())
            assert artifacts[0].diagnostics is not None
            self.assertEqual(artifacts[0].diagnostics.candidate_count, 1)
            self.assertEqual(artifacts[0].diagnostics.selected_transcripts, ())

    def test_graph_transcript_download_marks_content_permission_blocked(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = GraphTranscriptDownloadClient(
            access_token="token",
            intake_root=Path(tmp_dir.name) / "00_Intake",
            fetch_json=lambda url, token: {
                "value": [
                    {
                        "id": "transcript-1",
                        "createdDateTime": "2026-05-04T13:05:00Z",
                    }
                ]
            },
            fetch_bytes=lambda url, token: (_ for _ in ()).throw(_SyntheticHTTPError(url, 403, "Forbidden")),
        )

        artifacts = client.discover_artifacts(
            meeting=_meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
        )

        self.assertEqual(artifacts[0].source_name, "Teams .vtt transcript")
        self.assertEqual(artifacts[0].status, "permission_blocked")
        self.assertEqual(artifacts[0].detail, "Graph transcript content download failed with HTTP 403.")
        assert artifacts[0].diagnostics is not None
        self.assertEqual(artifacts[0].diagnostics.candidate_count, 1)
        self.assertEqual(artifacts[0].diagnostics.selected_transcripts[0].transcript_id, "transcript-1")
        self.assertEqual(artifacts[0].diagnostics.local_action, "none")

    def test_graph_transcript_download_includes_graph_400_message(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = GraphTranscriptDownloadClient(
            access_token="token",
            intake_root=Path(tmp_dir.name) / "00_Intake",
            fetch_json=lambda url, token: (_ for _ in ()).throw(
                _SyntheticHTTPError(
                    url,
                    400,
                    "Bad Request",
                    body={
                        "error": {
                            "code": "BadRequest",
                            "message": "The requested online meeting identifier is invalid.",
                        }
                    },
                )
            ),
        )

        artifacts = client.discover_artifacts(
            meeting=_meeting(
                event_id="evt-1",
                subject="Platform Sync",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
        )

        self.assertEqual(artifacts[0].source_name, "Teams .vtt transcript")
        self.assertEqual(artifacts[0].status, "not_attempted")
        self.assertEqual(
            artifacts[0].detail,
            "Graph transcript discovery failed with HTTP 400. Graph said: BadRequest: The requested online meeting identifier is invalid.",
        )

    def test_graph_meeting_fallback_summary_plans_recap_without_writing_until_bundle_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            requested_urls: list[str] = []
            meeting = _meeting(
                event_id="evt-1",
                subject="Platform Sync",
                response_status="accepted",
                organizer="Morgan",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )

            def fetch_json(url: str, token: str) -> dict[str, object]:
                requested_urls.append(url)
                self.assertEqual(token, "token")
                if url.endswith(
                    "/me/onlineMeetings?%24filter=JoinWebUrl+eq+%27https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253Ameeting_graph123%2540thread.v2%2F0%3Fcontext%3D%257B%257D%27"
                ):
                    return {"value": [{"id": "opaque-meeting-id"}]}
                if url.endswith("/me?$select=id"):
                    return {"id": "user-123"}
                if url.endswith("/copilot/users/user-123/onlineMeetings/opaque-meeting-id/aiInsights"):
                    return {"value": [{"id": "insight-1"}]}
                if url.endswith("/copilot/users/user-123/onlineMeetings/opaque-meeting-id/aiInsights/insight-1"):
                    return {
                        "id": "insight-1",
                        "meetingNotes": [{"title": "Summary", "text": "Decided to ship the fallback path."}],
                        "actionItems": [{"owner": "Matt", "text": "Validate the ingest handoff."}],
                        "contentCorrelation": {"mentionedSubjects": ["Fallback path"]},
                    }
                if url.endswith("/me/onlineMeetings/opaque-meeting-id?$select=chatInfo"):
                    return {"chatInfo": {"threadId": "19:meeting_graph123@thread.v2"}}
                if url.endswith(
                    "/chats/19:meeting_graph123@thread.v2/messages?%24top=50&%24orderby=createdDateTime+desc"
                ):
                    return {
                        "value": [
                            {
                                "from": {"user": {"displayName": "Priya"}},
                                "body": {"content": "<p>Please capture the fallback decision.</p>"},
                            }
                        ]
                    }
                self.fail(f"Unexpected Graph URL: {url}")

            client = GraphMeetingFallbackSummaryClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:30:00+00:00"),
            )
            fallback_path = intake_root / meeting_sync_module._fallback_summary_relative_path(meeting)

            recap_artifact = plan.items[0].bundle.artifact("Copilot recap / AI summary")
            chat_artifact = plan.items[0].bundle.artifact("Teams meeting chat")
            assert recap_artifact is not None
            assert chat_artifact is not None
            self.assertEqual(recap_artifact.status, "available")
            self.assertEqual(recap_artifact.matched_paths, (fallback_path,))
            self.assertEqual(chat_artifact.status, "available")
            self.assertIsNotNone(recap_artifact.planned_content)
            self.assertFalse(fallback_path.exists())

            write_planned_bundle_notes(plan)

            self.assertTrue(fallback_path.exists())
            rendered = fallback_path.read_text(encoding="utf-8")
            self.assertIn("## Summary", rendered)
            self.assertIn("Decided to ship the fallback path.", rendered)
            self.assertIn("## Action Items", rendered)
            self.assertIn("Validate the ingest handoff.", rendered)
            self.assertIn("## Meeting Chat", rendered)
            self.assertIn("Priya: Please capture the fallback decision.", rendered)
            self.assertIn("- Source Used: Copilot recap / AI summary", rendered)
            self.assertIn("- Source Used: Teams meeting chat", rendered)
            self.assertIn("- Source Used: Outlook calendar metadata", rendered)
            self.assertGreaterEqual(len(requested_urls), 5)

    def test_graph_meeting_fallback_summary_reuses_existing_file_and_preserves_chat_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            requested_urls: list[str] = []
            meeting = _meeting(
                event_id="evt-1",
                subject="Platform Sync",
                response_status="accepted",
                organizer="Morgan",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )

            def fetch_json(url: str, token: str) -> dict[str, object]:
                requested_urls.append(url)
                self.assertEqual(token, "token")
                if url.endswith(
                    "/me/onlineMeetings?%24filter=JoinWebUrl+eq+%27https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253Ameeting_graph123%2540thread.v2%2F0%3Fcontext%3D%257B%257D%27"
                ):
                    return {"value": [{"id": "opaque-meeting-id"}]}
                if url.endswith("/me?$select=id"):
                    return {"id": "user-123"}
                if url.endswith("/copilot/users/user-123/onlineMeetings/opaque-meeting-id/aiInsights"):
                    return {"value": [{"id": "insight-1"}]}
                if url.endswith("/copilot/users/user-123/onlineMeetings/opaque-meeting-id/aiInsights/insight-1"):
                    return {
                        "id": "insight-1",
                        "meetingNotes": [{"title": "Summary", "text": "Decided to ship the fallback path."}],
                    }
                if url.endswith("/me/onlineMeetings/opaque-meeting-id?$select=chatInfo"):
                    return {"chatInfo": {"threadId": "19:meeting_graph123@thread.v2"}}
                if url.endswith(
                    "/chats/19:meeting_graph123@thread.v2/messages?%24top=50&%24orderby=createdDateTime+desc"
                ):
                    return {
                        "value": [
                            {
                                "from": {"user": {"displayName": "Priya"}},
                                "body": {"content": "<p>Please capture the fallback decision.</p>"},
                            }
                        ]
                    }
                self.fail(f"Unexpected Graph URL: {url}")

            client = GraphMeetingFallbackSummaryClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
            )

            first_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:30:00+00:00"),
            )
            first_chat = first_plan.items[0].bundle.artifact("Teams meeting chat")
            assert first_chat is not None
            self.assertEqual(first_chat.status, "available")
            write_planned_bundle_notes(first_plan)
            requested_urls.clear()

            second_artifacts = client.discover_artifacts(meeting=meeting)
            artifact_by_name = {artifact.source_name: artifact for artifact in second_artifacts}

            self.assertEqual(artifact_by_name["Copilot recap / AI summary"].status, "available")
            self.assertEqual(artifact_by_name["Teams meeting chat"].status, "available")
            self.assertIn("existing fallback summary file", artifact_by_name["Teams meeting chat"].detail or "")
            self.assertEqual(requested_urls, [])

    def test_vtt_transcript_remains_primary_when_summary_fallback_is_also_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "bundles" / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            recap_path = intake_root / "bundles" / "fallbacks" / "2026-05-04 - Teams - Platform Sync.md"
            recap_path.parent.mkdir(parents=True, exist_ok=True)
            recap_path.write_text("# Recap\n", encoding="utf-8")

            artifact_client = ChainedMeetingArtifactDiscoveryClient(
                LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
                _StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            source_name="Copilot recap / AI summary",
                            status="available",
                            detail="Recap file available.",
                            matched_paths=(recap_path,),
                        ),
                    )
                ),
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=artifact_client,
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            bundle_note = plan.items[0].intake_bundle_note
            assert bundle_note is not None
            rendered_metadata = render_outlook_metadata_sidecar(
                meeting=plan.items[0].meeting, bundle=plan.items[0].bundle
            )

            self.assertEqual(bundle_note.processor_input_path, transcript_path)
            self.assertEqual(bundle_note.processor_input_source_name, "Teams .vtt transcript")
            self.assertEqual(plan.items[0].bundle.source_status("Copilot recap / AI summary"), "available")
            self.assertIn("Copilot recap / AI summary", bundle_note.sources_used)
            self.assertIn(f"- Preferred Input: `{transcript_path}`", bundle_note.content)
            self.assertIn("- Preferred Source: Teams .vtt transcript", bundle_note.content)
            self.assertIn('"source_type": "teams_vtt_transcript"', rendered_metadata)
            self.assertIn(f'"preferred_input_path": "{transcript_path}"', rendered_metadata)
            self.assertIn('"preferred_input_source_name": "Teams .vtt transcript"', rendered_metadata)

    def test_summary_fallback_bundle_records_summary_source_type_and_bundle_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            recap_path = intake_root / "bundles" / "fallbacks" / "2026-05-04 - Teams - Delivery Review.md"
            recap_path.parent.mkdir(parents=True, exist_ok=True)
            recap_path.write_text("# Recap\n", encoding="utf-8")
            meeting = _meeting(
                event_id="evt-9",
                subject="Delivery Review",
                response_status="accepted",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_delivery%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            source_name="Copilot recap / AI summary",
                            status="available",
                            detail="Fallback summary file available.",
                            matched_paths=(recap_path,),
                        ),
                        MeetingArtifact(
                            source_name="Teams meeting chat",
                            status="available",
                            detail="Chat context was captured for the summary.",
                        ),
                    )
                ),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            rendered = render_outlook_metadata_sidecar(meeting=meeting, bundle=plan.items[0].bundle)

            self.assertIn('"source_type": "copilot_recap_ai_summary"', rendered)
            self.assertIn(f'"preferred_input_path": "{recap_path}"', rendered)
            self.assertIn('"preferred_input_source_name": "Copilot recap / AI summary"', rendered)
            self.assertIn('"source_name": "Copilot recap / AI summary"', rendered)

    def test_summary_fallback_bundle_records_non_verbatim_source_limitation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            recap_path = intake_root / "bundles" / "fallbacks" / "2026-05-04 - Teams - Delivery Review.md"
            recap_path.parent.mkdir(parents=True, exist_ok=True)
            recap_path.write_text("# Recap\n", encoding="utf-8")
            meeting = _meeting(
                event_id="evt-9",
                subject="Delivery Review",
                response_status="accepted",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_delivery%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            source_name="Copilot recap / AI summary",
                            status="available",
                            detail="Downloaded Copilot recap fallback summary and wrote a local processor input.",
                            matched_paths=(recap_path,),
                        ),
                    )
                ),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:30:00+00:00"),
            )

            note = plan.items[0].intake_bundle_note
            assert note is not None

        self.assertIn("summary-derived and not a verbatim transcript", note.content)
        self.assertIn("copilot_recap_ai_summary", note.metadata_content)

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
            processor_input_path=bundle_note.processor_input_path,
            processor_input_source_name=bundle_note.processor_input_source_name,
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
        self.assertIn("## Processor Handoff", rendered)
        self.assertIn("- Preferred Input: None yet", rendered)

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
        self.assertIn('"identity_key": "evt-9|19:meeting_delivery@thread.v2"', rendered)
        self.assertIn('"outlook_event_id": "evt-9"', rendered)
        self.assertIn('"teams_meeting_id": "19:meeting_delivery@thread.v2"', rendered)
        self.assertIn('"email": "priya@example.com"', rendered)

    def test_render_outlook_metadata_sidecar_includes_matched_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            meeting = _meeting(
                event_id="evt-9",
                subject="Delivery Review",
                response_status="accepted",
                join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_delivery%40thread.v2/0?context=%7B%7D",
                online_meeting_provider="teamsForBusiness",
            )
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            rendered = render_outlook_metadata_sidecar(meeting=meeting, bundle=plan.items[0].bundle)

            self.assertIn('"artifacts": [', rendered)
            self.assertIn('"processor_handoff": {', rendered)
            self.assertIn(f'"preferred_input_path": "{transcript_path}"', rendered)
            self.assertIn('"preferred_input_source_name": "Teams .vtt transcript"', rendered)
            self.assertIn('"source_name": "Teams .vtt transcript"', rendered)
            self.assertIn(f'"matched_paths": [\n        "{transcript_path}"', rendered)

    def test_sidecars_serialize_exact_safe_transcript_diagnostic_keys(self) -> None:
        meeting = _recurring_meeting()
        diagnostics = TranscriptDiagnostics(
            candidate_count=3,
            selected_transcripts=(
                SelectedTranscriptDiagnostic("segment-2", datetime.fromisoformat("2026-07-17T17:20:00+00:00")),
                SelectedTranscriptDiagnostic("segment-1", datetime.fromisoformat("2026-07-17T17:05:00+00:00")),
            ),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="stale_preserved_and_replaced",
        )
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            artifact_discovery_client=_StubArtifactDiscoveryClient(
                artifacts=(MeetingArtifact("Teams .vtt transcript", "available", diagnostics=diagnostics),)
            ),
            since=date(2026, 7, 17),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=datetime.fromisoformat("2026-07-17T18:00:00+00:00"),
        )
        bundle = plan.items[0].bundle
        bundle_note = plan.items[0].intake_bundle_note
        assert bundle_note is not None

        metadata = json.loads(render_outlook_metadata_sidecar(meeting=meeting, bundle=bundle))
        identity = json.loads(
            render_meeting_identity_sidecar(
                meeting=meeting,
                bundle=bundle,
                bundle_note_path=bundle_note.path,
                metadata_path=bundle_note.metadata_path,
            )
        )

        expected = {
            "candidate_count": 3,
            "selected_transcripts": [
                {"id": "segment-1", "created_at": "2026-07-17T17:05:00+00:00"},
                {"id": "segment-2", "created_at": "2026-07-17T17:20:00+00:00"},
            ],
            "occurrence_start_at": "2026-07-17T17:00:00+00:00",
            "occurrence_end_at": "2026-07-17T17:25:00+00:00",
            "local_action": "stale_preserved_and_replaced",
        }
        self.assertEqual(metadata["artifacts"][0]["transcript_diagnostics"], expected)
        self.assertEqual(identity["artifact_statuses"][0]["transcript_diagnostics"], expected)
        self.assertEqual(
            set(expected),
            {"candidate_count", "selected_transcripts", "occurrence_start_at", "occurrence_end_at", "local_action"},
        )

    def test_sidecars_omit_transcript_diagnostics_when_absent(self) -> None:
        meeting = _recurring_meeting()
        artifact = MeetingArtifact("Teams .vtt transcript", "missing", "No transcript.")
        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            artifact_discovery_client=_StubArtifactDiscoveryClient(artifacts=(artifact,)),
            since=date(2026, 7, 17),
            now=datetime.fromisoformat("2026-07-17T18:00:00+00:00"),
        )

        metadata = json.loads(render_outlook_metadata_sidecar(meeting=meeting, bundle=plan.items[0].bundle))

        self.assertNotIn("transcript_diagnostics", metadata["artifacts"][0])
        self.assertNotIn("transcript_candidates:", render_transcript_sync_plan(plan))

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

        self.assertEqual(json.loads(rendered)["schema_version"], 2)
        self.assertIn('"source_type": "meeting_sync_pending"', rendered)
        self.assertIn('"outlook_event_id": "evt-9"', rendered)
        self.assertIn('"teams_meeting_id": "19:meeting_delivery@thread.v2"', rendered)
        self.assertIn('"first_seen_at": "2026-05-04T13:30:00+00:00"', rendered)
        self.assertIn('"last_checked_at":', rendered)
        self.assertIn('"retry_until": "2026-05-05T13:30:00+00:00"', rendered)
        self.assertIn('"artifact_statuses": [', rendered)
        self.assertIn(
            '"bundle_note_path": "/tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Delivery Review (bundle).md"',
            rendered,
        )

    def test_write_planned_bundle_notes_refreshes_pending_bundle_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
            meeting = _meeting(
                event_id="evt-1",
                subject="Platform Sync",
                response_status="accepted",
                join_url=(
                    "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                ),
                online_meeting_provider="teamsForBusiness",
            )
            first_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=now,
            )

            first_result = write_planned_bundle_notes(first_plan)
            self.assertEqual(first_result.written_count, 1)

            transcript_path = intake_root / "bundles" / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            second_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_RecordingArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            source_name="Teams .vtt transcript",
                            status="available",
                            detail=f"Matched local intake artifact(s): {transcript_path}",
                            matched_paths=(transcript_path,),
                        ),
                    )
                ),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=now,
            )
            second_result = write_planned_bundle_notes(second_plan)

            self.assertEqual(second_result.written_count, 1)
            metadata_path = intake_root / "bundles" / "2026-05-04 - Teams - Platform Sync (outlook).json"
            metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata_payload["processor_handoff"]["preferred_input_path"], str(transcript_path))
            identity_path = second_plan.items[0].intake_bundle_note.identity_path
            self.assertTrue(identity_path.exists())

    def test_pending_sync_refresh_cannot_downgrade_processed_marker_that_wins_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            first_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=10),
            )
            write_planned_bundle_notes(first_plan)
            refresh_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=11),
            )
            refresh_note = refresh_plan.items[0].intake_bundle_note
            assert refresh_note is not None
            processed = {
                "source_type": "meeting_bundle_processed",
                "schema_version": 2,
                "processing_source_kind": "manual",
                "upgrade_state": "terminal",
            }
            real_write_identity_marker = meeting_sync_module.write_identity_marker

            def processed_write_wins_before_refresh(
                path: Path,
                payload: dict[str, object],
            ) -> None:
                real_write_identity_marker(path, processed)
                real_write_identity_marker(path, payload)

            with (
                patch.object(
                    meeting_sync_module,
                    "write_identity_marker",
                    side_effect=processed_write_wins_before_refresh,
                ),
                self.assertRaisesRegex(ValueError, "processed identity marker"),
            ):
                write_planned_bundle_notes(refresh_plan)

            self.assertEqual(
                json.loads(refresh_note.identity_path.read_text(encoding="utf-8")),
                processed,
            )

    def test_bundle_refresh_keeps_metadata_old_when_bundle_atomic_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            initial_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=10),
            )
            write_planned_bundle_notes(initial_plan)
            initial_note = initial_plan.items[0].intake_bundle_note
            assert initial_note is not None
            old_bundle = initial_note.path.read_bytes()
            old_metadata = initial_note.metadata_path.read_bytes()
            refresh_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=11),
            )
            refresh_note = refresh_plan.items[0].intake_bundle_note
            assert refresh_note is not None
            real_atomic_write = transcript_provenance.atomic_write_bytes

            def fail_bundle_write(path: Path, content: bytes) -> None:
                if path == refresh_note.path:
                    raise OSError("bundle atomic write failed")
                real_atomic_write(path, content)

            with (
                patch.object(meeting_sync_module, "atomic_write_bytes", side_effect=fail_bundle_write),
                self.assertRaisesRegex(OSError, "bundle atomic write failed"),
            ):
                write_planned_bundle_notes(refresh_plan)

            self.assertEqual(refresh_note.path.read_bytes(), old_bundle)
            self.assertEqual(refresh_note.metadata_path.read_bytes(), old_metadata)
            self.assertIsInstance(json.loads(old_metadata), dict)

    def test_bundle_refresh_leaves_complete_note_and_old_valid_metadata_when_metadata_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            initial_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=10),
            )
            write_planned_bundle_notes(initial_plan)
            initial_note = initial_plan.items[0].intake_bundle_note
            assert initial_note is not None
            old_metadata = initial_note.metadata_path.read_bytes()
            refresh_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=11),
            )
            refresh_note = refresh_plan.items[0].intake_bundle_note
            assert refresh_note is not None
            real_atomic_write = transcript_provenance.atomic_write_bytes

            def fail_metadata_write(path: Path, content: bytes) -> None:
                if path == refresh_note.metadata_path:
                    raise OSError("metadata atomic write failed")
                real_atomic_write(path, content)

            with (
                patch.object(meeting_sync_module, "atomic_write_bytes", side_effect=fail_metadata_write),
                self.assertRaisesRegex(OSError, "metadata atomic write failed"),
            ):
                write_planned_bundle_notes(refresh_plan)

            self.assertEqual(
                refresh_note.path.read_bytes(),
                (refresh_note.content + "\n").encode("utf-8"),
            )
            self.assertEqual(refresh_note.metadata_path.read_bytes(), old_metadata)
            self.assertIsInstance(json.loads(old_metadata), dict)

    def test_bundle_write_validates_metadata_before_writing_bundle_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(_teams_meeting(),)),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T13:40:00+00:00"),
            )
            item = plan.items[0]
            note = item.intake_bundle_note
            assert note is not None
            invalid_note = replace(note, metadata_content="[]")
            invalid_plan = replace(plan, items=(replace(item, intake_bundle_note=invalid_note),))

            with self.assertRaisesRegex(ValueError, "metadata sidecar must be a JSON object"):
                write_planned_bundle_notes(invalid_plan)

            self.assertFalse(note.path.exists())
            self.assertFalse(note.metadata_path.exists())
            self.assertFalse(note.identity_path.exists())

    def test_pending_identity_marker_retries_artifact_discovery_inside_retry_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
            planning_probe = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
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
                now=now,
            )
            existing_identity = planning_probe.items[0].intake_bundle_note.identity_path
            existing_identity.parent.mkdir(parents=True)
            existing_identity.write_text(
                json.dumps(
                    {
                        "source_type": "meeting_sync_pending",
                        "first_seen_at": "2026-05-04T13:35:00+00:00",
                        "last_checked_at": "2026-05-04T13:35:00+00:00",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            transcript_path = intake_root / "bundles" / "raw_transcripts" / "transcript.vtt"
            discovery_client = _RecordingArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        source_name="Teams .vtt transcript",
                        status="available",
                        detail="Matched local intake artifact(s): transcript.vtt",
                        matched_paths=(transcript_path,),
                    ),
                )
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=discovery_client,
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=now,
            )

            self.assertEqual(discovery_client.calls, 1)
            self.assertEqual(plan.process_count, 1)
            self.assertEqual(plan.items[0].decision, "process")
            self.assertIn("Retrying pending meeting artifact discovery.", plan.items[0].reasons)

    def test_processed_identity_marker_skips_artifact_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            planning_probe = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
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
            existing_identity.write_text('{"source_type": "meeting_bundle_processed"}\n', encoding="utf-8")
            discovery_client = _RecordingArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        source_name="Teams .vtt transcript",
                        status="available",
                        detail="Should never be discovered after processing.",
                    ),
                )
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=discovery_client,
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(discovery_client.calls, 0)
            self.assertEqual(plan.items[0].decision, "skip")
            self.assertEqual(plan.items[0].reasons, ("Skipped meeting because it was already processed.",))

    def test_dangling_identity_marker_symlink_skips_artifact_discovery_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
            meeting = _meeting(
                event_id="evt-dangling",
                subject="Platform Sync",
                response_status="accepted",
                join_url=(
                    "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                ),
                online_meeting_provider="teamsForBusiness",
            )
            planning_probe = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=now,
            )
            existing_identity = planning_probe.items[0].intake_bundle_note.identity_path
            existing_identity.parent.mkdir(parents=True)
            existing_identity.symlink_to("missing-identity-target.json")
            discovery_client = _RecordingArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        source_name="Teams .vtt transcript",
                        status="available",
                        detail="Should not be discovered for a malformed identity entry.",
                    ),
                )
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=discovery_client,
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=now,
            )

            self.assertEqual(discovery_client.calls, 0)
            self.assertEqual(plan.items[0].decision, "skip")
            self.assertEqual(plan.items[0].reasons, ("Skipped meeting because it was already processed.",))

    def test_expired_pending_marker_skips_remote_artifact_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            planning_probe = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
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
            existing_identity.write_text(
                json.dumps(
                    {
                        "source_type": "meeting_sync_pending",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            discovery_client = _RecordingArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        source_name="Teams .vtt transcript",
                        status="available",
                        detail="Remote discovery should not run after expiration.",
                    ),
                )
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
                            join_url=(
                                "https://teams.microsoft.com/l/meetup-join/"
                                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                            ),
                            online_meeting_provider="teamsForBusiness",
                        ),
                    )
                ),
                artifact_discovery_client=discovery_client,
                since=date(2026, 5, 1),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T14:00:00+00:00"),
            )

            self.assertEqual(discovery_client.calls, 0)
            self.assertEqual(plan.items[0].decision, "process")
            self.assertIn(
                "Artifact retry window expired after 24 hours; network artifact discovery was skipped.",
                plan.items[0].reasons,
            )

    def test_planning_allows_identity_backfill_when_bundle_and_metadata_exist_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            intake_root.mkdir(parents=True)
            existing_bundle = intake_root / "bundles" / "2026-05-04 - Teams - Platform Sync (bundle).md"
            existing_bundle.parent.mkdir(parents=True, exist_ok=True)
            existing_bundle.write_text("existing bundle\n", encoding="utf-8")
            existing_metadata = intake_root / "bundles" / "2026-05-04 - Teams - Platform Sync (outlook).json"
            existing_metadata.write_text("{}\n", encoding="utf-8")

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(
                    meetings=(
                        _meeting(
                            event_id="evt-1",
                            subject="Platform Sync",
                            response_status="accepted",
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


class BundleProcessingPlanTests(unittest.TestCase):
    def test_execute_cleans_up_bundle_staging_and_writes_processed_marker_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake" / "bundles"
            intake_root.mkdir(parents=True)
            managed_transcript = intake_root / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
            managed_transcript.parent.mkdir(parents=True, exist_ok=True)
            managed_transcript.write_text("WEBVTT\n", encoding="utf-8")
            manual_bundle_artifact = intake_root / "manual" / "2026-05-04 - Teams - Platform Sync.md"
            manual_bundle_artifact.parent.mkdir(parents=True, exist_ok=True)
            manual_bundle_artifact.write_text("Transcript text\n", encoding="utf-8")
            bundle_note = intake_root / "2026-05-04 - Teams - Platform Sync (bundle).md"
            bundle_note.write_text("bundle note\n", encoding="utf-8")
            metadata_path = intake_root / "2026-05-04 - Teams - Platform Sync (outlook).json"
            processed_marker_path = intake_root / "_meeting_sync" / "identities" / "2026-05-04-processed.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "source_type": "teams_vtt_transcript",
                        "identity_key": "evt-1|19:meeting_platform@thread.v2",
                        "outlook_event_id": "evt-1",
                        "subject": "Platform Sync",
                        "scheduled_start_at": "2026-05-04T13:00:00+00:00",
                        "scheduled_end_at": "2026-05-04T13:30:00+00:00",
                        "teams_meeting_id": "19:meeting_platform@thread.v2",
                        "processed_marker_path": str(processed_marker_path),
                        "artifacts": [
                            {
                                "source_name": "Teams .vtt transcript",
                                "status": "available",
                                "detail": "Downloaded into bundle staging.",
                                "matched_paths": [str(managed_transcript)],
                            },
                            {
                                "source_name": "Manual / semi-manual intake",
                                "status": "available",
                                "detail": "Original operator note should remain.",
                                "matched_paths": [str(manual_bundle_artifact)],
                            },
                        ],
                        "processor_handoff": {
                            "preferred_input_path": str(managed_transcript),
                            "preferred_input_source_name": "Teams .vtt transcript",
                        },
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            canonical_note_path = (
                Path(tmp_dir) / "01_Meetings" / "2026" / "05_May" / "2026-05-04 - Teams - Platform Sync.md"
            )
            canonical_note_path.parent.mkdir(parents=True, exist_ok=True)
            canonical_note_path.write_text("# Platform Sync\n", encoding="utf-8")
            actions_file_path = Path(tmp_dir) / "07_Actions" / "2026-05-04.md"
            processor = _BundleProcessorStub(
                result=ProcessResult(
                    processed=True,
                    canonical_note_path=canonical_note_path,
                    actions_file_path=actions_file_path,
                ),
                meetings_path=Path(tmp_dir) / "01_Meetings",
                vault_path=Path(tmp_dir),
            )

            plan = build_bundle_processing_plan(
                intake_root=intake_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            self.assertEqual(processor.calls, [managed_transcript])
            self.assertFalse(bundle_note.exists())
            self.assertFalse(metadata_path.exists())
            self.assertFalse(managed_transcript.exists())
            self.assertTrue(manual_bundle_artifact.exists())
            self.assertTrue(processed_marker_path.exists())

            processed_marker = json.loads(processed_marker_path.read_text(encoding="utf-8"))
            self.assertEqual(processed_marker["source_type"], "meeting_bundle_processed")
            self.assertEqual(processed_marker["identity_key"], "evt-1|19:meeting_platform@thread.v2")
            self.assertEqual(processed_marker["outlook_event_id"], "evt-1")
            self.assertEqual(processed_marker["subject"], "Platform Sync")
            self.assertEqual(processed_marker["teams_meeting_id"], "19:meeting_platform@thread.v2")
            self.assertEqual(processed_marker["bundle_note_path"], str(bundle_note))
            self.assertEqual(processed_marker["outlook_metadata_path"], str(metadata_path))
            self.assertEqual(processed_marker["preferred_input_path"], str(managed_transcript))
            self.assertEqual(processed_marker["canonical_note_path"], str(canonical_note_path))
            self.assertEqual(processed_marker["actions_file_path"], str(actions_file_path))
            self.assertCountEqual(
                processed_marker["cleanup_paths"],
                [str(bundle_note), str(metadata_path), str(managed_transcript)],
            )
            self.assertNotIn(str(manual_bundle_artifact), processed_marker["cleanup_paths"])

    def test_execute_leaves_staging_in_place_when_atomic_processed_marker_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake" / "bundles"
            intake_root.mkdir(parents=True)
            managed_transcript = intake_root / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
            managed_transcript.parent.mkdir(parents=True, exist_ok=True)
            managed_transcript.write_text("WEBVTT\n", encoding="utf-8")
            bundle_note = intake_root / "2026-05-04 - Teams - Platform Sync (bundle).md"
            bundle_note.write_text("bundle note\n", encoding="utf-8")
            metadata_path = intake_root / "2026-05-04 - Teams - Platform Sync (outlook).json"
            processed_marker_path = intake_root / "_meeting_sync" / "identities" / "2026-05-04-processed.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "source_type": "teams_vtt_transcript",
                        "identity_key": "evt-1|19:meeting_platform@thread.v2",
                        "outlook_event_id": "evt-1",
                        "subject": "Platform Sync",
                        "scheduled_start_at": "2026-05-04T13:00:00+00:00",
                        "scheduled_end_at": "2026-05-04T13:30:00+00:00",
                        "teams_meeting_id": "19:meeting_platform@thread.v2",
                        "processed_marker_path": str(processed_marker_path),
                        "artifacts": [
                            {
                                "source_name": "Teams .vtt transcript",
                                "status": "available",
                                "detail": "Downloaded into bundle staging.",
                                "matched_paths": [str(managed_transcript)],
                            },
                        ],
                        "processor_handoff": {
                            "preferred_input_path": str(managed_transcript),
                            "preferred_input_source_name": "Teams .vtt transcript",
                        },
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            canonical_note_path = Path(tmp_dir) / "01_Meetings" / "2026-05-04 - Teams - Platform Sync.md"
            canonical_note_path.parent.mkdir(parents=True, exist_ok=True)
            canonical_note_path.write_text("# Platform Sync\n", encoding="utf-8")
            processor = _BundleProcessorStub(
                result=ProcessResult(processed=True, canonical_note_path=canonical_note_path),
                meetings_path=canonical_note_path.parent,
                vault_path=Path(tmp_dir),
            )
            plan = build_bundle_processing_plan(
                intake_root=intake_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            with patch.object(
                process_bundles_module.IdentityMarkerTransaction,
                "compare_and_write",
                side_effect=OSError("disk full"),
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertTrue(bundle_note.exists())
            self.assertTrue(metadata_path.exists())
            self.assertTrue(managed_transcript.exists())
            self.assertFalse(processed_marker_path.exists())

    def test_grace_recap_ten_minutes_after_end_is_discovered_without_processor_handoff(self) -> None:
        recap_path = Path("/tmp/vault/00_Intake/bundles/fallbacks/platform-sync.md")
        meeting = _teams_meeting()

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            artifact_discovery_client=_StubArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        "Copilot recap / AI summary",
                        "available",
                        "Recap downloaded.",
                        matched_paths=(recap_path,),
                    ),
                )
            ),
            since=date(2026, 5, 4),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=meeting.end_at + timedelta(minutes=10),
        )

        item = plan.items[0]
        assert item.intake_bundle_note is not None
        self.assertEqual(item.decision, "process")
        self.assertEqual(item.bundle.source_status("Copilot recap / AI summary"), "available")
        self.assertIsNone(item.intake_bundle_note.processor_input_path)
        self.assertIsNone(item.intake_bundle_note.processor_input_source_name)
        self.assertIn("Recap fallback deferred until 2026-05-04T14:30:00+00:00.", item.reasons)

    def test_grace_planned_recap_persists_on_explicit_write_without_processor_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            recap_path = intake_root / "bundles" / "fallbacks" / "platform-sync.md"
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            "Copilot recap / AI summary",
                            "available",
                            "Recap content is planned for explicit persistence.",
                            matched_paths=(recap_path,),
                            planned_content="# Planned recap\n",
                        ),
                    )
                ),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=10),
            )

            note = plan.items[0].intake_bundle_note
            assert note is not None
            self.assertIsNone(note.processor_input_path)
            self.assertFalse(recap_path.exists())

            write_planned_bundle_notes(plan)

            self.assertEqual(recap_path.read_text(encoding="utf-8"), "# Planned recap\n")

    def test_grace_planned_recap_rejects_target_outside_owned_bundles_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            outside_path = Path(tmp_dir) / "outside.md"
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            "Copilot recap / AI summary",
                            "available",
                            matched_paths=(outside_path,),
                            planned_content="# Must stay owned\n",
                        ),
                    )
                ),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=10),
            )

            with self.assertRaisesRegex(ValueError, "outside the owned bundles root"):
                write_planned_bundle_notes(plan)

            self.assertFalse(outside_path.exists())

    def test_grace_planned_recap_rejects_symlinked_parent_below_owned_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            bundles_root = intake_root / "bundles"
            escape_root = Path(tmp_dir) / "escape"
            bundles_root.mkdir(parents=True)
            escape_root.mkdir()
            (bundles_root / "fallbacks").symlink_to(escape_root, target_is_directory=True)
            recap_path = bundles_root / "fallbacks" / "platform-sync.md"
            meeting = _teams_meeting()
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            "Copilot recap / AI summary",
                            "available",
                            matched_paths=(recap_path,),
                            planned_content="# Must not escape\n",
                        ),
                    )
                ),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=10),
            )

            with self.assertRaisesRegex(ValueError, "symlinked path component"):
                write_planned_bundle_notes(plan)

            self.assertFalse((escape_root / "platform-sync.md").exists())

    def test_bundle_write_rejects_symlinked_owned_bundles_root_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            escape_root = Path(tmp_dir) / "escape"
            intake_root.mkdir()
            escape_root.mkdir()
            (intake_root / "bundles").symlink_to(escape_root, target_is_directory=True)
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(_teams_meeting(),)),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-04T13:40:00+00:00"),
            )

            with self.assertRaisesRegex(ValueError, "owned bundles root is symlinked"):
                write_planned_bundle_notes(plan)

            self.assertEqual(tuple(escape_root.iterdir()), ())

    def test_grace_planned_recap_preserves_competing_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            recap_path = intake_root / "bundles" / "fallbacks" / "platform-sync.md"
            meeting = _teams_meeting()
            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            "Copilot recap / AI summary",
                            "available",
                            matched_paths=(recap_path,),
                            planned_content="# Planned recap\n",
                        ),
                    )
                ),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=meeting.end_at + timedelta(minutes=10),
            )
            real_atomic_create = transcript_provenance.atomic_create_bytes

            def competing_create(path: Path, content: bytes) -> bool:
                path.write_bytes(b"COMPETING")
                return real_atomic_create(path, content)

            with patch.object(
                meeting_sync_module,
                "atomic_create_bytes",
                side_effect=competing_create,
            ):
                write_planned_bundle_notes(plan)

            self.assertEqual(recap_path.read_bytes(), b"COMPETING")

    def test_grace_transcript_wins_over_available_recap(self) -> None:
        meeting = _teams_meeting()
        transcript_path = Path("/tmp/vault/00_Intake/bundles/raw_transcripts/platform-sync.vtt")
        recap_path = Path("/tmp/vault/00_Intake/bundles/fallbacks/platform-sync.md")

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            artifact_discovery_client=_StubArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact("Teams .vtt transcript", "available", matched_paths=(transcript_path,)),
                    MeetingArtifact("Copilot recap / AI summary", "available", matched_paths=(recap_path,)),
                )
            ),
            since=date(2026, 5, 4),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=meeting.end_at + timedelta(minutes=10),
        )

        note = plan.items[0].intake_bundle_note
        assert note is not None
        self.assertEqual(note.processor_input_path, transcript_path)
        self.assertEqual(note.processor_input_source_name, "Teams .vtt transcript")

    def test_grace_recap_is_processor_ready_at_default_deadline(self) -> None:
        meeting = _teams_meeting()
        recap_path = Path("/tmp/vault/00_Intake/bundles/fallbacks/platform-sync.md")

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            artifact_discovery_client=_StubArtifactDiscoveryClient(
                artifacts=(MeetingArtifact("Copilot recap / AI summary", "available", matched_paths=(recap_path,)),)
            ),
            since=date(2026, 5, 4),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=meeting.end_at + timedelta(minutes=60),
        )

        note = plan.items[0].intake_bundle_note
        assert note is not None
        self.assertEqual(note.processor_input_path, recap_path)
        self.assertEqual(note.processor_input_source_name, "Copilot recap / AI summary")
        self.assertIn(
            "meeting_sync_fallback_awaiting_transcript: 0",
            render_transcript_sync_plan(plan),
        )

    def test_grace_override_changes_fallback_deadline(self) -> None:
        meeting = _teams_meeting()
        recap_path = Path("/tmp/vault/00_Intake/bundles/fallbacks/platform-sync.md")

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            artifact_discovery_client=_StubArtifactDiscoveryClient(
                artifacts=(MeetingArtifact("Copilot recap / AI summary", "available", matched_paths=(recap_path,)),)
            ),
            since=date(2026, 5, 4),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=meeting.end_at + timedelta(minutes=70),
            transcript_grace_minutes=90,
        )

        item = plan.items[0]
        assert item.intake_bundle_note is not None
        self.assertIsNone(item.intake_bundle_note.processor_input_path)
        self.assertIn("Recap fallback deferred until 2026-05-04T15:00:00+00:00.", item.reasons)
        self.assertEqual(plan.fallback_deferred_count, 1)
        self.assertEqual(plan.fallback_awaiting_transcript_count, 0)
        self.assertEqual(plan.late_transcript_upgrade_count, 0)
        rendered = render_transcript_sync_plan(plan)
        self.assertIn("meeting_sync_fallback_deferred: 1", rendered)
        self.assertIn("meeting_sync_fallback_awaiting_transcript: 0", rendered)
        self.assertIn("meeting_sync_late_transcript_upgrades: 0", rendered)

    def test_grace_sidecars_include_deadlines_and_occurrence_selection_identity(self) -> None:
        meeting = _teams_meeting()

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            since=date(2026, 5, 4),
            intake_root=Path("/tmp/vault/00_Intake"),
            now=meeting.end_at + timedelta(minutes=10),
        )

        note = plan.items[0].intake_bundle_note
        assert note is not None
        metadata = json.loads(note.metadata_content)
        identity = json.loads(note.identity_content)
        expected_fields = {
            "fallback_not_before": "2026-05-04T14:30:00+00:00",
            "retry_until": "2026-05-05T13:30:00+00:00",
            "primary_occurrence_event_id": "evt-1",
            "scheduled_start_at": "2026-05-04T13:00:00+00:00",
            "scheduled_end_at": "2026-05-04T13:30:00+00:00",
            "selection_window_start_at": "2026-05-04T12:45:00+00:00",
            "selection_window_end_at": "2026-05-04T14:00:00+00:00",
        }
        for payload in (metadata, identity):
            for key, value in expected_fields.items():
                self.assertEqual(payload[key], value)

    def test_fallback_processed_before_retry_deadline_polls_transcripts_without_summary_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            marker_path = meeting_sync_module._meeting_identity_path(meeting, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True)
            marker_payload = {
                "schema_version": 2,
                "source_type": "meeting_bundle_processed",
                "processing_source_kind": "fallback",
                "upgrade_state": "awaiting_transcript",
                "retry_until": "2026-05-05T13:30:00+00:00",
            }
            marker_path.write_text(json.dumps(marker_payload) + "\n", encoding="utf-8")
            transcript_client = _RecordingArtifactDiscoveryClient(
                artifacts=(MeetingArtifact("Teams .vtt transcript", "missing", "No valid transcript."),)
            )
            summary_client = GraphMeetingFallbackSummaryClient(
                access_token="unused",
                intake_root=intake_root,
                fetch_json=lambda _url, _token: self.fail("fallback summary client must not be called"),
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=ChainedMeetingArtifactDiscoveryClient(
                    transcript_client,
                    summary_client,
                ),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )
            write_result = write_planned_bundle_notes(plan)

            self.assertTrue(GraphMeetingFallbackSummaryClient.provides_summary_fallback)
            self.assertEqual(transcript_client.calls, 1)
            self.assertEqual(plan.items[0].decision, "skip")
            self.assertIsNone(plan.items[0].intake_bundle_note)
            self.assertEqual(write_result.written_count, 0)
            self.assertEqual(write_result.written_metadata_paths, ())
            self.assertEqual(write_result.written_identity_paths, ())
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8")), marker_payload)
            self.assertEqual(plan.fallback_deferred_count, 0)
            self.assertEqual(plan.fallback_awaiting_transcript_count, 1)
            self.assertEqual(plan.late_transcript_upgrade_count, 0)
            rendered = render_transcript_sync_plan(plan)
            self.assertIn("meeting_sync_fallback_deferred: 0", rendered)
            self.assertIn("meeting_sync_fallback_awaiting_transcript: 1", rendered)
            self.assertIn("meeting_sync_late_transcript_upgrades: 0", rendered)

    def test_fallback_processed_legacy_marker_plans_unique_transcript_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            marker_path = meeting_sync_module._meeting_identity_path(meeting, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "source_type": "meeting_bundle_processed",
                        "preferred_input_source_name": "Copilot recap / AI summary",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            transcript_path = intake_root / "bundles" / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_content = b"WEBVTT\n\nVerified occurrence bytes.\n"
            transcript_path.write_bytes(transcript_content)
            transcript_provenance.write_provenance(
                transcript_path,
                event_id=meeting.event_id,
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
                transcripts=(
                    {
                        "id": "verified-segment",
                        "created_at": "2026-05-04T13:10:00+00:00",
                    },
                ),
                content=transcript_content,
            )
            transcript_client = LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root)

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=transcript_client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )

            item = plan.items[0]
            assert item.intake_bundle_note is not None
            item.intake_bundle_note.path.parent.mkdir(parents=True, exist_ok=True)
            item.intake_bundle_note.path.write_text("stale fallback bundle\n", encoding="utf-8")
            item.intake_bundle_note.metadata_path.write_text("{}\n", encoding="utf-8")
            marker_before_write = marker_path.read_text(encoding="utf-8")
            write_result = write_planned_bundle_notes(plan)

            self.assertEqual(item.decision, "process")
            self.assertEqual(item.intake_bundle_note.processor_input_path, transcript_path)
            self.assertEqual(item.intake_bundle_note.processor_input_source_name, "Teams .vtt transcript")
            self.assertIn("Would upgrade fallback-processed meeting with a transcript.", item.reasons)
            self.assertEqual(plan.fallback_deferred_count, 0)
            self.assertEqual(plan.fallback_awaiting_transcript_count, 0)
            self.assertEqual(plan.late_transcript_upgrade_count, 1)
            rendered = render_transcript_sync_plan(plan)
            self.assertIn("meeting_sync_fallback_deferred: 0", rendered)
            self.assertIn("meeting_sync_fallback_awaiting_transcript: 0", rendered)
            self.assertIn("meeting_sync_late_transcript_upgrades: 1", rendered)
            self.assertEqual(write_result.written_bundle_note_paths, (item.intake_bundle_note.path,))
            self.assertEqual(write_result.written_metadata_paths, (item.intake_bundle_note.metadata_path,))
            self.assertEqual(write_result.written_identity_paths, ())
            self.assertEqual(marker_path.read_text(encoding="utf-8"), marker_before_write)

    def test_fallback_processed_local_only_transcript_without_provenance_does_not_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            marker_path = meeting_sync_module._meeting_identity_path(meeting, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            transcript_path = intake_root / "2026-05-04 - Teams - Platform Sync.vtt"

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            "Teams .vtt transcript",
                            "available",
                            matched_paths=(transcript_path,),
                        ),
                    )
                ),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )

            self.assertEqual(plan.items[0].decision, "skip")
            self.assertIsNone(plan.items[0].intake_bundle_note)
            self.assertIn("occurrence-valid transcript provenance", plan.items[0].reasons[0])

    def test_fallback_processed_graph_diagnostics_plus_unvalidated_local_transcript_does_not_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            marker_path = meeting_sync_module._meeting_identity_path(meeting, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            diagnostics = TranscriptDiagnostics(
                candidate_count=1,
                selected_transcripts=(
                    SelectedTranscriptDiagnostic(
                        "graph-selected-segment",
                        datetime.fromisoformat("2026-05-04T13:10:00+00:00"),
                    ),
                ),
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
                local_action="none",
            )
            local_path = intake_root / "2026-05-04 - Teams - Platform Sync.md"
            discovery_client = ChainedMeetingArtifactDiscoveryClient(
                _StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            "Teams transcript text",
                            "available",
                            "Graph metadata selected this occurrence.",
                            diagnostics=diagnostics,
                        ),
                    )
                ),
                _StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            "Teams transcript text",
                            "available",
                            "Local transcript matched the occurrence filename.",
                            matched_paths=(local_path,),
                        ),
                    )
                ),
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=discovery_client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )

            self.assertEqual(plan.items[0].decision, "skip")
            self.assertIsNone(plan.items[0].intake_bundle_note)
            self.assertIn("occurrence-valid transcript provenance", plan.items[0].reasons[0])

    def test_chain_does_not_transplant_occurrence_validated_path_to_replacement_path(self) -> None:
        meeting = _teams_meeting()
        graph_path = Path("/tmp/vault/00_Intake/bundles/raw_transcripts/graph.vtt")
        local_path = Path("/tmp/vault/00_Intake/2026-05-04 - Teams - Platform Sync.md")
        diagnostics = TranscriptDiagnostics(
            candidate_count=1,
            selected_transcripts=(
                SelectedTranscriptDiagnostic(
                    "graph-selected-segment",
                    datetime.fromisoformat("2026-05-04T13:10:00+00:00"),
                ),
            ),
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            local_action="validated",
        )
        client = ChainedMeetingArtifactDiscoveryClient(
            _StubArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        "Teams transcript text",
                        "available",
                        matched_paths=(graph_path,),
                        diagnostics=diagnostics,
                        occurrence_validated_paths=(graph_path,),
                    ),
                )
            ),
            _StubArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        "Teams transcript text",
                        "available",
                        matched_paths=(local_path,),
                    ),
                )
            ),
        )

        artifact = client.discover_artifacts(meeting=meeting)[0]

        self.assertEqual(artifact.matched_paths, (local_path,))
        self.assertEqual(artifact.occurrence_validated_paths, ())
        self.assertEqual(artifact.diagnostics, diagnostics)

    def test_fallback_processed_graph_metadata_content_fallback_retains_provenance_and_upgrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            marker_path = meeting_sync_module._meeting_identity_path(meeting, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            def fetch_json(url: str, token: str) -> dict[str, object]:
                self.assertEqual(token, "token")
                if "/onlineMeetings?" in url:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                if url.endswith("/transcripts"):
                    return {
                        "value": [
                            {
                                "id": "selected-segment",
                                "createdDateTime": "2026-05-04T13:10:00Z",
                            }
                        ]
                    }
                self.fail(f"Unexpected Graph URL: {url}")

            def fetch_bytes(url: str, token: str) -> bytes:
                self.assertEqual(token, "token")
                if url.endswith("/metadataContent"):
                    return b'{"speakerName":"Priya","spokenText":"Metadata fallback transcript."}\n'
                raise _SyntheticHTTPError(url, 404, "Not Found")

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=GraphTranscriptDownloadClient(
                    access_token="token",
                    intake_root=intake_root,
                    api_base_url="https://graph.example/v1.0",
                    fetch_json=fetch_json,
                    fetch_bytes=fetch_bytes,
                ),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )

            item = plan.items[0]
            artifact = item.bundle.artifact("Teams transcript text")
            assert artifact is not None
            assert artifact.diagnostics is not None
            self.assertEqual(item.decision, "process")
            self.assertEqual(artifact.occurrence_validated_paths, artifact.matched_paths)
            self.assertIsNotNone(
                transcript_provenance.matching_provenance(
                    artifact.matched_paths[0],
                    event_id=meeting.event_id,
                    occurrence_start_at=meeting.start_at,
                    occurrence_end_at=meeting.end_at,
                )
            )
            self.assertEqual(
                tuple(selected.transcript_id for selected in artifact.diagnostics.selected_transcripts),
                ("selected-segment",),
            )
            assert item.intake_bundle_note is not None
            self.assertEqual(
                item.intake_bundle_note.processor_input_source_name,
                "Teams transcript text",
            )

            artifact.matched_paths[0].write_text("mutated after validation\n", encoding="utf-8")
            mutated_plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )

            self.assertEqual(mutated_plan.items[0].decision, "skip")
            self.assertIsNone(mutated_plan.items[0].intake_bundle_note)

    def test_fallback_processed_filename_match_with_graph_ambiguity_does_not_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            marker_path = meeting_sync_module._meeting_identity_path(meeting, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            diagnostics = TranscriptDiagnostics(
                candidate_count=1,
                selected_transcripts=(),
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
                local_action="none",
                assignment_conflicts=(
                    meeting_sync_module.AssignmentConflict(
                        "ambiguous-segment",
                        datetime.fromisoformat("2026-05-04T13:20:00+00:00"),
                        ("evt-1", "evt-2"),
                    ),
                ),
            )
            local_path = intake_root / "2026-05-04 - Teams - Platform Sync.md"

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            "Teams transcript text",
                            "available",
                            matched_paths=(local_path,),
                            diagnostics=diagnostics,
                        ),
                    )
                ),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )

            self.assertEqual(plan.items[0].decision, "skip")
            self.assertIsNone(plan.items[0].intake_bundle_note)
            self.assertIn(
                "meeting_sync_occurrence_error: ambiguous_transcript_assignment",
                render_transcript_sync_plan(plan),
            )

    def test_fallback_processed_nested_chain_removes_summary_clients_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            marker_path = meeting_sync_module._meeting_identity_path(meeting, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            transcript_client = _RecordingArtifactDiscoveryClient(
                artifacts=(MeetingArtifact("Teams .vtt transcript", "missing"),)
            )
            summary_client = GraphMeetingFallbackSummaryClient(
                access_token="unused",
                intake_root=intake_root,
                fetch_json=lambda _url, _token: self.fail("nested fallback summary client must not be called"),
            )
            nested_chain = ChainedMeetingArtifactDiscoveryClient(
                ChainedMeetingArtifactDiscoveryClient(summary_client),
                transcript_client,
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=nested_chain,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )

            self.assertEqual(transcript_client.calls, 1)
            self.assertEqual(plan.items[0].decision, "skip")

    def test_fallback_processed_marker_is_eligible_at_deadline_and_terminal_strictly_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            marker_path = meeting_sync_module._meeting_identity_path(meeting, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            at_deadline_client = _RecordingArtifactDiscoveryClient(
                artifacts=(MeetingArtifact("Teams .vtt transcript", "missing"),)
            )

            at_deadline = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=at_deadline_client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )
            after_deadline_client = _RecordingArtifactDiscoveryClient(
                artifacts=(MeetingArtifact("Teams .vtt transcript", "available"),)
            )
            after_deadline = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=after_deadline_client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:01+00:00"),
            )

            self.assertEqual(at_deadline_client.calls, 1)
            self.assertEqual(at_deadline.items[0].decision, "skip")
            self.assertEqual(after_deadline_client.calls, 0)
            self.assertEqual(after_deadline.items[0].decision, "skip")
            self.assertEqual(
                after_deadline.items[0].reasons,
                ("Skipped meeting because it was already processed.",),
            )

    def test_fallback_processed_ambiguous_transcript_does_not_upgrade_and_renders_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            meeting = _teams_meeting()
            marker_path = meeting_sync_module._meeting_identity_path(meeting, intake_root=intake_root)
            assert marker_path is not None
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-05-05T13:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            diagnostics = TranscriptDiagnostics(
                candidate_count=1,
                selected_transcripts=(),
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
                local_action="none",
                assignment_conflicts=(
                    meeting_sync_module.AssignmentConflict(
                        "ambiguous-segment",
                        datetime.fromisoformat("2026-05-04T13:20:00+00:00"),
                        ("evt-1", "evt-2"),
                    ),
                ),
            )

            plan = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=_StubArtifactDiscoveryClient(
                    artifacts=(
                        MeetingArtifact(
                            "Teams .vtt transcript",
                            "missing",
                            "Ambiguous transcript was not selected.",
                            diagnostics=diagnostics,
                        ),
                    )
                ),
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T13:30:00+00:00"),
            )

            self.assertEqual(plan.items[0].decision, "skip")
            self.assertIsNone(plan.items[0].intake_bundle_note)
            rendered = render_transcript_sync_plan(plan)
            self.assertIn("meeting_sync_occurrence_error: ambiguous_transcript_assignment", rendered)
            self.assertIn("candidate_transcript_id: ambiguous-segment", rendered)
            self.assertNotIn("selected_transcript_id: ambiguous-segment", rendered)


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


@dataclass(slots=True)
class _StubArtifactDiscoveryClient:
    artifacts: tuple[MeetingArtifact, ...]

    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]:
        del meeting
        return self.artifacts


@dataclass(slots=True)
class _RecordingArtifactDiscoveryClient:
    artifacts: tuple[MeetingArtifact, ...]
    calls: int = 0

    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]:
        del meeting
        self.calls += 1
        return self.artifacts


@dataclass(slots=True)
class _BundleProcessorStub:
    result: ProcessResult
    meetings_path: Path
    vault_path: Path
    calls: list[Path] = field(default_factory=list)
    intake_state: object = field(init=False)
    actions_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.intake_state = _BundleIntakeStateStub()
        self.actions_path = self.vault_path / "07_Actions"

    def skip_reason(self, path: Path) -> None:
        del path
        return None

    def process_file(self, path: Path, dry_run: bool = False) -> ProcessResult:
        self.calls.append(path)
        assert dry_run is False
        return self.result


class _BundleIntakeStateStub:
    def is_under_intake(self, path: Path) -> bool:
        del path
        return True


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


def _teams_meeting() -> OutlookMeetingCandidate:
    return _meeting(
        event_id="evt-1",
        subject="Platform Sync",
        response_status="accepted",
        join_url=("https://teams.microsoft.com/l/meetup-join/19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"),
        online_meeting_provider="teamsForBusiness",
    )


def _recurring_meeting() -> OutlookMeetingCandidate:
    return OutlookMeetingCandidate(
        event_id="evt-recurring",
        subject="Recurring Platform Sync",
        start_at=datetime.fromisoformat("2026-07-17T17:00:00+00:00"),
        end_at=datetime.fromisoformat("2026-07-17T17:25:00+00:00"),
        response_status="accepted",
        join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
        online_meeting_provider="teamsForBusiness",
    )


if __name__ == "__main__":
    unittest.main()


class _SyntheticHTTPError(HTTPError):
    def __init__(self, url: str, code: int, message: str, body: dict[str, object] | None = None) -> None:
        self.url = url
        self.code = code
        self.msg = message
        self.hdrs = None
        self.fp = io.BytesIO(json.dumps(body).encode("utf-8")) if body is not None else None
