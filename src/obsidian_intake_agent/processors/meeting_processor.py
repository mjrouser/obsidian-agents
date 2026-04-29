from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from datetime import date
from os.path import relpath
from pathlib import Path

from ..llm.codex_extractor import run_codex_json
from ..llm.prompts import build_meeting_extraction_prompt
from ..rendering.meeting_renderer import (
    ActionRecord,
    render_actions_note,
    render_extracted_meeting_note,
    render_meeting_note,
    render_vtt_intake_sidecar,
)
from ..utils.dates import monday_of_week
from ..utils.fs import prepend_status_marker, replace_status_marker, safe_move_file, safe_write_text
from ..utils.normalization import normalize_owner
from ..utils.text import normalize_whitespace
from .docx_reader import read_docx
from .md_reader import OWNER_WILL_PATTERN, ActionItem, extract_markdown_action_items, parse_action_text, read_markdown
from .vtt_reader import read_vtt

ALLOWED_EXTENSIONS = {".md", ".docx", ".vtt"}
DISALLOWED_BASENAMES = {
    "INBOX.md",
    "Untitled.md",
    "YYYY-MM-DD - Teams - <meeting title>.md",
}
DISALLOWED_BASENAME_PREFIXES = ("Untitled ",)
DISALLOWED_PLACEHOLDER_PATTERNS = ("<meeting title>", "YYYY-MM-DD")
LEADING_DATE_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?: - )?(?P<rest>.*)$")


@dataclass(slots=True)
class MeetingMetadata:
    date: str
    source: str
    title: str
    canonical_basename: str


@dataclass(slots=True)
class ProcessingSummary:
    processed_files: int = 0
    meeting_notes_written: int = 0
    weekly_action_files_updated: int = 0
    skipped_files: int = 0
    skipped_ignored_basename: int = 0
    skipped_already_processed: int = 0
    skipped_unsupported_ext: int = 0
    processed_sources: list[str] | None = None


@dataclass(slots=True)
class ProcessResult:
    processed: bool
    canonical_note_path: Path | None = None
    actions_file_path: Path | None = None
    skip_reason: str | None = None


@dataclass(slots=True)
class Config:
    vault_path: Path
    intake_dir: str
    meetings_dir: str
    actions_dir: str
    archive_intake_dir: str
    templates_dir: str
    owner_filter: str
    dry_run: bool = True
    include_unassigned: bool = False
    llm_provider: str = "codex_cli"
    codex_model: str | None = None
    codex_exec_cmd: list[str] | None = None
    codex_timeout_seconds: int | None = 300
    extraction_mode: str = "draft"
    action_owner_aliases: dict[str, list[str]] | None = None
    git_auto_commit_vault: bool = False
    git_auto_commit_project: bool = False
    git_vault_repo_path: Path | None = None
    git_project_repo_path: Path | None = None
    weekly_reviews_dir: str = "09_Weekly Reviews"
    watcher_settle_seconds: int = 5
    watcher_stable_seconds: int = 2
    automation_log_dir: str = "logs"
    automation_error_dir: str = "_System/Agent Errors"

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = _load_config_data(path)
        vault_path = Path(data["vault_path"]).expanduser()
        return cls(
            vault_path=vault_path,
            intake_dir=data["intake_dir"],
            meetings_dir=data["meetings_dir"],
            actions_dir=data["actions_dir"],
            archive_intake_dir=data["archive_intake_dir"],
            templates_dir=data["templates_dir"],
            owner_filter=data["owner_filter"],
            dry_run=bool(data.get("dry_run", True)),
            include_unassigned=bool(data.get("include_unassigned", False)),
            llm_provider=str(data.get("llm_provider", "codex_cli")),
            codex_model=_optional_string(data.get("codex_model")),
            codex_exec_cmd=_string_list(data.get("codex_exec_cmd", ["codex", "exec"])),
            codex_timeout_seconds=_optional_positive_int(data.get("codex_timeout_seconds", 300)),
            extraction_mode=str(data.get("extraction_mode", "draft")),
            action_owner_aliases=_string_list_map(data.get("action_owner_aliases", {})),
            git_auto_commit_vault=bool(data.get("git_auto_commit_vault", False)),
            git_auto_commit_project=bool(data.get("git_auto_commit_project", False)),
            git_vault_repo_path=_optional_path(data.get("git_vault_repo_path")) or vault_path,
            git_project_repo_path=_optional_path(data.get("git_project_repo_path")) or path.resolve().parent,
            weekly_reviews_dir=str(data.get("weekly_reviews_dir") or data.get("snapshots_dir") or "09_Weekly Reviews"),
            watcher_settle_seconds=int(data.get("watcher_settle_seconds", 5)),
            watcher_stable_seconds=int(data.get("watcher_stable_seconds", 2)),
            automation_log_dir=str(data.get("automation_log_dir", "logs")),
            automation_error_dir=str(data.get("automation_error_dir", "_System/Agent Errors")),
        )


