# Web Clipper LaunchAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the local web clipper capture server as a macOS LaunchAgent so clipping works without keeping a terminal tab open.

**Architecture:** Reuse the existing launchd rendering and automation wrapper pattern. Add one thin shell wrapper that loads a local untracked token env file, then runs `obsidian-agent web-clips serve`; add one rendered plist and docs for setup, rotation, and health checks.

**Tech Stack:** Python 3.11, shell, macOS launchd, stdlib plist rendering, existing `obsidian-agent` CLI.

---

### Task 1: Launchd Rendering

**Files:**
- Modify: `scripts/render_launchd_plists.py`
- Test: `tests/test_render_launchd_plists.py`
- Regenerate: `ops/launchd/rendered/com.obsidian.agent.web-clipper.plist`

- [x] **Step 1: Write failing tests**

Add `tests/test_render_launchd_plists.py` with tests that call `render_plist` and `build_jobs` after importing `scripts/render_launchd_plists.py`. Verify the job list includes `com.obsidian.agent.web-clipper`, uses `scripts/run_web_clipper_server.sh`, and has `RunAtLoad` plus `KeepAlive`.

- [x] **Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_render_launchd_plists.py -q`
Expected: FAIL because `build_jobs` or the web clipper job does not exist yet.

- [x] **Step 3: Implement rendering support**

Extract the existing hardcoded job list into `build_jobs(label_prefix: str, scripts_dir: Path) -> list[tuple[str, Path, dict[str, str]]]`. Add the web clipper job:

```python
(
    f"{label_prefix}.web-clipper",
    scripts_dir / "run_web_clipper_server.sh",
    {
        "RunAtLoad": "<true/>",
        "KeepAlive": "<true/>",
    },
)
```

- [x] **Step 4: Run the focused test and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_render_launchd_plists.py -q`
Expected: PASS.

- [x] **Step 5: Regenerate plists**

Run: `./.venv/bin/python scripts/render_launchd_plists.py`
Expected: prints the existing three plist paths plus `ops/launchd/rendered/com.obsidian.agent.web-clipper.plist`.

### Task 2: Web Clipper Wrapper

**Files:**
- Create: `scripts/run_web_clipper_server.sh`
- Test: `tests/test_web_clipper_launch_wrapper.py`

- [x] **Step 1: Write failing wrapper tests**

Add tests that read `scripts/run_web_clipper_server.sh` and verify it references `~/.config/obsidian-agents/web-clipper.env`, checks for `OBSIDIAN_WEB_CLIPPER_TOKEN`, uses `logs/web-clipper.stdout.log` and `logs/web-clipper.stderr.log`, and executes `obsidian-agent web-clips serve` through `scripts/run_automation_command.sh`.

- [x] **Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_web_clipper_launch_wrapper.py -q`
Expected: FAIL because the wrapper does not exist yet.

- [x] **Step 3: Implement wrapper**

Create a shell wrapper that:

```bash
#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="${OBSIDIAN_WEB_CLIPPER_ENV_FILE:-$HOME/.config/obsidian-agents/web-clipper.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [ -z "${OBSIDIAN_WEB_CLIPPER_TOKEN:-}" ]; then
  echo "OBSIDIAN_WEB_CLIPPER_TOKEN is required. Create $ENV_FILE or set OBSIDIAN_WEB_CLIPPER_TOKEN before loading the LaunchAgent." >&2
  exit 2
fi

mkdir -p logs
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
export PYTHON_BIN
exec bash "$REPO_ROOT/scripts/run_automation_command.sh" \
  "web-clipper" \
  "logs/web-clipper.stdout.log" \
  "logs/web-clipper.stderr.log" \
  "$REPO_ROOT/.venv/bin/obsidian-agent" web-clips serve
```

- [x] **Step 4: Make wrapper executable**

Run: `chmod +x scripts/run_web_clipper_server.sh`
Expected: file mode includes executable bits.

- [x] **Step 5: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_web_clipper_launch_wrapper.py tests/test_render_launchd_plists.py -q`
Expected: PASS.

### Task 3: Operator Docs and Health Check

**Files:**
- Modify: `README.md`
- Modify: `scripts/check_launch_agents.sh`

- [x] **Step 1: Write failing health-check expectation**

Extend `tests/test_web_clipper_launch_wrapper.py` to verify `scripts/check_launch_agents.sh` includes `com.obsidian.agent.web-clipper` and the two web clipper log files.

- [x] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_web_clipper_launch_wrapper.py -q`
Expected: FAIL because the health check does not list web clipper yet.

- [x] **Step 3: Update health check**

Add `com.obsidian.agent.web-clipper`, `web-clipper.stdout.log`, and `web-clipper.stderr.log` to `scripts/check_launch_agents.sh`.

- [x] **Step 4: Update README setup**

Document:
- `mkdir -p ~/.config/obsidian-agents`
- `umask 077`
- `printf 'OBSIDIAN_WEB_CLIPPER_TOKEN=%q\n' "$(openssl rand -base64 32)" > ~/.config/obsidian-agents/web-clipper.env`
- `source ~/.config/obsidian-agents/web-clipper.env`
- `./.venv/bin/obsidian-agent web-clips bookmarklet | pbcopy`
- loading/unloading `com.obsidian.agent.web-clipper.plist`
- token rotation requires regenerating the bookmarklet and restarting the LaunchAgent

- [x] **Step 5: Run focused tests and full checks**

Run:
- `.venv/bin/python -m pytest tests/test_render_launchd_plists.py tests/test_web_clipper_launch_wrapper.py -q`
- `make check`
- `make test`
- `make smoke`
- `make build`

Expected: PASS, unless unrelated pre-existing dirty changes introduce failures.
