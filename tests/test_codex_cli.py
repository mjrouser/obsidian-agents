from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from obsidian_intake_agent.llm.codex_cli import build_codex_command, run_codex_stdout


class CodexCliTests(unittest.TestCase):
    def test_build_codex_command_uses_default_exec_and_model(self) -> None:
        self.assertEqual(
            build_codex_command("prompt", model="gpt-5"),
            ["codex", "exec", "--model", "gpt-5", "--skip-git-repo-check", "prompt"],
        )

    def test_build_codex_command_uses_configured_exec_command(self) -> None:
        self.assertEqual(
            build_codex_command("prompt", model=None, exec_cmd=["custom-codex", "exec"]),
            ["custom-codex", "exec", "--skip-git-repo-check", "prompt"],
        )

    def test_run_codex_stdout_returns_stdout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex", "exec", "prompt"],
            returncode=0,
            stdout="generated output",
        )

        with patch("subprocess.run", return_value=completed) as run_mock:
            result = run_codex_stdout(
                "prompt",
                model="gpt-5",
                exec_cmd=None,
                timeout_seconds=12,
                output_kind="markdown",
            )

        self.assertEqual(result, "generated output")
        run_mock.assert_called_once_with(
            ["codex", "exec", "--model", "gpt-5", "--skip-git-repo-check", "prompt"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            timeout=12,
        )

    def test_run_codex_stdout_raises_clear_timeout_for_output_kind(self) -> None:
        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["codex"], timeout=30),
            ),
            self.assertRaisesRegex(
                TimeoutError,
                "Codex CLI timed out after 30 seconds while returning markdown",
            ),
        ):
            run_codex_stdout(
                "prompt",
                model=None,
                exec_cmd=["codex", "exec"],
                timeout_seconds=30,
                output_kind="markdown",
            )


if __name__ == "__main__":
    unittest.main()
