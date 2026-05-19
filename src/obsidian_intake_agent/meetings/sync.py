from __future__ import annotations

import binascii
import hashlib
import html
import json
import re
from base64 import urlsafe_b64decode
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from ..utils.text import normalize_whitespace

ARTIFACT_SOURCE_PRIORITY = (
    "Teams .vtt transcript",
    "Teams transcript text",
    "Copilot recap / AI summary",
    "Teams meeting chat",
    "Outlook calendar metadata",
    "Manual / semi-manual intake",
)
GRAPH_REQUEST_TIMEOUT_SECONDS = 30

ArtifactStatus = Literal["available", "missing", "permission_blocked", "not_attempted"]
PlanDecision = Literal["process", "skip"]
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
RAW_TRANSCRIPTS_DIR = "Raw Transcripts"
FALLBACKS_DIR = "Fallbacks"
BUNDLES_DIR = "bundles"
BUNDLE_IDENTITIES_DIR = Path(BUNDLES_DIR) / "_meeting_sync" / "identities"
BUNDLE_RAW_TRANSCRIPTS_DIR = Path(BUNDLES_DIR) / "raw_transcripts"
BUNDLE_FALLBACKS_DIR = Path(BUNDLES_DIR) / "fallbacks"
V1_INELIGIBLE_RESPONSE_STATUSES = frozenset({"declined", "none", "notresponded"})
LOW_SIGNAL_SUBJECT_PATTERNS = (re.compile(r"\boffice\W*hours\b", re.IGNORECASE),)
SUMMARY_DERIVED_SOURCE_NAMES = frozenset({"Copilot recap / AI summary", "Manual / semi-manual summary"})
SUMMARY_DERIVED_LIMITATION = "Processor input is summary-derived and not a verbatim transcript."


class GraphOnlineMeetingNotFoundError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class MeetingArtifact:
    source_name: str
    status: ArtifactStatus
    detail: str | None = None
    matched_paths: tuple[Path, ...] = ()


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


class NoopMeetingArtifactDiscoveryClient:
    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]:
        del meeting
        return ()


class ChainedMeetingArtifactDiscoveryClient:
    def __init__(self, *clients: MeetingArtifactDiscoveryClient) -> None:
        self._clients = clients

    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]:
        artifacts: dict[str, MeetingArtifact] = {}
        current_meeting = meeting
        for client in self._clients:
            discovered = client.discover_artifacts(meeting=current_meeting)
            for artifact in discovered:
                existing = artifacts.get(artifact.source_name)
                if existing is None or _should_replace_artifact(existing=existing, candidate=artifact):
                    artifacts[artifact.source_name] = artifact
            current_meeting = replace(current_meeting, discovered_artifacts=tuple(artifacts.values()))
        return tuple(artifacts.values())


class LocalIntakeTranscriptDiscoveryClient:
    def __init__(self, *, intake_root: Path) -> None:
        self._intake_root = intake_root

    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]:
        if not self._intake_root.exists():
            detail = f"Local intake root does not exist: {self._intake_root}"
            return (
                MeetingArtifact("Teams .vtt transcript", "not_attempted", detail),
                MeetingArtifact("Teams transcript text", "not_attempted", detail),
            )

        matching_paths = tuple(self._matching_intake_paths(meeting))
        expected_stem = self._expected_stem(meeting)
        same_date_candidates = tuple(
            path for path in self._same_date_candidate_paths(meeting) if path not in matching_paths
        )
        missing_detail = _local_missing_detail(expected_stem=expected_stem, candidates=same_date_candidates)
        return (
            self._artifact_for_matches(
                source_name="Teams .vtt transcript",
                matches=tuple(path for path in matching_paths if path.suffix.lower() == ".vtt"),
                missing_detail=missing_detail,
            ),
            self._artifact_for_matches(
                source_name="Teams transcript text",
                matches=tuple(path for path in matching_paths if path.suffix.lower() in {".md", ".docx"}),
                missing_detail=missing_detail,
            ),
        )

    def _artifact_for_matches(
        self,
        *,
        source_name: str,
        matches: tuple[Path, ...],
        missing_detail: str,
    ) -> MeetingArtifact:
        if not matches:
            return MeetingArtifact(source_name, "missing", missing_detail)
        rendered_matches = ", ".join(str(path) for path in matches)
        return MeetingArtifact(
            source_name,
            "available",
            f"Matched local intake artifact(s): {rendered_matches}",
            matched_paths=matches,
        )

    def _expected_stem(self, meeting: OutlookMeetingCandidate) -> str:
        return f"{meeting.start_at.date().isoformat()} - Teams - {_normalized_bundle_title(meeting.subject)}"

    def _matching_intake_paths(self, meeting: OutlookMeetingCandidate) -> tuple[Path, ...]:
        expected_stem = self._expected_stem(meeting)
        matches: list[Path] = []
        for path in self._intake_root.rglob("*"):
            if not path.is_file():
                continue
            if _is_ignored_local_discovery_path(path.relative_to(self._intake_root)):
                continue
            if path.stem != expected_stem:
                continue
            if path.suffix.lower() not in {".vtt", ".md", ".docx"}:
                continue
            matches.append(path)
        return tuple(sorted(matches, key=_matching_intake_path_sort_key))

    def _same_date_candidate_paths(self, meeting: OutlookMeetingCandidate) -> tuple[Path, ...]:
        expected_prefix = f"{meeting.start_at.date().isoformat()} - Teams - "
        candidates: list[Path] = []
        for path in self._intake_root.rglob("*"):
            if not path.is_file():
                continue
            if _is_ignored_local_discovery_path(path.relative_to(self._intake_root)):
                continue
            if path.suffix.lower() not in {".vtt", ".md", ".docx"}:
                continue
            if not path.stem.startswith(expected_prefix):
                continue
            candidates.append(path)
        return tuple(sorted(candidates, key=_matching_intake_path_sort_key))


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
        self._fetch_json = fetch_json or (
            lambda url, token: _fetch_graph_json(url, token, prefer_outlook_timezone=True)
        )

    def list_recently_ended_meetings(
        self,
        *,
        since: date,
        now: datetime,
    ) -> MeetingDiscoverySnapshot:
        scope_warning = _graph_calendar_scope_warning(self._access_token)
        if scope_warning is not None:
            return MeetingDiscoverySnapshot(
                meetings=(),
                provider_label="graph_outlook_calendar",
                warning=scope_warning,
            )

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
                    "onlineMeetingProvider,onlineMeeting,body,bodyPreview,categories,organizer,type,attendees"
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


