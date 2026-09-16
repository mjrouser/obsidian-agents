from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import os
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from ..processors.meeting_metadata import MeetingMetadata, meeting_output_path
from ..processors.meeting_processor import MeetingProcessor, OutputMode, ProcessResult
from ..utils.dates import monday_of_week
from .identity_state import (
    IdentityMarkerTransaction,
    identity_marker_entry_exists,
    identity_marker_transaction,
    read_identity_state,
)
from .meeting_upgrade import (
    archive_canonical_note,
    canonical_matches_hash,
    migrate_action_backlinks,
)
from .transcript_provenance import matching_provenance, provenance_path

ArtifactStatus = Literal["available", "missing", "permission_blocked", "not_attempted"]
BundleProcessDecision = Literal["ready", "blocked"]
BundleExecutionStatus = Literal["processed", "skipped", "failed"]
PROCESSOR_SUPPORTED_EXTENSIONS = {".md", ".docx", ".vtt"}
SUPPORTED_ATTACH_EXTENSIONS = {".md", ".docx", ".vtt"}
MACHINE_MANAGED_BUNDLE_DIR_NAMES = ("raw_transcripts", "fallbacks")


class BundleProcessingLockError(RuntimeError):
    """Raised only when the bundle-root execution lock cannot be acquired safely."""


@dataclass(slots=True, frozen=True)
class BundleArtifactRecord:
    source_name: str
    status: ArtifactStatus
    detail: str | None
    matched_paths: tuple[Path, ...]
    transcript_diagnostics: dict[str, object] | None = None
    occurrence_validated_paths: tuple[Path, ...] = ()


@dataclass(slots=True, frozen=True)
class BundleProcessorHandoff:
    preferred_input_path: Path | None
    preferred_input_source_name: str | None


@dataclass(slots=True, frozen=True)
class BundleMetadataRecord:
    metadata_path: Path
    bundle_note_path: Path
    processed_marker_path: Path | None
    event_id: str | None
    subject: str
    start_at: datetime | None
    teams_meeting_id: str | None
    identity_key: str | None
    source_type: str | None
    processor_handoff: BundleProcessorHandoff
    artifacts: tuple[BundleArtifactRecord, ...]
    end_at: datetime | None = None
    fallback_not_before: datetime | None = None
    retry_until: datetime | None = None
    first_seen_at: datetime | None = None
    selection_window_start_at: datetime | None = None
    selection_window_end_at: datetime | None = None
    organizer: str | None = None
    attendees: tuple[str, ...] = ()
    join_url: str | None = None
    occurrence_event_id: str | None = None
    source_limitations: tuple[str, ...] = ()
    present_identity_fields: frozenset[str] = frozenset()
    invalid_identity_datetime_fields: frozenset[str] = frozenset()

    def available_sources(self) -> tuple[str, ...]:
        return tuple(artifact.source_name for artifact in self.artifacts if artifact.status == "available")

    def pending_artifacts(self) -> tuple[BundleArtifactRecord, ...]:
        return tuple(artifact for artifact in self.artifacts if artifact.status != "available")

    def permission_blocked_sources(self) -> tuple[str, ...]:
        return tuple(artifact.source_name for artifact in self.artifacts if artifact.status == "permission_blocked")


@dataclass(slots=True, frozen=True)
class BundleProcessingPlanItem:
    decision: BundleProcessDecision
    metadata: BundleMetadataRecord
    reasons: tuple[str, ...]
    upgrade_from_fallback: bool = False


@dataclass(slots=True, frozen=True)
class BundleProcessingPlan:
    generated_at: datetime
    bundle_root: Path
    items: tuple[BundleProcessingPlanItem, ...]
    warnings: tuple[str, ...] = ()

    @property
    def candidate_count(self) -> int:
        return len(self.items)

    @property
    def ready_count(self) -> int:
        return sum(1 for item in self.items if item.decision == "ready")

    @property
    def blocked_count(self) -> int:
        return sum(1 for item in self.items if item.decision == "blocked")


@dataclass(slots=True, frozen=True)
class BundleExecutionResultItem:
    status: BundleExecutionStatus
    plan_item: BundleProcessingPlanItem
    reasons: tuple[str, ...]
    canonical_note_path: Path | None = None
    actions_file_path: Path | None = None
    manual_review_required: bool = False
    archived_fallback_note_path: Path | None = None


@dataclass(slots=True, frozen=True)
class BundleExecutionResult:
    executed_at: datetime
    bundle_root: Path
    items: tuple[BundleExecutionResultItem, ...]
    warnings: tuple[str, ...] = ()
    output_mode: OutputMode = "normal"

    @property
    def candidate_count(self) -> int:
        return len(self.items)

    @property
    def processed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "processed")

    @property
    def skipped_count(self) -> int:
        return sum(1 for item in self.items if item.status == "skipped")

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")

    @property
    def manual_review_required_count(self) -> int:
        return sum(1 for item in self.items if item.manual_review_required)

    @property
    def upgrade_archived_note_paths(self) -> tuple[Path, ...]:
        paths: dict[Path, None] = {}
        for item in self.items:
            if item.archived_fallback_note_path is not None:
                paths.setdefault(item.archived_fallback_note_path, None)
        return tuple(paths)


@dataclass(slots=True, frozen=True)
class AttachTranscriptResult:
    metadata_path: Path
    attached_path: Path
    source_name: str


@dataclass(slots=True, frozen=True)
class _StagingFileSnapshot:
    path: Path
    content: bytes
    generated_archive_path: Path | None
    sidecar_path: Path | None
    sidecar_content: bytes | None


@dataclass(slots=True, frozen=True)
class _IdentityMarkerSnapshot:
    path: Path
    existed: bool
    content: bytes | None
    pending_payload: dict[str, object]


@dataclass(slots=True, frozen=True)
class _OutputFileSnapshot:
    path: Path
    content: bytes | None


@dataclass(slots=True, frozen=True)
class _FallbackUpgradeContext:
    canonical_note_path: Path
    archived_fallback_note_path: Path | None
    previous_payload: dict[str, object]
    previous_canonical_sha256: str
    fallback_source_alias: Path | None
    validated_transcript_provenance: dict[str, object]


def build_bundle_processing_plan(
    *,
    intake_root: Path,
    processor: MeetingProcessor,
    now: datetime | None = None,
) -> BundleProcessingPlan:
    generated_at = now or datetime.now().astimezone()
    items: list[BundleProcessingPlanItem] = []
    warnings: list[str] = []
    if not intake_root.exists():
        warnings.append(f"Meeting intake root does not exist: {intake_root}")
        return BundleProcessingPlan(
            generated_at=generated_at,
            bundle_root=intake_root,
            items=(),
            warnings=tuple(warnings),
        )

    for metadata_path in sorted(intake_root.glob("* (outlook).json")):
        try:
            metadata = _load_bundle_metadata_record(metadata_path)
        except ValueError as exc:
            warnings.append(f"Skipped unreadable bundle metadata {metadata_path}: {exc}")
            continue
        items.append(_plan_bundle_processing_item(metadata=metadata, processor=processor, now=generated_at))

    return BundleProcessingPlan(
        generated_at=generated_at,
        bundle_root=intake_root,
        items=tuple(items),
        warnings=tuple(warnings),
    )


def render_bundle_processing_plan(plan: BundleProcessingPlan) -> str:
    lines = [
        "meeting_bundle_process_mode: dry-run",
        f"meeting_bundle_process_generated_at: {plan.generated_at.isoformat()}",
        f"meeting_bundle_process_bundle_root: {plan.bundle_root}",
        f"meeting_bundle_process_candidates: {plan.candidate_count}",
        f"meeting_bundle_process_ready: {plan.ready_count}",
        f"meeting_bundle_process_blocked: {plan.blocked_count}",
        (
            "meeting_bundle_process_blocked_missing_preferred_input: "
            f"{_count_blocked_reason(plan.items, 'Missing preferred processor input path in bundle metadata.')}"
        ),
        (
            "meeting_bundle_process_blocked_missing_input_file: "
            f"{_count_blocked_reason(plan.items, 'Preferred processor input file does not exist on disk.')}"
        ),
        (
            "meeting_bundle_process_blocked_calendar_only: "
            f"{_count_blocked_reason(plan.items, 'Bundle is still calendar-only, so there is no processor-ready transcript artifact yet.')}"
        ),
        (
            "meeting_bundle_process_blocked_permission_blocked: "
            f"{_count_blocked_with_prefix(plan.items, 'Permission blocked retrieval of ')}"
        ),
        (
            "meeting_bundle_process_blocked_processor_skip: "
            f"{_count_blocked_with_prefix(plan.items, 'Preferred processor input would currently be skipped by the existing processor: ')}"
        ),
    ]
    for warning in plan.warnings:
        lines.append(f"meeting_bundle_process_warning: {warning}")
    for item in plan.items:
        lines.append(
            f"meeting_bundle_item: {item.decision} "
            f'event_id="{item.metadata.occurrence_event_id or item.metadata.event_id or ""}" '
            f'subject="{item.metadata.subject}"'
        )
        lines.append(f"  outlook_metadata: {item.metadata.metadata_path}")
        lines.append(f"  bundle_note: {item.metadata.bundle_note_path}")
        if item.upgrade_from_fallback:
            lines.append("  operation: transcript_upgrade")
        if item.metadata.processor_handoff.preferred_input_path is not None:
            lines.append(f"  preferred_input: {item.metadata.processor_handoff.preferred_input_path}")
        if item.metadata.processor_handoff.preferred_input_source_name is not None:
            lines.append(f"  preferred_source: {item.metadata.processor_handoff.preferred_input_source_name}")
        for source_name in item.metadata.available_sources():
            lines.append(f"  source_available: {source_name}")
        for artifact in item.metadata.pending_artifacts():
            detail_suffix = f" ({artifact.detail})" if artifact.detail else ""
            lines.append(f"  source_pending: {artifact.source_name}={artifact.status}{detail_suffix}")
        if item.decision == "ready" and item.metadata.processor_handoff.preferred_input_path is not None:
            lines.append(
                "  would_run: "
                f".venv/bin/obsidian-agent process {item.metadata.processor_handoff.preferred_input_path} --dry-run"
            )
        if item.decision == "blocked" and "Missing preferred processor input path in bundle metadata." in item.reasons:
            expected_stem = _expected_local_transcript_stem(item.metadata)
            if expected_stem is not None:
                lines.append(f"  expected_local_transcript_stem: {expected_stem}")
            lines.append(f"  next_step: {_bundle_next_step(item.metadata)}")
        if item.decision == "blocked" and "Preferred processor input file does not exist on disk." in item.reasons:
            missing_path = item.metadata.processor_handoff.preferred_input_path
            if missing_path is not None:
                lines.append(f"  missing_preferred_input: {missing_path}")
        for reason in item.reasons:
            lines.append(f"  reason: {reason}")
    return "\n".join(lines)


