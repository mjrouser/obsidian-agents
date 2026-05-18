# Entra Device-Code Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class Microsoft Entra delegated device-code authentication to the local meeting-ingestion CLI so Graph meeting sync can use a cached token, silently refresh it when MSAL can, and notify the operator when manual auth action is required.

**Architecture:** Keep the existing Graph clients token-oriented and add a small `graph_auth` module that owns MSAL public-client setup, token-cache loading/saving, silent token resolution with refresh support, device-code login, status, logout, and manual recovery instructions. CLI wiring resolves a token through env override first, then MSAL silent acquisition; only `graph login` starts the interactive device-code flow, and configured background sync exits nonzero when auth is required so the existing automation failure notification path can alert Matthew.

**Tech Stack:** Python 3.11, `unittest`, MSAL Python, existing `Config`, CLI wiring in `src/obsidian_intake_agent/main.py`, Graph clients in `src/obsidian_intake_agent/meetings/sync.py`, docs in `README.md`, `USAGE.md`, `config.example.yaml`, and `docs/meeting_transcript_automation.md`.

---

## File Structure

- Create: `src/obsidian_intake_agent/graph_auth.py`
  - Owns Entra/MSAL auth config checks, token cache persistence, env override token resolution, silent token lookup with refresh support, auth-required recovery instructions, device-code login, status, and logout.
- Create: `tests/test_graph_auth.py`
  - Uses fake MSAL applications and fake environments to test auth behavior without calling Microsoft.
- Modify: `src/obsidian_intake_agent/config.py`
  - Adds non-secret Entra app settings and safe token-cache default.
- Modify: `src/obsidian_intake_agent/main.py`
  - Adds `graph login/status/logout`, uses `GraphAuthProvider` from meeting client builders, preserves env-token override behavior, and exits loudly for configured auth-required sync failures.
- Modify: `tests/test_config.py`
  - Covers Graph auth config defaults and overrides.
- Modify: `tests/test_main.py`
  - Covers new graph CLI commands and meeting-client builder behavior with cached auth.
- Modify: `config.example.yaml`
  - Documents non-secret Entra settings and the local token cache path.
- Modify: `README.md`
  - Adds auth setup overview and quick-start commands.
- Modify: `USAGE.md`
  - Adds copy-pasteable operator commands and troubleshooting.
- Modify: `docs/meeting_transcript_automation.md`
  - Updates the live validation runbook to use cached device-code auth.
- Modify: `pyproject.toml`
  - Adds `msal` as a runtime dependency.
- Modify: `requirements.lock`
  - Regenerates the lock file after adding `msal`.

## Pre-Flight

- [ ] **Step 1: Start from a clean branch**

Run:

```bash
git status --short
git switch -c codex/entra-device-code-auth
```

Expected: `git status --short` is empty before switching. If it is not empty, stop and inspect the changes before continuing.

- [ ] **Step 2: Read the approved design**

Run:

```bash
sed -n '1,360p' docs/superpowers/specs/2026-05-18-entra-device-code-auth-design.md
```

Expected: design confirms device-code auth, cached tokens, silent refresh, env override compatibility, auth-required notification, docs, and live validation requirements.

## Task 1: Add Graph Auth Configuration

**Files:**
- Modify: `src/obsidian_intake_agent/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Add these tests to `tests/test_config.py` inside `ConfigCompatibilityTests`:

```python
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
```

- [ ] **Step 2: Run focused config tests and verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_config.ConfigCompatibilityTests.test_outlook_graph_auth_defaults_preserve_unconfigured_auth tests.test_config.ConfigCompatibilityTests.test_outlook_graph_auth_settings_can_be_overridden -v
```

Expected: `ERROR` because `Config` does not yet expose the new auth fields.

- [ ] **Step 3: Add config fields and defaults**

In `src/obsidian_intake_agent/config.py`, add this constant near the imports:

```python
DEFAULT_OUTLOOK_GRAPH_SCOPES = (
    "https://graph.microsoft.com/Calendars.Read",
    "https://graph.microsoft.com/OnlineMeetings.Read",
    "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All",
)
DEFAULT_OUTLOOK_GRAPH_TOKEN_CACHE_PATH = Path("~/.cache/obsidian-intake-agent/msal_token_cache.json").expanduser()
```

Add these fields to `Config` after `outlook_graph_access_token_env`:

```python
    outlook_graph_tenant_id: str | None = None
    outlook_graph_client_id: str | None = None
    outlook_graph_scopes: tuple[str, ...] = DEFAULT_OUTLOOK_GRAPH_SCOPES
    outlook_graph_token_cache_path: Path = DEFAULT_OUTLOOK_GRAPH_TOKEN_CACHE_PATH
```

Add these constructor arguments in `Config.load()` after `outlook_graph_access_token_env`:

```python
            outlook_graph_tenant_id=_optional_string(data.get("outlook_graph_tenant_id")),
            outlook_graph_client_id=_optional_string(data.get("outlook_graph_client_id")),
            outlook_graph_scopes=_string_tuple(data.get("outlook_graph_scopes", DEFAULT_OUTLOOK_GRAPH_SCOPES)),
            outlook_graph_token_cache_path=_optional_path(data.get("outlook_graph_token_cache_path"))
            or DEFAULT_OUTLOOK_GRAPH_TOKEN_CACHE_PATH,
```

Add this helper near `_string_list()`:

```python
def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)
```

- [ ] **Step 4: Update example config**

In `config.example.yaml`, add these keys near the existing Graph settings:

```yaml
# Entra public-client auth settings. Tenant/client IDs are not credentials, but
# keep real organization/app values in local config.yaml rather than committing
# them here.
outlook_graph_tenant_id: null
outlook_graph_client_id: null
outlook_graph_scopes:
  - "https://graph.microsoft.com/Calendars.Read"
  - "https://graph.microsoft.com/OnlineMeetings.Read"
  - "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All"
outlook_graph_token_cache_path: "~/.cache/obsidian-intake-agent/msal_token_cache.json"
```

- [ ] **Step 5: Re-run focused config tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_config.ConfigCompatibilityTests.test_outlook_graph_auth_defaults_preserve_unconfigured_auth tests.test_config.ConfigCompatibilityTests.test_outlook_graph_auth_settings_can_be_overridden -v
```

Expected: `OK`.

## Task 2: Add MSAL Dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.lock`

- [ ] **Step 1: Add runtime dependency**

In `pyproject.toml`, add `msal` to `[project].dependencies`:

```toml
dependencies = [
  "pyyaml",
  "watchdog",
  "python-docx",
  "webvtt-py",
  "msal>=1.31,<2",
]
```

- [ ] **Step 2: Regenerate lock file**

Run:

```bash
uv pip compile pyproject.toml --extra audit --extra dev --output-file requirements.lock
```

Expected: `requirements.lock` includes `msal`, its dependency `pyjwt`, and still includes existing project/dev/audit dependencies.

If `uv` is unavailable locally, install or run the project-standard lock-generation tool before continuing. Do not hand-edit the lock file except to resolve a tool-generated conflict.

- [ ] **Step 3: Install updated dependencies into the local venv**

Run:

```bash
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
```

Expected: install succeeds and `python -c "import msal"` works from `.venv`.

## Task 3: Implement Graph Auth Provider With Fakes First

**Files:**
- Create: `tests/test_graph_auth.py`
- Create: `src/obsidian_intake_agent/graph_auth.py`

- [ ] **Step 1: Write failing auth provider tests**

Create `tests/test_graph_auth.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
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
```

- [ ] **Step 2: Run auth tests and verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_graph_auth -v
```

Expected: `ERROR` because `obsidian_intake_agent.graph_auth` does not exist.

- [ ] **Step 3: Implement `graph_auth.py`**

Create `src/obsidian_intake_agent/graph_auth.py`:

```python
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import msal  # type: ignore[import-untyped]

from .config import Config


class MsalPublicClient(Protocol):
    def get_accounts(self) -> list[dict[str, object]]: ...

    def acquire_token_silent_with_error(self, scopes: list[str], account: dict[str, object]) -> dict[str, object] | None: ...

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, object]: ...

    def acquire_token_by_device_flow(self, flow: dict[str, object]) -> dict[str, object] | None: ...


AppFactory = Callable[..., MsalPublicClient]
OutputWriter = Callable[[str], None]

GRAPH_AUTH_RECOVERY_STEPS = (
    ".venv/bin/obsidian-agent graph status",
    ".venv/bin/obsidian-agent graph login",
    ".venv/bin/obsidian-agent graph logout",
    ".venv/bin/obsidian-agent graph login",
)


@dataclass(slots=True, frozen=True)
class GraphTokenResult:
    access_token: str | None
    source: str
    message: str
    recovery_steps: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.access_token is not None


