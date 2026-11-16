"""Meeting source bundles: artifact selection, bundle notes, and sidecars.

Builds a ``MeetingSourceBundle`` from discovered artifacts, picks the preferred
processor input, explains what is missing, and renders the planned intake
bundle note with its Outlook metadata and meeting identity sidecars. No disk
writes. Imported by ``meetings.sync``, which re-exports the public names.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .bundle_paths import (
    BUNDLES_DIR,
    _bundle_note_basename,
    _meeting_identity_key,
    _meeting_identity_relative_path,
    _normalized_bundle_title,
    _outlook_metadata_basename,
)
from .occurrence_assignment import OccurrenceWindow, selection_bounds
from .sync_models import (
    MeetingArtifact,
    MeetingSourceBundle,
    OutlookMeetingCandidate,
    PlannedIntakeBundleNote,
)
from .sync_rendering import _format_datetime, _serialize_artifact

ARTIFACT_SOURCE_PRIORITY = (
    "Teams .vtt transcript",
    "Teams transcript text",
    "Copilot recap / AI summary",
    "Teams meeting chat",
    "Outlook calendar metadata",
    "Manual / semi-manual intake",
)
PENDING_MARKER_SOURCE_TYPE = "meeting_sync_pending"
PENDING_ARTIFACT_RETRY_HOURS = 24
SUMMARY_DERIVED_SOURCE_NAMES = frozenset({"Copilot recap / AI summary", "Manual / semi-manual summary"})
SUMMARY_DERIVED_LIMITATION = "Processor input is summary-derived and not a verbatim transcript."


def _build_source_bundle(meeting: OutlookMeetingCandidate) -> MeetingSourceBundle:
    discovered_artifacts = {artifact.source_name: artifact for artifact in meeting.discovered_artifacts}
    artifacts = []
    for source_name in ARTIFACT_SOURCE_PRIORITY:
        discovered = discovered_artifacts.get(source_name)
        if discovered is not None:
            artifacts.append(discovered)
            continue
        if source_name == "Outlook calendar metadata":
            artifacts.append(
                MeetingArtifact(
                    source_name=source_name,
                    status="available",
                    detail="Candidate discovery used Outlook event metadata.",
                )
            )
            continue
        artifacts.append(
            MeetingArtifact(
                source_name=source_name,
                status="not_attempted",
                detail="Discovery-only dry run; artifact retrieval is deferred.",
            )
        )
    return MeetingSourceBundle(
        meeting=meeting,
        artifacts=tuple(artifacts),
        teams_meeting_id=meeting.teams_meeting_id(),
    )


def _build_intake_bundle_note(
    meeting: OutlookMeetingCandidate,
    bundle: MeetingSourceBundle,
    *,
    intake_root: Path | None,
    generated_at: datetime,
    transcript_grace_minutes: int,
    allow_summary_fallback: bool,
) -> PlannedIntakeBundleNote | None:
    if intake_root is None:
        return None
    basename = _bundle_note_basename(meeting)
    bundles_root = intake_root / BUNDLES_DIR
    path = bundles_root / basename
    metadata_path = bundles_root / _outlook_metadata_basename(meeting)
    identity_path = intake_root / _meeting_identity_relative_path(meeting)
    attendance_confidence, source_limitations = _bundle_transparency(bundle)
    sources_used = tuple(bundle.available_sources())
    processor_input_path, processor_input_source_name = _preferred_processor_input(
        bundle,
        allow_summary_fallback=allow_summary_fallback,
    )
    content = render_intake_bundle_note(
        meeting=meeting,
        bundle=bundle,
        processor_input_path=processor_input_path,
        processor_input_source_name=processor_input_source_name,
        attendance_confidence=attendance_confidence,
        sources_used=sources_used,
        source_limitations=source_limitations,
    )
    metadata_content = render_outlook_metadata_sidecar(
        meeting=meeting,
        bundle=bundle,
        processed_marker_path=identity_path,
        transcript_grace_minutes=transcript_grace_minutes,
        allow_summary_fallback=allow_summary_fallback,
    )
    identity_content = render_meeting_identity_sidecar(
        meeting=meeting,
        bundle=bundle,
        bundle_note_path=path,
        metadata_path=metadata_path,
        generated_at=generated_at,
        transcript_grace_minutes=transcript_grace_minutes,
    )
    return PlannedIntakeBundleNote(
        path=path,
        metadata_path=metadata_path,
        identity_path=identity_path,
        processor_input_path=processor_input_path,
        processor_input_source_name=processor_input_source_name,
        attendance_confidence=attendance_confidence,
        sources_used=sources_used,
        source_limitations=source_limitations,
        content=content,
        metadata_content=metadata_content,
        identity_content=identity_content,
    )


def render_intake_bundle_note(
    *,
    meeting: OutlookMeetingCandidate,
    bundle: MeetingSourceBundle,
    processor_input_path: Path | None,
    processor_input_source_name: str | None,
    attendance_confidence: str,
    sources_used: tuple[str, ...],
    source_limitations: tuple[str, ...],
) -> str:
    heading = f"{meeting.start_at.date().isoformat()} - Teams - {_normalized_bundle_title(meeting.subject)}"
    lines = [
        "---",
        'intake_kind: "meeting_source_bundle"',
        f'date: "{meeting.start_at.date().isoformat()}"',
        'source: "Teams"',
        f"title: {json.dumps(_normalized_bundle_title(meeting.subject))}",
        f"outlook_event_id: {json.dumps(meeting.event_id)}",
        f"teams_meeting_id: {json.dumps(bundle.teams_meeting_id)}"
        if bundle.teams_meeting_id
        else "teams_meeting_id: null",
        f'attendance_confidence: "{attendance_confidence}"',
        "sources_used:",
    ]
    for source_name in sources_used:
        lines.append(f"  - {json.dumps(source_name)}")
    lines.append("source_limitations:")
    for limitation in source_limitations:
        lines.append(f"  - {json.dumps(limitation)}")
    lines.extend(
        [
            "---",
            f"# {heading} (bundle)",
            "",
            "## Context",
            f"- Subject: {meeting.subject}",
            f"- Start: {meeting.start_at.isoformat()}",
            f"- End: {meeting.end_at.isoformat()}",
            f"- Organizer: {meeting.organizer or 'Unknown'}",
            f"- Your Response: {meeting.response_status or 'Unknown'}",
            f"- Outlook Event ID: `{meeting.event_id}`",
            f"- Join URL: {meeting.detected_join_url()}" if meeting.detected_join_url() else "- Join URL: Unknown",
            f"- Teams Meeting ID: `{bundle.teams_meeting_id}`"
            if bundle.teams_meeting_id
            else "- Teams Meeting ID: Unknown",
            "",
            "## Source",
            f"- Attendance Confidence: {attendance_confidence}",
        ]
    )
    if meeting.attendees:
        lines.append("- Attendees:")
        for attendee in meeting.attendees:
            lines.append(f"  - {attendee.display_label()}")
    else:
        lines.append("- Attendees: Unknown")
    for source_name in sources_used:
        lines.append(f"- Source Used: {source_name}")
    for limitation in source_limitations:
        lines.append(f"- Source Limitation: {limitation}")
    lines.extend(
        [
            "",
            "## Artifact Plan",
        ]
    )
    for artifact in bundle.artifacts:
        detail = f" ({artifact.detail})" if artifact.detail else ""
        lines.append(f"- {artifact.source_name}: {artifact.status}{detail}")
        for matched_path in artifact.matched_paths:
            lines.append(f"  - Matched Path: `{matched_path}`")
    lines.extend(
        [
            "",
            "## Processor Handoff",
        ]
    )
    if processor_input_path is not None and processor_input_source_name is not None:
        lines.append(f"- Preferred Input: `{processor_input_path}`")
        lines.append(f"- Preferred Source: {processor_input_source_name}")
        lines.append(f"- Suggested Dry Run: `.venv/bin/obsidian-agent process {processor_input_path} --dry-run`")
    else:
        lines.append("- Preferred Input: None yet")
        lines.append("- Preferred Source: None yet")
    return "\n".join(lines)


def _meeting_polling_identity_fields(
    meeting: OutlookMeetingCandidate,
    *,
    transcript_grace_minutes: int,
) -> dict[str, str]:
    occurrence = OccurrenceWindow(
        occurrence_id=meeting.event_id,
        series_id=meeting.series_master_id or meeting.event_id,
        scheduled_start=meeting.start_at,
        scheduled_end=meeting.end_at,
    )
    selection_window_start, selection_window_end = selection_bounds(occurrence)
    return {
        "fallback_not_before": _format_datetime(meeting.end_at + timedelta(minutes=transcript_grace_minutes)),
        "retry_until": _format_datetime(meeting.end_at + timedelta(hours=PENDING_ARTIFACT_RETRY_HOURS)),
        "primary_occurrence_event_id": meeting.event_id,
        "scheduled_start_at": _format_datetime(meeting.start_at),
        "scheduled_end_at": _format_datetime(meeting.end_at),
        "selection_window_start_at": _format_datetime(selection_window_start),
        "selection_window_end_at": _format_datetime(selection_window_end),
    }


def _bundle_transparency(bundle: MeetingSourceBundle) -> tuple[str, tuple[str, ...]]:
    if bundle.available_sources() == ["Outlook calendar metadata"]:
        limitations = ["Known from calendar invite; attendance not guaranteed."]
        for artifact in bundle.artifacts:
            if artifact.status == "available":
                continue
            limitations.append(_artifact_limitation(artifact))
        limitations.extend(_summary_derived_limitations(bundle))
        return "calendar_invite_only", tuple(limitations)
    limitations = [_artifact_limitation(artifact) for artifact in bundle.artifacts if artifact.status != "available"]
    limitations.extend(_summary_derived_limitations(bundle))
    return "partial_visibility", tuple(limitations)


def _summary_derived_limitations(bundle: MeetingSourceBundle) -> tuple[str, ...]:
    for artifact in bundle.artifacts:
        if artifact.status == "available" and artifact.source_name in SUMMARY_DERIVED_SOURCE_NAMES:
            return (SUMMARY_DERIVED_LIMITATION,)
    return ()


def _artifact_limitation(artifact: MeetingArtifact) -> str:
    detail = f": {artifact.detail}" if artifact.detail else "."
    if artifact.status == "missing":
        return f"{artifact.source_name} was not available{detail}"
    if artifact.status == "permission_blocked":
        return f"Permission blocked retrieval of {artifact.source_name}{detail}"
    return f"{artifact.source_name} was not retrieved yet{detail}"


def _preferred_processor_input(
    bundle: MeetingSourceBundle,
    *,
    allow_summary_fallback: bool,
) -> tuple[Path | None, str | None]:
    for source_name in (
        "Teams .vtt transcript",
        "Teams transcript text",
        "Copilot recap / AI summary",
        "Manual / semi-manual intake",
    ):
        if source_name == "Copilot recap / AI summary" and not allow_summary_fallback:
            continue
        artifact = bundle.artifact(source_name)
        if artifact is None or artifact.status != "available" or not artifact.matched_paths:
            continue
        return _preferred_artifact_path(artifact.matched_paths), artifact.source_name
    return None, None


def _bundle_source_type(preferred_input_source_name: str | None) -> str:
    source_type_by_source_name = {
        "Teams .vtt transcript": "teams_vtt_transcript",
        "Teams transcript text": "teams_transcript_text",
        "Copilot recap / AI summary": "copilot_recap_ai_summary",
        "Manual / semi-manual intake": "manual_semi_manual_intake",
    }
    if preferred_input_source_name is None:
        return "outlook_calendar_metadata"
    return source_type_by_source_name.get(preferred_input_source_name, "outlook_calendar_metadata")


def _preferred_artifact_path(paths: tuple[Path, ...]) -> Path:
    return min(paths, key=_matching_intake_path_sort_key)


def _matching_intake_path_sort_key(path: Path) -> tuple[int, str]:
    normalized_parts = tuple(part.lower() for part in path.parts)
    managed_prefix = (
        BUNDLES_DIR.lower(),
        "raw_transcripts",
    )
    is_managed_bundle_transcript = normalized_parts[-len(managed_prefix) - 1 : -1] == managed_prefix
    return (0 if is_managed_bundle_transcript else 1, str(path))


def render_outlook_metadata_sidecar(
    *,
    meeting: OutlookMeetingCandidate,
    bundle: MeetingSourceBundle,
    processed_marker_path: Path | None = None,
    transcript_grace_minutes: int = 60,
    allow_summary_fallback: bool = True,
) -> str:
    processor_input_path, processor_input_source_name = _preferred_processor_input(
        bundle,
        allow_summary_fallback=allow_summary_fallback,
    )
    payload = {
        "source_type": _bundle_source_type(processor_input_source_name),
        "identity_key": _meeting_identity_key(meeting),
        "outlook_event_id": meeting.event_id,
        "subject": meeting.subject,
        "start_at": meeting.start_at.isoformat(),
        "end_at": meeting.end_at.isoformat(),
        **_meeting_polling_identity_fields(
            meeting,
            transcript_grace_minutes=transcript_grace_minutes,
        ),
        "organizer": meeting.organizer,
        "response_status": meeting.response_status,
        "is_cancelled": meeting.is_cancelled,
        "is_all_day": meeting.is_all_day,
        "event_type": meeting.event_type,
        "online_meeting_provider": meeting.online_meeting_provider,
        "join_url": meeting.detected_join_url(),
        "teams_meeting_id": bundle.teams_meeting_id,
        "show_as": meeting.show_as,
        "categories": list(meeting.categories),
        "attendees": [
            {
                "name": attendee.name,
                "email": attendee.email,
                "role": attendee.role,
                "response_status": attendee.response_status,
            }
            for attendee in meeting.attendees
        ],
        "artifacts": [_serialize_artifact(artifact) for artifact in bundle.artifacts],
        "processor_handoff": {
            "preferred_input_path": str(processor_input_path) if processor_input_path is not None else None,
            "preferred_input_source_name": processor_input_source_name,
        },
        "processed_marker_path": str(processed_marker_path) if processed_marker_path is not None else None,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def render_meeting_identity_sidecar(
    *,
    meeting: OutlookMeetingCandidate,
    bundle: MeetingSourceBundle,
    bundle_note_path: Path,
    metadata_path: Path,
    generated_at: datetime | None = None,
    transcript_grace_minutes: int = 60,
) -> str:
    checked_at = generated_at or datetime.now(tz=meeting.end_at.tzinfo or UTC)
    payload = {
        "schema_version": 2,
        "source_type": PENDING_MARKER_SOURCE_TYPE,
        "identity_key": _meeting_identity_key(meeting),
        "outlook_event_id": meeting.event_id,
        "teams_meeting_id": bundle.teams_meeting_id,
        "bundle_note_path": str(bundle_note_path),
        "outlook_metadata_path": str(metadata_path),
        "meeting_date": meeting.start_at.date().isoformat(),
        "subject": meeting.subject,
        "first_seen_at": _format_datetime(meeting.end_at),
        "last_checked_at": _format_datetime(checked_at),
        **_meeting_polling_identity_fields(
            meeting,
            transcript_grace_minutes=transcript_grace_minutes,
        ),
        "artifact_statuses": [_serialize_artifact(artifact) for artifact in bundle.artifacts],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