class MeetingProcessor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.vault_path = config.vault_path
        self.intake_path = self.vault_path / config.intake_dir
        self.meetings_path = self.vault_path / config.meetings_dir
        self.actions_path = self.vault_path / config.actions_dir
        self.archive_path = self.vault_path / config.archive_intake_dir
        self.action_owner_aliases = config.action_owner_aliases or {}

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
        body = _strip_leading_status_marker(normalize_whitespace(self._read_source(source_path)))
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

        meeting_week = monday_of_week(date.fromisoformat(metadata.date))
        actions_note_path = self.actions_path / f"{meeting_week.isoformat()}.md"
        existing_actions = ""
        if actions_note_path.exists():
            existing_actions = actions_note_path.read_text(encoding="utf-8")
        action_records = self._build_action_records(
            action_items=action_items,
            meeting_date=metadata.date,
            canonical_basename=metadata.canonical_basename,
        )
        actions_note = render_actions_note(
            monday=meeting_week,
            action_records=action_records,
            owner_aliases=self.action_owner_aliases,
            existing_text=existing_actions,
        )
        status_text = f"PROCESSED — see [[{meeting_link}]]"
        actions_changed = bool(actions_note) and actions_note != existing_actions

        if effective_dry_run:
            print(f"DRY RUN — would write: {meeting_note_path}")
            if actions_changed:
                print(f"DRY RUN — would update: {actions_note_path}")
            print(f"DRY RUN — would prepend processed marker: {source_path} -> {status_text}")
            if source_in_intake:
                print(f"DRY RUN — would archive: {source_path} -> {archived_source_path}")
            return ProcessResult(
                processed=True,
                canonical_note_path=meeting_note_path,
                actions_file_path=actions_note_path if actions_changed else None,
            )

        safe_write_text(meeting_note_path, meeting_note)
        if actions_changed:
            safe_write_text(actions_note_path, actions_note)
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
            actions_file_path=actions_note_path if actions_changed else None,
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
        extracted = self._extract_vtt_meeting_data(transcript_text, metadata)
        extracted = self._normalize_extracted_meeting_data(extracted, metadata)
        meeting_note_path = self.meetings_path / metadata.canonical_basename
        canonical_note_name = metadata.canonical_basename
        archived_source_path = self._archive_destination(source_path) if source_in_intake else source_path
        meeting_note = render_extracted_meeting_note(
            heading=f"{metadata.date} - {metadata.source} - {metadata.title}",
            intake_file=self._vault_relative_path(archived_source_path),
            extracted=extracted,
        )

        meeting_week = monday_of_week(date.fromisoformat(metadata.date))
        actions_note_path = self.actions_path / f"{meeting_week.isoformat()}.md"
        existing_actions = actions_note_path.read_text(encoding="utf-8") if actions_note_path.exists() else ""
        action_records = self._build_action_records(
            action_items=self._action_items_from_extracted(extracted),
            meeting_date=metadata.date,
            canonical_basename=canonical_note_name,
        )
        actions_note = render_actions_note(
            monday=meeting_week,
            action_records=action_records,
            owner_aliases=self.action_owner_aliases,
            existing_text=existing_actions,
        )
        actions_changed = bool(actions_note) and actions_note != existing_actions

        sidecar_path = self.intake_path / f"{metadata.date} - {metadata.source} - {metadata.title} (intake).md"
        raw_relative_path = relpath(archived_source_path, sidecar_path.parent)
        sidecar_note = render_vtt_intake_sidecar(
            raw_relative_path=raw_relative_path,
            canonical_note_name=canonical_note_name,
            verbatim_excerpt=str(extracted.get("verbatim_excerpt", "")),
        )

        if dry_run:
            print(f"DRY RUN — would write: {meeting_note_path}")
            if actions_changed:
                print(f"DRY RUN — would update: {actions_note_path}")
            print(f"DRY RUN — would write: {sidecar_path}")
            if source_in_intake:
                print(f"DRY RUN — would archive: {source_path} -> {archived_source_path}")
            return ProcessResult(
                processed=True,
                canonical_note_path=meeting_note_path,
                actions_file_path=actions_note_path if actions_changed else None,
            )

        safe_write_text(meeting_note_path, meeting_note)
        if actions_changed:
            safe_write_text(actions_note_path, actions_note)
        safe_write_text(sidecar_path, sidecar_note)
        if source_in_intake:
            self._archive_processed_source(source_path)
        return ProcessResult(
            processed=True,
            canonical_note_path=meeting_note_path,
            actions_file_path=actions_note_path if actions_changed else None,
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

    def _should_include_action_owner(self, owner: str | None) -> bool:
        normalized_owner = self._normalize_owner(owner)
        normalized_filter = self._normalize_owner(self.config.owner_filter) or self.config.owner_filter
        if normalized_owner is None:
            return self.config.include_unassigned
        if normalized_owner.casefold() == normalized_filter.casefold():
            return True
        return False

    def _is_processed(self, path: Path) -> bool:
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as handle:
            for _ in range(3):
                line = handle.readline()
                if not line:
                    return False
                if line.startswith("STATUS: PROCESSED"):
                    return True
        return False

    def should_process_intake_file(self, path: Path) -> bool:
        return self.skip_reason(path) is None

    def skip_reason(self, path: Path) -> str | None:
        if not path.exists() or not path.is_file():
            return "ignored basename"
        if not self._is_under_intake(path):
            return "ignored basename"
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            return "unsupported ext"
        if path.name in DISALLOWED_BASENAMES:
            return "ignored basename"
        if any(path.stem.startswith(prefix) for prefix in DISALLOWED_BASENAME_PREFIXES):
            return "ignored basename"
        if any(pattern in path.name for pattern in DISALLOWED_PLACEHOLDER_PATTERNS):
            return "ignored basename"
        if path.suffix.lower() == ".vtt" and self._vtt_has_processed_sidecar(path):
            return "already processed"
        if self._is_processed(path):
            return "already processed"
        return None

    def _is_under_intake(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.intake_path.resolve())
            return True
        except ValueError:
            return False

    def _extract_vtt_meeting_data(self, transcript_text: str, metadata: MeetingMetadata) -> dict:
        if self.config.llm_provider == "codex_cli":
            prompt = build_meeting_extraction_prompt(
                transcript_text=transcript_text,
                meeting_date=metadata.date,
                source=metadata.source,
                title=metadata.title,
            )
            return run_codex_json(
                prompt,
                self.config.codex_model,
                exec_cmd=self.config.codex_exec_cmd,
                timeout_seconds=self.config.codex_timeout_seconds,
            )
        return self._heuristic_extract_meeting_data(transcript_text, metadata)

    def _heuristic_extract_meeting_data(self, transcript_text: str, metadata: MeetingMetadata) -> dict:
        key_points: list[str] = []
        decisions: list[str] = []
        risks: list[str] = []
        open_questions: list[str] = []
        action_items: list[dict[str, str | None]] = []
        for raw_line in transcript_text.splitlines():
            line = raw_line.strip()
            lowered = line.casefold()
            if not line:
                continue
            if lowered.startswith("action:"):
                action = _parse_heuristic_action(line.split(":", 1)[1].strip())
                action_items.append(action)
            elif OWNER_WILL_PATTERN.match(line):
                parsed = parse_action_text(line)
                action_items.append(
                    {
                        "text": parsed.text,
                        "owner": parsed.owner,
                        "due": parsed.due,
                    }
                )
            elif lowered.startswith("decision:"):
                decisions.append(line.split(":", 1)[1].strip())
            elif lowered.startswith("risk:"):
                risks.append(line.split(":", 1)[1].strip())
            elif lowered.startswith("question:"):
                open_questions.append(line.split(":", 1)[1].strip())
            else:
                key_points.append(line)
        return {
            "title": metadata.title,
            "date": metadata.date,
            "source": metadata.source,
            "participants": [],
            "summary_bullets": key_points[:3],
            "key_points": key_points,
            "decisions": decisions,
            "decision_signals": [],
            "risks": risks,
            "assumptions": [],
            "open_questions": open_questions,
            "action_items": action_items,
            "signals_and_tensions": [],
            "alignment_path": [],
            "related_initiatives": [],
            "related_themes": [],
            "verbatim_excerpt": key_points[0] if key_points else "",
        }

    def _normalize_extracted_meeting_data(self, extracted: dict, metadata: MeetingMetadata) -> dict:
        return {
            "title": str(extracted.get("title") or metadata.title),
            "date": str(extracted.get("date") or metadata.date),
            "source": str(extracted.get("source") or metadata.source),
            "participants": list(extracted.get("participants") or []),
            "summary_bullets": list(extracted.get("summary_bullets") or []),
            "key_points": list(extracted.get("key_points") or []),
            "decisions": list(extracted.get("decisions") or []),
            "decision_signals": list(extracted.get("decision_signals") or []),
            "risks": list(extracted.get("risks") or []),
            "assumptions": list(extracted.get("assumptions") or []),
            "open_questions": list(extracted.get("open_questions") or []),
            "action_items": list(extracted.get("action_items") or []),
            "signals_and_tensions": list(extracted.get("signals_and_tensions") or []),
            "alignment_path": list(extracted.get("alignment_path") or []),
            "related_initiatives": list(extracted.get("related_initiatives") or []),
            "related_themes": list(extracted.get("related_themes") or []),
            "verbatim_excerpt": str(extracted.get("verbatim_excerpt") or ""),
        }

    def _action_items_from_extracted(self, extracted: dict) -> list[ActionItem]:
        items: list[ActionItem] = []
        for raw in extracted.get("action_items", []):
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text", "")).strip()
            if not text:
                continue
            owner = raw.get("owner")
            due = raw.get("due")
            items.append(
                ActionItem(
                    owner=str(owner).strip() if owner else None,
                    text=text,
                    due=str(due).strip() if due else None,
                )
            )
        return items

    def _vtt_has_processed_sidecar(self, path: Path) -> bool:
        metadata = normalize_meeting_metadata(path)
        sidecar = self.intake_path / f"{metadata.date} - {metadata.source} - {metadata.title} (intake).md"
        return self._is_processed(sidecar)

    def _normalize_owner(self, owner: str | None) -> str | None:
        return normalize_owner(owner, self.action_owner_aliases)

    def _vault_relative_path(self, path: Path) -> Path:
        try:
            return path.resolve().relative_to(self.vault_path.resolve())
        except ValueError:
            return Path(path.name)

    def _archive_destination(self, source_path: Path) -> Path:
        relative_source = source_path.resolve().relative_to(self.intake_path.resolve())
        return self.archive_path / relative_source

    def _archive_processed_source(self, source_path: Path) -> Path:
        destination = self._archive_destination(source_path)
        safe_move_file(source_path, destination)
        return destination


def normalize_meeting_metadata(intake_path: Path) -> MeetingMetadata:
    basename = intake_path.stem
    match = LEADING_DATE_PATTERN.match(basename)
    if match:
        meeting_date = match.group("date")
        remainder = match.group("rest")
    else:
        meeting_date = date.fromtimestamp(intake_path.stat().st_mtime).isoformat()
        remainder = basename

    if " - Teams - " in basename:
        source = "Teams"
        title = basename.split(" - Teams - ", 1)[1]
    elif " - Copilot - " in basename:
        source = "Copilot"
        title = basename.split(" - Copilot - ", 1)[1]
    else:
        source = "Unknown"
        title = remainder

    title = normalize_whitespace(title)
    if not title:
        title = "Meeting"

    return MeetingMetadata(
        date=meeting_date,
        source=source,
        title=title,
        canonical_basename=f"{meeting_date} - {source} - {title}.md",
    )


def _load_config_data(path: Path) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError("Config must be a mapping.")
        return data
    except ModuleNotFoundError:
        data: dict[str, object] = {}
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, _, value = stripped.partition(":")
            if not _:
                continue
            normalized = value.strip().strip('"').strip("'")
            if normalized.lower() == "true":
                parsed: object = True
            elif normalized.lower() == "false":
                parsed = False
            elif normalized.lower() in {"null", "none"}:
                parsed = None
            elif normalized.startswith("[") and normalized.endswith("]"):
                parsed = ast.literal_eval(normalized)
            else:
                parsed = normalized
            data[key.strip()] = parsed
        return data


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_path(value: object) -> Path | None:
    text = _optional_string(value)
    if text is None:
        return None
    return Path(text).expanduser()


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("codex_timeout_seconds must be a positive integer or null.") from exc
    if parsed <= 0:
        raise ValueError("codex_timeout_seconds must be a positive integer or null.")
    return parsed


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _string_list_map(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, aliases in value.items():
        if isinstance(aliases, list):
            result[str(key)] = [str(alias) for alias in aliases]
    return result


def _parse_heuristic_action(text: str) -> dict[str, str | None]:
    match = re.match(r"^(?P<owner>[^.]+?)\s+will\s+(?P<action>.+)$", text, re.IGNORECASE)
    if match:
        return {
            "text": match.group("action").strip(),
            "owner": match.group("owner").strip(),
            "due": None,
        }
    return {"text": text.strip(), "owner": None, "due": None}


def _strip_leading_status_marker(text: str) -> str:
    lines = text.splitlines()
    while lines and lines[0].startswith("STATUS: PROCESSED"):
        lines.pop(0)
    return "\n".join(lines).strip()