@dataclass(slots=True, frozen=True)
class GraphLoginResult:
    succeeded: bool
    username: str | None
    message: str


@dataclass(slots=True, frozen=True)
class GraphLogoutResult:
    removed: bool
    cache_path: Path
    message: str


@dataclass(slots=True, frozen=True)
class GraphAuthStatus:
    configured: bool
    env_override: bool
    cache_path: Path
    cache_exists: bool
    account_count: int
    silent_token_available: bool
    message: str

    def lines(self) -> list[str]:
        return [
            f"graph_auth_configured: {'yes' if self.configured else 'no'}",
            f"graph_auth_env_override: {'yes' if self.env_override else 'no'}",
            f"graph_auth_cache_path: {self.cache_path}",
            f"graph_auth_cache_exists: {'yes' if self.cache_exists else 'no'}",
            f"graph_auth_cached_accounts: {self.account_count}",
            f"graph_auth_silent_token: {'available' if self.silent_token_available else 'unavailable'}",
            f"graph_auth_status: {self.message}",
        ]


class GraphAuthProvider:
    def __init__(
        self,
        config: Config,
        *,
        environ: Mapping[str, str] | None = None,
        app_factory: AppFactory | None = None,
    ) -> None:
        self._config = config
        self._environ = environ if environ is not None else os.environ
        self._app_factory = app_factory or msal.PublicClientApplication
        self._cache: Any | None = None

    def get_access_token(self) -> GraphTokenResult:
        env_token = self._env_token()
        if env_token is not None:
            return GraphTokenResult(env_token, "environment", "Using Graph access token from environment override.")
        missing = self._missing_config_message()
        if missing is not None:
            return GraphTokenResult(None, "unconfigured", missing)
        app = self._build_app()
        result, _account_count = self._acquire_silent_token(app)
        if result is not None:
            return result
        return GraphTokenResult(
            access_token=None,
            source="cache_miss",
            message="Graph auth is configured, but no cached token is available or refreshable.",
            recovery_steps=GRAPH_AUTH_RECOVERY_STEPS,
        )

    def login(self, output: OutputWriter = print) -> GraphLoginResult:
        missing = self._missing_config_message()
        if missing is not None:
            return GraphLoginResult(False, None, missing)
        app = self._build_app()
        flow = app.initiate_device_flow(scopes=list(self._config.outlook_graph_scopes))
        message = _optional_string(flow.get("message"))
        user_code = _optional_string(flow.get("user_code"))
        if message is None or user_code is None:
            return GraphLoginResult(False, None, "MSAL did not return a valid device-code login flow.")
        output(message)
        token_result = app.acquire_token_by_device_flow(flow) or {}
        access_token = _optional_string(token_result.get("access_token"))
        if access_token is None:
            error = _optional_string(token_result.get("error_description")) or _optional_string(token_result.get("error"))
            return GraphLoginResult(False, None, error or "Graph device-code login did not return an access token.")
        self._save_cache_if_changed()
        account = token_result.get("account")
        username = None
        if isinstance(account, dict):
            username = _optional_string(account.get("username"))
        return GraphLoginResult(True, username, "Graph device-code login succeeded.")

    def status(self) -> GraphAuthStatus:
        env_token = self._env_token()
        missing = self._missing_config_message()
        configured = missing is None
        cache_path = self._config.outlook_graph_token_cache_path
        cache_exists = cache_path.exists()
        if env_token is not None:
            return GraphAuthStatus(configured, True, cache_path, cache_exists, 0, True, "Environment override is set.")
        if missing is not None:
            return GraphAuthStatus(False, False, cache_path, cache_exists, 0, False, missing)
        app = self._build_app()
        result, account_count = self._acquire_silent_token(app)
        return GraphAuthStatus(
            True,
            False,
            cache_path,
            cache_exists,
            account_count,
            result is not None,
            "Cached token is available." if result is not None else "No cached token is available.",
        )

    def logout(self) -> GraphLogoutResult:
        cache_path = self._config.outlook_graph_token_cache_path
        if not cache_path.exists():
            return GraphLogoutResult(False, cache_path, "Graph token cache was already absent.")
        cache_path.unlink()
        return GraphLogoutResult(True, cache_path, "Graph token cache removed.")

    def _env_token(self) -> str | None:
        return _optional_string(self._environ.get(self._config.outlook_graph_access_token_env))

    def _missing_config_message(self) -> str | None:
        if self._config.outlook_graph_tenant_id and self._config.outlook_graph_client_id:
            if self._config.outlook_graph_scopes:
                return None
            return "Graph auth requires at least one configured scope."
        return "Graph auth requires outlook_graph_tenant_id and outlook_graph_client_id."

    def _build_app(self) -> MsalPublicClient:
        assert self._config.outlook_graph_tenant_id is not None
        assert self._config.outlook_graph_client_id is not None
        cache = _load_token_cache(self._config.outlook_graph_token_cache_path)
        self._cache = cache
        return self._app_factory(
            client_id=self._config.outlook_graph_client_id,
            authority=f"https://login.microsoftonline.com/{self._config.outlook_graph_tenant_id}",
            token_cache=cache,
        )

    def _acquire_silent_token(self, app: MsalPublicClient) -> tuple[GraphTokenResult | None, int]:
        accounts = app.get_accounts()
        for account in accounts:
            token_result = app.acquire_token_silent_with_error(list(self._config.outlook_graph_scopes), account=account) or {}
            access_token = _optional_string(token_result.get("access_token"))
            if access_token is not None:
                self._save_cache_if_changed()
                return (
                    GraphTokenResult(access_token, "cache", "Using cached or refreshed Graph access token."),
                    len(accounts),
                )
        return None, len(accounts)

    def _save_cache_if_changed(self) -> None:
        cache = getattr(self, "_cache", None)
        if cache is None or not getattr(cache, "has_state_changed", False):
            return
        cache_path = self._config.outlook_graph_token_cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize(), encoding="utf-8")


