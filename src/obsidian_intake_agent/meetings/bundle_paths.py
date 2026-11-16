"""Bundle file naming and path rules for meeting sync.

Pure functions that decide where meeting-sync bundle files live under the
intake root: bundle note and Outlook metadata basenames, raw transcript and
fallback summary paths, and the hashed meeting identity marker path. No I/O.
Imported by ``meetings.sync``, which re-exports these names.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..utils.text import normalize_whitespace
from .sync_models import OutlookMeetingCandidate

BUNDLES_DIR = "bundles"
BUNDLE_IDENTITIES_DIR = Path(BUNDLES_DIR) / "_meeting_sync" / "identities"
BUNDLE_RAW_TRANSCRIPTS_DIR = Path(BUNDLES_DIR) / "raw_transcripts"
BUNDLE_FALLBACKS_DIR = Path(BUNDLES_DIR) / "fallbacks"


def _bundle_note_basename(meeting: OutlookMeetingCandidate) -> str:
    title = _normalized_bundle_title(meeting.subject)
    return f"{meeting.start_at.date().isoformat()} - Teams - {title} (bundle).md"


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


def _meeting_identity_key(meeting: OutlookMeetingCandidate) -> str:
    teams_meeting_id = meeting.teams_meeting_id() or "no-teams-id"
    return f"{meeting.event_id}|{teams_meeting_id}"


def _normalized_bundle_title(value: str) -> str:
    title = normalize_whitespace(value)
    title = title.replace("/", "-").replace(":", "-").replace("\\", "-")
    return title or "Meeting"