class GraphTranscriptDiscoveryClient:
    def __init__(
        self,
        *,
        access_token: str,
        api_base_url: str = "https://graph.microsoft.com/v1.0",
        user_id: str | None = None,
        fetch_json: Callable[[str, str], dict[str, object]] | None = None,
    ) -> None:
        self._access_token = access_token
        self._api_base_url = api_base_url.rstrip("/")
        self._user_path = f"/users/{quote(user_id, safe='')}" if user_id else "/me"
        self._fetch_json = fetch_json or _fetch_graph_json

    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]:
        join_url = meeting.detected_join_url()
        if join_url is None:
            detail = "Outlook metadata did not include a Teams join URL."
            return (MeetingArtifact("Teams transcript text", "not_attempted", detail),)

        try:
            online_meeting_id = _resolve_graph_online_meeting_id(
                api_base_url=self._api_base_url,
                user_path=self._user_path,
                access_token=self._access_token,
                join_url=join_url,
                fetch_json=self._fetch_json,
            )
            payload = self._fetch_json(
                f"{self._api_base_url}{self._user_path}/onlineMeetings/{_graph_resource_path_id(online_meeting_id)}/transcripts",
                self._access_token,
            )
            transcript_ids = _graph_transcript_ids(payload)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                return (
                    MeetingArtifact(
                        "Teams transcript text",
                        "permission_blocked",
                        _graph_http_error_detail(
                            exc,
                            default=f"Graph transcript discovery failed with HTTP {exc.code}.",
                        ),
                    ),
                )
            if exc.code == 404:
                return (
                    MeetingArtifact(
                        "Teams transcript text",
                        "missing",
                        "Graph did not find transcripts for this Teams meeting.",
                    ),
                )
            return (
                MeetingArtifact(
                    "Teams transcript text",
                    "not_attempted",
                    _graph_http_error_detail(
                        exc,
                        default=f"Graph transcript discovery failed with HTTP {exc.code}.",
                    ),
                ),
            )
        except GraphOnlineMeetingNotFoundError:
            return (
                MeetingArtifact(
                    "Teams transcript text",
                    "missing",
                    "Graph did not find an online meeting record for this Outlook event.",
                ),
            )
        except (URLError, ValueError) as exc:
            return (
                MeetingArtifact(
                    "Teams transcript text",
                    "not_attempted",
                    f"Graph transcript discovery failed: {exc}",
                ),
            )

        if not transcript_ids:
            return (
                MeetingArtifact(
                    "Teams transcript text",
                    "missing",
                    "Graph transcript discovery returned no transcript records.",
                ),
            )
        rendered_ids = ", ".join(transcript_ids)
        return (
            MeetingArtifact(
                "Teams transcript text",
                "available",
                f"Graph transcript metadata available for transcript ID(s): {rendered_ids}; content download is deferred.",
            ),
        )


