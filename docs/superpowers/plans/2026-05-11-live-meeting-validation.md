# Live Meeting Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit validation mode for live meeting-ingestion checks so real meeting discovery can be exercised safely while canonical notes and Matthew-owned actions are redirected into `99_Test Notes/` instead of the production meeting and action lanes.

**Architecture:** Keep `meetings sync-transcripts` and bundle staging behavior unchanged, then add a validation-aware output-routing seam at processing time. Normal runs continue to use `01_Meetings` and `07_Actions`; validation execution uses the same source-selection and filtering logic but writes downstream outputs to `99_Test Notes/Meetings` and `99_Test Notes/Actions`.

**Tech Stack:** Python 3.11, `unittest`, existing `Config` loader, `MeetingProcessor`, bundle-processing flow in `obsidian_intake_agent.meetings.process_bundles`, CLI wiring in `main.py`, and operator docs in `README.md` and `docs/meeting_transcript_automation.md`.

---

## File Structure

- Modify: `src/obsidian_intake_agent/config.py`
  - Add permanent validation output directory settings with safe defaults.
- Modify: `src/obsidian_intake_agent/processors/meeting_processor.py`
  - Add validation-aware output routing for meeting notes and action files without changing discovery or extraction behavior.
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
  - Thread validation mode through bundle execution and expose validation output paths in result reporting.
- Modify: `src/obsidian_intake_agent/main.py`
  - Add an explicit CLI switch for validation execution and keep dry-run semantics unchanged.
- Modify: `config.example.yaml`
  - Document the permanent validation output directories.
- Modify: `README.md`
  - Document live-validation workflow and `99_Test Notes/` behavior.
- Modify: `docs/meeting_transcript_automation.md`
  - Update the operator runbook for validation execution.
- Modify: `tests/helpers.py`
  - Keep test config builders aligned with the new config fields.
- Modify: `tests/test_config.py`
  - Cover config defaults and explicit overrides for validation directories.
- Modify: `tests/test_processor.py`
  - Cover validation routing of canonical notes and Matthew-owned actions.
- Modify: `tests/test_main.py`
  - Cover CLI validation flag behavior and user-visible summaries.
- Modify: `tests/test_meeting_process_bundles.py`
  - Cover validation execution through the bundle pipeline.

### Task 1: Add Permanent Validation Output Settings

**Files:**
- Modify: `src/obsidian_intake_agent/config.py`
- Modify: `config.example.yaml`
- Modify: `tests/helpers.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing config tests for validation directory defaults and overrides**

```python
def test_load_includes_validation_output_directory_defaults(self) -> None:
    with TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    f'vault_path: "{tmp_dir}"',
                    'intake_dir: "00_Intake"',
                    'meetings_dir: "01_Meetings"',
                    'actions_dir: "07_Actions"',
                    'archive_intake_dir: "z_Archive/Intake"',
                    'templates_dir: "Templates"',
                    'owner_filter: "Matthew"',
                ]
            ),
            encoding="utf-8",
        )

        config = Config.load(config_path)

    self.assertEqual(config.validation_meetings_dir, "99_Test Notes/Meetings")
    self.assertEqual(config.validation_actions_dir, "99_Test Notes/Actions")


def test_load_allows_validation_output_directory_overrides(self) -> None:
    with TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    f'vault_path: "{tmp_dir}"',
                    'intake_dir: "00_Intake"',
                    'meetings_dir: "01_Meetings"',
                    'actions_dir: "07_Actions"',
                    'archive_intake_dir: "z_Archive/Intake"',
                    'templates_dir: "Templates"',
                    'owner_filter: "Matthew"',
                    'validation_meetings_dir: "98_Sandbox/Meetings"',
                    'validation_actions_dir: "98_Sandbox/Actions"',
                ]
            ),
            encoding="utf-8",
        )

        config = Config.load(config_path)

    self.assertEqual(config.validation_meetings_dir, "98_Sandbox/Meetings")
    self.assertEqual(config.validation_actions_dir, "98_Sandbox/Actions")
```

- [ ] **Step 2: Run the focused config tests and verify they fail**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_config.ConfigLoadTests.test_load_includes_validation_output_directory_defaults \
  tests.test_config.ConfigLoadTests.test_load_allows_validation_output_directory_overrides -v
```

Expected: `ERROR` or `FAIL` because `Config` does not yet define validation output fields.

- [ ] **Step 3: Add validation output fields to `Config` with permanent defaults**

