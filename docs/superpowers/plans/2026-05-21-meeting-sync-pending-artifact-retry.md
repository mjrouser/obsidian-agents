# Meeting Sync Pending Artifact Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep unprocessed meeting bundles retryable for 24 hours so late Teams transcripts or recap artifacts can be discovered and processed automatically.

**Architecture:** Preserve the current sync -> bundle sidecar -> process-bundles flow, but split identity state into pending and processed markers. Pending markers refresh bundle/metadata sidecars during the retry window; processed markers remain durable idempotency guards.

**Tech Stack:** Python 3.11, stdlib `dataclasses`, `datetime`, `json`, `pathlib`, `unittest`, existing `obsidian_intake_agent.meetings.sync` and `process_bundles` modules.

---

## File Structure

- Modify `src/obsidian_intake_agent/meetings/sync.py`: add pending marker parsing/rendering, 24-hour retry-window decisions, sidecar refresh behavior, and expired-network-retry handling.
- Modify `tests/test_meeting_sync.py`: replace identity-marker skip tests with pending/processed marker tests and add refresh/expiration coverage.
- Modify `README.md` and `docs/meeting_transcript_automation.md`: document pending retry semantics and the 24-hour retry window.
- Do not commit automatically. Project instructions require Matthew to review, test, commit, and merge manually.

---

### Task 1: Add Pending Marker Semantics

**Files:**
- Modify: `tests/test_meeting_sync.py`
- Modify: `src/obsidian_intake_agent/meetings/sync.py`

- [ ] **Step 1: Write failing tests for pending and processed markers**

In `tests/test_meeting_sync.py`, replace `test_planning_skips_meeting_when_identity_marker_already_exists` with:

```python
def test_pending_identity_marker_retries_artifact_discovery_inside_retry_window(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        intake_root = Path(tmp_dir) / "00_Intake"
        intake_root.mkdir(parents=True)
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        planning_probe = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            since=date(2026, 5, 1),
            intake_root=intake_root,
            now=now,
        )
        existing_identity = planning_probe.items[0].intake_bundle_note.identity_path
        existing_identity.parent.mkdir(parents=True)
        existing_identity.write_text(
            json.dumps(
                {
                    "source_type": "meeting_sync_pending",
                    "first_seen_at": "2026-05-04T13:35:00+00:00",
                    "last_checked_at": "2026-05-04T13:35:00+00:00",
                    "retry_until": "2026-05-05T13:30:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        discovery_client = _RecordingArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="available",
                    detail="Matched local intake artifact(s): transcript.vtt",
                    matched_paths=(intake_root / "bundles" / "raw_transcripts" / "transcript.vtt",),
                ),
            )
        )

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            artifact_discovery_client=discovery_client,
            since=date(2026, 5, 1),
            intake_root=intake_root,
            now=now,
        )

        self.assertEqual(discovery_client.calls, 1)
        self.assertEqual(plan.process_count, 1)
        self.assertEqual(plan.items[0].decision, "process")
        self.assertIn("Retrying pending meeting artifact discovery.", plan.items[0].reasons)

def test_processed_identity_marker_skips_artifact_discovery(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        intake_root = Path(tmp_dir) / "00_Intake"
        intake_root.mkdir(parents=True)
        planning_probe = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            since=date(2026, 5, 1),
            intake_root=intake_root,
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )
        existing_identity = planning_probe.items[0].intake_bundle_note.identity_path
        existing_identity.parent.mkdir(parents=True)
        existing_identity.write_text('{"source_type": "meeting_bundle_processed"}\n', encoding="utf-8")
        discovery_client = _RecordingArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="available",
                    detail="Should never be discovered after processing.",
                ),
            )
        )

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            artifact_discovery_client=discovery_client,
            since=date(2026, 5, 1),
            intake_root=intake_root,
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        self.assertEqual(discovery_client.calls, 0)
        self.assertEqual(plan.items[0].decision, "skip")
        self.assertEqual(plan.items[0].reasons, ("Skipped meeting because it was already processed.",))
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_pending_identity_marker_retries_artifact_discovery_inside_retry_window tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_processed_identity_marker_skips_artifact_discovery -q
```

Expected: the pending-marker test fails because any existing identity marker still skips discovery.

- [ ] **Step 3: Implement marker classification**

In `src/obsidian_intake_agent/meetings/sync.py`, add after `GRAPH_REQUEST_TIMEOUT_SECONDS = 30`:

```python
PENDING_MARKER_SOURCE_TYPE = "meeting_sync_pending"
LEGACY_PENDING_MARKER_SOURCE_TYPE = "meeting_sync_identity"
PROCESSED_MARKER_SOURCE_TYPE = "meeting_bundle_processed"
PENDING_ARTIFACT_RETRY_HOURS = 24
```

Add helper functions near `_meeting_identity_exists`:

```python
def _read_identity_marker(marker_path: Path) -> dict[str, object]:
    try:
        return json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _identity_marker_source_type(marker_path: Path) -> str | None:
    payload = _read_identity_marker(marker_path)
    source_type = payload.get("source_type")
    return source_type if isinstance(source_type, str) else None


def _marker_indicates_processed(marker_path: Path) -> bool:
    return _identity_marker_source_type(marker_path) == PROCESSED_MARKER_SOURCE_TYPE
```

Replace the identity-marker branch in `_should_prefilter_artifact_discovery` with:

```python
    if intake_root is not None:
        marker_path = intake_root / _meeting_identity_relative_path(meeting)
        if marker_path.exists() and _marker_indicates_processed(marker_path):
            return True
```

Replace the identity-marker branch in `_plan_item` with:

```python
    if intake_bundle_note is not None and intake_bundle_note.identity_path.exists():
        if _marker_indicates_processed(intake_bundle_note.identity_path):
            reasons.append("Skipped meeting because it was already processed.")
            return TranscriptSyncPlanItem("skip", meeting, bundle, tuple(reasons), intake_bundle_note)
        reasons.append("Retrying pending meeting artifact discovery.")
```

- [ ] **Step 4: Run the tests again**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_pending_identity_marker_retries_artifact_discovery_inside_retry_window tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_processed_identity_marker_skips_artifact_discovery -q
```

Expected: both tests pass.

---

### Task 2: Refresh Pending Sidecars

**Files:**
- Modify: `tests/test_meeting_sync.py`
- Modify: `src/obsidian_intake_agent/meetings/sync.py`

- [ ] **Step 1: Write a failing sidecar-refresh test**

Replace `test_write_planned_bundle_notes_writes_processable_notes_once` with:

```python
def test_write_planned_bundle_notes_refreshes_pending_bundle_sidecars(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        intake_root = Path(tmp_dir) / "00_Intake"
        now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
        meeting = _meeting(
            event_id="evt-1",
            subject="Platform Sync",
            response_status="accepted",
            join_url=(
                "https://teams.microsoft.com/l/meetup-join/"
                "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
            ),
            online_meeting_provider="teamsForBusiness",
        )
        first_plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            since=date(2026, 5, 1),
            intake_root=intake_root,
            now=now,
        )
        first_result = write_planned_bundle_notes(first_plan)
        self.assertEqual(first_result.written_count, 1)

        transcript_path = intake_root / "bundles" / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
        transcript_path.parent.mkdir(parents=True)
        transcript_path.write_text("WEBVTT\n", encoding="utf-8")
        second_plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(meetings=(meeting,)),
            artifact_discovery_client=_RecordingArtifactDiscoveryClient(
                artifacts=(
                    MeetingArtifact(
                        source_name="Teams .vtt transcript",
                        status="available",
                        detail=f"Matched local intake artifact(s): {transcript_path}",
                        matched_paths=(transcript_path,),
                    ),
                )
            ),
            since=date(2026, 5, 1),
            intake_root=intake_root,
            now=now,
        )
        second_result = write_planned_bundle_notes(second_plan)

        self.assertEqual(second_result.written_count, 1)
        metadata_path = intake_root / "bundles" / "2026-05-04 - Teams - Platform Sync (outlook).json"
        metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata_payload["processor_handoff"]["preferred_input_path"], str(transcript_path))
```

- [ ] **Step 2: Run the failing refresh test**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_write_planned_bundle_notes_refreshes_pending_bundle_sidecars -q
```

Expected: FAIL because existing bundle and metadata files are skipped instead of refreshed.

- [ ] **Step 3: Update write behavior**

In `write_planned_bundle_notes`, change the existing-file behavior for bundle note, metadata, and identity marker from always skip to refresh when the marker is not processed:

