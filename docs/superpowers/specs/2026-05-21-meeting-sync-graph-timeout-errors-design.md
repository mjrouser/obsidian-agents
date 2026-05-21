# Meeting Sync Graph Timeout Error Handling Design

## Summary

Meeting sync currently has a 30-second Microsoft Graph request timeout, which is
good for preventing launchd polling from hanging indefinitely. When Graph times
out during calendar discovery, though, the automation log gets a full Python
traceback. This design keeps the nonzero failure behavior but replaces the
traceback with a concise, stable operator-facing message.

## Goals

- Preserve the existing 30-second Graph request timeout.
- Keep Graph timeout failures nonzero so `run_automation_command.sh` still
  writes an automation failure note and notification.
- Make meeting-sync timeout logs concise, grep-friendly, and understandable.
- Ensure the next scheduled launchd tick retries normally.
- Avoid changing bundle processing, calendar-only staging, or permissions.

## Non-Goals

- Do not add retry or backoff behavior yet.
- Do not change the launchd schedule.
- Do not change Entra / Microsoft Graph permissions.
- Do not suppress genuine coding errors behind generic user messages.
- Do not process calendar-only bundles into production notes.

## Recommended Approach

Catch Microsoft Graph timeout failures at the `meetings sync-transcripts` CLI
boundary.

Low-level Graph helpers should still raise meaningful exceptions. The CLI should
recognize known network timeout failures, print a short structured message, and
return exit code `1`. This keeps the automation failure path intact while
removing traceback noise from expected operational failures.

Example output:

```text
meeting_sync_error: graph_timeout
meeting_sync_error_detail: Microsoft Graph did not respond within 30 seconds while listing recently ended meetings.
meeting_sync_next_step: The next scheduled run will retry automatically. If this repeats, run `obsidian-agent graph status` and consider increasing the Graph timeout.
```

The message should be written to `stderr` because the command failed. The stdout
log can still contain any normal summary lines produced before the timeout.

## Error Classification

Handle only timeout-shaped Graph network failures:

- `TimeoutError`
- `socket.timeout`
- `urllib.error.URLError` where the reason is timeout-shaped

The classification should avoid broad `Exception` handling. HTTP Graph failures
such as `403`, `404`, and `429` already have domain-specific handling in the
artifact discovery paths and should not be converted into this timeout message
unless they are explicitly caused by a socket/read timeout.

## Proposed Code Shape

Add a small domain exception in `src/obsidian_intake_agent/meetings/sync.py`:

```python
class MeetingSyncGraphTimeoutError(RuntimeError):
    def __init__(self, *, operation: str, timeout_seconds: int) -> None:
        ...
```

Wrap timeout-shaped failures where Graph JSON/byte requests are made or where
the Graph meeting client enters calendar discovery. Include the operation name
so the CLI can say whether the timeout happened while listing calendar meetings,
discovering transcripts, downloading transcript content, or looking up recap
metadata.

In `src/obsidian_intake_agent/main.py`, catch
`MeetingSyncGraphTimeoutError` only around the `meetings sync-transcripts`
command path. Print the stable lines and return `1`.

## Data Flow

1. launchd runs `scripts/run_meeting_sync.sh`.
2. The wrapper calls `obsidian-agent meetings sync-transcripts --download-transcripts`.
3. Microsoft Graph read/connect stalls past the configured timeout.
4. The Graph call raises a timeout-shaped exception.
5. Meeting sync wraps it as `MeetingSyncGraphTimeoutError`.
6. The CLI prints concise `meeting_sync_error` lines and exits `1`.
7. `run_automation_command.sh` writes the existing failure note and notification.
8. The next scheduled launchd tick retries normally.

## Testing

Add focused tests for:

- `_fetch_graph_json` or the Graph meeting client wrapping `TimeoutError` in
  `MeetingSyncGraphTimeoutError`.
- Timeout-shaped `URLError` being classified as a Graph timeout.
- Non-timeout `URLError` not being misclassified as a Graph timeout.
- `obsidian-agent meetings sync-transcripts` returning nonzero and printing
  concise timeout lines without a traceback.
- Existing successful sync command behavior remaining unchanged.

The CLI test should assert:

- Output includes `meeting_sync_error: graph_timeout`.
- Output includes the configured timeout value.
- Output does not include `Traceback`.
- Exit code is `1`.

## Operational Notes

If this happens occasionally, no manual action is required; the next scheduled
run retries. If it repeats frequently, the next evidence-based follow-up is to
evaluate whether the timeout should increase to 60 seconds or whether calendar
query paging should be reduced. That tuning is intentionally outside this first
cleanup.

## Acceptance Criteria

- A Graph timeout during `meetings sync-transcripts` no longer emits a Python
  traceback in automation logs.
- The command still exits nonzero so failure notes continue to work.
- The output gives a clear next step for repeated failures.
- Existing permission-blocked, missing transcript, and calendar-only behavior is
  unchanged.