def _load_token_cache(cache_path: Path) -> Any:
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    return cache


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
```

- [ ] **Step 4: Run graph auth tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_graph_auth -v
```

Expected: `OK`.

## Task 4: Add Graph CLI Commands

**Files:**
- Modify: `src/obsidian_intake_agent/main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Write failing CLI tests**

Add `GraphCliTests` to `tests/test_main.py`:

```python
class GraphCliTests(unittest.TestCase):
    def test_graph_status_prints_status_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                provider = provider_class.return_value
                provider.status.return_value.lines.return_value = [
                    "graph_auth_configured: yes",
                    "graph_auth_silent_token: available",
                ]

                exit_code = main(["--config", str(config_path), "graph", "status"])

        self.assertEqual(exit_code, 0)
        self.assertIn("graph_auth_configured: yes", stdout.getvalue())
        self.assertIn("graph_auth_silent_token: available", stdout.getvalue())

    def test_graph_login_returns_zero_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                provider = provider_class.return_value
                provider.login.side_effect = lambda output: _FakeLoginResult(True, "matthew@example.com", "ok")

                exit_code = main(["--config", str(config_path), "graph", "login"])

        self.assertEqual(exit_code, 0)
        self.assertIn("graph_login_succeeded: yes", stdout.getvalue())
        self.assertIn("graph_login_account: matthew@example.com", stdout.getvalue())

    def test_graph_login_returns_one_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                provider = provider_class.return_value
                provider.login.side_effect = lambda output: _FakeLoginResult(False, None, "missing config")

                exit_code = main(["--config", str(config_path), "graph", "login"])

        self.assertEqual(exit_code, 1)
        self.assertIn("graph_login_succeeded: no", stdout.getvalue())
        self.assertIn("graph_login_message: missing config", stdout.getvalue())

    def test_graph_logout_prints_removed_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                provider = provider_class.return_value
                provider.logout.return_value = _FakeLogoutResult(True, Path("/tmp/cache.json"), "removed")

                exit_code = main(["--config", str(config_path), "graph", "logout"])

        self.assertEqual(exit_code, 0)
        self.assertIn("graph_logout_removed: yes", stdout.getvalue())
        self.assertIn("graph_logout_cache_path: /tmp/cache.json", stdout.getvalue())


class _FakeLoginResult:
    def __init__(self, succeeded: bool, username: str | None, message: str) -> None:
        self.succeeded = succeeded
        self.username = username
        self.message = message


class _FakeLogoutResult:
    def __init__(self, removed: bool, cache_path: Path, message: str) -> None:
        self.removed = removed
        self.cache_path = cache_path
        self.message = message
```

- [ ] **Step 2: Run new CLI tests and verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_main.GraphCliTests -v
```

Expected: `ERROR` because the parser has no `graph` command and `main.py` does not import `GraphAuthProvider`.

- [ ] **Step 3: Add parser and command handling**

