from __future__ import annotations

import io
import json
import tempfile
import unittest
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

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
    OutlookMeetingCandidate,
    UnconfiguredOutlookMeetingDiscoveryClient,
    build_bundle_processing_plan,
    build_transcript_sync_plan,
    execute_bundle_processing_plan,
    render_bundle_write_result,
    render_intake_bundle_note,
    render_meeting_identity_sidecar,
    render_outlook_metadata_sidecar,
    render_transcript_sync_plan,
    write_planned_bundle_notes,
)
from obsidian_intake_agent.meetings import process_bundles as process_bundles_module
from obsidian_intake_agent.meetings import sync as meeting_sync_module
from obsidian_intake_agent.processors.meeting_processor import ProcessResult


class TranscriptSyncPlannerTests(unittest.TestCase):
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
            self.assertTrue(fallback_path.exists())
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
            return {"value": [{"id": "transcript-1"}, {"id": "transcript-2"}]}

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
                return {"value": [{"id": "transcript 1"}]}

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

    def test_graph_transcript_download_preserves_opaque_online_meeting_id_path_characters(self) -> None:
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
            intake_root=Path("/tmp/00_Intake"),
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

    def test_graph_transcript_download_reuses_existing_vtt_without_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            transcript_path = intake_root / "bundles" / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                fetch_json=lambda url, token: self.fail("fetch_json should not be called for existing VTT"),
                fetch_bytes=lambda url, token: self.fail("fetch_bytes should not be called for existing VTT"),
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
            self.assertEqual(artifacts[0].status, "available")
            self.assertEqual(artifacts[0].matched_paths, (transcript_path,))

    def test_graph_transcript_download_marks_content_permission_blocked(self) -> None:
        client = GraphTranscriptDownloadClient(
            access_token="token",
            intake_root=Path("/tmp/00_Intake"),
            fetch_json=lambda url, token: {"value": [{"id": "transcript-1"}]},
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

    def test_graph_transcript_download_includes_graph_400_message(self) -> None:
        client = GraphTranscriptDownloadClient(
            access_token="token",
            intake_root=Path("/tmp/00_Intake"),
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

    def test_graph_meeting_fallback_summary_writes_recap_markdown_and_uses_chat_as_context(self) -> None:
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

            artifacts = client.discover_artifacts(meeting=meeting)
            fallback_path = intake_root / meeting_sync_module._fallback_summary_relative_path(meeting)

            self.assertEqual(artifacts[0].source_name, "Copilot recap / AI summary")
            self.assertEqual(artifacts[0].status, "available")
            self.assertEqual(artifacts[0].matched_paths, (fallback_path,))
            self.assertEqual(artifacts[1].source_name, "Teams meeting chat")
            self.assertEqual(artifacts[1].status, "available")
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

            first_artifacts = client.discover_artifacts(meeting=meeting)
            self.assertEqual(first_artifacts[1].source_name, "Teams meeting chat")
            self.assertEqual(first_artifacts[1].status, "available")
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
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
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
            '"bundle_note_path": "/tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Delivery Review (bundle).md"',
            rendered,
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
                intake_root=intake_root,
                now=now,
            )

            first_result = write_planned_bundle_notes(plan)
            second_result = write_planned_bundle_notes(plan)

            self.assertEqual(first_result.written_count, 1)
            self.assertEqual(first_result.skipped_existing_count, 0)
            self.assertTrue((intake_root / "bundles" / "2026-05-04 - Teams - Platform Sync (bundle).md").exists())
            self.assertTrue((intake_root / "bundles" / "2026-05-04 - Teams - Platform Sync (outlook).json").exists())
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
            existing_identity.write_text("{}\n", encoding="utf-8")

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

    def test_existing_identity_marker_does_not_trigger_artifact_discovery(self) -> None:
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
            existing_identity.parent.mkdir(parents=True, exist_ok=True)
            existing_identity.write_text("{}\n", encoding="utf-8")

            discovery_client = _RecordingArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        source_name="Teams transcript text",
                        status="available",
                        detail="Should never be discovered when identity marker exists.",
                        matched_paths=(Path("/tmp/should-not-run.md"),),
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
            self.assertEqual(
                plan.items[0].reasons,
                ("Skipped meeting because a meeting identity marker already exists.",),
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

            canonical_note_path = Path(tmp_dir) / "01_Meetings" / "2026-05-04 - Teams - Platform Sync.md"
            actions_file_path = Path(tmp_dir) / "07_Actions" / "2026-05-04.md"
            processor = _BundleProcessorStub(
                result=ProcessResult(
                    processed=True,
                    canonical_note_path=canonical_note_path,
                    actions_file_path=actions_file_path,
                )
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

            processor = _BundleProcessorStub(result=ProcessResult(processed=True))
            plan = build_bundle_processing_plan(
                intake_root=intake_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            with patch.object(process_bundles_module, "_write_json_atomically", side_effect=OSError("disk full")):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertTrue(bundle_note.exists())
            self.assertTrue(metadata_path.exists())
            self.assertTrue(managed_transcript.exists())
            self.assertFalse(processed_marker_path.exists())


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
    calls: list[Path] = field(default_factory=list)
    intake_state: object = field(init=False)

    def __post_init__(self) -> None:
        self.intake_state = _BundleIntakeStateStub()

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


if __name__ == "__main__":
    unittest.main()


class _SyntheticHTTPError(HTTPError):
    def __init__(self, url: str, code: int, message: str, body: dict[str, object] | None = None) -> None:
        self.url = url
        self.code = code
        self.msg = message
        self.hdrs = None
        self.fp = io.BytesIO(json.dumps(body).encode("utf-8")) if body is not None else None
