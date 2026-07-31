# Meeting Fallback Grace and Late Transcript Upgrades Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not use subagent-driven development unless the user separately authorizes subagents.

**Goal:** Delay recap fallback processing for 60 minutes, keep each fallback-processed Outlook occurrence upgradeable for 24 hours, and safely replace unedited fallbacks when a uniquely assigned late transcript appears.

**Architecture:** Keep `sync.py` responsible for Graph discovery and bundle planning, but move occurrence-window assignment and marker interpretation into focused modules. Extend bundle metadata so `process_bundles.py` can distinguish deferred fallbacks, normal processing, and transcript upgrades. Use the existing processor with explicit bundle metadata/context, plus a small upgrade module that performs edit detection, recoverable archival, backlink migration, and rollback.

**Tech Stack:** Python 3.11+, standard library dataclasses/JSON/pathlib/hashlib/tempfile, existing unittest/pytest suite, Ruff, existing Make targets.

**Review constraint:** Do not commit, push, or open a PR. Replace the normal Superpowers per-task commit step with the review checkpoint at the end of each task.

---

## File Map

- Create `src/obsidian_intake_agent/meetings/occurrence_assignment.py`
  - Pure occurrence tolerance windows and cross-occurrence ambiguity decisions.
- Create `src/obsidian_intake_agent/meetings/identity_state.py`
  - Backward-compatible pending/processed marker interpretation and schema-v2 rendering helpers.
- Create `src/obsidian_intake_agent/meetings/meeting_upgrade.py`
  - Canonical-note hashing, archive paths, edit protection, backlink migration, and rollback snapshots.
- Create `tests/test_occurrence_assignment.py`
  - Cadence-independent occurrence assignment and ambiguity tests.
- Create `tests/test_meeting_identity_state.py`
  - Legacy and schema-v2 marker interpretation tests.
- Create `tests/test_meeting_upgrade.py`
  - Edit detection, archival, backlink migration, and rollback-helper tests.
- Modify `src/obsidian_intake_agent/config.py`
  - Add and validate `meeting_transcript_grace_minutes`.
- Modify `src/obsidian_intake_agent/meetings/sync.py`
  - Apply occurrence context, grace gating, fallback-processed polling, transcript-only retries, and structured diagnostics.
- Modify `src/obsidian_intake_agent/meetings/process_bundles.py`
  - Parse extended bundle metadata, block deferred recaps, allow valid upgrades, execute upgrades, and write enriched processed markers.
- Modify `src/obsidian_intake_agent/processors/meeting_processor.py`
  - Accept authoritative bundle metadata/context and action-source aliases without changing normal intake behavior.
- Modify `src/obsidian_intake_agent/rendering/meeting_renderer.py`
  - Add structured `artifact_state` and `supersedes_note` front matter.
- Modify `src/obsidian_intake_agent/rendering/action_renderer.py`
  - Treat old and clean canonical source names as equivalent during upgrade dedupe.
- Modify `src/obsidian_intake_agent/main.py`
  - Pass grace configuration into sync planning.
- Modify `src/obsidian_intake_agent/meetings/__init__.py`
  - Export new public dataclasses used by tests and command wiring.
- Modify `tests/test_config.py`, `tests/test_meeting_sync.py`,
  `tests/test_meeting_process_bundles.py`, `tests/test_processor.py`,
  `tests/test_renderer.py`, and `tests/test_main.py`
  - Add focused regression and CLI-wiring coverage.
- Modify `config.example.yaml`, `README.md`, and
  `docs/meeting_transcript_automation.md`
  - Document configuration, states, errors, and recovery.

## Task 1: Add the Transcript Grace Configuration

**Files:**
- Modify: `src/obsidian_intake_agent/config.py`
- Modify: `tests/test_config.py`
- Modify: `config.example.yaml`

- [ ] **Step 1: Write failing default, override, and validation tests**

Add to `ConfigCompatibilityTests`:

```python
def test_meeting_transcript_grace_defaults_to_60_minutes(self) -> None:
    loaded = Config.load(_write_config(self))
    self.assertEqual(loaded.meeting_transcript_grace_minutes, 60)

def test_meeting_transcript_grace_can_be_overridden(self) -> None:
    loaded = Config.load(_write_config(self, "meeting_transcript_grace_minutes: 90"))
    self.assertEqual(loaded.meeting_transcript_grace_minutes, 90)

def test_meeting_transcript_grace_must_be_positive(self) -> None:
    with self.assertRaisesRegex(
        ValueError,
        "meeting_transcript_grace_minutes must be a positive integer",
    ):
        Config.load(_write_config(self, "meeting_transcript_grace_minutes: 0"))
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest tests/test_config.py -k meeting_transcript_grace -q
```

Expected: three failures because `Config` has no
`meeting_transcript_grace_minutes` field.

- [ ] **Step 3: Add the validated configuration field**

Add to `Config`:

```python
meeting_transcript_grace_minutes: int = 60
```

Add to `Config.load()`:

```python
meeting_transcript_grace_minutes=_positive_int(
    data.get("meeting_transcript_grace_minutes", 60),
    "meeting_transcript_grace_minutes",
),
```

Add to `config.example.yaml` beside the Graph meeting settings:

```yaml
# Wait this long after scheduled meeting end before processing a recap fallback.
meeting_transcript_grace_minutes: 60
```

- [ ] **Step 4: Run focused configuration tests and verify GREEN**

Run:

```bash
./.venv/bin/python -m pytest tests/test_config.py -q
```

Expected: all configuration tests pass.

- [ ] **Step 5: Review checkpoint**

Run:

```bash
git diff --check
git diff -- src/obsidian_intake_agent/config.py tests/test_config.py config.example.yaml
```

