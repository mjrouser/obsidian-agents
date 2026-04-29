from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..utils.text import normalize_whitespace

LEADING_DATE_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?: - )?(?P<rest>.*)$")


@dataclass(slots=True)
class MeetingMetadata:
    date: str
    source: str
    title: str
    canonical_basename: str


def normalize_meeting_metadata(intake_path: Path) -> MeetingMetadata:
    basename = intake_path.stem
    match = LEADING_DATE_PATTERN.match(basename)
    if match:
        meeting_date = match.group("date")
        remainder = match.group("rest")
    else:
        meeting_date = date.fromtimestamp(intake_path.stat().st_mtime).isoformat()
        remainder = basename

    if " - Teams - " in basename:
        source = "Teams"
        title = basename.split(" - Teams - ", 1)[1]
    elif " - Copilot - " in basename:
        source = "Copilot"
        title = basename.split(" - Copilot - ", 1)[1]
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
    )
