# Meeting Sync Graph Timeout Error Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace Microsoft Graph timeout tracebacks from `meetings sync-transcripts` with concise `meeting_sync_error: graph_timeout` output while keeping nonzero automation failure behavior.

**Architecture:** Add a small timeout classifier and domain exception in `meetings/sync.py`, wrap timeout-shaped Graph request failures there, and catch only that domain exception in the `meetings sync-transcripts` CLI path. The existing launchd wrapper and automation failure-note path remain unchanged.

**Tech Stack:** Python 3.11-compatible code, `urllib`, `unittest`, existing CLI dispatcher in `src/obsidian_intake_agent/main.py`.

---

## File Structure

- Modify: `src/obsidian_intake_agent/meetings/sync.py`
  - Responsibility: classify timeout-shaped Graph network failures and expose `MeetingSyncGraphTimeoutError`.
- Modify: `src/obsidian_intake_agent/main.py`
  - Responsibility: catch `MeetingSyncGraphTimeoutError` only for `meetings sync-transcripts` and print concise failure lines.
- Modify: `tests/test_meeting_sync.py`
  - Responsibility: verify timeout classification and wrapping at the Graph helper boundary.
- Modify: `tests/test_main.py`
  - Responsibility: verify the CLI returns nonzero and prints no traceback for meeting-sync Graph timeouts.
- Existing spec: `docs/superpowers/specs/2026-05-21-meeting-sync-graph-timeout-errors-design.md`
  - Responsibility: approved behavior reference. Do not change unless implementation reveals a contradiction.

## Pre-Flight

- [x] **Step 1: Confirm worktree state**

Run:

```bash
git status --short
```

Expected: only the approved spec/plan files, or other known user work. Do not stage, commit, revert, or overwrite unrelated changes.

- [x] **Step 2: Re-read the approved spec**

Run:

```bash
sed -n '1,220p' docs/superpowers/specs/2026-05-21-meeting-sync-graph-timeout-errors-design.md
```

Expected: confirms CLI-boundary handling, nonzero exit, no retries, and no traceback.

## Task 1: Add Timeout Classification And Wrapping Tests

**Files:**
- Modify: `tests/test_meeting_sync.py`

- [x] **Step 1: Add imports for timeout test coverage**

In `tests/test_meeting_sync.py`, update the imports near the top:

```python
from urllib.error import HTTPError, URLError
```

- [x] **Step 2: Add failing tests near existing Graph fetch tests**

Add these tests inside `TranscriptSyncPlannerTests`, near the existing `_fetch_graph_json` tests:

```python
    def test_fetch_graph_json_wraps_timeout_error(self) -> None:
        def _fake_urlopen(request, *, timeout=None):
            del request, timeout
            raise TimeoutError("The read operation timed out")

        with (
            patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen),
            self.assertRaises(meeting_sync_module.MeetingSyncGraphTimeoutError) as exc,
        ):
            meeting_sync_module._fetch_graph_json(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                "token",
            )

        self.assertEqual(exc.exception.operation, "Microsoft Graph JSON request")
        self.assertEqual(exc.exception.timeout_seconds, meeting_sync_module.GRAPH_REQUEST_TIMEOUT_SECONDS)
        self.assertIn("Microsoft Graph did not respond within 30 seconds", str(exc.exception))

    def test_fetch_graph_json_wraps_urlerror_timeout_reason(self) -> None:
        def _fake_urlopen(request, *, timeout=None):
            del request, timeout
            raise URLError(TimeoutError("timed out"))

        with (
            patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen),
            self.assertRaises(meeting_sync_module.MeetingSyncGraphTimeoutError),
        ):
            meeting_sync_module._fetch_graph_json(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                "token",
            )

    def test_fetch_graph_json_preserves_non_timeout_urlerror(self) -> None:
        def _fake_urlopen(request, *, timeout=None):
            del request, timeout
            raise URLError("temporary DNS failure")

        with (
            patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen),
            self.assertRaises(URLError),
        ):
            meeting_sync_module._fetch_graph_json(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                "token",
            )
```

