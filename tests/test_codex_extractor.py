from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from obsidian_intake_agent.llm.codex_extractor import run_codex_json


class CodexExtractorTests(unittest.TestCase):
    def test_parses_json_from_stdout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex", "exec", "prompt"],
            returncode=0,
            stdout='{"title": "Weekly Sync", "actions": []}',
        )

        with patch("subprocess.run", return_value=completed) as run_mock:
            result = run_codex_json("prompt", "gpt-5")

        self.assertEqual(result["title"], "Weekly Sync")
        run_mock.assert_called_once_with(
            ["codex", "exec", "--model", "gpt-5", "--skip-git-repo-check", "prompt"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )

    def test_uses_configured_exec_command_when_provided(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["custom-codex", "exec", "prompt"],
            returncode=0,
            stdout='{"title": "Weekly Sync", "actions": []}',
        )

        with patch("subprocess.run", return_value=completed) as run_mock:
            run_codex_json("prompt", None, exec_cmd=["custom-codex", "exec"])

        run_mock.assert_called_once_with(
            ["custom-codex", "exec", "--skip-git-repo-check", "prompt"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )

    def test_raises_clear_error_on_invalid_json(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex", "exec", "prompt"],
            returncode=0,
            stdout="not json output",
        )

        with patch("subprocess.run", return_value=completed):
            with self.assertRaisesRegex(
                ValueError,
                "Codex CLI did not return valid JSON on stdout",
            ) as exc:
                run_codex_json("prompt", None)

        self.assertIn("not json output", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