class GraphTranscriptDownloadClient:
    def __init__(
        self,
        *,
        access_token: str,
        intake_root: Path,
        api_base_url: str = "https://graph.microsoft.com/v1.0",
        user_id: str | None = None,
        fetch_json: Callable[[str, str], dict[str, object]] | None = None,
        fetch_bytes: Callable[[str, str], bytes] | None = None,
    ) -> None:
        self._access_token = access_token
        self._intake_root = intake_root
        self._api_base_url = api_base_url.rstrip("/")
        self._user_path = f"/users/{quote(user_id, safe='')}" if user_id else "/me"
        self._fetch_json = fetch_json or _fetch_graph_json
        self._fetch_bytes = fetch_bytes or _fetch_graph_bytes

    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]:
        vtt_target_path = self._intake_root / _transcript_relative_path(meeting)
        if vtt_target_path.exists():
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "available",
                    f"Transcript file already exists: {vtt_target_path}",
                    matched_paths=(vtt_target_path,),
                ),
            )
        transcript_text_target_path = self._intake_root / _transcript_text_relative_path(meeting)
        if transcript_text_target_path.exists():
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "missing",
                    "Graph .vtt transcript has not been downloaded for this meeting.",
                ),
                MeetingArtifact(
                    "Teams transcript text",
                    "available",
                    f"Transcript text file already exists: {transcript_text_target_path}",
                    matched_paths=(transcript_text_target_path,),
                ),
            )

        join_url = meeting.detected_join_url()
        if join_url is None:
            detail = "Outlook metadata did not include a Teams join URL."
            return (
                MeetingArtifact("Teams .vtt transcript", "not_attempted", detail),
                MeetingArtifact("Teams transcript text", "not_attempted", detail),
            )

        try:
            online_meeting_id = _resolve_graph_online_meeting_id(
                api_base_url=self._api_base_url,
                user_path=self._user_path,
                access_token=self._access_token,
                join_url=join_url,
                fetch_json=self._fetch_json,
            )
            transcripts_url = f"{self._api_base_url}{self._user_path}/onlineMeetings/{_graph_resource_path_id(online_meeting_id)}/transcripts"
            payload = self._fetch_json(transcripts_url, self._access_token)
            transcript_ids = _graph_transcript_ids(payload)
        except HTTPError as exc:
            return self._paired_artifacts_from_http_error(exc, action="discovery")
        except GraphOnlineMeetingNotFoundError:
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "missing",
                    "Graph did not find an online meeting record for this Outlook event.",
                ),
                MeetingArtifact(
                    "Teams transcript text",
                    "missing",
                    "Graph did not find an online meeting record for this Outlook event.",
                ),
            )
        except (URLError, ValueError) as exc:
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "not_attempted",
                    f"Graph transcript discovery failed: {exc}",
                ),
                MeetingArtifact(
                    "Teams transcript text",
                    "not_attempted",
                    f"Graph transcript discovery failed: {exc}",
                ),
            )

        if not transcript_ids:
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "missing",
                    "Graph transcript discovery returned no transcript records.",
                ),
                MeetingArtifact(
                    "Teams transcript text",
                    "missing",
                    "Graph transcript discovery returned no transcript records.",
                ),
            )

        transcript_id = transcript_ids[0]
        content_url = f"{transcripts_url}/{_graph_resource_path_id(transcript_id)}/content?" + urlencode(
            {"$format": "text/vtt"}
        )
        try:
            content = self._fetch_bytes(content_url, self._access_token)
        except HTTPError as exc:
            if exc.code != 404:
                return self._paired_artifacts_from_http_error(exc, action="content download")
            metadata_content_url = f"{transcripts_url}/{_graph_resource_path_id(transcript_id)}/metadataContent"
            try:
                metadata_content = self._fetch_bytes(metadata_content_url, self._access_token)
            except HTTPError as metadata_exc:
                if metadata_exc.code == 404:
                    return (
                        MeetingArtifact(
                            "Teams .vtt transcript",
                            "missing",
                            "Graph did not find transcript content for this Teams meeting.",
                        ),
                        MeetingArtifact(
                            "Teams transcript text",
                            "missing",
                            "Graph did not find transcript metadata content for this Teams meeting.",
                        ),
                    )
                return self._paired_artifacts_from_http_error(metadata_exc, action="metadata content download")
            except URLError as metadata_exc:
                return (
                    MeetingArtifact(
                        "Teams .vtt transcript",
                        "missing",
                        "Graph did not find transcript content for this Teams meeting.",
                    ),
                    MeetingArtifact(
                        "Teams transcript text",
                        "not_attempted",
                        f"Graph transcript metadata content download failed: {metadata_exc}",
                    ),
                )

            transcript_text_target_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_text_target_path.write_text(
                _transcript_markdown_from_metadata_content(metadata_content),
                encoding="utf-8",
            )
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "missing",
                    "Graph did not find transcript content for this Teams meeting.",
                ),
                MeetingArtifact(
                    "Teams transcript text",
                    "available",
                    f"Downloaded Graph transcript metadata content for transcript ID {transcript_id}.",
                    matched_paths=(transcript_text_target_path,),
                ),
            )
        except URLError as exc:
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "not_attempted",
                    f"Graph transcript content download failed: {exc}",
                ),
                MeetingArtifact(
                    "Teams transcript text",
                    "not_attempted",
                    f"Graph transcript content download failed: {exc}",
                ),
            )

        vtt_target_path.parent.mkdir(parents=True, exist_ok=True)
        vtt_target_path.write_bytes(_normalized_vtt_bytes(content))
        return (
            MeetingArtifact(
                "Teams .vtt transcript",
                "available",
                f"Downloaded Graph transcript content for transcript ID {transcript_id}.",
                matched_paths=(vtt_target_path,),
            ),
        )

    def _http_error_artifact(self, error: HTTPError, *, action: str) -> MeetingArtifact:
        if error.code in {401, 403}:
            return MeetingArtifact(
                "Teams .vtt transcript",
                "permission_blocked",
                _graph_http_error_detail(
                    error,
                    default=f"Graph transcript {action} failed with HTTP {error.code}.",
                ),
            )
        if error.code == 404:
            return MeetingArtifact(
                "Teams .vtt transcript",
                "missing",
                "Graph did not find transcript content for this Teams meeting.",
            )
        return MeetingArtifact(
            "Teams .vtt transcript",
            "not_attempted",
            _graph_http_error_detail(
                error,
                default=f"Graph transcript {action} failed with HTTP {error.code}.",
            ),
        )

    def _paired_artifacts_from_http_error(self, error: HTTPError, *, action: str) -> tuple[MeetingArtifact, ...]:
        vtt_artifact = self._http_error_artifact(error, action=action)
        return (
            vtt_artifact,
            MeetingArtifact(
                "Teams transcript text",
                vtt_artifact.status,
                vtt_artifact.detail,
            ),
        )


