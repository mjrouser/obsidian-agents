"""Local and composite meeting artifact discovery clients.

Holds the clients that never touch the network: the no-op client, the chained
client that merges results from several clients, the local intake transcript
discovery client, and the placeholder used when Outlook discovery is not
configured. Also holds the provenance assignment check shared with the Graph
transcript download client. ``meetings.sync`` re-exports these names.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from .bundle_paths import (
    BUNDLES_DIR,
    _normalized_bundle_title,
)
from .graph_parsing import (
    _select_transcripts_for_occurrence,
    _transcript_diagnostics,
)
from .source_bundles import (
    _matching_intake_path_sort_key,
    _preferred_artifact_path,
)
from .sync_models import (
    GraphTranscriptRecord,
    MeetingArtifact,
    MeetingArtifactDiscoveryClient,
    MeetingDiscoverySnapshot,
    OutlookMeetingCandidate,
    TranscriptDiagnostics,
)
from .transcript_provenance import (
    TranscriptProvenancePayload,
    matching_provenance,
)


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


def _should_replace_artifact(*, existing: MeetingArtifact, candidate: MeetingArtifact) -> bool:
    if existing.source_name == "Teams .vtt transcript":
        return False
    if candidate.status == "available":
        return existing.status != "available" or bool(candidate.matched_paths)
    return False


def _artifact_client_uses_network(client: MeetingArtifactDiscoveryClient) -> bool:
    return bool(getattr(client, "uses_network", True))


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