def attach_transcript_to_bundle(*, bundle_root: Path, event_id: str, file_path: Path) -> AttachTranscriptResult:
    if file_path.suffix.lower() not in SUPPORTED_ATTACH_EXTENSIONS:
        raise ValueError(f"Unsupported transcript file type: {file_path.suffix}")
    if not file_path.exists() or file_path.is_dir():
        raise ValueError(f"Transcript file does not exist: {file_path}")

    metadata_paths = _metadata_paths_for_event_id(bundle_root=bundle_root, event_id=event_id)
    if not metadata_paths:
        raise ValueError(f"No meeting bundle metadata found for Outlook event ID: {event_id}")
    if len(metadata_paths) > 1:
        rendered = ", ".join(str(path) for path in metadata_paths)
        raise ValueError(f"Multiple meeting bundle metadata files matched Outlook event ID {event_id}: {rendered}")

    metadata_path = metadata_paths[0]
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    attached_dir = bundle_root / "raw_transcripts"
    attached_dir.mkdir(parents=True, exist_ok=True)
    attached_path = attached_dir / file_path.name
    if attached_path.exists():
        raise ValueError(f"Refusing to overwrite existing staged transcript: {attached_path}")
    attached_path.write_bytes(file_path.read_bytes())
    _update_payload_for_attached_transcript(payload=payload, attached_path=attached_path)
    metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return AttachTranscriptResult(
        metadata_path=metadata_path,
        attached_path=attached_path,
        source_name="Manual / semi-manual intake",
    )


def execute_bundle_processing_plan(
    plan: BundleProcessingPlan,
    *,
    processor: MeetingProcessor,
) -> BundleExecutionResult:
    if not any(item.decision == "ready" for item in plan.items):
        return _execute_bundle_processing_plan_locked(plan, processor=processor)
    try:
        with _bundle_processing_execution_lock(plan.bundle_root):
            return _execute_bundle_processing_plan_locked(plan, processor=processor)
    except BundleProcessingLockError as exc:
        return _bundle_processing_lock_failure_result(
            plan=plan,
            processor=processor,
            error=exc,
        )


def _execute_bundle_processing_plan_locked(
    plan: BundleProcessingPlan,
    *,
    processor: MeetingProcessor,
) -> BundleExecutionResult:
    items: list[BundleExecutionResultItem] = []
    action_notes_changed = False
    for plan_item in plan.items:
        if plan_item.decision != "ready":
            items.append(
                BundleExecutionResultItem(
                    status="skipped",
                    plan_item=plan_item,
                    reasons=plan_item.reasons,
                )
            )
            continue
        preferred_input = plan_item.metadata.processor_handoff.preferred_input_path
        if preferred_input is None:
            items.append(
                BundleExecutionResultItem(
                    status="skipped",
                    plan_item=plan_item,
                    reasons=("Ready bundle did not include a preferred processor input path at execution time.",),
                )
            )
            continue
        marker_path = plan_item.metadata.processed_marker_path
        transaction_context = identity_marker_transaction(marker_path) if marker_path is not None else nullcontext(None)
        try:
            with transaction_context as marker_transaction:
                result_item, committed_actions = _execute_ready_bundle_item_in_transaction(
                    plan_item=plan_item,
                    preferred_input=preferred_input,
                    processor=processor,
                    processed_at=plan.generated_at,
                    marker_transaction=marker_transaction,
                )
        except (OSError, UnicodeError, ValueError) as exc:
            result_item = BundleExecutionResultItem(
                status="failed",
                plan_item=plan_item,
                reasons=(f"Bundle identity state was unsafe at execution start: {exc}.",),
            )
            committed_actions = False
        items.append(result_item)
        if committed_actions:
            action_notes_changed = True
    warnings = list(plan.warnings)
    if action_notes_changed:
        try:
            _apply_deferred_action_retention(processor)
        except (OSError, ValueError) as exc:
            warning = f"Deferred action retention failed after committed bundle processing: {exc}"
            warnings.append(warning)
            items = [
                (
                    BundleExecutionResultItem(
                        status=item.status,
                        plan_item=item.plan_item,
                        reasons=(*item.reasons, f"Post-commit warning: {warning}."),
                        canonical_note_path=item.canonical_note_path,
                        actions_file_path=item.actions_file_path,
                    )
                    if item.status == "processed"
                    else item
                )
                for item in items
            ]
    return BundleExecutionResult(
        executed_at=datetime.now().astimezone(),
        bundle_root=plan.bundle_root,
        items=tuple(items),
        warnings=tuple(warnings),
        output_mode=getattr(processor, "output_mode", "normal"),
    )