Expected: only the grace configuration, validation tests, and example setting
are present; do not stage or commit.

## Task 2: Isolate Occurrence Assignment and Ambiguity

**Files:**
- Create: `src/obsidian_intake_agent/meetings/occurrence_assignment.py`
- Create: `tests/test_occurrence_assignment.py`
- Modify: `src/obsidian_intake_agent/meetings/sync.py`

- [ ] **Step 1: Write failing cadence-independent assignment tests**

Create `tests/test_occurrence_assignment.py` with:

```python
from datetime import datetime

from obsidian_intake_agent.meetings.occurrence_assignment import (
    OccurrenceWindow,
    TranscriptCandidate,
    assign_transcripts,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_assigns_distinct_transcripts_to_occurrences_regardless_of_cadence() -> None:
    occurrences = (
        OccurrenceWindow("daily-1", "series-1", dt("2026-07-27T16:00:00+00:00"), dt("2026-07-27T16:30:00+00:00")),
        OccurrenceWindow("daily-2", "series-1", dt("2026-07-28T16:00:00+00:00"), dt("2026-07-28T16:30:00+00:00")),
        OccurrenceWindow("weekly-1", "series-2", dt("2026-07-06T13:00:00+00:00"), dt("2026-07-06T13:30:00+00:00")),
        OccurrenceWindow("weekly-2", "series-2", dt("2026-07-13T13:00:00+00:00"), dt("2026-07-13T13:30:00+00:00")),
    )
    candidates = (
        TranscriptCandidate("daily-t1", "series-1", dt("2026-07-27T16:31:00+00:00")),
        TranscriptCandidate("daily-t2", "series-1", dt("2026-07-28T16:32:00+00:00")),
        TranscriptCandidate("weekly-t1", "series-2", dt("2026-07-06T13:31:00+00:00")),
        TranscriptCandidate("weekly-t2", "series-2", dt("2026-07-13T13:31:00+00:00")),
    )

    result = assign_transcripts(occurrences=occurrences, candidates=candidates)

    assert result.transcript_ids_for("daily-1") == ("daily-t1",)
    assert result.transcript_ids_for("daily-2") == ("daily-t2",)
    assert result.transcript_ids_for("weekly-1") == ("weekly-t1",)
    assert result.transcript_ids_for("weekly-2") == ("weekly-t2",)
    assert result.conflicts == ()


def test_accepts_early_and_late_actual_meeting_timing_inside_tolerance() -> None:
    occurrence = OccurrenceWindow(
        "occ-1",
        "series-1",
        dt("2026-07-29T16:00:00+00:00"),
        dt("2026-07-29T16:30:00+00:00"),
    )
    candidates = (
        TranscriptCandidate("early", "series-1", dt("2026-07-29T15:45:00+00:00")),
        TranscriptCandidate("late", "series-1", dt("2026-07-29T17:00:00+00:00")),
    )

    result = assign_transcripts(occurrences=(occurrence,), candidates=candidates)

    assert result.transcript_ids_for("occ-1") == ("early", "late")


def test_rejects_candidate_that_matches_overlapping_occurrence_windows() -> None:
    occurrences = (
        OccurrenceWindow("occ-1", "series-1", dt("2026-07-29T16:00:00+00:00"), dt("2026-07-29T16:30:00+00:00")),
        OccurrenceWindow("occ-2", "series-1", dt("2026-07-29T16:40:00+00:00"), dt("2026-07-29T17:10:00+00:00")),
    )
    candidate = TranscriptCandidate("ambiguous", "series-1", dt("2026-07-29T16:45:00+00:00"))

    result = assign_transcripts(occurrences=occurrences, candidates=(candidate,))

    assert result.transcript_ids_for("occ-1") == ()
    assert result.transcript_ids_for("occ-2") == ()
    assert result.conflicts[0].transcript_id == "ambiguous"
    assert result.conflicts[0].occurrence_ids == ("occ-1", "occ-2")


def test_timestamp_free_candidate_is_never_assigned() -> None:
    occurrence = OccurrenceWindow(
        "occ-1",
        "series-1",
        dt("2026-07-29T16:00:00+00:00"),
        dt("2026-07-29T16:30:00+00:00"),
    )
    result = assign_transcripts(
        occurrences=(occurrence,),
        candidates=(TranscriptCandidate("missing-time", "series-1", None),),
    )
    assert result.transcript_ids_for("occ-1") == ()
    assert result.unassigned_transcript_ids == ("missing-time",)
```

- [ ] **Step 2: Run the new test file and verify RED**

Run:

```bash
./.venv/bin/python -m pytest tests/test_occurrence_assignment.py -q
```

Expected: import failure because `occurrence_assignment.py` does not exist.

- [ ] **Step 3: Implement the pure occurrence assignment module**