```python
@dataclass(slots=True)
class Config:
    vault_path: Path
    intake_dir: str
    meetings_dir: str
    actions_dir: str
    archive_intake_dir: str
    templates_dir: str
    owner_filter: str
    validation_meetings_dir: str = "99_Test Notes/Meetings"
    validation_actions_dir: str = "99_Test Notes/Actions"

    @classmethod
    def load(cls, path: Path) -> Config:
        data = _load_config_data(path)
        vault_path = Path(_required_string(data, "vault_path")).expanduser()
        return cls(
            vault_path=vault_path,
            intake_dir=_required_string(data, "intake_dir"),
            meetings_dir=_required_string(data, "meetings_dir"),
            actions_dir=_required_string(data, "actions_dir"),
            validation_meetings_dir=str(data.get("validation_meetings_dir", "99_Test Notes/Meetings")),
            validation_actions_dir=str(data.get("validation_actions_dir", "99_Test Notes/Actions")),
            archive_intake_dir=_required_string(data, "archive_intake_dir"),
            templates_dir=_required_string(data, "templates_dir"),
            owner_filter=_required_string(data, "owner_filter"),
            dry_run=bool(data.get("dry_run", True)),
        )
```

- [ ] **Step 4: Update shared test helpers and example config to include the new settings**

```python
def make_config(*, vault: Path, dry_run: bool = True) -> Config:
    return Config(
        vault_path=vault,
        intake_dir="00_Intake",
        meetings_dir="01_Meetings",
        actions_dir="07_Actions",
        validation_meetings_dir="99_Test Notes/Meetings",
        validation_actions_dir="99_Test Notes/Actions",
        archive_intake_dir="z_Archive/Intake",
        templates_dir="Templates",
        owner_filter="Matthew",
        dry_run=dry_run,
    )
```

```yaml
meetings_dir: "01_Meetings"
actions_dir: "07_Actions"
validation_meetings_dir: "99_Test Notes/Meetings"
validation_actions_dir: "99_Test Notes/Actions"
archive_intake_dir: "z_Archive/Intake"
```

- [ ] **Step 5: Re-run the focused config tests and verify they pass**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_config.ConfigLoadTests.test_load_includes_validation_output_directory_defaults \
  tests.test_config.ConfigLoadTests.test_load_allows_validation_output_directory_overrides -v
```

Expected: `OK`

- [ ] **Step 6: Commit the config groundwork**

```bash
git add src/obsidian_intake_agent/config.py config.example.yaml tests/helpers.py tests/test_config.py
git commit -m "feat: add validation output directory settings"
```

### Task 2: Make Meeting Processing Validation-Aware Without Changing Extraction Logic

**Files:**
- Modify: `src/obsidian_intake_agent/processors/meeting_processor.py`
- Test: `tests/test_processor.py`

- [ ] **Step 1: Write the failing processor tests for validation note and action routing**

```python
def test_process_vtt_file_validation_mode_writes_meeting_note_under_test_lane(self) -> None:
    with TemporaryDirectory() as tmp_dir:
        vault = Path(tmp_dir)
        config = make_config(vault=vault, dry_run=False)
        processor = MeetingProcessor(config, output_mode="validation")
        source = vault / "00_Intake" / "2026-03-12 - Teams - weekly-sync.vtt"
        source.parent.mkdir(parents=True)
        source.write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nAction: Matthew to ship recap\n", encoding="utf-8")

        result = processor.process_file(source)

        self.assertTrue(result.processed)
        self.assertEqual(
            result.canonical_note_path,
            vault / "99_Test Notes" / "Meetings" / "2026-03-12 - Teams - weekly-sync.md",
        )
        self.assertTrue(result.canonical_note_path.exists())
        self.assertFalse((vault / "01_Meetings" / "2026-03-12 - Teams - weekly-sync.md").exists())


def test_process_markdown_validation_mode_routes_actions_to_test_lane(self) -> None:
    with TemporaryDirectory() as tmp_dir:
        vault = Path(tmp_dir)
        config = make_config(vault=vault, dry_run=False)
        processor = MeetingProcessor(config, output_mode="validation")
        source = vault / "00_Intake" / "2026-03-12 - Unknown - weekly-sync.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Notes\n- [ ] Matthew: Review action items\n", encoding="utf-8")

        result = processor.process_file(source)

        self.assertEqual(
            result.actions_file_path,
            vault / "99_Test Notes" / "Actions" / "2026-03-09.md",
        )
        self.assertTrue(result.actions_file_path.exists())
        self.assertFalse((vault / "07_Actions" / "2026-03-09.md").exists())
