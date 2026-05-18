from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch

from obsidian_intake_agent.config import Config
from obsidian_intake_agent.graph_auth import GraphAuthProvider


class GraphAuthProviderTests(unittest.TestCase):
    def test_env_token_override_wins_without_touching_msal(self) -> None:
        config = _config()
        provider = GraphAuthProvider(
            config,
            environ={"OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN": "env-token"},
            app_factory=lambda **kwargs: self.fail("MSAL app should not be built for env override."),
        )

        result = provider.get_access_token()

        self.assertTrue(result.available)
        self.assertEqual(result.access_token, "env-token")
        self.assertEqual(result.source, "environment")

    def test_missing_config_returns_unavailable_result(self) -> None:
        config = _config(tenant_id=None, client_id=None)
        provider = GraphAuthProvider(config, environ={})

        result = provider.get_access_token()

        self.assertFalse(result.available)
        self.assertIsNone(result.access_token)
        self.assertEqual(result.source, "unconfigured")
        self.assertIn("tenant ID and client ID", result.message)

    def test_silent_cached_token_returns_access_token(self) -> None:
        app = _FakeMsalApp(
            accounts=[{"username": "matthew@example.com"}],
            silent_results=[{"access_token": "cached-token"}],
        )
        provider = GraphAuthProvider(
            _config(),
            environ={},
            app_factory=lambda **kwargs: app,
        )

        result = provider.get_access_token()

        self.assertTrue(result.available)
        self.assertEqual(result.access_token, "cached-token")
        self.assertEqual(result.source, "cache")
        self.assertEqual(app.silent_scopes, [list(_config().outlook_graph_scopes)])

    def test_expired_cached_access_token_can_be_refreshed_silently(self) -> None:
        app = _FakeMsalApp(
            accounts=[{"username": "matthew@example.com"}],
            silent_results=[{"access_token": "refreshed-token", "token_source": "identity_provider"}],
        )
        provider = GraphAuthProvider(_config(), environ={}, app_factory=lambda **kwargs: app)

        result = provider.get_access_token()

        self.assertTrue(result.available)
        self.assertEqual(result.access_token, "refreshed-token")
        self.assertEqual(result.source, "cache")
        self.assertIn("cached or refreshed", result.message)

    def test_silent_lookup_does_not_start_device_code_flow(self) -> None:
        app = _FakeMsalApp(accounts=[{"username": "matthew@example.com"}], silent_results=[{}])
        provider = GraphAuthProvider(_config(), environ={}, app_factory=lambda **kwargs: app)

        result = provider.get_access_token()

        self.assertFalse(result.available)
        self.assertEqual(result.source, "cache_miss")
        self.assertEqual(app.device_flow_count, 0)
        self.assertIn(".venv/bin/obsidian-agent graph login", "\n".join(result.recovery_steps))

    def test_login_prints_device_message_and_returns_account_without_token_material(self) -> None:
        app = _FakeMsalApp(
            accounts=[],
            device_flow={"user_code": "ABC123", "message": "Go to login.example and enter ABC123."},
            device_result={"access_token": "login-token", "account": {"username": "matthew@example.com"}},
        )
        output: list[str] = []
        provider = GraphAuthProvider(
            _config(),
            environ={},
            app_factory=lambda **kwargs: app,
        )

        result = provider.login(output.append)

        self.assertTrue(result.succeeded)
        self.assertEqual(result.username, "matthew@example.com")
        self.assertEqual(output, ["Go to login.example and enter ABC123."])
        self.assertNotIn("login-token", result.message)

    def test_login_persists_changed_msal_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "msal-cache.json"
            fake_cache = _FakeTokenCache()
            app = _FakeMsalApp(
                accounts=[],
                device_flow={"user_code": "ABC123", "message": "Device message."},
                device_result={"access_token": "login-token"},
            )
            provider = GraphAuthProvider(
                _config(cache_path=cache_path),
                environ={},
                app_factory=lambda **kwargs: app,
            )

            with patch("obsidian_intake_agent.graph_auth.msal.SerializableTokenCache", return_value=fake_cache):
                result = provider.login(lambda message: None)

            self.assertTrue(result.succeeded)
            self.assertEqual(cache_path.read_text(encoding="utf-8"), '{"cache": true}')
            self.assertEqual(S_IMODE(cache_path.stat().st_mode), 0o600)

    def test_status_does_not_expose_token_material(self) -> None:
        app = _FakeMsalApp(
            accounts=[{"username": "matthew@example.com"}],
            silent_results=[{"access_token": "cached-token"}],
        )
        provider = GraphAuthProvider(_config(), environ={}, app_factory=lambda **kwargs: app)

        status = provider.status()

        rendered = "\n".join(status.lines())
        self.assertIn("graph_auth_configured: yes", rendered)
        self.assertIn("graph_auth_silent_token: available", rendered)
        self.assertNotIn("cached-token", rendered)

    def test_logout_removes_only_configured_cache_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "msal-cache.json"
            other_path = Path(tmp_dir) / "keep.txt"
            cache_path.write_text("cache", encoding="utf-8")
            other_path.write_text("keep", encoding="utf-8")
            provider = GraphAuthProvider(_config(cache_path=cache_path), environ={})

            result = provider.logout()

            self.assertTrue(result.removed)
            self.assertFalse(cache_path.exists())
            self.assertTrue(other_path.exists())


class _FakeMsalApp:
    def __init__(
        self,
        *,
        accounts: list[dict[str, object]],
        silent_results: list[dict[str, object]] | None = None,
        device_flow: dict[str, object] | None = None,
        device_result: dict[str, object] | None = None,
    ) -> None:
        self._accounts = accounts
        self._silent_results = silent_results or []
        self._device_flow = device_flow or {"user_code": "ABC123", "message": "Device message."}
        self._device_result = device_result or {}
        self.silent_scopes: list[list[str]] = []
        self.device_flow_count = 0

    def get_accounts(self) -> list[dict[str, object]]:
        return self._accounts

    def acquire_token_silent_with_error(self, scopes: list[str], account: dict[str, object]) -> dict[str, object]:
        del account
        self.silent_scopes.append(scopes)
        if self._silent_results:
            return self._silent_results.pop(0)
        return {}

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, object]:
        del scopes
        self.device_flow_count += 1
        return self._device_flow

    def acquire_token_by_device_flow(self, flow: dict[str, object]) -> dict[str, object]:
        del flow
        return self._device_result


class _FakeTokenCache:
    has_state_changed = True

    def deserialize(self, value: str) -> None:
        del value

    def serialize(self) -> str:
        return '{"cache": true}'


def _config(
    *,
    tenant_id: str | None = "tenant-123",
    client_id: str | None = "client-456",
    cache_path: Path | None = None,
) -> Config:
    return Config(
        vault_path=Path("/tmp/vault"),
        intake_dir="00_Intake",
        meetings_dir="01_Meetings",
        actions_dir="07_Actions",
        archive_intake_dir="z_Archive/Intake",
        templates_dir="Templates",
        owner_filter="Matthew",
        outlook_graph_tenant_id=tenant_id,
        outlook_graph_client_id=client_id,
        outlook_graph_token_cache_path=cache_path or Path("/tmp/obsidian-agent-msal-cache.json"),
    )