Create `occurrence_assignment.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

EARLY_TOLERANCE = timedelta(minutes=15)
LATE_TOLERANCE = timedelta(minutes=30)

@dataclass(frozen=True, slots=True)
class OccurrenceWindow:
    occurrence_id: str
    series_id: str
    scheduled_start: datetime
    scheduled_end: datetime

@dataclass(frozen=True, slots=True)
class TranscriptCandidate:
    transcript_id: str
    series_id: str
    created_at: datetime | None

@dataclass(frozen=True, slots=True)
class AssignmentConflict:
    transcript_id: str
    created_at: datetime
    occurrence_ids: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class AssignmentResult:
    assignments: tuple[tuple[str, tuple[TranscriptCandidate, ...]], ...]
    conflicts: tuple[AssignmentConflict, ...]
    unassigned_transcript_ids: tuple[str, ...]

    def transcript_ids_for(self, occurrence_id: str) -> tuple[str, ...]:
        for assigned_id, candidates in self.assignments:
            if assigned_id == occurrence_id:
                return tuple(item.transcript_id for item in candidates)
        return ()

    @classmethod
    def from_mutable(
        cls,
        assignments: dict[str, list[TranscriptCandidate]],
        conflicts: list[AssignmentConflict],
        unassigned: list[str],
    ) -> AssignmentResult:
        frozen_assignments = tuple(
            (
                occurrence_id,
                tuple(sorted(items, key=lambda item: (item.created_at or datetime.min, item.transcript_id))),
            )
            for occurrence_id, items in sorted(assignments.items())
        )
        return cls(
            assignments=frozen_assignments,
            conflicts=tuple(sorted(conflicts, key=lambda item: item.transcript_id)),
            unassigned_transcript_ids=tuple(sorted(unassigned)),
        )

def selection_bounds(occurrence: OccurrenceWindow) -> tuple[datetime, datetime]:
    return occurrence.scheduled_start - EARLY_TOLERANCE, occurrence.scheduled_end + LATE_TOLERANCE


def assign_transcripts(
    *,
    occurrences: tuple[OccurrenceWindow, ...],
    candidates: tuple[TranscriptCandidate, ...],
) -> AssignmentResult:
    assignments: dict[str, list[TranscriptCandidate]] = {item.occurrence_id: [] for item in occurrences}
    conflicts: list[AssignmentConflict] = []
    unassigned: list[str] = []
    for candidate in candidates:
        if candidate.created_at is None:
            unassigned.append(candidate.transcript_id)
            continue
        matches = tuple(
            occurrence
            for occurrence in occurrences
            if occurrence.series_id == candidate.series_id
            and selection_bounds(occurrence)[0] <= candidate.created_at <= selection_bounds(occurrence)[1]
        )
        if len(matches) == 1:
            assignments[matches[0].occurrence_id].append(candidate)
        elif len(matches) > 1:
            conflicts.append(
                AssignmentConflict(
                    transcript_id=candidate.transcript_id,
                    created_at=candidate.created_at,
                    occurrence_ids=tuple(item.occurrence_id for item in matches),
                )
            )
        else:
            unassigned.append(candidate.transcript_id)
    return AssignmentResult.from_mutable(assignments, conflicts, unassigned)
```

- [ ] **Step 4: Run occurrence tests and verify GREEN**

Run:

```bash
./.venv/bin/python -m pytest tests/test_occurrence_assignment.py -q
```

Expected: four passing tests.

- [ ] **Step 5: Integrate the pure decision into Graph selection**

In `sync.py`:

- Add `series_master_id: str | None = None` to `OutlookMeetingCandidate`.
- Add
  `occurrence_context: tuple[OccurrenceWindow, ...] = ()` to
  `OutlookMeetingCandidate`.
- Parse Graph `seriesMasterId`.
- Add `_meetings_with_occurrence_context()` that groups snapshot meetings by
  `teams_meeting_id`, with `series_master_id` as a fallback series label, and
  returns `replace(meeting, occurrence_context=group_windows)` for every
  meeting. Call it once before the plan loop.
- Extend `TranscriptDiagnostics` with
  `assignment_conflicts: tuple[AssignmentConflict, ...] = ()`.
- Change `_select_transcripts_for_occurrence()` to return:

```python
tuple[tuple[GraphTranscriptRecord, ...], tuple[AssignmentConflict, ...]]
```

- Inside it, convert `meeting.occurrence_context` and the fetched Graph records
  to `OccurrenceWindow` and `TranscriptCandidate`, call
  `assign_transcripts()`, then map the uniquely assigned transcript IDs back to
  `GraphTranscriptRecord` objects.
- Update both `GraphTranscriptDiscoveryClient` and
  `GraphTranscriptDownloadClient` call sites to unpack selected records and
  conflicts and to place conflicts in `TranscriptDiagnostics`.
- When the current occurrence has a conflict, return no selected records.
- Keep timestamp-free records rejected and keep chronological multi-segment
  selection.

Use the occurrence event ID as the primary occurrence ID. Do not derive cadence
from subject, weekday, or recurrence pattern.

- [ ] **Step 6: Add sync-level structured diagnostic tests**

Add tests to `tests/test_meeting_sync.py` that construct two same-series
occurrences with overlapping windows and assert:

```python
self.assertEqual(plan.items[0].decision, "process")
self.assertIn(
    "meeting_sync_occurrence_error: ambiguous_transcript_assignment",
    render_transcript_sync_plan(plan),
)
self.assertIn("candidate_transcript_id: ambiguous", render_transcript_sync_plan(plan))
self.assertNotIn("selected_transcript_id: ambiguous", render_transcript_sync_plan(plan))
```

Also retain the existing recurring-series tests for timestamp-free,
wrong-occurrence, pagination, and stale VTT behavior.

- [ ] **Step 7: Run focused occurrence and recurring-transcript tests**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_occurrence_assignment.py \
  tests/test_meeting_sync.py \
  -k "occurrence or recurring or transcript_diagnostics or timestamp_free or stale" -q
```

Expected: all selected tests pass and ambiguous records are never selected.

- [ ] **Step 8: Review checkpoint**

Run:

```bash
git diff --check
git diff -- src/obsidian_intake_agent/meetings/occurrence_assignment.py src/obsidian_intake_agent/meetings/sync.py tests/test_occurrence_assignment.py tests/test_meeting_sync.py
```

Expected: pure assignment logic, Graph integration, and diagnostics only; do
not stage or commit.

## Task 3: Centralize Backward-Compatible Marker State

**Files:**
- Create: `src/obsidian_intake_agent/meetings/identity_state.py`
- Create: `tests/test_meeting_identity_state.py`
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`

