from __future__ import annotations

from pathlib import Path
import tempfile


def safe_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as tmp:
        tmp.write(content)
        temp_name = tmp.name
    Path(temp_name).replace(path)


def prepend_status_marker(path: Path, status_text: str) -> None:
    original = path.read_text(encoding="utf-8")
    marker = f"STATUS: {status_text}"
    if original.startswith(marker):
        return
    updated = f"{marker}\n{original}"
    safe_write_text(path, updated)


def replace_status_marker(path: Path, status_text: str) -> None:
    original = path.read_text(encoding="utf-8")
    marker = f"STATUS: {status_text}"
    lines = original.splitlines()

    while lines and lines[0].startswith("STATUS: PROCESSED"):
        lines.pop(0)

    remainder = "\n".join(lines)
    if original.endswith("\n") and remainder:
        remainder = f"{remainder}\n"

    updated = f"{marker}\n{remainder}" if remainder else f"{marker}\n"
    safe_write_text(path, updated)


def safe_move_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Archive destination already exists: {destination}")
    source.replace(destination)
