"""Meeting sync eligibility rules.

Decides whether a meeting should be synced and when to retry it: response
status and low-signal subject filters, terminal identity markers, fallback
polling and pending-retry windows, and occurrence-valid transcript provenance.
Reads identity markers and provenance from disk; never writes. Imported by
``meetings.sync`` and ``meetings.sync_planning``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from .bundle_paths import _meeting_identity_path
from .identity_state import identity_marker_entry_exists, read_identity_state
from .occurrence_assignment import OccurrenceWindow, selection_bounds
from .source_bundles import _preferred_artifact_path
from .sync_models import MeetingSourceBundle, OutlookMeetingCandidate

V1_INELIGIBLE_RESPONSE_STATUSES = frozenset({"declined", "none", "notresponded"})
LOW_SIGNAL_SUBJECT_PATTERNS = (re.compile(r"\boffice\W*hours\b", re.IGNORECASE),)


def _should_skip_for_v1_response_status(meeting: OutlookMeetingCandidate) -> bool:
    response_status = (meeting.response_status or "none").strip().lower()
    return response_status in V1_INELIGIBLE_RESPONSE_STATUSES


def _has_low_signal_subject(meeting: OutlookMeetingCandidate) -> bool:
    subject = meeting.subject.strip()
    return any(pattern.search(subject) for pattern in LOW_SIGNAL_SUBJECT_PATTERNS)


def _meeting_has_terminal_identity(
    meeting: OutlookMeetingCandidate,
    *,
    intake_root: Path | None,
    now: datetime,
) -> bool:
    identity_path = _meeting_identity_path(meeting, intake_root=intake_root)
    if identity_path is None or not identity_marker_entry_exists(identity_path):
        return False
    return read_identity_state(identity_path, meeting.end_at).is_terminal(now=now)


def _is_fallback_processed_poll(
    meeting: OutlookMeetingCandidate,
    *,
    marker_path: Path | None,
    now: datetime,
) -> bool:
    if marker_path is None or not identity_marker_entry_exists(marker_path):
        return False
    identity_state = read_identity_state(marker_path, meeting.end_at)
    return (
        identity_state.marker_kind == "processed"
        and identity_state.processing_source_kind == "fallback"
        and identity_state.upgrade_state == "awaiting_transcript"
        and not identity_state.is_terminal(now=now)
    )


def _datetime_at_or_after(value: datetime, deadline: datetime) -> bool:
    comparable_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    comparable_deadline = deadline.replace(tzinfo=UTC) if deadline.tzinfo is None else deadline.astimezone(UTC)
    return comparable_value >= comparable_deadline


def _datetime_as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _has_occurrence_valid_transcript_provenance(
    meeting: OutlookMeetingCandidate,
    bundle: MeetingSourceBundle,
    *,
    processor_input_source_name: str,
) -> bool:
    transcript_artifacts = tuple(
        artifact
        for source_name in ("Teams .vtt transcript", "Teams transcript text")
        if (artifact := bundle.artifact(source_name)) is not None
    )
    for artifact in transcript_artifacts:
        diagnostics = artifact.diagnostics
        if diagnostics is not None and any(
            meeting.event_id in conflict.occurrence_ids for conflict in diagnostics.assignment_conflicts
        ):
            return False

    selected_artifact = bundle.artifact(processor_input_source_name)
    if selected_artifact is None or selected_artifact.diagnostics is None:
        return False
    selected_path = _preferred_artifact_path(selected_artifact.matched_paths)
    if selected_path not in selected_artifact.occurrence_validated_paths:
        return False
    diagnostics = selected_artifact.diagnostics
    if _datetime_as_utc(diagnostics.occurrence_start_at) != _datetime_as_utc(meeting.start_at) or _datetime_as_utc(
        diagnostics.occurrence_end_at
    ) != _datetime_as_utc(meeting.end_at):
        return False

    occurrence = OccurrenceWindow(
        occurrence_id=meeting.event_id,
        series_id=meeting.series_master_id or meeting.event_id,
        scheduled_start=meeting.start_at,
        scheduled_end=meeting.end_at,
    )
    selection_start, selection_end = selection_bounds(occurrence)
    transcript_id_counts: dict[str, int] = {}
    for selected in diagnostics.selected_transcripts:
        transcript_id_counts[selected.transcript_id] = transcript_id_counts.get(selected.transcript_id, 0) + 1
    return any(
        selected.created_at is not None
        and transcript_id_counts[selected.transcript_id] == 1
        and _datetime_as_utc(selection_start)
        <= _datetime_as_utc(selected.created_at)
        <= _datetime_as_utc(selection_end)
        for selected in diagnostics.selected_transcripts
    )


def _pending_retry_until(meeting: OutlookMeetingCandidate, marker_path: Path) -> datetime:
    return read_identity_state(marker_path, meeting.end_at).retry_until


def _pending_retry_expired(
    meeting: OutlookMeetingCandidate,
    *,
    marker_path: Path,
    now: datetime,
) -> bool:
    comparable_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return comparable_now > _pending_retry_until(meeting, marker_path)
