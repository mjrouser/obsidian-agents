from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from .bundle_paths import (
    _fallback_summary_relative_path,  # noqa: F401 - re-exported from meetings.sync
    _meeting_identity_key,  # noqa: F401 - re-exported from meetings.sync
    _meeting_identity_path,
    _transcript_relative_path,  # noqa: F401 - re-exported from meetings.sync
    _transcript_text_relative_path,  # noqa: F401 - re-exported from meetings.sync
)
from .bundle_writes import write_planned_bundle_notes  # noqa: F401 - re-exported from meetings.sync
from .graph_clients import (
    GraphOutlookMeetingDiscoveryClient,  # noqa: F401 - re-exported from meetings.sync
    GraphTranscriptDiscoveryClient,  # noqa: F401 - re-exported from meetings.sync
    GraphTranscriptDownloadClient,  # noqa: F401 - re-exported from meetings.sync
)
from .graph_fallback_summary import GraphMeetingFallbackSummaryClient
from .graph_http import (
    GRAPH_REQUEST_TIMEOUT_SECONDS,  # noqa: F401 - re-exported from meetings.sync
    MeetingSyncGraphTimeoutError,  # noqa: F401 - re-exported from meetings.sync
    _fetch_graph_bytes,  # noqa: F401 - re-exported from meetings.sync
    _fetch_graph_json,  # noqa: F401 - re-exported from meetings.sync
)
from .graph_parsing import (
    _select_transcripts_for_occurrence,  # noqa: F401 - re-exported from meetings.sync
)
from .identity_state import identity_marker_entry_exists
from .local_discovery import (
    ChainedMeetingArtifactDiscoveryClient,
    LocalIntakeTranscriptDiscoveryClient,  # noqa: F401 - re-exported from meetings.sync
    NoopMeetingArtifactDiscoveryClient,
    UnconfiguredOutlookMeetingDiscoveryClient,  # noqa: F401 - re-exported from meetings.sync
    _artifact_client_uses_network,
)
from .occurrence_assignment import (
    AssignmentConflict,  # noqa: F401 - re-exported from meetings.sync
)
from .recap_validation import (
    _recap_occurrences,  # noqa: F401 - re-exported from meetings.sync
)
from .source_bundles import (
    ARTIFACT_SOURCE_PRIORITY,  # noqa: F401 - re-exported from meetings.sync
    render_intake_bundle_note,  # noqa: F401 - re-exported from meetings.sync
    render_meeting_identity_sidecar,  # noqa: F401 - re-exported from meetings.sync
    render_outlook_metadata_sidecar,  # noqa: F401 - re-exported from meetings.sync
)
from .sync_eligibility import (
    _is_fallback_processed_poll,
    _pending_retry_expired,
)
from .sync_models import (
    ArtifactStatus,  # noqa: F401 - re-exported from meetings.sync
    BundleWriteResult,  # noqa: F401 - re-exported from meetings.sync
    GraphTranscriptRecord,  # noqa: F401 - re-exported from meetings.sync
    MeetingArtifact,  # noqa: F401 - re-exported from meetings.sync
    MeetingArtifactDiscoveryClient,
    MeetingAttendee,  # noqa: F401 - re-exported from meetings.sync
    MeetingDiscoveryClient,
    MeetingDiscoverySnapshot,  # noqa: F401 - re-exported from meetings.sync
    MeetingSourceBundle,  # noqa: F401 - re-exported from meetings.sync
    OutlookMeetingCandidate,  # noqa: F401 - re-exported from meetings.sync
    PlannedIntakeBundleNote,  # noqa: F401 - re-exported from meetings.sync
    SelectedTranscriptDiagnostic,  # noqa: F401 - re-exported from meetings.sync
    TranscriptDiagnostics,  # noqa: F401 - re-exported from meetings.sync
    TranscriptSyncPlan,
    TranscriptSyncPlanItem,
)
from .sync_planning import (
    _discover_meeting_artifacts,
    _meetings_with_occurrence_context,
    _plan_item,
    _should_prefilter_artifact_discovery,
)
from .sync_rendering import (
    render_bundle_write_result,  # noqa: F401 - re-exported from meetings.sync
    render_transcript_sync_plan,  # noqa: F401 - re-exported from meetings.sync
)
from .vtt_merge import (
    _merged_vtt_bytes,  # noqa: F401 - re-exported from meetings.sync
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


def _reset_recap_caches(client: MeetingArtifactDiscoveryClient) -> None:
    if isinstance(client, GraphMeetingFallbackSummaryClient):
        client.reset_run_cache()
    elif isinstance(client, ChainedMeetingArtifactDiscoveryClient):
        for child in client._clients:
            _reset_recap_caches(child)
