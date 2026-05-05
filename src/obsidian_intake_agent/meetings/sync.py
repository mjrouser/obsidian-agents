from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from ..utils.text import normalize_whitespace

ARTIFACT_SOURCE_PRIORITY = (
    "Teams .vtt transcript",
    "Teams transcript text",
    "Teams meeting chat",
    "Copilot recap / AI summary",
    "Outlook calendar metadata",
    "Manual / semi-manual intake",
)

ArtifactStatus = Literal["available", "missing", "permission_blocked", "not_attempted"]
PlanDecision = Literal["process", "skip"]


@dataclass(slots=True, frozen=True)
class MeetingArtifact:
    source_name: str
    status: ArtifactStatus
    detail: str | None = None


@dataclass(slots=True, frozen=True)
class MeetingAttendee:
    name: str
    email: str | None = None
    role: str | None = None
    response_status: str | None = None

    def display_label(self) -> str:
        label = self.name
        if self.email:
            label = f"{label} <{self.email}>"
        details = [detail for detail in (self.role, self.response_status) if detail]
        if details:
            return f"{label} ({', '.join(details)})"
        return label


@dataclass(slots=True, frozen=True)
class OutlookMeetingCandidate:
    event_id: str
    subject: str
    start_at: datetime
    end_at: datetime
    organizer: str | None = None
    attendees: tuple[MeetingAttendee, ...] = ()
    response_status: str | None = None
    is_cancelled: bool = False
    is_all_day: bool = False
    event_type: str | None = None
    online_meeting_provider: str | None = None
    join_url: str | None = None
    body_text: str | None = None
    categories: tuple[str, ...] = ()
    show_as: str | None = None

    def detected_join_url(self) -> str | None:
        explicit = _optional_string(self.join_url)
        if explicit is not None:
            return explicit
        body = _optional_string(self.body_text)
        if body is None:
            return None
        return _extract_teams_join_url(body)

    def teams_meeting_id(self) -> str | None:
        join_url = self.detected_join_url()
        if join_url is None:
            return None
        return _extract_teams_meeting_id(join_url)

    def has_meeting_content(self) -> bool:
        return self.is_teams_meeting()

    def is_teams_meeting(self) -> bool:
        provider = (self.online_meeting_provider or "").strip().lower()
        if provider in {"teamsforbusiness", "microsoftteams", "teams"}:
            return True
        join_url = self.detected_join_url()
        if join_url is None:
            return False
        normalized = join_url.lower()
        return "teams.microsoft.com/l/meetup-join/" in normalized or "teams.live.com/meet/" in normalized

    def is_focus_block(self) -> bool:
        show_as = (self.show_as or "").strip().lower()
        if show_as == "workingelsewhere":
            return True
        event_type = (self.event_type or "").strip().lower()
        if event_type == "focus":
            return True
        subject = self.subject.strip().lower()
        if "focus time" in subject:
            return True
        return any("focus time" in category.strip().lower() for category in self.categories)


@dataclass(slots=True, frozen=True)
class MeetingSourceBundle:
    meeting: OutlookMeetingCandidate
    artifacts: tuple[MeetingArtifact, ...]
    teams_meeting_id: str | None

    def available_sources(self) -> list[str]:
        return [artifact.source_name for artifact in self.artifacts if artifact.status == "available"]

    def pending_sources(self) -> list[str]:
        return [artifact.source_name for artifact in self.artifacts if artifact.status != "available"]


@dataclass(slots=True, frozen=True)
class PlannedIntakeBundleNote:
    path: Path
    attendance_confidence: str
    sources_used: tuple[str, ...]
    source_limitations: tuple[str, ...]
    content: str


@dataclass(slots=True, frozen=True)
class TranscriptSyncPlanItem:
    decision: PlanDecision
    meeting: OutlookMeetingCandidate
    bundle: MeetingSourceBundle
    reasons: tuple[str, ...]
    intake_bundle_note: PlannedIntakeBundleNote | None


