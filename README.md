# Obsidian Agents

`obsidian_intake_agent` watches or processes files from an Obsidian intake folder, writes canonical meeting notes into `01_Meetings`, appends a weekly actions note in `07_Actions`, and can update weekly review notes in `09_Weekly Reviews`.

For day-to-day commands, automation checks, troubleshooting, and recovery steps,
see [`USAGE.md`](USAGE.md).

## Requirements

- Python 3.11+
  - Python 3.11 is the supported compatibility floor.
  - CI also validates Python 3.14 to match the newer local runtime line.
- Git
- `make`
- macOS or Linux shell

## Environment Model

This repo is expected to run from the local virtual environment at [`.venv/bin/python`](/Users/matthew.rouser/repos/obsidian-agents/.venv/bin/python).

- Automation and `launchd` wrappers call `./.venv/bin/python` directly.
- `make test` and `make build` also use `./.venv/bin/python`.
- Do not rely on system `python` or `python3` for normal project execution.
- The CLI prints a warning if it is started outside the repo virtualenv.
- Keep code compatible with Python 3.11 unless `pyproject.toml`, CI, and this
  README are deliberately updated together.

## Code Organization

- `src/obsidian_intake_agent/config.py` owns configuration loading, defaults,
  and validation.
- `src/obsidian_intake_agent/processors/meeting_metadata.py` owns meeting
  filename metadata normalization.
- `src/obsidian_intake_agent/processors/intake_state.py` owns intake
  eligibility, processed-state detection, and archive destination rules.
- `src/obsidian_intake_agent/processors/meeting_processor.py` owns intake file
  processing and meeting/action output orchestration.
- `src/obsidian_intake_agent/processors/vtt_extractor.py` owns VTT transcript
  extraction, heuristic fallback, and extracted data normalization.
- `src/obsidian_intake_agent/llm/codex_cli.py` owns Codex CLI command
  construction, stdout capture, and timeout handling.
- `src/obsidian_intake_agent/rendering/meeting_renderer.py` owns meeting-note
  rendering.
- `src/obsidian_intake_agent/rendering/action_renderer.py` owns weekly
  action-note parsing, deduplication, and rendering.

## Setup

1. Clone the repository.
2. Create a virtual environment:

   ```bash
   python3 -m venv .venv
   ```

3. Activate it and install the package in editable mode:

   ```bash
   source .venv/bin/activate
   ./.venv/bin/python -m pip install -r requirements.lock
   ./.venv/bin/python -m pip install -e . --no-deps
   ```

4. Copy [`config.example.yaml`](config.example.yaml) to `config.yaml` if needed, then review and edit it.
5. Set `vault_path` to the absolute path of your Obsidian vault.

`config.yaml` is a local, ignored file because it contains machine-specific
paths and operational settings. Keep reusable defaults in
[`config.example.yaml`](config.example.yaml) instead.

Example:

```yaml
vault_path: "/Users/yourname/Documents/MyVault"
```

Default config:

```yaml
vault_path: "/PATH/TO/YOUR/VAULT"
intake_dir: "00_Intake"
meetings_dir: "01_Meetings"
actions_dir: "07_Actions"
validation_meetings_dir: "99_Test Notes/Meetings"
validation_actions_dir: "99_Test Notes/Actions"
archive_intake_dir: "z_Archive/Intake"
weekly_reviews_dir: "09_Weekly Reviews"
actions_archive_dir: "Actions Archive"
weekly_reviews_archive_dir: "Review & Wrap Archive"
archive_retention_count: 4
templates_dir: "Templates"
owner_filter: "Matthew"
dry_run: true
include_unassigned: false
llm_provider: "codex_cli"
codex_model: null
codex_exec_cmd: ["codex", "exec"]
codex_timeout_seconds: 300
extraction_mode: "draft"
git_auto_commit_vault: false
# Project repo auto-commit is intentionally unsupported at runtime.
git_auto_commit_project: false
git_vault_repo_path: null
git_project_repo_path: null
watcher_settle_seconds: 5
watcher_stable_seconds: 2
automation_log_dir: "logs"
automation_error_dir: "_System/Agent Errors"
# Entra public-client auth settings. Tenant/client IDs are not credentials, but
# keep real organization/app values in local config.yaml rather than committing
# them here.
outlook_graph_tenant_id: null
outlook_graph_client_id: null
outlook_graph_scopes:
  - "https://graph.microsoft.com/Calendars.Read"
  - "https://graph.microsoft.com/OnlineMeetings.Read"
  - "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All"
outlook_graph_token_cache_path: "~/.cache/obsidian-intake-agent/msal_token_cache.json"
# Optional debug override. Prefer device-code login and the MSAL cache above.
outlook_graph_access_token_env: "OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN"
outlook_graph_api_base_url: "https://graph.microsoft.com/v1.0"
```

