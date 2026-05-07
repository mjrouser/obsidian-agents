# Meeting Ingestion Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing meeting sync pipeline so eligible Teams meetings use transcript-first ingestion, fall back to full Copilot summaries when transcripts are missing, stage artifacts under `00_Intake/bundles`, and clean up bundle staging after successful processing while preserving durable dedupe state.

**Architecture:** Keep the current `meetings sync-transcripts` -> bundle metadata -> `meetings process-bundles` flow. Tighten meeting eligibility before artifact resolution, move machine-managed bundle assets into a dedicated subtree, keep a single preferred processor handoff per meeting, and add post-success cleanup that leaves behind only compact processed identity markers.

**Tech Stack:** Python 3.11, `unittest`, existing `obsidian_intake_agent.meetings` sync and process-bundles modules, Ruff, mypy, existing CLI wiring in `main.py`.

---

## File Structure

- Modify: `src/obsidian_intake_agent/meetings/sync.py`
  - Own bundle-relative paths, meeting eligibility, preferred handoff selection, processed-marker schema, and bundle-write behavior.
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
  - Own post-success cleanup and reading the expanded durable bundle metadata contract.
- Modify: `src/obsidian_intake_agent/main.py`
  - Own user-visible command descriptions if bundle paths or lifecycle messaging changes.
- Modify: `tests/test_meeting_sync.py`
  - Own planner, path layout, fallback handoff, and cleanup coverage.
- Modify: `README.md`
  - Own operator-facing CLI behavior and filesystem layout documentation.
- Modify: `docs/meeting_transcript_automation.md`
  - Own architecture/runbook notes for bundle staging and fallback behavior.

### Task 1: Tighten Eligibility And Move Staging Under `00_Intake/bundles`

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
- Test: `tests/test_meeting_sync.py`

- [ ] **Step 1: Write the failing planner tests for RSVP and subject filtering**

```python
def test_skips_unresponded_meeting_even_when_teams_join_url_exists(self) -> None:
    now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
    plan = build_transcript_sync_plan(
        client=_StubMeetingDiscoveryClient(
            meetings=(
                _meeting(
                    event_id="evt-no-rsvp",
                    subject="Platform Sync",
                    response_status="none",
                    join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_filter%40thread.v2/0?context=%7B%7D",
                    online_meeting_provider="teamsForBusiness",
                ),
            )
        ),
        since=date(2026, 5, 1),
        now=now,
    )

    self.assertEqual(plan.process_count, 0)
    self.assertEqual(plan.items[0].decision, "skip")
    self.assertIn("Skipped event because response status indicates you did not RSVP.", plan.items[0].reasons)


def test_skips_office_hours_subject_even_when_teams_meeting_exists(self) -> None:
    now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
    plan = build_transcript_sync_plan(
        client=_StubMeetingDiscoveryClient(
            meetings=(
                _meeting(
                    event_id="evt-office-hours",
                    subject="Office Hours - Product",
                    response_status="accepted",
                    join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_filter%40thread.v2/0?context=%7B%7D",
                    online_meeting_provider="teamsForBusiness",
                ),
            )
        ),
        since=date(2026, 5, 1),
        now=now,
    )

    self.assertEqual(plan.process_count, 0)
    self.assertEqual(plan.items[0].decision, "skip")
    self.assertIn("Skipped low-signal meeting based on subject filter: office hours.", plan.items[0].reasons)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_skips_unresponded_meeting_even_when_teams_join_url_exists \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_skips_office_hours_subject_even_when_teams_meeting_exists -v
```

Expected: `FAIL` because the planner still allows these meetings through today.

- [ ] **Step 3: Implement explicit v1 eligibility filtering in the sync planner**

```python
LOW_SIGNAL_SUBJECT_PATTERNS = (
    "office hours",
)


def _plan_transcript_sync_item(
    *,
    meeting: OutlookMeetingCandidate,
    artifact_discovery_client: MeetingArtifactDiscoveryClient,
    intake_root: Path | None,
) -> TranscriptSyncPlanItem:
    if meeting.is_cancelled:
        return _skip_plan_item(meeting, "Skipped canceled meeting.")
    if _response_status_blocks_processing(meeting.response_status):
        return _skip_plan_item(meeting, "Skipped event because response status indicates you did not RSVP.")
    if _is_low_signal_subject(meeting.subject):
        return _skip_plan_item(meeting, "Skipped low-signal meeting based on subject filter: office hours.")
    # keep the existing timing, Teams-content, identity-marker, and artifact-resolution logic below


def _response_status_blocks_processing(response_status: str | None) -> bool:
    normalized = (response_status or "").strip().lower()
    return normalized in {"declined", "none", "notresponded"}


def _is_low_signal_subject(subject: str) -> bool:
    normalized = subject.strip().lower()
    return any(pattern in normalized for pattern in LOW_SIGNAL_SUBJECT_PATTERNS)
```

