from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..utils.fs import safe_move_file
from .meeting_metadata import normalize_meeting_metadata

ALLOWED_EXTENSIONS = {".md", ".docx", ".vtt"}
DISALLOWED_BASENAMES = {
    "INBOX.md",
    "Untitled.md",
    "YYYY-MM-DD - Teams - <meeting title>.md",
}
DISALLOWED_BASENAME_PREFIXES = ("Untitled ",)
DISALLOWED_PLACEHOLDER_PATTERNS = ("<meeting title>", "YYYY-MM-DD")
STATUS_PROCESSED_PREFIX = "STATUS: PROCESSED"


@dataclass(slots=True)
class IntakeState:
    intake_path: Path
    archive_path: Path

    def should_process(self, path: Path) -> bool:
        return self.skip_reason(path) is None

    def skip_reason(self, path: Path) -> str | None:
        if not path.exists() or not path.is_file():
            return "ignored basename"
        if not self.is_under_intake(path):
            return "ignored basename"
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            return "unsupported ext"
        if path.name in DISALLOWED_BASENAMES:
            return "ignored basename"
        if any(path.stem.startswith(prefix) for prefix in DISALLOWED_BASENAME_PREFIXES):
            return "ignored basename"
        if any(pattern in path.name for pattern in DISALLOWED_PLACEHOLDER_PATTERNS):
            return "ignored basename"
        if path.suffix.lower() == ".vtt" and self.vtt_has_processed_sidecar(path):
            return "already processed"
        if self.is_processed(path):
            return "already processed"
        return None

    def is_processed(self, path: Path) -> bool:
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as handle:
            for _ in range(3):
                line = handle.readline()
                if not line:
                    return False
                if line.startswith(STATUS_PROCESSED_PREFIX):
                    return True
        return False

    def is_under_intake(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.intake_path.resolve())
            return True
        except ValueError:
            return False

    def vtt_sidecar_path(self, path: Path) -> Path:
        metadata = normalize_meeting_metadata(path)
        return self.intake_path / f"{metadata.date} - {metadata.source} - {metadata.title} (intake).md"

    def vtt_has_processed_sidecar(self, path: Path) -> bool:
        return self.is_processed(self.vtt_sidecar_path(path))

    def archive_destination(self, source_path: Path) -> Path:
        relative_source = source_path.resolve().relative_to(self.intake_path.resolve())
        return self.archive_path / relative_source

    def archive_processed_source(self, source_path: Path) -> Path:
        destination = self.archive_destination(source_path)
        safe_move_file(source_path, destination)
        return destination


def strip_leading_status_marker(text: str) -> str:
    lines = text.splitlines()
    while lines and lines[0].startswith(STATUS_PROCESSED_PREFIX):
        lines.pop(0)
    return "\n".join(lines).strip()