- [x] **Step 3: Run focused tests and verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_wraps_timeout_error \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_wraps_urlerror_timeout_reason \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_preserves_non_timeout_urlerror \
  -v
```

Expected: failures because `MeetingSyncGraphTimeoutError` does not exist or timeouts are not wrapped yet.

## Task 2: Implement Graph Timeout Classification

**Files:**
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
- Test: `tests/test_meeting_sync.py`

- [x] **Step 1: Add socket import**

In `src/obsidian_intake_agent/meetings/sync.py`, add this import near the other standard-library imports:

```python
import socket
```

- [x] **Step 2: Add the domain exception and classifier**

Near `GRAPH_REQUEST_TIMEOUT_SECONDS`, add:

```python
class MeetingSyncGraphTimeoutError(RuntimeError):
    def __init__(self, *, operation: str, timeout_seconds: int) -> None:
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Microsoft Graph did not respond within {timeout_seconds} seconds while {operation}."
        )


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | socket.timeout):
        return True
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError | socket.timeout):
            return True
        return "timed out" in str(reason).lower()
    return "timed out" in str(exc).lower()
```

- [x] **Step 3: Wrap Graph JSON and byte request timeouts**

Update `_fetch_graph_json`:

```python
    try:
        with urlopen(request, timeout=GRAPH_REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except (TimeoutError, socket.timeout, URLError) as exc:
        if _is_timeout_error(exc):
            raise MeetingSyncGraphTimeoutError(
                operation="Microsoft Graph JSON request",
                timeout_seconds=GRAPH_REQUEST_TIMEOUT_SECONDS,
            ) from exc
        raise
```

Update `_fetch_graph_bytes`:

```python
    try:
        with urlopen(request, timeout=GRAPH_REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except (TimeoutError, socket.timeout, URLError) as exc:
        if _is_timeout_error(exc):
            raise MeetingSyncGraphTimeoutError(
                operation="Microsoft Graph content request",
                timeout_seconds=GRAPH_REQUEST_TIMEOUT_SECONDS,
            ) from exc
        raise
```

- [x] **Step 4: Run focused timeout tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_wraps_timeout_error \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_wraps_urlerror_timeout_reason \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_preserves_non_timeout_urlerror \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_bytes_uses_timeout \
  -v
```

Expected: `OK`.

## Task 3: Add CLI Timeout Handling Tests

**Files:**
- Modify: `tests/test_main.py`

- [x] **Step 1: Import the timeout exception**

In `tests/test_main.py`, extend the `obsidian_intake_agent.meetings` import list:

```python
    MeetingSyncGraphTimeoutError,
```

- [x] **Step 2: Add a failing CLI test**

Add this test near the existing `test_meetings_sync_transcripts_*` tests:

```python
    def test_meetings_sync_transcripts_graph_timeout_prints_concise_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch(
                    "obsidian_intake_agent.main.build_transcript_sync_plan",
                    side_effect=MeetingSyncGraphTimeoutError(
                        operation="listing recently ended meetings",
                        timeout_seconds=30,
                    ),
                ),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "sync-transcripts",
                        "--since",
                        "2026-05-01",
                        "--dry-run",
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), "")
            error_output = stderr.getvalue()
            self.assertIn("meeting_sync_error: graph_timeout", error_output)
            self.assertIn("meeting_sync_error_detail:", error_output)
            self.assertIn("30 seconds", error_output)
            self.assertIn("meeting_sync_next_step:", error_output)
            self.assertNotIn("Traceback", error_output)
```

- [x] **Step 3: Run the CLI test and verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_main.MainCliTests.test_meetings_sync_transcripts_graph_timeout_prints_concise_error \
  -v
```

Expected: failure because `main()` does not catch `MeetingSyncGraphTimeoutError` yet.

## Task 4: Implement CLI Timeout Handling

**Files:**
- Modify: `src/obsidian_intake_agent/main.py`
- Test: `tests/test_main.py`

- [x] **Step 1: Import the timeout exception**

In `src/obsidian_intake_agent/main.py`, extend the `from .meetings import (...)` list:

```python
    MeetingSyncGraphTimeoutError,
```

- [x] **Step 2: Add a rendering helper**

Near the existing small output helper functions in `main.py`, add:

```python
def _print_meeting_sync_graph_timeout(exc: MeetingSyncGraphTimeoutError) -> None:
    print("meeting_sync_error: graph_timeout", file=sys.stderr)
    print(f"meeting_sync_error_detail: {exc}", file=sys.stderr)
    print(
        "meeting_sync_next_step: The next scheduled run will retry automatically. "
        "If this repeats, run `obsidian-agent graph status` and consider increasing the Graph timeout.",
        file=sys.stderr,
    )
```

- [x] **Step 3: Catch only the timeout exception around plan construction**

In the `args.meetings_command == "sync-transcripts"` path, replace:

```python
            sync_plan = build_transcript_sync_plan(
                client=_build_meeting_discovery_client(config),
                artifact_discovery_client=_build_meeting_artifact_discovery_client(
                    config,
                    download_transcripts=args.download_transcripts,
                ),
                since=since,
                intake_root=config.vault_path / config.intake_dir,
            )
```

with:

```python
            try:
                sync_plan = build_transcript_sync_plan(
                    client=_build_meeting_discovery_client(config),
                    artifact_discovery_client=_build_meeting_artifact_discovery_client(
                        config,
                        download_transcripts=args.download_transcripts,
                    ),
                    since=since,
                    intake_root=config.vault_path / config.intake_dir,
                )
            except MeetingSyncGraphTimeoutError as exc:
                _print_meeting_sync_graph_timeout(exc)
                return 1
```

- [x] **Step 4: Run CLI timeout and success tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_main.MainCliTests.test_meetings_sync_transcripts_graph_timeout_prints_concise_error \
  tests.test_main.MainCliTests.test_meetings_sync_transcripts_prints_dry_run_summary \
  tests.test_main.MainCliTests.test_meetings_sync_transcripts_download_transcripts_prints_write_summary \
  -v
```

Expected: `OK`.

## Task 5: Full Verification

**Files:**
- All changed files

- [x] **Step 1: Run focused meeting-sync tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests \
  tests.test_main.MainCliTests.test_meetings_sync_transcripts_graph_timeout_prints_concise_error \
  tests.test_main.MainCliTests.test_meetings_sync_transcripts_prints_dry_run_summary \
  tests.test_main.MainCliTests.test_meetings_sync_transcripts_download_transcripts_prints_write_summary \
  -v
```

Expected: `OK`.

- [x] **Step 2: Run project checks**

Run:

```bash
make check
```

Expected: all lint, typecheck, format-check, shell parse, and config checks pass.

- [x] **Step 3: Run tests when loopback permissions allow**

Run:

```bash
make test
```

Expected: `OK`. If sandbox blocks loopback socket tests with `PermissionError: [Errno 1] Operation not permitted`, rerun outside the sandbox with the same command.

- [x] **Step 4: Review the diff**

Run:

```bash
git diff -- src/obsidian_intake_agent/meetings/sync.py src/obsidian_intake_agent/main.py tests/test_meeting_sync.py tests/test_main.py docs/superpowers/specs/2026-05-21-meeting-sync-graph-timeout-errors-design.md docs/superpowers/plans/2026-05-21-meeting-sync-graph-timeout-errors.md
```

Expected: changes are limited to timeout handling, tests, and approved docs.

- [ ] **Step 5: Commit only this timeout-handling work after review**

Run:

```bash
git add src/obsidian_intake_agent/meetings/sync.py src/obsidian_intake_agent/main.py tests/test_meeting_sync.py tests/test_main.py docs/superpowers/specs/2026-05-21-meeting-sync-graph-timeout-errors-design.md docs/superpowers/plans/2026-05-21-meeting-sync-graph-timeout-errors.md
git commit -m "fix: summarize meeting sync graph timeouts"
```

Expected: commit includes only the timeout error handling implementation, tests, and spec/plan docs.
