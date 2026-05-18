from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from obsidian_intake_agent.config import Config
from obsidian_intake_agent.rendering.web_clip_renderer import render_processed_web_clip_note
from obsidian_intake_agent.utils.fs import safe_write_text
from obsidian_intake_agent.web_clips.models import ProcessedWebClip
from obsidian_intake_agent.web_clips.paths import validate_vault_path, vault_relative_path


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
        self.intake_path = validate_vault_path(self.vault_path, self.intake_path, label="web clip intake directory")
        if not self.intake_path.exists():
            return WebClipProcessingSummary()

        processed_files = 0
        written_reference_notes = 0
        archived_raw_captures = 0
        for raw_path in sorted(self.intake_path.glob("*.md")):
            if raw_path.is_symlink():
                continue
            try:
                raw_path = validate_vault_path(self.vault_path, raw_path, label="web clip intake note")
            except ValueError:
                continue
            raw_text = raw_path.read_text(encoding="utf-8")
            parsed = _parse_raw_note(raw_text)
            if parsed is None:
                continue

            base_reference_path = validate_vault_path(
                self.vault_path, self.references_path / raw_path.name, label="web clip reference note"
            )
            base_archive_path = validate_vault_path(
                self.vault_path, self.archive_path / raw_path.name, label="web clip archive note"
            )
            processed_files += 1

            recoverable_archive_path = _recoverable_archive_path_for_existing_reference(
                vault_path=self.vault_path,
                references_path=self.references_path,
                archive_path=self.archive_path,
                raw_name=raw_path.name,
            )
            if not effective_dry_run and recoverable_archive_path is not None:
                recoverable_archive_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.replace(recoverable_archive_path)
                archived_raw_captures += 1
                continue

            reference_path = _unique_path(base_reference_path)
            archive_path = _unique_path(base_archive_path)
            if effective_dry_run:
                print(f"DRY RUN: would write web clip reference note: {reference_path}")
                print(f"DRY RUN: would archive web clip intake note: {archive_path}")
                continue

            processed = _build_processed_clip(parsed, vault_relative_path(self.vault_path, archive_path))
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
    parsed = _split_frontmatter(raw_text)
    if parsed is None:
        return None
    metadata, body = parsed
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


def _split_frontmatter(raw_text: str) -> tuple[dict[str, Any], str] | None:
    if not raw_text.startswith("---\n"):
        return {}, raw_text
    try:
        _, frontmatter, body = raw_text.split("---\n", 2)
    except ValueError:
        return None
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(frontmatter) or {}
        if isinstance(loaded, dict):
            return loaded, body
    except ModuleNotFoundError:
        pass
    except Exception:
        return None
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


def _recoverable_archive_path_for_existing_reference(
    *,
    vault_path: Path,
    references_path: Path,
    archive_path: Path,
    raw_name: str,
) -> Path | None:
    reference_name_pattern = _unique_name_pattern(raw_name)
    archive_root = validate_vault_path(vault_path, archive_path, label="web clip archive directory")
    for reference_path in sorted(references_path.glob("*.md")):
        if reference_path.is_symlink() or not reference_name_pattern.match(reference_path.name):
            continue
        try:
            reference_path = validate_vault_path(vault_path, reference_path, label="web clip reference note")
        except ValueError:
            continue
        for link_target in _wikilink_targets(reference_path.read_text(encoding="utf-8")):
            try:
                archive_target = validate_vault_path(
                    vault_path, vault_path / link_target, label="web clip archive note"
                )
            except ValueError:
                continue
            if (
                archive_target.parent == archive_root
                and archive_target.name == reference_path.name
                and not archive_target.exists()
            ):
                return archive_target
    return None


def _unique_name_pattern(name: str) -> re.Pattern[str]:
    path = Path(name)
    return re.compile(rf"^{re.escape(path.stem)}(?: \d+)?{re.escape(path.suffix)}$")


def _wikilink_targets(text: str) -> list[str]:
    return [match.strip() for match in re.findall(r"\[\[([^]|#]+)", text)]
