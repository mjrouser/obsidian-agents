from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obsidian_intake_agent.config import Config


class ConfigCompatibilityTests(unittest.TestCase):
    def test_validation_output_dirs_default_when_omitted(self) -> None:
        config_path = _write_config(self)

        loaded = Config.load(config_path)

        self.assertEqual(loaded.validation_meetings_dir, "99_Test Notes/Meetings")
        self.assertEqual(loaded.validation_actions_dir, "99_Test Notes/Actions")

    def test_validation_output_dirs_can_be_overridden(self) -> None:
        config_path = _write_config(
            self,
            'validation_meetings_dir: "99_Custom Validation/Meetings"',
            'validation_actions_dir: "99_Custom Validation/Actions"',
        )

        loaded = Config.load(config_path)

        self.assertEqual(loaded.validation_meetings_dir, "99_Custom Validation/Meetings")
        self.assertEqual(loaded.validation_actions_dir, "99_Custom Validation/Actions")

    def test_legacy_snapshots_dir_key_still_loads_weekly_reviews_dir(self) -> None:
        config_path = _write_config(self, 'snapshots_dir: "09_Weekly Reviews"')

        loaded = Config.load(config_path)

        self.assertEqual(loaded.weekly_reviews_dir, "09_Weekly Reviews")

    def test_codex_timeout_defaults_to_300_seconds(self) -> None:
        config_path = _write_config(self)

        loaded = Config.load(config_path)

        self.assertEqual(loaded.codex_timeout_seconds, 300)

    def test_codex_timeout_can_be_disabled(self) -> None:
        config_path = _write_config(self, "codex_timeout_seconds: null")

        loaded = Config.load(config_path)

        self.assertIsNone(loaded.codex_timeout_seconds)

    def test_codex_timeout_must_be_positive(self) -> None:
        config_path = _write_config(self, "codex_timeout_seconds: 0")

        with self.assertRaisesRegex(ValueError, "codex_timeout_seconds must be a positive integer"):
            Config.load(config_path)

    def test_outlook_graph_defaults_load(self) -> None:
        config_path = _write_config(self)

        loaded = Config.load(config_path)

        self.assertEqual(loaded.outlook_graph_access_token_env, "OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN")
        self.assertEqual(loaded.outlook_graph_api_base_url, "https://graph.microsoft.com/v1.0")


def _write_config(test_case: unittest.TestCase, *extra_lines: str) -> Path:
    tmp_dir = tempfile.TemporaryDirectory()
    test_case.addCleanup(tmp_dir.cleanup)
    path = Path(tmp_dir.name) / "config.yaml"
    path.write_text(
        "\n".join(
            [
                'vault_path: "/tmp/vault"',
                'intake_dir: "00_Intake"',
                'meetings_dir: "01_Meetings"',
                'actions_dir: "07_Actions"',
                'archive_intake_dir: "z_Archive/Intake"',
                'templates_dir: "Templates"',
                'owner_filter: "Matthew"',
                *extra_lines,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path
