# Recurring Meeting Transcript Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every recurring Teams calendar occurrence downloads and processes only the transcript segments belonging to that occurrence, while safely recovering stale machine-managed VTT files.

**Architecture:** Preserve Graph transcript timestamps as structured records, select records inside a bounded window around the Outlook occurrence, and download selected segments chronologically. Write a small provenance sidecar next to each managed VTT so later runs can validate the local artifact; legacy files inside the retry window are compared with the selected Graph content and preserved under `bundles/fallbacks/stale_transcripts` before replacement when mismatched.

**Tech Stack:** Python 3.11+, standard-library dataclasses/datetime/hashlib/json/pathlib, unittest, existing Microsoft Graph fetch abstractions.

**Repository constraint:** Do not commit automatically. The project requires Matthew to review and commit manually, so each task ends with a diff/status checkpoint instead of a commit.

---

### Task 1: Select Graph transcripts for the calendar occurrence

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py:174-190, 667-725, 2361-2372`
- Test: `tests/test_meeting_sync.py:1500-1668`

- [x] **Step 1: Write the failing recurring-occurrence test**

Add a test where Graph returns an older transcript first and the current occurrence second:

```python
def test_graph_transcript_download_selects_record_for_current_occurrence(self) -> None:
    # Meeting runs 2026-07-17 17:00-17:25 UTC.
    # Graph returns July 8 first and July 17 second.
    # Assert only current-transcript content is requested and written.
```

- [x] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_graph_transcript_download_selects_record_for_current_occurrence -v
```

Expected: FAIL because the downloader requests the first, older transcript ID.

- [x] **Step 3: Introduce structured transcript records and selection**

Add a frozen record and helpers in `sync.py`:

```python
@dataclass(slots=True, frozen=True)
class GraphTranscriptRecord:
    transcript_id: str
    created_at: datetime | None


def _graph_transcript_records(payload: dict[str, object]) -> tuple[GraphTranscriptRecord, ...]:
    raw_items = payload.get("value")
    if not isinstance(raw_items, list):
        raise ValueError("Graph transcript response did not contain a value list.")
    records: list[GraphTranscriptRecord] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        transcript_id = _optional_string(str(item.get("id") or ""))
        if transcript_id is None:
            continue
        created_value = item.get("createdDateTime")
        created_text = str(created_value).replace("Z", "+00:00") if created_value else None
        records.append(
            GraphTranscriptRecord(
                transcript_id=transcript_id,
                created_at=_parse_datetime(created_text),
            )
        )
    return tuple(records)


def _select_transcripts_for_occurrence(
    records: tuple[GraphTranscriptRecord, ...],
    meeting: OutlookMeetingCandidate,
) -> tuple[GraphTranscriptRecord, ...]:
    window_start = meeting.start_at - timedelta(minutes=15)
    window_end = meeting.end_at + timedelta(minutes=30)
    selected = tuple(
        sorted(
            (record for record in records if record.created_at and window_start <= record.created_at <= window_end),
            key=lambda record: record.created_at or datetime.min.replace(tzinfo=UTC),
        )
    )
    if selected:
        return selected
    if len(records) == 1 and records[0].created_at is None:
        return records
    return ()
```

Selection rules:

- Window: 15 minutes before `start_at` through 30 minutes after `end_at`.
- Return every timestamped record inside the window, sorted chronologically, to support restarted transcription.
- Preserve backward compatibility only when Graph returns exactly one record without a timestamp.
- Return no selection for ambiguous multiple records without usable timestamps.
- Never fall back to `records[0]` when several recurring-occurrence records exist.

- [x] **Step 4: Download selected segments in chronological order**

Replace `transcript_ids[0]` with the selected records. Add a focused `_merge_vtt_segments()` helper that keeps one `WEBVTT` header and appends each normalized segment in order. Preserve the current metadata-content fallback for the single-selected-record case.

- [x] **Step 5: Verify GREEN and existing download behavior**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_graph_transcript_download_selects_record_for_current_occurrence \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_graph_transcript_download_writes_vtt_and_marks_processor_ready -v
```

Expected: both PASS.

- [x] **Step 6: Add edge-case tests**

Add separate tests proving:

- Two segments inside one occurrence are downloaded and merged chronologically.
- No matching timestamp returns `missing` and downloads nothing.
- Multiple timestamp-free records return `missing` instead of selecting the first.
- A single timestamp-free record remains supported.

- [x] **Step 7: Run the meeting-sync test module**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meeting_sync -v
```

Expected: PASS.

- [x] **Step 8: Review the task diff without committing**

