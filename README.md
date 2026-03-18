# Obsidian Agents

`obsidian_intake_agent` watches or processes files from an Obsidian intake folder, writes canonical meeting notes into `01_Meetings`, and appends a weekly actions note in `07_Actions`.

## Requirements

- Python 3.11+
- Git
- `make`
- macOS or Linux shell

## Setup

1. Clone the repository.
2. Create a virtual environment:

   ```bash
   python3 -m venv .venv
   ```

3. Activate it and install the package in editable mode:

   ```bash
   source .venv/bin/activate
   python3 -m pip install -e .
   ```

4. Review and edit [`config.yaml`](/Users/matthew.rouser/repos/obsidian-agents/config.yaml).
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
archive_intake_dir: "_Archive/Intake"
templates_dir: "Templates"
owner_filter: "Matthew"
dry_run: true
include_unassigned: false
```

Set `dry_run: false` when you want the agent to write files instead of printing planned changes.
Set `include_unassigned: true` if weekly action notes should also include items without a parsed owner.

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

Process a specific file:

```bash
obsidian-agent process /absolute/path/to/file.md
```

You can also run the installed CLI without activating the virtual environment:

```bash
.venv/bin/obsidian-agent run --once
```

If you do not want to install the console script yet, you can run the module directly:

```bash
PYTHONPATH=src python3 -m obsidian_intake_agent.main run --once
```

## Behavior

- Unprocessed files are read from `vault_path/00_Intake`.
- Markdown, `.docx`, and `.vtt` inputs are supported.
- `INBOX.md`, placeholder/template filenames, and already-processed notes are skipped.
- A canonical meeting note is written to `vault_path/01_Meetings`.
- Markdown intake files extract action items from `Action:` lines and `- [ ]` checkboxes.
- Raw `.vtt` files are never modified; processing writes a canonical meeting note plus a processed intake sidecar note in `00_Intake`.
- `.vtt` extraction uses Codex CLI when `llm_provider: "codex_cli"` and otherwise falls back to heuristic extraction from `Action:`, `Decision:`, `Risk:`, and `Question:` lines.
- The Monday actions note is created or updated in `vault_path/07_Actions`.
- Weekly actions only include items owned by `owner_filter`, plus `Unassigned` items when `include_unassigned: true`.
- The intake file is prepended with `STATUS: PROCESSED — see [[...]]`.
- `obsidian-agent run` currently requires `--once`.
- In dry-run mode, planned writes are printed and no files are changed.

## Quality Checks

Run:

```bash
make check
make test
make build
```

Current behavior:

- `make check` verifies `git`, `rg` when present, and `python3`.
- `make test` runs the unit test suite.
- `make build` byte-compiles the source and tests.