- [ ] **Step 4: Add failing path-layout tests for the new `00_Intake/bundles` subtree**

```python
def test_bundle_paths_live_under_machine_managed_bundles_subtree(self) -> None:
    now = datetime.fromisoformat("2026-05-04T14:00:00+00:00")
    plan = build_transcript_sync_plan(
        client=_StubMeetingDiscoveryClient(
            meetings=(
                _meeting(
                    event_id="evt-1",
                    subject="Platform Sync",
                    response_status="accepted",
                    join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_bundle123%40thread.v2/0?context=%7B%7D",
                    online_meeting_provider="teamsForBusiness",
                ),
            )
        ),
        since=date(2026, 5, 1),
        intake_root=Path("/tmp/vault/00_Intake"),
        now=now,
    )

    bundle_note = plan.items[0].intake_bundle_note
    assert bundle_note is not None
    self.assertEqual(
        bundle_note.path,
        Path("/tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Platform Sync (bundle).md"),
    )
    self.assertEqual(
        bundle_note.metadata_path,
        Path("/tmp/vault/00_Intake/bundles/2026-05-04 - Teams - Platform Sync (outlook).json"),
    )
    self.assertEqual(
        bundle_note.identity_path.parent,
        Path("/tmp/vault/00_Intake/bundles/_meeting_sync/identities"),
    )
```

- [ ] **Step 5: Run the path-layout test and verify it fails**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_bundle_paths_live_under_machine_managed_bundles_subtree -v
```

Expected: `FAIL` because bundle notes and sidecars are still written directly under `00_Intake`.

- [ ] **Step 6: Move bundle-relative paths into the dedicated bundle subtree**

```python
RAW_TRANSCRIPTS_DIR = "bundles/raw_transcripts"
FALLBACKS_DIR = "bundles/fallbacks"
BUNDLES_DIR = "bundles"
MEETING_SYNC_STATE_DIR = "bundles/_meeting_sync"


def _bundle_note_relative_path(meeting: OutlookMeetingCandidate) -> Path:
    return Path(BUNDLES_DIR) / f"{_meeting_file_stem(meeting)} (bundle).md"


def _outlook_metadata_relative_path(meeting: OutlookMeetingCandidate) -> Path:
    return Path(BUNDLES_DIR) / f"{_meeting_file_stem(meeting)} (outlook).json"


def _meeting_identity_relative_path(meeting: OutlookMeetingCandidate) -> Path:
    return Path(MEETING_SYNC_STATE_DIR) / "identities" / f"{_meeting_identity_key(meeting)}.json"
```

- [ ] **Step 7: Re-run the focused planner tests and verify they pass**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_skips_unresponded_meeting_even_when_teams_join_url_exists \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_skips_office_hours_subject_even_when_teams_meeting_exists \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_bundle_paths_live_under_machine_managed_bundles_subtree -v
```

Expected: `OK`

- [ ] **Step 8: Commit the focused filtering and path-layout changes**

```bash
git add tests/test_meeting_sync.py src/obsidian_intake_agent/meetings/sync.py
git commit -m "feat: filter low-signal meetings and isolate bundle staging"
```

### Task 2: Finalize Preferred Source Selection And Summary-Backed Bundle Metadata

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
- Test: `tests/test_meeting_sync.py`

- [ ] **Step 1: Add a failing test that transcript beats Copilot summary when both exist**

```python
def test_vtt_transcript_remains_primary_when_summary_fallback_is_also_available(self) -> None:
    plan = build_transcript_sync_plan(
        client=_StubMeetingDiscoveryClient(
            meetings=(
                _meeting(
                    event_id="evt-1",
                    subject="Platform Sync",
                    response_status="accepted",
                    join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                    online_meeting_provider="teamsForBusiness",
                    discovered_artifacts=(
                        MeetingArtifact(
                            source_name="Teams .vtt transcript",
                            status="available",
                            detail="Transcript downloaded.",
                            matched_paths=(Path("/tmp/vault/00_Intake/bundles/raw_transcripts/2026-05-04 - Teams - Platform Sync.vtt"),),
                        ),
                        MeetingArtifact(
                            source_name="Copilot recap / AI summary",
                            status="available",
                            detail="Fallback summary downloaded.",
                            matched_paths=(Path("/tmp/vault/00_Intake/bundles/fallbacks/2026-05-04 - Teams - Platform Sync (fallback).md"),),
                        ),
                    ),
                ),
            )
        ),
        since=date(2026, 5, 1),
        intake_root=Path("/tmp/vault/00_Intake"),
        now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
    )

    bundle_note = plan.items[0].intake_bundle_note
    assert bundle_note is not None
    self.assertEqual(bundle_note.processor_input_source_name, "Teams .vtt transcript")
```

