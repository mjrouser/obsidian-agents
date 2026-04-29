from __future__ import annotations

from pathlib import Path

from obsidian_intake_agent.config import Config


def config(
    vault: Path,
    *,
    dry_run: bool,
    include_unassigned: bool = False,
    llm_provider: str = "codex_cli",
    action_owner_aliases: dict[str, list[str]] | None = None,
) -> Config:
    return Config(
        vault_path=vault,
        intake_dir="00_Intake",
        meetings_dir="01_Meetings",
        actions_dir="07_Actions",
        archive_intake_dir="_Archive/Intake",
        templates_dir="Templates",
        owner_filter="Matthew",
        dry_run=dry_run,
        include_unassigned=include_unassigned,
        llm_provider=llm_provider,
        action_owner_aliases=action_owner_aliases
        or {
            "Matthew Rouser": [
                "Matthew Rouser",
                "Matthew",
                "Matt Rouser",
                "Matt",
                "matthew.rouser",
            ]
        },
    )