- [ ] **Step 1: Write failing marker interpretation tests**

Create `tests/test_meeting_identity_state.py` with:

```python
from datetime import datetime

from obsidian_intake_agent.meetings.identity_state import load_identity_state


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


FALLBACK_PAYLOAD = {
    "schema_version": 2,
    "source_type": "meeting_bundle_processed",
    "processing_source_kind": "fallback",
    "upgrade_state": "awaiting_transcript",
    "retry_until": "2026-07-30T16:30:00+00:00",
}


def test_schema_v2_fallback_is_upgradeable_before_retry_deadline() -> None:
    state = load_identity_state(
        payload={
            "schema_version": 2,
            "source_type": "meeting_bundle_processed",
            "processing_source_kind": "fallback",
            "upgrade_state": "awaiting_transcript",
            "retry_until": "2026-07-30T16:30:00+00:00",
        },
        scheduled_end=dt("2026-07-29T16:30:00+00:00"),
    )
    assert state.is_terminal(now=dt("2026-07-29T18:00:00+00:00")) is False

def test_schema_v2_fallback_is_terminal_after_retry_deadline() -> None:
    state = load_identity_state(payload=FALLBACK_PAYLOAD, scheduled_end=dt("2026-07-29T16:30:00+00:00"))
    assert state.is_terminal(now=dt("2026-07-30T17:00:00+00:00")) is True

def test_transcript_processed_state_is_immediately_terminal() -> None:
    state = load_identity_state(
        payload={
            "schema_version": 2,
            "source_type": "meeting_bundle_processed",
            "processing_source_kind": "transcript",
            "upgrade_state": "terminal",
        },
        scheduled_end=dt("2026-07-29T16:30:00+00:00"),
    )
    assert state.is_terminal(now=dt("2026-07-29T16:31:00+00:00")) is True

def test_legacy_fallback_infers_retry_deadline_from_scheduled_end() -> None:
    state = load_identity_state(
        payload={
            "source_type": "meeting_bundle_processed",
            "preferred_input_source_name": "Copilot recap / AI summary",
        },
        scheduled_end=dt("2026-07-29T16:30:00+00:00"),
    )
    assert state.processing_source_kind == "fallback"
    assert state.retry_until == dt("2026-07-30T16:30:00+00:00")

def test_legacy_processed_marker_without_source_information_is_terminal() -> None:
    state = load_identity_state(
        payload={"source_type": "meeting_bundle_processed"},
        scheduled_end=dt("2026-07-29T16:30:00+00:00"),
    )
    assert state.is_terminal(now=dt("2026-07-29T16:31:00+00:00")) is True
```

Also test legacy `meeting_sync_identity`, missing source type, malformed JSON,
and current `meeting_sync_pending`.

- [ ] **Step 2: Run marker tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest tests/test_meeting_identity_state.py -q
```

Expected: import failure because `identity_state.py` does not exist.

- [ ] **Step 3: Implement marker interpretation**

Implement:

```python
@dataclass(frozen=True, slots=True)
class MeetingIdentityState:
    marker_kind: Literal["pending", "processed", "malformed", "unknown"]
    processing_source_kind: Literal["fallback", "transcript", "manual", "unknown"]
    upgrade_state: Literal["awaiting_transcript", "terminal", "manual_review_required", "unknown"]
    retry_until: datetime
    payload: dict[str, object]

    def is_terminal(self, *, now: datetime) -> bool:
        if self.marker_kind in {"malformed", "unknown"}:
            return True
        if self.marker_kind == "pending":
            return False
        if self.processing_source_kind in {"transcript", "manual", "unknown"}:
            return True
        return self.upgrade_state == "manual_review_required" or now > self.retry_until
```

`load_identity_state()` must infer legacy source kind from
`preferred_input_source_name`, use `scheduled_end + timedelta(hours=24)` when
`retry_until` is absent, and never treat unreadable processed state as
upgradeable.

Provide `read_identity_state(path, scheduled_end)` and atomic marker-writing
helpers so both sync and bundle processing use one implementation.

- [ ] **Step 4: Replace duplicate marker predicates**

In `sync.py` and `process_bundles.py`, replace
`_identity_marker_indicates_processed()`,
`_identity_marker_indicates_pending()`, and
`_marker_indicates_bundle_processed()` decisions with
`read_identity_state()`.

Keep the old marker source-type strings unchanged for compatibility.

- [ ] **Step 5: Run marker, sync-marker, and bundle-marker tests**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_meeting_identity_state.py \
  tests/test_meeting_sync.py \
  tests/test_meeting_process_bundles.py \
  -k "marker or identity or processed" -q
```

Expected: all selected tests pass, including existing marker fixtures.

- [ ] **Step 6: Review checkpoint**

Run `git diff --check` and inspect only the marker module, its tests, and
replacements of old predicates. Do not stage or commit.

## Task 4: Add Grace Gating and Fallback-Processed Polling

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
- Modify: `src/obsidian_intake_agent/main.py`
- Modify: `tests/test_meeting_sync.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Write failing grace-period state-machine tests**

Add focused tests with a meeting ending at `13:30`:

```python
def _grace_plan(
    *,
    now: str,
    artifacts: tuple[MeetingArtifact, ...],
    grace_minutes: int = 60,
) -> TranscriptSyncPlan:
    return build_transcript_sync_plan(
        client=_StubMeetingDiscoveryClient(
            meetings=(
                _meeting(
                    event_id="grace-occurrence",
                    subject="Delivery Review",
                    response_status="accepted",
                    join_url=(
                        "https://teams.microsoft.com/l/meetup-join/"
                        "19%3Ameeting_grace%40thread.v2/0?context=%7B%7D"
                    ),
                    online_meeting_provider="teamsForBusiness",
                ),
            )
        ),
        artifact_discovery_client=_StubArtifactDiscoveryClient(artifacts=artifacts),
        since=date(2026, 7, 29),
        intake_root=Path("/tmp/vault/00_Intake"),
        now=datetime.fromisoformat(now),
        transcript_grace_minutes=grace_minutes,
    )


