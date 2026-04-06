# Obsidian Agents

`obsidian_intake_agent` watches or processes files from an Obsidian intake folder, writes canonical meeting notes into `01_Meetings`, appends a weekly actions note in `07_Actions`, and can update weekly review notes in `09_Weekly Reviews`.

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

## Setup

1. Clone the repository.
2. Create a virtual environment:

   ```bash
   python3 -m venv .venv
   ```

3. Activate it and install the package in editable mode:

   ```bash
   source .venv/bin/activate
   ./.venv/bin/python -m pip install -e .
   ```

4. Copy [`config.example.yaml`](/Users/matthew.rouser/repos/obsidian-agents/config.example.yaml) to [`config.yaml`](/Users/matthew.rouser/repos/obsidian-agents/config.yaml) if needed, then review and edit it.
5. Set `vault_path` to the absolute path of your Obsidian vault.

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
git_auto_commit_vault: false
git_auto_commit_project: false
git_vault_repo_path: null
git_project_repo_path: null
watcher_settle_seconds: 5
watcher_stable_seconds: 2
automation_log_dir: "logs"
```

`weekly_reviews_dir` is the preferred key. The older `snapshots_dir` key is still accepted for backward compatibility.

Set `dry_run: false` when you want the agent to write files instead of printing planned changes.
Set `include_unassigned: true` if weekly action notes should also include items without a parsed owner.
Set `git_auto_commit_vault: true` to auto-commit vault output changes after a successful non-dry-run processing command.
Set `git_auto_commit_project: true` to auto-commit project repo changes only when the project repo actually has changes.
If `git_vault_repo_path` is unset, it defaults to `vault_path`. If `git_project_repo_path` is unset, it defaults to the directory containing `config.yaml`.

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

With auto-commit enabled, successful non-dry-run `run --once` and `process` commands will independently attempt:
- a vault repo commit with `auto: process transcript outputs for <source filename>` (or `multiple intake files` for multi-file batch runs)
- a project repo commit with `auto: update agent code for transcript processing`

Dry runs never auto-commit, and each repo is skipped independently when disabled, unchanged, or not a git repo.

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
- For `launchd` jobs, prefer an absolute `codex_exec_cmd` path in `config.yaml` because `launchd` does not reliably inherit your interactive shell `PATH`.
- The Monday actions note is created or updated in `vault_path/07_Actions`.
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

## Quality Checks

Run:

```bash
make check
make test
make build
```

Current behavior:

- `make check` verifies `git`, `rg` when present, and `./.venv/bin/python`.
- `make test` runs the unit test suite.
- `make build` byte-compiles the source and tests.
