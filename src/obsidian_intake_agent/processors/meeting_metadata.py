from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..utils.text import normalize_whitespace

LEADING_DATE_PATTERN = re.compile(r"^(?P<year>\d{4})[-=](?P<month>\d{2})[-=](?P<day>\d{2})(?:\s*-\s*)?(?P<rest>.*)$")
SOURCE_PATTERN = re.compile(r"^(?P<source>Teams|Copilot)\s*-\s*(?P<title>.+)$")


@dataclass(slots=True)
class MeetingMetadata:
    date: str
    source: str
    title: str
    canonical_basename: str
    date_from_filename: bool = True


def normalize_meeting_metadata(intake_path: Path) -> MeetingMetadata:
    basename = intake_path.stem
    match = LEADING_DATE_PATTERN.match(basename)
    if match:
        meeting_date = f"{match.group('year')}-{match.group('month')}-{match.group('day')}"
        remainder = match.group("rest")
        date_from_filename = True
    else:
        meeting_date = date.fromtimestamp(intake_path.stat().st_mtime).isoformat()
        remainder = basename
        date_from_filename = False

    source_match = SOURCE_PATTERN.match(remainder)
    if source_match:
        source = source_match.group("source")
        title = source_match.group("title")
    else:
        source = "Unknown"
        title = remainder

    title = normalize_whitespace(title)
    if not title:
        title = "Meeting"

    return MeetingMetadata(
        date=meeting_date,
        source=source,
        title=title,
        canonical_basename=f"{meeting_date} - {source} - {title}.md",
        date_from_filename=date_from_filename,
    )
