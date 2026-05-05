from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from ..processors.meeting_processor import MeetingProcessor

ArtifactStatus = Literal["available", "missing", "permission_blocked", "not_attempted"]
BundleProcessDecision = Literal["ready", "blocked"]
PROCESSOR_SUPPORTED_EXTENSIONS = {".md", ".docx", ".vtt"}


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
    event_id: str
    subject: str
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


def _plan_bundle_processing_item(
    *,
    metadata: BundleMetadataRecord,
    processor: MeetingProcessor,
) -> BundleProcessingPlanItem:
    reasons: list[str] = []
    preferred_input = metadata.processor_handoff.preferred_input_path

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
        event_id=_string_or_default(payload.get("outlook_event_id"), metadata_path.stem),
        subject=_string_or_default(payload.get("subject"), metadata_path.stem),
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
