"""Plain-text rendering for meeting sync plans and bundle write results.

Side-effect-free formatting: the CLI transcript-sync plan report, the bundle
write result report, and the artifact/diagnostic serializers and timestamp
formatter that sidecar rendering shares. No I/O. Imported by ``meetings.sync``,
which re-exports the public ``render_*`` functions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .occurrence_assignment import selection_bounds
from .sync_models import (
    ArtifactStatus,
    BundleWriteResult,
    MeetingArtifact,
    SelectedTranscriptDiagnostic,
    TranscriptDiagnostics,
    TranscriptSyncPlan,
    TranscriptSyncPlanItem,
    _occurrence_conflict_key,
)

SYNC_SOURCE_SUMMARY_FIELDS = (
    ("Teams .vtt transcript", "vtt"),
    ("Teams transcript text", "transcript_text"),
    ("Teams meeting chat", "chat"),
    ("Copilot recap / AI summary", "recap"),
)
ARTIFACT_STATUS_SUMMARY_SEQUENCE: tuple[ArtifactStatus, ...] = (
    "available",
    "missing",
    "permission_blocked",
    "not_attempted",
)


def render_transcript_sync_plan(plan: TranscriptSyncPlan, *, mode: str = "dry-run") -> str:
    process_items = plan.process_items()
    lines = [
        f"meeting_sync_mode: {mode}",
        f"meeting_sync_since: {plan.since.isoformat()}",
        f"meeting_sync_generated_at: {plan.generated_at.isoformat()}",
        f"meeting_sync_provider: {plan.provider_label}",
    ]
    if plan.warning:
        lines.append(f"meeting_sync_warning: {plan.warning}")
    lines.extend(
        [
            f"meeting_sync_candidates: {plan.candidate_count}",
            f"meeting_sync_would_process: {plan.process_count}",
            f"meeting_sync_would_skip: {plan.skip_count}",
            f"meeting_sync_occurrence_errors: {plan.occurrence_error_count}",
            f"meeting_sync_fallback_deferred: {plan.fallback_deferred_count}",
            f"meeting_sync_fallback_awaiting_transcript: {plan.fallback_awaiting_transcript_count}",
            f"meeting_sync_late_transcript_upgrades: {plan.late_transcript_upgrade_count}",
            f"meeting_sync_processable_missing_vtt: {_count_process_items_missing_source(process_items, 'Teams .vtt transcript')}",
            (
                "meeting_sync_processable_missing_transcript_text: "
                f"{_count_process_items_missing_source(process_items, 'Teams transcript text')}"
            ),
            f"meeting_sync_processable_missing_chat: {_count_process_items_missing_source(process_items, 'Teams meeting chat')}",
            f"meeting_sync_processable_missing_recap: {_count_process_items_missing_source(process_items, 'Copilot recap / AI summary')}",
            f"meeting_sync_processable_calendar_only: {_count_process_items_with_only_calendar_metadata(process_items)}",
        ]
    )
    for source_name, summary_key in SYNC_SOURCE_SUMMARY_FIELDS:
        for status in ARTIFACT_STATUS_SUMMARY_SEQUENCE:
            lines.append(
                f"meeting_sync_processable_{summary_key}_{status}: "
                f"{_count_process_items_with_source_status(process_items, source_name, status)}"
            )
    rendered_conflicts: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in plan.items:
        meeting = item.meeting
        lines.append(f'meeting_sync_item: {item.decision} event_id="{meeting.event_id}" subject="{meeting.subject}"')
        lines.append(f"  end_at: {meeting.end_at.isoformat()}")
        lines.append(f"  teams_meeting_detected: {'yes' if meeting.is_teams_meeting() else 'no'}")
        if item.bundle.teams_meeting_id:
            lines.append(f'  teams_meeting_id: "{item.bundle.teams_meeting_id}"')
        if item.intake_bundle_note is not None:
            lines.append(f"  would_write_bundle: {item.intake_bundle_note.path}")
            lines.append(f"  would_write_outlook_metadata: {item.intake_bundle_note.metadata_path}")
            lines.append(f"  would_write_meeting_identity: {item.intake_bundle_note.identity_path}")
            if item.intake_bundle_note.processor_input_path is not None:
                lines.append(f"  would_process_intake_file: {item.intake_bundle_note.processor_input_path}")
            if item.intake_bundle_note.processor_input_source_name is not None:
                lines.append(f"  processor_input_source: {item.intake_bundle_note.processor_input_source_name}")
            lines.append(f"  bundle_attendance_confidence: {item.intake_bundle_note.attendance_confidence}")
            for source_name in item.intake_bundle_note.sources_used:
                lines.append(f"  bundle_source_used: {source_name}")
            for limitation in item.intake_bundle_note.source_limitations:
                lines.append(f"  bundle_source_limitation: {limitation}")
        for source_name in item.bundle.available_sources():
            artifact = item.bundle.artifact(source_name)
            if artifact and artifact.matched_paths:
                rendered_paths = ", ".join(str(path) for path in artifact.matched_paths)
                lines.append(f"  source_available: {source_name} ({rendered_paths})")
            else:
                lines.append(f"  source_available: {source_name}")
        for artifact in item.bundle.artifacts:
            if artifact.status != "available":
                detail_suffix = f" ({artifact.detail})" if artifact.detail else ""
                lines.append(f"  source_pending: {artifact.source_name}={artifact.status}{detail_suffix}")
        item_diagnostics = tuple(
            artifact.diagnostics for artifact in item.bundle.artifacts if artifact.diagnostics is not None
        )
        for artifact in item.bundle.artifacts:
            if artifact.recap_diagnostics is not None:
                lines.append("  recap_diagnostics: " + json.dumps(artifact.recap_diagnostics, sort_keys=True))
        selected_diagnostics = next(
            (diagnostics for diagnostics in item_diagnostics if diagnostics.selected_transcripts),
            item_diagnostics[0] if item_diagnostics else None,
        )
        if selected_diagnostics is not None:
            candidate_count = selected_diagnostics.candidate_count
            lines.append(f"  transcript_candidates: {candidate_count if candidate_count is not None else 'unknown'}")
            for selected in _chronological_selected_transcripts(selected_diagnostics.selected_transcripts):
                lines.append(f"  selected_transcript_id: {selected.transcript_id}")
                created_at = selected.created_at.isoformat() if selected.created_at is not None else "unknown"
                lines.append(f"  selected_transcript_created_at: {created_at}")
            lines.append(
                "  selected_for_occurrence: "
                f"{selected_diagnostics.occurrence_start_at.isoformat()} to "
                f"{selected_diagnostics.occurrence_end_at.isoformat()}"
            )
            lines.append(f"  local_transcript_action: {selected_diagnostics.local_action}")
        occurrence_windows = {occurrence.occurrence_id: occurrence for occurrence in meeting.occurrence_context}
        for diagnostics in item_diagnostics:
            for conflict in diagnostics.assignment_conflicts:
                if meeting.event_id not in conflict.occurrence_ids:
                    continue
                conflict_key = _occurrence_conflict_key(conflict)
                if conflict_key in rendered_conflicts:
                    continue
                rendered_conflicts.add(conflict_key)
                lines.append("  meeting_sync_occurrence_error: ambiguous_transcript_assignment")
                lines.append(f"  candidate_transcript_id: {conflict.transcript_id}")
                lines.append(f"  candidate_transcript_created_at: {conflict.created_at.isoformat()}")
                for occurrence_id in conflict.occurrence_ids:
                    lines.append(f"  occurrence_id: {occurrence_id}")
                    occurrence = occurrence_windows.get(occurrence_id)
                    if occurrence is not None:
                        selection_start, selection_end = selection_bounds(occurrence)
                        lines.append(
                            "  scheduled_window: "
                            f"{occurrence.scheduled_start.isoformat()} to "
                            f"{occurrence.scheduled_end.isoformat()}"
                        )
                        lines.append(
                            f"  selection_window: {selection_start.isoformat()} to {selection_end.isoformat()}"
                        )
        for reason in item.reasons:
            lines.append(f"  reason: {reason}")
    return "\n".join(lines)


def render_bundle_write_result(result: BundleWriteResult) -> str:
    lines = [
        f"meeting_sync_bundle_notes_written: {result.written_count}",
        f"meeting_sync_bundle_notes_skipped_existing: {result.skipped_existing_count}",
        f"meeting_sync_outlook_metadata_written: {len(result.written_metadata_paths)}",
        f"meeting_sync_outlook_metadata_skipped_existing: {len(result.skipped_existing_metadata_paths)}",
        f"meeting_sync_identity_markers_written: {len(result.written_identity_paths)}",
        f"meeting_sync_identity_markers_skipped_existing: {len(result.skipped_existing_identity_paths)}",
    ]
    for path in result.written_bundle_note_paths:
        lines.append(f"bundle_note_written: {path}")
    for path in result.skipped_existing_bundle_note_paths:
        lines.append(f"bundle_note_skipped_existing: {path}")
    for path in result.written_metadata_paths:
        lines.append(f"outlook_metadata_written: {path}")
    for path in result.skipped_existing_metadata_paths:
        lines.append(f"outlook_metadata_skipped_existing: {path}")
    for path in result.written_identity_paths:
        lines.append(f"meeting_identity_written: {path}")
    for path in result.skipped_existing_identity_paths:
        lines.append(f"meeting_identity_skipped_existing: {path}")
    return "\n".join(lines)


def _count_process_items_missing_source(
    items: tuple[TranscriptSyncPlanItem, ...],
    source_name: str,
) -> int:
    return sum(1 for item in items if item.bundle.source_status(source_name) != "available")


def _count_process_items_with_source_status(
    items: tuple[TranscriptSyncPlanItem, ...],
    source_name: str,
    status: ArtifactStatus,
) -> int:
    return sum(1 for item in items if item.bundle.source_status(source_name) == status)


def _count_process_items_with_only_calendar_metadata(items: tuple[TranscriptSyncPlanItem, ...]) -> int:
    return sum(1 for item in items if item.bundle.available_sources() == ["Outlook calendar metadata"])


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _chronological_selected_transcripts(
    selected: tuple[SelectedTranscriptDiagnostic, ...],
) -> tuple[SelectedTranscriptDiagnostic, ...]:
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.created_at is None,
                item.created_at or datetime.max.replace(tzinfo=UTC),
                item.transcript_id,
            ),
        )
    )


def _serialize_transcript_diagnostics(diagnostics: TranscriptDiagnostics) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_count": diagnostics.candidate_count,
        "selected_transcripts": [
            {
                "id": selected.transcript_id,
                "created_at": selected.created_at.isoformat() if selected.created_at is not None else None,
            }
            for selected in _chronological_selected_transcripts(diagnostics.selected_transcripts)
        ],
        "occurrence_start_at": diagnostics.occurrence_start_at.isoformat(),
        "occurrence_end_at": diagnostics.occurrence_end_at.isoformat(),
        "local_action": diagnostics.local_action,
    }
    if diagnostics.assignment_conflicts:
        payload["assignment_conflicts"] = [
            {
                "transcript_id": conflict.transcript_id,
                "created_at": conflict.created_at.isoformat(),
                "occurrence_ids": list(conflict.occurrence_ids),
            }
            for conflict in diagnostics.assignment_conflicts
        ]
    return payload


def _serialize_artifact(artifact: MeetingArtifact) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_name": artifact.source_name,
        "status": artifact.status,
        "detail": artifact.detail,
        "matched_paths": [str(path) for path in artifact.matched_paths],
    }
    if artifact.recap_provenance is not None:
        payload["recap_provenance"] = artifact.recap_provenance
    if artifact.recap_diagnostics is not None:
        payload["recap_diagnostics"] = artifact.recap_diagnostics
    if artifact.diagnostics is not None:
        payload["transcript_diagnostics"] = _serialize_transcript_diagnostics(artifact.diagnostics)
    if artifact.occurrence_validated_paths:
        payload["occurrence_validated_paths"] = [str(path) for path in artifact.occurrence_validated_paths]
    return payload
