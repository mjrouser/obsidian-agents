from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Config:
    vault_path: Path
    intake_dir: str
    meetings_dir: str
    actions_dir: str
    archive_intake_dir: str
    templates_dir: str
    owner_filter: str
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
    watcher_settle_seconds: int = 5
    watcher_stable_seconds: int = 2
    automation_log_dir: str = "logs"
    automation_error_dir: str = "_System/Agent Errors"

    @classmethod
    def load(cls, path: Path) -> Config:
        data = _load_config_data(path)
        vault_path = Path(data["vault_path"]).expanduser()
        return cls(
            vault_path=vault_path,
            intake_dir=data["intake_dir"],
            meetings_dir=data["meetings_dir"],
            actions_dir=data["actions_dir"],
            archive_intake_dir=data["archive_intake_dir"],
            templates_dir=data["templates_dir"],
            owner_filter=data["owner_filter"],
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
            watcher_settle_seconds=int(data.get("watcher_settle_seconds", 5)),
            watcher_stable_seconds=int(data.get("watcher_stable_seconds", 2)),
            automation_log_dir=str(data.get("automation_log_dir", "logs")),
            automation_error_dir=str(data.get("automation_error_dir", "_System/Agent Errors")),
        )


def _load_config_data(path: Path) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError("Config must be a mapping.")
        return data
    except ModuleNotFoundError:
        data: dict[str, object] = {}
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
            data[key.strip()] = parsed
        return data


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
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("codex_timeout_seconds must be a positive integer or null.") from exc
    if parsed <= 0:
        raise ValueError("codex_timeout_seconds must be a positive integer or null.")
    return parsed


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
