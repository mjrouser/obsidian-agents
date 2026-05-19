# Meeting Sync Interval Polling Design

## Summary

Add a workweek-only launchd polling job that imports recently ended Teams meeting
artifacts into the existing Obsidian meeting-ingestion pipeline. The job should
run on predictable clock ticks, avoid overlapping runs, keep calendar-only items
from becoming production notes, and fail clearly when Microsoft Graph or auth
hangs or breaks.

## Goals

- Poll Microsoft Graph during the workweek, Monday through Friday.
- Run at `:05` and `:35` clock ticks from `8:35 AM` through `6:05 PM`.
- Use a rolling seven-day sync window so late-published transcripts are picked
  up without scanning month-to-date on every run.
- Stage only useful meeting artifacts through the existing
  `meetings sync-transcripts --download-transcripts` path.
- Process ready bundles into production meeting notes and action notes through
  `meetings process-bundles --execute`.
- Reuse the existing automation wrapper so nonzero failures create an Obsidian
  action-needed note and best-effort desktop notification.
- Add a non-blocking lock so a slow prior run prevents overlap and exits cleanly.
- Add explicit Graph request timeouts so network stalls fail rather than hanging
  indefinitely.

## Non-Goals

- Do not build a long-running daemon.
- Do not switch the polling implementation to Graph change notifications.
- Do not use validation output folders for the installed automation job.
- Do not process calendar-only bundles into production notes.
- Do not change Entra app permissions or move to application permissions.

## Recommended Approach

Use launchd `StartCalendarInterval` entries instead of `StartInterval`.

`StartCalendarInterval` keeps the job aligned to the clock, which matches the
meeting pattern: meetings ending at the top or bottom of the hour are checked
about five minutes later. `StartInterval` is simpler, but it drifts from the
time launchd loaded the job and may run at unhelpful minutes such as `:12` and
`:42`.

The launchd job should call a new wrapper:

```bash
scripts/run_meeting_sync.sh
```

The wrapper should:

1. Change to the repo root.
2. Export `PYTHON_BIN="$REPO_ROOT/.venv/bin/python"`.
3. Acquire a non-blocking `lockf` lock under `logs/locks/meeting-sync.lockfile`.
4. If the lock is held, log that a previous run is still active and exit `0`.
5. Compute `SINCE` as local today minus seven days.
6. Run:

```bash
"$REPO_ROOT/.venv/bin/obsidian-agent" meetings sync-transcripts --since "$SINCE" --download-transcripts
"$REPO_ROOT/.venv/bin/obsidian-agent" meetings process-bundles --execute
```

The wrapper should run through `scripts/run_automation_command.sh` so failures
continue to use the current logs, failure-note, and notification behavior.

## Schedule

Add a launchd job labeled:

```text
com.obsidian.agent.meeting-sync
```

The schedule should include Monday through Friday at:

```text
08:35
09:05
09:35
10:05
10:35
11:05
11:35
12:05
12:35
13:05
13:35
14:05
14:35
15:05
15:35
16:05
16:35
17:05
17:35
18:05
```

This gives twenty runs per weekday and catches meetings ending at `6:00 PM`
without polling later into the evening.

## Data Flow

1. launchd starts `scripts/run_meeting_sync.sh` on a scheduled tick.
2. The wrapper obtains the lock or exits quietly if another run is active.
3. `sync-transcripts --download-transcripts` discovers ended meetings from the
   last seven days and stages available transcript or fallback artifacts.
4. Identity markers prevent repeated staging for already-imported meetings.
5. `process-bundles --execute` processes only bundles with a valid processor
   handoff. Calendar-only and permission-blocked bundles remain blocked.
6. Successful processing writes canonical meeting notes to `01_Meetings`,
   Matthew-owned actions to `07_Actions`, writes durable processed markers, and
   cleans up machine-managed staging files.

## Error Handling

- Overlap: if the `lockf` lock is already held, the wrapper logs and exits `0`.
- Graph auth required: existing `graph_auth_required` behavior exits nonzero,
  which triggers the automation failure note and desktop notification.
- Microsoft Graph stalls: Graph HTTP calls should use explicit timeouts and
  surface timeout failures as normal nonzero automation failures or per-source
  retrieval warnings, depending on the existing call path.
- Throttling: if Graph returns `429`, the current HTTP error path should report
  the Graph detail. Future retry/backoff can be added after observing real
  throttling; the first implementation should avoid aggressive retry loops.

## Operational Considerations

The cadence is intentionally near real-time for 30-minute meetings while staying
inside workweek hours. The rolling seven-day window catches transcripts that
appear late, and existing dedupe keeps repeated runs from creating duplicate
meeting notes.

The expected Microsoft Graph volume is modest for a single delegated user, but
it is still recurring polling. Keeping the job to work hours, seven days, and no
aggressive retries reduces IT and throttling risk.

## Files To Change

- `scripts/run_meeting_sync.sh`: new wrapper.
- `scripts/render_launchd_plists.py`: add meeting-sync launchd job and schedule.
- `scripts/check_launch_agents.sh`: include meeting-sync label and logs.
- `src/obsidian_intake_agent/meetings/sync.py`: add explicit Graph request
  timeout handling.
- `tests/test_render_launchd_plists.py`: cover generated meeting-sync job.
- Meeting sync or Graph tests as needed for timeout behavior.
- `README.md`, `USAGE.md`, or `docs/RUNBOOK_HOWTO.md`: document installation,
  schedule, logs, and manual recovery.

## Validation

- Run focused tests for launchd rendering and meeting sync timeout behavior.
- Run `make check`.
- Run a manual dry run:

```bash
./.venv/bin/obsidian-agent meetings sync-transcripts --since "$(date -v-7d +%F)" --dry-run
./.venv/bin/obsidian-agent meetings process-bundles --dry-run
```

- Render launchd plists and inspect the meeting-sync schedule.
- After installation, verify `scripts/check_launch_agents.sh` reports
  `com.obsidian.agent.meeting-sync` as loaded.
