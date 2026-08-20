from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import obsidian_intake_agent.meetings.process_bundles as process_bundles_module
import obsidian_intake_agent.meetings.sync as meeting_sync_module
from obsidian_intake_agent.config import Config
from obsidian_intake_agent.meetings import (
    GraphTranscriptDownloadClient,
    LocalIntakeTranscriptDiscoveryClient,
    MeetingArtifact,
    MeetingDiscoverySnapshot,
    OutlookMeetingCandidate,
    attach_transcript_to_bundle,
    build_bundle_processing_plan,
    build_transcript_sync_plan,
    execute_bundle_processing_plan,
    render_bundle_execution_result,
    render_bundle_processing_plan,
    write_planned_bundle_notes,
)
from obsidian_intake_agent.meetings.transcript_provenance import provenance_path, write_provenance
from obsidian_intake_agent.processors.meeting_processor import MeetingProcessor, ProcessResult


class BundleProcessingPlanTests(unittest.TestCase):
    def test_processor_ready_bundle_blocks_missing_authoritative_event_id_without_filename_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            metadata_path = bundle_root / "must-not-become-event-id (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=metadata_path,
                preferred_input=transcript_path,
                event_id="remove-me",
                subject="Delivery Review",
                preferred_source="Teams .vtt transcript",
            )
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload.pop("outlook_event_id")
            payload.pop("primary_occurrence_event_id", None)
            metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertIsNone(plan.items[0].metadata.event_id)
            self.assertIsNone(plan.items[0].metadata.occurrence_event_id)
            self.assertEqual(
                plan.items[0].reasons,
                ("Bundle metadata requires an authoritative Outlook occurrence/event ID.",),
            )

    def test_processor_ready_legacy_sidecar_with_outlook_event_id_remains_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            metadata_path = bundle_root / "Delivery Review (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=metadata_path,
                preferred_input=transcript_path,
                event_id="evt-legacy",
                subject="Delivery Review",
                preferred_source="Teams .vtt transcript",
            )
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload.pop("primary_occurrence_event_id", None)
            metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(plan.items[0].metadata.event_id, "evt-legacy")
            self.assertEqual(plan.items[0].decision, "ready")

    def test_processor_ready_bundle_blocks_missing_or_blank_outlook_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            processor = _processor_for_vault(vault)

            for index, subject in enumerate((None, "   ")):
                with self.subTest(subject=subject):
                    metadata_path = bundle_root / f"subject-{index} (outlook).json"
                    _write_ready_bundle_metadata(
                        metadata_path=metadata_path,
                        preferred_input=transcript_path,
                        event_id=f"evt-subject-{index}",
                        subject="Delivery Review",
                        preferred_source="Teams .vtt transcript",
                    )
                    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if subject is None:
                        payload.pop("subject")
                    else:
                        payload["subject"] = subject
                    metadata_path.write_text(
                        json.dumps(payload, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(plan.blocked_count, 2)
            for item in plan.items:
                self.assertEqual(
                    item.reasons,
                    ("Bundle metadata requires a nonblank Outlook subject.",),
                )

    def test_processor_ready_bundle_blocks_invalid_authoritative_schedule_fields(self) -> None:
        invalid_cases = (
            ("scheduled_start_at", None),
            ("scheduled_start_at", "not-a-date"),
            ("scheduled_start_at", "2026-05-04T13:00:00"),
            ("scheduled_end_at", None),
            ("scheduled_end_at", "not-a-date"),
            ("scheduled_end_at", "2026-05-04T13:30:00"),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            processor = _processor_for_vault(vault)

            for index, (field, value) in enumerate(invalid_cases):
                metadata_path = bundle_root / f"schedule-{index} (outlook).json"
                _write_ready_bundle_metadata(
                    metadata_path=metadata_path,
                    preferred_input=transcript_path,
                    event_id=f"evt-schedule-{index}",
                    subject="Delivery Review",
                    preferred_source="Teams .vtt transcript",
                )
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                if value is None:
                    payload.pop(field)
                else:
                    payload[field] = value
                metadata_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(plan.blocked_count, len(invalid_cases))
            for item, (field, _value) in zip(plan.items, invalid_cases, strict=True):
                self.assertEqual(
                    item.reasons,
                    (f"Bundle metadata requires a valid timezone-aware {field}.",),
                )

    def test_processor_ready_bundle_blocks_reverse_schedule_same_and_cross_offset(self) -> None:
        invalid_schedules = (
            ("2026-05-04T13:30:00+00:00", "2026-05-04T13:00:00+00:00"),
            ("2026-05-04T13:00:00+00:00", "2026-05-04T14:00:00+02:00"),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")

            for index, (start_at, end_at) in enumerate(invalid_schedules):
                metadata_path = bundle_root / f"reverse-schedule-{index} (outlook).json"
                _write_ready_bundle_metadata(
                    metadata_path=metadata_path,
                    preferred_input=transcript_path,
                    event_id=f"evt-reverse-{index}",
                    subject="Delivery Review",
                    preferred_source="Teams .vtt transcript",
                )
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                payload["scheduled_start_at"] = start_at
                payload["scheduled_end_at"] = end_at
                metadata_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T15:00:00+00:00"),
            )

            self.assertEqual(plan.blocked_count, 2)
            for item in plan.items:
                self.assertEqual(
                    item.reasons,
                    ("Bundle metadata scheduled_end_at must not be before scheduled_start_at.",),
                )

    def test_recap_blocks_fallback_deadline_before_end_same_and_cross_offset(self) -> None:
        invalid_deadlines = (
            (
                "2026-05-04T13:00:00+00:00",
                "2026-05-04T13:30:00+00:00",
                "2026-05-04T13:29:59+00:00",
            ),
            (
                "2026-05-04T13:00:00+00:00",
                "2026-05-04T13:30:00+00:00",
                "2026-05-04T15:00:00+02:00",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            fallback_path = bundle_root / "fallbacks" / "Delivery Review (fallback).md"
            fallback_path.parent.mkdir(parents=True)
            fallback_path.write_text("Summary-derived content.\n", encoding="utf-8")

            for index, (start_at, end_at, fallback_not_before) in enumerate(invalid_deadlines):
                metadata_path = bundle_root / f"reverse-fallback-{index} (outlook).json"
                _write_ready_bundle_metadata(
                    metadata_path=metadata_path,
                    preferred_input=fallback_path,
                    event_id=f"evt-reverse-fallback-{index}",
                    subject="Delivery Review",
                    preferred_source="Copilot recap / AI summary",
                )
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                payload["scheduled_start_at"] = start_at
                payload["scheduled_end_at"] = end_at
                payload["fallback_not_before"] = fallback_not_before
                metadata_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T15:00:00+00:00"),
            )

            self.assertEqual(plan.blocked_count, 2)
            for item in plan.items:
                self.assertEqual(
                    item.reasons,
                    ("Recap fallback_not_before must not be before scheduled_end_at.",),
                )

    def test_equal_schedule_and_fallback_boundaries_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            transcript_metadata_path = bundle_root / "equal-schedule (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=transcript_metadata_path,
                preferred_input=transcript_path,
                event_id="evt-equal-schedule",
                subject="Delivery Review",
                preferred_source="Teams .vtt transcript",
            )
            transcript_payload = json.loads(transcript_metadata_path.read_text(encoding="utf-8"))
            transcript_payload["scheduled_end_at"] = transcript_payload["scheduled_start_at"]
            transcript_metadata_path.write_text(
                json.dumps(transcript_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            fallback_path = bundle_root / "fallbacks" / "Delivery Review (fallback).md"
            fallback_path.parent.mkdir(parents=True)
            fallback_path.write_text("Summary-derived content.\n", encoding="utf-8")
            fallback_metadata_path = bundle_root / "equal-fallback (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=fallback_metadata_path,
                preferred_input=fallback_path,
                event_id="evt-equal-fallback",
                subject="Delivery Review",
                preferred_source="Copilot recap / AI summary",
            )
            fallback_payload = json.loads(fallback_metadata_path.read_text(encoding="utf-8"))
            fallback_payload["fallback_not_before"] = fallback_payload["scheduled_end_at"]
            fallback_metadata_path.write_text(
                json.dumps(fallback_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T15:00:00+00:00"),
            )

            self.assertEqual(plan.ready_count, 2)

    def test_recap_blocks_missing_malformed_or_naive_fallback_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            fallback_path = bundle_root / "fallbacks" / "Delivery Review (fallback).md"
            fallback_path.parent.mkdir(parents=True)
            fallback_path.write_text("Summary-derived content.\n", encoding="utf-8")
            processor = _processor_for_vault(vault)

            for index, fallback_not_before in enumerate((None, "not-a-date", "2026-05-04T14:30:00")):
                metadata_path = bundle_root / f"fallback-deadline-{index} (outlook).json"
                _write_ready_bundle_metadata(
                    metadata_path=metadata_path,
                    preferred_input=fallback_path,
                    event_id=f"evt-fallback-deadline-{index}",
                    subject="Delivery Review",
                    preferred_source="Copilot recap / AI summary",
                )
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                if fallback_not_before is not None:
                    payload["fallback_not_before"] = fallback_not_before
                metadata_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T15:00:00+00:00"),
            )

            self.assertEqual(plan.blocked_count, 3)
            for item in plan.items:
                self.assertEqual(
                    item.reasons,
                    ("Recap fallback requires a valid timezone-aware fallback_not_before.",),
                )

    def test_deferred_recap_is_blocked_before_fallback_deadline_and_ready_at_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            fallback_path = bundle_root / "fallbacks" / "Delivery Review (fallback).md"
            fallback_path.parent.mkdir(parents=True)
            fallback_path.write_text("Summary-derived content.\n", encoding="utf-8")
            metadata_path = bundle_root / "Delivery Review (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=metadata_path,
                preferred_input=fallback_path,
                event_id="evt-deferred",
                subject="Delivery Review",
                preferred_source="Copilot recap / AI summary",
            )
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload["fallback_not_before"] = "2026-05-04T14:30:00+00:00"
            metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            processor = _processor_for_vault(vault)

            deferred = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:29:59+00:00"),
            )
            boundary = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:30:00+00:00"),
            )

            self.assertEqual(deferred.items[0].decision, "blocked")
            self.assertEqual(
                deferred.items[0].reasons,
                ("Recap fallback is deferred until 2026-05-04T14:30:00+00:00.",),
            )
            self.assertEqual(boundary.items[0].decision, "ready")

    def test_deferred_recap_compares_cross_offset_deadline_and_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            fallback_path = bundle_root / "fallbacks" / "Delivery Review (fallback).md"
            fallback_path.parent.mkdir(parents=True)
            fallback_path.write_text("Summary-derived content.\n", encoding="utf-8")
            metadata_path = bundle_root / "Delivery Review (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=metadata_path,
                preferred_input=fallback_path,
                event_id="evt-cross-offset",
                subject="Delivery Review",
                preferred_source="Copilot recap / AI summary",
            )
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload["fallback_not_before"] = "2026-05-04T16:30:00+02:00"
            metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            processor = _processor_for_vault(vault)

            deferred = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:29:59+00:00"),
            )
            boundary = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:30:00+00:00"),
            )

            self.assertEqual(
                deferred.items[0].reasons,
                ("Recap fallback is deferred until 2026-05-04T16:30:00+02:00.",),
            )
            self.assertEqual(boundary.items[0].decision, "ready")

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

    def test_dry_run_calendar_only_bundle_prints_local_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            intake_root.mkdir(parents=True)

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-calendar", subject="Delivery Review"),
            )

            plan = build_bundle_processing_plan(
                intake_root=intake_root / "bundles",
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )
            rendered = render_bundle_processing_plan(plan)

        self.assertIn("expected_local_transcript_stem: 2026-05-04 - Teams - Delivery Review", rendered)
        self.assertIn(
            "next_step: add or attach a local .vtt, .md, or .docx transcript, then rerun process-bundles --dry-run",
            rendered,
        )

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

    def test_dry_run_missing_preferred_file_prints_missing_path_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            bundle_root = intake_root / "bundles"
            bundle_root.mkdir(parents=True)
            missing_input = bundle_root / "raw_transcripts" / "2026-05-04 - Teams - Delivery Review.vtt"
            _write_ready_bundle_metadata(
                metadata_path=bundle_root / "delivery-review (outlook).json",
                preferred_input=missing_input,
                event_id="evt-missing",
                subject="Delivery Review",
                preferred_source="Teams .vtt transcript",
            )

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )
            rendered = render_bundle_processing_plan(plan)

        self.assertIn(f"missing_preferred_input: {missing_input}", rendered)
        self.assertIn("preferred_source: Teams .vtt transcript", rendered)

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
            metadata_payload["subject"] = ""
            metadata_payload["scheduled_start_at"] = "not-a-date"
            metadata_payload["scheduled_end_at"] = "2026-05-04T13:30:00"
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

    def test_dangling_processed_marker_symlink_rejects_unsafe_bundle_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            bundle_root = intake_root / "bundles"
            bundle_root.mkdir(parents=True)
            transcript_path = bundle_root / "raw_transcripts" / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            processed_marker_path = bundle_root / "_meeting_sync" / "identities" / "evt-dangling.json"
            processed_marker_path.parent.mkdir(parents=True, exist_ok=True)
            processed_marker_path.symlink_to("missing-processed-target.json")

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-dangling", subject="Delivery Review"),
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

            self.assertEqual(plan.candidate_count, 0)
            self.assertEqual(len(plan.warnings), 1)
            self.assertIn("processed marker path must not be a symlink", plan.warnings[0])

    def test_external_processed_marker_path_rejects_unsafe_bundle_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, metadata_path, _marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["processed_marker_path"] = str(vault / "outside-identities" / "evt-marker.json")
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(plan.candidate_count, 0)
            self.assertEqual(len(plan.warnings), 1)
            self.assertIn("processed marker path must be directly under", plan.warnings[0])

    def test_parent_traversal_processed_marker_path_rejects_unsafe_bundle_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            outside = Path(tmp_dir) / "outside"
            outside.mkdir()
            traversal_path = outside / ".." / marker_path.relative_to(Path(tmp_dir))
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["processed_marker_path"] = str(traversal_path)
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(plan.candidate_count, 0)
            self.assertEqual(len(plan.warnings), 1)
            self.assertIn("processed marker path must not contain parent traversal", plan.warnings[0])

    def test_symlinked_processed_marker_ancestor_rejects_unsafe_bundle_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            marker_path.unlink()
            identities_dir = marker_path.parent
            identities_dir.rmdir()
            external_identities = vault / "external-identities"
            external_identities.mkdir()
            identities_dir.symlink_to(external_identities, target_is_directory=True)

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(plan.candidate_count, 0)
            self.assertEqual(len(plan.warnings), 1)
            self.assertIn("processed marker path ancestor must not be a symlink", plan.warnings[0])
            self.assertTrue(metadata_path.exists())

    def test_symlinked_bundle_root_rejects_unsafe_processed_marker_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, real_bundle_root, _input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            linked_bundle_root = vault / "linked-bundles"
            linked_bundle_root.symlink_to(real_bundle_root, target_is_directory=True)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["processed_marker_path"] = str(linked_bundle_root / marker_path.relative_to(real_bundle_root))
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            plan = build_bundle_processing_plan(
                intake_root=linked_bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            self.assertEqual(plan.candidate_count, 0)
            self.assertEqual(len(plan.warnings), 1)
            self.assertIn("bundle root must not be a symlink", plan.warnings[0])

    def test_legacy_identity_marker_blocks_ready_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            bundle_root = intake_root / "bundles"
            bundle_root.mkdir(parents=True)
            transcript_path = bundle_root / "raw_transcripts" / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            identity_path = bundle_root / "_meeting_sync" / "identities" / "evt-staged.json"
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            identity_path.write_text(
                '{"source_type": "meeting_sync_identity"}\n',
                encoding="utf-8",
            )

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-staged", subject="Delivery Review"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=bundle_root),
            )

            metadata_path = bundle_root / "2026-05-04 - Teams - Delivery Review (outlook).json"
            metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata_payload["processed_marker_path"] = str(identity_path)
            metadata_path.write_text(json.dumps(metadata_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            self.assertEqual(plan.ready_count, 0)
            self.assertEqual(plan.blocked_count, 1)
            self.assertIn("already processed successfully", plan.items[0].reasons[0])
            self.assertEqual(plan.items[0].decision, "blocked")

    def test_v2_fallback_marker_inside_retry_window_blocks_ordinary_bundle_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_root = vault / "00_Intake"
            bundle_root = intake_root / "bundles"
            bundle_root.mkdir(parents=True)
            transcript_path = bundle_root / "raw_transcripts" / "2026-05-04 - Teams - Delivery Review.vtt"
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            identity_path = bundle_root / "_meeting_sync" / "identities" / "evt-fallback.json"
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            identity_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "meeting_bundle_processed",
                        "processing_source_kind": "fallback",
                        "upgrade_state": "awaiting_transcript",
                        "retry_until": "2026-05-06T12:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-fallback", subject="Delivery Review"),
                artifact_discovery_client=LocalIntakeTranscriptDiscoveryClient(intake_root=bundle_root),
            )

            metadata_path = bundle_root / "2026-05-04 - Teams - Delivery Review (outlook).json"
            metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata_payload["processed_marker_path"] = str(identity_path)
            metadata_path.write_text(json.dumps(metadata_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            self.assertEqual(plan.ready_count, 0)
            self.assertEqual(plan.blocked_count, 1)
            self.assertEqual(plan.items[0].decision, "blocked")

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

    def test_processed_marker_fallback_writes_v2_upgrade_state_hash_and_allowlisted_pending_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Copilot recap / AI summary",
                extension=".md",
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.update(
                {
                    "fallback_not_before": "2026-05-04T13:30:00+00:00",
                    "retry_until": "2026-05-05T13:30:00+00:00",
                    "first_seen_at": "2026-05-04T13:31:00+00:00",
                    "primary_occurrence_event_id": "evt-primary",
                    "identity_key": "evt-primary|teams-123",
                    "teams_meeting_id": "teams-123",
                    "outlook_event_id": "evt-primary",
                }
            )
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            pending = {
                "source_type": "meeting_sync_pending",
                "first_seen_at": "2026-05-04T13:31:00+00:00",
                "retry_until": "2026-05-05T13:30:00+00:00",
                "primary_occurrence_event_id": "evt-primary",
                "scheduled_start_at": "2026-05-04T13:00:00+00:00",
                "scheduled_end_at": "2026-05-04T13:30:00+00:00",
                "selection_window_start_at": "2026-05-04T12:45:00+00:00",
                "selection_window_end_at": "2026-05-04T14:00:00+00:00",
                "identity_key": "evt-primary|teams-123",
                "teams_meeting_id": "teams-123",
                "outlook_event_id": "evt-primary",
                "access_token": "must-not-survive",
                "unknown_private_context": {"content": "must-not-survive"},
            }
            marker_path.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            canonical_path = result.items[0].canonical_note_path
            assert canonical_path is not None
            self.assertEqual(marker["source_type"], "meeting_bundle_processed")
            self.assertEqual(marker["schema_version"], 2)
            self.assertIs(type(marker["schema_version"]), int)
            self.assertEqual(marker["processing_source_kind"], "fallback")
            self.assertEqual(marker["upgrade_state"], "awaiting_transcript")
            self.assertEqual(marker["retry_until"], pending["retry_until"])
            for key in (
                "first_seen_at",
                "primary_occurrence_event_id",
                "scheduled_start_at",
                "scheduled_end_at",
                "selection_window_start_at",
                "selection_window_end_at",
                "identity_key",
                "teams_meeting_id",
                "outlook_event_id",
            ):
                self.assertEqual(marker[key], pending[key])
            self.assertNotIn("access_token", marker)
            self.assertNotIn("unknown_private_context", marker)
            self.assertEqual(marker["selected_transcripts"], [])
            self.assertEqual(marker["canonical_note_path"], str(canonical_path))
            self.assertEqual(
                marker["canonical_note_sha256"],
                hashlib.sha256(canonical_path.read_bytes()).hexdigest(),
            )
            self.assertFalse(input_path.exists())
            self.assertFalse(metadata_path.exists())

    def test_processed_marker_transcript_is_terminal_with_chronological_selected_ids_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Teams .vtt transcript",
                extension=".vtt",
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["artifacts"][0]["transcript_diagnostics"] = {
                "candidate_count": 2,
                "assignment_conflicts": [],
                "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                "selected_transcripts": [
                    {"id": "segment-2", "created_at": "2026-05-04T13:50:00+00:00"},
                    {"id": "segment-1", "created_at": "2026-05-04T13:40:00+00:00"},
                ],
                "access_token": "must-not-survive",
                "content": "must-not-survive",
            }
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            canonical_path = result.items[0].canonical_note_path
            assert canonical_path is not None
            self.assertEqual(marker["processing_source_kind"], "transcript")
            self.assertEqual(marker["upgrade_state"], "terminal")
            self.assertEqual(
                marker["selected_transcripts"],
                [
                    {"id": "segment-1", "created_at": "2026-05-04T13:40:00+00:00"},
                    {"id": "segment-2", "created_at": "2026-05-04T13:50:00+00:00"},
                ],
            )
            self.assertEqual(
                marker["canonical_note_sha256"],
                hashlib.sha256(canonical_path.read_bytes()).hexdigest(),
            )
            self.assertNotIn("transcript_diagnostics", marker["artifacts"][0])
            self.assertNotIn("access_token", json.dumps(marker))
            self.assertNotIn("must-not-survive", json.dumps(marker))

    def test_processed_marker_manual_input_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["processing_source_kind"], "manual")
            self.assertEqual(marker["upgrade_state"], "terminal")
            self.assertEqual(marker["selected_transcripts"], [])

    def test_processed_marker_hash_detects_later_canonical_note_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            result = execute_bundle_processing_plan(plan, processor=processor)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            canonical_path = result.items[0].canonical_note_path
            assert canonical_path is not None

            canonical_path.write_bytes(canonical_path.read_bytes() + b"\nManual edit.\n")

            self.assertNotEqual(
                marker["canonical_note_sha256"],
                hashlib.sha256(canonical_path.read_bytes()).hexdigest(),
            )

    def test_processed_marker_missing_fallback_deadline_fails_without_replacing_pending_or_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Copilot recap / AI summary",
                extension=".md",
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["fallback_not_before"] = "2026-05-04T13:30:00+00:00"
            metadata.pop("retry_until", None)
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            pending = json.loads(marker_path.read_text(encoding="utf-8"))
            pending.pop("retry_until", None)
            marker_path.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertIn("valid timezone-aware retry_until", result.items[0].reasons[0])
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8")), pending)
            self.assertTrue(input_path.exists())
            self.assertTrue(metadata_path.exists())

    def test_processed_marker_conflicting_transcript_diagnostics_fail_without_terminal_marker_or_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Teams transcript text",
                extension=".md",
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["artifacts"][0]["transcript_diagnostics"] = {
                "candidate_count": 2,
                "assignment_conflicts": [],
                "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                "selected_transcripts": [
                    {"id": "same-id", "created_at": "2026-05-04T13:40:00+00:00"},
                    {"id": "same-id", "created_at": "2026-05-04T13:50:00+00:00"},
                ],
            }
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            pending = marker_path.read_text(encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertIn("selected transcript IDs must be unique", result.items[0].reasons[0])
            self.assertEqual(marker_path.read_text(encoding="utf-8"), pending)
            self.assertTrue(input_path.exists())
            self.assertTrue(metadata_path.exists())

    def test_processed_marker_missing_transcript_diagnostics_requires_explicit_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Teams .vtt transcript",
                extension=".vtt",
            )
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["processing_source_kind"], "transcript")
            self.assertEqual(marker["upgrade_state"], "manual_review_required")
            self.assertEqual(marker["selected_transcripts"], [])

    def test_processed_marker_write_failure_retains_pending_marker_and_bundle_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            pending = marker_path.read_text(encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
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
            self.assertEqual(marker_path.read_text(encoding="utf-8"), pending)
            self.assertTrue(input_path.exists())
            self.assertTrue(metadata_path.exists())

    def test_processed_marker_concurrent_terminal_replacement_before_processor_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            terminal = {
                "schema_version": 2,
                "source_type": "meeting_bundle_processed",
                "processing_source_kind": "manual",
                "upgrade_state": "terminal",
            }
            original_capture = process_bundles_module._capture_bundle_staging

            def replace_marker_then_capture(**kwargs: object) -> object:
                marker_path.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
                return original_capture(**kwargs)

            with (
                patch.object(
                    process_bundles_module,
                    "_capture_bundle_staging",
                    side_effect=replace_marker_then_capture,
                ),
                patch.object(processor, "process_file") as process_file,
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            process_file.assert_not_called()
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8")), terminal)

    def test_processed_marker_concurrent_terminal_replacement_after_processor_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            canonical_path = vault / "01_Meetings" / "2026-05-04 - Teams - Delivery Review.md"
            terminal = {
                "schema_version": 2,
                "source_type": "meeting_bundle_processed",
                "processing_source_kind": "manual",
                "upgrade_state": "terminal",
            }

            def process_then_replace_marker(*_args: object, **_kwargs: object) -> ProcessResult:
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                canonical_path.write_text("# Delivery Review\n", encoding="utf-8")
                marker_path.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
                return ProcessResult(processed=True, canonical_note_path=canonical_path)

            with patch.object(processor, "process_file", side_effect=process_then_replace_marker):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8")), terminal)
            self.assertTrue(input_path.exists())

    def test_processed_marker_compare_failure_at_final_write_preserves_winner_and_restores_staging(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            winning_marker = {
                "source_type": "meeting_bundle_processed",
                "schema_version": 2,
                "processing_source_kind": "manual",
                "upgrade_state": "terminal",
                "executor": "winner",
            }
            original_compare_and_write = process_bundles_module.IdentityMarkerTransaction.compare_and_write

            def win_race_then_compare(
                transaction: object,
                expected_bytes_or_missing: bytes | None,
                payload: dict[str, object],
            ) -> None:
                assert isinstance(transaction, process_bundles_module.IdentityMarkerTransaction)
                transaction.write(winning_marker)
                original_compare_and_write(transaction, expected_bytes_or_missing, payload)

            with patch.object(
                process_bundles_module.IdentityMarkerTransaction,
                "compare_and_write",
                autospec=True,
                side_effect=win_race_then_compare,
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8")), winning_marker)
            self.assertTrue(input_path.exists())
            self.assertTrue(metadata_path.exists())

    def test_concurrent_full_executors_serialize_processor_and_hash_winner_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            canonical_path = vault / "01_Meetings" / "2026-05-04 - Teams - Delivery Review.md"
            start_barrier = Barrier(2)
            processor_started = Event()
            second_processor_started = Event()
            release_processor = Event()
            call_lock = Lock()
            processor_calls = 0

            def process_once(*_args: object, **_kwargs: object) -> ProcessResult:
                nonlocal processor_calls
                with call_lock:
                    processor_calls += 1
                    call_number = processor_calls
                    if processor_calls == 2:
                        second_processor_started.set()
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                canonical_path.write_bytes(f"executor-{call_number}\n".encode())
                processor_started.set()
                release_processor.wait(timeout=2)
                return ProcessResult(processed=True, canonical_note_path=canonical_path)

            def execute() -> object:
                start_barrier.wait()
                return execute_bundle_processing_plan(plan, processor=processor)

            with (
                patch.object(processor, "process_file", side_effect=process_once),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [executor.submit(execute) for _ in range(2)]
                self.assertTrue(processor_started.wait(timeout=1))
                second_processor_started.wait(timeout=0.2)
                release_processor.set()
                results = [future.result(timeout=3) for future in futures]

            self.assertEqual(processor_calls, 1)
            self.assertEqual(sum(result.processed_count for result in results), 1)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(
                marker["canonical_note_sha256"],
                hashlib.sha256(canonical_path.read_bytes()).hexdigest(),
            )

    def test_different_identity_failure_rollback_preserves_committed_shared_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                vault,
                plans,
                marker_paths,
                canonical_paths,
                actions_path,
                processor,
            ) = _ready_two_marker_execution_plans(tmp_dir)
            start_barrier = Barrier(2)
            processor_started = Event()
            second_processor_started = Event()
            release_processors = Event()
            activity_lock = Lock()
            active_processors = 0
            maximum_active_processors = 0

            def process_shared_actions(
                path: Path,
                **kwargs: object,
            ) -> ProcessResult:
                nonlocal active_processors, maximum_active_processors
                meeting_metadata = kwargs["meeting_metadata"]
                assert isinstance(meeting_metadata, process_bundles_module.MeetingMetadata)
                label = "alpha" if "Alpha" in path.name else "beta"
                existing_actions = actions_path.read_bytes() if actions_path.exists() else b""
                with activity_lock:
                    active_processors += 1
                    maximum_active_processors = max(maximum_active_processors, active_processors)
                    if active_processors == 2:
                        second_processor_started.set()
                processor_started.set()
                release_processors.wait(timeout=2)
                canonical_path = processor.meetings_path / meeting_metadata.canonical_basename
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                actions_path.parent.mkdir(parents=True, exist_ok=True)
                canonical_path.write_bytes(f"canonical-{label}\n".encode())
                actions_path.write_bytes(existing_actions + f"action-{label}\n".encode())
                with activity_lock:
                    active_processors -= 1
                return ProcessResult(
                    processed=True,
                    canonical_note_path=canonical_path,
                    actions_file_path=actions_path,
                )

            original_compare_and_write = process_bundles_module.IdentityMarkerTransaction.compare_and_write

            def fail_beta_marker(
                transaction: object,
                expected_bytes_or_missing: bytes | None,
                payload: dict[str, object],
            ) -> None:
                assert isinstance(transaction, process_bundles_module.IdentityMarkerTransaction)
                if transaction.path == marker_paths["beta"]:
                    raise OSError("beta marker failure")
                original_compare_and_write(transaction, expected_bytes_or_missing, payload)

            def execute(plan: object) -> object:
                start_barrier.wait()
                return execute_bundle_processing_plan(plan, processor=processor)

            with (
                patch.object(processor, "process_file", side_effect=process_shared_actions),
                patch.object(
                    process_bundles_module.IdentityMarkerTransaction,
                    "compare_and_write",
                    autospec=True,
                    side_effect=fail_beta_marker,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [executor.submit(execute, plan) for plan in plans]
                self.assertTrue(processor_started.wait(timeout=1))
                second_processor_started.wait(timeout=0.2)
                release_processors.set()
                results = [future.result(timeout=3) for future in futures]

            self.assertEqual(maximum_active_processors, 1)
            self.assertEqual(sum(result.processed_count for result in results), 1)
            self.assertEqual(sum(result.failed_count for result in results), 1)
            self.assertEqual(actions_path.read_bytes(), b"action-alpha\n")
            self.assertEqual(canonical_paths["alpha"].read_bytes(), b"canonical-alpha\n")
            self.assertFalse(canonical_paths["beta"].exists())
            self.assertEqual(
                json.loads(marker_paths["alpha"].read_text(encoding="utf-8"))["source_type"],
                "meeting_bundle_processed",
            )
            self.assertEqual(
                json.loads(marker_paths["beta"].read_text(encoding="utf-8"))["source_type"],
                "meeting_sync_pending",
            )
            self.assertTrue(vault.exists())

    def test_different_identity_successes_serialize_and_preserve_both_shared_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                _vault,
                plans,
                marker_paths,
                canonical_paths,
                actions_path,
                processor,
            ) = _ready_two_marker_execution_plans(tmp_dir)
            start_barrier = Barrier(2)
            processor_started = Event()
            second_processor_started = Event()
            release_processors = Event()
            activity_lock = Lock()
            active_processors = 0
            maximum_active_processors = 0

            def process_shared_actions(
                path: Path,
                **kwargs: object,
            ) -> ProcessResult:
                nonlocal active_processors, maximum_active_processors
                meeting_metadata = kwargs["meeting_metadata"]
                assert isinstance(meeting_metadata, process_bundles_module.MeetingMetadata)
                label = "alpha" if "Alpha" in path.name else "beta"
                existing_actions = actions_path.read_bytes() if actions_path.exists() else b""
                with activity_lock:
                    active_processors += 1
                    maximum_active_processors = max(maximum_active_processors, active_processors)
                    if active_processors == 2:
                        second_processor_started.set()
                processor_started.set()
                release_processors.wait(timeout=2)
                canonical_path = processor.meetings_path / meeting_metadata.canonical_basename
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                actions_path.parent.mkdir(parents=True, exist_ok=True)
                canonical_path.write_bytes(f"canonical-{label}\n".encode())
                actions_path.write_bytes(existing_actions + f"action-{label}\n".encode())
                with activity_lock:
                    active_processors -= 1
                return ProcessResult(
                    processed=True,
                    canonical_note_path=canonical_path,
                    actions_file_path=actions_path,
                )

            def execute(plan: object) -> object:
                start_barrier.wait()
                return execute_bundle_processing_plan(plan, processor=processor)

            with (
                patch.object(processor, "process_file", side_effect=process_shared_actions),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [executor.submit(execute, plan) for plan in plans]
                self.assertTrue(processor_started.wait(timeout=1))
                second_processor_started.wait(timeout=0.2)
                release_processors.set()
                results = [future.result(timeout=3) for future in futures]

            self.assertEqual(maximum_active_processors, 1)
            self.assertEqual(sum(result.processed_count for result in results), 2)
            self.assertCountEqual(
                actions_path.read_text(encoding="utf-8").splitlines(),
                ["action-alpha", "action-beta"],
            )
            for label in ("alpha", "beta"):
                self.assertEqual(canonical_paths[label].read_bytes(), f"canonical-{label}\n".encode())
                self.assertEqual(
                    json.loads(marker_paths[label].read_text(encoding="utf-8"))["source_type"],
                    "meeting_bundle_processed",
                )

    def test_bundle_execution_lock_rejects_symlink_and_hard_link_alias(self) -> None:
        for lock_kind in ("symlink", "hard-link"):
            with self.subTest(lock_kind=lock_kind), tempfile.TemporaryDirectory() as tmp_dir:
                vault, bundle_root, _input_path, _metadata_path, _marker_path = _ready_marker_bundle(
                    tmp_dir=tmp_dir,
                    preferred_source="Manual / semi-manual intake",
                    extension=".md",
                )
                lock_path = bundle_root / "_meeting_sync" / ".bundle-processing.lock"
                target_path = vault / "bundle-lock-target"
                target_path.write_bytes(b"private lock target")
                if lock_kind == "symlink":
                    lock_path.symlink_to(target_path)
                else:
                    os.link(target_path, lock_path)
                processor = _processor_for_vault(vault)
                plan = build_bundle_processing_plan(
                    intake_root=bundle_root,
                    processor=processor,
                    now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
                )

                with patch.object(processor, "process_file") as process_file:
                    result = execute_bundle_processing_plan(plan, processor=processor)

                self.assertEqual(result.failed_count, 1)
                self.assertIn("bundle processing lock file", result.items[0].reasons[0])
                process_file.assert_not_called()
                self.assertEqual(target_path.read_bytes(), b"private lock target")

    def test_marker_failure_rolls_back_existing_or_absent_outputs_and_skips_retention(self) -> None:
        for outputs_preexisting in (False, True):
            with self.subTest(outputs_preexisting=outputs_preexisting), tempfile.TemporaryDirectory() as tmp_dir:
                vault, bundle_root, input_path, metadata_path, _marker_path = _ready_marker_bundle(
                    tmp_dir=tmp_dir,
                    preferred_source="Manual / semi-manual intake",
                    extension=".md",
                )
                processor = _processor_for_vault(vault)
                plan = build_bundle_processing_plan(
                    intake_root=bundle_root,
                    processor=processor,
                    now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
                )
                canonical_path = vault / "01_Meetings" / "2026-05-04 - Teams - Delivery Review.md"
                actions_path = vault / "07_Actions" / "2026-05-04.md"
                canonical_before = b"existing canonical\n"
                actions_before = b"existing actions\n"
                if outputs_preexisting:
                    canonical_path.parent.mkdir(parents=True, exist_ok=True)
                    actions_path.parent.mkdir(parents=True, exist_ok=True)
                    canonical_path.write_bytes(canonical_before)
                    actions_path.write_bytes(actions_before)

                def mutate_outputs(*_args: object, **_kwargs: object) -> ProcessResult:
                    canonical_path.parent.mkdir(parents=True, exist_ok=True)
                    actions_path.parent.mkdir(parents=True, exist_ok=True)
                    canonical_path.write_bytes(b"mutated canonical\n")
                    actions_path.write_bytes(b"mutated actions\n")
                    return ProcessResult(
                        processed=True,
                        canonical_note_path=canonical_path,
                        actions_file_path=actions_path,
                    )

                with (
                    patch.object(processor, "process_file", side_effect=mutate_outputs),
                    patch.object(
                        process_bundles_module,
                        "_write_durable_processed_marker",
                        side_effect=OSError("marker disk full"),
                    ),
                    patch.object(processor, "_apply_actions_retention") as apply_retention,
                ):
                    result = execute_bundle_processing_plan(plan, processor=processor)

                self.assertEqual(result.failed_count, 1)
                self.assertTrue(input_path.exists())
                self.assertTrue(metadata_path.exists())
                apply_retention.assert_not_called()
                if outputs_preexisting:
                    self.assertEqual(canonical_path.read_bytes(), canonical_before)
                    self.assertEqual(actions_path.read_bytes(), actions_before)
                else:
                    self.assertFalse(canonical_path.exists())
                    self.assertFalse(actions_path.exists())

    def test_post_commit_retention_failure_preserves_processed_result_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            canonical_path = vault / "01_Meetings" / "2026-05-04 - Teams - Delivery Review.md"
            actions_path = vault / "07_Actions" / "2026-05-04.md"

            def write_committed_outputs(*_args: object, **_kwargs: object) -> ProcessResult:
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                actions_path.parent.mkdir(parents=True, exist_ok=True)
                canonical_path.write_bytes(b"committed canonical\n")
                actions_path.write_bytes(b"committed action\n")
                return ProcessResult(
                    processed=True,
                    canonical_note_path=canonical_path,
                    actions_file_path=actions_path,
                )

            with (
                patch.object(processor, "process_file", side_effect=write_committed_outputs),
                patch.object(
                    processor,
                    "_apply_actions_retention",
                    side_effect=OSError("retention storage unavailable"),
                ),
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            self.assertEqual(result.failed_count, 0)
            self.assertEqual(canonical_path.read_bytes(), b"committed canonical\n")
            self.assertEqual(actions_path.read_bytes(), b"committed action\n")
            self.assertEqual(
                json.loads(marker_path.read_text(encoding="utf-8"))["source_type"],
                "meeting_bundle_processed",
            )
            self.assertTrue(
                any("deferred action retention failed" in warning.casefold() for warning in result.warnings)
            )
            self.assertTrue(
                any("deferred action retention failed" in reason.casefold() for reason in result.items[0].reasons)
            )
            self.assertNotIn("unsafe bundle processing lock", " ".join(result.items[0].reasons).casefold())

    def test_processor_failure_rolls_back_outputs_and_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, _metadata_path, _marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            original_input = input_path.read_bytes()
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            canonical_path = vault / "01_Meetings" / "2026-05-04 - Teams - Delivery Review.md"
            actions_path = vault / "07_Actions" / "2026-05-04.md"

            def mutate_then_fail(*_args: object, **_kwargs: object) -> ProcessResult:
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                actions_path.parent.mkdir(parents=True, exist_ok=True)
                canonical_path.write_bytes(b"partial canonical\n")
                actions_path.write_bytes(b"partial actions\n")
                input_path.write_bytes(b"mutated staging\n")
                raise RuntimeError("processor failed after writes")

            with patch.object(processor, "process_file", side_effect=mutate_then_fail):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertFalse(canonical_path.exists())
            self.assertFalse(actions_path.exists())
            self.assertEqual(input_path.read_bytes(), original_input)

    def test_processed_marker_rejects_pending_metadata_identity_mismatch_without_echoing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Copilot recap / AI summary",
                extension=".md",
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.update(
                {
                    "fallback_not_before": "2026-05-04T13:30:00+00:00",
                    "retry_until": "2026-05-05T13:30:00+00:00",
                    "identity_key": "sensitive-metadata-identity",
                }
            )
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            pending = json.loads(marker_path.read_text(encoding="utf-8"))
            pending["identity_key"] = "sensitive-pending-identity"
            marker_path.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            reason = result.items[0].reasons[0]
            self.assertIn("identity_key", reason)
            self.assertNotIn("sensitive-metadata-identity", reason)
            self.assertNotIn("sensitive-pending-identity", reason)
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8")), pending)
            self.assertTrue(input_path.exists())

    def test_processed_marker_rejects_pending_metadata_retry_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Copilot recap / AI summary",
                extension=".md",
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.update(
                {
                    "fallback_not_before": "2026-05-04T13:30:00+00:00",
                    "retry_until": "2026-05-05T13:30:00+00:00",
                }
            )
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            pending = json.loads(marker_path.read_text(encoding="utf-8"))
            pending["retry_until"] = "2026-05-05T13:31:00+00:00"
            marker_path.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertIn("retry_until", result.items[0].reasons[0])
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8")), pending)
            self.assertTrue(input_path.exists())

    def test_processed_marker_uses_metadata_identity_times_when_pending_fields_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            pending = json.loads(marker_path.read_text(encoding="utf-8"))
            for key in ("first_seen_at", "selection_window_start_at", "selection_window_end_at"):
                pending.pop(key)
            marker_path.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["first_seen_at"], metadata["first_seen_at"])
            self.assertEqual(marker["selection_window_start_at"], metadata["selection_window_start_at"])
            self.assertEqual(marker["selection_window_end_at"], metadata["selection_window_end_at"])

    def test_processed_marker_normalizes_equal_cross_offset_identity_instants_and_plan_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Copilot recap / AI summary",
                extension=".md",
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.update(
                {
                    "first_seen_at": "2026-05-04T13:30:00+00:00",
                    "fallback_not_before": "2026-05-04T13:30:00+00:00",
                    "retry_until": "2026-05-05T13:30:00+00:00",
                    "selection_window_start_at": "2026-05-04T12:45:00+00:00",
                    "selection_window_end_at": "2026-05-04T14:00:00+00:00",
                }
            )
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            pending = json.loads(marker_path.read_text(encoding="utf-8"))
            pending.update(
                {
                    "first_seen_at": "2026-05-04T14:30:00+01:00",
                    "scheduled_start_at": "2026-05-04T14:00:00+01:00",
                    "scheduled_end_at": "2026-05-04T14:30:00+01:00",
                    "selection_window_start_at": "2026-05-04T13:45:00+01:00",
                    "selection_window_end_at": "2026-05-04T15:00:00+01:00",
                    "retry_until": "2026-05-05T15:30:00+02:00",
                }
            )
            marker_path.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan_time = datetime.fromisoformat("2026-05-05T15:30:00+02:00")
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=plan_time,
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["processed_at"], "2026-05-05T13:30:00+00:00")
            self.assertEqual(marker["retry_until"], "2026-05-05T13:30:00+00:00")
            self.assertEqual(marker["scheduled_end_at"], "2026-05-04T13:30:00+00:00")
            self.assertEqual(marker["first_seen_at"], "2026-05-04T13:30:00+00:00")

    def test_processed_marker_rejects_fallback_retry_equality_or_expired_plan_time(self) -> None:
        cases = (
            (
                "fallback-equals-retry",
                "2026-05-05T13:30:00+00:00",
                "2026-05-05T13:30:00+00:00",
                "2026-05-05T13:30:00+00:00",
            ),
            (
                "processed-after-retry",
                "2026-05-04T13:30:00+00:00",
                "2026-05-05T13:30:00+00:00",
                "2026-05-05T13:30:00.000001+00:00",
            ),
        )
        for label, fallback_not_before, retry_until, plan_time in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp_dir:
                vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                    tmp_dir=tmp_dir,
                    preferred_source="Copilot recap / AI summary",
                    extension=".md",
                )
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["fallback_not_before"] = fallback_not_before
                metadata["retry_until"] = retry_until
                metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                pending = json.loads(marker_path.read_text(encoding="utf-8"))
                pending["retry_until"] = retry_until
                marker_path.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                processor = _processor_for_vault(vault)
                plan = build_bundle_processing_plan(
                    intake_root=bundle_root,
                    processor=processor,
                    now=datetime.fromisoformat(plan_time),
                )

                result = execute_bundle_processing_plan(plan, processor=processor)

                self.assertEqual(result.failed_count, 1)
                self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8")), pending)
                self.assertTrue(input_path.exists())

    def test_processed_marker_rejects_canonical_output_outside_trusted_meetings_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            outside_path = Path(tmp_dir) / "outside.md"
            outside_path.write_text("outside\n", encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            with patch.object(
                processor,
                "process_file",
                return_value=ProcessResult(processed=True, canonical_note_path=outside_path),
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertIn("trusted meetings output", result.items[0].reasons[0])
            self.assertTrue(input_path.exists())
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["source_type"], "meeting_sync_pending")

    def test_processed_marker_rejects_symlinked_canonical_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            processor = _processor_for_vault(vault)
            canonical_path = vault / "01_Meetings" / "2026-05-04 - Teams - Delivery Review.md"
            target = Path(tmp_dir) / "canonical-target.md"
            target.write_text("target\n", encoding="utf-8")
            canonical_path.parent.mkdir(parents=True, exist_ok=True)
            canonical_path.symlink_to(target)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            with patch.object(
                processor,
                "process_file",
                return_value=ProcessResult(processed=True, canonical_note_path=canonical_path),
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertIn("symlink", result.items[0].reasons[0])
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["source_type"], "meeting_sync_pending")

    def test_processed_marker_rejects_symlinked_meetings_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _input_path, _metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Manual / semi-manual intake",
                extension=".md",
            )
            processor = _processor_for_vault(vault)
            meetings_root = processor.meetings_path
            if meetings_root.exists():
                meetings_root.rmdir()
            actual_root = Path(tmp_dir) / "actual-meetings"
            actual_root.mkdir()
            meetings_root.symlink_to(actual_root, target_is_directory=True)
            canonical_path = meetings_root / "2026-05-04 - Teams - Delivery Review.md"
            canonical_path.write_text("target\n", encoding="utf-8")
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            with patch.object(
                processor,
                "process_file",
                return_value=ProcessResult(processed=True, canonical_note_path=canonical_path),
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertIn("root must not be a symlink", result.items[0].reasons[0])
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["source_type"], "meeting_sync_pending")

    def test_processed_marker_rejects_malformed_transcript_diagnostic_contract(self) -> None:
        invalid_diagnostics = (
            {
                "candidate_count": True,
                "assignment_conflicts": [],
                "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                "selected_transcripts": [{"id": "segment-1", "created_at": "2026-05-04T13:40:00+00:00"}],
            },
            {
                "candidate_count": 1,
                "assignment_conflicts": {},
                "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                "selected_transcripts": [{"id": "segment-1", "created_at": "2026-05-04T13:40:00+00:00"}],
            },
            {
                "candidate_count": 1,
                "assignment_conflicts": [],
                "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                "selected_transcripts": [{"id": "segment-1", "created_at": "2026-05-04T14:00:00.000001+00:00"}],
            },
            {
                "candidate_count": 1,
                "assignment_conflicts": [],
                "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                "selected_transcripts": [
                    {"id": "segment-1", "created_at": "2026-05-04T13:40:00+00:00"},
                    {"id": "segment-2", "created_at": "2026-05-04T13:50:00+00:00"},
                ],
            },
            {
                "candidate_count": 1,
                "assignment_conflicts": [],
                "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                "selected_transcripts": [{"id": "segment-1", "created_at": "2026-05-04T13:40:00+00:00"}],
            },
            {
                "candidate_count": 1,
                "assignment_conflicts": [],
                "occurrence_start_at": "9999-12-31T23:59:59-23:59",
                "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                "selected_transcripts": [{"id": "segment-1", "created_at": "2026-05-04T13:40:00+00:00"}],
            },
        )
        for index, diagnostics in enumerate(invalid_diagnostics):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp_dir:
                vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                    tmp_dir=tmp_dir,
                    preferred_source="Teams .vtt transcript",
                    extension=".vtt",
                )
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["artifacts"][0]["transcript_diagnostics"] = diagnostics
                metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                processor = _processor_for_vault(vault)
                plan = build_bundle_processing_plan(
                    intake_root=bundle_root,
                    processor=processor,
                    now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
                )

                result = execute_bundle_processing_plan(plan, processor=processor)

                self.assertEqual(result.failed_count, 1)
                self.assertEqual(
                    json.loads(marker_path.read_text(encoding="utf-8"))["source_type"],
                    "meeting_sync_pending",
                )
                self.assertTrue(input_path.exists())

    def test_restore_staging_attempts_each_cleanup_and_atomically_restores_mutated_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source.vtt"
            archive = root / "archive.vtt"
            sidecar = root / "sidecar.md"
            source.write_bytes(b"mutated")
            archive.write_bytes(b"archived")
            sidecar.write_bytes(b"sidecar")
            snapshot = process_bundles_module._StagingFileSnapshot(
                path=source,
                content=b"original",
                generated_archive_path=archive,
                sidecar_path=sidecar,
                sidecar_content=None,
            )
            original_unlink = Path.unlink

            def fail_archive_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path == archive:
                    raise OSError("archive locked")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", autospec=True, side_effect=fail_archive_unlink):
                errors = process_bundles_module._restore_bundle_staging((snapshot,))

            self.assertEqual(source.read_bytes(), b"original")
            self.assertTrue(archive.exists())
            self.assertFalse(sidecar.exists())
            self.assertEqual(len(errors), 1)
            self.assertIn("archive locked", errors[0])

    def test_marker_failure_restores_preexisting_vtt_sidecar_bytes_and_keeps_retry_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, input_path, metadata_path, marker_path = _ready_marker_bundle(
                tmp_dir=tmp_dir,
                preferred_source="Teams .vtt transcript",
                extension=".vtt",
            )
            processor = _processor_for_vault(vault)
            sidecar_path = processor.intake_state.vtt_sidecar_path(input_path)
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            original_sidecar = b"user-authored unprocessed sidecar\r\n"
            sidecar_path.write_bytes(original_sidecar)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            self.assertEqual(plan.items[0].decision, "ready")

            with patch.object(
                process_bundles_module,
                "_write_durable_processed_marker",
                side_effect=OSError("marker failure"),
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.failed_count, 1)
            self.assertEqual(sidecar_path.read_bytes(), original_sidecar)
            self.assertTrue(input_path.exists())
            self.assertTrue(metadata_path.exists())
            self.assertEqual(
                json.loads(marker_path.read_text(encoding="utf-8"))["source_type"],
                "meeting_sync_pending",
            )
            retry_plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:01:00+00:00"),
            )
            self.assertEqual(retry_plan.items[0].decision, "ready")

    def test_atomic_restore_fsyncs_parent_directory_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "source.vtt"
            events: list[str] = []
            real_replace = os.replace

            def replace(source: str | Path, target: str | Path) -> None:
                events.append("replace")
                real_replace(source, target)

            with (
                patch.object(process_bundles_module.os, "replace", side_effect=replace),
                patch.object(
                    process_bundles_module,
                    "_fsync_parent_directory",
                    side_effect=lambda path: events.append(f"directory_fsync:{path}"),
                    create=True,
                ),
            ):
                process_bundles_module._atomic_restore_bytes(target_path, b"original")

            self.assertEqual(events, ["replace", f"directory_fsync:{target_path.parent}"])

    def test_execute_fallback_bundle_uses_clean_canonical_identity_and_bundle_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            fallback_path = bundle_root / "fallbacks" / "2026-05-04 - Teams - Delivery Review (fallback).md"
            fallback_path.parent.mkdir(parents=True)
            fallback_path.write_text("Summary-derived content.\n", encoding="utf-8")
            metadata_path = bundle_root / "Delivery Review (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=metadata_path,
                preferred_input=fallback_path,
                event_id="evt-fallback-context",
                subject="Delivery / Review: Q2",
                preferred_source="Copilot recap / AI summary",
            )
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload.update(
                {
                    "scheduled_start_at": "2026-05-04T13:00:00+00:00",
                    "scheduled_end_at": "2026-05-04T13:30:00+00:00",
                    "fallback_not_before": "2026-05-04T14:00:00+00:00",
                    "retry_until": "2026-05-05T13:30:00+00:00",
                    "primary_occurrence_event_id": "evt-occurrence",
                    "teams_meeting_id": "teams-123",
                    "organizer": "Ada Lovelace",
                    "attendees": [
                        {
                            "name": "Matthew Rouser",
                            "email": "matthew@example.com",
                            "role": "required",
                            "response_status": "accepted",
                        }
                    ],
                    "join_url": "https://teams.microsoft.com/l/meetup-join/example",
                }
            )
            payload["artifacts"] = [
                {
                    "source_name": "Copilot recap / AI summary",
                    "status": "available",
                    "detail": "recap",
                    "matched_paths": [str(fallback_path)],
                },
                {
                    "source_name": "Teams meeting chat",
                    "status": "available",
                    "detail": "chat",
                    "matched_paths": [],
                },
                {
                    "source_name": "Outlook calendar metadata",
                    "status": "available",
                    "detail": "calendar",
                    "matched_paths": [],
                },
            ]
            metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            metadata = plan.items[0].metadata
            self.assertEqual(metadata.start_at, datetime.fromisoformat("2026-05-04T13:00:00+00:00"))
            self.assertEqual(metadata.end_at, datetime.fromisoformat("2026-05-04T13:30:00+00:00"))
            self.assertEqual(metadata.fallback_not_before, datetime.fromisoformat("2026-05-04T14:00:00+00:00"))
            self.assertEqual(metadata.retry_until, datetime.fromisoformat("2026-05-05T13:30:00+00:00"))
            self.assertEqual(metadata.organizer, "Ada Lovelace")
            self.assertEqual(metadata.occurrence_event_id, "evt-occurrence")
            self.assertEqual(
                metadata.available_sources(),
                (
                    "Copilot recap / AI summary",
                    "Teams meeting chat",
                    "Outlook calendar metadata",
                ),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            canonical_path = vault / "01_Meetings" / "2026-05-04 - Teams - Delivery - Review- Q2.md"
            self.assertEqual(result.items[0].canonical_note_path, canonical_path)
            self.assertTrue(canonical_path.exists())
            self.assertFalse((vault / "01_Meetings" / "2026-05-04 - Teams - Delivery Review (fallback).md").exists())
            note = canonical_path.read_text(encoding="utf-8")
            self.assertIn("# 2026-05-04 - Teams - Delivery - Review- Q2", note)
            self.assertIn('artifact_state: "fallback"', note)
            self.assertIn('outlook_event_id: "evt-occurrence"', note)
            self.assertIn('teams_meeting_id: "teams-123"', note)
            self.assertIn('organizer: "Ada Lovelace"', note)
            self.assertIn('start_at: "2026-05-04T13:00:00+00:00"', note)
            self.assertIn('end_at: "2026-05-04T13:30:00+00:00"', note)
            self.assertIn('join_url: "https://teams.microsoft.com/l/meetup-join/example"', note)
            self.assertIn(
                'attendees: ["Matthew Rouser <matthew@example.com> (required, accepted)"]',
                note,
            )
            self.assertIn(
                'sources_used: ["Copilot recap / AI summary", "Teams meeting chat", "Outlook calendar metadata"]',
                note,
            )
            self.assertIn(
                'source_limitations: ["Processor input is summary-derived and not a verbatim transcript."]',
                note,
            )

    def test_execute_bundle_qualifies_fallback_action_alias_to_meetings_directory(self) -> None:
        cases = (
            ("01_Meetings", 1),
            ("02_Other", 2),
        )
        for source_directory, expected_action_count in cases:
            with self.subTest(source_directory=source_directory), tempfile.TemporaryDirectory() as tmp_dir:
                vault = Path(tmp_dir) / "vault"
                bundle_root = vault / "00_Intake" / "bundles"
                actions_dir = vault / "07_Actions"
                fallback_name = "2026-05-04 - Teams - Delivery Review (fallback).md"
                fallback_path = bundle_root / "fallbacks" / fallback_name
                fallback_path.parent.mkdir(parents=True)
                fallback_path.write_text(
                    "Action: Matthew will send the delivery update.\n",
                    encoding="utf-8",
                )
                actions_dir.mkdir(parents=True)
                actions_path = actions_dir / "2026-05-04.md"
                existing = (
                    "# Actions — Week of 2026-05-04\n\n"
                    "## This Week\n\n"
                    "- [ ] send the delivery update. (Owner: Matthew) — Source: 2026-05-04 "
                    f"[[{source_directory}/{fallback_name}]]\n"
                )
                actions_path.write_text(existing, encoding="utf-8")
                metadata_path = bundle_root / "Delivery Review (outlook).json"
                _write_ready_bundle_metadata(
                    metadata_path=metadata_path,
                    preferred_input=fallback_path,
                    event_id=f"evt-qualified-alias-{source_directory}",
                    subject="Delivery Review",
                    preferred_source="Copilot recap / AI summary",
                )
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                payload["fallback_not_before"] = payload["scheduled_end_at"]
                metadata_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                processor = _processor_for_vault(vault)
                plan = build_bundle_processing_plan(
                    intake_root=bundle_root,
                    processor=processor,
                    now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
                )

                result = execute_bundle_processing_plan(plan, processor=processor)

                self.assertEqual(result.processed_count, 1)
                actions_text = actions_path.read_text(encoding="utf-8")
                self.assertEqual(
                    actions_text.count("send the delivery update"),
                    expected_action_count,
                )
                if source_directory == "01_Meetings":
                    self.assertEqual(actions_text, existing)
                else:
                    self.assertIn(
                        "[[2026-05-04 - Teams - Delivery Review.md]]",
                        actions_text,
                    )

    def test_execute_bundle_sanitizes_multiline_and_unicode_control_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nDelivery discussion.\n",
                encoding="utf-8",
            )
            metadata_path = bundle_root / "unsafe-subject (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=metadata_path,
                preferred_input=transcript_path,
                event_id="evt-unsafe-subject",
                subject="Platform/\x00\n\tReview:\u200b Q2\\Plan",
                preferred_source="Teams .vtt transcript",
            )
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            expected_path = vault / "01_Meetings" / "2026-05-04 - Teams - Platform- Review- Q2-Plan.md"
            self.assertEqual(result.items[0].canonical_note_path, expected_path)
            note = expected_path.read_text(encoding="utf-8")
            self.assertIn("# 2026-05-04 - Teams - Platform- Review- Q2-Plan", note)
            self.assertNotIn("\x00", expected_path.name)
            self.assertNotIn("\n", expected_path.name)
            self.assertNotIn("\t", expected_path.name)
            self.assertNotIn("\u200b", expected_path.name)

    def test_execute_transcript_bundle_artifact_state_has_no_recap_caveat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nDelivery discussion.\n",
                encoding="utf-8",
            )
            metadata_path = bundle_root / "Delivery Review (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=metadata_path,
                preferred_input=transcript_path,
                event_id="evt-transcript-context",
                subject="Delivery Review",
                preferred_source="Teams .vtt transcript",
            )
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload.update(
                {
                    "scheduled_start_at": "2026-05-04T13:00:00+00:00",
                    "scheduled_end_at": "2026-05-04T13:30:00+00:00",
                }
            )
            payload["artifacts"].append(
                {
                    "source_name": "Outlook calendar metadata",
                    "status": "available",
                    "detail": "calendar",
                    "matched_paths": [],
                }
            )
            payload["artifacts"][0]["transcript_diagnostics"] = {
                "candidate_count": 1,
                "assignment_conflicts": [],
                "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                "selected_transcripts": [{"transcript_id": "transcript-1", "created_at": "2026-05-04T13:45:00+00:00"}],
            }
            metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            self.assertEqual(
                plan.items[0].metadata.artifacts[0].transcript_diagnostics,
                {
                    "candidate_count": 1,
                    "assignment_conflicts": [],
                    "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                    "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                    "selected_transcripts": [
                        {
                            "transcript_id": "transcript-1",
                            "created_at": "2026-05-04T13:45:00+00:00",
                        }
                    ],
                },
            )

            result = execute_bundle_processing_plan(plan, processor=processor)

            note = result.items[0].canonical_note_path.read_text(encoding="utf-8")
            self.assertIn('artifact_state: "transcript"', note)
            self.assertNotIn("Processor input is summary-derived and not a verbatim transcript.", note)

    def test_bundle_handoff_passes_only_individually_supported_optional_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            metadata_path = bundle_root / "Delivery Review (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=metadata_path,
                preferred_input=transcript_path,
                event_id="evt-partial-wrapper",
                subject="Delivery Review",
                preferred_source="Teams .vtt transcript",
            )
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )
            omitted_aliases = object()
            calls: list[tuple[Path, bool | None, object, object]] = []

            def partial_process_file(
                path: Path,
                *,
                dry_run: bool | None = None,
                meeting_metadata: object = None,
                source_note_aliases: object = omitted_aliases,
            ) -> ProcessResult:
                calls.append((path, dry_run, meeting_metadata, source_note_aliases))
                return ProcessResult(processed=True)

            result = process_bundles_module._process_file_with_deferred_retention(
                SimpleNamespace(process_file=partial_process_file),
                transcript_path,
                metadata=plan.items[0].metadata,
            )

            self.assertTrue(result.processed)
            self.assertEqual(calls[0][0], transcript_path)
            self.assertFalse(calls[0][1])
            self.assertIsNotNone(calls[0][2])
            self.assertIs(calls[0][3], omitted_aliases)

    def test_bundle_handoff_uninspectable_callable_uses_legacy_base_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            bundle_root = vault / "00_Intake" / "bundles"
            transcript_path = bundle_root / "raw_transcripts" / "staged.vtt"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("WEBVTT\n", encoding="utf-8")
            metadata_path = bundle_root / "Delivery Review (outlook).json"
            _write_ready_bundle_metadata(
                metadata_path=metadata_path,
                preferred_input=transcript_path,
                event_id="evt-uninspectable",
                subject="Delivery Review",
                preferred_source="Teams .vtt transcript",
            )
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=_processor_for_vault(vault),
                now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
            )

            class UninspectableProcessFile:
                __signature__ = "invalid"

                def __init__(self) -> None:
                    self.calls: list[tuple[Path, bool | None]] = []

                def __call__(self, path: Path, *, dry_run: bool | None = None) -> ProcessResult:
                    self.calls.append((path, dry_run))
                    return ProcessResult(processed=True)

            process_file = UninspectableProcessFile()
            result = process_bundles_module._process_file_with_deferred_retention(
                SimpleNamespace(process_file=process_file),
                transcript_path,
                metadata=plan.items[0].metadata,
            )

            self.assertTrue(result.processed)
            self.assertEqual(process_file.calls, [(transcript_path, False)])

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

    def test_attach_transcript_copies_file_and_updates_metadata_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            source = Path(tmp_dir) / "download.vtt"
            source.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n", encoding="utf-8")
            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-attach", subject="Delivery Review"),
            )

            result = attach_transcript_to_bundle(
                bundle_root=intake_root / "bundles",
                event_id="evt-attach",
                file_path=source,
            )

            self.assertTrue(result.attached_path.exists())
            self.assertEqual(result.attached_path.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["processor_handoff"]["preferred_input_path"], str(result.attached_path))
            self.assertEqual(payload["processor_handoff"]["preferred_input_source_name"], "Manual / semi-manual intake")
            self.assertEqual(payload["source_type"], "manual_semi_manual_intake")
            self.assertIn(
                "Manual / semi-manual intake",
                [artifact["source_name"] for artifact in payload["artifacts"]],
            )

    def test_attach_transcript_rejects_unsupported_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            source = Path(tmp_dir) / "download.txt"
            source.write_text("notes\n", encoding="utf-8")
            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-attach", subject="Delivery Review"),
            )

            with self.assertRaisesRegex(ValueError, "Unsupported transcript file type"):
                attach_transcript_to_bundle(
                    bundle_root=intake_root / "bundles",
                    event_id="evt-attach",
                    file_path=source,
                )

    def test_attach_transcript_rejects_missing_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            bundle_root = Path(tmp_dir) / "00_Intake" / "bundles"
            source = Path(tmp_dir) / "download.vtt"
            source.write_text("WEBVTT\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "No meeting bundle metadata found"):
                attach_transcript_to_bundle(
                    bundle_root=bundle_root,
                    event_id="missing",
                    file_path=source,
                )

    def test_attach_transcript_rejects_duplicate_bundle_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            bundle_root = Path(tmp_dir) / "00_Intake" / "bundles"
            bundle_root.mkdir(parents=True)
            source = Path(tmp_dir) / "download.vtt"
            source.write_text("WEBVTT\n", encoding="utf-8")
            _write_calendar_only_metadata(bundle_root / "a (outlook).json", event_id="evt-duplicate")
            _write_calendar_only_metadata(bundle_root / "b (outlook).json", event_id="evt-duplicate")

            with self.assertRaisesRegex(ValueError, "Multiple meeting bundle metadata files matched"):
                attach_transcript_to_bundle(
                    bundle_root=bundle_root,
                    event_id="evt-duplicate",
                    file_path=source,
                )

    def test_attach_transcript_rejects_existing_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_root = Path(tmp_dir) / "00_Intake"
            bundle_root = intake_root / "bundles"
            source = Path(tmp_dir) / "download.vtt"
            source.write_text("WEBVTT\n", encoding="utf-8")
            staged = bundle_root / "raw_transcripts" / source.name
            staged.parent.mkdir(parents=True)
            staged.write_text("existing\n", encoding="utf-8")
            _write_bundle_metadata_sidecar(
                intake_root=intake_root,
                meeting=_meeting(event_id="evt-existing", subject="Delivery Review"),
            )

            with self.assertRaisesRegex(ValueError, "Refusing to overwrite existing staged transcript"):
                attach_transcript_to_bundle(
                    bundle_root=bundle_root,
                    event_id="evt-existing",
                    file_path=source,
                )

    def test_upgrade_unedited_fallback_archives_and_replaces_canonical_transactionally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                vault,
                bundle_root,
                transcript_path,
                metadata_path,
                marker_path,
                canonical_path,
                actions_path,
            ) = _ready_upgrade_bundle(tmp_dir)
            fallback_bytes = canonical_path.read_bytes()
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            self.assertEqual(plan.items[0].decision, "ready")
            self.assertTrue(plan.items[0].upgrade_from_fallback)
            self.assertIn("operation: transcript_upgrade", render_bundle_processing_plan(plan))
            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            self.assertEqual(result.items[0].canonical_note_path, canonical_path)
            note = canonical_path.read_text(encoding="utf-8")
            self.assertIn('artifact_state: "transcript"', note)
            self.assertIn("supersedes_note:", note)
            archive_path = vault / "_Archive" / "Intake" / "Meeting Upgrades" / canonical_path.name
            self.assertEqual(archive_path.read_bytes(), fallback_bytes)
            self.assertFalse((vault / "01_Meetings" / "Delivery Review (fallback).md").exists())
            actions = actions_path.read_text(encoding="utf-8")
            self.assertEqual(actions.count("send update"), 1)
            self.assertIn("[[2026-05-04 - Teams - Delivery Review.md]]", actions)
            self.assertNotIn("[[2026-05-04 - Teams - Delivery Review (fallback).md]]", actions)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["processing_source_kind"], "transcript")
            self.assertEqual(marker["upgrade_state"], "terminal")
            self.assertEqual(marker["supersedes_note"], str(canonical_path))
            self.assertEqual(marker["archived_fallback_note_path"], str(archive_path))
            self.assertEqual(marker["previous_fallback_note_sha256"], hashlib.sha256(fallback_bytes).hexdigest())
            self.assertEqual(marker["previous_fallback_source_type"], "meeting_bundle_processed")
            self.assertEqual(marker["previous_fallback_upgrade_state"], "awaiting_transcript")
            self.assertEqual(marker["selected_transcripts"][0]["id"], "transcript-upgrade")
            self.assertEqual(
                marker["validated_transcript_provenance"],
                {
                    "content_sha256": hashlib.sha256(
                        b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nDelivery discussion.\n"
                    ).hexdigest(),
                    "event_id": "evt-upgrade",
                    "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                    "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                    "path": str(transcript_path),
                    "transcripts": [
                        {
                            "created_at": "2026-05-04T13:45:00+00:00",
                            "id": "transcript-upgrade",
                        }
                    ],
                },
            )
            self.assertEqual(marker["canonical_note_sha256"], hashlib.sha256(canonical_path.read_bytes()).hexdigest())
            self.assertFalse(transcript_path.exists())
            self.assertFalse(provenance_path(transcript_path).exists())
            self.assertFalse(metadata_path.exists())
            rendered = render_bundle_execution_result(result)
            self.assertEqual(result.manual_review_required_count, 0)
            self.assertIn("meeting_bundle_process_manual_review_required: 0", rendered)
            self.assertIn(f"meeting_bundle_process_upgrade_archived_note: {archive_path}", rendered)
            duplicate_archive_result = replace(result, items=(result.items[0], result.items[0]))
            self.assertEqual(duplicate_archive_result.upgrade_archived_note_paths, (archive_path,))
            self.assertEqual(
                render_bundle_execution_result(duplicate_archive_result).count(
                    f"meeting_bundle_process_upgrade_archived_note: {archive_path}"
                ),
                1,
            )

    def test_upgrade_rebases_legacy_vault_alias_paths_to_current_physical_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                vault,
                bundle_root,
                _transcript_path,
                metadata_path,
                marker_path,
                canonical_path,
                _actions_path,
            ) = _ready_upgrade_bundle(tmp_dir)
            legacy_vault_alias = Path(tmp_dir) / "legacy-vault-alias"
            legacy_vault_alias.symlink_to(vault, target_is_directory=True)

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["processed_marker_path"] = str(legacy_vault_alias / marker_path.relative_to(vault))
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["canonical_note_path"] = str(legacy_vault_alias / canonical_path.relative_to(vault))
            marker_path.write_text(
                json.dumps(marker, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            self.assertEqual(plan.warnings, ())
            self.assertEqual(plan.ready_count, 1)
            self.assertEqual(plan.items[0].metadata.processed_marker_path, marker_path)
            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            self.assertEqual(result.failed_count, 0)
            self.assertIn('artifact_state: "transcript"', canonical_path.read_text(encoding="utf-8"))
            upgraded_marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(upgraded_marker["processing_source_kind"], "transcript")
            self.assertEqual(upgraded_marker["upgrade_state"], "terminal")
            self.assertEqual(upgraded_marker["canonical_note_path"], str(canonical_path))

    def test_upgrade_rejects_leaf_symlink_alias_for_stored_canonical_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                vault,
                bundle_root,
                _transcript_path,
                _metadata_path,
                marker_path,
                canonical_path,
                _actions_path,
            ) = _ready_upgrade_bundle(tmp_dir)
            canonical_alias = Path(tmp_dir) / "canonical-alias.md"
            canonical_alias.symlink_to(canonical_path)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["canonical_note_path"] = str(canonical_alias)
            marker_path.write_text(
                json.dumps(marker, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )
            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 0)
            self.assertEqual(result.manual_review_required_count, 1)
            self.assertEqual(canonical_path.read_bytes(), b"fallback canonical\n")
            updated_marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_marker["upgrade_state"], "manual_review_required")
            self.assertEqual(updated_marker["manual_review_reason"], "canonical_note_path_missing_or_unexpected")

    def test_upgrade_rejects_parent_traversal_for_stored_canonical_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                vault,
                bundle_root,
                _transcript_path,
                _metadata_path,
                marker_path,
                canonical_path,
                _actions_path,
            ) = _ready_upgrade_bundle(tmp_dir)
            traversal_path = canonical_path.parent / ".." / canonical_path.parent.name / canonical_path.name
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["canonical_note_path"] = str(traversal_path)
            marker_path.write_text(
                json.dumps(marker, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )
            result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.processed_count, 0)
            self.assertEqual(result.manual_review_required_count, 1)
            self.assertEqual(canonical_path.read_bytes(), b"fallback canonical\n")
            updated_marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_marker["upgrade_state"], "manual_review_required")
            self.assertEqual(updated_marker["manual_review_reason"], "canonical_note_path_missing_or_unexpected")

    def test_graph_transcript_sync_rerun_retains_upgrade_evidence_through_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                vault,
                bundle_root,
                transcript_path,
                metadata_path,
                original_marker_path,
                canonical_path,
                _actions_path,
            ) = _ready_upgrade_bundle(tmp_dir)
            transcript_path.unlink()
            provenance_path(transcript_path).unlink()
            metadata_path.unlink()
            meeting = _meeting(event_id="evt-upgrade", subject="Delivery Review")
            intake_root = vault / "00_Intake"
            marker_path = meeting_sync_module._meeting_identity_path(
                meeting,
                intake_root=intake_root,
            )
            assert marker_path is not None
            marker = json.loads(original_marker_path.read_text(encoding="utf-8"))
            marker["identity_key"] = meeting_sync_module._meeting_identity_key(meeting)
            marker["teams_meeting_id"] = meeting.teams_meeting_id()
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if original_marker_path != marker_path:
                original_marker_path.unlink()

            def fetch_json(url: str, token: str) -> dict[str, object]:
                self.assertEqual(token, "token")
                if "/onlineMeetings?" in url:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                if url.endswith("/transcripts"):
                    return {
                        "value": [
                            {
                                "id": "late-transcript",
                                "createdDateTime": "2026-05-04T13:20:00Z",
                            }
                        ]
                    }
                self.fail(f"Unexpected Graph URL: {url}")

            graph_client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=lambda _url, _token: b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nLate transcript.\n",
            )
            first_sync = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=graph_client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )
            write_planned_bundle_notes(first_sync)

            rerun_sync = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=graph_client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T12:01:00+00:00"),
            )
            write_planned_bundle_notes(rerun_sync)

            processor = _processor_for_vault(vault)
            process_plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:01:00+00:00"),
            )
            self.assertEqual(process_plan.items[0].decision, "ready")
            result = execute_bundle_processing_plan(process_plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            self.assertIn('artifact_state: "transcript"', canonical_path.read_text(encoding="utf-8"))

    def test_graph_transcript_text_sync_rerun_retains_upgrade_evidence_through_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                vault,
                bundle_root,
                transcript_path,
                metadata_path,
                original_marker_path,
                canonical_path,
                _actions_path,
            ) = _ready_upgrade_bundle(tmp_dir)
            transcript_path.unlink()
            provenance_path(transcript_path).unlink()
            metadata_path.unlink()
            meeting = _meeting(event_id="evt-upgrade", subject="Delivery Review")
            intake_root = vault / "00_Intake"
            marker_path = meeting_sync_module._meeting_identity_path(
                meeting,
                intake_root=intake_root,
            )
            assert marker_path is not None
            marker = json.loads(original_marker_path.read_text(encoding="utf-8"))
            marker["identity_key"] = meeting_sync_module._meeting_identity_key(meeting)
            marker["teams_meeting_id"] = meeting.teams_meeting_id()
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if original_marker_path != marker_path:
                original_marker_path.unlink()

            def fetch_json(url: str, token: str) -> dict[str, object]:
                self.assertEqual(token, "token")
                if "/onlineMeetings?" in url:
                    return {"value": [{"id": "opaque-meeting-id"}]}
                if url.endswith("/transcripts"):
                    return {
                        "value": [
                            {
                                "id": "late-transcript-text",
                                "createdDateTime": "2026-05-04T13:20:00Z",
                            }
                        ]
                    }
                self.fail(f"Unexpected Graph URL: {url}")

            def fetch_bytes(url: str, token: str) -> bytes:
                self.assertEqual(token, "token")
                if url.endswith("/metadataContent"):
                    return b'{"speakerName":"Priya","spokenText":"Late transcript text."}\n'
                error = HTTPError(url, 404, "Not Found", None, None)
                error.close()
                raise error

            graph_client = GraphTranscriptDownloadClient(
                access_token="token",
                intake_root=intake_root,
                api_base_url="https://graph.example/v1.0",
                fetch_json=fetch_json,
                fetch_bytes=fetch_bytes,
            )
            first_sync = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=graph_client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )
            write_planned_bundle_notes(first_sync)

            rerun_sync = build_transcript_sync_plan(
                client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
                artifact_discovery_client=graph_client,
                since=date(2026, 5, 4),
                intake_root=intake_root,
                now=datetime.fromisoformat("2026-05-05T12:01:00+00:00"),
            )
            write_planned_bundle_notes(rerun_sync)

            processor = _processor_for_vault(vault)
            process_plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:01:00+00:00"),
            )
            self.assertEqual(process_plan.items[0].decision, "ready")
            result = execute_bundle_processing_plan(process_plan, processor=processor)

            self.assertEqual(result.processed_count, 1)
            self.assertIn('artifact_state: "transcript"', canonical_path.read_text(encoding="utf-8"))

    def test_upgrade_edited_fallback_diverts_to_manual_review_without_processor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                vault,
                bundle_root,
                transcript_path,
                _metadata_path,
                marker_path,
                canonical_path,
                actions_path,
            ) = _ready_upgrade_bundle(tmp_dir)
            canonical_path.write_bytes(b"user edited fallback\n")
            canonical_before = canonical_path.read_bytes()
            actions_before = actions_path.read_bytes()
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            with patch.object(processor, "process_file", side_effect=AssertionError("processor must not run")):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.items[0].status, "skipped")
            self.assertEqual(result.items[0].canonical_note_path, canonical_path)
            self.assertIn("manual review", " ".join(result.items[0].reasons).casefold())
            self.assertEqual(canonical_path.read_bytes(), canonical_before)
            self.assertEqual(actions_path.read_bytes(), actions_before)
            self.assertTrue(transcript_path.exists())
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["processing_source_kind"], "fallback")
            self.assertEqual(marker["upgrade_state"], "manual_review_required")
            self.assertEqual(marker["previous_fallback_upgrade_state"], "awaiting_transcript")
            self.assertEqual(marker["previous_fallback_source_type"], "meeting_bundle_processed")
            self.assertEqual(marker["canonical_note_sha256"], hashlib.sha256(b"fallback canonical\n").hexdigest())
            self.assertEqual(marker["manual_review_reason"], "canonical_note_hash_mismatch")
            self.assertNotIn("user edited fallback", marker_path.read_text(encoding="utf-8"))
            rendered = render_bundle_execution_result(result)
            self.assertEqual(result.manual_review_required_count, 1)
            self.assertIn("meeting_bundle_process_manual_review_required: 1", rendered)
            self.assertNotIn("meeting_bundle_process_upgrade_archived_note:", rendered)

    def test_upgrade_legacy_fallback_without_hash_requires_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            (
                vault,
                bundle_root,
                transcript_path,
                _metadata_path,
                marker_path,
                canonical_path,
                actions_path,
            ) = _ready_upgrade_bundle(tmp_dir, legacy_marker=True)
            canonical_before = canonical_path.read_bytes()
            actions_before = actions_path.read_bytes()
            legacy_payload = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertNotIn("processing_source_kind", legacy_payload)
            self.assertNotIn("upgrade_state", legacy_payload)
            self.assertNotIn("canonical_note_sha256", legacy_payload)
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )

            with patch.object(processor, "process_file", side_effect=AssertionError("processor must not run")):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.items[0].status, "skipped")
            self.assertEqual(result.items[0].canonical_note_path, canonical_path)
            self.assertEqual(canonical_path.read_bytes(), canonical_before)
            self.assertEqual(actions_path.read_bytes(), actions_before)
            self.assertTrue(transcript_path.exists())
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["schema_version"], 2)
            self.assertEqual(marker["upgrade_state"], "manual_review_required")
            self.assertIsNone(marker["previous_fallback_upgrade_state"])
            self.assertEqual(marker["previous_fallback_source_type"], "meeting_sync_identity")
            self.assertEqual(marker["manual_review_reason"], "canonical_note_hash_missing_or_invalid")

    def test_upgrade_planning_requires_matching_serialized_transcript_provenance(self) -> None:
        for failure in ("missing_sidecar", "changed_bytes", "wrong_occurrence", "tampered_selected_id"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp_dir:
                vault, bundle_root, transcript, metadata, _marker, _canonical, _actions = _ready_upgrade_bundle(tmp_dir)
                if failure == "missing_sidecar":
                    provenance_path(transcript).unlink()
                elif failure == "changed_bytes":
                    transcript.write_bytes(transcript.read_bytes() + b"tampered\n")
                elif failure == "wrong_occurrence":
                    write_provenance(
                        transcript,
                        event_id="different-occurrence",
                        occurrence_start_at=datetime.fromisoformat("2026-05-04T13:00:00+00:00"),
                        occurrence_end_at=datetime.fromisoformat("2026-05-04T13:30:00+00:00"),
                        transcripts=[
                            {
                                "id": "transcript-upgrade",
                                "created_at": "2026-05-04T13:45:00+00:00",
                            }
                        ],
                        content=transcript.read_bytes(),
                    )
                else:
                    payload = json.loads(metadata.read_text(encoding="utf-8"))
                    diagnostics = payload["artifacts"][0]["transcript_diagnostics"]
                    diagnostics["selected_transcripts"][0]["id"] = "tampered-id"
                    metadata.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

                plan = build_bundle_processing_plan(
                    intake_root=bundle_root,
                    processor=_processor_for_vault(vault),
                    now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
                )

                self.assertEqual(plan.items[0].decision, "blocked")
                self.assertIn("provenance", " ".join(plan.items[0].reasons).casefold())

    def test_upgrade_revalidates_transcript_provenance_under_locks_before_processor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, transcript, _metadata, marker, canonical, actions = _ready_upgrade_bundle(tmp_dir)
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )
            actions_before = actions.read_bytes()
            original_archive = process_bundles_module.archive_canonical_note

            def archive_then_tamper(*args: object, **kwargs: object) -> Path:
                archived = original_archive(*args, **kwargs)
                transcript.write_bytes(transcript.read_bytes() + b"changed under lock\n")
                return archived

            with (
                patch.object(
                    process_bundles_module,
                    "archive_canonical_note",
                    side_effect=archive_then_tamper,
                ),
                patch.object(processor, "process_file", side_effect=AssertionError("processor must not run")),
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.items[0].status, "skipped")
            self.assertEqual(actions.read_bytes(), actions_before)
            self.assertEqual(canonical.read_bytes(), b"fallback canonical\n")
            self.assertTrue(transcript.exists())
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["upgrade_state"], "manual_review_required")
            self.assertEqual(payload["manual_review_reason"], "transcript_provenance_changed_or_invalid")
            archive_root = vault / "_Archive" / "Intake" / "Meeting Upgrades"
            archive_path = next(archive_root.iterdir())
            self.assertIn(
                f"meeting_bundle_process_upgrade_archived_note: {archive_path}",
                render_bundle_execution_result(result),
            )

    def test_upgrade_revalidates_fallback_hash_after_archive_before_processor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, transcript, metadata, marker, canonical, actions = _ready_upgrade_bundle(tmp_dir)
            processor = _processor_for_vault(vault)
            plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )
            actions_before = actions.read_bytes()
            transcript_before = transcript.read_bytes()
            metadata_before = metadata.read_bytes()
            original_archive = process_bundles_module.archive_canonical_note

            def archive_then_edit(*args: object, **kwargs: object) -> Path:
                archived = original_archive(*args, **kwargs)
                canonical.write_bytes(b"boundary edit\n")
                return archived

            with (
                patch.object(
                    process_bundles_module,
                    "archive_canonical_note",
                    side_effect=archive_then_edit,
                ),
                patch.object(processor, "process_file", side_effect=AssertionError("processor must not run")),
            ):
                result = execute_bundle_processing_plan(plan, processor=processor)

            self.assertEqual(result.items[0].status, "skipped")
            self.assertEqual(canonical.read_bytes(), b"boundary edit\n")
            self.assertEqual(actions.read_bytes(), actions_before)
            self.assertEqual(transcript.read_bytes(), transcript_before)
            self.assertEqual(metadata.read_bytes(), metadata_before)
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["upgrade_state"], "manual_review_required")
            self.assertEqual(payload["manual_review_reason"], "canonical_note_hash_changed_before_processor")

    def test_upgrade_terminal_second_plan_blocks_without_duplicate_archive_or_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault, bundle_root, _transcript, metadata, marker, canonical, actions = _ready_upgrade_bundle(tmp_dir)
            processor = _processor_for_vault(vault)
            first_plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
            )
            first_result = execute_bundle_processing_plan(first_plan, processor=processor)
            self.assertEqual(first_result.processed_count, 1)
            first_canonical = canonical.read_bytes()
            first_actions = actions.read_bytes()
            archive_root = vault / "_Archive" / "Intake" / "Meeting Upgrades"
            archive_paths = tuple(archive_root.iterdir())

            _rewrite_upgrade_metadata_after_cleanup(metadata, bundle_root)
            second_plan = build_bundle_processing_plan(
                intake_root=bundle_root,
                processor=processor,
                now=datetime.fromisoformat("2026-05-05T12:01:00+00:00"),
            )
            second_result = execute_bundle_processing_plan(second_plan, processor=processor)

            self.assertEqual(second_plan.items[0].decision, "blocked")
            self.assertEqual(second_result.skipped_count, 1)
            self.assertEqual(canonical.read_bytes(), first_canonical)
            self.assertEqual(actions.read_bytes(), first_actions)
            self.assertEqual(tuple(archive_root.iterdir()), archive_paths)
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["upgrade_state"], "terminal")

    def test_upgrade_processor_migration_or_marker_failure_restores_outputs_and_staging(self) -> None:
        for failure_point in ("processor", "migration", "marker"):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as tmp_dir:
                (
                    vault,
                    bundle_root,
                    transcript,
                    metadata,
                    marker,
                    canonical,
                    actions,
                ) = _ready_upgrade_bundle(tmp_dir)
                before = {
                    path: path.read_bytes()
                    for path in (
                        transcript,
                        provenance_path(transcript),
                        metadata,
                        marker,
                        canonical,
                        actions,
                    )
                }
                processor = _processor_for_vault(vault)
                plan = build_bundle_processing_plan(
                    intake_root=bundle_root,
                    processor=processor,
                    now=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
                )
                if failure_point == "processor":
                    patcher = patch.object(processor, "process_file", side_effect=OSError("processor failure"))
                elif failure_point == "migration":
                    patcher = patch.object(
                        process_bundles_module,
                        "migrate_action_backlinks",
                        side_effect=OSError("migration failure"),
                    )
                else:
                    patcher = patch.object(
                        process_bundles_module,
                        "_write_durable_processed_marker",
                        side_effect=OSError("marker failure"),
                    )

                with patcher:
                    result = execute_bundle_processing_plan(plan, processor=processor)

                self.assertEqual(result.failed_count, 1)
                for path, content in before.items():
                    self.assertTrue(path.exists(), path)
                    self.assertEqual(path.read_bytes(), content, path)
                self.assertEqual(
                    json.loads(marker.read_text(encoding="utf-8"))["upgrade_state"],
                    "awaiting_transcript",
                )
                archive_root = vault / "_Archive" / "Intake" / "Meeting Upgrades"
                archive_paths = tuple(archive_root.iterdir())
                self.assertEqual(len(archive_paths), 1)
                self.assertIn(
                    f"meeting_bundle_process_upgrade_archived_note: {archive_paths[0]}",
                    render_bundle_execution_result(result),
                )


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
    preferred_source: str = "Local transcript",
) -> None:
    try:
        scheduled_date = date.fromisoformat(preferred_input.name[:10]).isoformat()
    except ValueError:
        scheduled_date = "2026-05-04"
    metadata_path.write_text(
        json.dumps(
            {
                "source_type": "outlook_meeting_bundle",
                "outlook_event_id": event_id,
                "subject": subject,
                "scheduled_start_at": f"{scheduled_date}T13:00:00+00:00",
                "scheduled_end_at": f"{scheduled_date}T13:30:00+00:00",
                "first_seen_at": f"{scheduled_date}T13:30:00+00:00",
                "selection_window_start_at": f"{scheduled_date}T12:45:00+00:00",
                "selection_window_end_at": f"{scheduled_date}T14:00:00+00:00",
                "processor_handoff": {
                    "preferred_input_path": str(preferred_input),
                    "preferred_input_source_name": preferred_source,
                },
                "artifacts": [
                    {
                        "source_name": preferred_source,
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


def _ready_marker_bundle(
    *,
    tmp_dir: str,
    preferred_source: str,
    extension: str,
) -> tuple[Path, Path, Path, Path, Path]:
    vault = Path(tmp_dir) / "vault"
    bundle_root = vault / "00_Intake" / "bundles"
    managed_dir = "fallbacks" if preferred_source == "Copilot recap / AI summary" else "raw_transcripts"
    input_path = bundle_root / managed_dir / f"2026-05-04 - Teams - Delivery Review{extension}"
    input_path.parent.mkdir(parents=True)
    if extension == ".vtt":
        input_path.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nDelivery discussion.\n",
            encoding="utf-8",
        )
    else:
        input_path.write_text("Delivery discussion.\n", encoding="utf-8")
    metadata_path = bundle_root / "Delivery Review (outlook).json"
    _write_ready_bundle_metadata(
        metadata_path=metadata_path,
        preferred_input=input_path,
        event_id="evt-marker",
        subject="Delivery Review",
        preferred_source=preferred_source,
    )
    marker_path = bundle_root / "_meeting_sync" / "identities" / "evt-marker.json"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text(
        json.dumps(
            {
                "source_type": "meeting_sync_pending",
                "identity_key": "evt-marker|teams-marker",
                "outlook_event_id": "evt-marker",
                "teams_meeting_id": "teams-marker",
                "first_seen_at": "2026-05-04T13:30:00+00:00",
                "retry_until": "2026-05-05T13:30:00+00:00",
                "primary_occurrence_event_id": "evt-marker",
                "scheduled_start_at": "2026-05-04T13:00:00+00:00",
                "scheduled_end_at": "2026-05-04T13:30:00+00:00",
                "selection_window_start_at": "2026-05-04T12:45:00+00:00",
                "selection_window_end_at": "2026-05-04T14:00:00+00:00",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        {
            "processed_marker_path": str(marker_path),
            "identity_key": "evt-marker|teams-marker",
            "teams_meeting_id": "teams-marker",
            "primary_occurrence_event_id": "evt-marker",
        }
    )
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return vault, bundle_root, input_path, metadata_path, marker_path


def _ready_two_marker_execution_plans(
    tmp_dir: str,
) -> tuple[
    Path,
    tuple[process_bundles_module.BundleProcessingPlan, process_bundles_module.BundleProcessingPlan],
    dict[str, Path],
    dict[str, Path],
    Path,
    MeetingProcessor,
]:
    vault = Path(tmp_dir) / "vault"
    bundle_root = vault / "00_Intake" / "bundles"
    raw_transcripts = bundle_root / "raw_transcripts"
    marker_root = bundle_root / "_meeting_sync" / "identities"
    raw_transcripts.mkdir(parents=True)
    marker_root.mkdir(parents=True)
    marker_paths: dict[str, Path] = {}
    canonical_paths: dict[str, Path] = {}
    for label, subject in (("alpha", "Alpha Review"), ("beta", "Beta Review")):
        event_id = f"evt-{label}"
        input_path = raw_transcripts / f"2026-05-04 - Teams - {subject}.md"
        input_path.write_text(f"{subject} discussion.\n", encoding="utf-8")
        metadata_path = bundle_root / f"{subject} (outlook).json"
        _write_ready_bundle_metadata(
            metadata_path=metadata_path,
            preferred_input=input_path,
            event_id=event_id,
            subject=subject,
            preferred_source="Manual / semi-manual intake",
        )
        marker_path = marker_root / f"{event_id}.json"
        identity_key = f"{event_id}|teams-{label}"
        marker_path.write_text(
            json.dumps(
                {
                    "source_type": "meeting_sync_pending",
                    "identity_key": identity_key,
                    "outlook_event_id": event_id,
                    "teams_meeting_id": f"teams-{label}",
                    "primary_occurrence_event_id": event_id,
                    "scheduled_start_at": "2026-05-04T13:00:00+00:00",
                    "scheduled_end_at": "2026-05-04T13:30:00+00:00",
                    "first_seen_at": "2026-05-04T13:30:00+00:00",
                    "selection_window_start_at": "2026-05-04T12:45:00+00:00",
                    "selection_window_end_at": "2026-05-04T14:00:00+00:00",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.update(
            {
                "processed_marker_path": str(marker_path),
                "identity_key": identity_key,
                "teams_meeting_id": f"teams-{label}",
                "primary_occurrence_event_id": event_id,
            }
        )
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker_paths[label] = marker_path
        canonical_paths[label] = vault / "01_Meetings" / f"2026-05-04 - Teams - {subject}.md"

    processor = _processor_for_vault(vault)
    combined_plan = build_bundle_processing_plan(
        intake_root=bundle_root,
        processor=processor,
        now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
    )
    items_by_event = {item.metadata.event_id: item for item in combined_plan.items}
    plans = (
        replace(combined_plan, items=(items_by_event["evt-alpha"],)),
        replace(combined_plan, items=(items_by_event["evt-beta"],)),
    )
    actions_path = vault / "07_Actions" / "2026-05-04.md"
    return vault, plans, marker_paths, canonical_paths, actions_path, processor


def _ready_upgrade_bundle(
    tmp_dir: str,
    *,
    legacy_marker: bool = False,
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    vault = Path(tmp_dir).resolve() / "vault"
    bundle_root = vault / "00_Intake" / "bundles"
    metadata_path = bundle_root / "Delivery Review (outlook).json"
    marker_path = bundle_root / "_meeting_sync" / "identities" / "evt-upgrade.json"
    canonical_path = vault / "01_Meetings" / "2026-05-04 - Teams - Delivery Review.md"
    actions_path = vault / "07_Actions" / "2026-05-04.md"
    canonical_path.parent.mkdir(parents=True)
    actions_path.parent.mkdir(parents=True)
    marker_path.parent.mkdir(parents=True)
    canonical_path.write_bytes(b"fallback canonical\n")
    actions_path.write_text(
        "# Actions — Week of 2026-05-04\n\n"
        "## This Week\n\n"
        "- [x] send update (Owner: Matthew) — Source: 2026-05-04 "
        "[[2026-05-04 - Teams - Delivery Review (fallback).md]]\n",
        encoding="utf-8",
    )
    marker_payload: dict[str, object] = {
        "source_type": "meeting_sync_identity" if legacy_marker else "meeting_bundle_processed",
        "retry_until": "2026-05-05T13:30:00+00:00",
        "processed_at": "2026-05-04T14:00:00+00:00",
        "subject": "Delivery Review",
        "outlook_event_id": "evt-upgrade",
        "primary_occurrence_event_id": "evt-upgrade",
        "teams_meeting_id": "teams-upgrade",
        "identity_key": "evt-upgrade|teams-upgrade",
        "scheduled_start_at": "2026-05-04T13:00:00+00:00",
        "scheduled_end_at": "2026-05-04T13:30:00+00:00",
        "selection_window_start_at": "2026-05-04T12:45:00+00:00",
        "selection_window_end_at": "2026-05-04T14:00:00+00:00",
        "first_seen_at": "2026-05-04T13:30:00+00:00",
        "canonical_note_path": str(canonical_path),
        "preferred_input_path": str(bundle_root / "fallbacks" / "2026-05-04 - Teams - Delivery Review (fallback).md"),
        "preferred_input_source_name": "Copilot recap / AI summary",
    }
    if not legacy_marker:
        marker_payload["schema_version"] = 2
        marker_payload["processing_source_kind"] = "fallback"
        marker_payload["upgrade_state"] = "awaiting_transcript"
        marker_payload["canonical_note_sha256"] = hashlib.sha256(canonical_path.read_bytes()).hexdigest()
    marker_path.write_text(json.dumps(marker_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    transcript_path = _rewrite_upgrade_metadata_after_cleanup(metadata_path, bundle_root)
    return vault, bundle_root, transcript_path, metadata_path, marker_path, canonical_path, actions_path


def _rewrite_upgrade_metadata_after_cleanup(metadata_path: Path, bundle_root: Path) -> Path:
    transcript_path = bundle_root / "raw_transcripts" / "2026-05-04 - Teams - Delivery Review.vtt"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nDelivery discussion.\n",
        encoding="utf-8",
    )
    write_provenance(
        transcript_path,
        event_id="evt-upgrade",
        occurrence_start_at=datetime.fromisoformat("2026-05-04T13:00:00+00:00"),
        occurrence_end_at=datetime.fromisoformat("2026-05-04T13:30:00+00:00"),
        transcripts=[
            {
                "id": "transcript-upgrade",
                "created_at": "2026-05-04T13:45:00+00:00",
            }
        ],
        content=transcript_path.read_bytes(),
    )
    marker_path = bundle_root / "_meeting_sync" / "identities" / "evt-upgrade.json"
    payload = {
        "source_type": "outlook_meeting_bundle",
        "outlook_event_id": "evt-upgrade",
        "primary_occurrence_event_id": "evt-upgrade",
        "teams_meeting_id": "teams-upgrade",
        "identity_key": "evt-upgrade|teams-upgrade",
        "subject": "Delivery Review",
        "scheduled_start_at": "2026-05-04T13:00:00+00:00",
        "scheduled_end_at": "2026-05-04T13:30:00+00:00",
        "first_seen_at": "2026-05-04T13:30:00+00:00",
        "selection_window_start_at": "2026-05-04T12:45:00+00:00",
        "selection_window_end_at": "2026-05-04T14:00:00+00:00",
        "retry_until": "2026-05-05T13:30:00+00:00",
        "processed_marker_path": str(marker_path),
        "processor_handoff": {
            "preferred_input_path": str(transcript_path),
            "preferred_input_source_name": "Teams .vtt transcript",
        },
        "artifacts": [
            {
                "source_name": "Teams .vtt transcript",
                "status": "available",
                "detail": "authoritative transcript",
                "matched_paths": [str(transcript_path)],
                "occurrence_validated_paths": [str(transcript_path)],
                "transcript_diagnostics": {
                    "candidate_count": 1,
                    "assignment_conflicts": [],
                    "occurrence_start_at": "2026-05-04T13:00:00+00:00",
                    "occurrence_end_at": "2026-05-04T13:30:00+00:00",
                    "selected_transcripts": [
                        {
                            "id": "transcript-upgrade",
                            "created_at": "2026-05-04T13:45:00+00:00",
                        }
                    ],
                },
            },
            {
                "source_name": "Outlook calendar metadata",
                "status": "available",
                "detail": "calendar",
                "matched_paths": [],
            },
        ],
    }
    metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return transcript_path


def _write_calendar_only_metadata(metadata_path: Path, *, event_id: str) -> None:
    metadata_path.write_text(
        json.dumps(
            {
                "outlook_event_id": event_id,
                "subject": "Delivery Review",
                "processor_handoff": {
                    "preferred_input_path": None,
                    "preferred_input_source_name": None,
                },
                "artifacts": [
                    {
                        "source_name": "Outlook calendar metadata",
                        "status": "available",
                        "detail": "metadata",
                        "matched_paths": [],
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
