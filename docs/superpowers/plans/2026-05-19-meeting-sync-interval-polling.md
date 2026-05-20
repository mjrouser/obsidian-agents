# Meeting Sync Interval Polling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Monday-Friday launchd polling job that syncs the last seven days of meeting artifacts on `:05`/`:35` clock ticks and processes ready bundles into production notes without overlapping runs.

**Architecture:** Reuse the existing launchd renderer and automation wrapper. Add one focused shell wrapper for meeting sync, a launchd schedule generated from code, a small Graph HTTP timeout change, and documentation/tests around the new automation. Keep calendar-only blocking in the existing bundle processing path.

**Tech Stack:** Python 3.11-compatible source, `unittest`, Bash, macOS `launchd`, Microsoft Graph via existing `urllib.request` helpers.

---

## File Structure

- Create: `scripts/run_meeting_sync.sh`
  - Responsibility: repo-root setup, non-overlap `lockf` lock, rolling seven-day `SINCE` computation, and invoking the existing CLI commands through `run_automation_command.sh`.
- Modify: `scripts/render_launchd_plists.py`
  - Responsibility: include `com.obsidian.agent.meeting-sync` with Monday-Friday `StartCalendarInterval` entries from `08:35` through `18:05`.
- Modify: `scripts/check_launch_agents.sh`
  - Responsibility: report meeting-sync launch agent and its stdout/stderr logs.
- Modify: `src/obsidian_intake_agent/meetings/sync.py`
  - Responsibility: add explicit timeout to Graph JSON and byte requests.
- Modify: `tests/test_render_launchd_plists.py`
  - Responsibility: verify the meeting-sync job, wrapper path, and schedule shape.
- Create: `tests/test_meeting_sync_launch_wrapper.py`
  - Responsibility: verify the meeting-sync wrapper lock, skip behavior, and signal forwarding.
- Modify: `tests/test_meeting_sync.py`
  - Responsibility: verify Graph helpers call `urlopen(..., timeout=...)`.
- Modify: `README.md` and `docs/RUNBOOK_HOWTO.md`
  - Responsibility: document the new job, schedule, logs, setup, and recovery commands.

## Pre-Flight

- [ ] **Step 1: Inspect the worktree and preserve unrelated edits**

Run:

```bash
git status --short
```

Expected: there may be unrelated web-clip changes. Do not stage, commit, revert, or overwrite them while implementing this plan.

- [ ] **Step 2: Re-read the approved spec**

Run:

```bash
sed -n '1,240p' docs/superpowers/specs/2026-05-19-meeting-sync-interval-polling-design.md
```

Expected: confirms Option A, rolling seven days, Mon-Fri `08:35` through `18:05`, lock, production execution, and Graph timeouts.

## Task 1: Add Graph Request Timeouts

**Files:**
- Modify: `tests/test_meeting_sync.py`
- Modify: `src/obsidian_intake_agent/meetings/sync.py`

- [ ] **Step 1: Add failing tests for URL open timeouts**

In `tests/test_meeting_sync.py`, update the two existing `_fake_urlopen` functions in:

- `TranscriptSyncPlannerTests.test_fetch_graph_json_omits_outlook_timezone_preference_by_default`
- `TranscriptSyncPlannerTests.test_fetch_graph_json_can_request_outlook_timezone_preference`

Change each fake from:

```python
def _fake_urlopen(request):
    captured["prefer"] = request.headers.get("Prefer")
    return _FakeResponse()
```

to:

```python
def _fake_urlopen(request, *, timeout=None):
    captured["prefer"] = request.headers.get("Prefer")
    captured["timeout"] = timeout
    return _FakeResponse()
```

Then add this assertion to each test after the existing header assertions:

```python
self.assertEqual(captured["timeout"], meeting_sync_module.GRAPH_REQUEST_TIMEOUT_SECONDS)
```

Add this new test near the JSON fetch tests:

```python
    def test_fetch_graph_bytes_uses_timeout(self) -> None:
        captured: dict[str, object] = {}

        class _FakeResponse:
            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def read(self) -> bytes:
                return b"WEBVTT\n"

        def _fake_urlopen(request, *, timeout=None):
            captured["accept"] = request.headers.get("Accept")
            captured["timeout"] = timeout
            return _FakeResponse()

        with patch("obsidian_intake_agent.meetings.sync.urlopen", side_effect=_fake_urlopen):
            content = meeting_sync_module._fetch_graph_bytes(
                "https://graph.microsoft.com/v1.0/me/onlineMeetings/transcripts/content",
                "token",
            )

        self.assertEqual(content, b"WEBVTT\n")
        self.assertEqual(captured["accept"], "text/vtt")
        self.assertEqual(captured["timeout"], meeting_sync_module.GRAPH_REQUEST_TIMEOUT_SECONDS)
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_omits_outlook_timezone_preference_by_default \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_can_request_outlook_timezone_preference \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_bytes_uses_timeout \
  -v
```

Expected: failure because `GRAPH_REQUEST_TIMEOUT_SECONDS` is not defined or `urlopen` is called without `timeout`.

- [ ] **Step 3: Implement Graph request timeouts**

In `src/obsidian_intake_agent/meetings/sync.py`, add this constant near the existing source-priority constants:

```python
GRAPH_REQUEST_TIMEOUT_SECONDS = 30
```

In `_fetch_graph_json`, change:

```python
with urlopen(request) as response:
```

to:

```python
with urlopen(request, timeout=GRAPH_REQUEST_TIMEOUT_SECONDS) as response:
```

In `_fetch_graph_bytes`, change:

```python
with urlopen(request) as response:
```

to:

```python
with urlopen(request, timeout=GRAPH_REQUEST_TIMEOUT_SECONDS) as response:
```

- [ ] **Step 4: Run focused timeout tests and verify pass**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_omits_outlook_timezone_preference_by_default \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_json_can_request_outlook_timezone_preference \
  tests.test_meeting_sync.TranscriptSyncPlannerTests.test_fetch_graph_bytes_uses_timeout \
  -v
```

Expected: `OK`.

## Task 2: Add The Meeting Sync Wrapper

**Files:**
- Create: `scripts/run_meeting_sync.sh`

- [ ] **Step 1: Create the wrapper script**

Create `scripts/run_meeting_sync.sh` with:

```bash
#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs logs/locks
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
AGENT_BIN="$REPO_ROOT/.venv/bin/obsidian-agent"
export PYTHON_BIN
CHILD_PID=""

LOCK_FILE="$REPO_ROOT/logs/locks/meeting-sync.lockfile"
terminate_child() {
  local signal="$1"
  if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
    terminate_tree "$CHILD_PID" "$signal"
    wait "$CHILD_PID" 2>/dev/null || true
  fi
}
terminate_tree() {
  local pid="$1"
  local signal="$2"
  local child_pid
  for child_pid in $(pgrep -P "$pid" 2>/dev/null || true); do
    terminate_tree "$child_pid" "$signal"
  done
  kill "-$signal" "$pid" 2>/dev/null || true
}
handle_term() {
  terminate_child TERM
  exit 143
}
handle_int() {
  terminate_child INT
  exit 130
}
trap handle_term TERM
trap handle_int INT

SINCE="$("$PYTHON_BIN" -c 'from datetime import date, timedelta; print((date.today() - timedelta(days=7)).isoformat())')"

lockf -t 0 "$LOCK_FILE" \
  bash "$REPO_ROOT/scripts/run_automation_command.sh" \
  "meeting-sync" \
  "logs/meeting-sync.stdout.log" \
  "logs/meeting-sync.stderr.log" \
  bash -c '"$1" meetings sync-transcripts --since "$2" --download-transcripts && "$1" meetings process-bundles --execute' \
  _ "$AGENT_BIN" "$SINCE" \
  2>/dev/null &
CHILD_PID=$!

set +e
wait "$CHILD_PID"
STATUS=$?
set -e
CHILD_PID=""
if [ "$STATUS" -eq 75 ]; then
  echo "meeting-sync: previous run still active; skipping this tick" | tee -a logs/meeting-sync.stdout.log
  exit 0
