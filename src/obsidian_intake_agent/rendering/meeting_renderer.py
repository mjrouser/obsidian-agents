from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..utils.normalization import normalize_free_text, normalize_owner

SOURCE_PATTERN = re.compile(
    r"(?:—|-)\s*Source:\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\[\[(?P<note>[^\]]+)\]\]\.?\s*$",
    re.IGNORECASE,
)
OWNER_PAREN_PATTERN = re.compile(r"\(\s*Owner:\s*(?P<owner>[^)]+)\)", re.IGNORECASE)
OWNER_INLINE_PATTERN = re.compile(r"\bOwner:\s*(?P<owner>[^—-]+?)(?=(?:\s*[—-]\s*Source:|$))", re.IGNORECASE)
CHECKBOX_PATTERN = re.compile(r"^\s*-\s*\[(?: |x|X)\]\s*")
THIS_WEEK_HEADING = "## This Week"
LONGER_TERM_HEADING = "## Longer-Term / In Progress"
OPEN_ACTIONS_HEADING = "## Open Actions"


@dataclass(frozen=True, slots=True)
class ActionRecord:
    text: str
    owner: str | None
    source_date: str
    source_note: str
    raw_line: str | None = None


def format_obsidian_link(path: Path | str) -> str:
    if isinstance(path, Path):
        target = path.as_posix()
    else:
        target = str(path).strip().replace("\\", "/")
    if target.startswith("[[") and target.endswith("]]"):
        return target
    return f"[[{target}]]"


def render_extracted_meeting_note(
    *,
    heading: str,
    intake_file: Path | str,
    extracted: dict,
) -> str:
    sections = [
        _render_list_section("Participants", extracted.get("participants", [])),
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
    ]
    verbatim_excerpt = str(extracted.get("verbatim_excerpt", "")).strip()
    body = "\n\n".join(section for section in sections if section)
    excerpt = ""
    if verbatim_excerpt:
        excerpt = f"\n\n## Verbatim Excerpt\n\n> {verbatim_excerpt}"

    return (
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
) -> str:
    return (
        f"# {heading}\n\n"
        f"- Date: {meeting_date}\n"
        f"- Source: {meeting_source}\n"
        f"- Title: {meeting_title}\n"
        f"- Intake File: {format_obsidian_link(intake_file)}\n"
        f"- Owner Filter: {owner_filter}\n\n"
        "## Transcript\n\n"
        f"{normalized_body}\n"
    )


def render_actions_note(
    *,
    monday: date,
    action_records: list[ActionRecord],
    owner_aliases: dict[str, list[str]],
    existing_text: str = "",
) -> str:
    title = f"# Actions — Week of {monday.isoformat()}"
    unique_records: list[ActionRecord] = []
    seen_records = set()
    for record in action_records:
        key = normalize_action_for_key(record, owner_aliases)
        if key not in seen_records:
            unique_records.append(record)
            seen_records.add(key)

    existing_records = parse_existing_actions(existing_text, owner_aliases)
    existing_keys = {normalize_action_for_key(record, owner_aliases) for record in existing_records}
    pending_lines = [
        render_action_line(record, owner_aliases)
        for record in unique_records
        if normalize_action_for_key(record, owner_aliases) not in existing_keys
    ]

    if not existing_text.strip():
        if not pending_lines:
            return ""
        return _build_actions_note(
            title=title,
            this_week_lines=pending_lines,
            longer_term_lines=[],
            preamble_lines=[],
        )

    if not pending_lines:
        return existing_text

    return _append_pending_actions(
        existing_text=existing_text,
        title=title,
        pending_lines=pending_lines,
    )


def render_vtt_intake_sidecar(
    *,
    raw_relative_path: str,
    canonical_note_name: str,
    verbatim_excerpt: str,
) -> str:
    parts = [
        f"STATUS: PROCESSED — see [[{canonical_note_name}]]",
        f"- Raw VTT: [{raw_relative_path}]({raw_relative_path})",
    ]
    excerpt = verbatim_excerpt.strip()
    if excerpt:
        parts.append(f'- Verbatim excerpt: "{excerpt}"')
    return "\n".join(parts) + "\n"


def _render_list_section(title: str, items: list[object]) -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    if not values:
        return ""
    lines = "\n".join(f"- {value}" for value in values)
    return f"## {title}\n\n{lines}"


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


def parse_existing_actions(existing_text: str, owner_aliases: dict[str, list[str]]) -> list[ActionRecord]:
    records: list[ActionRecord] = []
    for raw_line in existing_text.splitlines():
        if not raw_line.strip().startswith("- ["):
            continue
        record = _parse_action_line(raw_line, owner_aliases)
        if record is not None:
            records.append(record)
    return records


def normalize_action_for_key(record: ActionRecord, owner_aliases: dict[str, list[str]]) -> str:
    owner = normalize_owner(record.owner, owner_aliases) or ""
    text = normalize_free_text(record.text)
    source = normalize_free_text(f"{record.source_date} [[{record.source_note}]]")
    return "|".join([text, owner.casefold(), source])


