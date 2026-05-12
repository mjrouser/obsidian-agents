from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

FRONT_MATTER_KEYS = [
    "date",
    "source",
    "title",
    "participants",
    "organizer",
    "attendees",
    "owner_filter",
    "intake_file",
    "outlook_event_id",
    "teams_meeting_id",
    "transcript_id",
]


def format_obsidian_link(path: Path | str) -> str:
    target = path.as_posix() if isinstance(path, Path) else str(path).strip().replace("\\", "/")
    if target.startswith("[[") and target.endswith("]]"):
        return target
    return f"[[{target}]]"


def render_extracted_meeting_note(
    *,
    heading: str,
    intake_file: Path | str,
    extracted: dict,
    context: dict | None = None,
) -> str:
    context = context or {}
    participants = _string_list(extracted.get("participants", [])) or _string_list(context.get("participants", []))
    sections = [
        _render_text_section("Context", extracted.get("context", "")),
        _render_list_section("Participants", participants),
        _render_list_section("Summary", extracted.get("summary_bullets", [])),
        _render_list_section("Key Points", extracted.get("key_points", [])),
        _render_list_section("Decisions", extracted.get("decisions", [])),
        _render_list_section("Decision Signals", extracted.get("decision_signals", [])),
        _render_list_section("Risks", extracted.get("risks", [])),
        _render_list_section("Assumptions", extracted.get("assumptions", [])),
        _render_list_section("Open Questions", extracted.get("open_questions", [])),
        _render_action_section(extracted.get("action_items", [])),
        _render_list_section("Signals And Tensions", extracted.get("signals_and_tensions", [])),
        _render_list_section("Alignment Path", extracted.get("alignment_path", [])),
        _render_list_section("Related Initiatives", extracted.get("related_initiatives", [])),
        _render_list_section("Related Themes", extracted.get("related_themes", [])),
        _render_source_section(
            sources_used=extracted.get("sources_used", []),
            source_limitations=extracted.get("source_limitations", []),
            attendance_confidence=extracted.get("attendance_confidence", ""),
        ),
    ]
    verbatim_excerpt = str(extracted.get("verbatim_excerpt", "")).strip()
    body = "\n\n".join(section for section in sections if section)
    excerpt = ""
    if verbatim_excerpt:
        excerpt = f"\n\n## Verbatim Excerpt\n\n> {verbatim_excerpt}"

    front_matter = render_meeting_front_matter(
        date=str(extracted.get("date", "")),
        source=str(extracted.get("source", "")),
        title=str(extracted.get("title", "")),
        intake_file=intake_file,
        participants=participants,
        organizer=_optional_string(context.get("organizer")),
        attendees=_string_list(context.get("attendees", [])),
        outlook_event_id=_optional_string(context.get("outlook_event_id")),
        teams_meeting_id=_optional_string(context.get("teams_meeting_id")),
        transcript_id=_optional_string(context.get("transcript_id")),
    )
    return (
        f"{front_matter}"
        f"# {heading}\n\n"
        f"- Date: {extracted.get('date', '')}\n"
        f"- Source: {extracted.get('source', '')}\n"
        f"- Title: {extracted.get('title', '')}\n"
        f"- Intake File: {format_obsidian_link(intake_file)}\n\n"
        f"{body}{excerpt}\n"
    )


def render_meeting_note(
    *,
    heading: str,
    intake_file: Path | str,
    owner_filter: str,
    normalized_body: str,
    meeting_date: str,
    meeting_source: str,
    meeting_title: str,
    context: dict | None = None,
) -> str:
    context = context or {}
    front_matter = render_meeting_front_matter(
        date=meeting_date,
        source=meeting_source,
        title=meeting_title,
        intake_file=intake_file,
        participants=_string_list(context.get("participants", [])),
        organizer=_optional_string(context.get("organizer")),
        attendees=_string_list(context.get("attendees", [])),
        owner_filter=owner_filter,
        outlook_event_id=_optional_string(context.get("outlook_event_id")),
        teams_meeting_id=_optional_string(context.get("teams_meeting_id")),
        transcript_id=_optional_string(context.get("transcript_id")),
    )
    return (
        f"{front_matter}"
        f"# {heading}\n\n"
        f"- Date: {meeting_date}\n"
        f"- Source: {meeting_source}\n"
        f"- Title: {meeting_title}\n"
        f"- Intake File: {format_obsidian_link(intake_file)}\n"
        f"- Owner Filter: {owner_filter}\n\n"
        "## Transcript\n\n"
        f"{normalized_body}\n"
    )


def render_meeting_front_matter(
    *,
    date: str,
    source: str,
    title: str,
    intake_file: Path | str,
    participants: list[str] | None = None,
    organizer: str | None = None,
    attendees: list[str] | None = None,
    owner_filter: str | None = None,
    outlook_event_id: str | None = None,
    teams_meeting_id: str | None = None,
    transcript_id: str | None = None,
) -> str:
    values: dict[str, object] = {
        "date": date,
        "source": source,
        "title": title,
        "participants": participants or [],
        "organizer": organizer,
        "attendees": attendees or [],
        "owner_filter": owner_filter,
        "intake_file": _path_value(intake_file),
        "outlook_event_id": outlook_event_id,
        "teams_meeting_id": teams_meeting_id,
        "transcript_id": transcript_id,
    }
    lines = ["---"]
    for key in FRONT_MATTER_KEYS:
        lines.append(f"{key}: {_yaml_value(values[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def render_vtt_intake_sidecar(
    *,
    raw_relative_path: str,
    canonical_note_name: str,
    verbatim_excerpt: str,
    transcript_hash: str | None = None,
) -> str:
    parts = [
        f"STATUS: PROCESSED — see [[{canonical_note_name}]]",
        f"- Raw VTT: [{raw_relative_path}]({raw_relative_path})",
    ]
    if transcript_hash:
        parts.append(f"- Transcript SHA256: {transcript_hash}")
    excerpt = verbatim_excerpt.strip()
    if excerpt:
        parts.append(f'- Verbatim excerpt: "{excerpt}"')
    return "\n".join(parts) + "\n"


def _path_value(path: Path | str) -> str:
    return path.as_posix() if isinstance(path, Path) else str(path).strip().replace("\\", "/")


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def _yaml_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        if not value:
            return "[]"
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _render_list_section(title: str, items: Sequence[object]) -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    if not values:
        return ""
    lines = "\n".join(f"- {value}" for value in values)
    return f"## {title}\n\n{lines}"


def _render_text_section(title: str, value: object) -> str:
    text = str(value).strip()
    if not text:
        return ""
    return f"## {title}\n\n{text}"


def _render_source_section(
    *,
    sources_used: object,
    source_limitations: object,
    attendance_confidence: object,
) -> str:
    lines: list[str] = []
    confidence = str(attendance_confidence).strip()
    if confidence:
        lines.append(f"- Attendance Confidence: {confidence}")
    for source in _string_list(sources_used):
        lines.append(f"- Source Used: {source}")
    for limitation in _string_list(source_limitations):
        lines.append(f"- Limitation: {limitation}")
    if not lines:
        return ""
    return "## Source\n\n" + "\n".join(lines)


def _render_action_section(items: list[object]) -> str:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        owner = item.get("owner")
        due = item.get("due")
        suffix_parts = []
        if owner:
            suffix_parts.append(f"Owner: {owner}")
        if due:
            suffix_parts.append(f"Due: {due}")
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        lines.append(f"- [ ] {text}{suffix}")
    if not lines:
        return ""
    return "## Action Items\n\n" + "\n".join(lines)
