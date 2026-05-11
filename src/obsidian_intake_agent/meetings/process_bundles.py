from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from ..processors.meeting_processor import MeetingProcessor, OutputMode, ProcessResult

ArtifactStatus = Literal["available", "missing", "permission_blocked", "not_attempted"]
BundleProcessDecision = Literal["ready", "blocked"]
BundleExecutionStatus = Literal["processed", "skipped", "failed"]
PROCESSOR_SUPPORTED_EXTENSIONS = {".md", ".docx", ".vtt"}
MACHINE_MANAGED_BUNDLE_DIR_NAMES = ("raw_transcripts", "fallbacks")


@dataclass(slots=True, frozen=True)
class BundleArtifactRecord:
    source_name: str
    status: ArtifactStatus
    detail: str | None
    matched_paths: tuple[Path, ...]


@dataclass(slots=True, frozen=True)
class BundleProcessorHandoff:
    preferred_input_path: Path | None
    preferred_input_source_name: str | None


@dataclass(slots=True, frozen=True)
class BundleMetadataRecord:
    metadata_path: Path
    bundle_note_path: Path
    processed_marker_path: Path | None
    event_id: str
    subject: str
    teams_meeting_id: str | None
    identity_key: str | None
    source_type: str | None
    processor_handoff: BundleProcessorHandoff
    artifacts: tuple[BundleArtifactRecord, ...]

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
        items.append(_plan_bundle_processing_item(metadata=metadata, processor=processor))

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
            f'meeting_bundle_item: {item.decision} event_id="{item.metadata.event_id}" subject="{item.metadata.subject}"'
        )
        lines.append(f"  outlook_metadata: {item.metadata.metadata_path}")
        lines.append(f"  bundle_note: {item.metadata.bundle_note_path}")
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
        for reason in item.reasons:
            lines.append(f"  reason: {reason}")
    return "\n".join(lines)


def execute_bundle_processing_plan(
    plan: BundleProcessingPlan,
    *,
    processor: MeetingProcessor,
) -> BundleExecutionResult:
    items: list[BundleExecutionResultItem] = []
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
        try:
            process_result = processor.process_file(preferred_input, dry_run=False)
        except Exception as exc:
            items.append(
                BundleExecutionResultItem(
                    status="failed",
                    plan_item=plan_item,
                    reasons=(f"Processor execution failed: {exc}",),
                )
            )
            continue
        if process_result.processed:
            try:
                processed_marker_path = _write_durable_processed_marker(
                    metadata=plan_item.metadata,
                    process_result=process_result,
                    cleanup_paths=_bundle_cleanup_paths(plan_item.metadata),
                )
            except OSError as exc:
                items.append(
                    BundleExecutionResultItem(
                        status="failed",
                        plan_item=plan_item,
                        reasons=(f"Bundle processed successfully but could not write durable processed marker: {exc}",),
                        canonical_note_path=process_result.canonical_note_path,
                        actions_file_path=process_result.actions_file_path,
                    )
                )
                continue
            removed_paths, cleanup_errors = _cleanup_bundle_staging(metadata=plan_item.metadata)
            items.append(
                _execution_result_item_from_process_result(
                    plan_item=plan_item,
                    process_result=process_result,
                    processed_marker_path=processed_marker_path,
                    removed_paths=removed_paths,
                    cleanup_errors=cleanup_errors,
                )
            )
            continue
        items.append(_execution_result_item_from_process_result(plan_item=plan_item, process_result=process_result))
    return BundleExecutionResult(
        executed_at=datetime.now().astimezone(),
        bundle_root=plan.bundle_root,
        items=tuple(items),
        warnings=plan.warnings,
        output_mode=getattr(processor, "output_mode", "normal"),
    )