def test_recap_ten_minutes_after_end_is_staged_but_not_processor_ready(self) -> None:
    recap = Path("/tmp/vault/00_Intake/bundles/fallbacks/recap.md")
    plan = _grace_plan(
        now="2026-05-04T13:40:00+00:00",
        artifacts=(
            MeetingArtifact("Copilot recap / AI summary", "available", matched_paths=(recap,)),
        ),
    )
    note = plan.items[0].intake_bundle_note
    assert note is not None
    self.assertIsNone(note.processor_input_path)
    self.assertTrue(any("Recap fallback deferred until" in reason for reason in plan.items[0].reasons))

def test_transcript_during_grace_wins(self) -> None:
    transcript = Path("/tmp/vault/00_Intake/bundles/raw_transcripts/transcript.vtt")
    recap = Path("/tmp/vault/00_Intake/bundles/fallbacks/recap.md")
    plan = _grace_plan(
        now="2026-05-04T13:40:00+00:00",
        artifacts=(
            MeetingArtifact("Teams .vtt transcript", "available", matched_paths=(transcript,)),
            MeetingArtifact("Copilot recap / AI summary", "available", matched_paths=(recap,)),
        ),
    )
    note = plan.items[0].intake_bundle_note
    assert note is not None
    self.assertEqual(note.processor_input_source_name, "Teams .vtt transcript")

def test_recap_after_grace_is_processor_ready(self) -> None:
    recap = Path("/tmp/vault/00_Intake/bundles/fallbacks/recap.md")
    plan = _grace_plan(
        now="2026-05-04T14:31:00+00:00",
        artifacts=(
            MeetingArtifact("Copilot recap / AI summary", "available", matched_paths=(recap,)),
        ),
    )
    note = plan.items[0].intake_bundle_note
    assert note is not None
    self.assertEqual(note.processor_input_source_name, "Copilot recap / AI summary")
```

Add tests that a fallback-processed marker:

- calls transcript discovery before 24 hours;
- does not invoke `GraphMeetingFallbackSummaryClient`;
- writes no refreshed bundle when no transcript exists;
- writes an upgrade bundle when a unique transcript appears;
- becomes terminal after 24 hours.

- [ ] **Step 2: Run the new sync tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest tests/test_meeting_sync.py -k "grace or fallback_processed" -q
```

Expected: failures because recap handoff is immediate and processed markers
prefilter all discovery.

- [ ] **Step 3: Add grace-aware planning**

Change `build_transcript_sync_plan()` to accept:

```python
transcript_grace_minutes: int = 60
```

Calculate:

```python
fallback_not_before = meeting.end_at + timedelta(minutes=transcript_grace_minutes)
retry_until = meeting.end_at + timedelta(hours=24)
```

Pass `allow_summary_fallback=generated_at >= fallback_not_before` into
`_preferred_processor_input()`. When recap is available but disallowed, keep it
in `sources_used` and artifacts but return no recap processor handoff.

Serialize `fallback_not_before`, `retry_until`, occurrence ID, scheduled bounds,
and selection bounds into pending and Outlook metadata sidecars.

- [ ] **Step 4: Add transcript-only rediscovery**

Add `without_summary_fallback()` to
`ChainedMeetingArtifactDiscoveryClient`. It returns the same chain without
clients carrying `provides_summary_fallback = True`.

Set:

```python
class GraphMeetingFallbackSummaryClient:
    provides_summary_fallback = True
```

For a fallback-processed marker inside `retry_until`, use the transcript-only
chain. If no valid transcript becomes available, return a skip plan item after
discovery and do not write new bundle artifacts. If a transcript is available,
create an upgrade bundle while retaining the processed marker until execution
succeeds.

- [ ] **Step 5: Wire configuration through the CLI**

In `main.py`, pass:

```python
transcript_grace_minutes=config.meeting_transcript_grace_minutes,
```

Add a `tests/test_main.py` patch assertion that
`build_transcript_sync_plan()` receives `60` by default and the configured
override.

- [ ] **Step 6: Run focused sync and CLI tests**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_meeting_sync.py \
  tests/test_main.py \
  -k "grace or fallback_processed or sync_transcripts" -q
```

Expected: grace, late-polling, terminal-state, and config-wiring tests pass.

- [ ] **Step 7: Review checkpoint**

Run `git diff --check`, inspect sync/main/test diffs, and confirm no Graph
selection tolerance or stale-VTT rule was weakened. Do not stage or commit.

## Task 5: Carry Bundle Context into Clean Canonical Notes

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Modify: `src/obsidian_intake_agent/processors/meeting_processor.py`
- Modify: `src/obsidian_intake_agent/rendering/meeting_renderer.py`
- Modify: `tests/test_meeting_process_bundles.py`
- Modify: `tests/test_processor.py`
- Modify: `tests/test_renderer.py`

- [ ] **Step 1: Write failing presentation and deferred-bundle tests**

Add tests asserting:

```python
self.assertEqual(plan.ready_count, 0)
self.assertIn("Recap fallback is deferred until", plan.items[0].reasons)
```

for recap metadata before `fallback_not_before`.

Add an execution test whose staged input filename ends with `(fallback).md` and
assert:

```python
canonical = vault / "01_Meetings" / "2026-07-29 - Teams - Delivery Review.md"
self.assertTrue(canonical.exists())
self.assertFalse((vault / "01_Meetings" / "2026-07-29 - Teams - Delivery Review (fallback).md").exists())
text = canonical.read_text(encoding="utf-8")
self.assertIn('artifact_state: "fallback"', text)
self.assertIn('sources_used: ["Copilot recap / AI summary", "Teams meeting chat", "Outlook calendar metadata"]', text)
self.assertIn("summary-derived and not a verbatim transcript", text)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_meeting_process_bundles.py \
  tests/test_processor.py \
  tests/test_renderer.py \
  -k "deferred or fallback or artifact_state" -q