In `src/obsidian_intake_agent/main.py`, import the provider:

```python
from .graph_auth import GraphAuthProvider
```

In `build_parser()`, before the `meetings` parser, add:

```python
    graph_parser = subparsers.add_parser("graph", help="Authenticate and inspect Microsoft Graph access.")
    graph_subparsers = graph_parser.add_subparsers(dest="graph_command", required=True)
    graph_subparsers.add_parser("login", help="Start Microsoft Graph device-code login and cache the token.")
    graph_subparsers.add_parser("status", help="Show Microsoft Graph auth configuration and cache status.")
    graph_subparsers.add_parser("logout", help="Remove the local Microsoft Graph token cache.")
```

In `main()`, before `if args.command == "meetings":`, add:

```python
    if args.command == "graph":
        provider = GraphAuthProvider(config)
        if args.graph_command == "status":
            for line in provider.status().lines():
                print(line)
            return 0
        if args.graph_command == "login":
            login_result = provider.login(print)
            print(f"graph_login_succeeded: {'yes' if login_result.succeeded else 'no'}")
            if login_result.username:
                print(f"graph_login_account: {login_result.username}")
            print(f"graph_login_message: {login_result.message}")
            return 0 if login_result.succeeded else 1
        if args.graph_command == "logout":
            logout_result = provider.logout()
            print(f"graph_logout_removed: {'yes' if logout_result.removed else 'no'}")
            print(f"graph_logout_cache_path: {logout_result.cache_path}")
            print(f"graph_logout_message: {logout_result.message}")
            return 0
```

- [ ] **Step 4: Run graph CLI tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_main.GraphCliTests -v
```

Expected: `OK`.

## Task 5: Wire Cached Auth Into Meeting Client Builders

**Files:**
- Modify: `src/obsidian_intake_agent/main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Write failing builder tests for cached auth**

Add these tests to `MainCliTests` in `tests/test_main.py`:

```python
    def test_build_meeting_discovery_client_uses_cached_graph_auth_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
            ):
                provider_class.return_value.get_access_token.return_value = _FakeTokenResult("cached-token")
                client = _build_meeting_discovery_client(config)

            self.assertIsInstance(client, GraphOutlookMeetingDiscoveryClient)

    def test_build_meeting_discovery_client_falls_back_when_cached_auth_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
            ):
                provider_class.return_value.get_access_token.return_value = _FakeTokenResult(None)
                client = _build_meeting_discovery_client(config)

            self.assertIsInstance(client, UnconfiguredOutlookMeetingDiscoveryClient)

    def test_build_meeting_artifact_discovery_client_uses_cached_graph_auth_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)
            config = Config.load(config_path)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
            ):
                provider_class.return_value.get_access_token.return_value = _FakeTokenResult("cached-token")
                client = _build_meeting_artifact_discovery_client(config)

            self.assertIsInstance(client, ChainedMeetingArtifactDiscoveryClient)


class _FakeTokenResult:
    def __init__(
        self,
        access_token: str | None,
        *,
        source: str = "cache",
        message: str = "fake token result",
        recovery_steps: tuple[str, ...] = (),
    ) -> None:
        self.access_token = access_token
        self.available = access_token is not None
        self.source = source
        self.message = message
        self.recovery_steps = recovery_steps
```

Add this test to `MainCliTests`:

```python
    def test_meetings_sync_transcripts_exits_nonzero_when_configured_auth_requires_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            vault.mkdir(parents=True)
            config_path = _write_config(repo, vault)

            with (
                patch("obsidian_intake_agent.main.GraphAuthProvider") as provider_class,
                patch("obsidian_intake_agent.main.build_transcript_sync_plan") as sync_plan_mock,
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                provider_class.return_value.get_access_token.return_value = _FakeTokenResult(
                    None,
                    source="cache_miss",
                    message="Graph auth is configured, but no cached token is available or refreshable.",
                    recovery_steps=(
                        ".venv/bin/obsidian-agent graph status",
                        ".venv/bin/obsidian-agent graph login",
                    ),
                )

                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "meetings",
                        "sync-transcripts",
                        "--since",
                        "2026-05-01",
                        "--dry-run",
                    ]
                )

            self.assertEqual(exit_code, 2)
            sync_plan_mock.assert_not_called()
            self.assertIn("graph_auth_required: yes", stderr.getvalue())
            self.assertIn(".venv/bin/obsidian-agent graph login", stderr.getvalue())
```

