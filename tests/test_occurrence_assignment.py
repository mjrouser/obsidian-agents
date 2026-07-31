from datetime import UTC, datetime, timedelta

import pytest

from obsidian_intake_agent.meetings.occurrence_assignment import (
    AssignmentConflict,
    OccurrenceWindow,
    TranscriptCandidate,
    assign_transcripts,
    selection_bounds,
)


def _occurrence(
    occurrence_id: str,
    series_id: str,
    start: datetime,
    *,
    duration_minutes: int = 30,
) -> OccurrenceWindow:
    return OccurrenceWindow(
        occurrence_id=occurrence_id,
        series_id=series_id,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=duration_minutes),
    )


def test_assigns_daily_and_weekly_occurrences_without_inferring_cadence() -> None:
    monday = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)
    daily_occurrences = (
        _occurrence("daily-monday", "daily-series", monday),
        _occurrence("daily-tuesday", "daily-series", monday + timedelta(days=1)),
    )
    weekly_occurrences = (
        _occurrence("weekly-one", "weekly-series", monday + timedelta(hours=2)),
        _occurrence("weekly-two", "weekly-series", monday + timedelta(days=7, hours=2)),
    )
    candidates = (
        TranscriptCandidate("daily-transcript-two", "daily-series", monday + timedelta(days=1, minutes=10)),
        TranscriptCandidate("weekly-transcript-two", "weekly-series", monday + timedelta(days=7, hours=2, minutes=5)),
        TranscriptCandidate("daily-transcript-one", "daily-series", monday + timedelta(minutes=5)),
        TranscriptCandidate("weekly-transcript-one", "weekly-series", monday + timedelta(hours=2, minutes=5)),
    )

    result = assign_transcripts(
        occurrences=weekly_occurrences + daily_occurrences,
        candidates=candidates,
    )

    assert result.transcript_ids_for("daily-monday") == ("daily-transcript-one",)
    assert result.transcript_ids_for("daily-tuesday") == ("daily-transcript-two",)
    assert result.transcript_ids_for("weekly-one") == ("weekly-transcript-one",)
    assert result.transcript_ids_for("weekly-two") == ("weekly-transcript-two",)
    assert result.conflicts == ()
    assert result.unassigned_transcript_ids == ()


def test_selection_bounds_and_assignment_include_exact_tolerance_boundaries() -> None:
    start = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)
    occurrence = _occurrence("occurrence", "series", start)

    assert selection_bounds(occurrence) == (
        start - timedelta(minutes=15),
        start + timedelta(minutes=60),
    )

    result = assign_transcripts(
        occurrences=(occurrence,),
        candidates=(
            TranscriptCandidate("late", "series", start + timedelta(minutes=60)),
            TranscriptCandidate("early", "series", start - timedelta(minutes=15)),
        ),
    )

    assert result.transcript_ids_for("occurrence") == ("early", "late")


def test_assigned_segments_are_sorted_by_instant_across_timezone_offsets() -> None:
    start = datetime(2026, 7, 6, 11, 0, tzinfo=UTC)
    occurrence = _occurrence("occurrence", "series", start, duration_minutes=120)

    result = assign_transcripts(
        occurrences=(occurrence,),
        candidates=(
            TranscriptCandidate(
                "earlier-offset",
                "series",
                datetime.fromisoformat("2026-07-06T13:05:00+02:00"),
            ),
            TranscriptCandidate(
                "later-utc",
                "series",
                datetime.fromisoformat("2026-07-06T12:30:00+00:00"),
            ),
        ),
    )

    assert result.transcript_ids_for("occurrence") == ("earlier-offset", "later-utc")


def test_overlapping_occurrences_create_conflict_and_assign_candidate_nowhere() -> None:
    start = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)
    first = _occurrence("first", "same-series", start, duration_minutes=45)
    second = _occurrence("second", "same-series", start + timedelta(minutes=30), duration_minutes=45)
    candidate_time = start + timedelta(minutes=35)

    result = assign_transcripts(
        occurrences=(second, first),
        candidates=(TranscriptCandidate("ambiguous", "same-series", candidate_time),),
    )

    assert result.transcript_ids_for("first") == ()
    assert result.transcript_ids_for("second") == ()
    assert result.conflicts == (
        AssignmentConflict(
            transcript_id="ambiguous",
            created_at=candidate_time,
            occurrence_ids=("first", "second"),
        ),
    )
    assert result.unassigned_transcript_ids == ()


def test_identical_duplicate_occurrence_windows_are_collapsed() -> None:
    occurrence = _occurrence(
        "duplicate-occurrence",
        "series",
        datetime(2026, 7, 6, 13, 0, tzinfo=UTC),
    )

    result = assign_transcripts(
        occurrences=(occurrence, occurrence),
        candidates=(
            TranscriptCandidate(
                "transcript",
                "series",
                datetime(2026, 7, 6, 13, 5, tzinfo=UTC),
            ),
        ),
    )

    assert result.transcript_ids_for("duplicate-occurrence") == ("transcript",)
    assert result.conflicts == ()


def test_conflicting_duplicate_occurrence_ids_are_rejected_without_echoing_id() -> None:
    start = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)
    occurrence_id = "sensitive-occurrence-id"

    with pytest.raises(ValueError, match="conflicting definitions") as exc_info:
        assign_transcripts(
            occurrences=(
                _occurrence(occurrence_id, "series", start),
                _occurrence(occurrence_id, "other-series", start),
            ),
            candidates=(),
        )

    assert occurrence_id not in str(exc_info.value)


def test_identical_duplicate_candidates_do_not_duplicate_assignment() -> None:
    start = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)
    occurrence = _occurrence("occurrence", "series", start)
    candidate = TranscriptCandidate(
        "duplicate-transcript",
        "series",
        start + timedelta(minutes=5),
    )

    result = assign_transcripts(
        occurrences=(occurrence,),
        candidates=(candidate, candidate),
    )

    assert result.transcript_ids_for("occurrence") == ("duplicate-transcript",)
    assert result.unassigned_transcript_ids == ()


def test_conflicting_duplicate_candidate_id_is_assigned_nowhere_and_unassigned_once() -> None:
    start = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)
    occurrences = (
        _occurrence("first", "series", start),
        _occurrence("second", "series", start + timedelta(days=1)),
    )

    result = assign_transcripts(
        occurrences=occurrences,
        candidates=(
            TranscriptCandidate(
                "conflicting-transcript",
                "series",
                start + timedelta(minutes=5),
            ),
            TranscriptCandidate(
                "conflicting-transcript",
                "series",
                start + timedelta(days=1, minutes=5),
            ),
        ),
    )

    assert result.transcript_ids_for("first") == ()
    assert result.transcript_ids_for("second") == ()
    assert result.conflicts == ()
    assert result.unassigned_transcript_ids == ("conflicting-transcript",)


def test_timestamp_free_candidate_remains_unassigned() -> None:
    occurrence = _occurrence(
        "occurrence",
        "series",
        datetime(2026, 7, 6, 13, 0, tzinfo=UTC),
    )

    result = assign_transcripts(
        occurrences=(occurrence,),
        candidates=(TranscriptCandidate("missing-time", "series", None),),
    )

    assert result.transcript_ids_for("occurrence") == ()
    assert result.conflicts == ()
    assert result.unassigned_transcript_ids == ("missing-time",)