def render_bundle_execution_result(result: BundleExecutionResult) -> str:
    lines = [
        "meeting_bundle_process_mode: execute",
        f"meeting_bundle_process_executed_at: {result.executed_at.isoformat()}",
        f"meeting_bundle_process_bundle_root: {result.bundle_root}",
        f"meeting_bundle_process_candidates: {result.candidate_count}",
        f"meeting_bundle_process_processed: {result.processed_count}",
        f"meeting_bundle_process_skipped: {result.skipped_count}",
        f"meeting_bundle_process_failed: {result.failed_count}",
    ]
    if result.output_mode == "validation":
        lines.append("meeting_bundle_process_output_mode: validation")
    for warning in result.warnings:
        lines.append(f"meeting_bundle_process_warning: {warning}")
    for item in result.items:
        metadata = item.plan_item.metadata
        lines.append(f'meeting_bundle_item: {item.status} event_id="{metadata.event_id}" subject="{metadata.subject}"')
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
) -> BundleProcessingPlanItem:
    reasons: list[str] = []
    preferred_input = metadata.processor_handoff.preferred_input_path

    if metadata.processed_marker_path is not None and metadata.processed_marker_path.exists():
        reasons.append("Durable processed marker indicates this bundle was already processed successfully.")
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
    processed_marker_path = _path_or_none(payload.get("processed_marker_path"))

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
            artifacts.append(
                BundleArtifactRecord(
                    source_name=_string_or_default(raw_artifact.get("source_name"), "Unknown source"),
                    status=status,
                    detail=_string_or_none(raw_artifact.get("detail")),
                    matched_paths=matched_paths,
                )
            )

    return BundleMetadataRecord(
        metadata_path=metadata_path,
        bundle_note_path=metadata_path.with_name(metadata_path.name.replace(" (outlook).json", " (bundle).md")),
        processed_marker_path=processed_marker_path,
        event_id=_string_or_default(payload.get("outlook_event_id"), metadata_path.stem),
        subject=_string_or_default(payload.get("subject"), metadata_path.stem),
        teams_meeting_id=_string_or_none(payload.get("teams_meeting_id")),
        identity_key=_string_or_none(payload.get("identity_key")),
        source_type=_string_or_none(payload.get("source_type")),
        processor_handoff=BundleProcessorHandoff(
            preferred_input_path=preferred_input_path,
            preferred_input_source_name=preferred_input_source_name,
        ),
        artifacts=tuple(artifacts),
    )


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
) -> Path | None:
    if metadata.processed_marker_path is None:
        return None
    marker_path = metadata.processed_marker_path
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "source_type": "meeting_bundle_processed",
        "processed_at": datetime.now().astimezone().isoformat(),
        "outlook_event_id": metadata.event_id,
        "subject": metadata.subject,
        "teams_meeting_id": metadata.teams_meeting_id,
        "identity_key": metadata.identity_key,
        "bundle_note_path": str(metadata.bundle_note_path),
        "outlook_metadata_path": str(metadata.metadata_path),
        "preferred_input_path": (
            str(metadata.processor_handoff.preferred_input_path)
            if metadata.processor_handoff.preferred_input_path is not None
            else None
        ),
        "preferred_input_source_name": metadata.processor_handoff.preferred_input_source_name,
        "canonical_note_path": (
            str(process_result.canonical_note_path) if process_result.canonical_note_path is not None else None
        ),
        "actions_file_path": str(process_result.actions_file_path)
        if process_result.actions_file_path is not None
        else None,
        "cleanup_paths": [str(path) for path in cleanup_paths],
        "artifacts": [
            {
                "source_name": artifact.source_name,
                "status": artifact.status,
                "detail": artifact.detail,
                "matched_paths": [str(path) for path in artifact.matched_paths],
            }
            for artifact in metadata.artifacts
        ],
    }
    _write_json_atomically(marker_path, payload)
    return marker_path


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


def _bundle_cleanup_paths(metadata: BundleMetadataRecord) -> tuple[Path, ...]:
    seen: set[Path] = set()
    cleanup_paths = [metadata.bundle_note_path, metadata.metadata_path]
    preferred_input = metadata.processor_handoff.preferred_input_path
    if preferred_input is not None and _is_machine_managed_bundle_artifact(preferred_input, metadata):
        cleanup_paths.append(preferred_input)
    for artifact in metadata.artifacts:
        for matched_path in artifact.matched_paths:
            if _is_machine_managed_bundle_artifact(matched_path, metadata):
                cleanup_paths.append(matched_path)
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


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
