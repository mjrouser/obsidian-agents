from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse

from ..utils.text import normalize_whitespace
from .graph_http import (
    GRAPH_REQUEST_TIMEOUT_SECONDS,  # noqa: F401 - re-exported from meetings.sync
    GraphOnlineMeetingNotFoundError,
    MeetingSyncGraphTimeoutError,  # noqa: F401 - re-exported from meetings.sync
    _fetch_all_graph_transcript_records,
    _fetch_graph_bytes,
    _fetch_graph_json,
    _graph_calendar_scope_warning,
    _graph_http_error_detail,
    _graph_http_warning,
    _graph_resource_path_id,
    _resolve_graph_me_user_id,
    _resolve_graph_online_meeting_id,
)
from .graph_parsing import (
    _graph_ai_action_items,
    _graph_ai_mentions,
    _graph_chat_messages,
    _graph_chat_thread_id,
    _graph_meeting_notes,
    _graph_value_items,
    _parse_graph_event,
    _select_transcripts_for_occurrence,
    _transcript_diagnostics,
)
from .identity_state import identity_marker_entry_exists, read_identity_state, write_identity_marker
from .occurrence_assignment import (
    AssignmentConflict,  # noqa: F401 - re-exported from meetings.sync
    OccurrenceWindow,
    selection_bounds,
)
from .recap_provenance import RECAP_SOURCE, create_recap_provenance, read_recap_provenance, validate_recap_payload
from .recap_selection import select_recap, validate_detail
from .sync_models import (
    ArtifactStatus,
    BundleWriteResult,
    GraphTranscriptRecord,
    LocalTranscriptAction,
    MeetingArtifact,
    MeetingArtifactDiscoveryClient,
    MeetingAttendee,  # noqa: F401 - re-exported from meetings.sync
    MeetingDiscoveryClient,
    MeetingDiscoverySnapshot,
    MeetingSourceBundle,
    OutlookMeetingCandidate,
    PlannedIntakeBundleNote,
    SelectedTranscriptDiagnostic,
    TranscriptDiagnostics,
    TranscriptSyncPlan,
    TranscriptSyncPlanItem,
    _occurrence_conflict_key,
    _optional_string,
)
from .transcript_provenance import (
    TranscriptProvenancePayload,
    archive_stale_transcript,
    atomic_create_bytes,
    atomic_write_bytes,
    matching_provenance,
    provenance_path,
    write_provenance,
)

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


class NoopMeetingArtifactDiscoveryClient:
    uses_network = False

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

    def without_network(self) -> MeetingArtifactDiscoveryClient:
        local_clients = tuple(client for client in self._clients if not _artifact_client_uses_network(client))
        if not local_clients:
            return NoopMeetingArtifactDiscoveryClient()
        if len(local_clients) == 1:
            return local_clients[0]
        return ChainedMeetingArtifactDiscoveryClient(*local_clients)

    def without_summary_fallback(self) -> MeetingArtifactDiscoveryClient:
        transcript_clients: list[MeetingArtifactDiscoveryClient] = []
        for client in self._clients:
            if getattr(client, "provides_summary_fallback", False):
                continue
            if isinstance(client, ChainedMeetingArtifactDiscoveryClient):
                client = client.without_summary_fallback()
                if isinstance(client, NoopMeetingArtifactDiscoveryClient):
                    continue
            transcript_clients.append(client)
        if not transcript_clients:
            return NoopMeetingArtifactDiscoveryClient()
        if len(transcript_clients) == 1:
            return transcript_clients[0]
        return ChainedMeetingArtifactDiscoveryClient(*transcript_clients)

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
                if existing is None:
                    artifacts[artifact.source_name] = artifact
                elif _should_replace_artifact(existing=existing, candidate=artifact):
                    if artifact.diagnostics is None and existing.diagnostics is not None:
                        artifact = replace(artifact, diagnostics=existing.diagnostics)
                    artifacts[artifact.source_name] = artifact
                elif existing.diagnostics is None and artifact.diagnostics is not None:
                    artifacts[artifact.source_name] = replace(existing, diagnostics=artifact.diagnostics)
            current_meeting = replace(current_meeting, discovered_artifacts=tuple(artifacts.values()))
        return tuple(artifacts.values())