def render_action_line(record: ActionRecord, owner_aliases: dict[str, list[str]]) -> str:
    owner = normalize_owner(record.owner, owner_aliases)
    owner_suffix = f" (Owner: {owner})" if owner else ""
    source_suffix = f"— Source: {record.source_date} [[{record.source_note}]]"
    return f"- [ ] {record.text}{owner_suffix} {source_suffix}"


def _parse_action_line(raw_line: str, owner_aliases: dict[str, list[str]]) -> ActionRecord | None:
    source_match = SOURCE_PATTERN.search(raw_line.strip())
    if source_match is None:
        return None

    without_source = SOURCE_PATTERN.sub("", raw_line.strip()).strip()
    without_checkbox = CHECKBOX_PATTERN.sub("", without_source).strip()

    owner_match = OWNER_PAREN_PATTERN.search(without_checkbox)
    owner: str | None = None
    if owner_match is not None:
        owner = owner_match.group("owner").strip()
        text = OWNER_PAREN_PATTERN.sub("", without_checkbox).strip()
    else:
        inline_match = OWNER_INLINE_PATTERN.search(without_checkbox)
        if inline_match is not None:
            owner = inline_match.group("owner").strip()
            text = OWNER_INLINE_PATTERN.sub("", without_checkbox).strip(" -")
        else:
            text = without_checkbox

    return ActionRecord(
        text=text.strip(),
        owner=normalize_owner(owner, owner_aliases),
        source_date=source_match.group("date"),
        source_note=source_match.group("note").strip(),
        raw_line=raw_line,
    )


def _append_pending_actions(*, existing_text: str, title: str, pending_lines: list[str]) -> str:
    lines = existing_text.splitlines()
    this_week_index = _find_heading_index(lines, THIS_WEEK_HEADING)
    longer_term_index = _find_heading_index(lines, LONGER_TERM_HEADING)

    if this_week_index is not None and longer_term_index is not None and this_week_index < longer_term_index:
        insert_at = longer_term_index
        while insert_at > this_week_index + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        updated_lines = lines[:insert_at] + pending_lines + [""] + lines[longer_term_index:]
        return "\n".join(updated_lines).rstrip() + "\n"

    preamble_lines, this_week_lines, longer_term_lines = _extract_actions_note_parts(existing_text)
    this_week_lines.extend(pending_lines)
    return _build_actions_note(
        title=title,
        this_week_lines=this_week_lines,
        longer_term_lines=longer_term_lines,
        preamble_lines=preamble_lines,
    )


def _extract_actions_note_parts(existing_text: str) -> tuple[list[str], list[str], list[str]]:
    lines = existing_text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]

    this_week_index = _find_heading_index(lines, THIS_WEEK_HEADING)
    longer_term_index = _find_heading_index(lines, LONGER_TERM_HEADING)
    open_actions_index = _find_heading_index(lines, OPEN_ACTIONS_HEADING)

    if this_week_index is not None and longer_term_index is not None and this_week_index < longer_term_index:
        preamble_lines = _strip_edge_blank_lines(lines[:this_week_index])
        this_week_lines = _strip_edge_blank_lines(lines[this_week_index + 1 : longer_term_index])
        longer_term_lines = _strip_edge_blank_lines(lines[longer_term_index + 1 :])
        return preamble_lines, this_week_lines, longer_term_lines

    longer_term_lines: list[str] = []
    body_lines = lines
    if longer_term_index is not None:
        longer_term_lines = _strip_edge_blank_lines(lines[longer_term_index + 1 :])
        body_lines = lines[:longer_term_index]

    if open_actions_index is not None:
        body_lines = lines[:open_actions_index] + lines[open_actions_index + 1 :]

    preamble_lines: list[str] = []
    this_week_lines: list[str] = []
    for line in body_lines:
        if line.strip().startswith("- ["):
            this_week_lines.append(line)
        elif line.strip() not in {THIS_WEEK_HEADING, LONGER_TERM_HEADING, OPEN_ACTIONS_HEADING}:
            preamble_lines.append(line)
    return _strip_edge_blank_lines(preamble_lines), _strip_edge_blank_lines(this_week_lines), longer_term_lines


def _build_actions_note(
    *,
    title: str,
    this_week_lines: list[str],
    longer_term_lines: list[str],
    preamble_lines: list[str],
) -> str:
    parts = [title]
    if preamble_lines:
        parts.append("\n".join(_strip_edge_blank_lines(preamble_lines)))
    parts.append(THIS_WEEK_HEADING)
    if this_week_lines:
        parts.append("\n".join(_strip_edge_blank_lines(this_week_lines)))
    parts.append(LONGER_TERM_HEADING)
    if longer_term_lines:
        parts.append("\n".join(_strip_edge_blank_lines(longer_term_lines)))
    return "\n\n".join(part for part in parts if part is not None).rstrip() + "\n"


def _find_heading_index(lines: list[str], heading: str) -> int | None:
    for index, line in enumerate(lines):
        if line.strip() == heading:
            return index
    return None


def _strip_edge_blank_lines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]
