from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from urllib.parse import unquote, urlparse

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
class OutlookMeetingCandidate:
    event_id: str
    subject: str
    start_at: datetime
    end_at: datetime
    organizer: str | None = None
    response_status: str | None = None
    is_cancelled: bool = False
    is_all_day: bool = False
    event_type: str | None = None
    online_meeting_provider: str | None = None
    join_url: str | None = None
    body_text: str | None = None
    categories: tuple[str, ...] = ()

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


@dataclass(slots=True, frozen=True)
class TranscriptSyncPlanItem:
    decision: PlanDecision
    meeting: OutlookMeetingCandidate
    bundle: MeetingSourceBundle
    reasons: tuple[str, ...]


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


def build_transcript_sync_plan(
    *,
    client: MeetingDiscoveryClient,
    since: date,
    now: datetime | None = None,
) -> TranscriptSyncPlan:
    generated_at = now or datetime.now().astimezone()
    snapshot = client.list_recently_ended_meetings(since=since, now=generated_at)
    items = tuple(_plan_item(meeting, now=generated_at) for meeting in snapshot.meetings)
    return TranscriptSyncPlan(
        since=since,
        generated_at=generated_at,
        provider_label=snapshot.provider_label,
        warning=snapshot.warning,
        items=items,
    )


def render_transcript_sync_plan(plan: TranscriptSyncPlan) -> str:
    lines = [
        "meeting_sync_mode: dry-run",
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
        lines.append(
            "meeting_sync_item: "
            f"{item.decision} "
            f'event_id="{meeting.event_id}" '
            f'subject="{meeting.subject}"'
        )
        lines.append(f"  end_at: {meeting.end_at.isoformat()}")
        lines.append(f"  teams_meeting_detected: {'yes' if meeting.is_teams_meeting() else 'no'}")
        if item.bundle.teams_meeting_id:
            lines.append(f'  teams_meeting_id: "{item.bundle.teams_meeting_id}"')
        for source_name in item.bundle.available_sources():
            lines.append(f"  source_available: {source_name}")
        for artifact in item.bundle.artifacts:
            if artifact.status != "available":
                detail_suffix = f" ({artifact.detail})" if artifact.detail else ""
                lines.append(f"  source_pending: {artifact.source_name}={artifact.status}{detail_suffix}")
        for reason in item.reasons:
            lines.append(f"  reason: {reason}")
    return "\n".join(lines)


def _plan_item(meeting: OutlookMeetingCandidate, *, now: datetime) -> TranscriptSyncPlanItem:
    bundle = _build_source_bundle(meeting)
    reasons: list[str] = []

    if meeting.end_at > now:
        reasons.append("Meeting has not ended yet.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons))

    if meeting.is_cancelled:
        reasons.append("Skipped canceled meeting.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons))

    response_status = (meeting.response_status or "").strip().lower()
    if response_status == "declined" and not meeting.has_meeting_content():
        reasons.append("Skipped declined event because no meeting content was detected.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons))

    if meeting.is_all_day and not meeting.has_meeting_content():
        reasons.append("Skipped all-day event because no meeting content was detected.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons))

    if meeting.is_focus_block() and not meeting.has_meeting_content():
        reasons.append("Skipped focus block because no meeting content was detected.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons))

    if not meeting.is_teams_meeting():
        reasons.append("Skipped event because Outlook metadata did not identify a Teams meeting.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons))

    reasons.append("Would collect all available meeting artifacts with transcript sources prioritized first.")
    reasons.append("Discovery found a Teams meeting candidate from Outlook metadata.")
    reasons.append("Dry-run only: transcript, chat, and recap retrieval are not implemented in this command yet.")
    return TranscriptSyncPlanItem("process", meeting, bundle, tuple(reasons))


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
