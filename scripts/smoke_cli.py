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
        archive_intake_dir = "_Archive/Intake"
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
                    f'archive_intake_dir: "{archive_intake_dir}"',
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

        web_clips_intake = intake / "Web Clips"
        web_clips_intake.mkdir(parents=True)
        raw_web_clip = web_clips_intake / "2026-05-18 - Smoke Web Clip.md"
        raw_web_clip.write_text(
            "\n".join(
                [
                    "---",
                    "type: web_clip_intake",
                    "status: unprocessed",
                    'captured_at: "2026-05-18T10:00:00+00:00"',
                    'source_url: "https://example.com/smoke-web-clip"',
                    'source_title: "Smoke Web Clip"',
                    "---",
                    "",
                    "# Smoke Web Clip",
                    "",
                    "Source: https://example.com/smoke-web-clip",
                    "",
                    "## Why This Matters",
                    "",
                    "Useful context for smoke coverage of saved web clips.",
                    "",
                    "## Captured Passages",
                    "",
                    "> Saved clips should process into reference notes.",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        web_clip_completed = subprocess.run(
            [
                str(python_bin),
                "-m",
                "obsidian_intake_agent.main",
                "--config",
                str(config_path),
                "web-clips",
                "process",
            ],
            check=True,
            cwd=str(repo_root),
            env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
            capture_output=True,
            text=True,
        )

        web_clip_reference = vault / "10_References" / "Web Clips" / raw_web_clip.name
        web_clip_archive = vault / archive_intake_dir / "Web Clips" / raw_web_clip.name

        assert_contains(web_clip_completed.stdout, "web_clips_processed_files: 1")
        assert_contains(web_clip_completed.stdout, "web_clips_written_reference_notes: 1")
        assert_contains(web_clip_completed.stdout, "web_clips_archived_raw_captures: 1")
        assert_file_contains(web_clip_reference, "# Smoke Web Clip")
        assert_file_contains(web_clip_reference, "[[_Archive/Intake/Web Clips/2026-05-18 - Smoke Web Clip.md]]")
        assert_file_contains(web_clip_archive, "type: web_clip_intake")
        if raw_web_clip.exists():
            raise AssertionError(f"expected web clip source to be archived: {raw_web_clip}")

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