```bash
git diff --check
git diff -- src/obsidian_intake_agent/meetings/sync.py tests/test_meeting_sync.py
git status --short
```

### Task 2: Record transcript provenance and repair stale managed files

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py:620-805, 1840-1915`
- Test: `tests/test_meeting_sync.py:1620-1745, 2200-2470`

- [x] **Step 1: Write the failing stale-local-file test**

Create an existing managed VTT containing old content, return a current-occurrence Graph transcript with different content, and assert that the result points to the corrected VTT while the old bytes remain under `bundles/fallbacks/stale_transcripts`.

- [x] **Step 2: Run the focused test and verify RED**

Expected: FAIL because the current early return trusts every existing VTT without checking provenance.

- [x] **Step 3: Define the provenance payload**

Write `<meeting>.vtt.provenance.json` atomically with:

```json
{
  "event_id": "evt-1",
  "occurrence_start_at": "2026-07-17T17:00:00+00:00",
  "occurrence_end_at": "2026-07-17T17:25:00+00:00",
  "transcripts": [
    {"id": "current-transcript", "created_at": "2026-07-17T17:01:00+00:00"}
  ],
  "content_sha256": "0123456789abcdef"
}
```

Use existing atomic JSON-writing conventions in the module. Do not put tokens, URLs, attendees, or transcript content in this file.

- [x] **Step 4: Validate or repair local VTTs**

- If a matching provenance sidecar exists, return the VTT without network content download.
- If provenance is absent, discover the occurrence transcript and compare normalized bytes.
- If bytes match, backfill provenance without moving the VTT.
- If bytes differ, move the old managed VTT to `bundles/fallbacks/stale_transcripts/<stem>-<hash8>.vtt`, write the current bytes, and write provenance.
- If no occurrence transcript can be selected, preserve the local file but return `missing`; do not feed unverifiable content to the processor.

- [x] **Step 5: Add idempotency and safety tests**

Add separate tests proving:

- Matching provenance avoids redownloading content.
- A legacy VTT with matching bytes only gets provenance added.
- A stale VTT is preserved and replaced exactly once.
- Rerunning after repair does not create another stale copy.
- No matching occurrence preserves the local file and does not mark it processor-ready.

- [x] **Step 6: Run focused and module tests**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meeting_sync -v
```

Expected: PASS.

- [x] **Step 7: Review the task diff without committing**

```bash
git diff --check
git status --short
```

### Task 3: Expose occurrence selection in bundle metadata and diagnostics

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py:1178-1305, 1840-1915`
- Test: `tests/test_meeting_sync.py:500-900, 2100-2550`

- [x] **Step 1: Write failing rendering tests**

Assert dry-run and written identity metadata include candidate count, selected transcript IDs/timestamps, occurrence time, and a stale-file recovery reason without including transcript content.

- [x] **Step 2: Verify RED**

Run the named rendering tests and confirm the new diagnostic fields are absent.

- [x] **Step 3: Carry safe provenance through `MeetingArtifact` serialization**

Add optional structured provenance to `MeetingArtifact`, include it in bundle/identity JSON, and render concise lines such as:

```text
transcript_candidates: 3
selected_transcript_id: current-transcript
selected_for_occurrence: 2026-07-17T17:00:00+00:00
stale_local_transcript: preserved and replaced
```

- [x] **Step 4: Verify GREEN and confidentiality**

Assert output contains IDs/timestamps/recovery status but not VTT text, Graph tokens, or attendee data beyond existing calendar metadata.

- [x] **Step 5: Run meeting-sync and bundle-processing tests**

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_meeting_sync tests.test_meeting_process_bundles -v
```

Expected: PASS.

### Task 4: Documentation and full verification

**Files:**
- Modify: `README.md`
- Verify: all changed source and tests

- [x] **Step 1: Document recurring-meeting behavior**

Add a short operator note explaining occurrence-aware selection, provenance sidecars, stale preservation, and the safe dry-run recovery workflow.

- [x] **Step 2: Run required checks**

```bash
make check
make test
make smoke
make build
make audit
```

Expected: all commands exit 0. Run `make test` outside the restricted sandbox if loopback binding is denied.

- [x] **Step 3: Run production-vault dry-run only**

```bash
.venv/bin/obsidian-agent meetings sync-transcripts --since 2026-07-17 --dry-run
```

Expected: the Marcus occurrence reports a selected transcript timestamp matching the July 17 1:00 PM meeting. Do not download or process production data during this step.

- [x] **Step 4: Final review without committing**

```bash
git diff --check
git status --short
git diff --stat
```

Confirm only the plan, meeting-sync implementation, focused tests, and README changed.
