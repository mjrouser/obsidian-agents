from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..processors.meeting_metadata import (
    LEADING_DATE_PATTERN,
    LEGACY_MEETING_MONTH_NAMES,
    MEETING_MONTH_NAMES,
    meeting_output_path,
)
from ..utils.fs import safe_move_file, safe_write_text

_WIKILINK_PATTERN = re.compile(r"\[\[(?P<target>[^\]\r\n]+)\]\]")


@dataclass(slots=True)
class MeetingOrganizationSummary:
    moved: list[tuple[Path, Path]] = field(default_factory=list)
    would_move: list[tuple[Path, Path]] = field(default_factory=list)
    conflicts: list[tuple[Path, Path]] = field(default_factory=list)
    skipped_undated: list[Path] = field(default_factory=list)
    skipped_after_cutoff: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)
    updated_reference_files: list[Path] = field(default_factory=list)
    would_update_reference_files: list[Path] = field(default_factory=list)
    renamed_month_folders: list[tuple[Path, Path]] = field(default_factory=list)
    would_rename_month_folders: list[tuple[Path, Path]] = field(default_factory=list)
    month_folder_conflicts: list[tuple[Path, Path]] = field(default_factory=list)


def organize_meetings(
    *,
    vault_path: Path,
    meetings_root: Path,
    through: date,
    dry_run: bool,
) -> MeetingOrganizationSummary:
    if meetings_root.is_symlink() or not meetings_root.exists() or not meetings_root.is_dir():
        raise ValueError("meetings root must be an existing regular directory")

    summary = MeetingOrganizationSummary()
    folder_renames, folder_conflicts = _planned_month_folder_renames(meetings_root)
    summary.month_folder_conflicts.extend(folder_conflicts)
    moves: list[tuple[Path, Path]] = []
    for source in sorted(meetings_root.iterdir()):
        if not source.is_file() or source.is_symlink() or source.suffix.casefold() != ".md":
            continue
        meeting_date = _date_from_filename(source)
        if meeting_date is None:
            summary.skipped_undated.append(source)
            continue
        if meeting_date > through:
            summary.skipped_after_cutoff.append(source)
            continue
        destination = meeting_output_path(
            meetings_root,
            meeting_date=meeting_date.isoformat(),
            basename=source.name,
        )
        if destination.exists() or destination.is_symlink():
            summary.conflicts.append((source, destination))
            continue
        moves.append((source, destination))

    existing_moves = _existing_organized_moves(meetings_root, through)
    folder_note_moves = _note_moves_for_folder_renames(folder_renames)

    if dry_run:
        summary.would_move.extend(moves)
        summary.would_rename_month_folders.extend(folder_renames)
        summary.would_update_reference_files.extend(
            _reference_files_for_moves(
                vault_path,
                [*moves, *existing_moves, *folder_note_moves],
                include_destination=False,
            )
        )
        return summary

    successful_folder_renames: list[tuple[Path, Path]] = []
    for source, destination in folder_renames:
        try:
            source.rename(destination)
        except OSError as exc:
            summary.failed.append((source, str(exc)))
            continue
        successful_folder_renames.append((source, destination))
        summary.renamed_month_folders.append((source, destination))

    successful_moves: list[tuple[Path, Path]] = []
    for source, destination in moves:
        try:
            safe_move_file(source, destination)
        except OSError as exc:
            summary.failed.append((source, str(exc)))
            continue
        successful_moves.append((source, destination))
        summary.moved.append((source, destination))

    reference_files = _reference_files_for_moves(
        vault_path,
        [*successful_moves, *existing_moves, *_note_moves_for_folder_renames(successful_folder_renames)],
        include_destination=True,
    )
    reference_moves = [*successful_moves, *existing_moves, *_note_moves_for_folder_renames(successful_folder_renames)]
    for reference_path in reference_files:
        original = reference_path.read_text(encoding="utf-8")
        updated = _rewrite_meeting_links(original, reference_moves, vault_path=vault_path)
        if updated == original:
            continue
        safe_write_text(reference_path, updated)
        summary.updated_reference_files.append(reference_path)
    return summary


def render_organization_summary(summary: MeetingOrganizationSummary, *, dry_run: bool, through: date) -> str:
    mode = "dry-run" if dry_run else "execute"
    moves = summary.would_move if dry_run else summary.moved
    lines = [
        f"meeting_organization_mode: {mode}",
        f"meeting_organization_through: {through.isoformat()}",
        f"meeting_organization_candidates: {len(moves)}",
        f"meeting_organization_conflicts: {len(summary.conflicts)}",
        f"meeting_organization_month_folder_renames: "
        f"{len(summary.would_rename_month_folders if dry_run else summary.renamed_month_folders)}",
        f"meeting_organization_month_folder_conflicts: {len(summary.month_folder_conflicts)}",
        f"meeting_organization_skipped_undated: {len(summary.skipped_undated)}",
        f"meeting_organization_skipped_after_cutoff: {len(summary.skipped_after_cutoff)}",
        f"meeting_organization_failed: {len(summary.failed)}",
        f"meeting_organization_reference_files: "
        f"{len(summary.would_update_reference_files if dry_run else summary.updated_reference_files)}",
    ]
    for source, destination in moves:
        lines.append(f"  {source} -> {destination}")
    for source, destination in summary.conflicts:
        lines.append(f"  conflict: {source} -> {destination}")
    for source, destination in summary.would_rename_month_folders if dry_run else summary.renamed_month_folders:
        lines.append(f"  month_folder: {source} -> {destination}")
    for source, destination in summary.month_folder_conflicts:
        lines.append(f"  month_folder_conflict: {source} -> {destination}")
    for source, detail in summary.failed:
        lines.append(f"  failed: {source} ({detail})")
    return "\n".join(lines)