- [ ] **Step 2: Run focused builder tests and verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_main.MainCliTests.test_build_meeting_discovery_client_uses_cached_graph_auth_when_available tests.test_main.MainCliTests.test_build_meeting_discovery_client_falls_back_when_cached_auth_unavailable tests.test_main.MainCliTests.test_build_meeting_artifact_discovery_client_uses_cached_graph_auth_when_available tests.test_main.MainCliTests.test_meetings_sync_transcripts_exits_nonzero_when_configured_auth_requires_login -v
```

Expected: `FAIL` because builders still read only the environment directly and meeting sync does not yet fail loudly for configured auth-required states.

- [ ] **Step 3: Replace direct env reads with shared resolver**

In `src/obsidian_intake_agent/main.py`, add:

```python
def _resolve_graph_access_token_result(config: Config):
    return GraphAuthProvider(config).get_access_token()


def _resolve_graph_access_token(config: Config) -> str | None:
    return _resolve_graph_access_token_result(config).access_token


def _graph_auth_requires_manual_action(token_result: object) -> bool:
    return getattr(token_result, "source", "") == "cache_miss" and not getattr(token_result, "available", False)


def _print_graph_auth_required(token_result: object) -> None:
    print("graph_auth_required: yes", file=sys.stderr)
    print(f"graph_auth_message: {getattr(token_result, 'message', 'Graph auth requires manual action.')}", file=sys.stderr)
    recovery_steps = tuple(getattr(token_result, "recovery_steps", ()))
    if recovery_steps:
        print("graph_auth_recovery_steps:", file=sys.stderr)
        for step in recovery_steps:
            print(f"  {step}", file=sys.stderr)
```

Inside the `meetings sync-transcripts` branch, before building the sync plan, add:

```python
            token_result = _resolve_graph_access_token_result(config)
            if _graph_auth_requires_manual_action(token_result):
                _print_graph_auth_required(token_result)
                return 2
```

Change `_build_meeting_discovery_client()`:

```python
def _build_meeting_discovery_client(
    config: Config,
) -> GraphOutlookMeetingDiscoveryClient | UnconfiguredOutlookMeetingDiscoveryClient:
    token = _resolve_graph_access_token(config)
    if not token:
        return UnconfiguredOutlookMeetingDiscoveryClient()
    return GraphOutlookMeetingDiscoveryClient(
        access_token=token,
        api_base_url=config.outlook_graph_api_base_url,
    )
```

Change `_build_meeting_artifact_discovery_client()`:

```python
    token = _resolve_graph_access_token(config)
    if not token:
        return local_client
```

Remove the now-unused `import os` from `main.py` if no remaining code uses it.

- [ ] **Step 4: Run existing and new builder tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_main.MainCliTests.test_build_meeting_discovery_client_uses_graph_when_token_present tests.test_main.MainCliTests.test_build_meeting_discovery_client_falls_back_without_token tests.test_main.MainCliTests.test_build_meeting_artifact_discovery_client_uses_local_only_without_token tests.test_main.MainCliTests.test_build_meeting_artifact_discovery_client_chains_graph_and_local_with_token tests.test_main.MainCliTests.test_build_meeting_artifact_discovery_client_can_download_transcripts_with_token tests.test_main.MainCliTests.test_build_meeting_discovery_client_uses_cached_graph_auth_when_available tests.test_main.MainCliTests.test_build_meeting_discovery_client_falls_back_when_cached_auth_unavailable tests.test_main.MainCliTests.test_build_meeting_artifact_discovery_client_uses_cached_graph_auth_when_available -v
```

Expected: `OK`.

## Task 6: Document Auth Setup And Operator Workflow

**Files:**
- Modify: `README.md`
- Modify: `USAGE.md`
- Modify: `docs/meeting_transcript_automation.md`
- Modify: `config.example.yaml`

- [ ] **Step 1: Update README setup and config docs**

In `README.md`, extend the default config block with:

```yaml
outlook_graph_tenant_id: null
outlook_graph_client_id: null
outlook_graph_scopes:
  - "https://graph.microsoft.com/Calendars.Read"
  - "https://graph.microsoft.com/OnlineMeetings.Read"
  - "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All"
outlook_graph_token_cache_path: "~/.cache/obsidian-intake-agent/msal_token_cache.json"
```

Replace the old bearer-token-only note with:

