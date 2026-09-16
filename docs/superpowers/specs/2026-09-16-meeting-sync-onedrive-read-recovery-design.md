# Meeting Sync OneDrive Read Recovery Design

## Summary

Prevent one transient OneDrive file-provider lock from terminating the whole
`meeting-sync` automation run. The planner will retry only the documented
macOS `OSError: [Errno 11] Resource deadlock avoided` class of read failure a
small, bounded number of times. If the file remains unavailable, it will skip
that one bundle, emit a specific warning, and leave its metadata, input, and
identity marker unchanged for the next scheduled run.

## Evidence

The automation failure notes show four failures after the vault moved to the
OneDrive-backed physical path:

- 2026-09-15 12:05, 12:36, and 13:05 EDT
- 2026-09-16 11:35 EDT

Each terminates in a normal read operation with `OSError: [Errno 11] Resource
deadlock avoided`. The latest run first failed in
`_load_bundle_metadata_record()` while reading an `* (outlook).json` file, and
later in `IntakeState.is_processed()` while reading a processor input. A
read-only dry run at 11:40 EDT successfully scanned all 174 bundle metadata
files, and launchd then reported last exit code `0`. Microsoft Graph status was
healthy, so this is an intermittent filesystem-provider condition rather than
an auth or scheduler outage.

## Root Cause

`build_bundle_processing_plan()` catches only `ValueError` around metadata
loading. `_load_bundle_metadata_record()` converts malformed JSON to that
exception but lets `OSError` escape. Separately, the same plan calls
`MeetingProcessor.skip_reason()`, which calls `IntakeState.is_processed()` and
also lets a failed read escape. Either path aborts the entire automation run,
even though other bundles may be readable.

## Goals

- Retry only temporary vault read locks, with a bounded delay and no busy loop.
- Keep a single unavailable bundle from blocking other bundles.
- Preserve safety: do not treat an unreadable source as processed, safe, or
  eligible for processing.
- Preserve existing handling for malformed JSON, permission errors, missing
  files, symlinks, and all non-transient I/O failures.
- Make the final warning actionable and include the affected path.

## Non-Goals

- Changing OneDrive settings, moving the vault again, or disabling launchd.
- Retrying Graph, LLM, writes, archive moves, or identity-marker transactions.
- Suppressing a persistent filesystem failure silently.
- Automatically processing calendar-only bundles.

## Approved Behavior

1. A helper recognizes only `OSError` values whose errno is the platform's
   `EDEADLK` or `EAGAIN` value. It retries reads three times total with short
   increasing delays.
2. Metadata and processed-state reads use the helper. Success after a retry is
   indistinguishable from a normal successful read.
3. After the final retry, the helper raises a narrow `TransientVaultReadError`
   that keeps the original path and exception as context.
4. `build_bundle_processing_plan()` catches that exception around both metadata
   loading and item planning, adds `Skipped temporarily unavailable bundle
   data <path>: ...; it will be retried on the next scheduled run.`, and
   continues scanning.
5. The skipped bundle produces no `BundleProcessingPlanItem`, so execution
   cannot write, archive, remove staging paths, or change its identity state.
6. The automation exits successfully if every other discovery and processing
   step succeeds. The warning remains visible in normal stdout for operators.

## File Map

- Add `src/obsidian_intake_agent/utils/vault_reads.py`
  - Narrow errno classification, bounded retry policy, and text-read helper.
- Modify `src/obsidian_intake_agent/meetings/process_bundles.py`
  - Use the helper for bundle JSON and isolate a temporarily unavailable bundle
    during plan construction.
- Modify `src/obsidian_intake_agent/processors/intake_state.py`
  - Use the helper for processed-marker and intake-input reads.
- Modify `tests/test_meeting_process_bundles.py`
  - Prove an unavailable metadata file or processor input skips only its bundle
    while a later ready bundle remains planned.
- Add `tests/test_vault_reads.py`
  - Prove retry classification, success after a transient failure, exhaustion,
    and no retry for unrelated I/O errors.
- Modify `tests/test_processor_intake.py`
  - Prove processed-state read exhaustion reaches the bundle-plan recovery path
    rather than classifying the source as processed.

## Validation

Run focused vault-read and bundle-plan tests, then `make check`, `make test`,
`make smoke`, `make build`, and `make audit`. Finally run the live, read-only
command:

```bash
./.venv/bin/obsidian-agent meetings process-bundles --dry-run
```

Expected: no traceback, each temporarily unavailable file is named in a
warning, and other readable bundles still receive normal plan decisions.