fi
exit "$STATUS"
```

Rationale: the OS-held `lockf` lock is acquired before entering the automation wrapper, so overlap skips are clean exits and do not create failure notes. Actual sync/process failures still flow through `run_automation_command.sh`. The wrapper waits on the automation child instead of using `exec` so TERM/INT can be forwarded through the child process tree. Because `lockf` is tied to the holding process, the lock is released even after `SIGKILL` or power loss.

- [ ] **Step 2: Make the wrapper executable**

Run:

```bash
chmod +x scripts/run_meeting_sync.sh
```

- [ ] **Step 3: Syntax-check the wrapper**

Run:

```bash
bash -n scripts/run_meeting_sync.sh
```

Expected: no output and exit `0`.

- [ ] **Step 4: Manually smoke the skip lock path**

Run:

```bash
mkdir -p logs/locks
lockf logs/locks/meeting-sync.lockfile sleep 5 &
holder=$!
./scripts/run_meeting_sync.sh
kill "$holder" 2>/dev/null || true
wait "$holder" 2>/dev/null || true
```

Expected output includes:

```text
meeting-sync: previous run still active; skipping this tick
```

## Task 3: Render The Launchd Meeting Sync Job

**Files:**
- Modify: `tests/test_render_launchd_plists.py`
- Modify: `scripts/render_launchd_plists.py`

- [ ] **Step 1: Add failing launchd schedule tests**

In `tests/test_render_launchd_plists.py`, add:

```python
    def test_build_jobs_includes_meeting_sync_launch_agent(self) -> None:
        module = _load_script_module()

        jobs = module.build_jobs(
            label_prefix="com.obsidian.agent",
            scripts_dir=Path("/repo/scripts"),
        )

        meeting_jobs = [job for job in jobs if job[0] == "com.obsidian.agent.meeting-sync"]
        self.assertEqual(len(meeting_jobs), 1)
        label, script_path, schedule = meeting_jobs[0]
        self.assertEqual(label, "com.obsidian.agent.meeting-sync")
        self.assertEqual(script_path, Path("/repo/scripts/run_meeting_sync.sh"))
        self.assertIn("StartCalendarInterval", schedule)
        self.assertIn("<array>", schedule["StartCalendarInterval"])
        self.assertIn("<key>Hour</key><integer>8</integer><key>Minute</key><integer>35</integer>", schedule["StartCalendarInterval"])
        self.assertIn("<key>Hour</key><integer>18</integer><key>Minute</key><integer>5</integer>", schedule["StartCalendarInterval"])
        self.assertEqual(schedule["StartCalendarInterval"].count("<key>Weekday</key><integer>1</integer>"), 20)
        self.assertEqual(schedule["StartCalendarInterval"].count("<dict>"), 100)

    def test_rendered_meeting_sync_plist_uses_calendar_intervals(self) -> None:
        module = _load_script_module()

        plist = module.render_plist(
            label="com.obsidian.agent.meeting-sync",
            script_path=Path("/repo/scripts/run_meeting_sync.sh"),
            schedule={
                "StartCalendarInterval": module.meeting_sync_start_calendar_interval(),
            },
        )

        self.assertIn("<string>com.obsidian.agent.meeting-sync</string>", plist)
        self.assertIn("<string>/repo/scripts/run_meeting_sync.sh</string>", plist)
        self.assertIn("<key>StartCalendarInterval</key>", plist)
        self.assertIn("<array>", plist)
        self.assertNotIn("<key>RunAtLoad</key>", plist)
        self.assertNotIn("<key>KeepAlive</key>", plist)
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_render_launchd_plists -v
```

Expected: failures because the meeting-sync job and `meeting_sync_start_calendar_interval()` do not exist yet.

- [ ] **Step 3: Add the launchd schedule helper**

In `scripts/render_launchd_plists.py`, add:

```python
MEETING_SYNC_TIMES = (
    (8, 35),
    (9, 5),
    (9, 35),
    (10, 5),
    (10, 35),
    (11, 5),
    (11, 35),
    (12, 5),
    (12, 35),
    (13, 5),
    (13, 35),
    (14, 5),
    (14, 35),
    (15, 5),
    (15, 35),
    (16, 5),
    (16, 35),
    (17, 5),
    (17, 35),
    (18, 5),
)


def meeting_sync_start_calendar_interval() -> str:
    entries: list[str] = []
    for weekday in range(1, 6):
        for hour, minute in MEETING_SYNC_TIMES:
            entries.append(
                "<dict>"
                f"<key>Weekday</key><integer>{weekday}</integer>"
                f"<key>Hour</key><integer>{hour}</integer>"
                f"<key>Minute</key><integer>{minute}</integer>"
                "</dict>"
            )
    return "<array>" + "".join(entries) + "</array>"
