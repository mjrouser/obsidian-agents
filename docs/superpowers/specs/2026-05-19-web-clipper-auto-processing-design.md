# Web Clipper Auto-Processing Design

## Goal

Make the bookmarklet workflow complete in one action: saving a web clip should also create its processed reference note in `10_References/Web Clips` and archive the raw capture under `_Archive/Intake/Web Clips`.

## Current Behavior

The web clipper capture server writes a raw markdown note into `00_Intake/Web Clips`. The user then runs `obsidian-agent web-clips process` to convert raw clips into reference notes and archive the raw captures. This preserves source material, but it leaves a manual step between capture and future surfacing.

## Proposed Behavior

After the capture server validates and writes a raw clip, it immediately processes that single raw file. A successful response should tell the bookmarklet that the clip was both saved and processed. The raw clip should still be archived, preserving the existing source trail.

The processing should be single-file, not batch-wide. Capturing one new page should not process older raw clips that may have been left around for debugging, manual cleanup, or malformed-input investigation.

## Architecture

Add `WebClipProcessor.process_file(raw_path, dry_run=None)` as the single-file processing primitive. `WebClipProcessor.process_all()` should reuse this method for each eligible raw clip so batch processing and capture-time processing share the same safety checks, rendering, collision handling, partial recovery behavior, and summary counting.

Update the capture server to call `process_file()` after `capture_payload_to_note()` returns the newly written raw path. The server response should include the raw capture path, the processed reference path when one is written, and the archive path when the raw capture is moved.

Update the bookmarklet status text to distinguish:

- `Saved and processed.` when both steps succeed.
- `Saved, but processing failed: ...` when the raw capture was written but processing raised an error.
- `Failed: ...` when validation or capture itself fails.

## Failure Handling

Capture must remain durable. If raw capture writing succeeds and processing fails, the raw note stays in `00_Intake/Web Clips` so it can be retried by `obsidian-agent web-clips process`.

The capture server should return a clear error body for partial failures. It should not delete, archive, or overwrite the raw clip after a processing exception.

The processor should continue to skip malformed frontmatter, symlinks, and paths outside the vault. Single-file processing should validate that the requested raw path is under the configured web clip intake directory and inside the vault before reading it.

## Auto-Commit Behavior

When capture-time processing writes a reference note and archives the raw capture, it should mirror the manual `web-clips process` command: if `git_auto_commit_vault` is enabled, auto-commit the vault changes with source label `web clips`. Project repository auto-commit remains disabled for manual review.

If capture succeeds but processing fails, do not auto-commit. The raw clip remains unprocessed and should be handled by a later manual or automated process run.

## Testing

Add focused tests for:

- `WebClipProcessor.process_file()` writes one reference note and archives exactly the requested raw clip.
- `WebClipProcessor.process_all()` still processes all eligible raw clips by delegating through the same single-file behavior.
- The capture server can be given a processor factory and includes processed/archive paths in a successful response.
- A processing failure after capture leaves the raw note in intake and returns a clear partial-failure response.
- The bookmarklet renders status text for successful processing and partial processing failures.

Existing web clip path validation, token validation, payload validation, rendering, and retrieval tests should continue to pass unchanged.

## Out Of Scope

- Browser extension work.
- Scheduled batch processing.
- Watcher recursion into `00_Intake/Web Clips`.
- AI summarization beyond the current deterministic processed note generation.
- Changing the raw or processed note folder layout.
