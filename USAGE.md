# Obsidian Agents Usage

This guide is for day-to-day operation of `obsidian-agent`. For installation,
configuration details, and development checks, start with
[`README.md`](README.md). For macOS launchd operations, see
[`docs/RUNBOOK_HOWTO.md`](docs/RUNBOOK_HOWTO.md).

## Safety Model

The app is designed to be safe to test before it writes anything.

- `dry_run: true` in `config.yaml` prints planned changes without writing notes,
  action files, status markers, archives, or git commits.
- `obsidian-agent process --dry-run ...` and `obsidian-agent weekly --dry-run ...`
  override the config for that command.
- Already-processed notes are skipped unless `--force` is used.
- Raw `.vtt` transcripts are archived unchanged; the processed marker is written
  to a sidecar intake note.
- Codex CLI extraction and weekly generation use `codex_timeout_seconds` so
  automation fails clearly instead of hanging indefinitely.

Use dry runs when changing config, prompts, dependencies, or parsing behavior.

## Daily Commands

Run commands from the repository root:

```bash
cd /Users/matthew.rouser/repos/obsidian-agents
```

Process all currently unprocessed intake files once:

```bash
./.venv/bin/obsidian-agent run --once
```

Process one file:

```bash
./.venv/bin/obsidian-agent process "/absolute/path/to/intake-file.md"
```

Preview processing for one file:

```bash
./.venv/bin/obsidian-agent process "/absolute/path/to/intake-file.md" --dry-run
```

Reprocess an already-processed file:

```bash
./.venv/bin/obsidian-agent process "/absolute/path/to/intake-file.md" --force
```

Watch the intake folder continuously:

```bash
./.venv/bin/obsidian-agent watch
```

Generate weekly notes manually:

```bash
./.venv/bin/obsidian-agent weekly briefing
./.venv/bin/obsidian-agent weekly wrap
```

Preview a weekly note update:

```bash
./.venv/bin/obsidian-agent weekly briefing --dry-run
./.venv/bin/obsidian-agent weekly wrap --dry-run
```

Run a weekly job for a specific date:

```bash
./.venv/bin/obsidian-agent weekly briefing --date 2026-04-27
./.venv/bin/obsidian-agent weekly wrap --date 2026-04-27
```

## Expected Outputs

For intake processing, the app reads from `vault_path/intake_dir` and can write:

- canonical meeting notes under `meetings_dir`
- weekly action notes under `actions_dir`
- archived source files under `archive_intake_dir`
- processed status markers on markdown intake notes
- sidecar processed notes for `.vtt` inputs

For weekly processing, the app writes separate files under `weekly_reviews_dir`:

- `YYYY-MM-DD Weekly Briefing.md`
- `YYYY-MM-DD Weekly Wrap.md`

The date in those filenames is the Monday of the relevant week.

## Automation

The launchd workflow uses shell wrappers in `scripts/` and the repo virtual
environment at `./.venv/bin/python`.

Render launchd plist files after changing automation scripts or schedules:

```bash
./.venv/bin/python scripts/render_launchd_plists.py
```

Copy the rendered plists into LaunchAgents:

```bash
cp ops/launchd/rendered/*.plist ~/Library/LaunchAgents/
```

Restart all jobs:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.intake-watcher.plist 2>/dev/null || true
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-briefing.plist 2>/dev/null || true
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-wrap.plist 2>/dev/null || true

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.intake-watcher.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-briefing.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.obsidian.agent.weekly-wrap.plist
```

Check launchd status and expected log files:

```bash
./scripts/check_launch_agents.sh
```

## Troubleshooting

Check the stderr log first when something did not run:

```bash
tail -n 80 logs/intake-watcher.stderr.log
tail -n 80 logs/weekly-briefing.stderr.log
tail -n 80 logs/weekly-wrap.stderr.log
```

Check stdout logs for normal command output:

```bash
tail -n 80 logs/intake-watcher.stdout.log
tail -n 80 logs/weekly-briefing.stdout.log
tail -n 80 logs/weekly-wrap.stdout.log
```

Common checks:

- Confirm the repo virtual environment exists: `test -x ./.venv/bin/python`
- Confirm dependencies are installed: `./.venv/bin/python -m pip check`
- Confirm the app is installed: `./.venv/bin/obsidian-agent --help`
- Confirm launchd jobs are loaded: `launchctl list | grep obsidian.agent`
- Confirm local `config.yaml` exists and points to the intended Obsidian vault.
- Confirm `dry_run` is set the way you intend before expecting writes.
- If Codex CLI times out, review `codex_timeout_seconds`, `codex_exec_cmd`, and
  the relevant stderr log before rerunning.

`config.yaml` is intentionally ignored by git. To rebuild it on a new machine,
copy `config.example.yaml` to `config.yaml`, then set local paths and automation
flags.

After code, config, dependency, or prompt changes, run:

```bash
make check
make test
make build
make audit
```

Then restart launchd jobs if automation should use the new behavior.

## Recovery Notes

If an intake note processed incorrectly, inspect the canonical meeting note,
weekly action note, archived source file, and original intake status marker.
Use `--force --dry-run` first to preview a repair:

```bash
./.venv/bin/obsidian-agent process "/absolute/path/to/intake-file.md" --force --dry-run
```

If the preview is correct, rerun without `--dry-run`:

```bash
./.venv/bin/obsidian-agent process "/absolute/path/to/intake-file.md" --force
```

If vault auto-commit is enabled, inspect the vault repo after recovery:

```bash
git -C "/absolute/path/to/vault" status --short
```

Project repo changes are not auto-committed by the app. Review and commit them
manually after checks pass.