@dataclass(slots=True, frozen=True)
class MeetingDiscoverySnapshot:
    meetings: tuple[OutlookMeetingCandidate, ...]
    provider_label: str
    warning: str | None = None


@dataclass(slots=True, frozen=True)
class TranscriptSyncPlan:
    since: date
    generated_at: datetime
    provider_label: str
    warning: str | None
    items: tuple[TranscriptSyncPlanItem, ...]

    @property
    def candidate_count(self) -> int:
        return len(self.items)

    @property
    def process_count(self) -> int:
        return sum(1 for item in self.items if item.decision == "process")

    @property
    def skip_count(self) -> int:
        return sum(1 for item in self.items if item.decision == "skip")


@dataclass(slots=True, frozen=True)
class BundleWriteResult:
    written_paths: tuple[Path, ...]
    skipped_existing_paths: tuple[Path, ...]

    @property
    def written_count(self) -> int:
        return len(self.written_paths)

    @property
    def skipped_existing_count(self) -> int:
        return len(self.skipped_existing_paths)


class MeetingDiscoveryClient(Protocol):
    def list_recently_ended_meetings(
        self,
        *,
        since: date,
        now: datetime,
    ) -> MeetingDiscoverySnapshot: ...


class UnconfiguredOutlookMeetingDiscoveryClient:
    def list_recently_ended_meetings(
        self,
        *,
        since: date,
        now: datetime,
    ) -> MeetingDiscoverySnapshot:
        return MeetingDiscoverySnapshot(
            meetings=(),
            provider_label="outlook_calendar",
            warning="Outlook calendar discovery is not configured yet; no meeting candidates were discovered.",
        )


class GraphOutlookMeetingDiscoveryClient:
    def __init__(
        self,
        *,
        access_token: str,
        api_base_url: str = "https://graph.microsoft.com/v1.0",
        fetch_json: Callable[[str, str], dict[str, object]] | None = None,
    ) -> None:
        self._access_token = access_token
        self._api_base_url = api_base_url.rstrip("/")
        self._fetch_json = fetch_json or _fetch_graph_json

    def list_recently_ended_meetings(
        self,
        *,
        since: date,
        now: datetime,
    ) -> MeetingDiscoverySnapshot:
        start_at = datetime.combine(since, datetime.min.time(), tzinfo=UTC)
        start_text = start_at.isoformat().replace("+00:00", "Z")
        end_text = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        url = f"{self._api_base_url}/me/calendarView?" + urlencode(
            {
                "startDateTime": start_text,
                "endDateTime": end_text,
                "$top": "200",
                "$select": (
                    "id,subject,start,end,isCancelled,isAllDay,showAs,responseStatus,"
                    "onlineMeetingProvider,onlineMeeting,bodyPreview,categories,organizer,type,attendees"
                ),
            }
        )
        meetings: list[OutlookMeetingCandidate] = []
        next_url: str | None = url
        try:
            while next_url:
                payload = self._fetch_json(next_url, self._access_token)
                for raw_item in _graph_value_items(payload):
                    meetings.append(_parse_graph_event(raw_item))
                next_value = payload.get("@odata.nextLink")
                next_url = str(next_value) if next_value else None
        except HTTPError as exc:
            return MeetingDiscoverySnapshot(
                meetings=(),
                provider_label="graph_outlook_calendar",
                warning=_graph_http_warning(exc),
            )
        except (URLError, ValueError, KeyError) as exc:
            return MeetingDiscoverySnapshot(
                meetings=(),
                provider_label="graph_outlook_calendar",
                warning=f"Graph calendar discovery failed: {exc}",
            )
        return MeetingDiscoverySnapshot(
            meetings=tuple(meetings),
            provider_label="graph_outlook_calendar",
        )