- [ ] **Step 2: Add a failing test that summary-backed bundles write the expected metadata contract**

```python
def test_summary_fallback_bundle_records_summary_source_type_and_bundle_local_path(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        intake_root = Path(tmp_dir) / "00_Intake"
        fallback_path = intake_root / "bundles" / "fallbacks" / "2026-05-04 - Teams - Platform Sync (fallback).md"
        fallback_path.parent.mkdir(parents=True)
        fallback_path.write_text("# Fallback summary\n", encoding="utf-8")

        plan = build_transcript_sync_plan(
            client=_StubMeetingDiscoveryClient(
                meetings=(
                    _meeting(
                        event_id="evt-1",
                        subject="Platform Sync",
                        response_status="accepted",
                        join_url="https://teams.microsoft.com/l/meetup-join/19%3Ameeting_graph123%40thread.v2/0?context=%7B%7D",
                        online_meeting_provider="teamsForBusiness",
                        discovered_artifacts=(
                            MeetingArtifact("Teams .vtt transcript", "missing", "No transcript published."),
                            MeetingArtifact("Teams transcript text", "missing", "No transcript text published."),
                            MeetingArtifact(
                                "Copilot recap / AI summary",
                                "available",
                                "Downloaded Copilot recap fallback summary and wrote a local processor input.",
                                matched_paths=(fallback_path,),
                            ),
                            MeetingArtifact("Teams meeting chat", "available", "Fetched 2 Teams meeting chat message(s)."),
                        ),
                    ),
                )
            ),
            since=date(2026, 5, 1),
            intake_root=intake_root,
            now=datetime.fromisoformat("2026-05-04T14:00:00+00:00"),
        )

        write_planned_bundle_notes(plan)
        payload = json.loads((intake_root / "bundles" / "2026-05-04 - Teams - Platform Sync (outlook).json").read_text())
        self.assertEqual(payload["source_type"], "Copilot recap / AI summary")
        self.assertEqual(payload["processor_handoff"]["preferred_input_path"], str(fallback_path))
```

- [ ] **Step 3: Run the focused source-selection tests and verify they fail**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_vtt_transcript_remains_primary_when_summary_fallback_is_also_available \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_summary_fallback_bundle_records_summary_source_type_and_bundle_local_path -v
```

Expected: `FAIL` if source precedence or metadata writing does not fully honor the new bundle subtree contract.

- [ ] **Step 4: Normalize preferred-source selection and summary-backed metadata writing**

```python
SYNC_SOURCE_SUMMARY_FIELDS = (
    ("Teams .vtt transcript", "vtt"),
    ("Teams transcript text", "transcript_text"),
    ("Copilot recap / AI summary", "recap"),
    ("Teams meeting chat", "chat"),
)


def _preferred_processor_artifact(bundle: MeetingSourceBundle) -> MeetingArtifact | None:
    for source_name in ("Teams .vtt transcript", "Teams transcript text", "Copilot recap / AI summary"):
        artifact = bundle.artifact(source_name)
        if artifact and artifact.status == "available" and artifact.matched_paths:
            return artifact
    return None