def _date_from_filename(path: Path) -> date | None:
    match = LEADING_DATE_PATTERN.match(path.stem)
    if match is None:
        return None
    try:
        return date.fromisoformat(f"{match.group('year')}-{match.group('month')}-{match.group('day')}")
    except ValueError:
        return None


def _reference_files_for_moves(
    vault_path: Path,
    moves: list[tuple[Path, Path]],
    *,
    include_destination: bool,
) -> list[Path]:
    if not moves:
        return []
    candidates: set[Path] = set()
    moves_by_reference = [*moves]
    for path in vault_path.rglob("*.md"):
        if path.is_file() and not path.is_symlink():
            content = path.read_text(encoding="utf-8")
            if _rewrite_meeting_links(content, moves_by_reference, vault_path=vault_path) != content:
                candidates.add(path)
    if include_destination:
        for _, destination in moves:
            if destination.exists():
                candidates.add(destination)
    return sorted(candidates)


def _planned_month_folder_renames(
    meetings_root: Path,
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    renames: list[tuple[Path, Path]] = []
    conflicts: list[tuple[Path, Path]] = []
    for year_root in sorted(meetings_root.iterdir()):
        if not year_root.is_dir() or year_root.is_symlink() or not year_root.name.isdigit():
            continue
        for month_root in sorted(year_root.iterdir()):
            if not month_root.is_dir() or month_root.is_symlink():
                continue
            try:
                month_number = LEGACY_MEETING_MONTH_NAMES.index(month_root.name)
            except ValueError:
                continue
            destination = year_root / MEETING_MONTH_NAMES[month_number]
            if destination.exists() or destination.is_symlink():
                conflicts.append((month_root, destination))
            else:
                renames.append((month_root, destination))
    return renames, conflicts


def _note_moves_for_folder_renames(
    folder_renames: list[tuple[Path, Path]],
) -> list[tuple[Path, Path]]:
    note_moves: list[tuple[Path, Path]] = []
    for source_root, destination_root in folder_renames:
        scan_root = source_root if source_root.exists() else destination_root
        for scanned_path in sorted(scan_root.rglob("*.md")):
            relative_path = scanned_path.relative_to(scan_root)
            source = source_root / relative_path
            if not source.is_file() or source.is_symlink():
                if not scanned_path.is_file() or scanned_path.is_symlink():
                    continue
            note_moves.append((source, destination_root / relative_path))
    return note_moves


def _existing_organized_moves(meetings_root: Path, through: date) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    for year_root in sorted(meetings_root.iterdir()):
        if not year_root.is_dir() or year_root.is_symlink() or not year_root.name.isdigit():
            continue
        for month_root in sorted(year_root.iterdir()):
            if not month_root.is_dir() or month_root.is_symlink():
                continue
            for destination in sorted(month_root.glob("*.md")):
                if not destination.is_file() or destination.is_symlink():
                    continue
                meeting_date = _date_from_filename(destination)
                if meeting_date is None or meeting_date > through:
                    continue
                legacy_path = meetings_root / destination.name
                if legacy_path.exists() or legacy_path.is_symlink():
                    continue
                moves.append(
                    (
                        legacy_path,
                        meeting_output_path(
                            meetings_root,
                            meeting_date=meeting_date.isoformat(),
                            basename=destination.name,
                        ),
                    )
                )
    return moves


def _rewrite_meeting_links(
    content: str,
    moves: list[tuple[Path, Path]],
    *,
    vault_path: Path,
) -> str:
    mappings: dict[str, str] = {}
    for source, destination in moves:
        source_path = _relative_link_path(source, vault_path)
        destination_path = _relative_link_path(destination, vault_path)
        mappings[source_path] = destination_path
        if source_path.casefold().endswith(".md"):
            mappings[source_path[:-3]] = destination_path[:-3]
    if not mappings:
        return content

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        link_path, suffix = _split_wikilink_target(target)
        replacement = mappings.get(link_path.replace("\\", "/"))
        if replacement is None:
            return match.group(0)
        return f"[[{replacement}{suffix}]]"

    return _WIKILINK_PATTERN.sub(replace, content)


def _relative_link_path(path: Path, vault_path: Path) -> str:
    return path.resolve().relative_to(vault_path.resolve()).as_posix()


def _split_wikilink_target(target: str) -> tuple[str, str]:
    path_and_anchor, separator, alias = target.partition("|")
    link_path, anchor_separator, anchor = path_and_anchor.partition("#")
    suffix = ""
    if anchor_separator:
        suffix += f"#{anchor}"
    if separator:
        suffix += f"|{alias}"
    return link_path, suffix
