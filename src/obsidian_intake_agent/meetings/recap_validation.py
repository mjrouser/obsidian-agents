"""Occurrence checks for managed Copilot recap artifacts during meeting sync.

Builds the target occurrence and its series context for a meeting, and
downgrades managed recap artifacts whose provenance does not match that
occurrence. Reads recap provenance from disk; never writes. Imported by
``meetings.sync`` and ``meetings.sync_planning``.
"""

from __future__ import annotations

from dataclasses import replace

from .occurrence_assignment import OccurrenceWindow
from .recap_provenance import RECAP_SOURCE, read_recap_provenance, validate_recap_payload
from .sync_models import MeetingArtifact, OutlookMeetingCandidate


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
