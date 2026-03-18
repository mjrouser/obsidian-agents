from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


OWNER_WILL_PATTERN = re.compile(r"^(?P<owner>[^.]+?)\s+will\s+(?P<text>.+)$", re.IGNORECASE)
ACTION_SECTION_PATTERN = re.compile(r"#+\s+.*(?:action items?|next steps?)", re.IGNORECASE)
BULLET_PATTERN = re.compile(r"^-\s+(?!\[[ xX]\]\s*)(?P<text>.+)$")


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
        if in_action_section:
            match = BULLET_PATTERN.match(line)
            if match is None:
                continue
            action_text = match.group("text").strip()
            if OWNER_WILL_PATTERN.match(action_text):
                items.append(parse_action_text(action_text))
    return items


def parse_action_text(action_text: str) -> ActionItem:
    match = OWNER_WILL_PATTERN.match(action_text)
    if match:
        return ActionItem(
            owner=match.group("owner").strip(),
            text=match.group("text").strip(),
        )
    return ActionItem(owner=None, text=action_text.strip())
