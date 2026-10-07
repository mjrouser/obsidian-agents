"""Explicitly trusted synthetic recap fixtures for lifecycle tests."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from obsidian_intake_agent.meetings.occurrence_assignment import OccurrenceWindow
from obsidian_intake_agent.meetings.recap_provenance import create_recap_provenance
from obsidian_intake_agent.meetings.sync import MeetingArtifact, OutlookMeetingCandidate, _recap_occurrences
from obsidian_intake_agent.meetings.transcript_provenance import provenance_path


def verified_artifact(artifact: MeetingArtifact, meeting: OutlookMeetingCandidate) -> MeetingArtifact:
    if artifact.source_name != "Copilot recap / AI summary" or artifact.status != "available":
        return artifact
    target, context = _recap_occurrences(meeting)
    path = artifact.matched_paths[0]
    content = artifact.planned_content
    if content is None:
        content = path.read_text() if path.exists() else "# Synthetic recap\n"
    provenance = create_recap_provenance(
        content=content.encode(),
        target=target,
        context=context,
        insight={"id": "synthetic-insight", "createdDateTime": meeting.start_at.isoformat()},
        online_meeting_id="synthetic-online-meeting",
    )
    return replace(artifact, planned_content=content, recap_provenance=provenance)


def verify_metadata_fixture(metadata_path: Path) -> None:
    payload = json.loads(metadata_path.read_text())
    handoff = payload["processor_handoff"]
    if handoff["preferred_input_source_name"] != "Copilot recap / AI summary":
        return
    path = Path(handoff["preferred_input_path"])
    target = OccurrenceWindow(
        payload["outlook_event_id"],
        payload.setdefault("teams_meeting_id", "teams-marker"),
        datetime.fromisoformat(payload["scheduled_start_at"]),
        datetime.fromisoformat(payload["scheduled_end_at"]),
    )
    provenance = create_recap_provenance(
        content=path.read_bytes(),
        target=target,
        context=(target,),
        insight={"id": "synthetic-insight", "createdDateTime": target.scheduled_start.isoformat()},
        online_meeting_id="synthetic-online-meeting",
    )
    provenance_path(path).write_text(json.dumps(provenance))
    for artifact in payload["artifacts"]:
        if artifact["source_name"] == "Copilot recap / AI summary":
            artifact["recap_provenance"] = provenance
    metadata_path.write_text(json.dumps(payload))
