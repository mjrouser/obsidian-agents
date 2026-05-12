# Output Folder Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically archive older dated Markdown records from `07_Actions` and `09_Weekly Reviews` after the app writes new records, keeping the four newest filename-dated records in each root folder.

**Architecture:** Add a focused retention module that scans one output folder, sorts root-level dated Markdown files by leading filename date, and moves older files into the configured archive subfolder without overwriting collisions. Call that helper after successful non-dry-run action-note writes and weekly review writes.

**Tech Stack:** Python 3.11, `pathlib`, `dataclasses`, `unittest`, existing project CLI/config patterns.

---

## File Structure

- Create `src/obsidian_intake_agent/output_retention.py`
  - Owns filename-date parsing, retention candidate selection, collision-safe moves, and dry-run summaries.
- Create `tests/test_output_retention.py`
  - Unit tests for retention behavior in isolated temp folders.
- Modify `src/obsidian_intake_agent/config.py`
  - Adds `actions_archive_dir`, `weekly_reviews_archive_dir`, and `archive_retention_count` defaults and config loading.
- Modify `config.example.yaml`
  - Documents the new config keys.
- Modify `src/obsidian_intake_agent/processors/meeting_processor.py`
  - Runs action-folder retention after successful action-note writes in normal output mode.
- Modify `src/obsidian_intake_agent/weekly.py`
  - Runs weekly-review retention after successful weekly note writes.
- Modify `tests/test_config.py`
  - Verifies default and custom retention config loading.
- Modify `tests/test_processor.py`
  - Verifies action retention runs after a non-dry-run action note write and does not run on dry-run writes.
- Modify `tests/test_weekly.py`
  - Verifies weekly retention runs after a non-dry-run weekly note write and does not run on dry-run writes.
- Modify `README.md`
  - Documents the automatic retention behavior and configuration keys.

---

### Task 1: Add Retention Helper With Unit Tests

**Files:**
- Create: `src/obsidian_intake_agent/output_retention.py`
- Create: `tests/test_output_retention.py`

- [ ] **Step 1: Write failing tests for filename-date retention**

Create `tests/test_output_retention.py` with:

```python
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from obsidian_intake_agent.output_retention import apply_output_retention


class OutputRetentionTests(unittest.TestCase):
    def test_keeps_four_newest_by_filename_date_and_moves_older_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "07_Actions"
            root.mkdir()
            for name in [
                "2026-05-11.md",
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-20.md",
                "2026-04-13.md",
                "2026-04-06.md",
            ]:
                (root / name).write_text(name, encoding="utf-8")

            summary = apply_output_retention(root, archive_dir_name="Actions Archive", keep_count=4)

            self.assertEqual(
                [path.name for path in summary.moved],
                ["2026-04-13.md", "2026-04-06.md"],
            )
            self.assertTrue((root / "2026-05-11.md").exists())
            self.assertTrue((root / "2026-05-04.md").exists())
            self.assertTrue((root / "2026-04-27.md").exists())
            self.assertTrue((root / "2026-04-20.md").exists())
            self.assertFalse((root / "2026-04-13.md").exists())
            self.assertFalse((root / "2026-04-06.md").exists())
            self.assertTrue((root / "Actions Archive" / "2026-04-13.md").exists())
            self.assertTrue((root / "Actions Archive" / "2026-04-06.md").exists())

    def test_uses_filename_date_not_modified_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "09_Weekly Reviews"
            root.mkdir()
            names = [
                "2026-05-11 Weekly Briefing.md",
                "2026-05-04 Weekly Wrap.md",
                "2026-05-04 Weekly Briefing.md",
                "2026-04-27 Weekly Briefing.md",
                "2026-04-20 Weekly Wrap.md",
            ]
            for name in names:
                (root / name).write_text(name, encoding="utf-8")
            old_file = root / "2026-04-20 Weekly Wrap.md"
            os.utime(old_file, None)

            summary = apply_output_retention(root, archive_dir_name="Review & Wrap Archive", keep_count=4)

            self.assertEqual([path.name for path in summary.moved], ["2026-04-20 Weekly Wrap.md"])
            self.assertTrue((root / "Review & Wrap Archive" / "2026-04-20 Weekly Wrap.md").exists())

    def test_ignores_undated_non_markdown_and_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "07_Actions"
            archive = root / "Actions Archive"
            archive.mkdir(parents=True)
            for name in [
                "2026-05-11.md",
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-20.md",
                "2026-04-13.md",
            ]:
                (root / name).write_text(name, encoding="utf-8")
            (root / "README.md").write_text("not dated", encoding="utf-8")
            (root / "2026-04-01.txt").write_text("not markdown", encoding="utf-8")
            (archive / "2026-03-30.md").write_text("already archived", encoding="utf-8")

            summary = apply_output_retention(root, archive_dir_name="Actions Archive", keep_count=4)

            self.assertEqual([path.name for path in summary.moved], ["2026-04-13.md"])
            self.assertTrue((root / "README.md").exists())
            self.assertTrue((root / "2026-04-01.txt").exists())
            self.assertTrue((archive / "2026-03-30.md").exists())

    def test_collision_leaves_source_file_in_root_and_reports_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "07_Actions"
            archive = root / "Actions Archive"
            archive.mkdir(parents=True)
            for name in [
                "2026-05-11.md",
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-20.md",
                "2026-04-13.md",
            ]:
                (root / name).write_text(f"root {name}", encoding="utf-8")
            (archive / "2026-04-13.md").write_text("archived copy", encoding="utf-8")

            summary = apply_output_retention(root, archive_dir_name="Actions Archive", keep_count=4)

            self.assertEqual(summary.moved, [])
            self.assertEqual([path.name for path in summary.skipped_existing], ["2026-04-13.md"])
            self.assertTrue((root / "2026-04-13.md").exists())
            self.assertEqual((archive / "2026-04-13.md").read_text(encoding="utf-8"), "archived copy")

    def test_dry_run_reports_candidates_without_moving_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "07_Actions"
            root.mkdir()
            for name in [
                "2026-05-11.md",
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-20.md",
                "2026-04-13.md",
            ]:
                (root / name).write_text(name, encoding="utf-8")

            summary = apply_output_retention(root, archive_dir_name="Actions Archive", keep_count=4, dry_run=True)

            self.assertEqual([path.name for path in summary.would_move], ["2026-04-13.md"])
            self.assertEqual(summary.moved, [])
            self.assertTrue((root / "2026-04-13.md").exists())
            self.assertFalse((root / "Actions Archive").exists())
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_output_retention.py -q
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'obsidian_intake_agent.output_retention'`.

- [ ] **Step 3: Implement the retention helper**

Create `src/obsidian_intake_agent/output_retention.py` with:

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

LEADING_DATE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\b")


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    path: Path
    record_date: date


@dataclass(slots=True)
class OutputRetentionSummary:
    moved: list[Path] = field(default_factory=list)
    would_move: list[Path] = field(default_factory=list)
    skipped_existing: list[Path] = field(default_factory=list)


def apply_output_retention(
    root_dir: Path,
    *,
    archive_dir_name: str,
    keep_count: int,
    dry_run: bool = False,
) -> OutputRetentionSummary:
    summary = OutputRetentionSummary()
    if keep_count < 0:
        raise ValueError("keep_count must be zero or greater.")
    if not root_dir.exists() or not root_dir.is_dir():
        return summary

    candidates = _dated_markdown_candidates(root_dir)
    candidates.sort(key=lambda candidate: (candidate.record_date, candidate.path.name), reverse=True)
    archive_candidates = candidates[keep_count:]
    if not archive_candidates:
        return summary

    archive_dir = root_dir / archive_dir_name
    for candidate in archive_candidates:
        destination = archive_dir / candidate.path.name
        if destination.exists():
            summary.skipped_existing.append(candidate.path)
            continue
        if dry_run:
            summary.would_move.append(candidate.path)
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        candidate.path.rename(destination)
        summary.moved.append(candidate.path)
    return summary


