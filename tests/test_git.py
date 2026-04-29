from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import call, patch

from obsidian_intake_agent.utils.git import auto_commit_repo


class GitAutoCommitTests(unittest.TestCase):
    @patch("obsidian_intake_agent.utils.git.shutil.which", return_value=None)
    def test_fails_clearly_when_git_is_unavailable(self, _which_mock) -> None:
        with self.assertRaisesRegex(RuntimeError, "git is required for auto-commit"):
            auto_commit_repo(Path("/tmp/repo"), "msg")

    @patch("obsidian_intake_agent.utils.git.shutil.which", return_value="/usr/bin/git")
    @patch("obsidian_intake_agent.utils.git.subprocess.run")
    def test_skips_gracefully_when_repo_is_not_git_repo(self, run_mock, _which_mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=["git", "rev-parse", "--is-inside-work-tree"],
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        )

        status = auto_commit_repo(Path("/tmp/not-repo"), "msg")

        self.assertEqual(status.state, "not_git_repo")
        run_mock.assert_called_once_with(
            ["git", "rev-parse", "--is-inside-work-tree"],
            check=False,
            cwd="/tmp/not-repo",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @patch("obsidian_intake_agent.utils.git.shutil.which", return_value="/usr/bin/git")
    @patch("obsidian_intake_agent.utils.git.subprocess.run")
    def test_no_empty_commit_when_repo_has_no_changes(self, run_mock, _which_mock) -> None:
        run_mock.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="true\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        status = auto_commit_repo(Path("/tmp/repo"), "msg")

        self.assertEqual(status.state, "no_changes")
        self.assertEqual(run_mock.call_count, 2)

    @patch("obsidian_intake_agent.utils.git.shutil.which", return_value="/usr/bin/git")
    @patch("obsidian_intake_agent.utils.git.subprocess.run")
    def test_vault_commit_occurs_when_changes_exist(self, run_mock, _which_mock) -> None:
        run_mock.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="true\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=" M note.md\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="M  note.md\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[main abc123] msg\n", stderr=""),
        ]

        status = auto_commit_repo(Path("/tmp/repo"), "auto: process transcript outputs for file.md")

        self.assertEqual(status.state, "committed")
        self.assertEqual(
            run_mock.mock_calls,
            [
                call(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    check=False,
                    cwd="/tmp/repo",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
                call(
                    ["git", "status", "--porcelain"],
                    check=False,
                    cwd="/tmp/repo",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
                call(
                    ["git", "add", "-A"],
                    check=True,
                    cwd="/tmp/repo",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
                call(
                    ["git", "status", "--porcelain"],
                    check=False,
                    cwd="/tmp/repo",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
                call(
                    ["git", "commit", "-m", "auto: process transcript outputs for file.md"],
                    check=True,
                    cwd="/tmp/repo",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            ],
        )

    @patch("obsidian_intake_agent.utils.git.shutil.which", return_value="/usr/bin/git")
    @patch("obsidian_intake_agent.utils.git.subprocess.run")
    def test_no_empty_commit_after_staging_if_changes_disappear(self, run_mock, _which_mock) -> None:
        run_mock.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="true\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="?? new.md\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        status = auto_commit_repo(Path("/tmp/repo"), "msg")

        self.assertEqual(status.state, "no_changes")
        self.assertEqual(run_mock.call_count, 4)


if __name__ == "__main__":
    unittest.main()
