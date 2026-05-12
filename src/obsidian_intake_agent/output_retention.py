from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_DATED_MARKDOWN_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\b")


@dataclass
class OutputRetentionSummary:
    moved: list[Path] = field(default_factory=list)
    would_move: list[Path] = field(default_factory=list)
    skipped_existing: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class _OutputRecord:
    path: Path
    record_date: date


def apply_output_retention(
    root_dir: Path,
    *,
    archive_dir_name: str,
    keep_count: int,
    dry_run: bool = False,
) -> OutputRetentionSummary:
    if keep_count < 0:
        raise ValueError("keep_count must be greater than or equal to 0")
    archive_dir = _archive_dir(root_dir, archive_dir_name)

    summary = OutputRetentionSummary()
    if not root_dir.is_dir():
        return summary

    records = sorted(
        _dated_markdown_files(root_dir),
        key=lambda record: (record.record_date, record.path.name),
        reverse=True,
    )

    for record in records[keep_count:]:
        destination = archive_dir / record.path.name
        if destination.exists():
            summary.skipped_existing.append(record.path)
            continue
        if dry_run:
            summary.would_move.append(record.path)
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(record.path), str(destination))
        summary.moved.append(record.path)

    return summary


def _archive_dir(root_dir: Path, archive_dir_name: str) -> Path:
    archive_path = Path(archive_dir_name)
    if archive_path.is_absolute() or ".." in archive_path.parts:
        raise ValueError("archive_dir_name must be a relative path without '..' components")
    archive_dir = root_dir / archive_path
    try:
        archive_dir.resolve().relative_to(root_dir.resolve())
    except ValueError as exc:
        raise ValueError("archive_dir_name must resolve inside root_dir") from exc
    return archive_dir


def _dated_markdown_files(root_dir: Path) -> list[_OutputRecord]:
    records: list[_OutputRecord] = []
    for path in root_dir.iterdir():
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        match = _DATED_MARKDOWN_RE.match(path.name)
        if match is None:
            continue
        try:
            record_date = date.fromisoformat(match.group("date"))
        except ValueError:
            continue
        records.append(_OutputRecord(path=path, record_date=record_date))
    return records