def build_transcript_sync_plan(
    *,
    client: MeetingDiscoveryClient,
    since: date,
    intake_root: Path | None = None,
    now: datetime | None = None,
) -> TranscriptSyncPlan:
    generated_at = now or datetime.now().astimezone()
    snapshot = client.list_recently_ended_meetings(since=since, now=generated_at)
    items = tuple(_plan_item(meeting, now=generated_at, intake_root=intake_root) for meeting in snapshot.meetings)
    return TranscriptSyncPlan(
        since=since,
        generated_at=generated_at,
        provider_label=snapshot.provider_label,
        warning=snapshot.warning,
        items=items,
    )


def render_transcript_sync_plan(plan: TranscriptSyncPlan, *, mode: str = "dry-run") -> str:
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
        ]
    )
    for item in plan.items:
        meeting = item.meeting
        lines.append(f'meeting_sync_item: {item.decision} event_id="{meeting.event_id}" subject="{meeting.subject}"')
        lines.append(f"  end_at: {meeting.end_at.isoformat()}")
        lines.append(f"  teams_meeting_detected: {'yes' if meeting.is_teams_meeting() else 'no'}")
        if item.bundle.teams_meeting_id:
            lines.append(f'  teams_meeting_id: "{item.bundle.teams_meeting_id}"')
        if item.intake_bundle_note is not None:
            lines.append(f"  would_write_bundle: {item.intake_bundle_note.path}")
            lines.append(f"  bundle_attendance_confidence: {item.intake_bundle_note.attendance_confidence}")
            for source_name in item.intake_bundle_note.sources_used:
                lines.append(f"  bundle_source_used: {source_name}")
            for limitation in item.intake_bundle_note.source_limitations:
                lines.append(f"  bundle_source_limitation: {limitation}")
        for source_name in item.bundle.available_sources():
            lines.append(f"  source_available: {source_name}")
        for artifact in item.bundle.artifacts:
            if artifact.status != "available":
                detail_suffix = f" ({artifact.detail})" if artifact.detail else ""
                lines.append(f"  source_pending: {artifact.source_name}={artifact.status}{detail_suffix}")
        for reason in item.reasons:
            lines.append(f"  reason: {reason}")
    return "\n".join(lines)


def write_planned_bundle_notes(plan: TranscriptSyncPlan) -> BundleWriteResult:
    written_paths: list[Path] = []
    skipped_existing_paths: list[Path] = []
    for item in plan.items:
        if item.decision != "process" or item.intake_bundle_note is None:
            continue
        path = item.intake_bundle_note.path
        if path.exists():
            skipped_existing_paths.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.intake_bundle_note.content + "\n", encoding="utf-8")
        written_paths.append(path)
    return BundleWriteResult(
        written_paths=tuple(written_paths),
        skipped_existing_paths=tuple(skipped_existing_paths),
    )


def render_bundle_write_result(result: BundleWriteResult) -> str:
    lines = [
        f"meeting_sync_bundle_notes_written: {result.written_count}",
        f"meeting_sync_bundle_notes_skipped_existing: {result.skipped_existing_count}",
    ]
    for path in result.written_paths:
        lines.append(f"bundle_note_written: {path}")
    for path in result.skipped_existing_paths:
        lines.append(f"bundle_note_skipped_existing: {path}")
    return "\n".join(lines)


