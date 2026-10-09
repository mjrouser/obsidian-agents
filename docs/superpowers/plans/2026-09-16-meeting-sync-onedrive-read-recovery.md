# Meeting Sync OneDrive Read Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make transient OneDrive `Errno 11` read locks skip and retry the
affected meeting bundle instead of failing the entire scheduled meeting sync.

**Architecture:** Add a narrowly scoped retryable vault-read helper. Use it for
bundle metadata and the intake processed-state read paths, then catch its
exhaustion once at the bundle-plan boundary. Other errors retain their existing
behavior and no write path is retried.

**Tech Stack:** Python 3.11+, `errno`, `time`, `pathlib`, `unittest`, existing
meeting bundle planner and launchd wrapper.

**Approved design:**
`docs/superpowers/specs/2026-09-16-meeting-sync-onedrive-read-recovery-design.md`

---

## Pre-Flight

- [ ] Run `git status --short --branch` and confirm the implementation starts
  from a clean worktree.
- [ ] Re-read the approved design above.
- [ ] Do not run `meetings process-bundles --execute` as validation; the live
  verification lane is dry-run only.

## Task 1: Add Narrow Retryable Read Coverage

**Files:**

- Add: `tests/test_vault_reads.py`

- [ ] Add a test that patches a text read to raise `OSError(errno.EDEADLK,
  "Resource deadlock avoided")` twice and then return content. Assert that the
  helper returns the content and uses exactly three attempts.
- [ ] Add a test that exhausts the retry budget and asserts a
  `TransientVaultReadError` includes the original path and chains the original
  `OSError`.
- [ ] Add a test that `PermissionError` and an unrelated `OSError` propagate
  immediately with one attempt.
- [ ] Patch the helper's sleep function in all tests so they are deterministic
  and do not slow the suite.

## Task 2: Implement the Vault Read Helper

**Files:**

- Add: `src/obsidian_intake_agent/utils/vault_reads.py`

- [ ] Define a small explicit constant for three total attempts and fixed,
  short increasing delays.
- [ ] Define `TransientVaultReadError` with path context.
- [ ] Implement `read_text_with_retry(path, *, encoding)` using `Path.read_text`.
  Retry only errno values matching `errno.EDEADLK` or `errno.EAGAIN`; use a
  de-duplicated set because those values can be equal on macOS.
- [ ] Do not catch `BaseException`, do not retry writes, and preserve exception
  chaining on exhaustion.
- [ ] Run `python -m unittest tests.test_vault_reads`.

## Task 3: Keep Unavailable Bundles Isolated During Planning

**Files:**

- Modify: `src/obsidian_intake_agent/meetings/process_bundles.py`
- Modify: `tests/test_meeting_process_bundles.py`

- [ ] Replace the direct metadata `read_text()` call with the helper.
- [ ] Extend the per-metadata-file planning `try` block to catch
  `TransientVaultReadError` from both metadata loading and
  `_plan_bundle_processing_item()`.
- [ ] Append a warning that identifies the unavailable path and says the next
  scheduled run will retry it. Do not create an item for that bundle.
- [ ] Add a two-bundle regression: make the first metadata read exhaust the
  retry policy, keep the second metadata readable, then assert one warning and
  exactly one normal item for the second bundle.
- [ ] Add a regression where a readable metadata file's preferred processor
  input is transiently unreadable during `skip_reason`; assert the planner
  warns and continues to a later bundle.
- [ ] Run the focused bundle test module.

## Task 4: Make Intake Processed-State Reads Retryable Without Reclassification

**Files:**

- Modify: `src/obsidian_intake_agent/processors/intake_state.py`
- Modify: `tests/test_processor_intake.py`

- [ ] Replace the direct `path.open()` loop in `is_processed()` with the shared
  retryable text read and inspect only the first three lines of the returned
  content.
- [ ] Let `TransientVaultReadError` propagate. Do not return `False`, because
  that could process a source whose current status could not be read.
- [ ] Add a focused test proving a normal three-line status-marker check still
  works and an exhausted transient error is not converted to `already
  processed` or a false-ready result.
- [ ] Run the intake-state and bundle-plan test modules together.

## Task 5: Full Validation and Live Read-Only Verification

- [ ] Run `make check`.
- [ ] Run `make test`. If the sandbox blocks the web-clip loopback tests, rerun
  with normal local permissions and record that distinction.
- [ ] Run `make smoke`, `make build`, and `make audit`.
- [ ] Run the live read-only bundle plan:

```bash
./.venv/bin/obsidian-agent meetings process-bundles --dry-run
```

- [ ] Confirm there is no traceback. If OneDrive reproduces the lock, confirm
  only the named bundle is deferred and the command still prints plans for
  readable bundles.
- [ ] Review `git diff --check` and `git diff` before handing the change back.

## Commit and Merge Handoff

After review and successful validation:

```bash
git add src/obsidian_intake_agent/utils/vault_reads.py \
  src/obsidian_intake_agent/meetings/process_bundles.py \
  src/obsidian_intake_agent/processors/intake_state.py \
  tests/test_vault_reads.py tests/test_meeting_process_bundles.py \
  tests/test_processor_intake.py \
  docs/superpowers/specs/2026-09-16-meeting-sync-onedrive-read-recovery-design.md \
  docs/superpowers/plans/2026-09-16-meeting-sync-onedrive-read-recovery.md
git commit -m "Recover from transient OneDrive meeting reads"
git push -u origin HEAD
```
