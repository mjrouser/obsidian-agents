from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..utils.text import normalize_whitespace

MEETING_MONTH_NAMES = (
    "",
    "01_January",
    "02_February",
    "03_March",
    "04_April",
    "05_May",
    "06_June",
    "07_July",
    "08_August",
    "09_September",
    "10_October",
    "11_November",
    "12_December",
)

LEGACY_MEETING_MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

LEADING_DATE_PATTERN = re.compile(r"^(?P<year>\d{4})[-=](?P<month>\d{2})[-=](?P<day>\d{2})(?:\s*-\s*)?(?P<rest>.*)$")
SOURCE_PATTERN = re.compile(r"^(?P<source>Teams|Copilot)\s*-\s*(?P<title>.+)$")


@dataclass(slots=True)
class MeetingMetadata:
    date: str
    source: str
    title: str
    canonical_basename: str
    date_from_filename: bool = True


def meeting_output_path(meetings_root: Path, *, meeting_date: str, basename: str) -> Path:
    parsed_date = date.fromisoformat(meeting_date)
    if not basename or Path(basename).name != basename:
        raise ValueError("meeting basename must be a non-empty filename")
    return meetings_root / str(parsed_date.year) / MEETING_MONTH_NAMES[parsed_date.month] / basename


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
