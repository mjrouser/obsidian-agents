# Local Transcript Fallbacks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the local/manual meeting transcript lane while Microsoft Graph tenant permissions are pending.

**Architecture:** Keep automatic selection conservative: exact local matches and explicit attachments may become processor handoffs, while near matches are diagnostics only. Extend the existing meeting sync and bundle processing modules instead of creating a parallel ingestion path.

**Tech Stack:** Python 3.11+, argparse CLI, stdlib `unittest`, existing `MeetingProcessor`, `meetings/sync.py`, and `meetings/process_bundles.py`.

**Issues:** [#23](https://github.com/mjrouser/obsidian-agents/issues/23), [#24](https://github.com/mjrouser/obsidian-agents/issues/24), [#25](https://github.com/mjrouser/obsidian-agents/issues/25), [#26](https://github.com/mjrouser/obsidian-agents/issues/26)

---

## File Structure

- Modify `src/obsidian_intake_agent/meetings/sync.py`: local transcript diagnostics, artifact/source typing, sidecar rendering.
- Modify `src/obsidian_intake_agent/meetings/process_bundles.py`: attach-transcript helper functions and improved readiness output.
- Modify `src/obsidian_intake_agent/main.py`: add `meetings attach-transcript` CLI.
- Modify `tests/test_meeting_sync.py`: sync planner and local discovery tests.
- Modify `tests/test_meeting_process_bundles.py`: readiness output and attach helper tests.
- Modify `tests/test_main.py`: CLI parser and command behavior tests.
- Modify `README.md` and `docs/meeting_transcript_automation.md`: operator workflow docs.

## Task 1: Local Near-Match Diagnostics (#23)

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
- Test: `tests/test_meeting_sync.py`

- [ ] **Step 1: Add failing test for same-date candidates**

Add a test near the existing `LocalIntakeTranscriptDiscoveryClient` tests:

```python
def test_local_transcript_discovery_reports_same_date_candidates_when_exact_match_missing(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        intake_root = Path(tmpdir) / "00_Intake"
        intake_root.mkdir(parents=True)
        candidate = intake_root / "2026-05-04 - Teams - Delivery Planning.vtt"
        candidate.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n", encoding="utf-8")
        meeting = _meeting(subject="Delivery Review", start_at=datetime(2026, 5, 4, 14, 0, tzinfo=UTC))

        artifacts = LocalIntakeTranscriptDiscoveryClient(intake_root=intake_root).discover_artifacts(meeting=meeting)

    vtt_artifact = next(artifact for artifact in artifacts if artifact.source_name == "Teams .vtt transcript")
    self.assertEqual(vtt_artifact.status, "missing")
    self.assertIn("Expected local transcript stem: 2026-05-04 - Teams - Delivery Review", vtt_artifact.detail or "")
    self.assertIn("Same-date local candidate(s):", vtt_artifact.detail or "")
    self.assertIn("Delivery Planning.vtt", vtt_artifact.detail or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync.TranscriptSyncPlannerTests.test_local_transcript_discovery_reports_same_date_candidates_when_exact_match_missing -v
```

Expected: FAIL because the missing detail does not include expected stem or candidates.

- [ ] **Step 3: Implement candidate diagnostics**

In `LocalIntakeTranscriptDiscoveryClient`, add a helper that finds same-date transcript candidates without selecting them:

```python
    def _same_date_candidate_paths(self, meeting: OutlookMeetingCandidate) -> tuple[Path, ...]:
        expected_date = meeting.start_at.date().isoformat()
        candidates: list[Path] = []
        for path in self._intake_root.rglob("*"):
            if not path.is_file():
                continue
            if path.relative_to(self._intake_root).parts[:1] == ("_meeting_sync",):
                continue
            if path.suffix.lower() not in {".vtt", ".md", ".docx"}:
                continue
            if not path.stem.startswith(f"{expected_date} - Teams - "):
                continue
            candidates.append(path)
        return tuple(sorted(candidates, key=_matching_intake_path_sort_key))
```

Change `discover_artifacts()` so the missing detail is built once:

```python
        expected_stem = self._expected_stem(meeting)
        same_date_candidates = tuple(
            path for path in self._same_date_candidate_paths(meeting) if path not in matching_paths
        )
        missing_detail = _local_missing_detail(expected_stem=expected_stem, candidates=same_date_candidates)
```

Add:

```python
    def _expected_stem(self, meeting: OutlookMeetingCandidate) -> str:
        return f"{meeting.start_at.date().isoformat()} - Teams - {_normalized_bundle_title(meeting.subject)}"
```

and use `_expected_stem()` inside `_matching_intake_paths()`.

Add module helper:

```python
def _local_missing_detail(*, expected_stem: str, candidates: tuple[Path, ...]) -> str:
    if not candidates:
        return f"Expected local transcript stem: {expected_stem}; no same-date local transcript candidates found."
    rendered_candidates = ", ".join(str(path) for path in candidates)
    return (
        f"Expected local transcript stem: {expected_stem}; "
        f"same-date local candidate(s): {rendered_candidates}. "
        "Candidates are suggestions only and were not selected automatically."
    )
```

- [ ] **Step 4: Update exact match test if needed**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync.TranscriptSyncPlannerTests.test_local_transcript_discovery_marks_matching_vtt_as_available -v
```

Expected: PASS; exact match behavior is unchanged.

- [ ] **Step 5: Run focused meeting sync tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync -v
```

Expected: PASS.

## Task 2: Improve Bundle Readiness Output (#25)

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Test: `tests/test_meeting_process_bundles.py`

- [ ] **Step 1: Add failing test for calendar-only next steps**

Add:

```python
def test_dry_run_calendar_only_bundle_prints_local_next_step(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        intake_root = Path(tmpdir) / "00_Intake"
        _write_bundle_metadata_sidecar(
            intake_root=intake_root,
            meeting=_meeting(event_id="evt-calendar", subject="Delivery Review"),
        )
        processor = _FakeProcessor(intake_root=intake_root)

        plan = build_bundle_processing_plan(intake_root=intake_root / "bundles", processor=processor)
        rendered = render_bundle_processing_plan(plan)

    self.assertIn("expected_local_transcript_stem: 2026-05-04 - Teams - Delivery Review", rendered)
    self.assertIn("next_step: add or attach a local .vtt, .md, or .docx transcript, then rerun process-bundles --dry-run", rendered)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_process_bundles.BundleProcessingPlanTests.test_dry_run_calendar_only_bundle_prints_local_next_step -v
```

Expected: FAIL because the output lacks the next-step lines.

- [ ] **Step 3: Extend metadata record with start date**

In `BundleMetadataRecord`, add:

```python
    start_at: datetime | None
```

In `_load_bundle_metadata_record()`, parse:

```python
        start_at=_datetime_or_none(payload.get("start_at")),
```

Add helper:

```python
def _datetime_or_none(value: object) -> datetime | None:
    text = _string_or_none(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
```

- [ ] **Step 4: Render expected stem and next step**

Add helpers:

```python
def _expected_local_transcript_stem(metadata: BundleMetadataRecord) -> str | None:
    if metadata.start_at is None:
        return None
    title = metadata.subject.strip().replace("/", "-").replace(":", "-").replace("\\", "-") or "Meeting"
    return f"{metadata.start_at.date().isoformat()} - Teams - {title}"


def _bundle_next_step(metadata: BundleMetadataRecord) -> str:
    return (
        "add or attach a local .vtt, .md, or .docx transcript, "
        "then rerun process-bundles --dry-run"
    )
```

In `render_bundle_processing_plan()`, for blocked items with missing preferred input, render:

```python
        expected_stem = _expected_local_transcript_stem(item.metadata)
        if expected_stem is not None:
            lines.append(f"  expected_local_transcript_stem: {expected_stem}")
        lines.append(f"  next_step: {_bundle_next_step(item.metadata)}")
```

- [ ] **Step 5: Add failing test for missing preferred file detail**

Add:

```python
def test_dry_run_missing_preferred_file_prints_missing_path_and_source(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        intake_root = Path(tmpdir) / "00_Intake"
        bundle_root = intake_root / "bundles"
        bundle_root.mkdir(parents=True)
        missing_input = bundle_root / "raw_transcripts" / "2026-05-04 - Teams - Delivery Review.vtt"
        _write_ready_bundle_metadata(
            metadata_path=bundle_root / "delivery-review (outlook).json",
            preferred_input_path=missing_input,
            preferred_input_source_name="Teams .vtt transcript",
        )
        processor = _FakeProcessor(intake_root=intake_root)

        plan = build_bundle_processing_plan(intake_root=bundle_root, processor=processor)
        rendered = render_bundle_processing_plan(plan)

    self.assertIn(f"missing_preferred_input: {missing_input}", rendered)
    self.assertIn("preferred_source: Teams .vtt transcript", rendered)
```

- [ ] **Step 6: Implement missing preferred file line**

When rendering blocked items whose reasons include `Preferred processor input file does not exist on disk.`, add:

```python
        if "Preferred processor input file does not exist on disk." in item.reasons:
            missing_path = item.metadata.processor_handoff.preferred_input_path
            if missing_path is not None:
                lines.append(f"  missing_preferred_input: {missing_path}")
```

- [ ] **Step 7: Run focused bundle processing tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_process_bundles -v
```

Expected: PASS.

## Task 3: Add Attach Transcript Command (#24)

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Modify: `src/obsidian_intake_agent/main.py`
- Test: `tests/test_meeting_process_bundles.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Add attach result dataclass and failing success test**

In `tests/test_meeting_process_bundles.py`, add:

```python
def test_attach_transcript_copies_file_and_updates_metadata_handoff(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        intake_root = Path(tmpdir) / "00_Intake"
        source = Path(tmpdir) / "download.vtt"
        source.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n", encoding="utf-8")
        _write_bundle_metadata_sidecar(
            intake_root=intake_root,
            meeting=_meeting(event_id="evt-attach", subject="Delivery Review"),
        )

        result = attach_transcript_to_bundle(bundle_root=intake_root / "bundles", event_id="evt-attach", file_path=source)

        self.assertTrue(result.attached_path.exists())
        payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["processor_handoff"]["preferred_input_path"], str(result.attached_path))
        self.assertEqual(payload["processor_handoff"]["preferred_input_source_name"], "Manual / semi-manual intake")
        self.assertIn("Manual / semi-manual intake", [artifact["source_name"] for artifact in payload["artifacts"]])
```

Import `attach_transcript_to_bundle`.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_process_bundles.BundleProcessingPlanTests.test_attach_transcript_copies_file_and_updates_metadata_handoff -v
```

Expected: FAIL because `attach_transcript_to_bundle` does not exist.

- [ ] **Step 3: Implement attach helper**

In `process_bundles.py`, add:

```python
@dataclass(slots=True, frozen=True)
class AttachTranscriptResult:
    metadata_path: Path
    attached_path: Path
    source_name: str


SUPPORTED_ATTACH_EXTENSIONS = {".vtt", ".md", ".docx"}
```

Add:

```python
def attach_transcript_to_bundle(*, bundle_root: Path, event_id: str, file_path: Path) -> AttachTranscriptResult:
    if file_path.suffix.lower() not in SUPPORTED_ATTACH_EXTENSIONS:
        raise ValueError(f"Unsupported transcript file type: {file_path.suffix}")
    if not file_path.exists() or file_path.is_dir():
        raise ValueError(f"Transcript file does not exist: {file_path}")

    metadata_paths = _metadata_paths_for_event_id(bundle_root=bundle_root, event_id=event_id)
    if not metadata_paths:
        raise ValueError(f"No meeting bundle metadata found for Outlook event ID: {event_id}")
    if len(metadata_paths) > 1:
        rendered = ", ".join(str(path) for path in metadata_paths)
        raise ValueError(f"Multiple meeting bundle metadata files matched Outlook event ID {event_id}: {rendered}")

    metadata_path = metadata_paths[0]
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    attached_dir = bundle_root / "raw_transcripts"
    attached_dir.mkdir(parents=True, exist_ok=True)
    attached_path = attached_dir / file_path.name
    if attached_path.exists():
        raise ValueError(f"Refusing to overwrite existing staged transcript: {attached_path}")
    attached_path.write_bytes(file_path.read_bytes())
    _update_payload_for_attached_transcript(payload=payload, attached_path=attached_path)
    metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return AttachTranscriptResult(metadata_path=metadata_path, attached_path=attached_path, source_name="Manual / semi-manual intake")
```

Add helpers:

```python
def _metadata_paths_for_event_id(*, bundle_root: Path, event_id: str) -> tuple[Path, ...]:
    matches: list[Path] = []
    for metadata_path in sorted(bundle_root.glob("* (outlook).json")):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if _string_or_none(payload.get("outlook_event_id")) == event_id:
            matches.append(metadata_path)
    return tuple(matches)


def _update_payload_for_attached_transcript(*, payload: dict[str, object], attached_path: Path) -> None:
    payload["source_type"] = "manual_semi_manual_intake"
    payload["processor_handoff"] = {
        "preferred_input_path": str(attached_path),
        "preferred_input_source_name": "Manual / semi-manual intake",
    }
    raw_artifacts = payload.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    artifacts = [artifact for artifact in artifacts if not (isinstance(artifact, dict) and artifact.get("source_name") == "Manual / semi-manual intake")]
    artifacts.append(
        {
            "source_name": "Manual / semi-manual intake",
            "status": "available",
            "detail": "Manually attached local processor input.",
            "matched_paths": [str(attached_path)],
        }
    )
    payload["artifacts"] = artifacts
```

- [ ] **Step 4: Add failure tests**

Add tests for unsupported extension, missing bundle, duplicate bundle metadata, and existing staged file:

```python
with self.assertRaisesRegex(ValueError, "Unsupported transcript file type"):
    attach_transcript_to_bundle(bundle_root=bundle_root, event_id="evt", file_path=source_txt)
```

```python
with self.assertRaisesRegex(ValueError, "No meeting bundle metadata found"):
    attach_transcript_to_bundle(bundle_root=bundle_root, event_id="missing", file_path=source_vtt)
```

```python
with self.assertRaisesRegex(ValueError, "Multiple meeting bundle metadata files matched"):
    attach_transcript_to_bundle(bundle_root=bundle_root, event_id="evt-duplicate", file_path=source_vtt)
```

```python
with self.assertRaisesRegex(ValueError, "Refusing to overwrite existing staged transcript"):
    attach_transcript_to_bundle(bundle_root=bundle_root, event_id="evt-existing", file_path=source_vtt)
```

- [ ] **Step 5: Add CLI parser and command**

In `main.py`, import `attach_transcript_to_bundle` from `.meetings`.

Add parser:

```python
    attach_parser = meetings_subparsers.add_parser(
        "attach-transcript",
        help="Attach a local transcript file to an existing synced meeting bundle.",
    )
    attach_parser.add_argument("--event-id", required=True, help="Outlook event ID from the meeting bundle metadata.")
    attach_parser.add_argument("--file", required=True, help="Local .vtt, .md, or .docx transcript file to attach.")
```

Add command handling inside `if args.command == "meetings":`

```python
        if args.meetings_command == "attach-transcript":
            result = attach_transcript_to_bundle(
                bundle_root=config.vault_path / config.intake_dir / "bundles",
                event_id=args.event_id,
                file_path=Path(args.file),
            )
            print(f"meeting_bundle_transcript_attached: {result.attached_path}")
            print(f"meeting_bundle_metadata_updated: {result.metadata_path}")
            print(f"meeting_bundle_preferred_source: {result.source_name}")
            return 0
```

- [ ] **Step 6: Add CLI test**

In `tests/test_main.py`, add a test that patches `attach_transcript_to_bundle` and asserts command output includes:

```text
meeting_bundle_transcript_attached:
meeting_bundle_metadata_updated:
meeting_bundle_preferred_source:
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_process_bundles tests.test_main -v
```

Expected: PASS.

## Task 4: Distinguish Summary-Derived Inputs (#26)

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Test: `tests/test_meeting_sync.py`
- Test: `tests/test_meeting_process_bundles.py`

- [ ] **Step 1: Add source limitation test**

Add a test around existing fallback summary tests:

```python
def test_summary_fallback_bundle_records_non_verbatim_source_limitation(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        intake_root = Path(tmpdir) / "00_Intake"
        meeting = _meeting(subject="Delivery Review")
        artifact = MeetingArtifact(
            "Copilot recap / AI summary",
            "available",
            "Downloaded Copilot recap fallback summary and wrote a local processor input.",
            matched_paths=(intake_root / "bundles" / "fallbacks" / "delivery.md",),
        )
        bundle = _build_source_bundle(replace(meeting, discovered_artifacts=(artifact,)))
        note = _build_intake_bundle_note(meeting, bundle, intake_root=intake_root)

    self.assertIsNotNone(note)
    assert note is not None
    self.assertIn("summary-derived and not a verbatim transcript", note.content)
    self.assertIn("copilot_recap_ai_summary", note.metadata_content)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync.TranscriptSyncPlannerTests.test_summary_fallback_bundle_records_non_verbatim_source_limitation -v
```

Expected: FAIL if the limitation text is not present.

- [ ] **Step 3: Implement limitation helper**

In `sync.py`, add:

```python
SUMMARY_DERIVED_SOURCE_NAMES = frozenset({"Copilot recap / AI summary", "Manual / semi-manual summary"})
SUMMARY_DERIVED_LIMITATION = "Processor input is summary-derived and not a verbatim transcript."
```

In `_bundle_transparency()`, append the limitation when an available artifact source is summary-derived:

```python
    if any(artifact.status == "available" and artifact.source_name in SUMMARY_DERIVED_SOURCE_NAMES for artifact in bundle.artifacts):
        limitations.append(SUMMARY_DERIVED_LIMITATION)
```

Ensure this happens for both calendar-invite-only and partial-visibility branches without duplicating the line.

- [ ] **Step 4: Preserve precedence tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync.TranscriptSyncPlannerTests.test_vtt_transcript_remains_primary_when_summary_fallback_is_also_available -v
```

Expected: PASS.

- [ ] **Step 5: Add readiness output test if needed**

If existing output does not clearly show `preferred_source: Copilot recap / AI summary`, add a focused test in `tests/test_meeting_process_bundles.py` asserting that line appears for a summary-derived metadata sidecar.

- [ ] **Step 6: Run focused sync and bundle tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync tests.test_meeting_process_bundles -v
```

Expected: PASS.

## Task 5: Docs And Full Validation

**Files:**
- Modify: `README.md`
- Modify: `docs/meeting_transcript_automation.md`

- [ ] **Step 1: Update README commands**

Add the attach command near the existing `meetings process-bundles` docs:

```bash
.venv/bin/obsidian-agent meetings attach-transcript \
  --event-id "<outlook-event-id>" \
  --file "/absolute/path/to/transcript.vtt"
```

Document that `attach-transcript` stages the file and updates the bundle sidecar but does not process the bundle.

- [ ] **Step 2: Update automation docs**

In `docs/meeting_transcript_automation.md`, add a short "Manual local transcript lane" section:

```markdown
## Manual Local Transcript Lane

When Graph transcript permissions are unavailable, place a local `.vtt`, `.md`, or `.docx` transcript in `00_Intake` using the expected meeting filename, or attach it to an existing bundle with `meetings attach-transcript`. Near-match candidates are diagnostic only; the system processes exact matches or explicit attachments.
```

- [ ] **Step 3: Run full checks**

Run:

```bash
make check
make test
make smoke
make build
```

Expected: PASS.

- [ ] **Step 4: Run audit if dependency files changed**

Only if `pyproject.toml` or `requirements.lock` changed, run:

```bash
make audit
```

Expected: PASS. This feature should not require dependency changes.

## Execution Order

1. Implement #23 first because near-match diagnostics clarify the operator loop without changing write behavior.
2. Implement #25 next because readiness output can reuse the diagnostic language.
3. Implement #24 after output is clear, because attach-transcript introduces writes to bundle metadata.
4. Implement #26 last or alongside #24 if manual summary attachment is included in the first attach command.

## Self-Review Notes

- No Graph permission assumptions are added.
- No near-match candidate is automatically processed.
- The attach command is explicit, scoped to existing bundle sidecars, and non-destructive by default.
- The plan keeps production output untouched until the existing `process-bundles --execute` flow runs.
