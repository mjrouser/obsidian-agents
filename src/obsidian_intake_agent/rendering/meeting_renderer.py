from __future__ import annotations

from pathlib import Path


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
