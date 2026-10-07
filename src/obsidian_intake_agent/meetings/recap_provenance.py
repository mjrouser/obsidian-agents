"""Validate managed recap bytes and their captured occurrence assignment."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from .occurrence_assignment import OccurrenceWindow, selection_bounds
from .recap_selection import insight_metadata, select_recap, source_time
from .transcript_provenance import provenance_path

RECAP_SOURCE = "Copilot recap / AI summary"
PROVENANCE_KEYS = frozenset(
    {
        "schema_version",
        "policy_version",
        "source_type",
        "outcome",
        "occurrence",
        "occurrence_context",
        "selection_start_at",
        "selection_end_at",
        "context_start_at",
        "context_end_at",
        "online_meeting_id",
        "insight",
        "retrieved_at",
        "content_sha256",
    }
)


def occurrence_payload(item: OccurrenceWindow) -> dict[str, object]:
    return {
        "event_id": item.occurrence_id,
        "series_id": item.series_id,
        "start_at": item.scheduled_start.isoformat(),
        "end_at": item.scheduled_end.isoformat(),
    }


def create_recap_provenance(
    *,
    content: bytes,
    target: OccurrenceWindow,
    context: tuple[OccurrenceWindow, ...],
    insight: dict[str, object],
    online_meeting_id: str,
) -> dict[str, object]:
    lower, upper = selection_bounds(target)
    return {
        "schema_version": 1,
        "policy_version": 1,
        "source_type": "copilot_recap",
        "outcome": "validated",
        "occurrence": occurrence_payload(target),
        "occurrence_context": [occurrence_payload(item) for item in context],
        "selection_start_at": lower.isoformat(),
        "selection_end_at": upper.isoformat(),
        "context_start_at": min(item.scheduled_start for item in context).isoformat(),
        "context_end_at": max(item.scheduled_end for item in context).isoformat(),
        "online_meeting_id": online_meeting_id,
        "insight": insight_metadata(insight),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }


def _occurrence(raw: object) -> OccurrenceWindow:
    if not isinstance(raw, dict):
        raise ValueError("missing occurrence")
    if not all(isinstance(raw.get(key), str) and raw[key] for key in ("event_id", "series_id")):
        raise ValueError("missing occurrence identity")
    start, end = source_time(raw.get("start_at")), source_time(raw.get("end_at"))
    if end < start:
        raise ValueError("invalid occurrence bounds")
    return OccurrenceWindow(raw["event_id"], raw["series_id"], start, end)


def safe_recap_path(path: Path) -> bool:
    if ".." in path.parts or path.is_symlink():
        return False
    # The vault may live under macOS /var -> /private/var. Check the owned
    # bundle tree rather than rejecting system aliases above that boundary.
    for parent in path.parents:
        if parent.is_symlink():
            return False
        if parent.name == "bundles":
            break
    return True


def validate_recap_payload(
    payload: object,
    content: bytes,
    target: OccurrenceWindow,
    context: tuple[OccurrenceWindow, ...] | None = None,
) -> dict[str, object]:
    if not isinstance(payload, dict) or any(
        payload.get(k) != v
        for k, v in {
            "schema_version": 1,
            "policy_version": 1,
            "source_type": "copilot_recap",
            "outcome": "validated",
        }.items()
    ):
        raise ValueError("unverified_cached_recap: unsupported provenance")
    if (
        set(payload) != PROVENANCE_KEYS
        or type(payload["schema_version"]) is not int
        or type(payload["policy_version"]) is not int
    ):
        raise ValueError("unverified_cached_recap: unexpected provenance fields")
    if _occurrence(payload.get("occurrence")) != target:
        raise ValueError("unverified_cached_recap: occurrence mismatch")
    if payload.get("content_sha256") != hashlib.sha256(content).hexdigest():
        raise ValueError("unverified_cached_recap: source content changed")
    if not isinstance(payload.get("online_meeting_id"), str) or not payload["online_meeting_id"]:
        raise ValueError("missing Graph meeting identity")
    source_time(payload.get("retrieved_at"))
    raw_context = payload.get("occurrence_context")
    if not isinstance(raw_context, list) or not raw_context:
        raise ValueError("missing captured occurrence context")
    captured = tuple(_occurrence(item) for item in raw_context)
    bounds = selection_bounds(target)
    if (source_time(payload.get("selection_start_at")), source_time(payload.get("selection_end_at"))) != bounds:
        raise ValueError("selection bounds mismatch")
    if source_time(payload.get("context_start_at")) != min(item.scheduled_start for item in captured):
        raise ValueError("context bounds mismatch")
    if source_time(payload.get("context_end_at")) != max(item.scheduled_end for item in captured):
        raise ValueError("context bounds mismatch")
    insight = payload.get("insight")
    if not isinstance(insight, dict):
        raise ValueError("missing insight provenance")
    if insight_metadata(insight) != insight:
        raise ValueError("insight provenance must contain normalized metadata only")
    for windows in (captured, context if context is not None else captured):
        if select_recap([insight], target, windows).selected is None:
            raise ValueError("unverified_cached_recap: assignment no longer unique")
    return payload


def read_recap_provenance(
    path: Path,
    target: OccurrenceWindow,
    context: tuple[OccurrenceWindow, ...] | None = None,
) -> dict[str, object]:
    sidecar = provenance_path(path)
    if not safe_recap_path(path) or not safe_recap_path(sidecar):
        raise ValueError("unverified_cached_recap: unsafe source path")
    try:
        return validate_recap_payload(json.loads(sidecar.read_text()), path.read_bytes(), target, context)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError(f"unverified_cached_recap: {exc}") from exc
