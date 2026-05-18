from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from obsidian_intake_agent.config import Config
from obsidian_intake_agent.rendering.web_clip_renderer import render_processed_web_clip_note
from obsidian_intake_agent.utils.fs import safe_write_text
from obsidian_intake_agent.web_clips.models import ProcessedWebClip


@dataclass(frozen=True, slots=True)
class WebClipProcessingSummary:
    processed_files: int = 0
    written_reference_notes: int = 0
    archived_raw_captures: int = 0


class WebClipProcessor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.vault_path = config.vault_path
        self.intake_path = self.vault_path / config.web_clips_intake_dir
        self.references_path = self.vault_path / config.web_clips_references_dir
        self.archive_path = self.vault_path / config.archive_intake_dir / "Web Clips"

    def process_all(self, dry_run: bool | None = None) -> WebClipProcessingSummary:
        effective_dry_run = self.config.dry_run if dry_run is None else dry_run
        self.intake_path = _validate_vault_destination(self.vault_path, self.intake_path)
        if not self.intake_path.exists():
            return WebClipProcessingSummary()

        processed_files = 0
        written_reference_notes = 0
        archived_raw_captures = 0
        for raw_path in sorted(self.intake_path.glob("*.md")):
            raw_text = raw_path.read_text(encoding="utf-8")
            parsed = _parse_raw_note(raw_text)
            if parsed is None:
                continue

            reference_path = _unique_path(
                _validate_vault_destination(self.vault_path, self.references_path / raw_path.name)
            )
            archive_path = _unique_path(_validate_vault_destination(self.vault_path, self.archive_path / raw_path.name))
            processed_files += 1
            if effective_dry_run:
                print(f"DRY RUN: would write web clip reference note: {reference_path}")
                print(f"DRY RUN: would archive web clip intake note: {archive_path}")
                continue

            processed = _build_processed_clip(parsed, _vault_relative_path(self.vault_path, archive_path))
            safe_write_text(reference_path, render_processed_web_clip_note(processed))
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.replace(archive_path)
            written_reference_notes += 1
            archived_raw_captures += 1

        return WebClipProcessingSummary(
            processed_files=processed_files,
            written_reference_notes=written_reference_notes,
            archived_raw_captures=archived_raw_captures,
        )


@dataclass(frozen=True, slots=True)
class _ParsedRawWebClip:
    source_url: str
    source_title: str
    captured_at: str
    why: str
    passages: list[str]


def _parse_raw_note(raw_text: str) -> _ParsedRawWebClip | None:
    metadata, body = _split_frontmatter(raw_text)
    if metadata.get("type") != "web_clip_intake" or metadata.get("status") != "unprocessed":
        return None

    source_url = str(metadata.get("source_url", "")).strip()
    source_title = str(metadata.get("source_title", "")).strip()
    captured_at = str(metadata.get("captured_at", "")).strip()
    why = _extract_section(body, "Why This Matters").strip()
    passages = _extract_blockquotes(_extract_section(body, "Captured Passages"))
    if not source_url or not source_title or not captured_at or not why or not passages:
        return None

    return _ParsedRawWebClip(
        source_url=source_url,
        source_title=source_title,
        captured_at=captured_at,
        why=why,
        passages=passages,
    )


def _split_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    if not raw_text.startswith("---\n"):
        return {}, raw_text
    _, frontmatter, body = raw_text.split("---\n", 2)
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(frontmatter) or {}
        if isinstance(loaded, dict):
            return loaded, body
    except ModuleNotFoundError:
        pass
    return _parse_simple_frontmatter(frontmatter), body


def _parse_simple_frontmatter(frontmatter: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        metadata[key.strip()] = value.strip().strip('"')
    return metadata


def _extract_section(body: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(body)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^##\s+", body[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(body)
    return body[start:end].strip()


def _extract_blockquotes(section: str) -> list[str]:
    passages: list[str] = []
    current: list[str] = []
    for line in section.splitlines():
        if line.startswith(">"):
            current.append(line[1:].removeprefix(" "))
        elif current:
            passages.append("\n".join(current).strip())
            current = []
    if current:
        passages.append("\n".join(current).strip())
    return [passage for passage in passages if passage]


def _build_processed_clip(parsed: _ParsedRawWebClip, intake_source: str) -> ProcessedWebClip:
    summary = parsed.passages[0] if parsed.passages else parsed.why
    return ProcessedWebClip(
        source_url=parsed.source_url,
        source_title=parsed.source_title,
        captured_at=parsed.captured_at,
        why=parsed.why,
        summary=summary,
        passages=parsed.passages,
        topics=_topics_from_text(" ".join([parsed.source_title, parsed.why, *parsed.passages])),
        application=parsed.why or summary,
        related=[],
        intake_source=intake_source,
    )


def _topics_from_text(text: str) -> list[str]:
    stopwords = {
        "about",
        "after",
        "article",
        "captured",
        "exact",
        "from",
        "into",
        "passage",
        "source",
        "that",
        "this",
        "with",
    }
    topics: list[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text.lower()):
        if word in stopwords or word in topics:
            continue
        topics.append(word)
        if len(topics) == 5:
            break
    return topics


def _vault_relative_path(vault_path: Path, path: Path) -> str:
    return _validate_vault_destination(vault_path, path).relative_to(vault_path.resolve()).as_posix()


def _validate_vault_destination(vault_path: Path, path: Path) -> Path:
    resolved_vault = vault_path.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_vault and resolved_vault not in resolved_path.parents:
        raise ValueError(f"web clip destination is outside the vault: {path}")
    return resolved_path


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = path.with_name(f"{stem} {counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