`weekly_reviews_dir` is the preferred key. The older `snapshots_dir` key is still accepted for backward compatibility.

Set `dry_run: false` when you want the agent to write files instead of printing planned changes.
Set `include_unassigned: true` if weekly action notes should also include items without a parsed owner.
Set `codex_timeout_seconds` to control how long Codex CLI extraction and weekly generation may run. VTT extraction falls back to heuristic extraction on timeout; weekly generation still fails clearly. Use `null` only when you intentionally want no timeout.
Set `automation_error_dir` to control where automation failure notes are written inside the vault.
Set `git_auto_commit_vault: true` to auto-commit vault output changes after a successful non-dry-run processing command.
Project repo auto-commit is intentionally skipped even if `git_auto_commit_project: true`; review, test, commit, and merge project code changes manually.
If `git_vault_repo_path` is unset, it defaults to `vault_path`.
For meeting transcript sync, set `outlook_graph_tenant_id` and
`outlook_graph_client_id` in local `config.yaml`, then run
`obsidian-agent graph login` once to create a local MSAL token cache. Tenant IDs
and public-client app IDs are not secrets, but the project keeps real
organization/app values out of committed config so app registrations can be
rotated or replaced without repo churn. `outlook_graph_access_token_env` remains
available as a temporary override for debugging; do not commit access tokens.

## Run

After installation, the CLI is available as `obsidian-agent`. In this repository, dependencies were installed into [`.venv`](/Users/matthew.rouser/repos/obsidian-agents/.venv).

Process the current unprocessed intake files once:

```bash
obsidian-agent run --once
```

Watch the intake directory for new files:

```bash
obsidian-agent watch
```

Generate or update the Monday weekly briefing block:

```bash
obsidian-agent weekly briefing
```

Generate or update the Friday weekly wrap block:

```bash
obsidian-agent weekly wrap
```

Process a specific file:

```bash
obsidian-agent process /absolute/path/to/file.md
```

Dry-run recently ended meeting candidates for future transcript sync:

```bash
obsidian-agent graph status
obsidian-agent graph login
obsidian-agent meetings sync-transcripts --since 2026-05-01 --dry-run
```

Write only planned intake bundle notes, without downloading artifacts yet:

```bash
obsidian-agent meetings sync-transcripts --since 2026-05-01 --write-bundles
```

Download available Teams `.vtt` transcripts into
`00_Intake/bundles/raw_transcripts` and write matching bundle notes into
`00_Intake/bundles`:

```bash
obsidian-agent meetings sync-transcripts --since 2026-05-01 --download-transcripts
```

Dry-run which written meeting bundles are ready to feed into the existing processor:

```bash
obsidian-agent meetings process-bundles --dry-run
```

Attach an explicit local transcript to an existing synced bundle:

```bash
obsidian-agent meetings attach-transcript \
  --event-id "<outlook-event-id>" \
  --file "/absolute/path/to/transcript.vtt"
```

`attach-transcript` stages the `.vtt`, `.md`, or `.docx` file under
`00_Intake/bundles/raw_transcripts` and updates the bundle sidecar. It does not
process the bundle; rerun `process-bundles --dry-run` before executing.

Process ready meeting bundles through the existing intake processor:

```bash
obsidian-agent meetings process-bundles --execute
```

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

Use these Graph auth helpers to inspect, refresh, or clear the local cached
login:

```bash
obsidian-agent graph status
obsidian-agent graph login
obsidian-agent graph logout
```

With vault auto-commit enabled, successful non-dry-run `run --once` and `process` commands will attempt a vault repo commit with `auto: process transcript outputs for <source filename>` (or `multiple intake files` for multi-file batch runs).

Dry runs never auto-commit. Project repo changes are never auto-committed by the app; commit them manually after reviewing the diff and running checks.

You can also run the installed CLI without activating the virtual environment:

```bash
.venv/bin/obsidian-agent run --once
```

The repo also includes thin wrapper scripts around the same CLI:

```bash
./.venv/bin/python scripts/process_intake.py
./.venv/bin/python scripts/process_intake.py --path "/absolute/path/to/file.md"
./.venv/bin/python scripts/watch_intake.py
./.venv/bin/python scripts/generate_weekly_summary.py briefing
./.venv/bin/python scripts/generate_weekly_summary.py wrap
```

If you do not want to install the console script yet, you can run the module directly:

```bash
PYTHONPATH=src ./.venv/bin/python -m obsidian_intake_agent.main run --once
```

## Behavior

- Unprocessed files are read from `vault_path/00_Intake`.
- Markdown, `.docx`, and `.vtt` inputs are supported.
- `INBOX.md`, placeholder/template filenames, and already-processed notes are skipped.
- A canonical meeting note is written to `vault_path/01_Meetings`.
- Meeting notes start with YAML front matter for Obsidian properties, including
  participant, attendee, organizer, Outlook event, Teams meeting, transcript,
  and source-file fields when that context is available.