class GraphMeetingFallbackSummaryClient:
    def __init__(
        self,
        *,
        access_token: str,
        intake_root: Path,
        api_base_url: str = "https://graph.microsoft.com/v1.0",
        user_id: str | None = None,
        fetch_json: Callable[[str, str], dict[str, object]] | None = None,
    ) -> None:
        self._access_token = access_token
        self._intake_root = intake_root
        self._api_base_url = api_base_url.rstrip("/")
        self._graph_user_path = f"/users/{quote(user_id, safe='')}" if user_id else "/me"
        self._copilot_user_id = user_id
        self._fetch_json = fetch_json or _fetch_graph_json

    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]:
        if _meeting_has_processor_ready_transcript_source(meeting):
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "not_attempted",
                    "Transcript-priority source is already available; recap fallback was not needed.",
                ),
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "Transcript-priority source is already available; chat fallback was not needed.",
                ),
            )

        fallback_path = self._intake_root / _fallback_summary_relative_path(meeting)
        if fallback_path.exists():
            chat_artifact = _chat_artifact_from_existing_fallback_summary(fallback_path)
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "available",
                    f"Fallback summary file already exists: {fallback_path}",
                    matched_paths=(fallback_path,),
                ),
                chat_artifact,
            )

        join_url = meeting.detected_join_url()
        if join_url is None:
            detail = "Outlook metadata did not include a Teams join URL."
            return (
                MeetingArtifact("Copilot recap / AI summary", "not_attempted", detail),
                MeetingArtifact("Teams meeting chat", "not_attempted", detail),
            )

        try:
            online_meeting_id = _resolve_graph_online_meeting_id(
                api_base_url=self._api_base_url,
                user_path=self._graph_user_path,
                access_token=self._access_token,
                join_url=join_url,
                fetch_json=self._fetch_json,
            )
        except HTTPError as exc:
            recap_artifact = self._graph_fallback_http_error_artifact(
                exc,
                source_name="Copilot recap / AI summary",
                action="online meeting lookup",
            )
            return (
                recap_artifact,
                MeetingArtifact("Teams meeting chat", "not_attempted", "Recap lookup did not complete."),
            )
        except GraphOnlineMeetingNotFoundError:
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "missing",
                    "Graph did not find an online meeting record for this Outlook event.",
                ),
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "No online meeting record was available for meeting chat lookup.",
                ),
            )
        except (URLError, ValueError) as exc:
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "not_attempted",
                    f"Graph recap discovery failed: {exc}",
                ),
                MeetingArtifact("Teams meeting chat", "not_attempted", "Recap lookup did not complete."),
            )

        try:
            user_id = self._copilot_user_id or _resolve_graph_me_user_id(
                api_base_url=self._api_base_url,
                access_token=self._access_token,
                fetch_json=self._fetch_json,
            )
            ai_insight = self._latest_ai_insight(user_id=user_id, online_meeting_id=online_meeting_id)
        except HTTPError as exc:
            recap_artifact = self._graph_fallback_http_error_artifact(
                exc,
                source_name="Copilot recap / AI summary",
                action="recap discovery",
            )
            return (
                recap_artifact,
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "Chat lookup was skipped because no recap was available.",
                ),
            )
        except (URLError, ValueError) as exc:
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "not_attempted",
                    f"Graph recap discovery failed: {exc}",
                ),
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "Chat lookup was skipped because no recap was available.",
                ),
            )

        if ai_insight is None:
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "missing",
                    "Graph recap discovery returned no AI insights for this meeting.",
                ),
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "Chat lookup was skipped because no recap was available.",
                ),
            )

        chat_artifact, chat_messages = self._meeting_chat_artifact(
            online_meeting_id=online_meeting_id,
        )
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        fallback_path.write_text(
            _render_fallback_summary_markdown(
                meeting=meeting,
                ai_insight=ai_insight,
                chat_messages=chat_messages,
                chat_artifact=chat_artifact,
            ),
            encoding="utf-8",
        )
        return (
            MeetingArtifact(
                "Copilot recap / AI summary",
                "available",
                "Downloaded Copilot recap fallback summary and wrote a local processor input.",
                matched_paths=(fallback_path,),
            ),
            chat_artifact,
        )

    def _latest_ai_insight(self, *, user_id: str, online_meeting_id: str) -> dict[str, object] | None:
        list_url = (
            f"{self._api_base_url}/copilot/users/{quote(user_id, safe='')}/onlineMeetings/"
            f"{_graph_resource_path_id(online_meeting_id)}/aiInsights"
        )
        payload = self._fetch_json(list_url, self._access_token)
        items = _graph_value_items(payload)
        if not items:
            return None
        latest_item = items[-1]
        insight_id = _optional_string(str(latest_item.get("id") or ""))
        if insight_id is None:
            raise ValueError("Graph recap discovery did not return an aiInsight id.")
        detail_url = f"{list_url}/{_graph_resource_path_id(insight_id)}"
        detail_payload = self._fetch_json(detail_url, self._access_token)
        if not isinstance(detail_payload, dict):
            raise ValueError("Graph recap detail response was not an object.")
        return detail_payload

    def _meeting_chat_artifact(
        self,
        *,
        online_meeting_id: str,
    ) -> tuple[MeetingArtifact, tuple[dict[str, str], ...]]:
        try:
            payload = self._fetch_json(
                f"{self._api_base_url}{self._graph_user_path}/onlineMeetings/{_graph_resource_path_id(online_meeting_id)}"
                "?$select=chatInfo",
                self._access_token,
            )
            thread_id = _graph_chat_thread_id(payload)
        except HTTPError as exc:
            return (
                self._graph_fallback_http_error_artifact(
                    exc,
                    source_name="Teams meeting chat",
                    action="chat discovery",
                ),
                (),
            )
        except (URLError, ValueError) as exc:
            return (
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    f"Graph chat discovery failed: {exc}",
                ),
                (),
            )

        if thread_id is None:
            return (
                MeetingArtifact(
                    "Teams meeting chat",
                    "missing",
                    "Graph online meeting metadata did not include chatInfo.threadId.",
                ),
                (),
            )

        try:
            payload = self._fetch_json(
                f"{self._api_base_url}/chats/{_graph_resource_path_id(thread_id)}/messages?"
                + urlencode({"$top": "50", "$orderby": "createdDateTime desc"}),
                self._access_token,
            )
            messages = _graph_chat_messages(payload)
        except HTTPError as exc:
            return (
                self._graph_fallback_http_error_artifact(
                    exc,
                    source_name="Teams meeting chat",
                    action="chat message retrieval",
                ),
                (),
            )
        except (URLError, ValueError) as exc:
            return (
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    f"Graph chat message retrieval failed: {exc}",
                ),
                (),
            )

        if not messages:
            return (
                MeetingArtifact(
                    "Teams meeting chat",
                    "missing",
                    "Graph meeting chat retrieval returned no readable messages.",
                ),
                (),
            )

        return (
            MeetingArtifact(
                "Teams meeting chat",
                "available",
                f"Fetched {len(messages)} Teams meeting chat message(s) to enrich the fallback summary.",
            ),
            tuple(messages),
        )

    def _graph_fallback_http_error_artifact(
        self,
        error: HTTPError,
        *,
        source_name: str,
        action: str,
    ) -> MeetingArtifact:
        if error.code in {401, 403}:
            status: ArtifactStatus = "permission_blocked"
        elif error.code == 404:
            status = "missing"
        else:
            status = "not_attempted"
        return MeetingArtifact(
            source_name,
            status,
            _graph_http_error_detail(error, default=f"Graph {action} failed with HTTP {error.code}."),
        )


