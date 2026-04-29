from __future__ import annotations

import unittest
from unittest.mock import patch

from obsidian_intake_agent.llm.codex_extractor import run_codex_json


class CodexExtractorTests(unittest.TestCase):
    def test_parses_json_from_stdout(self) -> None:
        with patch(
            "obsidian_intake_agent.llm.codex_extractor.run_codex_stdout",
            return_value='{"title": "Weekly Sync", "actions": []}',
        ) as run_mock:
            result = run_codex_json("prompt", "gpt-5")

        self.assertEqual(result["title"], "Weekly Sync")
        run_mock.assert_called_once_with(
            "prompt",
            model="gpt-5",
            exec_cmd=None,
            timeout_seconds=None,
            output_kind="JSON",
        )

    def test_uses_configured_exec_command_when_provided(self) -> None:
        with patch(
            "obsidian_intake_agent.llm.codex_extractor.run_codex_stdout",
            return_value='{"title": "Weekly Sync", "actions": []}',
        ) as run_mock:
            run_codex_json("prompt", None, exec_cmd=["custom-codex", "exec"])

        run_mock.assert_called_once_with(
            "prompt",
            model=None,
            exec_cmd=["custom-codex", "exec"],
            timeout_seconds=None,
            output_kind="JSON",
        )

    def test_raises_clear_error_on_invalid_json(self) -> None:
        with (
            patch("obsidian_intake_agent.llm.codex_extractor.run_codex_stdout", return_value="not json output"),
            self.assertRaisesRegex(
                ValueError,
                "Codex CLI did not return valid JSON on stdout",
            ) as exc,
        ):
            run_codex_json("prompt", None)

        self.assertIn("not json output", str(exc.exception))

    def test_raises_clear_error_on_timeout(self) -> None:
        with (
            patch(
                "obsidian_intake_agent.llm.codex_extractor.run_codex_stdout",
                side_effect=TimeoutError("Codex CLI timed out after 12 seconds while returning JSON."),
            ),
            self.assertRaisesRegex(
                TimeoutError,
                "Codex CLI timed out after 12 seconds while returning JSON",
            ),
        ):
            run_codex_json("prompt", None, timeout_seconds=12)


if __name__ == "__main__":
    unittest.main()
