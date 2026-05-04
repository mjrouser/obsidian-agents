from __future__ import annotations

import re

from ..config import Config
from ..llm.codex_extractor import run_codex_json
from ..llm.prompts import build_meeting_extraction_prompt
from .md_reader import OWNER_WILL_PATTERN, ActionItem, parse_action_text
from .meeting_metadata import MeetingMetadata


def extract_vtt_meeting_data(*, transcript_text: str, metadata: MeetingMetadata, config: Config) -> dict:
    if config.llm_provider == "codex_cli":
        prompt = build_meeting_extraction_prompt(
            transcript_text=transcript_text,
            meeting_date=metadata.date,
            source=metadata.source,
            title=metadata.title,
        )
        return run_codex_json(
            prompt,
            config.codex_model,
            exec_cmd=config.codex_exec_cmd,
            timeout_seconds=config.codex_timeout_seconds,
        )
    return heuristic_extract_meeting_data(transcript_text, metadata)


def heuristic_extract_meeting_data(transcript_text: str, metadata: MeetingMetadata) -> dict:
    key_points: list[str] = []
    decisions: list[str] = []
    risks: list[str] = []
    open_questions: list[str] = []
    action_items: list[dict[str, str | None]] = []
    for raw_line in transcript_text.splitlines():
        line = raw_line.strip()
        lowered = line.casefold()
        if not line:
            continue
        if lowered.startswith("action:"):
            action = _parse_heuristic_action(line.split(":", 1)[1].strip())
            action_items.append(action)
        elif OWNER_WILL_PATTERN.match(line):
            parsed = parse_action_text(line)
            action_items.append(
                {
                    "text": parsed.text,
                    "owner": parsed.owner,
                    "due": parsed.due,
                }
            )
        elif lowered.startswith("decision:"):
            decisions.append(line.split(":", 1)[1].strip())
        elif lowered.startswith("risk:"):
            risks.append(line.split(":", 1)[1].strip())
        elif lowered.startswith("question:"):
            open_questions.append(line.split(":", 1)[1].strip())
        else:
            key_points.append(line)
    return {
        "title": metadata.title,
        "date": metadata.date,
        "source": metadata.source,
        "context": "",
        "participants": [],
        "attendance_confidence": "unknown",
        "summary_bullets": key_points[:3],
        "key_points": key_points,
        "decisions": decisions,
        "decision_signals": [],
        "risks": risks,
        "assumptions": [],
        "open_questions": open_questions,
        "action_items": action_items,
        "signals_and_tensions": [],
        "alignment_path": [],
        "related_initiatives": [],
        "related_themes": [],
        "sources_used": ["VTT transcript"],
        "source_limitations": ["Heuristic extraction used; no calendar, chat, or recap context was available."],
        "verbatim_excerpt": key_points[0] if key_points else "",
    }


def normalize_extracted_meeting_data(extracted: dict, metadata: MeetingMetadata) -> dict:
    return {
        "title": str(extracted.get("title") or metadata.title),
        "date": str(extracted.get("date") or metadata.date),
        "source": str(extracted.get("source") or metadata.source),
        "context": str(extracted.get("context") or ""),
        "participants": list(extracted.get("participants") or []),
        "attendance_confidence": str(extracted.get("attendance_confidence") or "unknown"),
        "summary_bullets": list(extracted.get("summary_bullets") or []),
        "key_points": list(extracted.get("key_points") or []),
        "decisions": list(extracted.get("decisions") or []),
        "decision_signals": list(extracted.get("decision_signals") or []),
        "risks": list(extracted.get("risks") or []),
        "assumptions": list(extracted.get("assumptions") or []),
        "open_questions": list(extracted.get("open_questions") or []),
        "action_items": list(extracted.get("action_items") or []),
        "signals_and_tensions": list(extracted.get("signals_and_tensions") or []),
        "alignment_path": list(extracted.get("alignment_path") or []),
        "related_initiatives": list(extracted.get("related_initiatives") or []),
        "related_themes": list(extracted.get("related_themes") or []),
        "sources_used": list(extracted.get("sources_used") or []),
        "source_limitations": list(extracted.get("source_limitations") or []),
        "verbatim_excerpt": str(extracted.get("verbatim_excerpt") or ""),
    }


def action_items_from_extracted(extracted: dict) -> list[ActionItem]:
    items: list[ActionItem] = []
    for raw in extracted.get("action_items", []):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        owner = raw.get("owner")
        due = raw.get("due")
        items.append(
            ActionItem(
                owner=str(owner).strip() if owner else None,
                text=text,
                due=str(due).strip() if due else None,
            )
        )
    return items


def _parse_heuristic_action(text: str) -> dict[str, str | None]:
    match = re.match(r"^(?P<owner>[^.]+?)\s+will\s+(?P<action>.+)$", text, re.IGNORECASE)
    if match:
        return {
            "text": match.group("action").strip(),
            "owner": match.group("owner").strip(),
            "due": None,
        }
    return {"text": text.strip(), "owner": None, "due": None}
