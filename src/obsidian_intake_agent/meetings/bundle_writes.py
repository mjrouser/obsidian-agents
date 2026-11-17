"""Disk writes for planned meeting sync bundles.

Writes each planned intake bundle note, its Outlook metadata sidecar, and its
meeting identity marker under the owned ``bundles/`` root, and persists planned
fallback recap content with provenance. Refuses symlinked or out-of-root
targets and never downgrades a processed identity marker. Called by the
``meeting-sync`` CLI through ``meetings.sync``, which re-exports
``write_planned_bundle_notes``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .identity_state import identity_marker_entry_exists, read_identity_state, write_identity_marker
from .recap_provenance import RECAP_SOURCE, read_recap_provenance, validate_recap_payload
from .recap_validation import _recap_occurrences
from .sync_models import BundleWriteResult, MeetingSourceBundle, TranscriptSyncPlan
from .transcript_provenance import atomic_create_bytes, atomic_write_bytes, provenance_path


def write_planned_bundle_notes(plan: TranscriptSyncPlan) -> BundleWriteResult:
    written_bundle_note_paths: list[Path] = []
    written_metadata_paths: list[Path] = []
    written_identity_paths: list[Path] = []
    skipped_existing_bundle_note_paths: list[Path] = []
    skipped_existing_metadata_paths: list[Path] = []
    skipped_existing_identity_paths: list[Path] = []
    for item in plan.items:
        if item.decision != "process" or item.intake_bundle_note is None:
            continue
        note = item.intake_bundle_note
        bundle_note_content = (note.content + "\n").encode("utf-8")
        metadata_content = (note.metadata_content + "\n").encode("utf-8")
        metadata_payload = json.loads(metadata_content)
        if not isinstance(metadata_payload, dict):
            raise ValueError("Rendered Outlook metadata sidecar must be a JSON object.")
        identity_payload = json.loads(note.identity_content)
        if not isinstance(identity_payload, dict):
            raise ValueError("Rendered meeting identity marker must be a JSON object.")
        owned_bundles_root = _prepare_owned_bundles_root(note.path.parent)
        note.identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_state = (
            read_identity_state(note.identity_path, item.meeting.end_at)
            if identity_marker_entry_exists(note.identity_path)
            else None
        )
        if identity_state is not None and "processing_exclusion" in identity_state.payload:
            skipped_existing_identity_paths.append(note.identity_path)
            skipped_existing_bundle_note_paths.append(note.path)
            skipped_existing_metadata_paths.append(note.metadata_path)
            continue
        _persist_planned_artifact_content(
            item.bundle,
            owned_bundles_root=owned_bundles_root,
        )
        should_refresh_pending = identity_state is not None and identity_state.marker_kind == "pending"
        should_refresh_upgrade = (
            identity_state is not None
            and identity_state.marker_kind == "processed"
            and identity_state.processing_source_kind == "fallback"
            and note.processor_input_source_name in {"Teams .vtt transcript", "Teams transcript text"}
        )
        should_refresh_bundle = should_refresh_pending or should_refresh_upgrade
        if note.path.exists() and not should_refresh_bundle:
            skipped_existing_bundle_note_paths.append(note.path)
        else:
            atomic_write_bytes(note.path, bundle_note_content)
            written_bundle_note_paths.append(note.path)
        if note.metadata_path.exists() and not should_refresh_bundle:
            skipped_existing_metadata_paths.append(note.metadata_path)
        else:
            atomic_write_bytes(note.metadata_path, metadata_content)
            written_metadata_paths.append(note.metadata_path)
        if identity_state is not None and (
            identity_state.marker_kind == "processed" or identity_state.is_terminal(now=plan.generated_at)
        ):
            skipped_existing_identity_paths.append(note.identity_path)
        else:
            write_identity_marker(note.identity_path, identity_payload)
            written_identity_paths.append(note.identity_path)
    return BundleWriteResult(
        written_bundle_note_paths=tuple(written_bundle_note_paths),
        written_metadata_paths=tuple(written_metadata_paths),
        written_identity_paths=tuple(written_identity_paths),
        skipped_existing_bundle_note_paths=tuple(skipped_existing_bundle_note_paths),
        skipped_existing_metadata_paths=tuple(skipped_existing_metadata_paths),
        skipped_existing_identity_paths=tuple(skipped_existing_identity_paths),
    )


def _persist_planned_artifact_content(
    bundle: MeetingSourceBundle,
    *,
    owned_bundles_root: Path,
) -> None:
    for artifact in bundle.artifacts:
        if artifact.planned_content is None:
            continue
        if artifact.status != "available" or len(artifact.matched_paths) != 1:
            raise ValueError("Planned meeting artifact content requires one available local path.")
        target_path = _validate_owned_artifact_target(
            artifact.matched_paths[0],
            owned_bundles_root=owned_bundles_root,
        )
        if artifact.source_name == RECAP_SOURCE:
            target, context = _recap_occurrences(bundle.meeting)
            validate_recap_payload(artifact.recap_provenance, artifact.planned_content.encode("utf-8"), target, context)
            sidecar = _validate_owned_artifact_target(
                provenance_path(target_path), owned_bundles_root=owned_bundles_root
            )
            if target_path.exists() or sidecar.exists():
                read_recap_provenance(target_path, target, context)
                if target_path.read_bytes() != artifact.planned_content.encode("utf-8"):
                    raise ValueError("manual_review_required: competing recap create")
                continue
            if not atomic_create_bytes(target_path, artifact.planned_content.encode("utf-8")):
                read_recap_provenance(target_path, target, context)
                raise ValueError("manual_review_required: competing recap create")
            atomic_create_bytes(
                sidecar, (json.dumps(artifact.recap_provenance, sort_keys=True, indent=2) + "\n").encode()
            )
            read_recap_provenance(target_path, target, context)
        else:
            atomic_create_bytes(target_path, artifact.planned_content.encode("utf-8"))


def _prepare_owned_bundles_root(owned_bundles_root: Path) -> Path:
    lexical_root = Path(os.path.abspath(owned_bundles_root))
    if lexical_root.is_symlink():
        raise ValueError(f"Meeting sync owned bundles root is symlinked: {lexical_root}")
    lexical_root.mkdir(parents=True, exist_ok=True)
    if lexical_root.is_symlink():
        raise ValueError(f"Meeting sync owned bundles root is symlinked: {lexical_root}")
    return lexical_root


def _validate_owned_artifact_target(
    target_path: Path,
    *,
    owned_bundles_root: Path,
) -> Path:
    lexical_root = Path(os.path.abspath(owned_bundles_root))
    lexical_target = Path(os.path.abspath(target_path))
    try:
        relative_target = lexical_target.relative_to(lexical_root)
    except ValueError:
        raise ValueError(f"Planned meeting artifact target is outside the owned bundles root: {target_path}") from None
    if relative_target == Path("."):
        raise ValueError(f"Planned meeting artifact target must be a file below {lexical_root}")

    current = lexical_root
    for part in relative_target.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Planned meeting artifact target contains a symlinked path component: {current}")
        current.mkdir(exist_ok=True)
        if current.is_symlink():
            raise ValueError(f"Planned meeting artifact target contains a symlinked path component: {current}")

    if lexical_target.is_symlink():
        raise ValueError(f"Planned meeting artifact target contains a symlinked path component: {lexical_target}")
    return lexical_target