```markdown
For Microsoft Graph meeting sync, configure the approved Entra app values in
local `config.yaml`:

```yaml
outlook_graph_tenant_id: "<tenant-id>"
outlook_graph_client_id: "<public-client-app-id>"
```

Then authenticate once with device-code login:

```bash
.venv/bin/obsidian-agent graph login
.venv/bin/obsidian-agent graph status
```

The CLI stores delegated auth state in `outlook_graph_token_cache_path`. The
tenant ID and client ID are not credentials; the token cache is local machine state
and must not be committed. Keep real tenant and client IDs in local ignored
`config.yaml`; committed files should use `null` or placeholders. When cached
auth expires, MSAL attempts silent refresh automatically. If refresh is not
possible, the CLI prints recovery commands and background automation triggers
the existing action-needed failure note and desktop notification path.
`OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN` still overrides cached auth for debugging or
emergency manual validation.
```

- [ ] **Step 2: Update USAGE operator commands**

In `USAGE.md`, add a Microsoft Graph auth section before the meeting sync commands:

```markdown
## Microsoft Graph Auth

Configure local `config.yaml` with the approved Entra public-client app:

```yaml
outlook_graph_tenant_id: "<tenant-id>"
outlook_graph_client_id: "<client-id>"
```

Authenticate:

```bash
.venv/bin/obsidian-agent graph login
.venv/bin/obsidian-agent graph status
```

Reset local cached auth:

```bash
.venv/bin/obsidian-agent graph logout
```

Manual bearer-token override, mostly for debugging:

```bash
export OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN="..."
```

When the env override is set, it wins over cached device-code auth.
```

Add a troubleshooting subsection:

```markdown
### Graph Troubleshooting

- `graph_auth_configured: no`: add `outlook_graph_tenant_id` and
  `outlook_graph_client_id` to local `config.yaml`.
- `graph_auth_silent_token: unavailable`: run
  `.venv/bin/obsidian-agent graph login`.
- `graph_auth_required: yes` from a background run: open
  `_System/Agent Errors/ACTION NEEDED - Latest Automation Failure.md`, then run
  the recovery commands shown in that note.
- If login still fails after `graph login`, reset the local cache with
  `.venv/bin/obsidian-agent graph logout`, then run
  `.venv/bin/obsidian-agent graph login` again.
- Calendar warnings mentioning `Calendars.Read`: confirm the approved app has
  delegated calendar consent.
- Transcript `permission_blocked`: confirm delegated transcript consent for
  `OnlineMeetingTranscript.Read.All`.
- Recap or chat `permission_blocked`: record the permission block from the
  dry-run output and validate whether the tenant grants those endpoints.
```

- [ ] **Step 3: Update meeting automation runbook**

In `docs/meeting_transcript_automation.md`, replace any manual token setup with this flow:

```markdown
### Auth Check

Before a live Graph validation run:

```bash
.venv/bin/obsidian-agent graph status
.venv/bin/obsidian-agent graph login
.venv/bin/obsidian-agent graph status
```

Expected:

```text
graph_auth_configured: yes
graph_auth_silent_token: available
```

If background automation needs manual auth, it exits nonzero so the wrapper
writes `_System/Agent Errors/ACTION NEEDED - Latest Automation Failure.md` and
sends the configured desktop notification. Follow the recovery commands in that
note before rerunning meeting sync.

Then run the validation sequence:

```bash
.venv/bin/obsidian-agent meetings sync-transcripts --since 2026-05-01 --dry-run
.venv/bin/obsidian-agent meetings sync-transcripts --since 2026-05-01 --write-bundles
.venv/bin/obsidian-agent meetings process-bundles --dry-run
.venv/bin/obsidian-agent meetings process-bundles --execute --validation
```
```

- [ ] **Step 4: Re-run doc grep for stale bearer-token-only guidance**

Run:

```bash
rg -n "OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN|bearer token|graph login|graph status|graph logout" README.md USAGE.md docs/meeting_transcript_automation.md config.example.yaml
```

Expected: env-token references remain only as override/debugging guidance; primary workflow is `graph login`.

## Task 7: Run Focused And Full Verification

**Files:**
- No code changes unless verification finds an issue.

