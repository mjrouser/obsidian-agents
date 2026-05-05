from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from os.path import relpath
from pathlib import Path

from ..config import Config
from ..rendering.action_renderer import ActionRecord, parse_incomplete_actions, render_actions_note
from ..rendering.meeting_renderer import (
    render_extracted_meeting_note,
    render_meeting_note,
    render_vtt_intake_sidecar,
)
from ..utils.dates import monday_of_week
from ..utils.fs import prepend_status_marker, replace_status_marker, safe_write_text
from ..utils.normalization import normalize_owner
from ..utils.text import normalize_whitespace
from .docx_reader import read_docx
from .intake_state import IntakeState, strip_leading_status_marker
from .md_reader import ActionItem, extract_markdown_action_items, read_markdown
from .meeting_metadata import normalize_meeting_metadata
from .vtt_extractor import action_items_from_extracted, extract_vtt_meeting_data, normalize_extracted_meeting_data
from .vtt_reader import read_vtt


@dataclass(slots=True)
class ProcessingSummary:
    processed_files: int = 0
    meeting_notes_written: int = 0
    weekly_action_files_updated: int = 0
    skipped_files: int = 0
    skipped_ignored_basename: int = 0
    skipped_already_processed: int = 0
    skipped_unsupported_ext: int = 0
    processed_sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProcessResult:
    processed: bool
    canonical_note_path: Path | None = None
    actions_file_path: Path | None = None
    skip_reason: str | None = None


@dataclass(slots=True)
class ActionNoteUpdate:
    path: Path
    content: str
    changed: bool


