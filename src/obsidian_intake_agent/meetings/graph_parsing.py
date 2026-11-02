"""Side-effect-free parsing of Microsoft Graph payloads for meeting sync.

Covers calendar events, attendees, chat messages, Copilot AI insights, and transcript
records, plus selecting which transcript records belong to a meeting occurrence.
Split out of ``meetings/sync.py``; ``sync`` re-exports these names for existing callers.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

from ..utils.text import normalize_whitespace
from .occurrence_assignment import AssignmentConflict, OccurrenceWindow, TranscriptCandidate, assign_transcripts
from .sync_models import (
    GraphTranscriptRecord,
    LocalTranscriptAction,
    MeetingAttendee,
    OutlookMeetingCandidate,
    SelectedTranscriptDiagnostic,
    TranscriptDiagnostics,
    _optional_string,
)


def _graph_chat_thread_id(payload: dict[str, object]) -> str | None:
    raw_chat_info = payload.get("chatInfo")
    if not isinstance(raw_chat_info, dict):
        return None
    return _optional_string(str(raw_chat_info.get("threadId") or ""))


def _graph_chat_messages(payload: dict[str, object]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in reversed(_graph_value_items(payload)):
        body = item.get("body")
        if not isinstance(body, dict):
            continue
        content = _chat_message_text(body.get("content"))
        if content is None:
            continue
        sender = _graph_chat_message_sender(item.get("from"))
        messages.append(
            {
                "sender": sender or "Unknown",
                "text": content,
            }
        )
    return messages


def _graph_chat_message_sender(raw_value: object) -> str | None:
    if not isinstance(raw_value, dict):
        return None
    for key in ("user", "application", "device"):
        payload = raw_value.get(key)
        if not isinstance(payload, dict):
            continue
        display_name = _optional_string(str(payload.get("displayName") or ""))
        if display_name is not None:
            return display_name
    return None


def _chat_message_text(raw_value: object) -> str | None:
    if raw_value is None:
        return None
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(raw_value)))
    normalized = normalize_whitespace(text)
    return normalized or None


def _graph_meeting_notes(ai_insight: dict[str, object]) -> list[str]:
    raw_notes = ai_insight.get("meetingNotes")
    if not isinstance(raw_notes, list):
        return []
    lines: list[str] = []
    for note in raw_notes:
        if not isinstance(note, dict):
            continue
        title = _optional_string(str(note.get("title") or ""))
        text = _optional_string(str(note.get("text") or ""))
        if title and text:
            lines.append(f"- {title}: {text}")
        elif title:
            lines.append(f"- {title}")
        elif text:
            lines.append(f"- {text}")
        subpoints = note.get("subpoints")
        if isinstance(subpoints, list):
            for subpoint in subpoints:
                if not isinstance(subpoint, dict):
                    continue
                sub_title = _optional_string(str(subpoint.get("title") or ""))
                sub_text = _optional_string(str(subpoint.get("text") or ""))
                if sub_title and sub_text:
                    lines.append(f"- {sub_title}: {sub_text}")
                elif sub_title:
                    lines.append(f"- {sub_title}")
                elif sub_text:
                    lines.append(f"- {sub_text}")
    return lines


def _graph_ai_action_items(ai_insight: dict[str, object]) -> list[str]:
    raw_items = ai_insight.get("actionItems")
    if not isinstance(raw_items, list):
        return []
    lines: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        owner = _optional_string(str(item.get("ownerDisplayName") or ""))
        title = _optional_string(str(item.get("title") or ""))
        text = _optional_string(str(item.get("text") or ""))
        if owner and text:
            lines.append(f"- {owner} to {text}")
        elif owner and title:
            lines.append(f"- {owner} to {title}")
        elif text:
            lines.append(f"- [ ] {text}")
        elif title:
            lines.append(f"- [ ] {title}")
    return lines


def _graph_ai_mentions(ai_insight: dict[str, object]) -> list[str]:
    raw_viewpoint = ai_insight.get("viewpoint")
    if not isinstance(raw_viewpoint, dict):
        return []
    raw_mentions = raw_viewpoint.get("mentionEvents")
    if not isinstance(raw_mentions, list):
        return []
    lines: list[str] = []
    for mention in raw_mentions:
        if not isinstance(mention, dict):
            continue
        utterance = _optional_string(str(mention.get("transcriptUtterance") or ""))
        if utterance is None:
            continue
        lines.append(f"- {utterance}")
    return lines


def _graph_value_items(payload: dict[str, object]) -> list[dict[str, object]]:
    raw_items = payload.get("value")
    if not isinstance(raw_items, list):
        raise ValueError("Graph calendar response did not contain a value list.")
    items: list[dict[str, object]] = []
    for item in raw_items:
        if isinstance(item, dict):
            items.append(item)
    return items


def _graph_transcript_records(payload: dict[str, object]) -> tuple[GraphTranscriptRecord, ...]:
    raw_items = payload.get("value")
    if not isinstance(raw_items, list):
        raise ValueError("Graph transcript response did not contain a value list.")
    records: list[GraphTranscriptRecord] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        transcript_id = _optional_string(str(item.get("id") or ""))
        if transcript_id is not None:
            created_at = None
            raw_created_at = item.get("createdDateTime")
            created_at_text = _optional_string(raw_created_at) if isinstance(raw_created_at, str) else None
            created_at_missing = "createdDateTime" not in item or (
                isinstance(raw_created_at, str) and created_at_text is None
            )
            if created_at_text is not None:
                try:
                    parsed_created_at = datetime.fromisoformat(created_at_text.replace("Z", "+00:00"))
                    if parsed_created_at.tzinfo is not None:
                        created_at = parsed_created_at
                except ValueError:
                    pass
            records.append(
                GraphTranscriptRecord(
                    transcript_id=transcript_id,
                    created_at=created_at,
                    created_at_missing=created_at_missing,
                )
            )
    return tuple(records)


def _transcript_diagnostics(
    meeting: OutlookMeetingCandidate,
    candidates: tuple[GraphTranscriptRecord, ...],
    selected: tuple[GraphTranscriptRecord, ...],
    *,
    local_action: LocalTranscriptAction,
    assignment_conflicts: tuple[AssignmentConflict, ...] = (),
) -> TranscriptDiagnostics:
    return TranscriptDiagnostics(
        candidate_count=len(candidates),
        selected_transcripts=tuple(
            SelectedTranscriptDiagnostic(record.transcript_id, record.created_at) for record in selected
        ),
        occurrence_start_at=meeting.start_at,
        occurrence_end_at=meeting.end_at,
        local_action=local_action,
        assignment_conflicts=assignment_conflicts,
    )


def _canonical_graph_transcript_records(
    records: tuple[GraphTranscriptRecord, ...],
) -> tuple[GraphTranscriptRecord, ...]:
    records_by_id: dict[str, GraphTranscriptRecord] = {}
    ordered_transcript_ids: list[str] = []
    conflicting_transcript_ids: set[str] = set()
    for record in records:
        existing = records_by_id.get(record.transcript_id)
        if existing is None:
            records_by_id[record.transcript_id] = record
            ordered_transcript_ids.append(record.transcript_id)
        elif existing != record:
            conflicting_transcript_ids.add(record.transcript_id)
    return tuple(
        records_by_id[transcript_id]
        for transcript_id in ordered_transcript_ids
        if transcript_id not in conflicting_transcript_ids
    )


def _select_transcripts_for_occurrence(
    records: tuple[GraphTranscriptRecord, ...],
    meeting: OutlookMeetingCandidate,
) -> tuple[tuple[GraphTranscriptRecord, ...], tuple[AssignmentConflict, ...]]:
    canonical_records = _canonical_graph_transcript_records(records)
    occurrence_context = meeting.occurrence_context
    current_occurrence = next(
        (occurrence for occurrence in occurrence_context if occurrence.occurrence_id == meeting.event_id),
        None,
    )
    if current_occurrence is None:
        current_occurrence = OccurrenceWindow(
            occurrence_id=meeting.event_id,
            series_id=meeting.teams_meeting_id() or meeting.series_master_id or meeting.event_id,
            scheduled_start=meeting.start_at,
            scheduled_end=meeting.end_at,
        )
        occurrence_context = occurrence_context + (current_occurrence,)
    assignment = assign_transcripts(
        occurrences=occurrence_context,
        candidates=tuple(
            TranscriptCandidate(
                transcript_id=record.transcript_id,
                series_id=current_occurrence.series_id,
                created_at=record.created_at,
            )
            for record in canonical_records
        ),
    )
    if any(meeting.event_id in conflict.occurrence_ids for conflict in assignment.conflicts):
        return (), assignment.conflicts
    assigned_ids = assignment.transcript_ids_for(meeting.event_id)
    records_by_id = {record.transcript_id: record for record in canonical_records}
    selected_records = tuple(
        records_by_id[transcript_id] for transcript_id in dict.fromkeys(assigned_ids) if transcript_id in records_by_id
    )
    return selected_records, assignment.conflicts


def _parse_graph_event(payload: dict[str, object]) -> OutlookMeetingCandidate:
    online_meeting = payload.get("onlineMeeting")
    online_meeting_provider = _optional_string(str(payload.get("onlineMeetingProvider") or "")) or None
    join_url = None
    if isinstance(online_meeting, dict):
        join_url = _optional_string(str(online_meeting.get("joinUrl") or ""))
    response_status = None
    raw_response_status = payload.get("responseStatus")
    if isinstance(raw_response_status, dict):
        response_status = _optional_string(str(raw_response_status.get("response") or ""))
    organizer = None
    raw_organizer = payload.get("organizer")
    if isinstance(raw_organizer, dict):
        raw_email = raw_organizer.get("emailAddress")
        if isinstance(raw_email, dict):
            organizer = _optional_string(str(raw_email.get("name") or raw_email.get("address") or ""))
    categories = payload.get("categories")
    category_values: tuple[str, ...] = ()
    if isinstance(categories, list):
        category_values = tuple(str(item) for item in categories)
    attendee_values = _parse_graph_attendees(payload.get("attendees"))
    body_text = _graph_event_body_text(payload.get("body")) or _optional_string(str(payload.get("bodyPreview") or ""))
    return OutlookMeetingCandidate(
        event_id=str(payload["id"]),
        subject=str(payload.get("subject") or "(untitled meeting)"),
        start_at=_parse_graph_datetime(payload["start"]),
        end_at=_parse_graph_datetime(payload["end"]),
        organizer=organizer,
        attendees=attendee_values,
        response_status=response_status,
        is_cancelled=bool(payload.get("isCancelled", False)),
        is_all_day=bool(payload.get("isAllDay", False)),
        event_type=_optional_string(str(payload.get("type") or "")),
        online_meeting_provider=online_meeting_provider,
        join_url=join_url,
        body_text=body_text,
        categories=category_values,
        show_as=_optional_string(str(payload.get("showAs") or "")),
        series_master_id=_optional_string(str(payload.get("seriesMasterId") or "")),
    )


def _graph_event_body_text(raw_value: object) -> str | None:
    if not isinstance(raw_value, dict):
        return None
    return _optional_string(str(raw_value.get("content") or ""))


def _parse_graph_attendees(raw_value: object) -> tuple[MeetingAttendee, ...]:
    if not isinstance(raw_value, list):
        return ()
    attendees: list[MeetingAttendee] = []
    for raw_attendee in raw_value:
        if not isinstance(raw_attendee, dict):
            continue
        email_payload = raw_attendee.get("emailAddress")
        name = None
        email = None
        if isinstance(email_payload, dict):
            name = _optional_string(str(email_payload.get("name") or ""))
            email = _optional_string(str(email_payload.get("address") or ""))
        attendee_name = name or email
        if attendee_name is None:
            continue
        attendee_response = None
        raw_response_status = raw_attendee.get("status")
        if isinstance(raw_response_status, dict):
            attendee_response = _optional_string(str(raw_response_status.get("response") or ""))
        attendees.append(
            MeetingAttendee(
                name=attendee_name,
                email=email,
                role=_optional_string(str(raw_attendee.get("type") or "")),
                response_status=attendee_response,
            )
        )
    return tuple(attendees)


def _parse_graph_datetime(raw_value: object) -> datetime:
    if not isinstance(raw_value, dict):
        raise ValueError("Graph event datetime payload was not a mapping.")
    date_text = str(raw_value.get("dateTime") or "").strip()
    if not date_text:
        raise ValueError("Graph event datetime payload was missing dateTime.")
    if date_text.endswith("Z"):
        return datetime.fromisoformat(date_text.replace("Z", "+00:00"))
    timezone_name = str(raw_value.get("timeZone") or "").strip()
    parsed = datetime.fromisoformat(date_text)
    if parsed.tzinfo is not None:
        return parsed
    if timezone_name.upper() == "UTC":
        return parsed.replace(tzinfo=UTC)
    raise ValueError(f"Unsupported Graph event timezone: {timezone_name or 'unknown'}")