```

- [ ] **Step 2: Run the focused processor tests and verify they fail**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_processor.MeetingProcessorTests.test_process_vtt_file_validation_mode_writes_meeting_note_under_test_lane \
  tests.test_processor.MeetingProcessorTests.test_process_markdown_validation_mode_routes_actions_to_test_lane -v
```

Expected: `ERROR` because `MeetingProcessor` does not yet accept a validation output mode.

- [ ] **Step 3: Add explicit output-mode routing inside `MeetingProcessor`**

```python
from typing import Literal

OutputMode = Literal["normal", "validation"]


class MeetingProcessor:
    def __init__(self, config: Config, *, output_mode: OutputMode = "normal") -> None:
        self.config = config
        self.output_mode = output_mode
        self.vault_path = config.vault_path
        self.intake_path = self.vault_path / config.intake_dir
        self.meetings_path = self._resolve_meetings_path()
        self.actions_path = self._resolve_actions_path()
        self.archive_path = self.vault_path / config.archive_intake_dir
        self.action_owner_aliases = config.action_owner_aliases or {}
        self.intake_state = IntakeState(intake_path=self.intake_path, archive_path=self.archive_path)

    def _resolve_meetings_path(self) -> Path:
        if self.output_mode == "validation":
            return self.vault_path / self.config.validation_meetings_dir
        return self.vault_path / self.config.meetings_dir

    def _resolve_actions_path(self) -> Path:
        if self.output_mode == "validation":
            return self.vault_path / self.config.validation_actions_dir
        return self.vault_path / self.config.actions_dir
```

- [ ] **Step 4: Mark validation-generated outputs clearly in note content and returned paths**

```python
def _apply_validation_marker(self, rendered_note: str) -> str:
    if self.output_mode != "validation":
        return rendered_note
    return "---\nvalidation_mode: true\n---\n" + rendered_note
```

```python
meeting_note = self._apply_validation_marker(
    render_extracted_meeting_note(
        heading=f"{metadata.date} - {metadata.source} - {metadata.title}",
        intake_file=self._vault_relative_path(archived_source_path),
        extracted=extracted,
    )
)
```

```python
if self.output_mode == "validation" and action_update.changed:
    print(f"validation_actions_output: {action_update.path}")
```

- [ ] **Step 5: Re-run the focused processor tests and verify they pass**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_processor.MeetingProcessorTests.test_process_vtt_file_validation_mode_writes_meeting_note_under_test_lane \
  tests.test_processor.MeetingProcessorTests.test_process_markdown_validation_mode_routes_actions_to_test_lane -v
```

Expected: `OK`

- [ ] **Step 6: Commit the processor routing changes**

```bash
git add src/obsidian_intake_agent/processors/meeting_processor.py tests/test_processor.py
git commit -m "feat: route validation meeting outputs to test notes"
```

### Task 3: Thread Validation Mode Through Bundle Execution And CLI

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Modify: `src/obsidian_intake_agent/main.py`
- Test: `tests/test_meeting_process_bundles.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write the failing bundle-processing and CLI tests**

```python
def test_execute_bundle_processing_plan_validation_mode_routes_outputs_to_test_lane(self) -> None:
    with TemporaryDirectory() as tmp_dir:
        vault = Path(tmp_dir)
        bundle_root = vault / "00_Intake" / "bundles"
        bundle_root.mkdir(parents=True)
        preferred_input = bundle_root / "fallbacks" / "2026-05-04 - Teams - Platform Sync.md"
        preferred_input.parent.mkdir(parents=True)
        preferred_input.write_text("# Fallback summary", encoding="utf-8")
        _write_bundle_metadata(bundle_root, preferred_input=preferred_input)

        processor = MeetingProcessor(make_config(vault=vault, dry_run=False), output_mode="validation")
        plan = build_bundle_processing_plan(intake_root=bundle_root, processor=processor)

        result = execute_bundle_processing_plan(plan, processor=processor)

        processed = next(item for item in result.items if item.status == "processed")
        self.assertEqual(
            processed.canonical_note_path,
            vault / "99_Test Notes" / "Meetings" / "2026-05-04 - Teams - Platform Sync.md",
        )
```

```python
def test_meetings_process_bundles_execute_validation_mode_prints_test_lane_paths(self) -> None:
    with TemporaryDirectory() as tmp_dir:
        vault = Path(tmp_dir)
        config_path = _write_config(vault)
        bundle_root = vault / "00_Intake" / "bundles"
        bundle_root.mkdir(parents=True)
        preferred_input = bundle_root / "raw_transcripts" / "2026-05-04 - Teams - Platform Sync.vtt"
        preferred_input.parent.mkdir(parents=True)
        preferred_input.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nAction: Matthew to confirm follow-up\n", encoding="utf-8")
        _write_bundle_metadata(bundle_root, preferred_input=preferred_input)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "--config",
                    str(config_path),
                    "meetings",
                    "process-bundles",
                    "--execute",
                    "--validation",
                ]
            )

    self.assertEqual(exit_code, 0)
    self.assertIn("99_Test Notes/Meetings", stdout.getvalue())
```

