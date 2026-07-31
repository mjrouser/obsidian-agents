from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

EARLY_TOLERANCE = timedelta(minutes=15)
LATE_TOLERANCE = timedelta(minutes=30)


@dataclass(slots=True, frozen=True)
class OccurrenceWindow:
    occurrence_id: str
    series_id: str
    scheduled_start: datetime
    scheduled_end: datetime


@dataclass(slots=True, frozen=True)
class TranscriptCandidate:
    transcript_id: str
    series_id: str
    created_at: datetime | None


@dataclass(slots=True, frozen=True)
class AssignmentConflict:
    transcript_id: str
    created_at: datetime
    occurrence_ids: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class AssignmentResult:
    assignments: tuple[tuple[str, str], ...]
    conflicts: tuple[AssignmentConflict, ...]
    unassigned_transcript_ids: tuple[str, ...]

    def transcript_ids_for(self, occurrence_id: str) -> tuple[str, ...]:
        return tuple(
            transcript_id
            for assigned_occurrence_id, transcript_id in self.assignments
            if assigned_occurrence_id == occurrence_id
        )


def selection_bounds(occurrence: OccurrenceWindow) -> tuple[datetime, datetime]:
    return (
        occurrence.scheduled_start - EARLY_TOLERANCE,
        occurrence.scheduled_end + LATE_TOLERANCE,
    )


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _unique_occurrences(
    occurrences: tuple[OccurrenceWindow, ...],
) -> tuple[OccurrenceWindow, ...]:
    occurrences_by_id: dict[str, OccurrenceWindow] = {}
    ordered_occurrences: list[OccurrenceWindow] = []
    for occurrence in occurrences:
        existing = occurrences_by_id.get(occurrence.occurrence_id)
        if existing is None:
            occurrences_by_id[occurrence.occurrence_id] = occurrence
            ordered_occurrences.append(occurrence)
        elif existing != occurrence:
            raise ValueError("Occurrence input contains conflicting definitions for the same occurrence ID.")
    return tuple(ordered_occurrences)


def _canonical_candidates(
    candidates: tuple[TranscriptCandidate, ...],
) -> tuple[tuple[TranscriptCandidate, ...], tuple[str, ...]]:
    candidates_by_id: dict[str, TranscriptCandidate] = {}
    conflicting_transcript_ids: set[str] = set()
    for candidate in candidates:
        existing = candidates_by_id.get(candidate.transcript_id)
        if existing is None:
            candidates_by_id[candidate.transcript_id] = candidate
        elif existing != candidate:
            conflicting_transcript_ids.add(candidate.transcript_id)
    canonical_candidates = tuple(
        candidate
        for transcript_id, candidate in candidates_by_id.items()
        if transcript_id not in conflicting_transcript_ids
    )
    return canonical_candidates, tuple(sorted(conflicting_transcript_ids))


def assign_transcripts(
    *,
    occurrences: tuple[OccurrenceWindow, ...],
    candidates: tuple[TranscriptCandidate, ...],
) -> AssignmentResult:
    ordered_occurrences = tuple(
        sorted(
            _unique_occurrences(occurrences),
            key=lambda occurrence: (
                _utc_datetime(occurrence.scheduled_start),
                _utc_datetime(occurrence.scheduled_end),
                occurrence.occurrence_id,
            ),
        )
    )
    canonical_candidates, conflicting_transcript_ids = _canonical_candidates(candidates)
    ordered_candidates = tuple(
        sorted(
            canonical_candidates,
            key=lambda candidate: (
                candidate.created_at is None,
                _utc_datetime(candidate.created_at)
                if candidate.created_at is not None
                else datetime.max.replace(tzinfo=UTC),
                candidate.transcript_id,
            ),
        )
    )
    assignments: list[tuple[str, str]] = []
    conflicts: list[AssignmentConflict] = []
    unassigned_transcript_ids = list(conflicting_transcript_ids)

    for candidate in ordered_candidates:
        if candidate.created_at is None:
            unassigned_transcript_ids.append(candidate.transcript_id)
            continue
        matching_occurrences = tuple(
            occurrence
            for occurrence in ordered_occurrences
            if occurrence.series_id == candidate.series_id
            and _utc_datetime(selection_bounds(occurrence)[0])
            <= _utc_datetime(candidate.created_at)
            <= _utc_datetime(selection_bounds(occurrence)[1])
        )
        if len(matching_occurrences) == 1:
            assignments.append((matching_occurrences[0].occurrence_id, candidate.transcript_id))
        elif len(matching_occurrences) > 1:
            conflicts.append(
                AssignmentConflict(
                    transcript_id=candidate.transcript_id,
                    created_at=candidate.created_at,
                    occurrence_ids=tuple(occurrence.occurrence_id for occurrence in matching_occurrences),
                )
            )
        else:
            unassigned_transcript_ids.append(candidate.transcript_id)

    return AssignmentResult(
        assignments=tuple(assignments),
        conflicts=tuple(conflicts),
        unassigned_transcript_ids=tuple(unassigned_transcript_ids),
    )
