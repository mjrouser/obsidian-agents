"""Occurrence assignment for Copilot insights, independent of Graph I/O."""

from dataclasses import dataclass
from datetime import UTC, datetime

from .occurrence_assignment import OccurrenceWindow, selection_bounds


def source_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing source timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("source timestamp must include timezone")
    return parsed.astimezone(UTC)


def insight_metadata(value: dict[str, object]) -> dict[str, object]:
    identifier = value.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("missing insight id")
    created = source_time(value.get("createdDateTime"))
    end = source_time(value["endDateTime"]) if value.get("endDateTime") is not None else None
    if end is not None and end < created:
        raise ValueError("source end precedes creation")
    result: dict[str, object] = {"id": identifier, "createdDateTime": created.isoformat()}
    if end is not None:
        result["endDateTime"] = end.isoformat()
    for key in ("callId", "contentCorrelationId"):
        if value.get(key) is not None:
            if not isinstance(value[key], str) or not str(value[key]).strip():
                raise ValueError(f"invalid {key}")
            result[key] = value[key]
    return result


@dataclass(frozen=True)
class RecapSelection:
    selected: dict[str, object] | None
    reason: str
    candidate_count: int
    rejected: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


def select_recap(
    records: list[dict[str, object]], target: OccurrenceWindow, context: tuple[OccurrenceWindow, ...]
) -> RecapSelection:
    windows = {item.occurrence_id: item for item in context}
    if windows.get(target.occurrence_id) != target or len(windows) != len(context):
        return RecapSelection(None, "invalid_occurrence_context", len(records))
    unique: dict[str, dict[str, object]] = {}
    invalid: list[str] = []
    for raw in records:
        try:
            item = insight_metadata(raw)
        except (ValueError, TypeError):
            invalid.append(str(raw.get("id", "unknown")))
            continue
        key = str(item["id"])
        if key in unique and unique[key] != item:
            return RecapSelection(None, "conflicting_recap_metadata", len(records))
        unique[key] = item
    # An invalid duplicate must not disappear behind a valid record of the same ID.
    if set(invalid).intersection(unique):
        return RecapSelection(None, "conflicting_recap_metadata", len(records))
    matches: list[dict[str, object]] = []
    conflicts: set[str] = set()
    for item in unique.values():
        created = source_time(item["createdDateTime"])
        owners = [
            occurrence.occurrence_id
            for occurrence in context
            if occurrence.series_id == target.series_id
            and selection_bounds(occurrence)[0] <= created <= selection_bounds(occurrence)[1]
        ]
        if target.occurrence_id in owners:
            if len(owners) > 1:
                conflicts.update(owners)
            else:
                matches.append(item)
    if conflicts:
        reason = "ambiguous_recap_assignment"
    elif len(matches) > 1:
        reason = "multiple_matching_recaps"
    elif len(matches) == 1:
        return RecapSelection(matches[0], "validated", len(records), tuple(invalid))
    else:
        reason = "invalid_recap_metadata" if invalid else "no_matching_recap"
    return RecapSelection(None, reason, len(records), tuple(invalid), tuple(sorted(conflicts)))


def validate_detail(selected: dict[str, object], detail: dict[str, object]) -> dict[str, object]:
    if not isinstance(detail, dict):
        raise ValueError("recap detail is not an object")
    metadata = insight_metadata(detail)
    for key, value in selected.items():
        if key in metadata and metadata[key] != value:
            raise ValueError("recap detail contradicts selected metadata")
    rows: list[object] = []
    for field in ("meetingNotes", "actionItems"):
        value = detail.get(field)
        if isinstance(value, list):
            rows.extend(value)
    if not any(
        isinstance(row, dict)
        and any(isinstance(row.get(key), str) and str(row[key]).strip() for key in ("text", "title"))
        for row in rows
    ):
        raise ValueError("recap detail contains no summary or actions")
    return {**selected, **metadata}