```

- [ ] **Step 4: Add meeting-sync to `build_jobs()`**

In `scripts/render_launchd_plists.py`, add this tuple to the list returned by `build_jobs()`:

```python
        (
            f"{label_prefix}.meeting-sync",
            scripts_dir / "run_meeting_sync.sh",
            {
                "StartCalendarInterval": meeting_sync_start_calendar_interval(),
            },
        ),
```

Place it near the other scheduled jobs, after `intake-watcher` and before weekly jobs.

- [ ] **Step 5: Run launchd renderer tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_render_launchd_plists -v
```

Expected: `OK`.

- [ ] **Step 6: Render plists and inspect meeting-sync output**

Run:

```bash
./.venv/bin/python scripts/render_launchd_plists.py
sed -n '1,80p' ops/launchd/rendered/com.obsidian.agent.meeting-sync.plist
```

Expected: the rendered plist exists, has label `com.obsidian.agent.meeting-sync`, calls `scripts/run_meeting_sync.sh`, and contains a `StartCalendarInterval` array.

## Task 4: Add Health Check Coverage

**Files:**
- Modify: `scripts/check_launch_agents.sh`

- [ ] **Step 1: Add the meeting-sync label**

In `scripts/check_launch_agents.sh`, add this label to the `labels` array:

```bash
  "com.obsidian.agent.meeting-sync"
```

- [ ] **Step 2: Add meeting-sync logs**

In the `for log_name in \` block, add:

```bash
  "meeting-sync.stdout.log" \
  "meeting-sync.stderr.log" \
```

Place them near the other automation logs.

- [ ] **Step 3: Syntax-check the health script**

Run:

```bash
bash -n scripts/check_launch_agents.sh
```

Expected: no output and exit `0`.

## Task 4.5: Add Wrapper Lock And Signal Tests

**Files:**
- Create: `tests/test_meeting_sync_launch_wrapper.py`

- [ ] **Step 1: Add wrapper tests**

Create tests that:

- Assert `scripts/run_meeting_sync.sh` uses `lockf -t 0`, `meeting-sync.lockfile`, recursive `terminate_tree`, `pgrep -P`, and TERM/INT exit codes.
- Copy the wrapper into a temporary repo, hold `logs/locks/meeting-sync.lockfile` with `lockf`, run the wrapper, and verify it logs `previous run still active` and exits `0`.
- Copy the wrapper into a temporary repo with a fake `run_automation_command.sh` that creates a child and grandchild, send `TERM` to the wrapper, and verify both descendants exit.

- [ ] **Step 2: Run wrapper tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync_launch_wrapper -v
```

Expected: `OK`.

## Task 5: Document The New Automation

**Files:**
- Modify: `README.md`
- Modify: `docs/RUNBOOK_HOWTO.md`

- [ ] **Step 1: Update README automation setup**

In `README.md`, in the Automation Setup section, update the numbered list so meeting sync is included:

```markdown
1. Run the watcher continuously with `scripts/run_intake_watcher.sh`.
2. Poll meeting transcripts during the workweek with `scripts/run_meeting_sync.sh`.
3. Schedule Monday and Friday jobs with `launchd`.
4. Keep the web clipper capture server available with `launchd`.
5. Inspect `logs/*.log` when a run needs debugging.
```

In the launchctl unload/load command blocks, add:

```bash
launchctl unload ~/Library/LaunchAgents/com.obsidian.agent.meeting-sync.plist 2>/dev/null || true
```

and:

```bash
launchctl load ~/Library/LaunchAgents/com.obsidian.agent.meeting-sync.plist
```

In the default schedule list, add:

```markdown
- Meeting sync: Monday-Friday at `:05` and `:35`, from `08:35` through `18:05`
```

Add this short operational note after the default schedule list:

```markdown
Meeting sync uses a rolling seven-day window and processes only ready bundles.
Calendar-only and permission-blocked bundles remain staged/blocked until a real
transcript or fallback artifact is available. If a previous meeting-sync run is
still active, the next tick logs a skip and exits successfully.
```

- [ ] **Step 2: Update runbook launchctl commands**

In `docs/RUNBOOK_HOWTO.md`, add `com.obsidian.agent.meeting-sync.plist` to the start, stop, and restart command blocks.

Add this troubleshooting section after "Check Weekly Jobs":

```markdown
### Check Meeting Sync

```bash
tail -n 50 logs/meeting-sync.stdout.log
tail -n 50 logs/meeting-sync.stderr.log
```

Meeting sync runs Monday-Friday at `:05` and `:35` from `08:35` through `18:05`.
It uses a rolling seven-day window. If Graph auth expires, run:

```bash
./.venv/bin/obsidian-agent graph status
./.venv/bin/obsidian-agent graph login
```
```

When adding the fenced code block inside Markdown, keep the outer file syntax valid by using normal Markdown fences in the actual file.

- [ ] **Step 3: Review docs for exact labels and log names**

Run:

```bash
rg -n "meeting-sync|Meeting sync|com.obsidian.agent.meeting-sync" README.md docs/RUNBOOK_HOWTO.md
```

Expected: labels and log names consistently use `meeting-sync`.

## Task 6: Full Verification

**Files:**
- All changed files

- [ ] **Step 1: Run focused tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_render_launchd_plists tests.test_meeting_sync.TranscriptSyncPlannerTests -v
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_meeting_sync_launch_wrapper -v
```

Expected: `OK`.

- [ ] **Step 2: Run project check**

Run:

```bash
make check
```

Expected: all checks pass, including Ruff, mypy, format check, shell parse, and config checks.

- [ ] **Step 3: Smoke the manual dry-run commands**

Run:

```bash
SINCE="$("./.venv/bin/python" -c 'from datetime import date, timedelta; print((date.today() - timedelta(days=7)).isoformat())')"
./.venv/bin/obsidian-agent meetings sync-transcripts --since "$SINCE" --dry-run | sed -n '1,20p'
./.venv/bin/obsidian-agent meetings process-bundles --dry-run | sed -n '1,20p'
```

Expected: both commands print summary lines and do not write production notes.

- [ ] **Step 4: Review changed files**

Run:

```bash
git diff -- scripts/run_meeting_sync.sh scripts/render_launchd_plists.py scripts/check_launch_agents.sh src/obsidian_intake_agent/meetings/sync.py tests/test_render_launchd_plists.py tests/test_meeting_sync.py tests/test_meeting_sync_launch_wrapper.py README.md docs/RUNBOOK_HOWTO.md
```

Expected: diff contains only meeting-sync polling changes.

- [ ] **Step 5: Stage and commit only meeting-sync polling files**

Run:

```bash
git add scripts/run_meeting_sync.sh scripts/render_launchd_plists.py scripts/check_launch_agents.sh src/obsidian_intake_agent/meetings/sync.py tests/test_render_launchd_plists.py tests/test_meeting_sync.py tests/test_meeting_sync_launch_wrapper.py README.md docs/RUNBOOK_HOWTO.md ops/launchd/rendered/com.obsidian.agent.meeting-sync.plist docs/superpowers/specs/2026-05-19-meeting-sync-interval-polling-design.md docs/superpowers/plans/2026-05-19-meeting-sync-interval-polling.md
git commit -m "feat: add meeting sync interval polling"
```

Expected: commit includes only meeting-sync polling code, docs, spec, and plan.

## Task 7: Manual Installation After Merge Or Local Approval

**Files:**
- Generated: `ops/launchd/rendered/com.obsidian.agent.meeting-sync.plist`

- [ ] **Step 1: Render launchd plists**

Run:

```bash
./.venv/bin/python scripts/render_launchd_plists.py
```

Expected: output includes:

```text
ops/launchd/rendered/com.obsidian.agent.meeting-sync.plist
```

- [ ] **Step 2: Copy and load launch agents**

Run:

```bash
cp ops/launchd/rendered/*.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.obsidian.agent.meeting-sync.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.obsidian.agent.meeting-sync.plist
```

Expected: no error from `launchctl load`.

- [ ] **Step 3: Verify health check sees the job**

Run:

```bash
./scripts/check_launch_agents.sh
```

Expected: includes:

```text
loaded: com.obsidian.agent.meeting-sync
```

- [ ] **Step 4: Watch first-run logs**

Run after the next scheduled tick:

```bash
tail -n 80 logs/meeting-sync.stdout.log
tail -n 80 logs/meeting-sync.stderr.log
```

Expected: stdout shows meeting sync summaries or a clean overlap skip. Stderr should be empty or contain only expected Graph retrieval warnings already reflected in command output.