```python
        should_refresh_pending = note.identity_path.exists() and not _marker_indicates_processed(note.identity_path)
        if note.path.exists() and not should_refresh_pending:
            skipped_existing_bundle_note_paths.append(note.path)
        else:
            note.path.write_text(note.content + "\n", encoding="utf-8")
            written_bundle_note_paths.append(note.path)
        if note.metadata_path.exists() and not should_refresh_pending:
            skipped_existing_metadata_paths.append(note.metadata_path)
        else:
            note.metadata_path.write_text(note.metadata_content + "\n", encoding="utf-8")
            written_metadata_paths.append(note.metadata_path)
        if note.identity_path.exists() and _marker_indicates_processed(note.identity_path):
            skipped_existing_identity_paths.append(note.identity_path)
        else:
            note.identity_path.write_text(note.identity_content + "\n", encoding="utf-8")
            written_identity_paths.append(note.identity_path)
```

- [ ] **Step 4: Run the refresh test**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_write_planned_bundle_notes_refreshes_pending_bundle_sidecars -q
```

Expected: PASS.

---

### Task 3: Render Pending Marker Metadata

**Files:**
- Modify: `tests/test_meeting_sync.py`
- Modify: `src/obsidian_intake_agent/meetings/sync.py`

- [ ] **Step 1: Update marker rendering test**

In `test_render_meeting_identity_sidecar_renders_expected_json`, change the expected source type and add retry fields:

```python
self.assertIn('"source_type": "meeting_sync_pending"', rendered)
self.assertIn('"first_seen_at":', rendered)
self.assertIn('"last_checked_at":', rendered)
self.assertIn('"retry_until": "2026-05-05T13:30:00+00:00"', rendered)
self.assertIn('"artifact_statuses": [', rendered)
```

- [ ] **Step 2: Run the marker test**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_render_meeting_identity_sidecar_renders_expected_json -q
```

Expected: FAIL because marker rendering still writes `meeting_sync_identity` and lacks retry fields.

- [ ] **Step 3: Update marker renderer**

Change `render_meeting_identity_sidecar` to emit:

```python
payload = {
    "source_type": PENDING_MARKER_SOURCE_TYPE,
    "identity_key": _meeting_identity_key(meeting),
    "outlook_event_id": meeting.event_id,
    "subject": meeting.subject,
    "meeting_date": meeting.start_at.date().isoformat(),
    "teams_meeting_id": meeting.teams_meeting_id(),
    "bundle_note_path": str(bundle_note_path),
    "outlook_metadata_path": str(metadata_path),
    "first_seen_at": _format_datetime(meeting.end_at),
    "last_checked_at": _format_datetime(datetime.now(tz=meeting.end_at.tzinfo or UTC)),
    "retry_until": _format_datetime(meeting.end_at + timedelta(hours=PENDING_ARTIFACT_RETRY_HOURS)),
    "artifact_statuses": [
        {
            "source_name": artifact.source_name,
            "status": artifact.status,
            "detail": artifact.detail,
            "matched_paths": [str(path) for path in artifact.matched_paths],
        }
        for artifact in bundle.artifacts
    ],
}
```

Also import `timedelta` from `datetime` at the top of `sync.py`.

- [ ] **Step 4: Run the marker test**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_render_meeting_identity_sidecar_renders_expected_json -q
```

Expected: PASS.

---

### Task 4: Expire Network Retries After 24 Hours

**Files:**
- Modify: `tests/test_meeting_sync.py`
- Modify: `src/obsidian_intake_agent/meetings/sync.py`

- [ ] **Step 1: Write the expired-pending test**

Add this test near the pending marker tests:

```python
def test_expired_pending_marker_skips_remote_artifact_discovery(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        intake_root = Path(tmp_dir) / "00_Intake"
        intake_root.mkdir(parents=True)
        planning_probe = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            since=date(2026, 5, 1),
            intake_root=intake_root,
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )
        existing_identity = planning_probe.items[0].intake_bundle_note.identity_path
        existing_identity.parent.mkdir(parents=True)
        existing_identity.write_text(
            json.dumps(
                {
                    "source_type": "meeting_sync_pending",
                    "retry_until": "2026-05-05T13:30:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        discovery_client = _RecordingArtifactDiscoveryClient(
            artifacts=(
                MeetingArtifact(
                    source_name="Teams .vtt transcript",
                    status="available",
                    detail="Remote discovery should not run after expiration.",
                ),
            )
        )

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
                        join_url=(
                            "https://teams.microsoft.com/l/meetup-join/"
                            "19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D"
                        ),
                        online_meeting_provider="teamsForBusiness",
                    ),
                )
            ),
            artifact_discovery_client=discovery_client,
            since=date(2026, 5, 1),
            intake_root=intake_root,
            now=datetime.fromisoformat("2026-05-05T14:00:00+00:00"),
        )

        self.assertEqual(discovery_client.calls, 0)
        self.assertEqual(plan.items[0].decision, "process")
        self.assertIn(
            "Artifact retry window expired after 24 hours; network artifact discovery was skipped.",
            plan.items[0].reasons,
        )
