"""Microsoft Graph meeting discovery and transcript download clients.

Holds the Outlook calendar discovery client, the Teams transcript discovery
client, and the transcript download client that writes raw transcripts and
their provenance. Patch ``archive_stale_transcript``, ``atomic_write_bytes`` and
``write_provenance`` here in tests that exercise the download client.
``meetings.sync`` re-exports these names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode

from .bundle_paths import (
    BUNDLE_FALLBACKS_DIR,
    _transcript_relative_path,
    _transcript_text_relative_path,
)
from .graph_http import (
    GraphOnlineMeetingNotFoundError,
    _fetch_all_graph_transcript_records,
    _fetch_graph_bytes,
    _fetch_graph_json,
    _graph_calendar_scope_warning,
    _graph_http_error_detail,
    _graph_http_warning,
    _graph_resource_path_id,
    _resolve_graph_online_meeting_id,
)
from .graph_parsing import (
    _graph_value_items,
    _parse_graph_event,
    _select_transcripts_for_occurrence,
    _transcript_diagnostics,
)
from .local_discovery import _validate_provenance_assignment
from .sync_models import (
    LocalTranscriptAction,
    MeetingArtifact,
    MeetingDiscoverySnapshot,
    OutlookMeetingCandidate,
    TranscriptDiagnostics,
)
from .transcript_provenance import (
    archive_stale_transcript,
    atomic_write_bytes,
    matching_provenance,
    write_provenance,
)
from .vtt_merge import (
    _merged_vtt_bytes,
    _normalized_vtt_bytes,
    _transcript_markdown_from_metadata_content,
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
        transcript_text_target_path = self._intake_root / _transcript_text_relative_path(meeting)
        cached_artifacts = self._cached_transcript_artifacts(
            meeting,
            vtt_target_path=vtt_target_path,
            transcript_text_target_path=transcript_text_target_path,
        )
        if cached_artifacts is not None:
            return cached_artifacts

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
                return self._metadata_content_fallback_artifacts(
                    meeting,
                    transcripts_url=transcripts_url,
                    transcript_id=transcript_id,
                    transcript_text_target_path=transcript_text_target_path,
                    selected_transcripts=selected_transcripts,
                    diagnostics=diagnostics,
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
        action, local_action = self._store_vtt_transcript(
            meeting,
            vtt_target_path=vtt_target_path,
            normalized_content=normalized_content,
            selected_transcripts=selected_transcripts,
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

    def _cached_transcript_artifacts(
        self,
        meeting: OutlookMeetingCandidate,
        *,
        vtt_target_path: Path,
        transcript_text_target_path: Path,
    ) -> tuple[MeetingArtifact, ...] | None:
        """Return artifacts for transcripts already on disk, or None to query Graph."""
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

        return None

    def _metadata_content_fallback_artifacts(
        self,
        meeting: OutlookMeetingCandidate,
        *,
        transcripts_url: str,
        transcript_id: str,
        transcript_text_target_path: Path,
        selected_transcripts: list[dict[str, str | None]],
        diagnostics: TranscriptDiagnostics,
    ) -> tuple[MeetingArtifact, ...]:
        """Fetch metadataContent after the single selected transcript's .vtt content returned 404."""
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

    def _store_vtt_transcript(
        self,
        meeting: OutlookMeetingCandidate,
        *,
        vtt_target_path: Path,
        normalized_content: bytes,
        selected_transcripts: list[dict[str, str | None]],
    ) -> tuple[str, LocalTranscriptAction]:
        """Archive a stale VTT if needed, write the new one and its provenance.

        Returns the action label used in the artifact detail and the local action.
        """
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
        return action, local_action

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