- Markdown intake files extract action items from `Action:` lines and `- [ ]` checkboxes.
- Raw `.vtt` files are never modified; processing writes a canonical meeting note plus a processed intake sidecar note in `00_Intake`.
- `.vtt` extraction uses Codex CLI when `llm_provider: "codex_cli"` and otherwise falls back to heuristic extraction from `Action:`, `Decision:`, `Risk:`, and `Question:` lines.
- If Codex CLI times out during VTT extraction, the processor writes a heuristic note with an explicit source limitation instead of failing the watcher.
- Timeout fallback events are also reported as `vtt_extraction_fallback` warnings in `logs/intake-watcher.log` and as `processing_warning:` lines during manual `process` runs.
- Codex CLI calls use `codex_timeout_seconds` so long-running weekly automation fails clearly instead of hanging indefinitely.
- For `launchd` jobs, prefer an absolute `codex_exec_cmd` path in `config.yaml` because `launchd` does not reliably inherit your interactive shell `PATH`.
- The Monday actions note is created or updated in `vault_path/07_Actions`.
- Weekly action notes include a top Tasks query for open tasks in the current file, then use `## This Week`, `## Carry Over Items`, and `## Longer-Term / In Progress`; new weekly notes seed `Carry Over Items` from the previous week's unfinished items, and automated inserts always go under `This Week`.
- Weekly reviews are written to `vault_path/09_Weekly Reviews` as separate files:
- `YYYY-MM-DD Weekly Briefing.md`
- `YYYY-MM-DD Weekly Wrap.md`
- Both filenames use the Monday date of the relevant week.
- After successful non-dry-run writes, the agent keeps the four newest dated Markdown records in `07_Actions` and `09_Weekly Reviews` and moves older dated records into their configured archive subfolders. Recency is based on the leading `YYYY-MM-DD` filename date, not file modified time. Files without a leading date and files already inside archive folders are ignored.
- Weekly actions only include items owned by `owner_filter`, plus `Unassigned` items when `include_unassigned: true`.
- The intake file is prepended with `STATUS: PROCESSED — see [[...]]`.
- The watcher listens for both create and modify events, waits for the file to stop changing, and uses a per-file lock to avoid duplicate processing attempts.
- Draft intake basenames such as `Untitled.md` and `Untitled 2.md` are ignored until you rename them.
- `obsidian-agent run` currently requires `--once`.
- `obsidian-agent meetings sync-transcripts` currently requires exactly one of
  `--dry-run`, `--write-bundles`, or `--download-transcripts`. `--dry-run` and
  `--write-bundles` plan candidate meetings from Outlook metadata without
  downloading transcripts. `--download-transcripts` downloads available Teams
  `.vtt` transcript content into `00_Intake/bundles/raw_transcripts`. When Graph
  transcript content is unavailable but transcript `metadataContent` is
  available, the same command falls back to writing a local Markdown transcript
  artifact in `00_Intake/bundles/raw_transcripts` and records that file as the
  preferred processor handoff. When neither transcript-quality source is
  available, the sync path now tries a Copilot AI recap before chat, writes any
  recap-based fallback handoff to `00_Intake/bundles/fallbacks`, and uses
  meeting chat only as supplemental context for that fallback note rather than
  as a standalone processor source. If Entra auth is not configured, the
  command returns a warning-only plan instead of discovered meetings. If Entra
  auth is configured but the cached Graph token cannot be refreshed silently,
  the command exits nonzero with `graph_auth_required` and prints manual
  recovery steps such as `obsidian-agent graph login`. For meetings that would
  be processed, the plan reports the intake bundle note path and
  source-transparency metadata.
  `--write-bundles` writes only those planned bundle notes into
  `00_Intake/bundles` and skips existing bundle files. If the planned bundle
  note already exists, the planner now reports that meeting as an explicit skip
  so repeated polling runs stay quieter. Planned bundle notes now include
  Outlook organizer, attendee, response-status, and join-link context when
  Graph discovery provides it. `--write-bundles` also writes a sibling Outlook
  metadata sidecar JSON file for each processable meeting and will backfill
  that sidecar when an older bundle note exists without it. The sync path now
  also writes a hidden identity marker under `00_Intake/bundles/_meeting_sync`
  keyed from the Outlook event ID and Teams meeting ID so repeated polling can
  skip already-imported meetings even if the bundle filename later changes.
  Dry-run output now includes top-level counts for processable meetings that
  are still missing `.vtt`, transcript text, chat, recap, or any non-calendar
  source. The planner also now reports explicit per-source retrieval states for
  `.vtt`, transcript text, chat, and recap: `available`, `missing`,
  `permission_blocked`, or `not_attempted`. When the local intake folder
  exists, the planner now actively probes `00_Intake` for canonical
  date/title-matched `.vtt`, `.md`, and `.docx` transcript artifacts before
  falling back to `missing` or `not_attempted`, and it preserves those exact
  matched artifact paths in the bundle note artifact plan plus the raw Outlook
  metadata sidecar. If no exact match is found, the missing-source detail shows
  the expected local transcript filename stem and any same-date local transcript
  candidates as suggestions only. When one of those transcript artifacts is
  available, the bundle contract now also records a preferred processor input
  path/source so the next ingestion step can feed the existing processor
  directly. Bundle
  source limitations include the retrieval detail from Graph or local discovery
  so permission blocks and unpublished transcripts are visible in the note. The
  sync planner prefilters meetings that are not yet eligible for bundle
  ingestion, including canceled meetings, non-Teams events, not-yet-ended
  events, declined or no-response meetings, low-signal office-hours subjects,
  all-day or focus blocks without meeting content, and meetings whose bundle
  identity marker already exists.
- `obsidian-agent meetings process-bundles` currently requires `--dry-run` and
  `--execute` as mutually exclusive modes. The dry run reads the written
  Outlook metadata sidecars in `00_Intake/bundles` to report which synced
  meeting bundles are actually ready for the existing `process` command. The
  execution mode runs only those ready handoffs through the current intake
  processor and reports per-bundle `processed`, `skipped`, and `failed`
  outcomes. On successful processing, the execution step writes a durable
  processed marker under `00_Intake/bundles/_meeting_sync/identities` and
  removes the machine-managed bundle note, Outlook metadata sidecar, and any
  machine-managed artifacts under `00_Intake/bundles/raw_transcripts` or
  `00_Intake/bundles/fallbacks`, while leaving unrelated intake files alone.
  Add `--validation` with `--execute` to route generated canonical meeting
  notes and Matthew-owned weekly actions into the test-only `99_Test Notes`
  lane instead of `01_Meetings` and `07_Actions`.
  Both modes distinguish calendar-only bundles, missing local transcript files,
  permission-blocked retrieval, and handoffs the current intake processor would
  skip as already processed.
