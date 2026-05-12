from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Config:
    vault_path: Path
    intake_dir: str
    meetings_dir: str
    actions_dir: str
    archive_intake_dir: str
    templates_dir: str
    owner_filter: str
    validation_meetings_dir: str = "99_Test Notes/Meetings"
    validation_actions_dir: str = "99_Test Notes/Actions"
    dry_run: bool = True
    include_unassigned: bool = False
    llm_provider: str = "codex_cli"
    codex_model: str | None = None
    codex_exec_cmd: list[str] | None = None
    codex_timeout_seconds: int | None = 300
    extraction_mode: str = "draft"
    action_owner_aliases: dict[str, list[str]] | None = None
    git_auto_commit_vault: bool = False
    git_auto_commit_project: bool = False
    git_vault_repo_path: Path | None = None
    git_project_repo_path: Path | None = None
    weekly_reviews_dir: str = "09_Weekly Reviews"
    actions_archive_dir: str = "Actions Archive"
    weekly_reviews_archive_dir: str = "Review & Wrap Archive"
    archive_retention_count: int = 4
    watcher_settle_seconds: int = 5
    watcher_stable_seconds: int = 2
    automation_log_dir: str = "logs"
    automation_error_dir: str = "_System/Agent Errors"
    outlook_graph_access_token_env: str = "OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN"
    outlook_graph_api_base_url: str = "https://graph.microsoft.com/v1.0"

    @classmethod
    def load(cls, path: Path) -> Config:
        data = _load_config_data(path)
        vault_path = Path(_required_string(data, "vault_path")).expanduser()
        return cls(
            vault_path=vault_path,
            intake_dir=_required_string(data, "intake_dir"),
            meetings_dir=_required_string(data, "meetings_dir"),
            actions_dir=_required_string(data, "actions_dir"),
            validation_meetings_dir=str(data.get("validation_meetings_dir", "99_Test Notes/Meetings")),
            validation_actions_dir=str(data.get("validation_actions_dir", "99_Test Notes/Actions")),
            archive_intake_dir=_required_string(data, "archive_intake_dir"),
            templates_dir=_required_string(data, "templates_dir"),
            owner_filter=_required_string(data, "owner_filter"),
            dry_run=bool(data.get("dry_run", True)),
            include_unassigned=bool(data.get("include_unassigned", False)),
            llm_provider=str(data.get("llm_provider", "codex_cli")),
            codex_model=_optional_string(data.get("codex_model")),
            codex_exec_cmd=_string_list(data.get("codex_exec_cmd", ["codex", "exec"])),
            codex_timeout_seconds=_optional_positive_int(data.get("codex_timeout_seconds", 300)),
            extraction_mode=str(data.get("extraction_mode", "draft")),
            action_owner_aliases=_string_list_map(data.get("action_owner_aliases", {})),
            git_auto_commit_vault=bool(data.get("git_auto_commit_vault", False)),
            git_auto_commit_project=bool(data.get("git_auto_commit_project", False)),
            git_vault_repo_path=_optional_path(data.get("git_vault_repo_path")) or vault_path,
            git_project_repo_path=_optional_path(data.get("git_project_repo_path")) or path.resolve().parent,
            weekly_reviews_dir=str(data.get("weekly_reviews_dir") or data.get("snapshots_dir") or "09_Weekly Reviews"),
            actions_archive_dir=str(data.get("actions_archive_dir", "Actions Archive")),
            weekly_reviews_archive_dir=str(data.get("weekly_reviews_archive_dir", "Review & Wrap Archive")),
            archive_retention_count=_positive_int(data.get("archive_retention_count", 4), "archive_retention_count"),
            watcher_settle_seconds=_positive_int(data.get("watcher_settle_seconds", 5), "watcher_settle_seconds"),
            watcher_stable_seconds=_positive_int(data.get("watcher_stable_seconds", 2), "watcher_stable_seconds"),
            automation_log_dir=str(data.get("automation_log_dir", "logs")),
            automation_error_dir=str(data.get("automation_error_dir", "_System/Agent Errors")),
            outlook_graph_access_token_env=str(
                data.get("outlook_graph_access_token_env", "OBSIDIAN_AGENT_GRAPH_ACCESS_TOKEN")
            ),
            outlook_graph_api_base_url=str(data.get("outlook_graph_api_base_url", "https://graph.microsoft.com/v1.0")),
        )


def _load_config_data(path: Path) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        yaml_data = yaml.safe_load(raw)
        if not isinstance(yaml_data, dict):
            raise ValueError("Config must be a mapping.")
        return _string_key_mapping(yaml_data)
    except ModuleNotFoundError:
        fallback_data: dict[str, object] = {}
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, _, value = stripped.partition(":")
            if not _:
                continue
            normalized = value.strip().strip('"').strip("'")
            if normalized.lower() == "true":
                parsed: object = True
            elif normalized.lower() == "false":
                parsed = False
            elif normalized.lower() in {"null", "none"}:
                parsed = None
            elif normalized.startswith("[") and normalized.endswith("]"):
                parsed = ast.literal_eval(normalized)
            else:
                parsed = normalized
            fallback_data[key.strip()] = parsed
        return fallback_data


def _string_key_mapping(value: dict[Any, Any]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}


def _required_string(data: dict[str, object], key: str) -> str:
    value = data[key]
    return str(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_path(value: object) -> Path | None:
    text = _optional_string(value)
    if text is None:
        return None
    return Path(text).expanduser()


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    parsed = _parse_int(value, error_message="codex_timeout_seconds must be a positive integer or null.")
    if parsed <= 0:
        raise ValueError("codex_timeout_seconds must be a positive integer or null.")
    return parsed


def _positive_int(value: object, key: str) -> int:
    error_message = f"{key} must be a positive integer."
    parsed = _parse_int(value, error_message=error_message)
    if parsed <= 0:
        raise ValueError(error_message)
    return parsed


def _parse_int(value: object, *, error_message: str) -> int:
    try:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(error_message) from exc


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _string_list_map(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, aliases in value.items():
        if isinstance(aliases, list):
            result[str(key)] = [str(alias) for alias in aliases]
    return result
