from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from obsidian_intake_agent.config import Config
from obsidian_intake_agent.meetings import (
    LocalIntakeTranscriptDiscoveryClient,
    MeetingArtifact,
    MeetingDiscoverySnapshot,
    OutlookMeetingCandidate,
    build_bundle_processing_plan,
    build_transcript_sync_plan,
    render_bundle_processing_plan,
)
from obsidian_intake_agent.processors.meeting_processor import MeetingProcessor


class BundleProcessingPlanTests(unittest.TestCase):
    def test_marks_bundle_ready_when_local_vtt_handoff_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")

            note = _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-ready", subject="Delivery Review"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
            )

            plan = build_bundle_processing_plan(
                intake_root=intake_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            self.assertEqual(plan.ready_count, 1)
            self.assertEqual(plan.blocked_count, 0)
            item = plan.items[0]
            self.assertEqual(item.decision, "ready")
            self.assertEqual(item.metadata.metadata_path, note.metadata_path)
            self.assertEqual(item.metadata.processor_handoff.preferred_input_path, transcript_path)
            rendered = render_bundle_processing_plan(plan)
            self.assertIn("meeting_bundle_process_ready: 1", rendered)
            self.assertIn(f"preferred_input: {transcript_path}", rendered)
            self.assertIn(f"would_run: .venv/bin/obsidian-agent process {transcript_path} --dry-run", rendered)

    def test_blocks_calendar_only_bundle_without_processor_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-calendar", subject="Calendar Only Review"),
            )

            plan = build_bundle_processing_plan(
                intake_root=intake_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            self.assertEqual(plan.ready_count, 0)
            self.assertEqual(plan.blocked_count, 1)
            self.assertEqual(
                plan.items[0].reasons,
                (
                    "Missing preferred processor input path in bundle metadata.",
                    "Bundle is still calendar-only, so there is no processor-ready transcript artifact yet.",
                ),
            )
            rendered = render_bundle_processing_plan(plan)
            self.assertIn("meeting_bundle_process_blocked_calendar_only: 1", rendered)

    def test_blocks_missing_preferred_input_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-missing", subject="Delivery Review"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
            )
            transcript_path.unlink()

            plan = build_bundle_processing_plan(
                intake_root=intake_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            self.assertEqual(plan.ready_count, 0)
            self.assertEqual(plan.blocked_count, 1)
            self.assertEqual(
                plan.items[0].reasons,
                ("Preferred processor input file does not exist on disk.",),
            )
            rendered = render_bundle_processing_plan(plan)
            self.assertIn("meeting_bundle_process_blocked_missing_input_file: 1", rendered)

    def test_blocks_preferred_input_that_processor_would_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "2026-05-04 - Teams - Delivery Review.md"
            transcript_path.write_text(
                "STATUS: PROCESSED — see [[01_Meetings/already.md]]\nTranscript text\n",
                encoding="utf-8",
            )

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-processed", subject="Delivery Review"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
            )

            plan = build_bundle_processing_plan(
                intake_root=intake_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            self.assertEqual(plan.ready_count, 0)
            self.assertEqual(plan.blocked_count, 1)
            self.assertEqual(
                plan.items[0].reasons,
                ("Preferred processor input would currently be skipped by the existing processor: already processed.",),
            )
            rendered = render_bundle_processing_plan(plan)
            self.assertIn("meeting_bundle_process_blocked_processor_skip: 1", rendered)


class _StubMeetingDiscoveryClient:
    def __init__(self, *, meetings: tuple[OutlookMeetingCandidate, ...]) -> None:
        self._meetings = meetings

    def list_recently_ended_meetings(
        self,
        *,
        since: date,
        now: datetime,
    ) -> MeetingDiscoverySnapshot:
        del since, now
        return MeetingDiscoverySnapshot(meetings=self._meetings, provider_label="stub_outlook_calendar")


def _write_bundle_metadata_sidecar(
    *,
    intake_root: Path,
    meeting: OutlookMeetingCandidate,
    artifact_discovery_client: LocalIntakeTranscriptDiscoveryClient | None = None,
):
    plan = build_transcript_sync_plan(
        client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
        artifact_discovery_client=artifact_discovery_client,
        since=date(2026, 5, 1),
        intake_root=intake_root,
        now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
    )
    note = plan.items[0].intake_bundle_note
    assert note is not None
    note.metadata_path.write_text(note.metadata_content + "\n", encoding="utf-8")
    return note


def _processor_for_vault(vault: Path) -> MeetingProcessor:
    config_path = vault.parent / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f'vault_path: "{vault}"',
                'intake_dir: "00_Intake"',
                'meetings_dir: "01_Meetings"',
                'actions_dir: "07_Actions"',
                'archive_intake_dir: "_Archive/Intake"',
                'templates_dir: "Templates"',
                'owner_filter: "Matthew"',
                "dry_run: false",
                "include_unassigned: false",
                'llm_provider: "none"',
                "codex_model: null",
                'codex_exec_cmd: ["codex", "exec"]',
                'extraction_mode: "draft"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return MeetingProcessor(Config.load(config_path))


def _meeting(
    *,
    event_id: str,
    subject: str,
    discovered_artifacts: tuple[MeetingArtifact, ...] = (),
) -> OutlookMeetingCandidate:
    return OutlookMeetingCandidate(
        event_id=event_id,
        subject=subject,
        start_at=datetime.fromisoformat("2026-05-04T13:00:00+00:00"),
        end_at=datetime.fromisoformat("2026-05-04T13:30:00+00:00"),
        online_meeting_provider="teamsForBusiness",
        join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_delivery%40thread.v2/0?context=%7B%7D",
        discovered_artifacts=discovered_artifacts,
    )


if __name__ == "__main__":
    unittest.main()