- [ ] **Step 2: Run the focused bundle and CLI tests and verify they fail**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_process_bundles.BundleExecutionTests.test_execute_bundle_processing_plan_validation_mode_routes_outputs_to_test_lane \
  tests.test_main.MainTests.test_meetings_process_bundles_execute_validation_mode_prints_test_lane_paths -v
```

Expected: `FAIL` because neither the bundle executor nor CLI accepts validation mode yet.

- [ ] **Step 3: Add an explicit `--validation` switch to `meetings process-bundles` execution**

```python
process_bundles_parser.add_argument(
    "--validation",
    action="store_true",
    help="Write processed meeting notes and Matthew-owned actions into 99_Test Notes instead of production lanes.",
)
```

```python
if args.meetings_command == "process-bundles":
    processor = MeetingProcessor(
        config,
        output_mode="validation" if args.validation else "normal",
    )
```

- [ ] **Step 4: Surface validation routing in bundle execution reporting**

```python
if item.canonical_note_path is not None:
    lines.append(f"  canonical_output_file: {item.canonical_note_path}")
if item.actions_file_path is not None:
    lines.append(f"  actions_output_file: {item.actions_file_path}")
if processor.output_mode == "validation":
    lines.append("meeting_bundle_process_output_mode: validation")
```

- [ ] **Step 5: Re-run the focused bundle and CLI tests and verify they pass**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_process_bundles.BundleExecutionTests.test_execute_bundle_processing_plan_validation_mode_routes_outputs_to_test_lane \
  tests.test_main.MainTests.test_meetings_process_bundles_execute_validation_mode_prints_test_lane_paths -v
```

Expected: `OK`

- [ ] **Step 6: Commit the validation CLI and bundle-execution changes**

```bash
git add src/obsidian_intake_agent/meetings/process_bundles.py src/obsidian_intake_agent/main.py tests/test_meeting_process_bundles.py tests/test_main.py
git commit -m "feat: add validation mode for bundle execution"
```

### Task 4: Document The Live-Validation Workflow And Run Project Checks

**Files:**
- Modify: `README.md`
- Modify: `docs/meeting_transcript_automation.md`

- [ ] **Step 1: Update `README.md` with the new validation workflow**

````md
Validate recent meetings without touching production note lanes:

```bash
obsidian-agent meetings sync-transcripts --since 2026-05-01 --dry-run
obsidian-agent meetings process-bundles --execute --validation
```

Validation outputs land in:

- `99_Test Notes/Meetings`
- `99_Test Notes/Actions`

Normal runs still write to:

- `01_Meetings`
- `07_Actions`
````

- [ ] **Step 2: Update `docs/meeting_transcript_automation.md` with operator guidance**

````md
## Live Validation Lane

Use validation mode after inspecting a dry-run window when you want to process a
small representative sample without updating production meeting notes or weekly
actions.

```bash
obsidian-agent meetings process-bundles --execute --validation
```

This mode preserves transcript/fallback/filter semantics and only redirects
canonical outputs into `99_Test Notes/`.
````

- [ ] **Step 3: Run focused validation tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_config \
  tests.test_processor \
  tests.test_meeting_process_bundles \
  tests.test_main -v
```

Expected: `OK`

- [ ] **Step 4: Run project verification commands**

Run:

```bash
make check
make test
make smoke
make build
```

Expected: all commands pass. Do not run `make audit` because dependencies are unchanged in this slice.

- [ ] **Step 5: Commit docs after checks pass**

```bash
git add README.md docs/meeting_transcript_automation.md
git commit -m "docs: add live meeting validation workflow"
```

## Self-Review

- Spec coverage check:
  - live validation over real meetings is covered by Task 4 operator workflow docs and Task 3 validation execution wiring
  - permanent `99_Test Notes/` meeting/action outputs are covered by Tasks 1 and 2
  - unchanged discovery/filter/source-selection semantics are covered by Task 3 plus focused bundle/processor tests
- Placeholder scan:
  - no `TODO`, `TBD`, or unresolved placeholders remain
- Type consistency:
  - plan uses `validation_meetings_dir`, `validation_actions_dir`, and `output_mode="validation"` consistently across config, processor, CLI, and tests
