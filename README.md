# Obsidian Agents

`obsidian_intake_agent` watches or processes files from an Obsidian intake folder, writes canonical meeting notes into `01_Meetings`, appends a weekly actions note in `07_Actions`, and can update weekly review notes in `09_Weekly Reviews`.

For day-to-day commands, automation checks, troubleshooting, and recovery steps,
see [`USAGE.md`](USAGE.md).

## Requirements

- Python 3.11+
- Git
- `make`
- macOS or Linux shell

## Environment Model

This repo is expected to run from the local virtual environment at [`.venv/bin/python`](/Users/matthew.rouser/repos/obsidian-agents/.venv/bin/python).

- Automation and `launchd` wrappers call `./.venv/bin/python` directly.
- `make test` and `make build` also use `./.venv/bin/python`.
- Do not rely on system `python` or `python3` for normal project execution.
- The CLI prints a warning if it is started outside the repo virtualenv.

## Code Organization

- `src/obsidian_intake_agent/config.py` owns configuration loading, defaults,
  and validation.
- `src/obsidian_intake_agent/processors/meeting_processor.py` owns intake file
  processing and meeting/action output orchestration.

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
archive_intake_dir: "z_Archive/Intake"
weekly_reviews_dir: "09_Weekly Reviews"
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
```

`weekly_reviews_dir` is the preferred key. The older `snapshots_dir` key is still accepted for backward compatibility.

Set `dry_run: false` when you want the agent to write files instead of printing planned changes.
Set `include_unassigned: true` if weekly action notes should also include items without a parsed owner.
Set `codex_timeout_seconds` to control how long Codex CLI extraction and weekly generation may run before failing. Use `null` only when you intentionally want no timeout.
Set `automation_error_dir` to control where automation failure notes are written inside the vault.
Set `git_auto_commit_vault: true` to auto-commit vault output changes after a successful non-dry-run processing command.
Project repo auto-commit is intentionally skipped even if `git_auto_commit_project: true`; review, test, commit, and merge project code changes manually.
If `git_vault_repo_path` is unset, it defaults to `vault_path`.

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
- Markdown intake files extract action items from `Action:` lines and `- [ ]` checkboxes.
- Raw `.vtt` files are never modified; processing writes a canonical meeting note plus a processed intake sidecar note in `00_Intake`.
- `.vtt` extraction uses Codex CLI when `llm_provider: "codex_cli"` and otherwise falls back to heuristic extraction from `Action:`, `Decision:`, `Risk:`, and `Question:` lines.
- Codex CLI calls use `codex_timeout_seconds` so watcher and weekly automation fail clearly instead of hanging indefinitely.
- For `launchd` jobs, prefer an absolute `codex_exec_cmd` path in `config.yaml` because `launchd` does not reliably inherit your interactive shell `PATH`.
- The Monday actions note is created or updated in `vault_path/07_Actions`.
- Weekly action notes use `## This Week` and `## Longer-Term / In Progress`; automated inserts always go under `This Week`.
- Weekly reviews are written to `vault_path/09_Weekly Reviews` as separate files:
- `YYYY-MM-DD Weekly Briefing.md`
- `YYYY-MM-DD Weekly Wrap.md`
- Both filenames use the Monday date of the relevant week.
- Weekly actions only include items owned by `owner_filter`, plus `Unassigned` items when `include_unassigned: true`.
- The intake file is prepended with `STATUS: PROCESSED — see [[...]]`.
- The watcher listens for both create and modify events, waits for the file to stop changing, and uses a per-file lock to avoid duplicate processing attempts.
- Draft intake basenames such as `Untitled.md` and `Untitled 2.md` are ignored until you rename them.
- `obsidian-agent run` currently requires `--once`.
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
`vault_path/_System/Agent Errors` by default and leaves the detailed trace in
the matching `logs/*.stderr.log` file.

## Quality Checks

Run:

```bash
make check
make lint
make test
make build
make audit
```

Current behavior:

- `make check` runs Ruff linting, verifies `git`, `rg` when present, verifies
  formatting, verifies `./.venv/bin/python`, checks config hygiene, and parses
  shell scripts.
- `make lint` runs Ruff lint and import-order checks against `src`, `tests`, and
  `scripts`.
- `make format-check` verifies Ruff formatting.
- `make format` applies Ruff formatting to `src`, `tests`, and `scripts`.
- `make test` runs the unit test suite.
- `make build` byte-compiles the source and tests.
- `make audit` scans locked dependencies with `pip-audit`.
- CI creates `.venv`, installs locked dependencies from `requirements.lock`,
  installs the project in editable mode without re-resolving dependencies, then
  runs the same `make check`, `make test`, `make build`, and `make audit`
  commands.

Update the dependency lockfile after changing `pyproject.toml`:

```bash
uv pip compile pyproject.toml --extra audit --extra dev --output-file requirements.lock
```