```

- [ ] **Step 2: Run the expired-pending test**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_expired_pending_marker_skips_remote_artifact_discovery -q
```

Expected: FAIL because expired pending markers are not recognized.

- [ ] **Step 3: Add retry-expiration helpers**

In `sync.py`, add:

```python
def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _pending_retry_until(meeting: OutlookMeetingCandidate, marker_path: Path) -> datetime:
    payload = _read_identity_marker(marker_path)
    explicit_retry_until = _parse_datetime(payload.get("retry_until"))
    if explicit_retry_until is not None:
        return explicit_retry_until
    return meeting.end_at + timedelta(hours=PENDING_ARTIFACT_RETRY_HOURS)


def _pending_retry_expired(
    meeting: OutlookMeetingCandidate,
    *,
    marker_path: Path,
    now: datetime,
) -> bool:
    return now > _pending_retry_until(meeting, marker_path)
```

Update `_should_prefilter_artifact_discovery` so an expired pending marker returns `True`.

Update `_plan_item` so expired pending markers still produce a `process` item with the retry-expired reason. This keeps `write_planned_bundle_notes` able to refresh metadata with the expired state.

- [ ] **Step 4: Run the expired-pending test**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_expired_pending_marker_skips_remote_artifact_discovery -q
```

Expected: PASS.

---

### Task 5: Verify Bundle Processing Still Produces Processed Markers

**Files:**
- Modify: `tests/test_meeting_sync.py` if needed
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py` only if tests expose a regression

- [ ] **Step 1: Run existing processed-marker test**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py::TranscriptSyncPlannerTests::test_execute_cleans_up_bundle_staging_and_writes_processed_marker_after_success -q
```

Expected: PASS. The processed marker should still have `"source_type": "meeting_bundle_processed"`.

- [ ] **Step 2: Fix only if needed**

If the test fails because constants moved, update `process_bundles.py` to keep writing:

```python
"source_type": "meeting_bundle_processed"
```

Do not change cleanup behavior unless a focused test fails.

---

### Task 6: Update Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/meeting_transcript_automation.md`

- [ ] **Step 1: Update README meeting-sync paragraph**

In `README.md`, replace the identity-marker sentence with:

```markdown
The sync path writes a hidden pending identity marker under
`00_Intake/bundles/_meeting_sync` for staged but unprocessed meetings, then
refreshes that pending bundle for up to 24 hours after meeting end so late Teams
transcripts or recap artifacts can be picked up on later polling cycles. After
`process-bundles --execute` succeeds, the pending marker is replaced by a durable
processed marker that blocks future reprocessing.
```

- [ ] **Step 2: Update automation docs**

In `docs/meeting_transcript_automation.md`, add under the sync behavior bullets:

```markdown
- Calendar-only bundles remain pending, not final, for 24 hours after the
  meeting ends. During that window each polling cycle refreshes artifact
  discovery and rewrites bundle metadata so a late Teams `.vtt`, transcript text,
  or recap fallback can become the preferred processor input.
- After 24 hours, sync stops Graph network retries for that meeting but keeps
  local exact-match artifact discovery available for manual transcript
  attachment.
```

- [ ] **Step 3: Run docs grep sanity check**

Run:

```bash
rg -n "meeting identity marker already exists|meeting_sync_identity|24 hours|pending" README.md docs/meeting_transcript_automation.md
```

Expected: docs describe pending markers and 24-hour retry behavior, not permanent calendar-only identity skips.

---

### Task 7: Run Focused and Broader Validation

**Files:**
- No code changes unless tests fail.

- [ ] **Step 1: Run focused sync tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_meeting_sync.py tests/test_meeting_process_bundles.py -q
```

Expected: PASS.

- [ ] **Step 2: Run priority project checks**

Run:

```bash
make check
make test
```

Expected: both pass. If either target is unavailable, run the nearest project equivalent and report the substitution.

- [ ] **Step 3: Review diffs**

Run:

```bash
git diff -- src/obsidian_intake_agent/meetings/sync.py tests/test_meeting_sync.py README.md docs/meeting_transcript_automation.md
```

Expected: diff is limited to pending artifact retry behavior, tests, and docs.

---

## Completion Notes

After checks pass, do not commit automatically. Report:

- tests/checks run and results
- changed files
- any residual risk
- exact commit commands for Matthew to run manually

