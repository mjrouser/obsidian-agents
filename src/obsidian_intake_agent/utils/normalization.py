from __future__ import annotations

import re


def normalize_owner_name(owner: str | None, aliases: dict[str, list[str]]) -> str | None:
    if owner is None:
        return None

    normalized_owner = owner.strip()
    if not normalized_owner:
        return normalized_owner

    owner_key = normalized_owner.casefold()
    for canonical, alias_values in aliases.items():
        if owner_key == canonical.strip().casefold():
            return canonical
        for alias in alias_values:
            if owner_key == alias.strip().casefold():
                return canonical

    return normalized_owner


def normalize_owner(owner: str | None, aliases: dict[str, list[str]]) -> str | None:
    normalized = normalize_owner_name(owner, aliases)
    if normalized is None:
        return None
    cleaned = normalized.strip()
    if not cleaned or cleaned.casefold() in {"null", "none", "unknown"}:
        return None
    return cleaned


def normalize_free_text(value: str) -> str:
    compact = re.sub(r"\s+", " ", value.strip())
    compact = compact.replace("—", "-").replace("–", "-")
    compact = re.sub(r"\s*-\s*", "-", compact)
    compact = compact.rstrip(".").strip()
    return compact.casefold()
