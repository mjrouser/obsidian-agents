from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import msal  # type: ignore[import-untyped]

from .config import Config

GRAPH_AUTH_RECOVERY_STEPS = (
    ".venv/bin/obsidian-agent graph status",
    ".venv/bin/obsidian-agent graph login",
    ".venv/bin/obsidian-agent graph logout",
    ".venv/bin/obsidian-agent graph login",
)


class MsalPublicClient(Protocol):
    def get_accounts(self) -> list[dict[str, object]]: ...

    def acquire_token_silent_with_error(
        self,
        scopes: list[str],
        account: dict[str, object],
    ) -> dict[str, object] | None: ...

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, object]: ...

    def acquire_token_by_device_flow(self, flow: dict[str, object]) -> dict[str, object] | None: ...


AppFactory = Callable[..., MsalPublicClient]
OutputWriter = Callable[[str], None]


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
            error = _optional_string(token_result.get("error_description")) or _optional_string(
                token_result.get("error")
            )
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
        return "Graph auth requires tenant ID and client ID in outlook_graph_tenant_id and outlook_graph_client_id."

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
            token_result = (
                app.acquire_token_silent_with_error(list(self._config.outlook_graph_scopes), account=account) or {}
            )
            access_token = _optional_string(token_result.get("access_token"))
            if access_token is not None:
                self._save_cache_if_changed()
                return (
                    GraphTokenResult(access_token, "cache", "Using cached or refreshed Graph access token."),
                    len(accounts),
                )
        return None, len(accounts)

    def _save_cache_if_changed(self) -> None:
        cache = self._cache
        if cache is None or not getattr(cache, "has_state_changed", False):
            return
        cache_path = self._config.outlook_graph_token_cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize(), encoding="utf-8")
        cache_path.chmod(0o600)


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
