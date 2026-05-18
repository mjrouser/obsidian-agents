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

    def test_outlook_graph_auth_defaults_preserve_unconfigured_auth(self) -> None:
        config_path = _write_config(self)

        loaded = Config.load(config_path)

        self.assertIsNone(loaded.outlook_graph_tenant_id)
        self.assertIsNone(loaded.outlook_graph_client_id)
        self.assertEqual(
            loaded.outlook_graph_scopes,
            (
                "https://graph.microsoft.com/Calendars.Read",
                "https://graph.microsoft.com/OnlineMeetings.Read",
                "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All",
            ),
        )
        self.assertEqual(
            loaded.outlook_graph_token_cache_path,
            Path("~/.cache/obsidian-intake-agent/msal_token_cache.json").expanduser(),
        )

    def test_outlook_graph_auth_settings_can_be_overridden(self) -> None:
        config_path = _write_config(
            self,
            'outlook_graph_tenant_id: "tenant-123"',
            'outlook_graph_client_id: "client-456"',
            'outlook_graph_scopes: ["scope-a", "scope-b"]',
            'outlook_graph_token_cache_path: "/tmp/custom-msal-cache.json"',
        )

        loaded = Config.load(config_path)

        self.assertEqual(loaded.outlook_graph_tenant_id, "tenant-123")
        self.assertEqual(loaded.outlook_graph_client_id, "client-456")
        self.assertEqual(loaded.outlook_graph_scopes, ("scope-a", "scope-b"))
        self.assertEqual(loaded.outlook_graph_token_cache_path, Path("/tmp/custom-msal-cache.json"))

    def test_archive_retention_defaults_load_when_omitted(self) -> None:
        config_path = _write_config(self)

        loaded = Config.load(config_path)

        self.assertEqual(loaded.actions_archive_dir, "Actions Archive")
        self.assertEqual(loaded.weekly_reviews_archive_dir, "Review & Wrap Archive")
        self.assertEqual(loaded.archive_retention_count, 4)

    def test_archive_retention_values_can_be_overridden(self) -> None:
        config_path = _write_config(
            self,
            'actions_archive_dir: "07_Actions/Archive"',
            'weekly_reviews_archive_dir: "09_Weekly Reviews/Archive"',
            "archive_retention_count: 8",
        )

        loaded = Config.load(config_path)

        self.assertEqual(loaded.actions_archive_dir, "07_Actions/Archive")
        self.assertEqual(loaded.weekly_reviews_archive_dir, "09_Weekly Reviews/Archive")
        self.assertEqual(loaded.archive_retention_count, 8)

    def test_archive_retention_count_must_be_positive(self) -> None:
        config_path = _write_config(self, "archive_retention_count: 0")

        with self.assertRaisesRegex(ValueError, "archive_retention_count must be a positive integer"):
            Config.load(config_path)


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
