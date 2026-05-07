#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    repo_python_bin = repo_root / ".venv" / "bin" / "python"
    python_bin = repo_python_bin if repo_python_bin.exists() else Path(sys.executable)
    with tempfile.TemporaryDirectory(prefix="obsidian-agent-smoke-") as tmp_dir:
        workspace = Path(tmp_dir)
        vault = workspace / "vault"
        intake = vault / "00_Intake"
        intake.mkdir(parents=True)
        source = intake / "2026-03-12 - Teams - Smoke Test.md"
        source.write_text(
            "Agenda\n\nAction: Matthew will verify the CLI smoke test by Friday.\n",
            encoding="utf-8",
        )
        ts = datetime(2026, 3, 12, 10, 0, 0).timestamp()
        os.utime(source, (ts, ts))

        config_path = workspace / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    f'vault_path: "{vault}"',
                    'intake_dir: "00_Intake"',
                    'meetings_dir: "01_Meetings"',
                    'actions_dir: "07_Actions"',
                    'archive_intake_dir: "_Archive/Intake"',
                    'weekly_reviews_dir: "09_Weekly Reviews"',
                    'templates_dir: "Templates"',
                    'owner_filter: "Matthew"',
                    "dry_run: false",
                    "include_unassigned: false",
                    'llm_provider: "none"',
                    "git_auto_commit_vault: false",
                    "git_auto_commit_project: false",
                    "action_owner_aliases:",
                    "  Matthew Rouser:",
                    '    - "Matthew"',
                    '    - "Matthew Rouser"',
                    '    - "Matt"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                str(python_bin),
                "-m",
                "obsidian_intake_agent.main",
                "--config",
                str(config_path),
                "run",
                "--once",
            ],
            check=True,
            cwd=str(repo_root),
            env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
            capture_output=True,
            text=True,
        )

        meeting = vault / "01_Meetings" / "2026-03-12 - Teams - Smoke Test.md"
        actions = vault / "07_Actions" / "2026-03-09.md"
        archived = vault / "_Archive" / "Intake" / source.name

        assert_contains(completed.stdout, "processed_files: 1")
        assert_contains(completed.stdout, "written_meeting_notes: 1")
        assert_contains(completed.stdout, "updated_actions_files: 1")
        assert_contains(completed.stdout, "git auto-commit vault: skipped (disabled)")
        assert_file_contains(meeting, "# 2026-03-12 - Teams - Smoke Test")
        assert_file_contains(meeting, "- Intake File: [[_Archive/Intake/2026-03-12 - Teams - Smoke Test.md]]")
        assert_file_contains(actions, "verify the CLI smoke test by Friday")
        assert_file_contains(archived, "STATUS: PROCESSED")
        if source.exists():
            raise AssertionError(f"expected source to be archived: {source}")

    print("[OK] CLI smoke test passed")
    return 0


def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in output:\n{text}")


def assert_file_contains(path: Path, expected: str) -> None:
    if not path.exists():
        raise AssertionError(f"expected file to exist: {path}")
    text = path.read_text(encoding="utf-8")
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {path}:\n{text}")


if __name__ == "__main__":
    raise SystemExit(main())