class LocalIntakeTranscriptDiscoveryClient:
    uses_network = False

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
                meeting=meeting,
                source_name="Teams .vtt transcript",
                matches=tuple(path for path in matching_paths if path.suffix.lower() == ".vtt"),
                missing_detail=missing_detail,
            ),
            self._artifact_for_matches(
                meeting=meeting,
                source_name="Teams transcript text",
                matches=tuple(path for path in matching_paths if path.suffix.lower() in {".md", ".docx"}),
                missing_detail=missing_detail,
            ),
        )

    def _artifact_for_matches(
        self,
        *,
        meeting: OutlookMeetingCandidate,
        source_name: str,
        matches: tuple[Path, ...],
        missing_detail: str,
    ) -> MeetingArtifact:
        if not matches:
            return MeetingArtifact(source_name, "missing", missing_detail)
        rendered_matches = ", ".join(str(path) for path in matches)
        preferred_path = _preferred_artifact_path(matches)
        provenance_validation_by_path = {
            path: _validate_provenance_assignment(meeting, provenance)
            for path in matches
            if (
                provenance := matching_provenance(
                    path,
                    event_id=meeting.event_id,
                    occurrence_start_at=meeting.start_at,
                    occurrence_end_at=meeting.end_at,
                )
            )
            is not None
        }
        preferred_validation = provenance_validation_by_path.get(preferred_path)
        return MeetingArtifact(
            source_name,
            "available",
            f"Matched local intake artifact(s): {rendered_matches}",
            matched_paths=matches,
            diagnostics=(preferred_validation[0] if preferred_validation is not None else None),
            occurrence_validated_paths=tuple(
                path
                for path in matches
                if path in provenance_validation_by_path and provenance_validation_by_path[path][1]
            ),
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


def _validate_provenance_assignment(
    meeting: OutlookMeetingCandidate,
    provenance: TranscriptProvenancePayload,
) -> tuple[TranscriptDiagnostics, bool]:
    provenance_records = tuple(
        GraphTranscriptRecord(
            transcript_id=record["id"],
            created_at=(datetime.fromisoformat(record["created_at"]) if record["created_at"] is not None else None),
            created_at_missing=record["created_at"] is None,
        )
        for record in provenance["transcripts"]
    )
    selected_records, conflicts = _select_transcripts_for_occurrence(
        provenance_records,
        meeting,
    )
    diagnostics = _transcript_diagnostics(
        meeting,
        provenance_records,
        selected_records,
        local_action="validated",
        assignment_conflicts=conflicts,
    )
    return diagnostics, not conflicts and selected_records == provenance_records


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
                    "onlineMeetingProvider,onlineMeeting,body,bodyPreview,categories,organizer,type,attendees,"
                    "seriesMasterId"
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
    uses_network = True

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
            transcript_records = _fetch_all_graph_transcript_records(
                f"{self._api_base_url}{self._user_path}/onlineMeetings/{_graph_resource_path_id(online_meeting_id)}/transcripts",
                self._access_token,
                self._fetch_json,
            )
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

        if not transcript_records:
            return (
                MeetingArtifact(
                    "Teams transcript text",
                    "missing",
                    "Graph transcript discovery returned no transcript records.",
                    diagnostics=_transcript_diagnostics(
                        meeting,
                        transcript_records,
                        (),
                        local_action="none",
                    ),
                ),
            )

        selected_records, conflicts = _select_transcripts_for_occurrence(transcript_records, meeting)
        diagnostics = _transcript_diagnostics(
            meeting,
            transcript_records,
            selected_records,
            local_action="none",
            assignment_conflicts=conflicts,
        )
        if not selected_records:
            return (
                MeetingArtifact(
                    "Teams transcript text",
                    "missing",
                    "Graph transcript discovery returned no transcript records for this meeting occurrence.",
                    diagnostics=diagnostics,
                ),
            )

        rendered_ids = ", ".join(record.transcript_id for record in selected_records)
        return (
            MeetingArtifact(
                "Teams transcript text",
                "available",
                f"Graph transcript metadata available for transcript ID(s): {rendered_ids}; content download is deferred.",
                diagnostics=diagnostics,
            ),
        )


class GraphTranscriptDownloadClient:
    uses_network = True

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
            provenance = matching_provenance(
                vtt_target_path,
                event_id=meeting.event_id,
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
            )
            if provenance is not None:
                transcript_ids = ", ".join(item["id"] for item in provenance["transcripts"])
                diagnostics, exact_unique_match = _validate_provenance_assignment(meeting, provenance)
                if not exact_unique_match:
                    return (
                        MeetingArtifact(
                            "Teams .vtt transcript",
                            "missing",
                            "Stored Graph transcript provenance no longer uniquely matches this meeting occurrence.",
                            diagnostics=diagnostics,
                        ),
                    )
                return (
                    MeetingArtifact(
                        "Teams .vtt transcript",
                        "available",
                        f"Validated Graph transcript provenance for transcript ID(s): {transcript_ids}.",
                        matched_paths=(vtt_target_path,),
                        diagnostics=diagnostics,
                        occurrence_validated_paths=(vtt_target_path,),
                    ),
                )
        transcript_text_target_path = self._intake_root / _transcript_text_relative_path(meeting)
        if not vtt_target_path.exists() and transcript_text_target_path.exists():
            text_provenance = matching_provenance(
                transcript_text_target_path,
                event_id=meeting.event_id,
                occurrence_start_at=meeting.start_at,
                occurrence_end_at=meeting.end_at,
            )
            if text_provenance is not None:
                text_diagnostics, exact_unique_match = _validate_provenance_assignment(
                    meeting,
                    text_provenance,
                )
            else:
                text_diagnostics = None
                exact_unique_match = False
            transcript_text_artifact = (
                MeetingArtifact(
                    "Teams transcript text",
                    "available",
                    f"Validated Graph transcript provenance: {transcript_text_target_path}",
                    matched_paths=(transcript_text_target_path,),
                    diagnostics=text_diagnostics,
                    occurrence_validated_paths=(transcript_text_target_path,),
                )
                if exact_unique_match
                else MeetingArtifact(
                    "Teams transcript text",
                    "missing" if text_provenance is not None else "available",
                    (
                        "Stored Graph transcript provenance no longer uniquely matches this meeting occurrence."
                        if text_provenance is not None
                        else f"Transcript text file already exists: {transcript_text_target_path}"
                    ),
                    matched_paths=(() if text_provenance is not None else (transcript_text_target_path,)),
                    diagnostics=text_diagnostics,
                )
            )
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "missing",
                    "Graph .vtt transcript has not been downloaded for this meeting.",
                ),
                transcript_text_artifact,
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
            transcript_records = _fetch_all_graph_transcript_records(
                transcripts_url,
                self._access_token,
                self._fetch_json,
            )
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

        if not transcript_records:
            diagnostics = _transcript_diagnostics(meeting, transcript_records, (), local_action="none")
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "missing",
                    "Graph transcript discovery returned no transcript records.",
                    diagnostics=diagnostics,
                ),
                MeetingArtifact(
                    "Teams transcript text",
                    "missing",
                    "Graph transcript discovery returned no transcript records.",
                ),
            )

        selected_records, conflicts = _select_transcripts_for_occurrence(transcript_records, meeting)
        if not selected_records:
            diagnostics = _transcript_diagnostics(
                meeting,
                transcript_records,
                selected_records,
                local_action="none",
                assignment_conflicts=conflicts,
            )
            return (
                MeetingArtifact(
                    "Teams .vtt transcript",
                    "missing",
                    "Graph transcript discovery returned no transcript records for this meeting occurrence.",
                    diagnostics=diagnostics,
                ),
                MeetingArtifact(
                    "Teams transcript text",
                    "missing",
                    "Graph transcript discovery returned no transcript records for this meeting occurrence.",
                ),
            )

        diagnostics = _transcript_diagnostics(
            meeting,
            transcript_records,
            selected_records,
            local_action="none",
            assignment_conflicts=conflicts,
        )
        selected_transcripts = [
            {
                "id": record.transcript_id,
                "created_at": record.created_at.isoformat() if record.created_at is not None else None,
            }
            for record in selected_records
        ]
        downloaded_contents: list[bytes] = []
        for record in selected_records:
            transcript_id = record.transcript_id
            content_url = f"{transcripts_url}/{_graph_resource_path_id(transcript_id)}/content?" + urlencode(
                {"$format": "text/vtt"}
            )
            try:
                downloaded_contents.append(self._fetch_bytes(content_url, self._access_token))
            except HTTPError as exc:
                if exc.code != 404 or len(selected_records) != 1:
                    return self._paired_artifacts_from_http_error(
                        exc,
                        action="content download",
                        diagnostics=diagnostics,
                    )
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
                                diagnostics=diagnostics,
                            ),
                            MeetingArtifact(
                                "Teams transcript text",
                                "missing",
                                "Graph did not find transcript metadata content for this Teams meeting.",
                            ),
                        )
                    return self._paired_artifacts_from_http_error(
                        metadata_exc,
                        action="metadata content download",
                        diagnostics=diagnostics,
                    )
                except URLError as metadata_exc:
                    return (
                        MeetingArtifact(
                            "Teams .vtt transcript",
                            "missing",
                            "Graph did not find transcript content for this Teams meeting.",
                            diagnostics=diagnostics,
                        ),
                        MeetingArtifact(
                            "Teams transcript text",
                            "not_attempted",
                            f"Graph transcript metadata content download failed: {metadata_exc}",
                        ),
                    )

                rendered_metadata_content = _transcript_markdown_from_metadata_content(metadata_content).encode("utf-8")
                atomic_write_bytes(
                    transcript_text_target_path,
                    rendered_metadata_content,
                )
                write_provenance(
                    transcript_text_target_path,
                    event_id=meeting.event_id,
                    occurrence_start_at=meeting.start_at,
                    occurrence_end_at=meeting.end_at,
                    transcripts=selected_transcripts,
                    content=rendered_metadata_content,
                )
                return (
                    MeetingArtifact(
                        "Teams .vtt transcript",
                        "missing",
                        "Graph did not find transcript content for this Teams meeting.",
                        diagnostics=diagnostics,
                    ),
                    MeetingArtifact(
                        "Teams transcript text",
                        "available",
                        f"Downloaded Graph transcript metadata content for transcript ID {transcript_id}.",
                        matched_paths=(transcript_text_target_path,),
                        diagnostics=diagnostics,
                        occurrence_validated_paths=(transcript_text_target_path,),
                    ),
                )
            except URLError as exc:
                return (
                    MeetingArtifact(
                        "Teams .vtt transcript",
                        "not_attempted",
                        f"Graph transcript content download failed: {exc}",
                        diagnostics=diagnostics,
                    ),
                    MeetingArtifact(
                        "Teams transcript text",
                        "not_attempted",
                        f"Graph transcript content download failed: {exc}",
                    ),
                )

        content = downloaded_contents[0] if len(downloaded_contents) == 1 else _merged_vtt_bytes(downloaded_contents)
        normalized_content = _normalized_vtt_bytes(content)
        transcript_ids = ", ".join(record.transcript_id for record in selected_records)
        if vtt_target_path.exists():
            existing_content = vtt_target_path.read_bytes()
            if existing_content == normalized_content:
                action = "Backfilled Graph transcript provenance"
                local_action: LocalTranscriptAction = "provenance_backfilled"
            else:
                archive_stale_transcript(
                    vtt_target_path,
                    archive_dir=self._intake_root / BUNDLE_FALLBACKS_DIR / "stale_transcripts",
                    content=existing_content,
                )
                atomic_write_bytes(vtt_target_path, normalized_content)
                action = "Replaced stale Graph transcript after occurrence validation"
                local_action = "stale_preserved_and_replaced"
        else:
            atomic_write_bytes(vtt_target_path, normalized_content)
            action = "Downloaded Graph transcript content"
            local_action = "downloaded"
        write_provenance(
            vtt_target_path,
            event_id=meeting.event_id,
            occurrence_start_at=meeting.start_at,
            occurrence_end_at=meeting.end_at,
            transcripts=selected_transcripts,
            content=normalized_content,
        )
        detail = f"{action} for transcript ID(s): {transcript_ids}."
        return (
            MeetingArtifact(
                "Teams .vtt transcript",
                "available",
                detail,
                matched_paths=(vtt_target_path,),
                diagnostics=_transcript_diagnostics(
                    meeting,
                    transcript_records,
                    selected_records,
                    local_action=local_action,
                    assignment_conflicts=conflicts,
                ),
                occurrence_validated_paths=(vtt_target_path,),
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

    def _paired_artifacts_from_http_error(
        self,
        error: HTTPError,
        *,
        action: str,
        diagnostics: TranscriptDiagnostics | None = None,
    ) -> tuple[MeetingArtifact, ...]:
        vtt_artifact = self._http_error_artifact(error, action=action)
        if diagnostics is not None:
            vtt_artifact = replace(vtt_artifact, diagnostics=diagnostics)
        return (
            vtt_artifact,
            MeetingArtifact(
                "Teams transcript text",
                vtt_artifact.status,
                vtt_artifact.detail,
            ),
        )


class GraphMeetingFallbackSummaryClient:
    uses_network = True
    provides_summary_fallback = True

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
        target, context = _recap_occurrences(meeting)
        if fallback_path.exists() or fallback_path.is_symlink():
            try:
                provenance = read_recap_provenance(fallback_path, target, context)
                return (
                    MeetingArtifact(
                        RECAP_SOURCE,
                        "available",
                        "Validated cached recap.",
                        matched_paths=(fallback_path,),
                        recap_provenance=provenance,
                    ),
                )
            except ValueError as exc:
                return (
                    MeetingArtifact(
                        RECAP_SOURCE,
                        "not_attempted",
                        f"manual_review_required: {exc}",
                        recap_diagnostics={"reason": "unverified_cached_recap"},
                    ),
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
            records = self._recap_records(user_id=user_id, online_meeting_id=online_meeting_id)
            selection = select_recap(records, target, context)
            diagnostics: dict[str, object] = {
                "reason": selection.reason,
                "candidate_count": selection.candidate_count,
                "rejected_ids": list(selection.rejected),
                "conflicting_occurrences": list(selection.conflicts),
                "occurrence_id": target.occurrence_id,
                "selection_start_at": selection_bounds(target)[0].isoformat(),
                "selection_end_at": selection_bounds(target)[1].isoformat(),
                "context_start_at": min(item.scheduled_start for item in context).isoformat(),
                "context_end_at": max(item.scheduled_end for item in context).isoformat(),
            }
            if selection.selected is None:
                status: ArtifactStatus = "missing" if selection.reason == "no_matching_recap" else "not_attempted"
                return (MeetingArtifact(RECAP_SOURCE, status, selection.reason, recap_diagnostics=diagnostics),)
            detail_url = f"{self._recap_list_url(user_id, online_meeting_id)}/{_graph_resource_path_id(str(selection.selected['id']))}"
            detail_cache = getattr(self, "_recap_detail_cache", {})
            ai_insight = detail_cache.get(detail_url)
            if ai_insight is None:
                ai_insight = self._fetch_json(detail_url, self._access_token)
            selected_metadata = validate_detail(selection.selected, ai_insight)
            detail_cache[detail_url] = ai_insight
            self._recap_detail_cache = detail_cache
            diagnostics["selected_insight"] = selected_metadata
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

        chat_artifact = MeetingArtifact(
            "Teams meeting chat",
            "not_attempted",
            "Occurrence-scoped chat is unavailable; recurring-thread enrichment omitted.",
        )
        chat_messages: tuple[dict[str, str], ...] = ()
        planned_content = _render_fallback_summary_markdown(
            meeting=meeting,
            ai_insight=ai_insight,
            chat_messages=chat_messages,
            chat_artifact=chat_artifact,
        )
        return (
            MeetingArtifact(
                "Copilot recap / AI summary",
                "available",
                "Discovered Copilot recap fallback summary; local persistence is planned for explicit bundle write.",
                matched_paths=(fallback_path,),
                planned_content=planned_content,
                recap_provenance=create_recap_provenance(
                    content=planned_content.encode("utf-8"),
                    target=target,
                    context=context,
                    insight=selected_metadata,
                    online_meeting_id=online_meeting_id,
                ),
                recap_diagnostics=diagnostics,
            ),
            chat_artifact,
        )

    def reset_run_cache(self) -> None:
        self._recap_cache: dict[str, list[dict[str, object]]] = {}
        self._recap_detail_cache = {}

    def _recap_list_url(self, user_id: str, online_meeting_id: str) -> str:
        return (
            f"{self._api_base_url}/copilot/users/{quote(user_id, safe='')}/onlineMeetings/"
            f"{_graph_resource_path_id(online_meeting_id)}/aiInsights"
        )

    def _recap_records(self, *, user_id: str, online_meeting_id: str) -> list[dict[str, object]]:
        url = self._recap_list_url(user_id, online_meeting_id)
        cache = getattr(self, "_recap_cache", {})
        if url in cache:
            return cache[url]
        records: list[dict[str, object]] = []
        seen: set[str] = set()
        next_url: str | None = url
        expected = urlparse(self._api_base_url)
        while next_url:
            parsed = urlparse(next_url)
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.fragment
                or (parsed.hostname, parsed.port or 443) != (expected.hostname, expected.port or 443)
                or next_url in seen
            ):
                raise ValueError("recap_discovery_incomplete: unsafe pagination URL")
            seen.add(next_url)
            page = self._fetch_json(next_url, self._access_token)
            if not isinstance(page, dict):
                raise ValueError("recap_discovery_incomplete: malformed page")
            rows = page.get("value")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("recap_discovery_incomplete: malformed page")
            records.extend(rows)
            link = page.get("@odata.nextLink")
            if link is not None and (not isinstance(link, str) or not link):
                raise ValueError("recap_discovery_incomplete: malformed nextLink")
            next_url = link
        cache[url] = records
        self._recap_cache = cache
        return records

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


def build_transcript_sync_plan(
    *,
    client: MeetingDiscoveryClient,
    artifact_discovery_client: MeetingArtifactDiscoveryClient | None = None,
    since: date,
    intake_root: Path | None = None,
    now: datetime | None = None,
    transcript_grace_minutes: int = 60,
) -> TranscriptSyncPlan:
    generated_at = now or datetime.now().astimezone()
    snapshot = client.list_recently_ended_meetings(since=since, now=generated_at)
    effective_artifact_discovery_client = artifact_discovery_client or NoopMeetingArtifactDiscoveryClient()
    _reset_recap_caches(effective_artifact_discovery_client)
    items: list[TranscriptSyncPlanItem] = []
    for meeting in _meetings_with_occurrence_context(snapshot.meetings):
        if _should_prefilter_artifact_discovery(meeting, now=generated_at, intake_root=intake_root):
            items.append(
                _plan_item(
                    meeting,
                    now=generated_at,
                    intake_root=intake_root,
                    transcript_grace_minutes=transcript_grace_minutes,
                )
            )
            continue
        artifact_client = effective_artifact_discovery_client
        marker_path = _meeting_identity_path(meeting, intake_root=intake_root)
        fallback_processed_poll = _is_fallback_processed_poll(
            meeting,
            marker_path=marker_path,
            now=generated_at,
        )
        if fallback_processed_poll:
            artifact_client = _artifact_client_without_summary_fallback(artifact_client)
        if (
            marker_path is not None
            and identity_marker_entry_exists(marker_path)
            and _pending_retry_expired(meeting, marker_path=marker_path, now=generated_at)
        ):
            artifact_client = _artifact_client_without_network(effective_artifact_discovery_client)
        items.append(
            _plan_item(
                _discover_meeting_artifacts(meeting, artifact_discovery_client=artifact_client),
                now=generated_at,
                intake_root=intake_root,
                transcript_grace_minutes=transcript_grace_minutes,
                fallback_processed_poll=fallback_processed_poll,
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
        bundle_note_content = (note.content + "\n").encode("utf-8")
        metadata_content = (note.metadata_content + "\n").encode("utf-8")
        metadata_payload = json.loads(metadata_content)
        if not isinstance(metadata_payload, dict):
            raise ValueError("Rendered Outlook metadata sidecar must be a JSON object.")
        identity_payload = json.loads(note.identity_content)
        if not isinstance(identity_payload, dict):
            raise ValueError("Rendered meeting identity marker must be a JSON object.")
        owned_bundles_root = _prepare_owned_bundles_root(note.path.parent)
        note.identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_state = (
            read_identity_state(note.identity_path, item.meeting.end_at)
            if identity_marker_entry_exists(note.identity_path)
            else None
        )
        if identity_state is not None and "processing_exclusion" in identity_state.payload:
            skipped_existing_identity_paths.append(note.identity_path)
            skipped_existing_bundle_note_paths.append(note.path)
            skipped_existing_metadata_paths.append(note.metadata_path)
            continue
        _persist_planned_artifact_content(
            item.bundle,
            owned_bundles_root=owned_bundles_root,
        )
        should_refresh_pending = identity_state is not None and identity_state.marker_kind == "pending"
        should_refresh_upgrade = (
            identity_state is not None
            and identity_state.marker_kind == "processed"
            and identity_state.processing_source_kind == "fallback"
            and note.processor_input_source_name in {"Teams .vtt transcript", "Teams transcript text"}
        )
        should_refresh_bundle = should_refresh_pending or should_refresh_upgrade
        if note.path.exists() and not should_refresh_bundle:
            skipped_existing_bundle_note_paths.append(note.path)
        else:
            atomic_write_bytes(note.path, bundle_note_content)
            written_bundle_note_paths.append(note.path)
        if note.metadata_path.exists() and not should_refresh_bundle:
            skipped_existing_metadata_paths.append(note.metadata_path)
        else:
            atomic_write_bytes(note.metadata_path, metadata_content)
            written_metadata_paths.append(note.metadata_path)
        if identity_state is not None and (
            identity_state.marker_kind == "processed" or identity_state.is_terminal(now=plan.generated_at)
        ):
            skipped_existing_identity_paths.append(note.identity_path)
        else:
            write_identity_marker(note.identity_path, identity_payload)
            written_identity_paths.append(note.identity_path)
    return BundleWriteResult(
        written_bundle_note_paths=tuple(written_bundle_note_paths),
        written_metadata_paths=tuple(written_metadata_paths),
        written_identity_paths=tuple(written_identity_paths),
        skipped_existing_bundle_note_paths=tuple(skipped_existing_bundle_note_paths),
        skipped_existing_metadata_paths=tuple(skipped_existing_metadata_paths),
        skipped_existing_identity_paths=tuple(skipped_existing_identity_paths),
    )


def _persist_planned_artifact_content(
    bundle: MeetingSourceBundle,
    *,
    owned_bundles_root: Path,
) -> None:
    for artifact in bundle.artifacts:
        if artifact.planned_content is None:
            continue
        if artifact.status != "available" or len(artifact.matched_paths) != 1:
            raise ValueError("Planned meeting artifact content requires one available local path.")
        target_path = _validate_owned_artifact_target(
            artifact.matched_paths[0],
            owned_bundles_root=owned_bundles_root,
        )
        if artifact.source_name == RECAP_SOURCE:
            target, context = _recap_occurrences(bundle.meeting)
            validate_recap_payload(artifact.recap_provenance, artifact.planned_content.encode("utf-8"), target, context)
            sidecar = _validate_owned_artifact_target(
                provenance_path(target_path), owned_bundles_root=owned_bundles_root
            )
            if target_path.exists() or sidecar.exists():
                read_recap_provenance(target_path, target, context)
                if target_path.read_bytes() != artifact.planned_content.encode("utf-8"):
                    raise ValueError("manual_review_required: competing recap create")
                continue
            if not atomic_create_bytes(target_path, artifact.planned_content.encode("utf-8")):
                read_recap_provenance(target_path, target, context)
                raise ValueError("manual_review_required: competing recap create")
            atomic_create_bytes(
                sidecar, (json.dumps(artifact.recap_provenance, sort_keys=True, indent=2) + "\n").encode()
            )
            read_recap_provenance(target_path, target, context)
        else:
            atomic_create_bytes(target_path, artifact.planned_content.encode("utf-8"))


def _prepare_owned_bundles_root(owned_bundles_root: Path) -> Path:
    lexical_root = Path(os.path.abspath(owned_bundles_root))
    if lexical_root.is_symlink():
        raise ValueError(f"Meeting sync owned bundles root is symlinked: {lexical_root}")
    lexical_root.mkdir(parents=True, exist_ok=True)
    if lexical_root.is_symlink():
        raise ValueError(f"Meeting sync owned bundles root is symlinked: {lexical_root}")
    return lexical_root


def _validate_owned_artifact_target(
    target_path: Path,
    *,
    owned_bundles_root: Path,
) -> Path:
    lexical_root = Path(os.path.abspath(owned_bundles_root))
    lexical_target = Path(os.path.abspath(target_path))
    try:
        relative_target = lexical_target.relative_to(lexical_root)
    except ValueError:
        raise ValueError(f"Planned meeting artifact target is outside the owned bundles root: {target_path}") from None
    if relative_target == Path("."):
        raise ValueError(f"Planned meeting artifact target must be a file below {lexical_root}")

    current = lexical_root
    for part in relative_target.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Planned meeting artifact target contains a symlinked path component: {current}")
        current.mkdir(exist_ok=True)
        if current.is_symlink():
            raise ValueError(f"Planned meeting artifact target contains a symlinked path component: {current}")

    if lexical_target.is_symlink():
        raise ValueError(f"Planned meeting artifact target contains a symlinked path component: {lexical_target}")
    return lexical_target


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


def _should_replace_artifact(*, existing: MeetingArtifact, candidate: MeetingArtifact) -> bool:
    if existing.source_name == "Teams .vtt transcript":
        return False
    if candidate.status == "available":
        return existing.status != "available" or bool(candidate.matched_paths)
    return False


def _artifact_client_uses_network(client: MeetingArtifactDiscoveryClient) -> bool:
    return bool(getattr(client, "uses_network", True))


def _artifact_client_without_network(client: MeetingArtifactDiscoveryClient) -> MeetingArtifactDiscoveryClient:
    without_network = getattr(client, "without_network", None)
    if callable(without_network):
        local_client = without_network()
        if not _artifact_client_uses_network(local_client):
            return local_client
    if _artifact_client_uses_network(client):
        return NoopMeetingArtifactDiscoveryClient()
    return client


def _artifact_client_without_summary_fallback(
    client: MeetingArtifactDiscoveryClient,
) -> MeetingArtifactDiscoveryClient:
    without_summary_fallback = getattr(client, "without_summary_fallback", None)
    if callable(without_summary_fallback):
        return without_summary_fallback()
    if getattr(client, "provides_summary_fallback", False):
        return NoopMeetingArtifactDiscoveryClient()
    return client


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


def _meeting_identity_path(meeting: OutlookMeetingCandidate, *, intake_root: Path | None) -> Path | None:
    if intake_root is None:
        return None
    return intake_root / _meeting_identity_relative_path(meeting)


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


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


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


def _normalized_vtt_bytes(content: bytes) -> bytes:
    return content if content.endswith(b"\n") else content + b"\n"


def _merged_vtt_bytes(contents: list[bytes]) -> bytes:
    bodies: list[bytes] = []
    for content in contents:
        normalized = content.removeprefix(b"\xef\xbb\xbf")
        lines = normalized.splitlines()
        signature_line = lines[0] if lines else b""
        has_webvtt_signature = signature_line == b"WEBVTT" or signature_line.startswith((b"WEBVTT ", b"WEBVTT\t"))
        if has_webvtt_signature and b"-->" not in signature_line:
            lines = lines[1:]
            while lines and lines[0].strip():
                lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
        body = b"\n".join(lines).strip()
        if body:
            bodies.append(body)
    return b"WEBVTT\n\n" + b"\n\n".join(bodies) + b"\n"


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


def _meeting_has_processor_ready_transcript_source(meeting: OutlookMeetingCandidate) -> bool:
    for artifact in meeting.discovered_artifacts:
        if (
            artifact.source_name in {"Teams .vtt transcript", "Teams transcript text"}
            and artifact.status == "available"
            and artifact.matched_paths
        ):
            return True
    return False


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


def _reset_recap_caches(client: MeetingArtifactDiscoveryClient) -> None:
    if isinstance(client, GraphMeetingFallbackSummaryClient):
        client.reset_run_cache()
    elif isinstance(client, ChainedMeetingArtifactDiscoveryClient):
        for child in client._clients:
            _reset_recap_caches(child)


def _recap_occurrences(meeting: OutlookMeetingCandidate) -> tuple[OccurrenceWindow, tuple[OccurrenceWindow, ...]]:
    target = OccurrenceWindow(
        meeting.event_id,
        meeting.teams_meeting_id() or meeting.series_master_id or meeting.event_id,
        meeting.start_at,
        meeting.end_at,
    )
    return target, meeting.occurrence_context or (target,)


def _validate_managed_recaps(meeting: OutlookMeetingCandidate) -> OutlookMeetingCandidate:
    artifacts: list[MeetingArtifact] = []
    target, context = _recap_occurrences(meeting)
    for artifact in meeting.discovered_artifacts:
        managed_path = artifact.source_name not in {"Teams .vtt transcript", "Teams transcript text"} and any(
            "fallbacks" in path.parts for path in artifact.matched_paths
        )
        if artifact.status == "available" and (artifact.source_name == RECAP_SOURCE or managed_path):
            try:
                if artifact.source_name != RECAP_SOURCE or len(artifact.matched_paths) != 1:
                    raise ValueError("managed recap cannot be relabeled as another source")
                if artifact.planned_content is not None:
                    provenance = validate_recap_payload(
                        artifact.recap_provenance, artifact.planned_content.encode(), target, context
                    )
                else:
                    provenance = read_recap_provenance(artifact.matched_paths[0], target, context)
                artifact = replace(artifact, recap_provenance=provenance)
            except ValueError as exc:
                artifact = replace(
                    artifact,
                    status="not_attempted",
                    detail=str(exc),
                    matched_paths=(),
                    planned_content=None,
                    recap_provenance=None,
                    recap_diagnostics={"reason": "unverified_cached_recap"},
                )
        artifacts.append(artifact)
    return replace(meeting, discovered_artifacts=tuple(artifacts))