class MeetingProcessor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.vault_path = config.vault_path
        self.intake_path = self.vault_path / config.intake_dir
        self.meetings_path = self.vault_path / config.meetings_dir
        self.actions_path = self.vault_path / config.actions_dir
        self.archive_path = self.vault_path / config.archive_intake_dir
        self.action_owner_aliases = config.action_owner_aliases or {}
        self.intake_state = IntakeState(intake_path=self.intake_path, archive_path=self.archive_path)

    def process_all_unprocessed(self) -> ProcessingSummary:
        summary = ProcessingSummary(processed_sources=[])
        for path in sorted(self.intake_path.iterdir()):
            if not path.is_file():
                continue
            reason = self.skip_reason(path)
            if reason is None:
                result = self.process_file(path)
                if result.processed:
                    summary.processed_files += 1
                    summary.processed_sources.append(path.name)
                    if result.canonical_note_path is not None:
                        summary.meeting_notes_written += 1
                    if result.actions_file_path is not None:
                        summary.weekly_action_files_updated += 1
            else:
                summary.skipped_files += 1
                if reason == "ignored basename":
                    summary.skipped_ignored_basename += 1
                elif reason == "already processed":
                    summary.skipped_already_processed += 1
                elif reason == "unsupported ext":
                    summary.skipped_unsupported_ext += 1
        return summary

    def process_file(self, path: Path, *, force: bool = False, dry_run: bool | None = None) -> ProcessResult:
        source_path = path if path.is_absolute() else self.vault_path / path
        if not source_path.exists() or source_path.is_dir():
            raise FileNotFoundError(source_path)
        source_in_intake = self._is_under_intake(source_path)
        reason = self.skip_reason(source_path) if source_in_intake else None
        if reason is not None and not (force and reason == "already processed"):
            print(f"Skipping {reason} intake file: {source_path}")
            return ProcessResult(processed=False, skip_reason=reason)
        if force and reason == "already processed":
            print(f"Reprocessing (forced) {source_path}")

        effective_dry_run = self.config.dry_run if dry_run is None else dry_run
        body = strip_leading_status_marker(normalize_whitespace(self._read_source(source_path)))
        if source_path.suffix.lower() == ".vtt":
            return self._process_vtt_file(
                source_path,
                body,
                dry_run=effective_dry_run,
                source_in_intake=source_in_intake,
            )
        action_items = self._extract_action_items(source_path, body)
        metadata = normalize_meeting_metadata(source_path)
        meeting_note_path = self.meetings_path / metadata.canonical_basename
        meeting_link = f"{self.config.meetings_dir}/{metadata.canonical_basename}".replace("\\", "/")
        archived_source_path = self._archive_destination(source_path) if source_in_intake else source_path

        meeting_note = render_meeting_note(
            heading=f"{metadata.date} - {metadata.source} - {metadata.title}",
            intake_file=self._vault_relative_path(archived_source_path),
            owner_filter=self.config.owner_filter,
            normalized_body=body,
            meeting_date=metadata.date,
            meeting_source=metadata.source,
            meeting_title=metadata.title,
        )

        action_update = self._prepare_action_note_update(
            action_items=action_items,
            meeting_date=metadata.date,
            canonical_basename=metadata.canonical_basename,
        )
        status_text = f"PROCESSED — see [[{meeting_link}]]"

        if effective_dry_run:
            print(f"DRY RUN — would write: {meeting_note_path}")
            if action_update.changed:
                print(f"DRY RUN — would update: {action_update.path}")
            print(f"DRY RUN — would prepend processed marker: {source_path} -> {status_text}")
            if source_in_intake:
                print(f"DRY RUN — would archive: {source_path} -> {archived_source_path}")
            return ProcessResult(
                processed=True,
                canonical_note_path=meeting_note_path,
                actions_file_path=action_update.path if action_update.changed else None,
            )

        safe_write_text(meeting_note_path, meeting_note)
        if action_update.changed:
            safe_write_text(action_update.path, action_update.content)
        if source_in_intake and source_path.suffix.lower() == ".md":
            if force:
                replace_status_marker(source_path, status_text)
            else:
                prepend_status_marker(source_path, status_text)
        if source_in_intake:
            self._archive_processed_source(source_path)
        return ProcessResult(
            processed=True,
            canonical_note_path=meeting_note_path,
            actions_file_path=action_update.path if action_update.changed else None,
        )

    def _process_vtt_file(
        self,
        source_path: Path,
        transcript_text: str,
        *,
        dry_run: bool,
        source_in_intake: bool,
    ) -> ProcessResult:
        metadata = normalize_meeting_metadata(source_path)
        extracted = extract_vtt_meeting_data(transcript_text=transcript_text, metadata=metadata, config=self.config)
        extracted = normalize_extracted_meeting_data(extracted, metadata)
        meeting_note_path = self.meetings_path / metadata.canonical_basename
        canonical_note_name = metadata.canonical_basename
        archived_source_path = self._archive_destination(source_path) if source_in_intake else source_path
        meeting_note = render_extracted_meeting_note(
            heading=f"{metadata.date} - {metadata.source} - {metadata.title}",
            intake_file=self._vault_relative_path(archived_source_path),
            extracted=extracted,
        )

        action_update = self._prepare_action_note_update(
            action_items=action_items_from_extracted(extracted),
            meeting_date=metadata.date,
            canonical_basename=canonical_note_name,
        )

        sidecar_path = self.intake_path / f"{metadata.date} - {metadata.source} - {metadata.title} (intake).md"
        raw_relative_path = relpath(archived_source_path, sidecar_path.parent)
        sidecar_note = render_vtt_intake_sidecar(
            raw_relative_path=raw_relative_path,
            canonical_note_name=canonical_note_name,
            verbatim_excerpt=str(extracted.get("verbatim_excerpt", "")),
        )

        if dry_run:
            print(f"DRY RUN — would write: {meeting_note_path}")
            if action_update.changed:
                print(f"DRY RUN — would update: {action_update.path}")
            print(f"DRY RUN — would write: {sidecar_path}")
            if source_in_intake:
                print(f"DRY RUN — would archive: {source_path} -> {archived_source_path}")
            return ProcessResult(
                processed=True,
                canonical_note_path=meeting_note_path,
                actions_file_path=action_update.path if action_update.changed else None,
            )

        safe_write_text(meeting_note_path, meeting_note)
        if action_update.changed:
            safe_write_text(action_update.path, action_update.content)
        safe_write_text(sidecar_path, sidecar_note)
        if source_in_intake:
            self._archive_processed_source(source_path)
        return ProcessResult(
            processed=True,
            canonical_note_path=meeting_note_path,
            actions_file_path=action_update.path if action_update.changed else None,
        )

    def _read_source(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".md":
            return read_markdown(path)
        if suffix == ".docx":
            return read_docx(path)
        if suffix == ".vtt":
            return read_vtt(path)
        raise ValueError(f"Unsupported file type: {path.suffix}")

    def _extract_action_items(self, path: Path, body: str) -> list[ActionItem]:
        if path.suffix.lower() != ".md":
            return []
        return extract_markdown_action_items(body)

    def _build_action_records(
        self,
        *,
        action_items: list[ActionItem],
        meeting_date: str,
        canonical_basename: str,
    ) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for item in action_items:
            normalized_owner = self._normalize_owner(item.owner)
            if not self._should_include_action_owner(normalized_owner):
                continue
            records.append(
                ActionRecord(
                    text=item.text,
                    owner=normalized_owner,
                    source_date=meeting_date,
                    source_note=canonical_basename,
                )
            )
        return records

    def _prepare_action_note_update(
        self,
        *,
        action_items: list[ActionItem],
        meeting_date: str,
        canonical_basename: str,
    ) -> ActionNoteUpdate:
        meeting_week = monday_of_week(date.fromisoformat(meeting_date))
        actions_note_path = self.actions_path / f"{meeting_week.isoformat()}.md"
        existing_actions = actions_note_path.read_text(encoding="utf-8") if actions_note_path.exists() else ""
        action_records = self._build_action_records(
            action_items=action_items,
            meeting_date=meeting_date,
            canonical_basename=canonical_basename,
        )
        carry_over_records = (
            self._load_carry_over_records(meeting_week) if action_records and not existing_actions.strip() else []
        )
        actions_note = render_actions_note(
            monday=meeting_week,
            action_records=action_records,
            owner_aliases=self.action_owner_aliases,
            existing_text=existing_actions,
            carry_over_records=carry_over_records,
        )
        return ActionNoteUpdate(
            path=actions_note_path,
            content=actions_note,
            changed=bool(actions_note) and actions_note != existing_actions,
        )

    def _load_carry_over_records(self, meeting_week: date) -> list[ActionRecord]:
        previous_actions_path = self.actions_path / f"{(meeting_week - timedelta(days=7)).isoformat()}.md"
        if not previous_actions_path.exists():
            return []
        previous_actions = previous_actions_path.read_text(encoding="utf-8")
        return parse_incomplete_actions(previous_actions, self.action_owner_aliases)

    def _should_include_action_owner(self, owner: str | None) -> bool:
        normalized_owner = self._normalize_owner(owner)
        normalized_filter = self._normalize_owner(self.config.owner_filter) or self.config.owner_filter
        if normalized_owner is None:
            return self.config.include_unassigned
        return normalized_owner.casefold() == normalized_filter.casefold()

    def _is_processed(self, path: Path) -> bool:
        return self.intake_state.is_processed(path)

    def should_process_intake_file(self, path: Path) -> bool:
        return self.intake_state.should_process(path)

    def skip_reason(self, path: Path) -> str | None:
        return self.intake_state.skip_reason(path)

    def _is_under_intake(self, path: Path) -> bool:
        return self.intake_state.is_under_intake(path)

    def _vtt_has_processed_sidecar(self, path: Path) -> bool:
        return self.intake_state.vtt_has_processed_sidecar(path)

    def _normalize_owner(self, owner: str | None) -> str | None:
        return normalize_owner(owner, self.action_owner_aliases)

    def _vault_relative_path(self, path: Path) -> Path:
        try:
            return path.resolve().relative_to(self.vault_path.resolve())
        except ValueError:
            return Path(path.name)

    def _archive_destination(self, source_path: Path) -> Path:
        return self.intake_state.archive_destination(source_path)

    def _archive_processed_source(self, source_path: Path) -> Path:
        return self.intake_state.archive_processed_source(source_path)
