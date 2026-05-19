from __future__ import annotations

from pathlib import Path


def validate_vault_path(vault_path: Path, path: Path, *, label: str) -> Path:
    resolved_vault = vault_path.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_vault and resolved_vault not in resolved_path.parents:
        raise ValueError(f"{label} is outside the vault: {path}")
    return resolved_path


def vault_relative_path(vault_path: Path, path: Path, *, label: str = "web clip path") -> str:
    return validate_vault_path(vault_path, path, label=label).relative_to(vault_path.resolve()).as_posix()
