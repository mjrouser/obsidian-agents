from __future__ import annotations

from pathlib import Path
import re


def read_vtt(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "WEBVTT":
            continue
        if "-->" in stripped:
            continue
        if re.fullmatch(r"\d+", stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines)