@contextmanager
def _bundle_processing_execution_lock(bundle_root: Path) -> Iterator[None]:
    descriptor = -1
    try:
        if bundle_root.is_symlink():
            raise BundleProcessingLockError("bundle processing lock root must not be a symlink")
        lock_root = bundle_root / "_meeting_sync"
        if lock_root.is_symlink():
            raise BundleProcessingLockError("bundle processing lock parent must not be a symlink")
        lock_root.mkdir(parents=True, exist_ok=True)
        if bundle_root.is_symlink() or lock_root.is_symlink():
            raise BundleProcessingLockError("bundle processing lock path changed during setup")
        lock_path = lock_root / ".bundle-processing.lock"
        descriptor = _open_and_lock_bundle_processing_file(lock_path)
        _fsync_parent_directory(lock_root)
    except BundleProcessingLockError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except (OSError, ValueError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise BundleProcessingLockError("bundle processing lock setup failed") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _open_and_lock_bundle_processing_file(lock_path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise BundleProcessingLockError("bundle processing lock file could not be opened safely") from exc
    try:
        lock_status = os.fstat(descriptor)
        if not stat.S_ISREG(lock_status.st_mode) or lock_status.st_nlink != 1:
            raise BundleProcessingLockError("bundle processing lock file must be an unaliased regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        descriptor_status = os.fstat(descriptor)
        path_status = os.stat(lock_path, follow_symlinks=False)
        if (
            descriptor_status.st_dev != path_status.st_dev
            or descriptor_status.st_ino != path_status.st_ino
            or not stat.S_ISREG(path_status.st_mode)
            or path_status.st_nlink != 1
        ):
            raise BundleProcessingLockError("bundle processing lock file changed while it was acquired")
    except BundleProcessingLockError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise BundleProcessingLockError("bundle processing lock file could not be acquired safely") from exc
    return descriptor


def _bundle_processing_lock_failure_result(
    *,
    plan: BundleProcessingPlan,
    processor: MeetingProcessor,
    error: Exception,
) -> BundleExecutionResult:
    items = tuple(
        BundleExecutionResultItem(
            status="failed" if plan_item.decision == "ready" else "skipped",
            plan_item=plan_item,
            reasons=(
                (f"Unsafe bundle processing lock file: {error}.",)
                if plan_item.decision == "ready"
                else plan_item.reasons
            ),
        )
        for plan_item in plan.items
    )
    return BundleExecutionResult(
        executed_at=datetime.now().astimezone(),
        bundle_root=plan.bundle_root,
        items=items,
        warnings=plan.warnings,
        output_mode=getattr(processor, "output_mode", "normal"),
    )


def _execute_ready_bundle_item_in_transaction(
    *,
    plan_item: BundleProcessingPlanItem,
    preferred_input: Path,
    processor: MeetingProcessor,
    processed_at: datetime,
    marker_transaction: IdentityMarkerTransaction | None,
) -> tuple[BundleExecutionResultItem, bool]:
    try:
        identity_marker_snapshot = _capture_identity_marker_snapshot(
            plan_item.metadata,
            marker_transaction=marker_transaction,
            allow_fallback_upgrade=plan_item.upgrade_from_fallback,
        )
        _revalidate_identity_marker_snapshot(
            identity_marker_snapshot,
            marker_transaction=marker_transaction,
            allow_fallback_upgrade=plan_item.upgrade_from_fallback,
        )
        if plan_item.upgrade_from_fallback:
            upgrade_validation, manual_review_reason, manual_review_path = _validate_fallback_upgrade(
                metadata=plan_item.metadata,
                processor=processor,
                identity_marker_snapshot=identity_marker_snapshot,
            )
            if manual_review_reason is not None:
                _write_upgrade_manual_review_marker(
                    identity_marker_snapshot=identity_marker_snapshot,
                    marker_transaction=marker_transaction,
                    reason=manual_review_reason,
                )
                return (
                    BundleExecutionResultItem(
                        status="skipped",
                        plan_item=plan_item,
                        reasons=(f"Fallback transcript upgrade requires manual review: {manual_review_reason}.",),
                        canonical_note_path=manual_review_path,
                        manual_review_required=True,
                    ),
                    False,
                )
        else:
            upgrade_validation = None
        output_snapshots = _capture_expected_output_snapshots(
            metadata=plan_item.metadata,
            processor=processor,
        )
        staging_snapshots = _capture_bundle_staging(
            metadata=plan_item.metadata,
            processor=processor,
        )
        _revalidate_identity_marker_snapshot(
            identity_marker_snapshot,
            marker_transaction=marker_transaction,
            allow_fallback_upgrade=plan_item.upgrade_from_fallback,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return (
            BundleExecutionResultItem(
                status="failed",
                plan_item=plan_item,
                reasons=(f"Bundle identity or output state was unsafe before processor execution: {exc}.",),
            ),
            False,
        )

    upgrade_context: _FallbackUpgradeContext | None = None
    if upgrade_validation is not None:
        try:
            archive_root = _meeting_upgrade_archive_root(processor)
            archived_fallback_path = archive_canonical_note(
                upgrade_validation.canonical_note_path,
                archive_root,
                expected_sha256=upgrade_validation.previous_canonical_sha256,
            )
            upgrade_context = replace(
                upgrade_validation,
                archived_fallback_note_path=archived_fallback_path,
            )
        except (OSError, ValueError) as exc:
            return (
                BundleExecutionResultItem(
                    status="failed",
                    plan_item=plan_item,
                    reasons=(f"Fallback note could not be archived safely before upgrade: {exc}.",),
                    canonical_note_path=upgrade_validation.canonical_note_path,
                ),
                False,
            )

        manual_review_reason = _upgrade_input_revalidation_reason(
            metadata=plan_item.metadata,
            upgrade_context=upgrade_context,
        )
        if manual_review_reason is not None:
            try:
                _write_upgrade_manual_review_marker(
                    identity_marker_snapshot=identity_marker_snapshot,
                    marker_transaction=marker_transaction,
                    reason=manual_review_reason,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                return (
                    BundleExecutionResultItem(
                        status="failed",
                        plan_item=plan_item,
                        reasons=(f"Fallback upgrade manual-review marker could not be written: {exc}.",),
                        canonical_note_path=upgrade_context.canonical_note_path,
                        archived_fallback_note_path=upgrade_context.archived_fallback_note_path,
                    ),
                    False,
                )
            return (
                BundleExecutionResultItem(
                    status="skipped",
                    plan_item=plan_item,
                    reasons=(f"Fallback transcript upgrade requires manual review: {manual_review_reason}.",),
                    canonical_note_path=upgrade_context.canonical_note_path,
                    manual_review_required=True,
                    archived_fallback_note_path=upgrade_context.archived_fallback_note_path,
                ),
                False,
            )

    try:
        process_result = _process_file_with_deferred_retention(
            processor,
            preferred_input,
            metadata=plan_item.metadata,
            upgrade_context=upgrade_context,
        )
    except Exception as exc:
        restore_errors = _restore_processing_transaction(
            output_snapshots=output_snapshots,
            staging_snapshots=staging_snapshots,
        )
        return (
            BundleExecutionResultItem(
                status="failed",
                plan_item=plan_item,
                reasons=(f"Processor execution failed: {exc}{_restore_error_suffix(restore_errors)}",),
                archived_fallback_note_path=(
                    upgrade_context.archived_fallback_note_path if upgrade_context is not None else None
                ),
            ),
            False,
        )

    if not process_result.processed:
        restore_errors = _restore_processing_transaction(
            output_snapshots=output_snapshots,
            staging_snapshots=staging_snapshots,
        )
        result_item = _execution_result_item_from_process_result(
            plan_item=plan_item,
            process_result=process_result,
        )
        if restore_errors:
            result_item = BundleExecutionResultItem(
                status=result_item.status,
                plan_item=result_item.plan_item,
                reasons=(*result_item.reasons, f"Transaction restoration failed: {'; '.join(restore_errors)}."),
                canonical_note_path=result_item.canonical_note_path,
                actions_file_path=result_item.actions_file_path,
            )
        if upgrade_context is not None:
            result_item = replace(
                result_item,
                archived_fallback_note_path=upgrade_context.archived_fallback_note_path,
            )
        return result_item, False

    try:
        if upgrade_context is not None:
            process_result = _migrate_upgrade_action_note(
                process_result=process_result,
                output_snapshots=output_snapshots,
                upgrade_context=upgrade_context,
                processor=processor,
            )
        _validate_process_result_output_paths(
            process_result=process_result,
            output_snapshots=output_snapshots,
        )
        processed_marker_path = _write_durable_processed_marker(
            metadata=plan_item.metadata,
            process_result=process_result,
            cleanup_paths=_bundle_cleanup_paths(plan_item.metadata),
            processor=processor,
            processed_at=processed_at,
            identity_marker_snapshot=identity_marker_snapshot,
            marker_transaction=marker_transaction,
            upgrade_context=upgrade_context,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        restore_errors = _restore_processing_transaction(
            output_snapshots=output_snapshots,
            staging_snapshots=staging_snapshots,
        )
        return (
            BundleExecutionResultItem(
                status="failed",
                plan_item=plan_item,
                reasons=(
                    "Bundle processed successfully but could not write durable processed marker: "
                    f"{exc}.{_restore_error_suffix(restore_errors)}",
                ),
                canonical_note_path=process_result.canonical_note_path,
                actions_file_path=process_result.actions_file_path,
                archived_fallback_note_path=(
                    upgrade_context.archived_fallback_note_path if upgrade_context is not None else None
                ),
            ),
            False,
        )

    removed_paths, cleanup_errors = _cleanup_bundle_staging(metadata=plan_item.metadata)
    result_item = _execution_result_item_from_process_result(
        plan_item=plan_item,
        process_result=process_result,
        processed_marker_path=processed_marker_path,
        removed_paths=removed_paths,
        cleanup_errors=cleanup_errors,
    )
    if upgrade_context is not None:
        result_item = replace(
            result_item,
            archived_fallback_note_path=upgrade_context.archived_fallback_note_path,
        )
    return result_item, process_result.actions_file_path is not None


def _capture_expected_output_snapshots(
    *,
    metadata: BundleMetadataRecord,
    processor: MeetingProcessor,
) -> tuple[_OutputFileSnapshot, _OutputFileSnapshot]:
    meeting_metadata = _authoritative_meeting_metadata(metadata)
    meetings_root = getattr(processor, "meetings_path", None)
    actions_root = getattr(processor, "actions_path", None)
    if not isinstance(meetings_root, Path) or not isinstance(actions_root, Path):
        raise ValueError("processor does not expose trusted meeting and action output roots")
    if meetings_root.is_symlink():
        raise ValueError("trusted meetings output root must not be a symlink")
    if actions_root.is_symlink():
        raise ValueError("trusted actions output root must not be a symlink")
    canonical_path = meeting_output_path(
        meetings_root,
        meeting_date=meeting_metadata.date,
        basename=meeting_metadata.canonical_basename,
    )
    action_week = monday_of_week(metadata.start_at.date()) if metadata.start_at is not None else None
    if action_week is None:
        raise ValueError("bundle metadata is missing the action-note week")
    actions_path = actions_root / f"{action_week.isoformat()}.md"
    return (
        _capture_output_file_snapshot(canonical_path),
        _capture_output_file_snapshot(actions_path),
    )


def _capture_output_file_snapshot(path: Path) -> _OutputFileSnapshot:
    if path.is_symlink():
        raise ValueError("transaction output path must not be a symlink")
    if not path.exists():
        return _OutputFileSnapshot(path=path, content=None)
    if not path.is_file():
        raise ValueError("transaction output path must be a regular file")
    return _OutputFileSnapshot(path=path, content=_read_regular_file_bytes(path))


def _read_regular_file_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("transaction output could not be opened without following symlinks") from exc
    try:
        output_status = os.fstat(descriptor)
        if not stat.S_ISREG(output_status.st_mode):
            raise ValueError("transaction output is not a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_process_result_output_paths(
    *,
    process_result: ProcessResult,
    output_snapshots: tuple[_OutputFileSnapshot, _OutputFileSnapshot],
) -> None:
    canonical_snapshot, actions_snapshot = output_snapshots
    if process_result.canonical_note_path != canonical_snapshot.path:
        raise ValueError("canonical note path does not match the trusted meetings output")
    if process_result.actions_file_path is not None and process_result.actions_file_path != actions_snapshot.path:
        raise ValueError("processor actions output path does not match the transaction snapshot")


def _restore_processing_transaction(
    *,
    output_snapshots: tuple[_OutputFileSnapshot, _OutputFileSnapshot],
    staging_snapshots: tuple[_StagingFileSnapshot, ...],
) -> tuple[str, ...]:
    errors = list(_restore_output_files(output_snapshots))
    errors.extend(_restore_bundle_staging(staging_snapshots))
    return tuple(errors)


def _restore_output_files(
    snapshots: tuple[_OutputFileSnapshot, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    for snapshot in snapshots:
        try:
            if snapshot.content is None:
                if snapshot.path.is_symlink() or snapshot.path.exists():
                    if snapshot.path.is_dir() and not snapshot.path.is_symlink():
                        raise OSError("transaction-created output is a directory")
                    snapshot.path.unlink()
                    _fsync_parent_directory(snapshot.path.parent)
                continue
            current_content = (
                _read_regular_file_bytes(snapshot.path)
                if snapshot.path.exists() and not snapshot.path.is_symlink()
                else None
            )
            if current_content != snapshot.content:
                _atomic_restore_bytes(snapshot.path, snapshot.content)
        except (OSError, ValueError) as exc:
            errors.append(f"{snapshot.path}: {exc}")
    return tuple(errors)


def _restore_error_suffix(errors: tuple[str, ...]) -> str:
    if not errors:
        return ""
    return " Transaction restoration also failed: " + "; ".join(errors) + "."


def _process_file_with_deferred_retention(
    processor: MeetingProcessor,
    preferred_input: Path,
    *,
    metadata: BundleMetadataRecord,
    upgrade_context: _FallbackUpgradeContext | None = None,
) -> ProcessResult:
    process_file = processor.process_file
    legacy_call = cast(Callable[..., ProcessResult], process_file)
    try:
        parameters = inspect.signature(process_file).parameters
    except (TypeError, ValueError):
        return legacy_call(preferred_input, dry_run=False)
    supports_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    meeting_metadata = _authoritative_meeting_metadata(metadata)
    meeting_context = _bundle_meeting_context(metadata)
    if upgrade_context is not None:
        archived_path = upgrade_context.archived_fallback_note_path
        if archived_path is None:
            raise ValueError("fallback upgrade archive path is missing")
        meeting_context["supersedes_note"] = _vault_relative_path(processor, archived_path).as_posix()
    optional_values: dict[str, object] = {
        "dry_run": False,
        "apply_retention": False,
        "meeting_metadata": meeting_metadata,
        "meeting_context": meeting_context,
    }
    source_note_aliases = _qualified_bundle_source_note_aliases(
        processor=processor,
        preferred_input=preferred_input,
        meeting_date=meeting_metadata.date,
        canonical_basename=meeting_metadata.canonical_basename,
    )
    if source_note_aliases is not None:
        if upgrade_context is not None and upgrade_context.fallback_source_alias is not None:
            clean_qualified = next(iter(source_note_aliases.values()))
            source_note_aliases[upgrade_context.fallback_source_alias.as_posix()] = clean_qualified
        optional_values["source_note_aliases"] = source_note_aliases
    kwargs = {name: value for name, value in optional_values.items() if supports_kwargs or name in parameters}
    return legacy_call(preferred_input, **kwargs)


def _apply_deferred_action_retention(processor: MeetingProcessor) -> None:
    config = getattr(processor, "config", None)
    if config is not None and config.dry_run:
        return
    apply_actions_retention = getattr(processor, "_apply_actions_retention", None)
    if callable(apply_actions_retention):
        apply_actions_retention()


def _qualified_bundle_source_note_aliases(
    *,
    processor: object,
    preferred_input: Path,
    meeting_date: str,
    canonical_basename: str,
) -> dict[str, str] | None:
    meetings_path = getattr(processor, "meetings_path", None)
    vault_path = getattr(processor, "vault_path", None)
    if not isinstance(meetings_path, Path) or not isinstance(vault_path, Path):
        return None
    try:
        relative_meetings_path = meetings_path.resolve().relative_to(vault_path.resolve())
    except (OSError, ValueError):
        return None
    if not relative_meetings_path.parts:
        return None
    canonical_path = meeting_output_path(
        meetings_path,
        meeting_date=meeting_date,
        basename=canonical_basename,
    )
    canonical_relative_path = canonical_path.resolve().relative_to(vault_path.resolve())
    return {(relative_meetings_path / preferred_input.name).as_posix(): (canonical_relative_path).as_posix()}


def render_bundle_execution_result(result: BundleExecutionResult) -> str:
    lines = [
        "meeting_bundle_process_mode: execute",
        f"meeting_bundle_process_executed_at: {result.executed_at.isoformat()}",
        f"meeting_bundle_process_bundle_root: {result.bundle_root}",
        f"meeting_bundle_process_candidates: {result.candidate_count}",
        f"meeting_bundle_process_processed: {result.processed_count}",
        f"meeting_bundle_process_skipped: {result.skipped_count}",
        f"meeting_bundle_process_failed: {result.failed_count}",
        f"meeting_bundle_process_manual_review_required: {result.manual_review_required_count}",
    ]
    for archive_path in result.upgrade_archived_note_paths:
        lines.append(f"meeting_bundle_process_upgrade_archived_note: {archive_path}")
    if result.output_mode == "validation":
        lines.append("meeting_bundle_process_output_mode: validation")
    for warning in result.warnings:
        lines.append(f"meeting_bundle_process_warning: {warning}")
    for item in result.items:
        metadata = item.plan_item.metadata
        lines.append(
            f"meeting_bundle_item: {item.status} "
            f'event_id="{metadata.occurrence_event_id or metadata.event_id or ""}" '
            f'subject="{metadata.subject}"'
        )
        lines.append(f"  outlook_metadata: {metadata.metadata_path}")
        lines.append(f"  bundle_note: {metadata.bundle_note_path}")
        if metadata.processor_handoff.preferred_input_path is not None:
            lines.append(f"  preferred_input: {metadata.processor_handoff.preferred_input_path}")
        if metadata.processor_handoff.preferred_input_source_name is not None:
            lines.append(f"  preferred_source: {metadata.processor_handoff.preferred_input_source_name}")
        if item.canonical_note_path is not None:
            lines.append(f"  canonical_output_file: {item.canonical_note_path}")
        if item.actions_file_path is not None:
            lines.append(f"  actions_output_file: {item.actions_file_path}")
        for reason in item.reasons:
            lines.append(f"  reason: {reason}")
    return "\n".join(lines)


def _plan_bundle_processing_item(
    *,
    metadata: BundleMetadataRecord,
    processor: MeetingProcessor,
    now: datetime,
) -> BundleProcessingPlanItem:
    reasons: list[str] = []
    preferred_input = metadata.processor_handoff.preferred_input_path

    identity_state = None
    if metadata.processed_marker_path is not None and identity_marker_entry_exists(metadata.processed_marker_path):
        identity_state = read_identity_state(
            metadata.processed_marker_path,
            metadata.end_at or metadata.start_at or datetime.min.replace(tzinfo=UTC),
        )
    legacy_fallback_upgrade = (
        identity_state is not None
        and identity_state.marker_kind == "processed"
        and identity_state.payload.get("source_type") == "meeting_sync_identity"
        and "schema_version" not in identity_state.payload
        and identity_state.payload.get("preferred_input_source_name") == "Copilot recap / AI summary"
        and identity_state.payload.get("upgrade_state") in {None, "awaiting_transcript"}
        and not _datetime_before(identity_state.retry_until, now)
    )
    upgrade_from_fallback = (
        identity_state is not None
        and identity_state.marker_kind == "processed"
        and (
            (
                identity_state.processing_source_kind == "fallback"
                and identity_state.upgrade_state == "awaiting_transcript"
                and not identity_state.is_terminal(now=now)
            )
            or legacy_fallback_upgrade
        )
        and metadata.processor_handoff.preferred_input_source_name in {"Teams .vtt transcript", "Teams transcript text"}
    )
    if (
        identity_state is not None
        and not upgrade_from_fallback
        and (identity_state.marker_kind == "processed" or identity_state.is_terminal(now=now))
    ):
        reasons.append("Durable processed marker indicates this bundle was already processed successfully.")
        return BundleProcessingPlanItem(decision="blocked", metadata=metadata, reasons=tuple(reasons))

    metadata_validation_reasons = _processor_ready_metadata_validation_reasons(metadata)
    if metadata_validation_reasons:
        return BundleProcessingPlanItem(
            decision="blocked",
            metadata=metadata,
            reasons=metadata_validation_reasons,
        )

    if (
        metadata.processor_handoff.preferred_input_source_name == "Copilot recap / AI summary"
        and metadata.fallback_not_before is not None
        and _datetime_before(now, metadata.fallback_not_before)
    ):
        reasons.append(f"Recap fallback is deferred until {metadata.fallback_not_before.isoformat()}.")
        return BundleProcessingPlanItem(decision="blocked", metadata=metadata, reasons=tuple(reasons))

    if preferred_input is None:
        reasons.append("Missing preferred processor input path in bundle metadata.")
        if metadata.available_sources() == ("Outlook calendar metadata",):
            reasons.append("Bundle is still calendar-only, so there is no processor-ready transcript artifact yet.")
        for source_name in metadata.permission_blocked_sources():
            reasons.append(f"Permission blocked retrieval of {source_name}.")
        return BundleProcessingPlanItem(decision="blocked", metadata=metadata, reasons=tuple(reasons))

    if preferred_input.suffix.lower() not in PROCESSOR_SUPPORTED_EXTENSIONS:
        reasons.append(
            f"Preferred processor input file type is unsupported by the existing processor: {preferred_input.suffix}"
        )
        return BundleProcessingPlanItem(decision="blocked", metadata=metadata, reasons=tuple(reasons))

    if not preferred_input.exists() or preferred_input.is_dir():
        reasons.append("Preferred processor input file does not exist on disk.")
        return BundleProcessingPlanItem(decision="blocked", metadata=metadata, reasons=tuple(reasons))

    skip_reason = None
    if processor.intake_state.is_under_intake(preferred_input):
        skip_reason = processor.skip_reason(preferred_input)
    if skip_reason is not None:
        reasons.append(
            f"Preferred processor input would currently be skipped by the existing processor: {skip_reason}."
        )
        return BundleProcessingPlanItem(decision="blocked", metadata=metadata, reasons=tuple(reasons))

    if upgrade_from_fallback:
        assert identity_state is not None
        try:
            identity_fields = _processed_marker_identity_fields(
                metadata=metadata,
                pending_payload=identity_state.payload,
            )
            selected_transcripts, _manual_review_required = _processed_marker_selected_transcripts(
                metadata=metadata,
                processing_source_kind="transcript",
                identity_fields=identity_fields,
            )
            _validated_upgrade_transcript_provenance(
                metadata=metadata,
                selected_transcripts=selected_transcripts,
            )
        except ValueError as exc:
            reasons.append(f"Fallback upgrade transcript or occurrence diagnostics are unsafe: {exc}.")
            return BundleProcessingPlanItem(decision="blocked", metadata=metadata, reasons=tuple(reasons))
        reasons.append("Preferred Teams transcript can safely upgrade the existing fallback meeting note.")
        return BundleProcessingPlanItem(
            decision="ready",
            metadata=metadata,
            reasons=tuple(reasons),
            upgrade_from_fallback=True,
        )

    reasons.append("Bundle has a processor-ready local artifact and can be handed to the existing intake processor.")
    return BundleProcessingPlanItem(decision="ready", metadata=metadata, reasons=tuple(reasons))


def _load_bundle_metadata_record(metadata_path: Path) -> BundleMetadataRecord:
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("expected top-level JSON object")

    raw_handoff = payload.get("processor_handoff")
    handoff_payload = raw_handoff if isinstance(raw_handoff, dict) else {}
    preferred_input_path = _path_or_none(handoff_payload.get("preferred_input_path"))
    preferred_input_source_name = _string_or_none(handoff_payload.get("preferred_input_source_name"))
    processed_marker_path = _validated_processed_marker_path(
        bundle_root=metadata_path.parent,
        path=_path_or_none(payload.get("processed_marker_path")),
    )

    raw_artifacts = payload.get("artifacts")
    artifacts: list[BundleArtifactRecord] = []
    if isinstance(raw_artifacts, list):
        for raw_artifact in raw_artifacts:
            if not isinstance(raw_artifact, dict):
                continue
            status = _artifact_status_or_default(raw_artifact.get("status"))
            raw_paths = raw_artifact.get("matched_paths")
            matched_paths: tuple[Path, ...] = ()
            if isinstance(raw_paths, list):
                matched_paths = tuple(Path(str(raw_path)) for raw_path in raw_paths if str(raw_path).strip())
            raw_validated_paths = raw_artifact.get("occurrence_validated_paths")
            occurrence_validated_paths: tuple[Path, ...] = ()
            if isinstance(raw_validated_paths, list):
                occurrence_validated_paths = tuple(
                    Path(str(raw_path)) for raw_path in raw_validated_paths if str(raw_path).strip()
                )
            artifacts.append(
                BundleArtifactRecord(
                    source_name=_string_or_default(raw_artifact.get("source_name"), "Unknown source"),
                    status=status,
                    detail=_string_or_none(raw_artifact.get("detail")),
                    matched_paths=matched_paths,
                    transcript_diagnostics=_dict_or_none(raw_artifact.get("transcript_diagnostics")),
                    occurrence_validated_paths=occurrence_validated_paths,
                )
            )

    preferred_source = preferred_input_source_name
    identity_datetime_keys = (
        "scheduled_start_at",
        "scheduled_end_at",
        "fallback_not_before",
        "retry_until",
        "first_seen_at",
        "selection_window_start_at",
        "selection_window_end_at",
    )
    identity_string_keys = (
        "primary_occurrence_event_id",
        "outlook_event_id",
        "teams_meeting_id",
        "identity_key",
    )
    parsed_identity_datetimes = {key: _aware_datetime_or_none(payload.get(key)) for key in identity_datetime_keys}
    present_identity_fields = frozenset(
        key for key in (*identity_datetime_keys, *identity_string_keys) if key in payload
    )
    invalid_identity_datetime_fields = frozenset(
        key for key in identity_datetime_keys if key in payload and parsed_identity_datetimes[key] is None
    )
    return BundleMetadataRecord(
        metadata_path=metadata_path,
        bundle_note_path=metadata_path.with_name(metadata_path.name.replace(" (outlook).json", " (bundle).md")),
        processed_marker_path=processed_marker_path,
        event_id=_string_or_none(payload.get("outlook_event_id")),
        subject=_string_or_none(payload.get("subject")) or "",
        start_at=parsed_identity_datetimes["scheduled_start_at"],
        teams_meeting_id=_string_or_none(payload.get("teams_meeting_id")),
        identity_key=_string_or_none(payload.get("identity_key")),
        source_type=_string_or_none(payload.get("source_type")),
        processor_handoff=BundleProcessorHandoff(
            preferred_input_path=preferred_input_path,
            preferred_input_source_name=preferred_input_source_name,
        ),
        artifacts=tuple(artifacts),
        end_at=parsed_identity_datetimes["scheduled_end_at"],
        fallback_not_before=parsed_identity_datetimes["fallback_not_before"],
        retry_until=parsed_identity_datetimes["retry_until"],
        first_seen_at=parsed_identity_datetimes["first_seen_at"],
        selection_window_start_at=parsed_identity_datetimes["selection_window_start_at"],
        selection_window_end_at=parsed_identity_datetimes["selection_window_end_at"],
        organizer=_string_or_none(payload.get("organizer")),
        attendees=_attendee_labels(payload.get("attendees")),
        join_url=_string_or_none(payload.get("join_url")),
        occurrence_event_id=_string_or_none(payload.get("primary_occurrence_event_id")),
        source_limitations=_source_limitations(
            payload=payload,
            artifacts=tuple(artifacts),
            preferred_source=preferred_source,
        ),
        present_identity_fields=present_identity_fields,
        invalid_identity_datetime_fields=invalid_identity_datetime_fields,
    )


def _metadata_paths_for_event_id(*, bundle_root: Path, event_id: str) -> tuple[Path, ...]:
    matches: list[Path] = []
    for metadata_path in sorted(bundle_root.glob("* (outlook).json")):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if _string_or_none(payload.get("outlook_event_id")) == event_id:
            matches.append(metadata_path)
    return tuple(matches)


def _update_payload_for_attached_transcript(*, payload: dict[str, object], attached_path: Path) -> None:
    payload["source_type"] = "manual_semi_manual_intake"
    payload["processor_handoff"] = {
        "preferred_input_path": str(attached_path),
        "preferred_input_source_name": "Manual / semi-manual intake",
    }
    raw_artifacts = payload.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    artifacts = [
        artifact
        for artifact in artifacts
        if not (isinstance(artifact, dict) and artifact.get("source_name") == "Manual / semi-manual intake")
    ]
    artifacts.append(
        {
            "source_name": "Manual / semi-manual intake",
            "status": "available",
            "detail": "Manually attached local processor input.",
            "matched_paths": [str(attached_path)],
        }
    )
    payload["artifacts"] = artifacts


def _count_blocked_reason(items: tuple[BundleProcessingPlanItem, ...], exact_reason: str) -> int:
    return sum(1 for item in items if item.decision == "blocked" and exact_reason in item.reasons)


def _count_blocked_with_prefix(items: tuple[BundleProcessingPlanItem, ...], prefix: str) -> int:
    return sum(
        1 for item in items if item.decision == "blocked" and any(reason.startswith(prefix) for reason in item.reasons)
    )


def _path_or_none(value: object) -> Path | None:
    text = _string_or_none(value)
    if text is None:
        return None
    return Path(text)


def _validated_processed_marker_path(*, bundle_root: Path, path: Path | None) -> Path | None:
    if path is None:
        return None
    if bundle_root.is_symlink():
        raise ValueError("bundle root must not be a symlink")
    identities_root = bundle_root / "_meeting_sync" / "identities"
    if ".." in path.parts:
        raise ValueError("processed marker path must not contain parent traversal")
    if path.is_symlink():
        raise ValueError("processed marker path must not be a symlink")
    try:
        relative_path = path.relative_to(identities_root)
    except ValueError as exc:
        candidate = identities_root / path.name
        if not _same_existing_path(path, candidate):
            raise ValueError(f"processed marker path must be directly under {identities_root}") from exc
        path = candidate
        relative_path = Path(path.name)
    if len(relative_path.parts) != 1 or relative_path.parts[0] in {"", ".", ".."} or path.suffix.casefold() != ".json":
        raise ValueError(f"processed marker path must be a direct JSON entry under {identities_root}")
    for ancestor in (bundle_root / "_meeting_sync", identities_root):
        if ancestor.is_symlink():
            raise ValueError("processed marker path ancestor must not be a symlink")
    if path.is_symlink():
        raise ValueError("processed marker path must not be a symlink")
    if path.exists() and not path.is_file():
        raise ValueError("processed marker path must be a regular JSON file")
    return path


def _same_existing_path(first: Path, second: Path) -> bool:
    try:
        return first.resolve(strict=True) == second.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def _datetime_or_none(value: object) -> datetime | None:
    text = _string_or_none(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except (OverflowError, ValueError):
        return None


def _aware_datetime_or_none(value: object) -> datetime | None:
    parsed = _datetime_or_none(value)
    if parsed is None or parsed.tzinfo is None:
        return None
    try:
        if parsed.utcoffset() is None:
            return None
        parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
    return parsed


def _expected_local_transcript_stem(metadata: BundleMetadataRecord) -> str | None:
    if metadata.start_at is None:
        return None
    title = metadata.subject.strip().replace("/", "-").replace(":", "-").replace("\\", "-") or "Meeting"
    return f"{metadata.start_at.date().isoformat()} - Teams - {title}"


def _bundle_next_step(metadata: BundleMetadataRecord) -> str:
    del metadata
    return "add or attach a local .vtt, .md, or .docx transcript, then rerun process-bundles --dry-run"


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _string_or_default(value: object, default: str) -> str:
    return _string_or_none(value) or default


def _artifact_status_or_default(value: object) -> ArtifactStatus:
    if value in {"available", "missing", "permission_blocked", "not_attempted"}:
        return cast(ArtifactStatus, value)
    return "not_attempted"


def _execution_result_item_from_process_result(
    *,
    plan_item: BundleProcessingPlanItem,
    process_result: ProcessResult,
    processed_marker_path: Path | None = None,
    removed_paths: tuple[Path, ...] = (),
    cleanup_errors: tuple[str, ...] = (),
) -> BundleExecutionResultItem:
    if process_result.processed:
        reasons = ["Bundle preferred input was processed successfully."]
        if processed_marker_path is not None:
            reasons.append(f"Durable processed marker written to {processed_marker_path}.")
        if removed_paths:
            reasons.append("Removed bundle staging paths: " + ", ".join(str(path) for path in removed_paths) + ".")
        for cleanup_error in cleanup_errors:
            reasons.append(f"Bundle staging cleanup warning: {cleanup_error}")
        return BundleExecutionResultItem(
            status="processed",
            plan_item=plan_item,
            reasons=tuple(reasons),
            canonical_note_path=process_result.canonical_note_path,
            actions_file_path=process_result.actions_file_path,
        )
    if process_result.skip_reason is not None:
        return BundleExecutionResultItem(
            status="skipped",
            plan_item=plan_item,
            reasons=(f"Processor skipped preferred input: {process_result.skip_reason}.",),
        )
    return BundleExecutionResultItem(
        status="failed",
        plan_item=plan_item,
        reasons=("Processor returned without processing but did not provide a skip reason.",),
    )


def _write_durable_processed_marker(
    *,
    metadata: BundleMetadataRecord,
    process_result: ProcessResult,
    cleanup_paths: tuple[Path, ...],
    processor: MeetingProcessor,
    processed_at: datetime,
    identity_marker_snapshot: _IdentityMarkerSnapshot | None,
    marker_transaction: IdentityMarkerTransaction | None,
    upgrade_context: _FallbackUpgradeContext | None = None,
) -> Path | None:
    if metadata.processed_marker_path is None:
        return None
    if identity_marker_snapshot is None:
        raise ValueError("processed marker write requires a captured identity marker snapshot")
    if marker_transaction is None or marker_transaction.path != metadata.processed_marker_path:
        raise ValueError("processed marker write requires the active marker transaction")
    marker_path = metadata.processed_marker_path
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    pending_payload = identity_marker_snapshot.pending_payload
    processing_source_kind = _processing_source_kind(metadata)
    preserved_identity_fields = _processed_marker_identity_fields(
        metadata=metadata,
        pending_payload=pending_payload,
    )
    normalized_processed_at = _normalized_aware_datetime(processed_at, field_name="processed_at")
    retry_until = _processed_marker_retry_until(
        identity_fields=preserved_identity_fields,
        processing_source_kind=processing_source_kind,
    )
    if processing_source_kind == "fallback":
        _validate_fallback_chronology(
            metadata=metadata,
            identity_fields=preserved_identity_fields,
            processed_at=normalized_processed_at,
        )
    selected_transcripts, transcript_manual_review_required = _processed_marker_selected_transcripts(
        metadata=metadata,
        processing_source_kind=processing_source_kind,
        identity_fields=preserved_identity_fields,
    )
    canonical_note_path, canonical_note_bytes = _trusted_canonical_note_output(
        processor=processor,
        metadata=metadata,
        process_result=process_result,
    )
    canonical_note_sha256 = hashlib.sha256(canonical_note_bytes).hexdigest()
    payload: dict[str, object] = {
        "source_type": "meeting_bundle_processed",
        "schema_version": 2,
        "processing_source_kind": processing_source_kind,
        "upgrade_state": (
            "awaiting_transcript"
            if processing_source_kind == "fallback"
            else "manual_review_required"
            if transcript_manual_review_required
            else "terminal"
        ),
        "retry_until": retry_until,
        "processed_at": normalized_processed_at.isoformat(),
        "subject": metadata.subject,
        "bundle_note_path": str(metadata.bundle_note_path),
        "outlook_metadata_path": str(metadata.metadata_path),
        "preferred_input_path": (
            str(metadata.processor_handoff.preferred_input_path)
            if metadata.processor_handoff.preferred_input_path is not None
            else None
        ),
        "preferred_input_source_name": metadata.processor_handoff.preferred_input_source_name,
        "canonical_note_path": str(canonical_note_path),
        "canonical_note_sha256": canonical_note_sha256,
        "actions_file_path": str(process_result.actions_file_path)
        if process_result.actions_file_path is not None
        else None,
        "cleanup_paths": [str(path) for path in cleanup_paths],
        "selected_transcripts": selected_transcripts,
        "artifacts": [
            {
                "source_name": artifact.source_name,
                "status": artifact.status,
                "matched_paths": [str(path) for path in artifact.matched_paths],
            }
            for artifact in metadata.artifacts
        ],
        **preserved_identity_fields,
    }
    if upgrade_context is not None:
        if upgrade_context.archived_fallback_note_path is None:
            raise ValueError("fallback upgrade archive path is missing")
        payload.update(
            {
                "processing_source_kind": "transcript",
                "upgrade_state": "terminal",
                "supersedes_note": str(upgrade_context.canonical_note_path),
                "archived_fallback_note_path": str(upgrade_context.archived_fallback_note_path),
                "previous_fallback_note_sha256": upgrade_context.previous_canonical_sha256,
                "previous_fallback_source_type": upgrade_context.previous_payload.get("source_type"),
                "previous_fallback_processing_source_kind": upgrade_context.previous_payload.get(
                    "processing_source_kind"
                ),
                "previous_fallback_upgrade_state": upgrade_context.previous_payload.get("upgrade_state"),
                "previous_fallback_processed_at": upgrade_context.previous_payload.get("processed_at"),
                "previous_fallback_retry_until": upgrade_context.previous_payload.get("retry_until"),
                "validated_transcript_provenance": upgrade_context.validated_transcript_provenance,
            }
        )
    expected_marker_content = identity_marker_snapshot.content if identity_marker_snapshot.existed else None
    marker_transaction.compare_and_write(expected_marker_content, payload)
    return marker_path


def _capture_identity_marker_snapshot(
    metadata: BundleMetadataRecord,
    *,
    marker_transaction: IdentityMarkerTransaction | None,
    allow_fallback_upgrade: bool = False,
) -> _IdentityMarkerSnapshot | None:
    marker_path = metadata.processed_marker_path
    if marker_path is None:
        return None
    if marker_transaction is None or marker_transaction.path != marker_path:
        raise ValueError("identity marker snapshot requires the active marker transaction")
    content = marker_transaction.read_bytes_or_missing()
    if content is None:
        return _IdentityMarkerSnapshot(path=marker_path, existed=False, content=None, pending_payload={})
    payload = (
        _fallback_upgrade_marker_payload_from_bytes(content)
        if allow_fallback_upgrade
        else _pending_marker_payload_from_bytes(content)
    )
    return _IdentityMarkerSnapshot(
        path=marker_path,
        existed=True,
        content=content,
        pending_payload=payload,
    )


def _pending_marker_payload_from_bytes(content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("identity marker is not valid pending-marker JSON") from exc
    if not isinstance(payload, dict) or payload.get("source_type") != "meeting_sync_pending":
        raise ValueError("identity marker is no longer pending")
    return payload


def _fallback_upgrade_marker_payload_from_bytes(content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("identity marker is not valid fallback-marker JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("identity marker is not a fallback marker")
    source_type = payload.get("source_type")
    if source_type not in {"meeting_bundle_processed", "meeting_sync_identity"}:
        raise ValueError("identity marker is no longer a processed fallback marker")
    if source_type == "meeting_sync_identity" and "schema_version" not in payload:
        if payload.get("preferred_input_source_name") != "Copilot recap / AI summary":
            raise ValueError("legacy identity marker is not an allowlisted recap fallback")
        if payload.get("processing_source_kind") not in {None, "fallback"}:
            raise ValueError("legacy identity marker has an unsafe processing source")
        if payload.get("upgrade_state") not in {None, "awaiting_transcript"}:
            raise ValueError("legacy identity marker no longer awaits a transcript upgrade")
        return payload
    if payload.get("processing_source_kind") != "fallback":
        raise ValueError("identity marker is no longer a fallback marker")
    if payload.get("upgrade_state") != "awaiting_transcript":
        raise ValueError("identity marker no longer awaits a transcript upgrade")
    return payload


def _validate_fallback_upgrade(
    *,
    metadata: BundleMetadataRecord,
    processor: MeetingProcessor,
    identity_marker_snapshot: _IdentityMarkerSnapshot | None,
) -> tuple[_FallbackUpgradeContext | None, str | None, Path | None]:
    if identity_marker_snapshot is None:
        raise ValueError("fallback upgrade requires an identity marker")
    previous_payload = identity_marker_snapshot.pending_payload
    meetings_root = getattr(processor, "meetings_path", None)
    if not isinstance(meetings_root, Path) or meetings_root.is_symlink():
        raise ValueError("fallback upgrade meetings root is unsafe")
    meeting_metadata = _authoritative_meeting_metadata(metadata)
    expected_path = meeting_output_path(
        meetings_root,
        meeting_date=meeting_metadata.date,
        basename=meeting_metadata.canonical_basename,
    )
    legacy_path = meetings_root / meeting_metadata.canonical_basename
    raw_marker_path = _string_or_none(previous_payload.get("canonical_note_path"))
    if raw_marker_path is None:
        return None, "canonical_note_path_missing_or_unexpected", expected_path
    stored_canonical_path = Path(raw_marker_path)
    if ".." in stored_canonical_path.parts or stored_canonical_path.is_symlink():
        return None, "canonical_note_path_missing_or_unexpected", expected_path
    legacy_match = stored_canonical_path == legacy_path or _same_existing_path(stored_canonical_path, legacy_path)
    if legacy_match and not expected_path.exists() and legacy_path.exists():
        canonical_path = legacy_path
    elif legacy_match and expected_path.exists():
        canonical_path = expected_path
    elif stored_canonical_path == expected_path or _same_existing_path(stored_canonical_path, expected_path):
        canonical_path = expected_path
    else:
        return None, "canonical_note_path_missing_or_unexpected", expected_path
    if canonical_path.is_symlink() or not canonical_path.exists() or not canonical_path.is_file():
        return None, "canonical_note_path_missing_or_unsafe", expected_path

    expected_hash = _string_or_none(previous_payload.get("canonical_note_sha256"))
    if (
        expected_hash is None
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_hash)
    ):
        return None, "canonical_note_hash_missing_or_invalid", canonical_path
    if not canonical_matches_hash(canonical_path, expected_hash):
        return None, "canonical_note_hash_mismatch", canonical_path

    try:
        identity_fields = _processed_marker_identity_fields(
            metadata=metadata,
            pending_payload=previous_payload,
        )
        selected_transcripts, manual_review_required = _processed_marker_selected_transcripts(
            metadata=metadata,
            processing_source_kind="transcript",
            identity_fields=identity_fields,
        )
        if manual_review_required:
            raise ValueError("transcript diagnostics require manual review")
        validated_provenance = _validated_upgrade_transcript_provenance(
            metadata=metadata,
            selected_transcripts=selected_transcripts,
        )
    except ValueError:
        return None, "transcript_provenance_changed_or_invalid", canonical_path

    fallback_source_alias = _fallback_source_alias(
        processor=processor,
        previous_payload=previous_payload,
    )
    return (
        _FallbackUpgradeContext(
            canonical_note_path=canonical_path,
            archived_fallback_note_path=None,
            previous_payload=dict(previous_payload),
            previous_canonical_sha256=expected_hash.casefold(),
            fallback_source_alias=fallback_source_alias,
            validated_transcript_provenance=validated_provenance,
        ),
        None,
        expected_path,
    )


def _write_upgrade_manual_review_marker(
    *,
    identity_marker_snapshot: _IdentityMarkerSnapshot | None,
    marker_transaction: IdentityMarkerTransaction | None,
    reason: str,
) -> None:
    if identity_marker_snapshot is None or identity_marker_snapshot.content is None:
        raise ValueError("manual-review transition requires an existing fallback marker")
    if marker_transaction is None or marker_transaction.path != identity_marker_snapshot.path:
        raise ValueError("manual-review transition requires the active marker transaction")
    payload = dict(identity_marker_snapshot.pending_payload)
    previous_source_type = payload.get("source_type")
    previous_processing_source_kind = payload.get("processing_source_kind")
    previous_upgrade_state = payload.get("upgrade_state")
    payload.update(
        {
            "source_type": "meeting_bundle_processed",
            "schema_version": 2,
            "processing_source_kind": "fallback",
            "upgrade_state": "manual_review_required",
            "manual_review_reason": reason,
            "previous_fallback_source_type": previous_source_type,
            "previous_fallback_processing_source_kind": previous_processing_source_kind,
            "previous_fallback_upgrade_state": previous_upgrade_state,
        }
    )
    marker_transaction.compare_and_write(identity_marker_snapshot.content, payload)


def _fallback_source_alias(
    *,
    processor: MeetingProcessor,
    previous_payload: dict[str, object],
) -> Path | None:
    previous_input = _path_or_none(previous_payload.get("preferred_input_path"))
    if previous_input is None:
        return None
    meetings_path = getattr(processor, "meetings_path", None)
    vault_path = getattr(processor, "vault_path", None)
    if not isinstance(meetings_path, Path) or not isinstance(vault_path, Path):
        return None
    try:
        relative_meetings = meetings_path.resolve().relative_to(vault_path.resolve())
    except (OSError, ValueError):
        return None
    return relative_meetings / previous_input.name


def _meeting_upgrade_archive_root(processor: MeetingProcessor) -> Path:
    archive_path = getattr(processor, "archive_path", None)
    if not isinstance(archive_path, Path):
        raise ValueError("processor does not expose a trusted intake archive root")
    return archive_path / "Meeting Upgrades"


def _upgrade_input_revalidation_reason(
    *,
    metadata: BundleMetadataRecord,
    upgrade_context: _FallbackUpgradeContext,
) -> str | None:
    if not canonical_matches_hash(
        upgrade_context.canonical_note_path,
        upgrade_context.previous_canonical_sha256,
    ):
        return "canonical_note_hash_changed_before_processor"
    try:
        identity_fields = _processed_marker_identity_fields(
            metadata=metadata,
            pending_payload=upgrade_context.previous_payload,
        )
        selected_transcripts, manual_review_required = _processed_marker_selected_transcripts(
            metadata=metadata,
            processing_source_kind="transcript",
            identity_fields=identity_fields,
        )
        if manual_review_required:
            return "transcript_provenance_changed_or_invalid"
        current_provenance = _validated_upgrade_transcript_provenance(
            metadata=metadata,
            selected_transcripts=selected_transcripts,
        )
    except ValueError:
        return "transcript_provenance_changed_or_invalid"
    if current_provenance != upgrade_context.validated_transcript_provenance:
        return "transcript_provenance_changed_or_invalid"
    return None


def _vault_relative_path(processor: MeetingProcessor, path: Path) -> Path:
    vault_path = getattr(processor, "vault_path", None)
    if not isinstance(vault_path, Path):
        return Path(path.name)
    try:
        return path.resolve().relative_to(vault_path.resolve())
    except (OSError, ValueError):
        return Path(path.name)


def _migrate_upgrade_action_note(
    *,
    process_result: ProcessResult,
    output_snapshots: tuple[_OutputFileSnapshot, _OutputFileSnapshot],
    upgrade_context: _FallbackUpgradeContext,
    processor: MeetingProcessor,
) -> ProcessResult:
    fallback_source_alias = upgrade_context.fallback_source_alias
    if fallback_source_alias is None:
        return process_result
    _canonical_snapshot, actions_snapshot = output_snapshots
    if not actions_snapshot.path.exists():
        return process_result
    current = _read_regular_file_bytes(actions_snapshot.path)
    clean_note = _vault_relative_path(processor, upgrade_context.canonical_note_path)
    migrated = migrate_action_backlinks(
        current,
        old_note=fallback_source_alias,
        clean_note=clean_note,
    )
    if migrated == current:
        return process_result
    _atomic_restore_bytes(actions_snapshot.path, migrated)
    return replace(process_result, actions_file_path=actions_snapshot.path)


def _revalidate_identity_marker_snapshot(
    snapshot: _IdentityMarkerSnapshot | None,
    *,
    marker_transaction: IdentityMarkerTransaction | None,
    allow_fallback_upgrade: bool = False,
) -> None:
    if snapshot is None:
        return
    if marker_transaction is None or marker_transaction.path != snapshot.path:
        raise ValueError("identity marker revalidation requires the active marker transaction")
    expected_content = snapshot.content if snapshot.existed else None
    marker_transaction.compare(expected_content)
    if snapshot.content is not None:
        if allow_fallback_upgrade:
            _fallback_upgrade_marker_payload_from_bytes(snapshot.content)
        else:
            _pending_marker_payload_from_bytes(snapshot.content)


def _processing_source_kind(metadata: BundleMetadataRecord) -> Literal["fallback", "transcript", "manual"]:
    source_name = metadata.processor_handoff.preferred_input_source_name
    normalized_source_name = " ".join((source_name or "").casefold().split())
    if "copilot recap" in normalized_source_name or "ai summary" in normalized_source_name:
        return "fallback"
    if normalized_source_name in {"teams .vtt transcript", "teams transcript text"}:
        return "transcript"
    return "manual"


def _processed_marker_retry_until(
    *,
    identity_fields: dict[str, object],
    processing_source_kind: Literal["fallback", "transcript", "manual"],
) -> str | None:
    retry_until = cast(str | None, identity_fields.get("retry_until"))
    if processing_source_kind == "fallback" and retry_until is None:
        raise ValueError("fallback processing requires a valid timezone-aware retry_until")
    return retry_until


def _processed_marker_identity_fields(
    *,
    metadata: BundleMetadataRecord,
    pending_payload: dict[str, object],
) -> dict[str, object]:
    fields: dict[str, object] = {}
    datetime_metadata = {
        "scheduled_start_at": metadata.start_at,
        "scheduled_end_at": metadata.end_at,
        "selection_window_start_at": metadata.selection_window_start_at,
        "selection_window_end_at": metadata.selection_window_end_at,
        "first_seen_at": metadata.first_seen_at,
        "retry_until": metadata.retry_until,
    }
    for key, datetime_metadata_value in datetime_metadata.items():
        datetime_value = _reconciled_identity_datetime(
            key=key,
            pending_payload=pending_payload,
            metadata_value=datetime_metadata_value,
            metadata_present=key in metadata.present_identity_fields,
            metadata_invalid=key in metadata.invalid_identity_datetime_fields,
        )
        if datetime_value is not None:
            fields[key] = datetime_value.isoformat()

    string_metadata = {
        "primary_occurrence_event_id": (
            metadata.occurrence_event_id or metadata.event_id,
            "primary_occurrence_event_id" in metadata.present_identity_fields
            or "outlook_event_id" in metadata.present_identity_fields,
        ),
        "identity_key": (metadata.identity_key, "identity_key" in metadata.present_identity_fields),
        "teams_meeting_id": (metadata.teams_meeting_id, "teams_meeting_id" in metadata.present_identity_fields),
        "outlook_event_id": (metadata.event_id, "outlook_event_id" in metadata.present_identity_fields),
    }
    for key, (string_metadata_value, metadata_present) in string_metadata.items():
        string_value = _reconciled_identity_string(
            key=key,
            pending_payload=pending_payload,
            metadata_value=string_metadata_value,
            metadata_present=metadata_present,
        )
        if string_value is not None:
            fields[key] = string_value
    return fields


def _reconciled_identity_datetime(
    *,
    key: str,
    pending_payload: dict[str, object],
    metadata_value: datetime | None,
    metadata_present: bool,
    metadata_invalid: bool,
) -> datetime | None:
    pending_present = key in pending_payload
    pending_value = (
        _normalized_datetime_value(pending_payload.get(key), field_name=f"pending {key}") if pending_present else None
    )
    if metadata_invalid:
        raise ValueError(f"bundle metadata {key} is not a valid timezone-aware datetime")
    normalized_metadata = (
        _normalized_aware_datetime(metadata_value, field_name=f"bundle metadata {key}")
        if metadata_present and metadata_value is not None
        else None
    )
    if metadata_present and metadata_value is None:
        raise ValueError(f"bundle metadata {key} is not a valid timezone-aware datetime")
    if pending_present and normalized_metadata is not None and pending_value != normalized_metadata:
        raise ValueError(f"pending identity field {key} does not match bundle metadata")
    return pending_value if pending_present else normalized_metadata


def _reconciled_identity_string(
    *,
    key: str,
    pending_payload: dict[str, object],
    metadata_value: str | None,
    metadata_present: bool,
) -> str | None:
    pending_present = key in pending_payload
    pending_value = _string_or_none(pending_payload.get(key)) if pending_present else None
    if pending_present and pending_value is None:
        raise ValueError(f"pending identity field {key} is invalid")
    if metadata_present and metadata_value is None:
        raise ValueError(f"bundle metadata {key} is invalid")
    if pending_present and metadata_present and pending_value != metadata_value:
        raise ValueError(f"pending identity field {key} does not match bundle metadata")
    return pending_value if pending_present else metadata_value


def _validate_fallback_chronology(
    *,
    metadata: BundleMetadataRecord,
    identity_fields: dict[str, object],
    processed_at: datetime,
) -> None:
    scheduled_end = _normalized_datetime_value(
        identity_fields.get("scheduled_end_at"),
        field_name="scheduled_end_at",
    )
    fallback_not_before = _normalized_aware_datetime(
        metadata.fallback_not_before,
        field_name="fallback_not_before",
    )
    retry_until = _normalized_datetime_value(
        identity_fields.get("retry_until"),
        field_name="retry_until",
    )
    if not scheduled_end <= fallback_not_before < retry_until:
        raise ValueError("fallback chronology requires scheduled_end_at <= fallback_not_before < retry_until")
    if processed_at > retry_until:
        raise ValueError("fallback processing time is after retry_until")


def _processed_marker_selected_transcripts(
    *,
    metadata: BundleMetadataRecord,
    processing_source_kind: Literal["fallback", "transcript", "manual"],
    identity_fields: dict[str, object],
) -> tuple[list[dict[str, str]], bool]:
    if processing_source_kind != "transcript":
        return [], False

    preferred_source_name = metadata.processor_handoff.preferred_input_source_name
    preferred_artifacts = [
        artifact
        for artifact in metadata.artifacts
        if artifact.source_name == preferred_source_name
        and (
            metadata.processor_handoff.preferred_input_path is None
            or metadata.processor_handoff.preferred_input_path in artifact.matched_paths
        )
    ]
    if len(preferred_artifacts) != 1:
        raise ValueError("transcript processing requires exactly one preferred transcript artifact")
    diagnostics = preferred_artifacts[0].transcript_diagnostics
    if diagnostics is None:
        return [], True
    if not isinstance(diagnostics, dict):
        raise ValueError("preferred artifact transcript diagnostics must be an object")
    candidate_count = diagnostics.get("candidate_count")
    if type(candidate_count) is not int or candidate_count <= 0:
        raise ValueError("preferred transcript diagnostics candidate_count must be a positive integer")
    assignment_conflicts = diagnostics.get("assignment_conflicts", [])
    if not isinstance(assignment_conflicts, list):
        raise ValueError("preferred transcript diagnostics assignment_conflicts must be a list")
    if assignment_conflicts:
        raise ValueError("preferred transcript diagnostics contain assignment conflicts")
    _validate_transcript_diagnostic_occurrence(metadata=metadata, diagnostics=diagnostics)

    raw_selected = diagnostics.get("selected_transcripts")
    if not isinstance(raw_selected, list):
        raise ValueError("selected transcript diagnostics must be a list")
    if not raw_selected:
        raise ValueError("selected transcript diagnostics must not be empty")
    if candidate_count < len(raw_selected):
        raise ValueError("preferred transcript candidate_count is smaller than selected transcript count")
    selection_window_start = _normalized_datetime_value(
        identity_fields.get("selection_window_start_at"),
        field_name="selection_window_start_at",
    )
    selection_window_end = _normalized_datetime_value(
        identity_fields.get("selection_window_end_at"),
        field_name="selection_window_end_at",
    )
    if selection_window_end < selection_window_start:
        raise ValueError("selection window end is before selection window start")
    selected: list[tuple[datetime, str, dict[str, str]]] = []
    seen_ids: set[str] = set()
    for raw_item in raw_selected:
        if not isinstance(raw_item, dict):
            raise ValueError("selected transcript diagnostics must be objects")
        modern_id = _string_or_none(raw_item.get("id"))
        legacy_id = _string_or_none(raw_item.get("transcript_id"))
        if modern_id is not None and legacy_id is not None and modern_id != legacy_id:
            raise ValueError("selected transcript diagnostic contains conflicting IDs")
        transcript_id = modern_id or legacy_id
        if transcript_id is None:
            raise ValueError("selected transcript diagnostic requires an ID")
        if transcript_id in seen_ids:
            raise ValueError("selected transcript IDs must be unique")
        created_at = _normalized_datetime_value(
            raw_item.get("created_at"),
            field_name="selected transcript created_at",
        )
        if not selection_window_start <= created_at <= selection_window_end:
            raise ValueError("selected transcript creation timestamp is outside the selection window")
        seen_ids.add(transcript_id)
        selected.append(
            (
                created_at,
                transcript_id,
                {
                    "id": transcript_id,
                    "created_at": created_at.isoformat(),
                },
            )
        )
    selected.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in selected], False


def _validate_transcript_diagnostic_occurrence(
    *,
    metadata: BundleMetadataRecord,
    diagnostics: dict[str, object],
) -> None:
    for key, expected in (
        ("occurrence_start_at", metadata.start_at),
        ("occurrence_end_at", metadata.end_at),
    ):
        if key not in diagnostics:
            raise ValueError(f"preferred transcript diagnostics require {key}")
        parsed = _normalized_datetime_value(diagnostics.get(key), field_name=key)
        normalized_expected = _normalized_aware_datetime(expected, field_name=f"bundle {key}")
        if parsed != normalized_expected:
            raise ValueError(f"preferred transcript diagnostics {key} does not match the bundle occurrence")


def _validated_upgrade_transcript_provenance(
    *,
    metadata: BundleMetadataRecord,
    selected_transcripts: list[dict[str, str]],
) -> dict[str, object]:
    preferred_path = metadata.processor_handoff.preferred_input_path
    preferred_source_name = metadata.processor_handoff.preferred_input_source_name
    if preferred_path is None:
        raise ValueError("upgrade transcript provenance requires a preferred transcript path")
    artifacts = [
        artifact
        for artifact in metadata.artifacts
        if artifact.source_name == preferred_source_name and preferred_path in artifact.matched_paths
    ]
    if len(artifacts) != 1:
        raise ValueError("upgrade transcript provenance requires one preferred transcript artifact")
    if preferred_path not in artifacts[0].occurrence_validated_paths:
        raise ValueError("preferred transcript path was not serialized as occurrence validated")
    event_id = metadata.occurrence_event_id or metadata.event_id
    if event_id is None or metadata.start_at is None or metadata.end_at is None:
        raise ValueError("upgrade transcript provenance requires authoritative occurrence identity and times")
    provenance = matching_provenance(
        preferred_path,
        event_id=event_id,
        occurrence_start_at=metadata.start_at,
        occurrence_end_at=metadata.end_at,
    )
    if provenance is None:
        raise ValueError("preferred transcript has no matching current-content provenance")
    normalized_provenance_transcripts: list[dict[str, str]] = []
    for record in provenance["transcripts"]:
        created_at = record["created_at"]
        if created_at is None:
            raise ValueError("upgrade transcript provenance requires selected transcript timestamps")
        normalized_provenance_transcripts.append(
            {
                "id": record["id"],
                "created_at": _normalized_datetime_value(
                    created_at,
                    field_name="provenance transcript created_at",
                ).isoformat(),
            }
        )
    normalized_provenance_transcripts.sort(key=lambda item: (item["created_at"], item["id"]))
    if normalized_provenance_transcripts != selected_transcripts:
        raise ValueError("selected transcript diagnostics do not match validated provenance")
    return {
        "path": str(preferred_path),
        "event_id": provenance["event_id"],
        "occurrence_start_at": provenance["occurrence_start_at"],
        "occurrence_end_at": provenance["occurrence_end_at"],
        "transcripts": normalized_provenance_transcripts,
        "content_sha256": provenance["content_sha256"],
    }


def _normalized_datetime_value(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} requires a timezone-aware datetime")
    parsed = _aware_datetime_or_none(value)
    if parsed is None:
        raise ValueError(f"{field_name} requires a timezone-aware datetime")
    return _normalized_aware_datetime(parsed, field_name=field_name)


def _normalized_aware_datetime(value: datetime | None, *, field_name: str) -> datetime:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} requires a timezone-aware datetime")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} requires a safely normalizable datetime") from exc


def _trusted_canonical_note_output(
    *,
    processor: MeetingProcessor,
    metadata: BundleMetadataRecord,
    process_result: ProcessResult,
) -> tuple[Path, bytes]:
    canonical_note_path = process_result.canonical_note_path
    if canonical_note_path is None:
        raise ValueError("processed bundle did not return a canonical note path")
    meetings_root = getattr(processor, "meetings_path", None)
    if not isinstance(meetings_root, Path):
        raise ValueError("processor does not expose a trusted meetings output root")
    meeting_metadata = _authoritative_meeting_metadata(metadata)
    expected_path = meeting_output_path(
        meetings_root,
        meeting_date=meeting_metadata.date,
        basename=meeting_metadata.canonical_basename,
    )
    if canonical_note_path != expected_path:
        raise ValueError("canonical note path does not match the trusted meetings output")
    if meetings_root.is_symlink():
        raise ValueError("trusted meetings output root must not be a symlink")
    if not meetings_root.exists() or not meetings_root.is_dir():
        raise ValueError("trusted meetings output root does not exist")
    current = meetings_root
    for part in expected_path.relative_to(meetings_root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("canonical note path contains a symlink")
    if not canonical_note_path.exists() or not canonical_note_path.is_file():
        raise ValueError("canonical note does not exist in the trusted meetings output")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(canonical_note_path, flags)
    except OSError as exc:
        raise ValueError("canonical note could not be opened without following symlinks") from exc
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise ValueError("canonical note is not a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return canonical_note_path, content


def _cleanup_bundle_staging(metadata: BundleMetadataRecord) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    removed_paths: list[Path] = []
    cleanup_errors: list[str] = []
    for path in _bundle_cleanup_paths(metadata):
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError as exc:
            cleanup_errors.append(f"{path}: {exc}")
            continue
        removed_paths.append(path)
    return tuple(removed_paths), tuple(cleanup_errors)


def _capture_bundle_staging(
    *,
    metadata: BundleMetadataRecord,
    processor: MeetingProcessor,
) -> tuple[_StagingFileSnapshot, ...]:
    snapshots: list[_StagingFileSnapshot] = []
    intake_state = getattr(processor, "intake_state", None)
    for path in _bundle_cleanup_paths(metadata):
        try:
            if not path.exists() or not path.is_file() or path.is_symlink():
                continue
            content = path.read_bytes()
        except OSError:
            continue

        generated_archive_path: Path | None = None
        sidecar_path: Path | None = None
        sidecar_content: bytes | None = None
        if intake_state is not None:
            try:
                is_under_intake = intake_state.is_under_intake(path)
            except (AttributeError, OSError, TypeError, ValueError):
                is_under_intake = False
            if is_under_intake:
                try:
                    archive_path = intake_state.archive_destination(path)
                    if not archive_path.exists():
                        generated_archive_path = archive_path
                except (AttributeError, OSError, TypeError, ValueError):
                    generated_archive_path = None
                if path.suffix.lower() == ".vtt":
                    try:
                        sidecar_path = intake_state.vtt_sidecar_path(path)
                    except AttributeError:
                        sidecar_path = None
                    except (OSError, TypeError, ValueError) as exc:
                        raise ValueError("VTT intake sidecar path could not be determined safely") from exc
                    if sidecar_path is not None and sidecar_path.is_symlink():
                        raise ValueError("VTT intake sidecar must not be a symlink")
                    if sidecar_path is not None and sidecar_path.exists():
                        if not sidecar_path.is_file():
                            raise ValueError("VTT intake sidecar must be a regular file")
                        sidecar_content = _read_regular_file_bytes(sidecar_path)
        snapshots.append(
            _StagingFileSnapshot(
                path=path,
                content=content,
                generated_archive_path=generated_archive_path,
                sidecar_path=sidecar_path,
                sidecar_content=sidecar_content,
            )
        )
    return tuple(snapshots)


def _restore_bundle_staging(
    snapshots: tuple[_StagingFileSnapshot, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    for snapshot in snapshots:
        if snapshot.generated_archive_path is not None and snapshot.generated_archive_path.is_file():
            try:
                snapshot.generated_archive_path.unlink()
            except OSError as exc:
                errors.append(f"{snapshot.generated_archive_path}: {exc}")
        if snapshot.sidecar_path is not None:
            try:
                if snapshot.sidecar_content is None:
                    if snapshot.sidecar_path.is_symlink() or snapshot.sidecar_path.exists():
                        if snapshot.sidecar_path.is_dir() and not snapshot.sidecar_path.is_symlink():
                            raise OSError("transaction-created VTT sidecar is a directory")
                        snapshot.sidecar_path.unlink()
                        _fsync_parent_directory(snapshot.sidecar_path.parent)
                else:
                    current_sidecar_content = (
                        _read_regular_file_bytes(snapshot.sidecar_path)
                        if snapshot.sidecar_path.exists() and not snapshot.sidecar_path.is_symlink()
                        else None
                    )
                    if current_sidecar_content != snapshot.sidecar_content:
                        _atomic_restore_bytes(snapshot.sidecar_path, snapshot.sidecar_content)
            except (OSError, ValueError) as exc:
                errors.append(f"{snapshot.sidecar_path}: {exc}")
        try:
            current_content = snapshot.path.read_bytes() if snapshot.path.is_file() else None
            if current_content != snapshot.content:
                _atomic_restore_bytes(snapshot.path, snapshot.content)
        except OSError as exc:
            errors.append(f"{snapshot.path}: {exc}")
    return tuple(errors)


def _atomic_restore_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".restore.tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_parent_directory(path.parent)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _fsync_parent_directory(parent: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bundle_cleanup_paths(metadata: BundleMetadataRecord) -> tuple[Path, ...]:
    seen: set[Path] = set()
    cleanup_paths = [metadata.bundle_note_path, metadata.metadata_path]
    preferred_input = metadata.processor_handoff.preferred_input_path
    if preferred_input is not None and _is_machine_managed_bundle_artifact(preferred_input, metadata):
        cleanup_paths.append(preferred_input)
        preferred_provenance_path = provenance_path(preferred_input)
        if preferred_provenance_path.is_symlink() or preferred_provenance_path.exists():
            cleanup_paths.append(preferred_provenance_path)
    for artifact in metadata.artifacts:
        for matched_path in artifact.matched_paths:
            if _is_machine_managed_bundle_artifact(matched_path, metadata):
                cleanup_paths.append(matched_path)
                matched_provenance_path = provenance_path(matched_path)
                if matched_provenance_path.is_symlink() or matched_provenance_path.exists():
                    cleanup_paths.append(matched_provenance_path)
    ordered_paths: list[Path] = []
    for path in cleanup_paths:
        if metadata.processed_marker_path is not None and path == metadata.processed_marker_path:
            continue
        if path in seen:
            continue
        seen.add(path)
        ordered_paths.append(path)
    return tuple(ordered_paths)


def _is_machine_managed_bundle_artifact(path: Path, metadata: BundleMetadataRecord) -> bool:
    root = metadata.metadata_path.parent
    try:
        relative_path = path.relative_to(root)
    except ValueError:
        return False
    return relative_path.parts[:1] in {(dir_name,) for dir_name in MACHINE_MANAGED_BUNDLE_DIR_NAMES}


def _authoritative_meeting_metadata(metadata: BundleMetadataRecord) -> MeetingMetadata:
    if metadata.start_at is None or not metadata.subject.strip():
        raise ValueError("Bundle metadata is missing authoritative meeting identity fields.")
    meeting_date = metadata.start_at.date().isoformat()
    title = _safe_bundle_subject(metadata.subject)
    return MeetingMetadata(
        date=meeting_date,
        source="Teams",
        title=title,
        canonical_basename=f"{meeting_date} - Teams - {title}.md",
        date_from_filename=metadata.start_at is not None,
    )


def _safe_bundle_subject(subject: str) -> str:
    without_controls = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character for character in subject
    )
    title = " ".join(without_controls.split())
    return title.replace("/", "-").replace(":", "-").replace("\\", "-") or "Meeting"


def _bundle_meeting_context(metadata: BundleMetadataRecord) -> dict[str, object]:
    context: dict[str, object] = {
        "outlook_event_id": metadata.occurrence_event_id or metadata.event_id,
        "organizer": metadata.organizer,
        "attendees": list(metadata.attendees),
        "teams_meeting_id": metadata.teams_meeting_id,
        "join_url": metadata.join_url,
        "start_at": metadata.start_at.isoformat() if metadata.start_at is not None else None,
        "end_at": metadata.end_at.isoformat() if metadata.end_at is not None else None,
        "sources_used": list(metadata.available_sources()),
        "source_limitations": list(metadata.source_limitations),
        "artifact_state": _bundle_artifact_state(metadata),
    }
    return context


def _bundle_artifact_state(metadata: BundleMetadataRecord) -> str | None:
    source_name = metadata.processor_handoff.preferred_input_source_name
    if source_name == "Copilot recap / AI summary":
        return "fallback"
    if source_name in {"Teams .vtt transcript", "Teams transcript text"}:
        return "transcript"
    if source_name == "Manual / semi-manual intake":
        return "manual"
    return None


def _source_limitations(
    *,
    payload: dict[str, object],
    artifacts: tuple[BundleArtifactRecord, ...],
    preferred_source: str | None,
) -> tuple[str, ...]:
    limitations: list[str] = []
    raw_limitations = payload.get("source_limitations")
    if isinstance(raw_limitations, list):
        for raw_limitation in raw_limitations:
            limitation = str(raw_limitation).strip()
            if limitation and limitation not in limitations:
                limitations.append(limitation)
    for artifact in artifacts:
        if artifact.status == "available":
            continue
        detail = f": {artifact.detail}" if artifact.detail else "."
        if artifact.status == "missing":
            limitation = f"{artifact.source_name} was not available{detail}"
        elif artifact.status == "permission_blocked":
            limitation = f"Permission blocked retrieval of {artifact.source_name}{detail}"
        else:
            limitation = f"{artifact.source_name} was not retrieved yet{detail}"
        if limitation not in limitations:
            limitations.append(limitation)
    recap_caveat = "Processor input is summary-derived and not a verbatim transcript."
    if preferred_source == "Copilot recap / AI summary" and recap_caveat not in limitations:
        limitations.append(recap_caveat)
    elif preferred_source != "Copilot recap / AI summary":
        limitations = [limitation for limitation in limitations if limitation != recap_caveat]
    return tuple(limitations)


def _attendee_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    labels: list[str] = []
    for item in value:
        if isinstance(item, str):
            label = item.strip()
        elif isinstance(item, dict):
            name = _string_or_none(item.get("name"))
            email = _string_or_none(item.get("email"))
            label = name or email or ""
            if name and email:
                label = f"{name} <{email}>"
            details = [
                detail
                for detail in (
                    _string_or_none(item.get("role")),
                    _string_or_none(item.get("response_status")),
                )
                if detail
            ]
            if label and details:
                label = f"{label} ({', '.join(details)})"
        else:
            label = ""
        if label and label not in labels:
            labels.append(label)
    return tuple(labels)


def _dict_or_none(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return dict(value)


def _datetime_before(value: datetime, deadline: datetime) -> bool:
    comparable_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    comparable_deadline = deadline.replace(tzinfo=UTC) if deadline.tzinfo is None else deadline.astimezone(UTC)
    return comparable_value < comparable_deadline


def _processor_ready_metadata_validation_reasons(metadata: BundleMetadataRecord) -> tuple[str, ...]:
    if metadata.processor_handoff.preferred_input_path is None:
        return ()
    reasons: list[str] = []
    if not metadata.subject.strip():
        reasons.append("Bundle metadata requires a nonblank Outlook subject.")
    if metadata.occurrence_event_id is None and metadata.event_id is None:
        reasons.append("Bundle metadata requires an authoritative Outlook occurrence/event ID.")
    if metadata.start_at is None:
        reasons.append("Bundle metadata requires a valid timezone-aware scheduled_start_at.")
    if metadata.end_at is None:
        reasons.append("Bundle metadata requires a valid timezone-aware scheduled_end_at.")
    if (
        metadata.start_at is not None
        and metadata.end_at is not None
        and _datetime_before(metadata.end_at, metadata.start_at)
    ):
        reasons.append("Bundle metadata scheduled_end_at must not be before scheduled_start_at.")
    if (
        metadata.processor_handoff.preferred_input_source_name == "Copilot recap / AI summary"
        and metadata.fallback_not_before is None
    ):
        reasons.append("Recap fallback requires a valid timezone-aware fallback_not_before.")
    if (
        metadata.processor_handoff.preferred_input_source_name == "Copilot recap / AI summary"
        and metadata.end_at is not None
        and metadata.fallback_not_before is not None
        and _datetime_before(metadata.fallback_not_before, metadata.end_at)
    ):
        reasons.append("Recap fallback_not_before must not be before scheduled_end_at.")
    return tuple(reasons)