def _plan_item(
    meeting: OutlookMeetingCandidate,
    *,
    now: datetime,
    intake_root: Path | None,
) -> TranscriptSyncPlanItem:
    bundle = _build_source_bundle(meeting)
    reasons: list[str] = []
    intake_bundle_note = _build_intake_bundle_note(meeting, bundle, intake_root=intake_root)

    if meeting.end_at > now:
        reasons.append("Meeting has not ended yet.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    if meeting.is_cancelled:
        reasons.append("Skipped canceled meeting.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    response_status = (meeting.response_status or "").strip().lower()
    if response_status == "declined" and not meeting.has_meeting_content():
        reasons.append("Skipped declined event because no meeting content was detected.")
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

    if intake_bundle_note is not None and intake_bundle_note.path.exists():
        reasons.append("Skipped meeting because intake bundle already exists.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    reasons.append("Would collect all available meeting artifacts with transcript sources prioritized first.")
    reasons.append("Discovery found a Teams meeting candidate from Outlook metadata.")
    reasons.append("Dry-run only: transcript, chat, recap, and bundle-note writes are not implemented yet.")
    return TranscriptSyncPlanItem("process", meeting, bundle, tuple(reasons), intake_bundle_note)


def _build_source_bundle(meeting: OutlookMeetingCandidate) -> MeetingSourceBundle:
    artifacts = []
    for source_name in ARTIFACT_SOURCE_PRIORITY:
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
) -> PlannedIntakeBundleNote | None:
    if intake_root is None:
        return None
    basename = _bundle_note_basename(meeting)
    path = intake_root / basename
    attendance_confidence, source_limitations = _bundle_transparency(bundle)
    sources_used = tuple(bundle.available_sources())
    content = render_intake_bundle_note(
        meeting=meeting,
        bundle=bundle,
        attendance_confidence=attendance_confidence,
        sources_used=sources_used,
        source_limitations=source_limitations,
    )
    return PlannedIntakeBundleNote(
        path=path,
        attendance_confidence=attendance_confidence,
        sources_used=sources_used,
        source_limitations=source_limitations,
        content=content,
    )


def render_intake_bundle_note(
    *,
    meeting: OutlookMeetingCandidate,
    bundle: MeetingSourceBundle,
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
    return "\n".join(lines)


def _bundle_note_basename(meeting: OutlookMeetingCandidate) -> str:
    title = _normalized_bundle_title(meeting.subject)
    return f"{meeting.start_at.date().isoformat()} - Teams - {title} (bundle).md"


def _normalized_bundle_title(value: str) -> str:
    title = normalize_whitespace(value)
    title = title.replace("/", "-").replace(":", "-").replace("\\", "-")
    return title or "Meeting"


def _bundle_transparency(bundle: MeetingSourceBundle) -> tuple[str, tuple[str, ...]]:
    if bundle.available_sources() == ["Outlook calendar metadata"]:
        limitations = ["Known from calendar invite; attendance not guaranteed."]
        for source_name in bundle.pending_sources():
            limitations.append(f"{source_name} was not retrieved yet.")
        return "calendar_invite_only", tuple(limitations)
    limitations = [f"{source_name} was not retrieved yet." for source_name in bundle.pending_sources()]
    return "partial_visibility", tuple(limitations)


def _optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _extract_teams_join_url(text: str) -> str | None:
    for token in text.split():
        candidate = token.strip("()<>,[]")
        lowered = candidate.lower()
        if "teams.microsoft.com/l/meetup-join/" in lowered or "teams.live.com/meet/" in lowered:
            return candidate
    return None


def _extract_teams_meeting_id(join_url: str) -> str | None:
    path = urlparse(join_url).path
    marker = "/l/meetup-join/"
    if marker not in path:
        return None
    encoded = path.split(marker, 1)[1].split("/", 1)[0]
    decoded = unquote(encoded).strip()
    return decoded or None


def _fetch_graph_json(url: str, access_token: str) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Prefer": 'outlook.timezone="UTC"',
        },
    )
    with urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def _graph_value_items(payload: dict[str, object]) -> list[dict[str, object]]:
    raw_items = payload.get("value")
    if not isinstance(raw_items, list):
        raise ValueError("Graph calendar response did not contain a value list.")
    items: list[dict[str, object]] = []
    for item in raw_items:
        if isinstance(item, dict):
            items.append(item)
    return items


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
    body_preview = _optional_string(str(payload.get("bodyPreview") or ""))
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
        body_text=body_preview,
        categories=category_values,
        show_as=_optional_string(str(payload.get("showAs") or "")),
    )


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


def _graph_http_warning(error: HTTPError) -> str:
    if error.code in {401, 403}:
        return (
            f"Graph calendar discovery failed with HTTP {error.code}. "
            "Check delegated calendar/transcript permissions and the bearer token."
        )
    return f"Graph calendar discovery failed with HTTP {error.code}."
