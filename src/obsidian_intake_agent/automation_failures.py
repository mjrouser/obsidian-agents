from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .processors.meeting_processor import Config
from .utils.fs import safe_write_text

MAX_STDERR_CHARS = 6000


@dataclass(frozen=True, slots=True)
class AutomationFailureNote:
    path: Path
    title: str


def write_automation_failure_note(
    config: Config,
    *,
    job_name: str,
    exit_code: int,
    stderr_log_path: Path,
    occurred_at: datetime | None = None,
) -> AutomationFailureNote:
    occurred = occurred_at or datetime.now().astimezone()
    title = f"Automation failure: {job_name}"
    slug = _slugify(job_name)
    note_dir = config.vault_path / config.automation_error_dir
    note_path = _unique_note_path(note_dir / f"{occurred.strftime('%Y-%m-%d %H%M%S')} - {slug} failed.md")
    content = _render_failure_note(
        title=title,
        job_name=job_name,
        exit_code=exit_code,
        occurred=occurred,
        stderr_log_path=stderr_log_path,
    )
    safe_write_text(note_path, content)
    return AutomationFailureNote(path=note_path, title=title)


def _render_failure_note(
    *,
    title: str,
    job_name: str,
    exit_code: int,
    occurred: datetime,
    stderr_log_path: Path,
) -> str:
    stderr_preview = _read_stderr_preview(stderr_log_path)
    return (
        f"# {title}\n\n"
        f"- Job: `{job_name}`\n"
        f"- Exit code: `{exit_code}`\n"
        f"- Time: `{occurred.isoformat(timespec='seconds')}`\n"
        f"- Stderr log: `{stderr_log_path}`\n\n"
        "## Recent stderr\n\n"
        "```text\n"
        f"{stderr_preview}\n"
        "```\n\n"
        "## Next checks\n\n"
        "- Open the stderr log above for full details.\n"
        "- Check the repo and vault status before rerunning automation.\n"
        "- If this followed a code or config change, run the project checks before restart.\n"
    )


def _read_stderr_preview(stderr_log_path: Path) -> str:
    if not stderr_log_path.exists():
        return f"Stderr log was not found: {stderr_log_path}"
    text = stderr_log_path.read_text(encoding="utf-8", errors="replace")
    preview = text[-MAX_STDERR_CHARS:].strip()
    if not preview:
        return "Stderr log exists but is empty."
    return preview.replace("```", "'''")


def _unique_note_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for counter in range(2, 1000):
        candidate = path.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find available automation failure note path near: {path}")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug or "automation"