def _dated_markdown_candidates(root_dir: Path) -> list[RetentionCandidate]:
    candidates: list[RetentionCandidate] = []
    for path in root_dir.iterdir():
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        record_date = _leading_date(path.name)
        if record_date is None:
            continue
        candidates.append(RetentionCandidate(path=path, record_date=record_date))
    return candidates


def _leading_date(filename: str) -> date | None:
    match = LEADING_DATE.match(filename)
    if match is None:
        return None
    return date.fromisoformat(match.group("date"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_output_retention.py -q
```

Expected: PASS with `5 passed`.

---

### Task 2: Add Retention Configuration

**Files:**
- Modify: `src/obsidian_intake_agent/config.py`
- Modify: `config.example.yaml`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Append these tests to `ConfigTests` in `tests/test_config.py`:

```python
    def test_output_retention_defaults_load(self) -> None:
        config_path = _write_config(self, "")
        loaded = Config.load(config_path)

        self.assertEqual(loaded.actions_archive_dir, "Actions Archive")
        self.assertEqual(loaded.weekly_reviews_archive_dir, "Review & Wrap Archive")
        self.assertEqual(loaded.archive_retention_count, 4)

    def test_output_retention_custom_values_load(self) -> None:
        config_path = _write_config(
            self,
            "\n".join(
                [
                    'actions_archive_dir: "Old Actions"',
                    'weekly_reviews_archive_dir: "Old Reviews"',
                    "archive_retention_count: 6",
                ]
            ),
        )
        loaded = Config.load(config_path)

        self.assertEqual(loaded.actions_archive_dir, "Old Actions")
        self.assertEqual(loaded.weekly_reviews_archive_dir, "Old Reviews")
        self.assertEqual(loaded.archive_retention_count, 6)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::ConfigTests::test_output_retention_defaults_load tests/test_config.py::ConfigTests::test_output_retention_custom_values_load -q
```

Expected: FAIL with `AttributeError` for `actions_archive_dir`.

- [ ] **Step 3: Implement config fields and loading**

In `src/obsidian_intake_agent/config.py`, add fields to `Config`:

```python
    actions_archive_dir: str = "Actions Archive"
    weekly_reviews_archive_dir: str = "Review & Wrap Archive"
    archive_retention_count: int = 4
```

In `Config.load`, add keyword arguments after `weekly_reviews_dir`:

```python
            actions_archive_dir=str(data.get("actions_archive_dir", "Actions Archive")),
            weekly_reviews_archive_dir=str(data.get("weekly_reviews_archive_dir", "Review & Wrap Archive")),
            archive_retention_count=_positive_int(data.get("archive_retention_count", 4), "archive_retention_count"),
```

In `config.example.yaml`, add after `weekly_reviews_dir`:

```yaml
actions_archive_dir: "Actions Archive"
weekly_reviews_archive_dir: "Review & Wrap Archive"
archive_retention_count: 4
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::ConfigTests::test_output_retention_defaults_load tests/test_config.py::ConfigTests::test_output_retention_custom_values_load -q
```

Expected: PASS with `2 passed`.

---

### Task 3: Trigger Retention After Action Note Writes

**Files:**
- Modify: `src/obsidian_intake_agent/processors/meeting_processor.py`
- Modify: `tests/test_processor.py`

- [ ] **Step 1: Write failing processor tests**

Append these tests to `MeetingProcessorTests` in `tests/test_processor.py`:

```python
    def test_archives_older_action_notes_after_action_note_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake = vault / "00_Intake"
            actions = vault / "07_Actions"
            intake.mkdir(parents=True)
            actions.mkdir(parents=True)
            for name in [
                "2026-05-11.md",
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-20.md",
                "2026-04-13.md",
            ]:
                (actions / name).write_text(f"# Actions {name}\n", encoding="utf-8")
            source = intake / "2026-05-12 - Teams - Sync.md"
            source.write_text("- [ ] Follow up with Alex (Owner: Matthew)\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=False))
            result = processor.process_file(source)

            self.assertTrue(result.processed)
            self.assertFalse((actions / "2026-04-13.md").exists())
            self.assertTrue((actions / "Actions Archive" / "2026-04-13.md").exists())

    def test_dry_run_does_not_archive_older_action_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake = vault / "00_Intake"
            actions = vault / "07_Actions"
            intake.mkdir(parents=True)
            actions.mkdir(parents=True)
            for name in [
                "2026-05-11.md",
                "2026-05-04.md",
                "2026-04-27.md",
                "2026-04-20.md",
                "2026-04-13.md",
            ]:
                (actions / name).write_text(f"# Actions {name}\n", encoding="utf-8")
            source = intake / "2026-05-12 - Teams - Sync.md"
            source.write_text("- [ ] Follow up with Alex (Owner: Matthew)\n", encoding="utf-8")

            processor = MeetingProcessor(_config(vault, dry_run=True))
            result = processor.process_file(source)

            self.assertTrue(result.processed)
            self.assertTrue((actions / "2026-04-13.md").exists())
            self.assertFalse((actions / "Actions Archive").exists())
```

- [ ] **Step 2: Run tests to verify at least the non-dry-run test fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_processor.py::MeetingProcessorTests::test_archives_older_action_notes_after_action_note_write tests/test_processor.py::MeetingProcessorTests::test_dry_run_does_not_archive_older_action_notes -q
```

Expected: FAIL for `test_archives_older_action_notes_after_action_note_write` because `2026-04-13.md` still exists in the root folder.

- [ ] **Step 3: Implement action retention hook**

In `src/obsidian_intake_agent/processors/meeting_processor.py`, add import:

```python
from ..output_retention import apply_output_retention
```

Add a method to `MeetingProcessor`:

```python
    def _apply_actions_retention(self) -> None:
        if self.output_mode != "normal":
            return
        apply_output_retention(
            self.actions_path,
            archive_dir_name=self.config.actions_archive_dir,
            keep_count=self.config.archive_retention_count,
        )
```

After each non-dry-run `safe_write_text(action_update.path, action_update.content)` call, add:

```python
            self._apply_actions_retention()
```

This needs to be added in both `process_file` and `_process_vtt_file` because both can write action notes.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_processor.py::MeetingProcessorTests::test_archives_older_action_notes_after_action_note_write tests/test_processor.py::MeetingProcessorTests::test_dry_run_does_not_archive_older_action_notes -q
```

Expected: PASS with `2 passed`.

---

### Task 4: Trigger Retention After Weekly Review Writes

**Files:**
- Modify: `src/obsidian_intake_agent/weekly.py`
- Modify: `tests/test_weekly.py`

- [ ] **Step 1: Write failing weekly tests**

Append these tests to `WeeklySnapshotTests` in `tests/test_weekly.py`:

```python
    def test_archives_older_weekly_reviews_after_weekly_note_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            review_dir = vault / "09_Weekly Reviews"
            review_dir.mkdir(parents=True)
            for name in [
                "2026-05-04 Weekly Wrap.md",
                "2026-05-04 Weekly Briefing.md",
                "2026-04-27 Weekly Briefing.md",
                "2026-04-20 Weekly Wrap.md",
            ]:
                (review_dir / name).write_text(f"# {name}\n", encoding="utf-8")
            config = _config(vault)

            with patch("obsidian_intake_agent.weekly.run_codex_markdown", return_value="# Weekly Briefing"):
                result = generate_weekly_snapshot(
                    config,
                    mode="briefing",
                    target_date=date(2026, 5, 11),
                    dry_run=False,
                )

            self.assertTrue(result.changed)
            self.assertFalse((review_dir / "2026-04-20 Weekly Wrap.md").exists())
            self.assertTrue(
                (review_dir / "Review & Wrap Archive" / "2026-04-20 Weekly Wrap.md").exists()
            )

    def test_dry_run_does_not_archive_older_weekly_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            review_dir = vault / "09_Weekly Reviews"
            review_dir.mkdir(parents=True)
            for name in [
                "2026-05-04 Weekly Wrap.md",
                "2026-05-04 Weekly Briefing.md",
                "2026-04-27 Weekly Briefing.md",
                "2026-04-20 Weekly Wrap.md",
            ]:
                (review_dir / name).write_text(f"# {name}\n", encoding="utf-8")
            config = _config(vault)

            with patch("obsidian_intake_agent.weekly.run_codex_markdown", return_value="# Weekly Briefing"):
                result = generate_weekly_snapshot(
                    config,
                    mode="briefing",
                    target_date=date(2026, 5, 11),
                    dry_run=True,
                )

            self.assertTrue(result.changed)
            self.assertTrue((review_dir / "2026-04-20 Weekly Wrap.md").exists())
            self.assertFalse((review_dir / "Review & Wrap Archive").exists())
```

- [ ] **Step 2: Run tests to verify at least the non-dry-run test fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_weekly.py::WeeklySnapshotTests::test_archives_older_weekly_reviews_after_weekly_note_write tests/test_weekly.py::WeeklySnapshotTests::test_dry_run_does_not_archive_older_weekly_reviews -q
```

Expected: FAIL for `test_archives_older_weekly_reviews_after_weekly_note_write` because `2026-04-20 Weekly Wrap.md` still exists in the root folder.

- [ ] **Step 3: Implement weekly retention hook**

In `src/obsidian_intake_agent/weekly.py`, add import:

```python
from .output_retention import apply_output_retention
```

After `safe_write_text(review_path, updated)`, add:

```python
        apply_output_retention(
            review_dir,
            archive_dir_name=config.weekly_reviews_archive_dir,
            keep_count=config.archive_retention_count,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_weekly.py::WeeklySnapshotTests::test_archives_older_weekly_reviews_after_weekly_note_write tests/test_weekly.py::WeeklySnapshotTests::test_dry_run_does_not_archive_older_weekly_reviews -q
```

Expected: PASS with `2 passed`.

---

### Task 5: Document Behavior and Run Focused Checks

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README configuration sample**

In the README config sample near `weekly_reviews_dir`, add:

```yaml
actions_archive_dir: "Actions Archive"
weekly_reviews_archive_dir: "Review & Wrap Archive"
archive_retention_count: 4
```

- [ ] **Step 2: Add operational note**

In the README section that describes generated outputs, add:

```markdown
After successful non-dry-run writes, the agent keeps the four newest dated
Markdown records in `07_Actions` and `09_Weekly Reviews` and moves older dated
records into their configured archive subfolders. Recency is based on the
leading `YYYY-MM-DD` filename date, not file modified time. Files without a
leading date and files already inside archive folders are ignored.
```

- [ ] **Step 3: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_output_retention.py tests/test_config.py tests/test_processor.py tests/test_weekly.py -q
```

Expected: PASS.

- [ ] **Step 4: Run project priority checks**

Run:

```bash
make check
make test
make smoke
make build
```

Expected: each command exits 0. Do not run `make audit` unless dependency or packaging files changed.

- [ ] **Step 5: Review git diff**

Run:

```bash
git status --short
git diff -- src/obsidian_intake_agent/output_retention.py src/obsidian_intake_agent/config.py src/obsidian_intake_agent/processors/meeting_processor.py src/obsidian_intake_agent/weekly.py tests/test_output_retention.py tests/test_config.py tests/test_processor.py tests/test_weekly.py config.example.yaml README.md docs/superpowers/specs/2026-05-11-output-folder-retention-design.md docs/superpowers/plans/2026-05-11-output-folder-retention.md
```

Expected: only intended project files changed; no `config.yaml`, credentials, generated logs, or unrelated files.

---

## Implementation Notes

- Preserve project dry-run behavior. The production hooks should not call retention in dry-run branches.
- Use `Path.rename()` for same-vault moves. These are local filesystem moves within one folder tree.
- The helper returns source paths in `moved` and `would_move` summaries because tests and logs care about which root files were selected.
- Retention should run only when a relevant output file changed. If a rerun produces no action update or no weekly review change, no archive cleanup is needed.
- Validation output mode should not archive production action folders.