def build_transcript_sync_plan(
    *,
    client: MeetingDiscoveryClient,
    artifact_discovery_client: MeetingArtifactDiscoveryClient | None = None,
    since: date,
    intake_root: Path | None = None,
    now: datetime | None = None,
) -> TranscriptSyncPlan:
    generated_at = now or datetime.now().astimezone()
    snapshot = client.list_recently_ended_meetings(since=since, now=generated_at)
    effective_artifact_discovery_client = artifact_discovery_client or NoopMeetingArtifactDiscoveryClient()
    items: list[TranscriptSyncPlanItem] = []
    for meeting in snapshot.meetings:
        if _should_prefilter_artifact_discovery(meeting, now=generated_at, intake_root=intake_root):
            items.append(_plan_item(meeting, now=generated_at, intake_root=intake_root))
            continue
        items.append(
            _plan_item(
                _discover_meeting_artifacts(meeting, artifact_discovery_client=effective_artifact_discovery_client),
                now=generated_at,
                intake_root=intake_root,
            )
        )
    return TranscriptSyncPlan(
        since=since,
        generated_at=generated_at,
        provider_label=snapshot.provider_label,
        warning=snapshot.warning,
        items=tuple(items),
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
        for reason in item.reasons:
            lines.append(f"  reason: {reason}")
    return "\n".join(lines)


def write_planned_bundle_notes(plan: TranscriptSyncPlan) -> BundleWriteResult:
    written_bundle_note_paths: list[Path] = []
    written_metadata_paths: list[Path] = []
    written_identity_paths: list[Path] = []
    skipped_existing_bundle_note_paths: list[Path] = []
    skipped_existing_metadata_paths: list[Path] = []
    skipped_existing_identity_paths: list[Path] = []
    for item in plan.items:
        if item.decision != "process" or item.intake_bundle_note is None:
            continue
        note = item.intake_bundle_note
        note.path.parent.mkdir(parents=True, exist_ok=True)
        note.identity_path.parent.mkdir(parents=True, exist_ok=True)
        if note.path.exists():
            skipped_existing_bundle_note_paths.append(note.path)
        else:
            note.path.write_text(note.content + "\n", encoding="utf-8")
            written_bundle_note_paths.append(note.path)
        if note.metadata_path.exists():
            skipped_existing_metadata_paths.append(note.metadata_path)
        else:
            note.metadata_path.write_text(note.metadata_content + "\n", encoding="utf-8")
            written_metadata_paths.append(note.metadata_path)
        if note.identity_path.exists():
            skipped_existing_identity_paths.append(note.identity_path)
        else:
            note.identity_path.write_text(note.identity_content + "\n", encoding="utf-8")
            written_identity_paths.append(note.identity_path)
    return BundleWriteResult(
        written_bundle_note_paths=tuple(written_bundle_note_paths),
        written_metadata_paths=tuple(written_metadata_paths),
        written_identity_paths=tuple(written_identity_paths),
        skipped_existing_bundle_note_paths=tuple(skipped_existing_bundle_note_paths),
        skipped_existing_metadata_paths=tuple(skipped_existing_metadata_paths),
        skipped_existing_identity_paths=tuple(skipped_existing_identity_paths),
    )


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


def _artifact_status_rank(status: ArtifactStatus) -> int:
    return {
        "not_attempted": 0,
        "missing": 1,
        "permission_blocked": 2,
        "available": 3,
    }[status]


def _should_replace_artifact(*, existing: MeetingArtifact, candidate: MeetingArtifact) -> bool:
    if candidate.status == "available":
        return existing.status != "available" or bool(candidate.matched_paths)
    return False


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
    if _meeting_identity_exists(meeting, intake_root=intake_root):
        return True
    return False


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

    if intake_bundle_note is not None and intake_bundle_note.identity_path.exists():
        reasons.append("Skipped meeting because a meeting identity marker already exists.")
        return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)

    reasons.append("Would collect all available meeting artifacts with transcript sources prioritized first.")
    reasons.append("Discovery found a Teams meeting candidate from Outlook metadata.")
    reasons.append("Bundle output preserves artifact retrieval status for transcript, chat, and recap sources.")
    return TranscriptSyncPlanItem("process", meeting, bundle, tuple(reasons), intake_bundle_note)


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
    processor_input_path, processor_input_source_name = _preferred_processor_input(bundle)
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
    )
    identity_content = render_meeting_identity_sidecar(
        meeting=meeting,
        bundle=bundle,
        bundle_note_path=path,
        metadata_path=metadata_path,
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


def _bundle_note_basename(meeting: OutlookMeetingCandidate) -> str:
    title = _normalized_bundle_title(meeting.subject)
    return f"{meeting.start_at.date().isoformat()} - Teams - {title} (bundle).md"


def _should_skip_for_v1_response_status(meeting: OutlookMeetingCandidate) -> bool:
    response_status = (meeting.response_status or "none").strip().lower()
    return response_status in V1_INELIGIBLE_RESPONSE_STATUSES


def _has_low_signal_subject(meeting: OutlookMeetingCandidate) -> bool:
    subject = meeting.subject.strip()
    return any(pattern.search(subject) for pattern in LOW_SIGNAL_SUBJECT_PATTERNS)


def _transcript_basename(meeting: OutlookMeetingCandidate) -> str:
    title = _normalized_bundle_title(meeting.subject)
    return f"{meeting.start_at.date().isoformat()} - Teams - {title}.vtt"


def _transcript_relative_path(meeting: OutlookMeetingCandidate) -> Path:
    return BUNDLE_RAW_TRANSCRIPTS_DIR / _transcript_basename(meeting)


def _transcript_text_relative_path(meeting: OutlookMeetingCandidate) -> Path:
    title = _normalized_bundle_title(meeting.subject)
    return BUNDLE_RAW_TRANSCRIPTS_DIR / f"{meeting.start_at.date().isoformat()} - Teams - {title}.md"


def _fallback_summary_relative_path(meeting: OutlookMeetingCandidate) -> Path:
    title = _normalized_bundle_title(meeting.subject)
    return BUNDLE_FALLBACKS_DIR / f"{meeting.start_at.date().isoformat()} - Teams - {title} (fallback).md"


def _outlook_metadata_basename(meeting: OutlookMeetingCandidate) -> str:
    title = _normalized_bundle_title(meeting.subject)
    return f"{meeting.start_at.date().isoformat()} - Teams - {title} (outlook).json"


def _meeting_identity_relative_path(meeting: OutlookMeetingCandidate) -> Path:
    identity_key = _meeting_identity_key(meeting)
    digest = hashlib.sha1(identity_key.encode("utf-8")).hexdigest()[:16]
    return BUNDLE_IDENTITIES_DIR / f"{meeting.start_at.date().isoformat()}-{digest}.json"


def _meeting_identity_exists(meeting: OutlookMeetingCandidate, *, intake_root: Path | None) -> bool:
    if intake_root is None:
        return False
    return (intake_root / _meeting_identity_relative_path(meeting)).exists()


def _meeting_identity_key(meeting: OutlookMeetingCandidate) -> str:
    teams_meeting_id = meeting.teams_meeting_id() or "no-teams-id"
    return f"{meeting.event_id}|{teams_meeting_id}"


def _normalized_bundle_title(value: str) -> str:
    title = normalize_whitespace(value)
    title = title.replace("/", "-").replace(":", "-").replace("\\", "-")
    return title or "Meeting"


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


def _preferred_processor_input(bundle: MeetingSourceBundle) -> tuple[Path | None, str | None]:
    for source_name in (
        "Teams .vtt transcript",
        "Teams transcript text",
        "Copilot recap / AI summary",
        "Manual / semi-manual intake",
    ):
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


def _is_ignored_local_discovery_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    return parts[:1] == ("_meeting_sync",) or parts[:2] == (BUNDLES_DIR, "_meeting_sync")


def _local_missing_detail(*, expected_stem: str, candidates: tuple[Path, ...]) -> str:
    if not candidates:
        return f"Expected local transcript stem: {expected_stem}; no same-date local transcript candidates found."
    rendered_candidates = ", ".join(str(path) for path in candidates)
    return (
        f"Expected local transcript stem: {expected_stem}; "
        f"Same-date local candidate(s): {rendered_candidates}. "
        "Candidates are suggestions only and were not selected automatically."
    )


def render_outlook_metadata_sidecar(
    *,
    meeting: OutlookMeetingCandidate,
    bundle: MeetingSourceBundle,
    processed_marker_path: Path | None = None,
) -> str:
    processor_input_path, processor_input_source_name = _preferred_processor_input(bundle)
    payload = {
        "source_type": _bundle_source_type(processor_input_source_name),
        "identity_key": _meeting_identity_key(meeting),
        "outlook_event_id": meeting.event_id,
        "subject": meeting.subject,
        "start_at": meeting.start_at.isoformat(),
        "end_at": meeting.end_at.isoformat(),
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
        "artifacts": [
            {
                "source_name": artifact.source_name,
                "status": artifact.status,
                "detail": artifact.detail,
                "matched_paths": [str(path) for path in artifact.matched_paths],
            }
            for artifact in bundle.artifacts
        ],
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
) -> str:
    payload = {
        "source_type": "meeting_sync_identity",
        "identity_key": _meeting_identity_key(meeting),
        "outlook_event_id": meeting.event_id,
        "teams_meeting_id": bundle.teams_meeting_id,
        "bundle_note_path": str(bundle_note_path),
        "outlook_metadata_path": str(metadata_path),
        "meeting_date": meeting.start_at.date().isoformat(),
        "subject": meeting.subject,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


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


def _resolve_graph_online_meeting_id(
    *,
    api_base_url: str,
    user_path: str,
    access_token: str,
    join_url: str,
    fetch_json: Callable[[str, str], dict[str, object]],
) -> str:
    lookup_url = f"{api_base_url}{user_path}/onlineMeetings?" + urlencode(
        {
            "$filter": f"JoinWebUrl eq '{_escape_graph_odata_literal(join_url)}'",
        }
    )
    payload = fetch_json(lookup_url, access_token)
    try:
        items = _graph_value_items(payload)
    except ValueError as exc:
        raise ValueError("Graph online meeting lookup response did not contain a value list.") from exc
    if not items:
        raise GraphOnlineMeetingNotFoundError("Graph did not find an online meeting record for this Outlook event.")
    raw_id = items[0].get("id")
    meeting_id = _optional_string(raw_id if isinstance(raw_id, str) else None)
    if meeting_id is None:
        raise ValueError("Graph online meeting lookup did not return an id.")
    return meeting_id


def _resolve_graph_me_user_id(
    *,
    api_base_url: str,
    access_token: str,
    fetch_json: Callable[[str, str], dict[str, object]],
) -> str:
    payload = fetch_json(f"{api_base_url}/me?$select=id", access_token)
    raw_id = payload.get("id") if isinstance(payload, dict) else None
    user_id = _optional_string(str(raw_id or ""))
    if user_id is None:
        raise ValueError("Graph /me lookup did not return a user id.")
    return user_id


def _escape_graph_odata_literal(value: str) -> str:
    return value.replace("'", "''")


def _graph_resource_path_id(value: str) -> str:
    return quote(value, safe=":@*")


def _is_graph_online_meeting_lookup_url(url: str) -> bool:
    return "/onlineMeetings?" in url and ("$filter=JoinWebUrl" in url or "%24filter=JoinWebUrl" in url)


def _fetch_graph_json(
    url: str,
    access_token: str,
    *,
    prefer_outlook_timezone: bool = False,
) -> dict[str, object]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if prefer_outlook_timezone:
        headers["Prefer"] = 'outlook.timezone="UTC"'
    request = Request(
        url,
        headers=headers,
    )
    with urlopen(request, timeout=GRAPH_REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_graph_bytes(url: str, access_token: str) -> bytes:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/vtt",
        },
    )
    with urlopen(request, timeout=GRAPH_REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def _normalized_vtt_bytes(content: bytes) -> bytes:
    return content if content.endswith(b"\n") else content + b"\n"


def _transcript_markdown_from_metadata_content(content: bytes) -> str:
    lines: list[str] = []
    for raw_line in content.decode("utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped == "WEBVTT" or "-->" in stripped:
            continue
        if stripped.isdigit():
            continue
        speaker, spoken_text = _metadata_content_line_parts(stripped)
        if spoken_text is None:
            continue
        if speaker:
            lines.append(f"{speaker}: {spoken_text}")
        else:
            lines.append(spoken_text)
    rendered = "\n".join(lines).strip()
    return f"{rendered}\n" if rendered else ""


def _metadata_content_line_parts(line: str) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return _vtt_voice_line_parts(line)
    if not isinstance(payload, dict):
        return None, None
    spoken_text = _optional_string(str(payload.get("spokenText") or ""))
    if spoken_text is None:
        return None, None
    speaker_name = _optional_string(str(payload.get("speakerName") or ""))
    return speaker_name, spoken_text


def _vtt_voice_line_parts(line: str) -> tuple[str | None, str | None]:
    if line.startswith("<v ") and ">" in line and "</v>" in line:
        speaker_name = _optional_string(line[3 : line.index(">")].strip())
        spoken_text = _optional_string(line[line.index(">") + 1 : line.rindex("</v>")].strip())
        return speaker_name, spoken_text
    return None, _optional_string(line)


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


def _meeting_has_processor_ready_transcript_source(meeting: OutlookMeetingCandidate) -> bool:
    for artifact in meeting.discovered_artifacts:
        if (
            artifact.source_name in {"Teams .vtt transcript", "Teams transcript text"}
            and artifact.status == "available"
            and artifact.matched_paths
        ):
            return True
    return False


def _chat_artifact_from_existing_fallback_summary(fallback_path: Path) -> MeetingArtifact:
    try:
        content = fallback_path.read_text(encoding="utf-8")
    except OSError as exc:
        return MeetingArtifact(
            "Teams meeting chat",
            "not_attempted",
            f"Could not inspect existing fallback summary file for chat provenance: {exc}",
        )

    meeting_chat_section = _markdown_section(content, "Meeting Chat")
    if meeting_chat_section:
        first_bullet = next(
            (line.strip() for line in meeting_chat_section.splitlines() if line.strip().startswith("- ")), None
        )
        if first_bullet is not None:
            lowered = first_bullet.lower()
            for status in ARTIFACT_STATUS_SUMMARY_SEQUENCE:
                prefix = f"- {status}:"
                if lowered.startswith(prefix):
                    detail = first_bullet[len(prefix) :].strip() or None
                    return MeetingArtifact(
                        "Teams meeting chat",
                        status,
                        f"Recovered from existing fallback summary file: {detail}"
                        if detail
                        else "Recovered from existing fallback summary file.",
                    )

    source_section = _markdown_section(content, "Source")
    if source_section and "- Source Used: Teams meeting chat" in source_section:
        return MeetingArtifact(
            "Teams meeting chat",
            "available",
            f"Recovered from existing fallback summary file: {fallback_path}",
        )
    return MeetingArtifact(
        "Teams meeting chat",
        "not_attempted",
        f"Existing fallback summary file did not record Teams meeting chat provenance: {fallback_path}",
    )


def _markdown_section(content: str, heading: str) -> str | None:
    pattern = re.compile(
        rf"^## {re.escape(heading)}\n(?P<body>.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if match is None:
        return None
    return match.group("body")


def _render_fallback_summary_markdown(
    *,
    meeting: OutlookMeetingCandidate,
    ai_insight: dict[str, object],
    chat_messages: tuple[dict[str, str], ...],
    chat_artifact: MeetingArtifact,
) -> str:
    lines = [
        f"# {meeting.start_at.date().isoformat()} - Teams - {_normalized_bundle_title(meeting.subject)}",
        "",
        "## Context",
        f"- Subject: {meeting.subject}",
        f"- Start: {meeting.start_at.isoformat()}",
        f"- End: {meeting.end_at.isoformat()}",
        f"- Organizer: {meeting.organizer or 'Unknown'}",
        f"- Your Response: {meeting.response_status or 'Unknown'}",
        "",
        "## Summary",
    ]
    meeting_notes = _graph_meeting_notes(ai_insight)
    if meeting_notes:
        lines.extend(meeting_notes)
    else:
        lines.append("- No AI-generated meeting notes were returned.")

    lines.extend(["", "## Action Items"])
    action_items = _graph_ai_action_items(ai_insight)
    if action_items:
        lines.extend(action_items)
    else:
        lines.append("- No AI-generated action items were returned.")

    lines.extend(["", "## Mentions"])
    mentions = _graph_ai_mentions(ai_insight)
    if mentions:
        lines.extend(mentions)
    else:
        lines.append("- No AI-generated mentions were returned.")

    lines.extend(["", "## Meeting Chat"])
    if chat_messages:
        for message in chat_messages:
            lines.append(f"- {message['sender']}: {message['text']}")
    else:
        if chat_artifact.detail:
            lines.append(f"- {chat_artifact.status}: {chat_artifact.detail}")
        else:
            lines.append("- No meeting chat context was available.")

    lines.extend(
        [
            "",
            "## Source",
            "- Source Used: Copilot recap / AI summary",
        ]
    )
    if chat_artifact.status == "available":
        lines.append("- Source Used: Teams meeting chat")
    lines.append("- Source Used: Outlook calendar metadata")
    lines.append("- Attendance Confidence: partial_visibility")
    lines.append(
        "- Source Limitation: Fallback artifact synthesized from summary-oriented meeting sources, not a verbatim transcript."
    )
    return "\n".join(lines) + "\n"


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


def _graph_transcript_ids(payload: dict[str, object]) -> tuple[str, ...]:
    raw_items = payload.get("value")
    if not isinstance(raw_items, list):
        raise ValueError("Graph transcript response did not contain a value list.")
    transcript_ids: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        transcript_id = _optional_string(str(item.get("id") or ""))
        if transcript_id is not None:
            transcript_ids.append(transcript_id)
    return tuple(transcript_ids)


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


def _graph_http_warning(error: HTTPError) -> str:
    if error.code in {401, 403}:
        return _graph_http_error_detail(
            error,
            default=(
                f"Graph calendar discovery failed with HTTP {error.code}. "
                "Check delegated calendar/transcript permissions and refresh Graph auth with `obsidian-agent graph login`."
            ),
        )
    return _graph_http_error_detail(
        error,
        default=f"Graph calendar discovery failed with HTTP {error.code}.",
    )


def _graph_calendar_scope_warning(access_token: str) -> str | None:
    claims = _decode_jwt_claims(access_token)
    if claims is None:
        return None

    grants = _claim_grants(claims.get("scp")) | _claim_grants(claims.get("roles"))
    if not grants:
        return None

    calendar_grants = {
        "Calendars.ReadBasic",
        "Calendars.Read",
        "Calendars.Read.Shared",
        "Calendars.ReadWrite",
        "Calendars.ReadWrite.Shared",
    }
    if grants & calendar_grants:
        return None

    grant_summary = _format_graph_grants(grants)
    return (
        "Graph token is missing delegated Microsoft Graph calendar permission. "
        "Calendar discovery needs Calendars.Read for full meeting context; "
        "Calendars.ReadBasic may only support limited metadata. "
        "Request a token with https://graph.microsoft.com/Calendars.Read. "
        f"Current token grants: {grant_summary}."
    )


def _decode_jwt_claims(access_token: str) -> dict[str, object] | None:
    parts = access_token.split(".")
    if len(parts) < 2:
        return None

    payload = parts[1] + ("=" * (-len(parts[1]) % 4))
    try:
        decoded = urlsafe_b64decode(payload.encode()).decode()
        claims = json.loads(decoded)
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


def _claim_grants(raw_value: object) -> set[str]:
    if isinstance(raw_value, str):
        return {part for part in raw_value.split() if part}
    if isinstance(raw_value, list):
        return {str(part) for part in raw_value if str(part).strip()}
    return set()


def _format_graph_grants(grants: set[str]) -> str:
    sorted_grants = sorted(grants)
    if len(sorted_grants) <= 8:
        return ", ".join(sorted_grants)
    shown = ", ".join(sorted_grants[:8])
    return f"{shown}, ... ({len(sorted_grants)} total)"


def _graph_http_error_detail(error: HTTPError, *, default: str) -> str:
    message = _graph_http_error_message(error)
    if message is None:
        return default
    return f"{default} Graph said: {message}"


def _graph_http_error_message(error: HTTPError) -> str | None:
    if error.fp is None:
        return None
    try:
        body = error.fp.read()
    except OSError:
        return None
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = body.decode("utf-8", errors="ignore").strip()
        return text or None
    if not isinstance(payload, dict):
        return None
    top_level_message = _optional_string(payload.get("message") if isinstance(payload.get("message"), str) else None)
    if top_level_message is not None:
        return top_level_message
    raw_error = payload.get("error")
    if not isinstance(raw_error, dict):
        return None
    code = _optional_string(raw_error.get("code") if isinstance(raw_error.get("code"), str) else None)
    message = _optional_string(raw_error.get("message") if isinstance(raw_error.get("message"), str) else None)
    if code and message:
        return f"{code}: {message}"
    return message or code
