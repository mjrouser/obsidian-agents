"""Per-meeting transcript sync planning.

Adds occurrence context to discovered meetings, runs artifact discovery for
each meeting, and turns each meeting into a process-or-skip plan item with its
planned intake bundle note. Reads identity state from disk; never writes.
``build_transcript_sync_plan`` in ``meetings.sync`` drives these steps.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from .identity_state import identity_marker_entry_exists, read_identity_state
from .occurrence_assignment import OccurrenceWindow
from .recap_validation import _validate_managed_recaps
from .source_bundles import _build_intake_bundle_note, _build_source_bundle, _preferred_processor_input
from .sync_eligibility import (
    _datetime_at_or_after,
    _has_low_signal_subject,
    _has_occurrence_valid_transcript_provenance,
    _meeting_has_terminal_identity,
    _pending_retry_expired,
    _should_skip_for_v1_response_status,
)
from .sync_models import MeetingArtifactDiscoveryClient, OutlookMeetingCandidate, TranscriptSyncPlanItem
from .sync_rendering import _format_datetime


def _meetings_with_occurrence_context(
    meetings: tuple[OutlookMeetingCandidate, ...],
) -> tuple[OutlookMeetingCandidate, ...]:
    unique_meetings: list[OutlookMeetingCandidate] = []
    meetings_by_event_id: dict[str, OutlookMeetingCandidate] = {}
    for meeting in meetings:
        existing = meetings_by_event_id.get(meeting.event_id)
        if existing is None:
            meetings_by_event_id[meeting.event_id] = meeting
            unique_meetings.append(meeting)
        elif existing != meeting:
            raise ValueError("Meeting snapshot contains conflicting definitions for the same Outlook event ID.")

    grouped_meetings: dict[tuple[str, str], list[OutlookMeetingCandidate]] = {}
    series_labels: dict[tuple[str, str], str] = {}
    meeting_group_keys: dict[str, tuple[str, str]] = {}
    for meeting in unique_meetings:
        teams_meeting_id = meeting.teams_meeting_id()
        if teams_meeting_id is not None:
            group_key = ("teams_meeting", teams_meeting_id)
            series_label = teams_meeting_id
        elif meeting.series_master_id is not None:
            group_key = ("series_master", meeting.series_master_id)
            series_label = meeting.series_master_id
        else:
            group_key = ("isolated_event", meeting.event_id)
            series_label = meeting.event_id
        grouped_meetings.setdefault(group_key, []).append(meeting)
        series_labels[group_key] = series_label
        meeting_group_keys[meeting.event_id] = group_key

    occurrence_contexts = {
        group_key: tuple(
            OccurrenceWindow(
                occurrence_id=meeting.event_id,
                series_id=series_labels[group_key],
                scheduled_start=meeting.start_at,
                scheduled_end=meeting.end_at,
            )
            for meeting in sorted(
                group,
                key=lambda candidate: (
                    candidate.start_at,
                    candidate.end_at,
                    candidate.event_id,
                ),
            )
        )
        for group_key, group in grouped_meetings.items()
    }
    return tuple(
        replace(
            meeting,
            occurrence_context=occurrence_contexts[meeting_group_keys[meeting.event_id]],
        )
        for meeting in unique_meetings
    )


def _discover_meeting_artifacts(
    meeting: OutlookMeetingCandidate,
    *,
    artifact_discovery_client: MeetingArtifactDiscoveryClient,
) -> OutlookMeetingCandidate:
    discovered_artifacts = artifact_discovery_client.discover_artifacts(meeting=meeting)
    if not discovered_artifacts:
        return meeting
    existing = {artifact.source_name: artifact for artifact in meeting.discovered_artifacts}
    for artifact in discovered_artifacts:
        existing[artifact.source_name] = artifact
    return replace(meeting, discovered_artifacts=tuple(existing.values()))


def _should_prefilter_artifact_discovery(
    meeting: OutlookMeetingCandidate,
    *,
    now: datetime,
    intake_root: Path | None,
) -> bool:
    if meeting.end_at > now:
        return True
    if meeting.is_cancelled:
        return True
    if _should_skip_for_v1_response_status(meeting):
        return True
    if _has_low_signal_subject(meeting):
        return True
    if meeting.is_all_day and not meeting.has_meeting_content():
        return True
    if meeting.is_focus_block() and not meeting.has_meeting_content():
        return True
    if not meeting.is_teams_meeting():
        return True
    if _meeting_has_terminal_identity(meeting, intake_root=intake_root, now=now):
        return True
    return False


def _plan_item(
    meeting: OutlookMeetingCandidate,
    *,
    now: datetime,
    intake_root: Path | None,
    transcript_grace_minutes: int = 60,
    fallback_processed_poll: bool = False,
) -> TranscriptSyncPlanItem:
    meeting = _validate_managed_recaps(meeting)
    bundle = _build_source_bundle(meeting)
    reasons: list[str] = []
    fallback_deferred = False
    fallback_not_before = meeting.end_at + timedelta(minutes=transcript_grace_minutes)
    allow_summary_fallback = _datetime_at_or_after(now, fallback_not_before)
    intake_bundle_note = _build_intake_bundle_note(
        meeting,
        bundle,
        intake_root=intake_root,
        generated_at=now,
        transcript_grace_minutes=transcript_grace_minutes,
        allow_summary_fallback=allow_summary_fallback,
    )

    if meeting.end_at > now:
        reasons.append("Meeting has not ended yet.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    if meeting.is_cancelled:
        reasons.append("Skipped canceled meeting.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    if _should_skip_for_v1_response_status(meeting):
        reasons.append(f"Skipped event because response status was {meeting.response_status or 'none'}.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    if _has_low_signal_subject(meeting):
        reasons.append("Skipped low-signal office-hours event.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    if meeting.is_all_day and not meeting.has_meeting_content():
        reasons.append("Skipped all-day event because no meeting content was detected.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    if meeting.is_focus_block() and not meeting.has_meeting_content():
        reasons.append("Skipped focus block because no meeting content was detected.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    if not meeting.is_teams_meeting():
        reasons.append("Skipped event because Outlook metadata did not identify a Teams meeting.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    if fallback_processed_poll:
        processor_input_path, processor_input_source_name = _preferred_processor_input(
            bundle,
            allow_summary_fallback=False,
        )
        if (
            processor_input_path is None
            or processor_input_source_name
            not in {
                "Teams .vtt transcript",
                "Teams transcript text",
            }
            or not _has_occurrence_valid_transcript_provenance(
                meeting,
                bundle,
                processor_input_source_name=processor_input_source_name,
            )
        ):
            reasons.append(
                "No occurrence-valid transcript provenance was found while polling a fallback-processed meeting."
            )
            return TranscriptSyncPlanItem(
                "skip",
                meeting,
                bundle,
                tuple(reasons),
                None,
                fallback_awaiting_transcript=True,
            )
        reasons.append("Would upgrade fallback-processed meeting with a transcript.")
        return TranscriptSyncPlanItem(
            "process",
            meeting,
            bundle,
            tuple(reasons),
            intake_bundle_note,
            late_transcript_upgrade=True,
        )

    if intake_bundle_note is not None and identity_marker_entry_exists(intake_bundle_note.identity_path):
        identity_state = read_identity_state(intake_bundle_note.identity_path, meeting.end_at)
        if identity_state.is_terminal(now=now):
            reasons.append("Skipped meeting because it was already processed.")
            return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)
        if _pending_retry_expired(meeting, marker_path=intake_bundle_note.identity_path, now=now):
            reasons.append("Artifact retry window expired after 24 hours; network artifact discovery was skipped.")
        else:
            reasons.append("Retrying pending meeting artifact discovery.")

    recap = bundle.artifact("Copilot recap / AI summary")
    transcript_input_path, _ = _preferred_processor_input(bundle, allow_summary_fallback=False)
    if (
        not allow_summary_fallback
        and transcript_input_path is None
        and recap is not None
        and recap.status == "available"
        and recap.matched_paths
    ):
        reasons.append(f"Recap fallback deferred until {_format_datetime(fallback_not_before)}.")
        fallback_deferred = True

    reasons.append("Would collect all available meeting artifacts with transcript sources prioritized first.")
    reasons.append("Discovery found a Teams meeting candidate from Outlook metadata.")
    reasons.append("Bundle output preserves artifact retrieval status for transcript, chat, and recap sources.")
    return TranscriptSyncPlanItem(
        "process",
        meeting,
        bundle,
        tuple(reasons),
        intake_bundle_note,
        fallback_deferred=fallback_deferred,
    )
