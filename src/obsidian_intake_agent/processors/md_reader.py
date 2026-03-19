from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


OWNER_TOKEN = r"[A-Za-z][A-Za-z.'-]*"
OWNER_NAME_PATTERN = rf"{OWNER_TOKEN}(?:\s+{OWNER_TOKEN}){{0,2}}"
COORDINATED_OWNER_PATTERN = rf"{OWNER_NAME_PATTERN}\s+and\s+{OWNER_NAME_PATTERN}"
OWNER_WILL_PATTERN = re.compile(rf"^(?P<owner>{OWNER_NAME_PATTERN})\s+will\s+(?P<text>.+)$", re.IGNORECASE)
OWNER_TO_PATTERN = re.compile(rf"^(?P<owner>{OWNER_NAME_PATTERN})\s+to\s+(?P<text>.+)$", re.IGNORECASE)
COORDINATED_OWNER_TO_PATTERN = re.compile(rf"^(?P<owner>{COORDINATED_OWNER_PATTERN})\s+to\s+(?P<text>.+)$", re.IGNORECASE)
TRAILING_OWNER_WILL_PATTERN = re.compile(
    rf"(?:^|.*[;:]\s+)(?P<owner>{OWNER_NAME_PATTERN})\s+will\s+(?P<text>.+)$",
    re.IGNORECASE,
)
OWNER_LABEL_PATTERN = re.compile(
    rf"^(?P<owner>{OWNER_NAME_PATTERN})\s*(?::|[-—])\s*(?P<text>.+)$",
    re.IGNORECASE,
)
ASSIGNED_TO_PATTERN = re.compile(
    rf"^Assigned\s+to\s+(?P<owner>{OWNER_NAME_PATTERN})\s*:\s*(?P<text>.+)$",
    re.IGNORECASE,
)
OWNER_ASSIGNMENT_PATTERN = re.compile(
    rf"^Owner:\s*(?P<owner>{OWNER_NAME_PATTERN})\s*(?::|[-—])\s*(?P<text>.+)$",
    re.IGNORECASE,
)
ACTION_SECTION_PATTERN = re.compile(r"#+\s+.*(?:action items?|next steps?)", re.IGNORECASE)
BULLET_PATTERN = re.compile(r"^-\s+(?!\[[ xX]\]\s*)(?P<text>.+)$")
TASK_OWNER_PATTERN = re.compile(rf"^(?P<text>.+?)\s*\((?P<owner>{OWNER_NAME_PATTERN})\)$")
DISALLOWED_OWNER_TOKENS = {
    "notes",
    "context",
    "summary",
    "agenda",
    "decision",
    "risk",
    "question",
    "questions",
    "action",
    "actions",
    "owner",
    "assigned",
}


@dataclass(frozen=True, slots=True)
class ActionItem:
    owner: str | None
    text: str
    due: str | None = None


def read_markdown(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_markdown_action_items(text: str) -> list[ActionItem]:
    items: list[ActionItem] = []
    in_action_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            in_action_section = ACTION_SECTION_PATTERN.match(line) is not None
            continue
        if line.lower().startswith("action:"):
            action_text = line[len("action:") :].strip()
            items.append(parse_action_text(action_text))
            continue
        if line.startswith("- [ ]"):
            action_text = line[len("- [ ]") :].strip()
            items.append(parse_action_text(action_text))
            continue
        if line.startswith("- "):
            action_text = line[len("- ") :].strip()
            parsed_bullet = parse_action_text(action_text)
            if parsed_bullet.owner is not None:
                items.append(parsed_bullet)
                continue
        parsed_standalone = parse_action_text(line)
        if parsed_standalone.owner is not None and not line.startswith("-"):
            items.append(parsed_standalone)
            continue
        if in_action_section:
            match = BULLET_PATTERN.match(line)
            if match is None:
                continue
            action_text = match.group("text").strip()
            parsed = parse_action_text(action_text)
            if parsed.owner is not None:
                items.append(parsed)
                continue
    return items


def parse_action_text(action_text: str) -> ActionItem:
    for pattern in (
        OWNER_WILL_PATTERN,
        COORDINATED_OWNER_TO_PATTERN,
        OWNER_TO_PATTERN,
        TRAILING_OWNER_WILL_PATTERN,
        ASSIGNED_TO_PATTERN,
        OWNER_ASSIGNMENT_PATTERN,
        OWNER_LABEL_PATTERN,
    ):
        match = pattern.match(action_text)
        if match and _is_owner_like(match.group("owner")):
            return ActionItem(
                owner=match.group("owner").strip(),
                text=match.group("text").strip(),
            )

    owner_match = TASK_OWNER_PATTERN.match(action_text)
    if owner_match and _is_owner_like(owner_match.group("owner")):
        return ActionItem(
            owner=owner_match.group("owner").strip(),
            text=owner_match.group("text").strip(),
        )
    return ActionItem(owner=None, text=action_text.strip())


def _is_owner_like(value: str) -> bool:
    tokens = value.strip().split()
    if not tokens:
        return False
    filtered_tokens = [token for token in tokens if token.casefold() != "and"]
    if not filtered_tokens:
        return False
    if any(token.casefold() in DISALLOWED_OWNER_TOKENS for token in filtered_tokens):
        return False
    return all(token[0].isupper() for token in filtered_tokens)