- In dry-run mode, planned writes are printed and no files are changed.

## Automation Setup

The simplest local v1 setup on macOS is:

1. Run the watcher continuously with `scripts/run_intake_watcher.sh`.
2. Schedule Monday and Friday jobs with `launchd`.
3. Inspect `logs/*.log` when a run needs debugging.

Render launchd plist files:

```bash
./.venv/bin/python scripts/render_launchd_plists.py
```

That writes plist files into [`ops/launchd/rendered`](/Users/matthew.rouser/repos/obsidian-agents/ops/launchd/rendered). Copy them into `~/Library/LaunchAgents/`, then load them:

```bash
launchctl unload ~/Library/LaunchAgents/com.obsidian.agent.intake-watcher.plist 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.obsidian.agent.weekly-briefing.plist 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.obsidian.agent.weekly-wrap.plist 2>/dev/null || true

cp ops/launchd/rendered/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.obsidian.agent.intake-watcher.plist
launchctl load ~/Library/LaunchAgents/com.obsidian.agent.weekly-briefing.plist
launchctl load ~/Library/LaunchAgents/com.obsidian.agent.weekly-wrap.plist
```

Quick health check after a restart:

```bash
./scripts/check_launch_agents.sh
```

Default schedule:

- Monday `09:00`: weekly briefing
- Friday `12:15`: weekly wrap
- Watcher: starts at login and stays alive

When a launchd wrapper exits with a real failure, it writes an Obsidian note to
`vault_path/_System/Agent Errors` by default. The timestamped note preserves the
failure details, and `ACTION NEEDED - Latest Automation Failure.md` points at the
newest failure so it is easy to spot. The detailed trace stays in the matching
`logs/*.stderr.log` file. If the configured `config.yaml` is missing or cannot be
loaded, the failure-note helper falls back to `logs/automation-failures/` beside
the stderr log so the job still leaves a readable artifact. On macOS, the wrapper
also sends a best-effort desktop notification through `osascript`; set
`AUTOMATION_FAILURE_NOTIFICATIONS=0` to disable that notification. Graph sync
auth failures use this same path, so an unattended run that prints
`graph_auth_required` also leaves an action-needed note and desktop notification
instead of relying on daily log review.

## Quality Checks

Run:

```bash
make check
make lint
make typecheck
make test
make smoke
make build
make audit
```

Current behavior:

- `make check` runs Ruff linting, mypy type checking, verifies `git`, `rg` when
  present, verifies formatting, verifies `./.venv/bin/python`, checks config
  hygiene, and parses shell scripts.
- `make lint` runs Ruff lint and import-order checks against `src`, `tests`, and
  `scripts`.
- `make typecheck` runs mypy against `src`.
- `make format-check` verifies Ruff formatting.
- `make format` applies Ruff formatting to `src`, `tests`, and `scripts`.
- `make test` runs the unit test suite.
- `make smoke` runs the installed CLI against a temporary vault and verifies
  meeting-note, action-note, archive, and command-output behavior.
- `make build` byte-compiles the source and tests.
- `make audit` scans locked dependencies with `pip-audit`.
- CI creates `.venv`, installs locked dependencies from `requirements.lock`,
  installs the project in editable mode without re-resolving dependencies, then
  runs the same `make check`, `make test`, `make smoke`, `make build`, and
  `make audit` commands against Python 3.11 and Python 3.14.

Update the dependency lockfile after changing `pyproject.toml`:

```bash
uv pip compile pyproject.toml --extra audit --extra dev --output-file requirements.lock
```
