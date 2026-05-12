from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from obsidian_intake_agent.config import Config
from obsidian_intake_agent.meetings import (
    LocalIntakeTranscriptDiscoveryClient,
    MeetingArtifact,
    MeetingDiscoverySnapshot,
    OutlookMeetingCandidate,
    build_bundle_processing_plan,
    build_transcript_sync_plan,
    execute_bundle_processing_plan,
    render_bundle_execution_result,
    render_bundle_processing_plan,
)
from obsidian_intake_agent.processors.meeting_processor import MeetingProcessor, ProcessResult


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
                intake_root=intake_root / "bundles",
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
                intake_root=intake_root / "bundles",
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
                intake_root=intake_root / "bundles",
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
                intake_root=intake_root / "bundles",
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

    def test_blocks_rerun_when_durable_processed_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            bundle_root = intake_root / "bundles"
            bundle_root.mkdir(parents=True)
            transcript_path = bundle_root / "raw_transcripts" / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            processed_marker_path = bundle_root / "_meeting_sync" / "identities" / "evt-processed.json"
            processed_marker_path.parent.mkdir(parents=True, exist_ok=True)
            processed_marker_path.write_text(
                '{"source_type": "meeting_bundle_processed"}\n',
                encoding="utf-8",
            )

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-processed", subject="Delivery Review"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=bundle_root),
            )

            metadata_path = bundle_root / "2026-05-04 - Teams - Delivery Review (outlook).json"
            metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata_payload["processed_marker_path"] = str(processed_marker_path)
            metadata_path.write_text(json.dumps(metadata_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            self.assertEqual(plan.ready_count, 0)
            self.assertEqual(plan.blocked_count, 1)
            self.assertEqual(
                plan.items[0].reasons,
                ("Durable processed marker indicates this bundle was already processed successfully.",),
            )
            rendered = render_bundle_processing_plan(plan)
            self.assertIn("meeting_bundle_process_blocked: 1", rendered)

    def test_execute_processes_ready_bundle_and_renders_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-execute", subject="Delivery Review"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
            )

            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=intake_root / "bundles",
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            self.assertEqual(result.skipped_count, 0)
            self.assertEqual(result.failed_count, 0)
            self.assertEqual(result.items[0].status, "processed")
            self.assertIsNotNone(result.items[0].canonical_note_path)
            rendered = render_bundle_execution_result(result)
            self.assertIn("meeting_bundle_process_mode: execute", rendered)
            self.assertIn("meeting_bundle_process_processed: 1", rendered)
            self.assertIn("reason: Bundle preferred input was processed successfully.", rendered)

    def test_execute_defers_retention_until_ready_bundle_carry_over_sources_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            bundle_root = intake_root / "bundles"
            actions_dir = vault / "07_Actions"
            bundle_root.mkdir(parents=True)
            actions_dir.mkdir(parents=True)
            for name in [
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-14.md",
            ]:
                (actions_dir / name).write_text(f"# Actions — Week of {name.removesuffix('.md')}\n", encoding="utf-8")
            (actions_dir / "2026-04-13.md").write_text(
                "# Actions — Week of 2026-04-13\n\n"
                "## This Week\n\n"
                "- [ ] Preserve this carry-over context (Owner: Matthew Rouser) — Source: 2026-04-13 [[source.md]]\n\n"
                "## Carry Over Items\n\n"
                "## Longer-Term / In Progress\n",
                encoding="utf-8",
            )

            newer_input = bundle_root / "2026-05-12 - Teams - Newer Planning.md"
            newer_input.write_text("Action: Matthew will update the May plan.\n", encoding="utf-8")
            older_input = bundle_root / "2026-04-21 - Teams - Older Planning.md"
            older_input.write_text("Action: Matthew will update the April plan.\n", encoding="utf-8")
            _write_ready_bundle_metadata(
                metadata_path=bundle_root / "a-newer-planning (outlook).json",
                preferred_input=newer_input,
                event_id="evt-newer",
                subject="Newer Planning",
            )
            _write_ready_bundle_metadata(
                metadata_path=bundle_root / "b-older-planning (outlook).json",
                preferred_input=older_input,
                event_id="evt-older",
                subject="Older Planning",
            )

            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 2)
            april_actions_text = (actions_dir / "2026-04-20.md").read_text(encoding="utf-8")
            self.assertIn("Preserve this carry-over context", april_actions_text)
            self.assertFalse((actions_dir / "2026-04-13.md").exists())
            self.assertTrue((actions_dir / "Actions Archive" / "2026-04-13.md").exists())

    def test_execute_bundle_processing_plan_validation_mode_routes_outputs_to_test_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "2026-05-04 - Teams - Platform Sync.vtt"
            transcript_path.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nAction: Matthew will confirm follow-up\n",
                encoding="utf-8",
            )

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-validation", subject="Platform Sync"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
            )

            processor = MeetingProcessor(
                _config_for_vault(vault),
                output_mode="validation",
            )
            plan = build_bundle_processing_plan(
                intake_root=intake_root / "bundles",
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            processed = next(item for item in result.items if item.status == "processed")
            self.assertEqual(
                processed.canonical_note_path,
                vault / "99_Test Notes" / "Meetings" / "2026-05-04 - Teams - Platform Sync.md",
            )
            self.assertEqual(
                processed.actions_file_path,
                vault / "99_Test Notes" / "Actions" / "2026-05-04.md",
            )
            self.assertFalse((vault / "01_Meetings" / "2026-05-04 - Teams - Platform Sync.md").exists())
            self.assertFalse((vault / "07_Actions" / "2026-05-04.md").exists())

    def test_execute_skips_blocked_bundle_without_calling_processor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-blocked", subject="Calendar Only Review"),
            )

            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=intake_root / "bundles",
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            with patch.object(processor, "process_file") as process_file_mock:
                result = execute_bundle_processing_plan(plan, processor=processor)

            process_file_mock.assert_not_called()
            self.assertEqual(result.processed_count, 0)
            self.assertEqual(result.skipped_count, 1)
            self.assertEqual(result.items[0].reasons, plan.items[0].reasons)

    def test_execute_reports_processor_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-fail", subject="Delivery Review"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
            )

            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=intake_root / "bundles",
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            with patch.object(processor, "process_file", side_effect=RuntimeError("boom")):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 0)
            self.assertEqual(result.failed_count, 1)
            self.assertEqual(result.items[0].status, "failed")
            self.assertEqual(result.items[0].reasons, ("Processor execution failed: boom",))

    def test_execute_skips_when_processor_returns_skip_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)
            transcript_path = intake_root / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-skip", subject="Delivery Review"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root),
            )

            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=intake_root / "bundles",
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            with patch.object(
                processor,
                "process_file",
                return_value=ProcessResult(processed=False, skip_reason="already processed"),
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 0)
            self.assertEqual(result.skipped_count, 1)
            self.assertEqual(result.items[0].reasons, ("Processor skipped preferred input: already processed.",))


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
    note.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    note.metadata_path.write_text(note.metadata_content + "\n", encoding="utf-8")
    return note


def _write_ready_bundle_metadata(
    *,
    metadata_path: Path,
    preferred_input: Path,
    event_id: str,
    subject: str,
) -> None:
    metadata_path.write_text(
        json.dumps(
            {
                "source_type": "outlook_meeting_bundle",
                "outlook_event_id": event_id,
                "subject": subject,
                "processor_handoff": {
                    "preferred_input_path": str(preferred_input),
                    "preferred_input_source_name": "Local transcript",
                },
                "artifacts": [
                    {
                        "source_name": "Local transcript",
                        "status": "available",
                        "detail": None,
                        "matched_paths": [str(preferred_input)],
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _processor_for_vault(vault: Path) -> MeetingProcessor:
    return MeetingProcessor(_config_for_vault(vault))


def _config_for_vault(vault: Path) -> Config:
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
    return Config.load(config_path)


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
        response_status="accepted",
        discovered_artifacts=discovered_artifacts,
    )


if __name__ == "__main__":
    unittest.main()