- [ ] **Step 1: Run focused tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_config tests.test_graph_auth tests.test_main tests.test_meeting_sync -v
```

Expected: `OK`.

- [ ] **Step 2: Run standard checks**

Run:

```bash
make check
make test
make smoke
make build
```

Expected: all pass.

- [ ] **Step 3: Run dependency audit**

Because this plan adds `msal`, run:

```bash
make audit
```

Expected: pass. If audit reports a vulnerability introduced by `msal` or a transitive dependency, stop and evaluate whether to pin a patched version or choose a different auth implementation.

- [ ] **Step 4: Review diff for secrets and local paths**

Run:

```bash
git diff --stat
git diff -- pyproject.toml requirements.lock config.example.yaml README.md USAGE.md docs/meeting_transcript_automation.md src/obsidian_intake_agent/config.py src/obsidian_intake_agent/graph_auth.py src/obsidian_intake_agent/main.py tests/test_config.py tests/test_graph_auth.py tests/test_main.py
```

Expected:

- no tenant-specific values
- no client secrets
- no access tokens or refresh tokens
- no Matthew-specific live token cache path
- docs show placeholder tenant/client values only

## Task 8: Live Validation With Approved Entra App

**Files:**
- No project code changes expected.
- Local `config.yaml` changes are allowed but remain untracked.

- [ ] **Step 1: Configure local Entra app values**

Edit local `config.yaml`, not `config.example.yaml`:

```yaml
outlook_graph_tenant_id: "<tenant-id-from-approved-app>"
outlook_graph_client_id: "<client-id-from-approved-app>"
outlook_graph_token_cache_path: "~/.cache/obsidian-intake-agent/msal_token_cache.json"
```

Run:

```bash
git status --short config.yaml
```

Expected: no tracked project diff for `config.yaml`.

- [ ] **Step 2: Authenticate**

Run:

```bash
.venv/bin/obsidian-agent graph status
.venv/bin/obsidian-agent graph login
.venv/bin/obsidian-agent graph status
```

Expected after login:

```text
graph_auth_configured: yes
graph_auth_silent_token: available
```

The output must not print access tokens or refresh tokens.

- [ ] **Step 3: Run Graph discovery dry run**

Run:

```bash
.venv/bin/obsidian-agent meetings sync-transcripts --since 2026-05-01 --dry-run
```

Expected:

- provider is `graph_outlook_calendar`
- candidates are discovered or a clear Graph warning is printed
- permission blocks include actionable Graph details
- output includes source status counts for transcripts, recap, and chat

- [ ] **Step 4: Stage bundle notes if dry-run output looks sane**

Run:

```bash
.venv/bin/obsidian-agent meetings sync-transcripts --since 2026-05-01 --write-bundles
```

Expected: bundle notes and Outlook metadata sidecars are written under the vault `00_Intake/bundles` lane, with no project repo changes.

- [ ] **Step 5: Validate processing lane**

Run:

```bash
.venv/bin/obsidian-agent meetings process-bundles --dry-run
.venv/bin/obsidian-agent meetings process-bundles --execute --validation
```

Expected:

- ready bundles process into validation outputs
- canonical meeting notes land under `99_Test Notes/Meetings`
- Matthew-owned actions land under `99_Test Notes/Actions`
- production `01_Meetings` and `07_Actions` are not touched by validation execution

## Task 9: Manual Commit And Merge Instructions

**Files:**
- All changed implementation, test, docs, dependency files.

- [ ] **Step 1: Final status check**

Run:

```bash
git status --short
```

Expected: only intentional files from this plan are modified.

- [ ] **Step 2: Matthew reviews and commits manually**

After checks pass and the diff is reviewed, Matthew runs:

```bash
git add pyproject.toml requirements.lock config.example.yaml README.md USAGE.md docs/meeting_transcript_automation.md src/obsidian_intake_agent/config.py src/obsidian_intake_agent/graph_auth.py src/obsidian_intake_agent/main.py tests/test_config.py tests/test_graph_auth.py tests/test_main.py
git commit -m "feat: add Entra device-code Graph auth"
```

- [ ] **Step 3: Merge manually when ready**

If the work is on `codex/entra-device-code-auth` and should be merged locally:

```bash
git switch main
git merge --no-ff codex/entra-device-code-auth
```

If the repo workflow prefers a pull request, push and open a PR instead:

```bash
git push -u origin codex/entra-device-code-auth
```

## Self-Review Checklist

- [ ] The plan preserves existing Graph API client boundaries.
- [ ] The plan preserves `OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN` as an override.
- [ ] Only `graph login` starts device-code login.
- [ ] Normal meeting sync does not block for interactive auth.
- [ ] Token material is never printed or committed.
- [ ] Docs include setup, status, logout, validation, and troubleshooting.
- [ ] Dependency changes require both lockfile update and `make audit`.