```

Expected: failures because bundle processing ignores grace metadata and generic
Markdown processing derives `(fallback)` from the filename.

- [ ] **Step 3: Extend bundle metadata and processor context**

Extend `BundleMetadataRecord` with scheduled end, fallback deadline, retry
deadline, organizer/attendee/ID fields, and a computed source context. Extend
`BundleArtifactRecord` with the serialized transcript diagnostics dictionary so
selected transcript IDs and creation timestamps remain available when the
processed marker is written.

Add to `MeetingProcessor.process_file()`:

```python
meeting_metadata: MeetingMetadata | None = None,
meeting_context: dict[str, object] | None = None,
source_note_aliases: dict[str, str] | None = None,
```

Use `meeting_metadata or normalize_meeting_metadata(source_path)` for Markdown
and VTT. Merge bundle `sources_used`, `source_limitations`, IDs, times, and
`artifact_state` into renderer context. Normal intake callers continue passing
nothing and retain current behavior.

In bundle execution, construct authoritative metadata from the Outlook sidecar:

```python
MeetingMetadata(
    date=metadata.start_at.date().isoformat(),
    source="Teams",
    title=metadata.subject,
    canonical_basename=f"{metadata.start_at.date().isoformat()} - Teams - {safe_title}.md",
)
```

- [ ] **Step 4: Render structured source state**

Add `artifact_state` and `supersedes_note` to `FRONT_MATTER_KEYS` and
`render_meeting_front_matter()`. Pass them from both meeting renderers.

For a recap bundle, populate:

```python
{
    "artifact_state": "fallback",
    "sources_used": available_bundle_sources,
    "source_limitations": [
        *bundle_limitations,
        "Processor input is summary-derived and not a verbatim transcript.",
    ],
}
```

For transcript input, use `artifact_state: transcript` and omit only the recap
limitation.

- [ ] **Step 5: Block recap handoff before its deadline**

In `_plan_bundle_processing_item()`, before processor skip checks:

```python
if (
    metadata.processor_handoff.preferred_input_source_name == "Copilot recap / AI summary"
    and metadata.fallback_not_before is not None
    and now < metadata.fallback_not_before
):
    return BundleProcessingPlanItem(
        decision="blocked",
        metadata=metadata,
        reasons=(f"Recap fallback is deferred until {metadata.fallback_not_before.isoformat()}.",),
    )
```

Pass `now` into this helper from `build_bundle_processing_plan()`.

- [ ] **Step 6: Run processor, renderer, and bundle tests**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_meeting_process_bundles.py \
  tests/test_processor.py \
  tests/test_renderer.py -q
```

Expected: all tests pass, including existing normal-intake behavior.

- [ ] **Step 7: Review checkpoint**

Run `git diff --check` and confirm the processor API additions are optional,
fallback titles are clean, and source limitations are explicit. Do not stage
or commit.

## Task 6: Write Enriched Processed Markers

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Modify: `src/obsidian_intake_agent/meetings/identity_state.py`
- Modify: `tests/test_meeting_process_bundles.py`
- Modify: `tests/test_meeting_identity_state.py`

- [ ] **Step 1: Write failing processed-marker payload tests**

For fallback processing, assert:

```python
self.assertEqual(marker["schema_version"], 2)
self.assertEqual(marker["processing_source_kind"], "fallback")
self.assertEqual(marker["upgrade_state"], "awaiting_transcript")
self.assertEqual(marker["retry_until"], "2026-07-30T13:30:00+00:00")
self.assertRegex(marker["canonical_note_sha256"], r"^[0-9a-f]{64}$")
```

For transcript processing, assert:

```python
self.assertEqual(marker["processing_source_kind"], "transcript")
self.assertEqual(marker["upgrade_state"], "terminal")
self.assertEqual(marker["selected_transcripts"][0]["id"], "transcript-29")
```

- [ ] **Step 2: Run focused marker-writing tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest tests/test_meeting_process_bundles.py -k "processed_marker" -q
```

Expected: failures because current markers lack schema, state, retry, hash, and
selected-transcript fields.

- [ ] **Step 3: Extend durable marker writing**

Before overwriting the pending marker, read its payload and preserve
`first_seen_at`, `retry_until`, and occurrence fields.

Determine source kind:

```python
source_kind = (
    "fallback"
    if metadata.processor_handoff.preferred_input_source_name == "Copilot recap / AI summary"
    else "transcript"
    if metadata.processor_handoff.preferred_input_source_name
    in {"Teams .vtt transcript", "Teams transcript text"}
    else "manual"
)
```

Hash the written canonical note bytes with SHA-256. Serialize transcript IDs
and creation timestamps from artifact diagnostics. Set `upgrade_state` to
`awaiting_transcript` only for fallback; otherwise `terminal`.

- [ ] **Step 4: Run marker and bundle tests**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_meeting_identity_state.py \
  tests/test_meeting_process_bundles.py \
  -k "marker or fallback or transcript" -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Review checkpoint**

Inspect the marker payload diff for compatibility: `source_type` remains
`meeting_bundle_processed`; additions are schema-v2 fields. Do not stage or
commit.

## Task 7: Implement Safe, Idempotent Transcript Upgrades

**Files:**
- Create: `src/obsidian_intake_agent/meetings/meeting_upgrade.py`
- Create: `tests/test_meeting_upgrade.py`
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Modify: `src/obsidian_intake_agent/rendering/action_renderer.py`
- Modify: `src/obsidian_intake_agent/processors/meeting_processor.py`
- Modify: `tests/test_meeting_process_bundles.py`
- Modify: `tests/test_processor.py`

- [ ] **Step 1: Write failing pure upgrade-helper tests**

Create `tests/test_meeting_upgrade.py` with:

```python
import hashlib
from pathlib import Path

