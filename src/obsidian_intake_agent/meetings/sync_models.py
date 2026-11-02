"""Data models and client protocols for meeting transcript sync.

Split out of ``meetings/sync.py``; ``sync`` re-exports these names for existing callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import unquote, urlparse

from .occurrence_assignment import AssignmentConflict, OccurrenceWindow

ArtifactStatus = Literal["available", "missing", "permission_blocked", "not_attempted"]
LocalTranscriptAction = Literal[
    "downloaded",
    "validated",
    "provenance_backfilled",
    "stale_preserved_and_replaced",
    "none",
]
PlanDecision = Literal["process", "skip"]


@dataclass(slots=True, frozen=True)
class SelectedTranscriptDiagnostic:
    transcript_id: str
    created_at: datetime | None


@dataclass(slots=True, frozen=True)
class TranscriptDiagnostics:
    candidate_count: int | None
    selected_transcripts: tuple[SelectedTranscriptDiagnostic, ...]
    occurrence_start_at: datetime
    occurrence_end_at: datetime
    local_action: LocalTranscriptAction
    assignment_conflicts: tuple[AssignmentConflict, ...] = ()


@dataclass(slots=True, frozen=True)
class MeetingArtifact:
    source_name: str
    status: ArtifactStatus
    detail: str | None = None
    matched_paths: tuple[Path, ...] = ()
    diagnostics: TranscriptDiagnostics | None = None
    planned_content: str | None = None
    recap_provenance: dict[str, object] | None = None
    recap_diagnostics: dict[str, object] | None = None
    occurrence_validated_paths: tuple[Path, ...] = ()


@dataclass(slots=True, frozen=True)
class GraphTranscriptRecord:
    transcript_id: str
    created_at: datetime | None
    created_at_missing: bool


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
    discovered_artifacts: tuple[MeetingArtifact, ...] = ()
    series_master_id: str | None = None
    occurrence_context: tuple[OccurrenceWindow, ...] = ()

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

    def source_status(self, source_name: str) -> ArtifactStatus | None:
        for artifact in self.artifacts:
            if artifact.source_name == source_name:
                return artifact.status
        return None

    def artifact(self, source_name: str) -> MeetingArtifact | None:
        for artifact in self.artifacts:
            if artifact.source_name == source_name:
                return artifact
        return None

    def artifact_paths(self, source_name: str) -> tuple[Path, ...]:
        artifact = self.artifact(source_name)
        if artifact is None:
            return ()
        return artifact.matched_paths


@dataclass(slots=True, frozen=True)
class PlannedIntakeBundleNote:
    path: Path
    metadata_path: Path
    identity_path: Path
    processor_input_path: Path | None
    processor_input_source_name: str | None
    attendance_confidence: str
    sources_used: tuple[str, ...]
    source_limitations: tuple[str, ...]
    content: str
    metadata_content: str
    identity_content: str


@dataclass(slots=True, frozen=True)
class TranscriptSyncPlanItem:
    decision: PlanDecision
    meeting: OutlookMeetingCandidate
    bundle: MeetingSourceBundle
    reasons: tuple[str, ...]
    intake_bundle_note: PlannedIntakeBundleNote | None
    fallback_deferred: bool = False
    fallback_awaiting_transcript: bool = False
    late_transcript_upgrade: bool = False


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

    def process_items(self) -> tuple[TranscriptSyncPlanItem, ...]:
        return tuple(item for item in self.items if item.decision == "process")

    @property
    def occurrence_errors(self) -> tuple[AssignmentConflict, ...]:
        return _unique_occurrence_assignment_conflicts(self.items)

    @property
    def occurrence_error_count(self) -> int:
        return len(self.occurrence_errors)

    @property
    def has_ambiguous_transcript_assignments(self) -> bool:
        return bool(self.occurrence_errors)

    @property
    def fallback_deferred_count(self) -> int:
        return sum(1 for item in self.items if item.fallback_deferred)

    @property
    def fallback_awaiting_transcript_count(self) -> int:
        return sum(1 for item in self.items if item.fallback_awaiting_transcript)

    @property
    def late_transcript_upgrade_count(self) -> int:
        return sum(1 for item in self.items if item.late_transcript_upgrade)


@dataclass(slots=True, frozen=True)
class BundleWriteResult:
    written_bundle_note_paths: tuple[Path, ...]
    written_metadata_paths: tuple[Path, ...]
    written_identity_paths: tuple[Path, ...]
    skipped_existing_bundle_note_paths: tuple[Path, ...]
    skipped_existing_metadata_paths: tuple[Path, ...]
    skipped_existing_identity_paths: tuple[Path, ...]

    @property
    def written_count(self) -> int:
        return len(self.written_bundle_note_paths)

    @property
    def skipped_existing_count(self) -> int:
        return len(self.skipped_existing_bundle_note_paths)


class MeetingDiscoveryClient(Protocol):
    def list_recently_ended_meetings(
        self,
        *,
        since: date,
        now: datetime,
    ) -> MeetingDiscoverySnapshot: ...


class MeetingArtifactDiscoveryClient(Protocol):
    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]: ...


def _occurrence_conflict_key(conflict: AssignmentConflict) -> tuple[str, str, tuple[str, ...]]:
    return (
        conflict.transcript_id,
        conflict.created_at.isoformat(),
        conflict.occurrence_ids,
    )


def _unique_occurrence_assignment_conflicts(
    items: tuple[TranscriptSyncPlanItem, ...],
) -> tuple[AssignmentConflict, ...]:
    conflicts: dict[tuple[str, str, tuple[str, ...]], AssignmentConflict] = {}
    for item in items:
        for artifact in item.bundle.artifacts:
            if artifact.diagnostics is None:
                continue
            for conflict in artifact.diagnostics.assignment_conflicts:
                conflicts.setdefault(_occurrence_conflict_key(conflict), conflict)
    return tuple(conflicts.values())


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
    encoded_segment = path.split(marker, 1)[1].split("/", 1)[0]
    meeting_id = _optional_string(unquote(encoded_segment))
    if meeting_id is None:
        return None
    if meeting_id.startswith("19:meeting_") and "@thread.v2" in meeting_id:
        return meeting_id
    return None