def render_outlook_metadata_sidecar(note: PlannedIntakeBundleNote) -> str:
    payload = {
        "source_type": note.processor_input_source_name,
        "processor_handoff": {
            "preferred_input_path": str(note.processor_input_path) if note.processor_input_path else None,
            "preferred_input_source_name": note.processor_input_source_name,
        },
        # preserve the existing event and artifact fields
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
```

- [ ] **Step 5: Re-run the focused source-selection tests and the existing fallback-summary test**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_graph_meeting_fallback_summary_writes_recap_markdown_and_uses_chat_as_context \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_vtt_transcript_remains_primary_when_summary_fallback_is_also_available \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_summary_fallback_bundle_records_summary_source_type_and_bundle_local_path -v
```

Expected: `OK`

- [ ] **Step 6: Commit the preferred-source and summary-bundle contract changes**

```bash
git add tests/test_meeting_sync.py src/obsidian_intake_agent/meetings/sync.py
git commit -m "feat: formalize summary fallback bundle handoff"
```

### Task 3: Clean Up Bundle Staging After Successful Processing And Keep Durable Markers

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
- Test: `tests/test_meeting_sync.py`

- [ ] **Step 1: Add a failing execution test for post-success cleanup**

```python
def test_execute_bundle_processing_plan_removes_bundle_staging_after_success(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        intake_root = Path(tmp_dir) / "00_Intake"
        bundle_root = intake_root / "bundles"
        fallback_path = bundle_root / "fallbacks" / "2026-05-04 - Teams - Platform Sync (fallback).md"
        metadata_path = bundle_root / "2026-05-04 - Teams - Platform Sync (outlook).json"
        bundle_note_path = bundle_root / "2026-05-04 - Teams - Platform Sync (bundle).md"
        identity_path = bundle_root / "_meeting_sync" / "identities" / "identity.json"

        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps({
            "outlook_event_id": "evt-1",
            "subject": "Platform Sync",
            "processor_handoff": {
                "preferred_input_path": str(fallback_path),
                "preferred_input_source_name": "Copilot recap / AI summary",
            },
            "artifacts": [],
            "processed_marker_path": str(identity_path),
        }), encoding="utf-8")
        bundle_note_path.write_text("# Bundle\n", encoding="utf-8")
        fallback_path.write_text("# Fallback summary\n", encoding="utf-8")

        processor = _StubMeetingProcessor(
            result=ProcessResult(
                processed=True,
                canonical_note_path=Path("/tmp/vault/01_Meetings/2026-05-04 - Platform Sync.md"),
                actions_file_path=None,
                skip_reason=None,
            )
        )
        plan = build_bundle_processing_plan(intake_root=bundle_root, processor=processor)
        result = execute_bundle_processing_plan(plan, processor=processor)

        self.assertEqual(result.processed_count, 1)
        self.assertFalse(bundle_note_path.exists())
        self.assertFalse(metadata_path.exists())
        self.assertFalse(fallback_path.exists())
        self.assertTrue(identity_path.exists())
```

- [ ] **Step 2: Run the cleanup test and verify it fails**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.BundleProcessingPlanTests.test_execute_bundle_processing_plan_removes_bundle_staging_after_success -v
```

Expected: `FAIL` because successful execution currently reports success without removing staging files or writing a compact processed marker payload.

- [ ] **Step 3: Extend bundle metadata loading to carry processed-marker paths**

```python
@dataclass(slots=True, frozen=True)
class BundleMetadataRecord:
    metadata_path: Path
    bundle_note_path: Path
    event_id: str
    subject: str
    source_type: str | None
    processed_marker_path: Path | None
    processor_handoff: BundleProcessorHandoff
    artifacts: tuple[BundleArtifactRecord, ...]


def _load_bundle_metadata_record(metadata_path: Path) -> BundleMetadataRecord:
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    return BundleMetadataRecord(
        metadata_path=metadata_path,
        bundle_note_path=metadata_path.with_name(metadata_path.name.replace(" (outlook).json", " (bundle).md")),
        event_id=_string_or_default(payload.get("outlook_event_id"), metadata_path.stem),
        subject=_string_or_default(payload.get("subject"), metadata_path.stem),
        source_type=_string_or_none(payload.get("source_type")),
        processed_marker_path=_path_or_none(payload.get("processed_marker_path")),
        processor_handoff=BundleProcessorHandoff(...),
        artifacts=tuple(artifacts),
    )
```

- [ ] **Step 4: Implement success cleanup plus durable processed-marker writing**

```python
def execute_bundle_processing_plan(
    plan: BundleProcessingPlan,
    *,
    processor: MeetingProcessor,
) -> BundleExecutionResult:
    items: list[BundleExecutionResultItem] = []
    for plan_item in plan.items:
        ...
        execution_item = _execution_result_item_from_process_result(plan_item=plan_item, process_result=process_result)
        if execution_item.status == "processed":
            _write_processed_marker(plan_item=plan_item, execution_item=execution_item)
            _cleanup_bundle_staging(plan_item)
        items.append(execution_item)
    return BundleExecutionResult(...)


def _write_processed_marker(*, plan_item: BundleProcessingPlanItem, execution_item: BundleExecutionResultItem) -> None:
    marker_path = plan_item.metadata.processed_marker_path
    if marker_path is None:
        return
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "outlook_event_id": plan_item.metadata.event_id,
                "subject": plan_item.metadata.subject,
                "source_type": plan_item.metadata.source_type,
                "processed_at": datetime.now().astimezone().isoformat(),
                "canonical_note_path": str(execution_item.canonical_note_path) if execution_item.canonical_note_path else None,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


def _cleanup_bundle_staging(plan_item: BundleProcessingPlanItem) -> None:
    paths = {
        plan_item.metadata.bundle_note_path,
        plan_item.metadata.metadata_path,
    }
    preferred_input = plan_item.metadata.processor_handoff.preferred_input_path
    if preferred_input is not None and "bundles" in preferred_input.parts:
        paths.add(preferred_input)
    for artifact in plan_item.metadata.artifacts:
        for artifact_path in artifact.matched_paths:
            if "bundles" in artifact_path.parts:
                paths.add(artifact_path)
    for path in paths:
        if path.exists() and path.is_file():
            path.unlink()
```

- [ ] **Step 5: Re-run the cleanup test plus an existing dry-run plan test**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.BundleProcessingPlanTests.test_execute_bundle_processing_plan_removes_bundle_staging_after_success \
  tests.test_meeting_sync.BundleProcessingPlanTests.test_build_bundle_processing_plan_blocks_calendar_only_bundle -v
```

Expected: `OK`

- [ ] **Step 6: Commit the cleanup and durable-marker changes**

```bash
git add tests/test_meeting_sync.py src/obsidian_intake_agent/meetings/process_bundles.py src/obsidian_intake_agent/meetings/sync.py
git commit -m "feat: clean up processed bundle staging"
```

### Task 4: Update CLI And Operator Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/meeting_transcript_automation.md`
- Modify: `src/obsidian_intake_agent/main.py`

- [ ] **Step 1: Update the CLI help text if it still implies top-level intake staging**

```python
process_bundles_parser = meetings_subparsers.add_parser(
    "process-bundles",
    help="Dry-run which synced meeting bundles under 00_Intake/bundles are ready for the existing intake processor.",
)
```

- [ ] **Step 2: Update README examples and behavior notes for `00_Intake/bundles`**

```markdown
Download available Teams `.vtt` transcripts into `00_Intake/bundles/raw_transcripts`
and write matching bundle notes under `00_Intake/bundles`:

    obsidian-agent meetings sync-transcripts --since 2026-05-01 --download-transcripts

When Graph recap fallback is available, the sync command writes a processor-ready
summary Markdown file under `00_Intake/bundles/fallbacks` and records that file
as the preferred bundle handoff.
```

- [ ] **Step 3: Update the automation design doc to reflect the finalized architecture**

```markdown
- Bundle staging now lives under `00_Intake/bundles` instead of top-level `00_Intake`.
- Successful `meetings process-bundles --execute` runs remove bundle staging files
  after canonical processing and keep only compact processed identity markers for dedupe.
- Eligibility filtering now skips no-response and low-signal meetings such as office hours.
```

- [ ] **Step 4: Run formatting-free docs verification with targeted grep checks**

Run:

```bash
rg -n "00_Intake/Raw Transcripts|00_Intake/Fallbacks" README.md docs/meeting_transcript_automation.md src/obsidian_intake_agent/main.py
```

Expected: no stale references to the old bundle staging locations in the updated docs and help text.

- [ ] **Step 5: Commit the docs and help-text updates**

```bash
git add README.md docs/meeting_transcript_automation.md src/obsidian_intake_agent/main.py
git commit -m "docs: describe bundle-based meeting staging"
```

### Task 5: Run Project Validation Before Merge

**Files:**
- Test: `tests/test_meeting_sync.py`
- Verify: `Makefile`

- [ ] **Step 1: Run the full meeting-sync test module**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync -v
```

Expected: `OK`

- [ ] **Step 2: Run the required project checks in repo-preferred order**

Run:

```bash
make check
make test
make smoke
make build
```

Expected:

- `make check`: exits `0`
- `make test`: exits `0`
- `make smoke`: exits `0`
- `make build`: exits `0`

- [ ] **Step 3: Run `make audit` only if dependency or packaging files changed**

Run:

```bash
make audit
```

Expected: `0` exit status, but only if `pyproject.toml` or `requirements.lock` changed during implementation.

- [ ] **Step 4: Review the diff for unrelated churn before handing off**

Run:

```bash
git status --short
git diff -- src/obsidian_intake_agent/meetings/sync.py src/obsidian_intake_agent/meetings/process_bundles.py tests/test_meeting_sync.py README.md docs/meeting_transcript_automation.md src/obsidian_intake_agent/main.py
```

Expected: only the planned meeting-ingestion files are changed, plus any user-owned pre-existing work that should be preserved.
