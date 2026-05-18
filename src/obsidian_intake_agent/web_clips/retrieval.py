from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from obsidian_intake_agent.config import Config


@dataclass(frozen=True, slots=True)
class RelevantWebClip:
    path: Path
    title: str
    score: int
    reason: str


STOP_WORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "for",
    "from",
    "has",
    "have",
    "includes",
    "into",
    "its",
    "not",
    "our",
    "plan",
    "planning",
    "saved",
    "source",
    "summary",
    "that",
    "the",
    "this",
    "use",
    "week",
    "with",
}


def find_relevant_web_clips(config: Config, query_text: str) -> list[RelevantWebClip]:
    root = _references_root(config)
    query_terms = _terms(query_text)
    if not root.exists() or not query_terms:
        return []

    results: list[RelevantWebClip] = []
    for path in sorted(root.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text)
        title = _clip_title(path, metadata, body)
        topics = _list_metadata(metadata.get("topics"))
        searchable_terms = _terms(" ".join([title, *topics, body]))
        matches = sorted(query_terms & searchable_terms)
        if not matches:
            continue
        results.append(
            RelevantWebClip(
                path=path,
                title=title,
                score=len(matches),
                reason=f"matched terms: {', '.join(matches)}",
            )
        )

    return sorted(results, key=lambda clip: (-clip.score, clip.title.lower(), clip.path.as_posix()))[
        : config.web_clips_max_weekly_results
    ]


def render_relevant_web_clips_source(config: Config, query_text: str) -> str:
    clips = find_relevant_web_clips(config, query_text)
    if not clips:
        return ""

    lines = ["## Source: Relevant Saved Clips", ""]
    for clip in clips:
        relative_path = _vault_relative_path(config.vault_path, clip.path)
        lines.append(f"- [[{relative_path}]] — {clip.reason}")
    return "\n".join(lines)


def _references_root(config: Config) -> Path:
    return _validate_vault_path(
        config.vault_path,
        config.vault_path / config.web_clips_references_dir,
        label="web clip references directory",
    )


def _vault_relative_path(vault_path: Path, path: Path) -> str:
    return _validate_vault_path(vault_path, path, label="web clip path").relative_to(vault_path.resolve()).as_posix()


def _validate_vault_path(vault_path: Path, path: Path, *, label: str) -> Path:
    resolved_vault = vault_path.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_vault and resolved_vault not in resolved_path.parents:
        raise ValueError(f"{label} is outside the vault: {path}")
    return resolved_path


def _terms(text: str) -> set[str]:
    return {term for term in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text.lower()) if term not in STOP_WORDS}


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    try:
        _, frontmatter, body = text.split("---\n", 2)
    except ValueError:
        return {}, text
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(frontmatter) or {}
        if isinstance(loaded, dict):
            return loaded, body
    except ModuleNotFoundError:
        pass
    return _parse_simple_frontmatter(frontmatter), body


def _parse_simple_frontmatter(frontmatter: str) -> dict[str, str | list[str]]:
    metadata: dict[str, str | list[str]] = {}
    current_list_key: str | None = None
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if current_list_key and stripped.startswith("- "):
            existing = metadata.setdefault(current_list_key, [])
            if isinstance(existing, list):
                existing.append(stripped.removeprefix("- ").strip().strip('"'))
            continue
        key, separator, value = line.partition(":")
        if not separator:
            current_list_key = None
            continue
        current_list_key = None
        clean_key = key.strip()
        clean_value = value.strip()
        if clean_value:
            metadata[clean_key] = clean_value.strip('"')
        else:
            metadata[clean_key] = []
            current_list_key = clean_key
    return metadata


def _clip_title(path: Path, metadata: dict[str, Any], body: str) -> str:
    source_title = str(metadata.get("source_title", "")).strip()
    if source_title:
        return source_title
    match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    if match:
        return match.group(1)
    return path.stem


def _list_metadata(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