from obsidian_intake_agent.meetings.meeting_upgrade import (
    archive_canonical_note,
    canonical_matches_hash,
    migrate_action_backlinks,
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_note(root: Path, content: str) -> Path:
    path = root / "meeting.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_detects_edited_canonical_note(tmp_path: Path) -> None:
    note = tmp_path / "meeting.md"
    note.write_text("edited", encoding="utf-8")
    assert canonical_matches_hash(note, sha256_text("original")) is False

def test_archive_path_is_recoverable_and_collision_safe(tmp_path: Path) -> None:
    first = archive_canonical_note(
        canonical_path=write_note(tmp_path, "fallback"),
        archive_root=tmp_path / "z_Archive" / "Intake" / "Meeting Upgrades",
    )
    second = archive_canonical_note(
        canonical_path=write_note(tmp_path, "fallback"),
        archive_root=tmp_path / "z_Archive" / "Intake" / "Meeting Upgrades",
    )
    assert first.exists()
    assert second.exists()
    assert first != second

def test_migrates_only_exact_action_backlinks_and_preserves_checkbox_state() -> None:
    original = (
        "- [x] Send recap (Owner: Matthew Rouser) — Source: 2026-07-29 "
        "[[2026-07-29 - Teams - Delivery Review (fallback).md]]\n"
        "- [ ] Unrelated — Source: 2026-07-29 [[Other.md]]\n"
    )
    updated = migrate_action_backlinks(
        original,
        old_note="2026-07-29 - Teams - Delivery Review (fallback).md",
        new_note="2026-07-29 - Teams - Delivery Review.md",
    )
    assert "- [x] Send recap" in updated
    assert "[[2026-07-29 - Teams - Delivery Review.md]]" in updated
    assert "[[Other.md]]" in updated
```

- [ ] **Step 2: Run helper tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest tests/test_meeting_upgrade.py -q
```

Expected: import failure because `meeting_upgrade.py` does not exist.

- [ ] **Step 3: Implement pure upgrade helpers**

Implement:

```python
def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def canonical_matches_hash(path: Path, expected_hash: str | None) -> bool:
    return bool(expected_hash) and path.is_file() and hmac.compare_digest(sha256_path(path), expected_hash)

def archive_canonical_note(*, canonical_path: Path, archive_root: Path) -> Path:
    archive_root.mkdir(parents=True, exist_ok=True)
    digest = sha256_path(canonical_path)[:12]
    candidate = archive_root / f"{canonical_path.stem} - before transcript upgrade - {digest}.md"
    if candidate.exists() and candidate.read_bytes() == canonical_path.read_bytes():
        return candidate
    suffix = 2
    while candidate.exists():
        candidate = archive_root / (
            f"{canonical_path.stem} - before transcript upgrade - {digest}-{suffix}.md"
        )
        suffix += 1
    safe_write_text(candidate, canonical_path.read_text(encoding="utf-8"))
    return candidate

def migrate_action_backlinks(text: str, *, old_note: str, new_note: str) -> str:
    return text.replace(f"[[{old_note}]]", f"[[{new_note}]]")
```

Add immutable snapshot/restore helpers for canonical and action files so failed
upgrade execution can restore prior bytes atomically.

- [ ] **Step 4: Make action dedupe understand source aliases**

Add optional `source_note_aliases: dict[str, str] | None = None` to
`render_actions_note()` and `normalize_action_for_key()`. Normalize
`record.source_note` through that mapping before building the key.

Pass the aliases from `MeetingProcessor.process_file()` to
`_prepare_action_note_update()` and `render_actions_note()`.

Existing callers pass `None` and retain current behavior.

- [ ] **Step 5: Write failing end-to-end upgrade tests**

Add bundle tests covering:

1. Unedited fallback marker + valid transcript:
   - clean canonical note contains `artifact_state: "transcript"`;
   - prior fallback canonical is archived under
     `z_Archive/Intake/Meeting Upgrades`;
   - old `(fallback)` active note is gone;
   - actions contain one clean backlink;
   - marker is terminal and contains archive provenance.
2. Edited canonical hash mismatch:
   - canonical and actions are byte-for-byte unchanged;
   - transcript remains staged;
   - marker becomes `manual_review_required`;
   - rendered execution output names the canonical note.
3. Second run after successful upgrade:
   - bundle is blocked as transcript-terminal;
   - no additional archive or action is created.
4. Processor or marker-write failure:
   - canonical and actions are restored from snapshots;
   - staged transcript remains available for retry.

- [ ] **Step 6: Run end-to-end tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_meeting_process_bundles.py \
  tests/test_processor.py \
  -k "upgrade or edited_fallback or action_backlink or rollback" -q
```

Expected: failures because fallback-processed markers still block bundle
execution and no upgrade coordinator exists.

- [ ] **Step 7: Integrate the upgrade coordinator**

In `_plan_bundle_processing_item()`:

- allow a processed fallback marker only when the preferred handoff is a Teams
  transcript and `now <= retry_until`;
- block transcript-processed and expired fallback markers;
- block and report `manual_review_required`.

In `execute_bundle_processing_plan()`:

1. Load the existing fallback state.
2. Compare the current canonical hash.
3. On mismatch, atomically update only the marker to
   `manual_review_required`, keep the staged transcript, and skip processing.
4. On match, snapshot canonical/actions, archive the fallback, and invoke
   `MeetingProcessor` with clean metadata, transcript context, and fallback
   source alias.
5. Migrate old action backlinks after successful processor output.
6. Write the terminal marker with `supersedes_note` and archived path.
7. On any processor, migration, or marker exception, restore snapshots and
   leave staging intact.
8. Clean machine-managed staging only after all upgrade outputs are durable.

- [ ] **Step 8: Run upgrade, bundle, processor, and action tests**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_meeting_upgrade.py \
  tests/test_meeting_process_bundles.py \
  tests/test_processor.py \
  tests/test_action_renderer.py -q
```

Expected: all tests pass, including checkbox/order preservation and rerun
idempotency.

- [ ] **Step 9: Review checkpoint**

Run `git diff --check` and inspect upgrade diffs for three invariants: edited
notes never enter processor execution, rollback restores prior bytes, and
staging cleanup occurs last. Do not stage or commit.

## Task 8: Complete Diagnostics, Documentation, and Regression Coverage

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Modify: `src/obsidian_intake_agent/meetings/__init__.py`
- Modify: `tests/test_meeting_sync.py`
- Modify: `tests/test_meeting_process_bundles.py`
- Modify: `README.md`
- Modify: `docs/meeting_transcript_automation.md`

- [ ] **Step 1: Add final structured-output assertions**

Assert exact output keys for:

```text
meeting_sync_occurrence_errors: 1
meeting_sync_occurrence_error: ambiguous_transcript_assignment
meeting_sync_fallback_deferred: 1
meeting_sync_fallback_awaiting_transcript: 1
meeting_sync_late_transcript_upgrades: 1
meeting_bundle_process_manual_review_required: 1
meeting_bundle_process_upgrade_archived_note: <path>
```

Also assert candidate transcript IDs/timestamps, scheduled window, selection
window, and conflicting occurrence details are present.

- [ ] **Step 2: Run diagnostic tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_meeting_sync.py \
  tests/test_meeting_process_bundles.py \
  -k "render or diagnostic or ambiguity or manual_review" -q
```

Expected: only newly added aggregate/output assertions fail.

- [ ] **Step 3: Implement aggregate and item diagnostics**

Add plan/result properties for deferred fallbacks, awaiting-transcript
fallbacks, late upgrades, ambiguity errors, and manual-review results. Render
the exact keys from Step 1 without turning per-occurrence ambiguity into a
whole-command failure.

- [ ] **Step 4: Update operator documentation**

Update both docs to state:

- `meeting_transcript_grace_minutes` defaults to 60;
- selection tolerances remain 15 minutes early and 30 minutes late;
- recurrence cadence is irrelevant because markers are per occurrence;
- recap-pending, fallback-processed, manual-review, and terminal states;
- fallback upgrades continue until 24 hours after scheduled end;
- ambiguity output includes candidates and conflicting occurrences;
- edited canonical notes are never overwritten automatically;
- archived fallback notes live below the configured intake archive root in
  `Meeting Upgrades`;
- historical fallback notes are not bulk-renamed.

- [ ] **Step 5: Run focused meeting suites**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/test_occurrence_assignment.py \
  tests/test_meeting_identity_state.py \
  tests/test_meeting_upgrade.py \
  tests/test_meeting_sync.py \
  tests/test_meeting_process_bundles.py \
  tests/test_processor.py \
  tests/test_action_renderer.py \
  tests/test_renderer.py \
  tests/test_config.py \
  tests/test_main.py -q
```

Expected: all focused suites pass.

- [ ] **Step 6: Review checkpoint**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: changes are limited to the spec, plan, meeting sync/state/upgrade
implementation, focused tests, configuration example, and docs. Do not stage or
commit.

## Task 9: Full Verification and Review Handoff

**Files:**
- Verify all changed files; do not add new scope.

- [ ] **Step 1: Run repository checks**

Run in order:

```bash
make check
make test
make smoke
make build
make audit
```

Expected: every command exits zero. If a test fails only because the restricted
environment blocks a loopback socket or Graph DNS, rerun that exact command
with the required environment permission and report both results.

- [ ] **Step 2: Run a safe transcript-processing dry run**

Run:

```bash
./.venv/bin/obsidian-agent meetings sync-transcripts --since 2026-07-22 --dry-run
./.venv/bin/obsidian-agent meetings process-bundles --dry-run
```

Expected: no vault writes; output distinguishes deferred fallbacks,
fallback-processed polling, terminal transcripts, and any occurrence ambiguity.
Do not run `--download-transcripts` or `--execute` against the production vault
as part of validation.

- [ ] **Step 3: Review the complete diff**

Run:

```bash
git diff --check
git diff --stat
git status --short --branch
```

Inspect the full diff for secrets, machine-specific paths, unrelated changes,
weakened occurrence validation, and destructive vault operations.

- [ ] **Step 4: Stop for user review**

Report:

- confirmed root cause;
- implemented state and upgrade design;
- files changed;
- focused and full verification commands with counts/results;
- dry-run result;
- compatibility and residual risks;
- focused diff summary.

Do not stage, commit, push, open a PR, or modify historical vault meeting notes.
