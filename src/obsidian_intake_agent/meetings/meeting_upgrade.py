from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_ACTION_LINE_PATTERN = re.compile(rb"^(?P<prefix>[ \t]*-[ \t]*\[(?P<state>[ xX])\][ \t]*)(?P<body>.*)$")
_SOURCE_LINK_PATTERN = re.compile(rb"(?i:\bSource:)[ \t]*\d{4}-\d{2}-\d{2}[ \t]*\[\[(?P<target>[^\]\r\n]+)\]\]")


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    content: bytes | None


def sha256_path(path: Path) -> str | None:
    """Return a regular file's SHA-256, or ``None`` when it cannot be read safely."""
    try:
        content = _read_regular_file(path)
    except (OSError, ValueError):
        return None
    return hashlib.sha256(content).hexdigest()


def canonical_matches_hash(path: Path, expected_sha256: str | None) -> bool:
    if not isinstance(expected_sha256, str) or _SHA256_PATTERN.fullmatch(expected_sha256) is None:
        return False
    actual_sha256 = sha256_path(path)
    if actual_sha256 is None:
        return False
    return hmac.compare_digest(actual_sha256.casefold(), expected_sha256.casefold())


def archive_canonical_note(
    canonical_path: Path,
    archive_root: Path,
    *,
    expected_sha256: str,
) -> Path:
    """Archive exact canonical bytes without overwriting an existing archive."""
    if _SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise ValueError("meeting upgrade archive requires a valid expected SHA-256")
    content = _read_regular_file(canonical_path)
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256.casefold()):
        raise ValueError("meeting upgrade canonical note no longer matches its expected SHA-256")
    common_root = Path(
        os.path.commonpath(
            (
                canonical_path.absolute(),
                archive_root.absolute(),
            )
        )
    )
    if common_root == Path(common_root.anchor):
        raise ValueError("meeting upgrade archive must share a trusted non-root ancestor with the canonical note")
    _validate_existing_directory_components(canonical_path.absolute().parent)
    _ensure_safe_directory(archive_root)

    digest = actual_sha256
    candidates = (
        archive_root / canonical_path.name,
        archive_root / f"{canonical_path.stem}.{digest[:12]}{canonical_path.suffix}",
    )
    for candidate in candidates:
        if candidate.is_symlink():
            raise ValueError("meeting upgrade archive destination must not be a symlink")
        if candidate.exists():
            if not candidate.is_file():
                raise ValueError("meeting upgrade archive destination must be a regular file")
            if _read_regular_file(candidate) == content:
                return candidate
            continue
        _atomic_create_bytes(candidate, content)
        return candidate
    raise ValueError("meeting upgrade archive collision could not be resolved safely")


def migrate_action_backlinks(
    content: bytes,
    *,
    old_note: Path,
    clean_note: Path,
) -> bytes:
    """Migrate only action Source links that exactly identify the old meeting note."""
    old_qualified = old_note.as_posix()
    clean_qualified = clean_note.as_posix()
    old_unqualified = old_note.name
    clean_unqualified = clean_note.name
    migrated_lines: list[bytes] = []
    candidate_indexes: dict[bytes, int] = {}

    for line in content.splitlines(keepends=True):
        line_body, ending = _split_line_ending(line)
        action_match = _ACTION_LINE_PATTERN.match(line_body)
        if action_match is None:
            migrated_lines.append(line)
            continue
        action_body = action_match.group("body")
        source_match = _SOURCE_LINK_PATTERN.search(action_body)
        if source_match is None:
            migrated_lines.append(line)
            continue
        target = source_match.group("target").decode("utf-8")
        migrated_target = _migrated_wikilink_target(
            target,
            old_qualified=old_qualified,
            clean_qualified=clean_qualified,
            old_unqualified=old_unqualified,
            clean_unqualified=clean_unqualified,
        )
        is_clean_target = _wikilink_targets_note(
            target,
            qualified=clean_qualified,
            unqualified=clean_unqualified,
        )
        if migrated_target is None and not is_clean_target:
            migrated_lines.append(line)
            continue
        effective_target = migrated_target or target
        migrated_action_body = (
            action_body[: source_match.start("target")]
            + effective_target.encode("utf-8")
            + action_body[source_match.end("target") :]
        )
        migrated_body = action_match.group("prefix") + migrated_action_body
        migrated_line = migrated_body + ending
        key = _action_semantic_key(migrated_body)
        existing_index = candidate_indexes.get(key)
        if existing_index is None:
            candidate_indexes[key] = len(migrated_lines)
            migrated_lines.append(migrated_line)
            continue
        migrated_lines[existing_index] = _prefer_completed_action(
            migrated_lines[existing_index],
            migrated_line,
        )

    return b"".join(migrated_lines)


def capture_file_snapshots(paths: tuple[Path, ...]) -> tuple[FileSnapshot, ...]:
    snapshots: list[FileSnapshot] = []
    for path in paths:
        if path.is_symlink():
            raise ValueError("meeting upgrade output must not be a symlink")
        if not path.exists():
            snapshots.append(FileSnapshot(path=path, content=None))
            continue
        snapshots.append(FileSnapshot(path=path, content=_read_regular_file(path)))
    return tuple(snapshots)


def restore_file_snapshots(snapshots: tuple[FileSnapshot, ...]) -> None:
    errors: list[str] = []
    for snapshot in snapshots:
        try:
            if snapshot.path.is_symlink():
                raise ValueError("meeting upgrade restore target must not be a symlink")
            if snapshot.content is None:
                if snapshot.path.exists():
                    if not snapshot.path.is_file():
                        raise ValueError("meeting upgrade restore target must be a regular file")
                    snapshot.path.unlink()
                    _fsync_parent_directory(snapshot.path.parent)
                continue
            current = _read_regular_file(snapshot.path) if snapshot.path.exists() else None
            if current != snapshot.content:
                _atomic_replace_bytes(snapshot.path, snapshot.content)
        except (OSError, ValueError) as exc:
            errors.append(f"{snapshot.path}: {exc}")
    if errors:
        raise ValueError("meeting upgrade restoration failed: " + "; ".join(errors))


def _migrated_wikilink_target(
    target: str,
    *,
    old_qualified: str,
    clean_qualified: str,
    old_unqualified: str,
    clean_unqualified: str,
) -> str | None:
    path_target, suffix = _split_wikilink_target(target)
    normalized_target = path_target.replace("\\", "/")
    qualified_variants = {old_qualified, _without_md(old_qualified)}
    unqualified_variants = {old_unqualified, _without_md(old_unqualified)}
    if normalized_target in qualified_variants:
        return clean_qualified + suffix
    if "/" not in normalized_target and normalized_target in unqualified_variants:
        return clean_unqualified + suffix
    return None


def _split_wikilink_target(target: str) -> tuple[str, str]:
    indexes = [index for separator in ("#", "|") if (index := target.find(separator)) >= 0]
    if not indexes:
        return target, ""
    split_at = min(indexes)
    return target[:split_at], target[split_at:]


def _wikilink_targets_note(target: str, *, qualified: str, unqualified: str) -> bool:
    path_target, _suffix = _split_wikilink_target(target)
    normalized_target = path_target.replace("\\", "/")
    return normalized_target in {
        qualified,
        _without_md(qualified),
        unqualified,
        _without_md(unqualified),
    }


def _without_md(value: str) -> str:
    return value[:-3] if value.casefold().endswith(".md") else value


def _action_semantic_key(line: bytes) -> bytes:
    action_match = _ACTION_LINE_PATTERN.match(line)
    if action_match is None:
        return line
    return b" ".join(action_match.group("body").lower().split())


def _prefer_completed_action(existing: bytes, candidate: bytes) -> bytes:
    existing_body, existing_ending = _split_line_ending(existing)
    candidate_body, _candidate_ending = _split_line_ending(candidate)
    existing_match = _ACTION_LINE_PATTERN.match(existing_body)
    candidate_match = _ACTION_LINE_PATTERN.match(candidate_body)
    if existing_match is None or candidate_match is None:
        return existing
    if existing_match.group("state") in {b"x", b"X"} or candidate_match.group("state") == b" ":
        return existing
    state_start, state_end = existing_match.span("state")
    return existing_body[:state_start] + candidate_match.group("state") + existing_body[state_end:] + existing_ending


def _split_line_ending(line: bytes) -> tuple[bytes, bytes]:
    if line.endswith(b"\r\n"):
        return line[:-2], b"\r\n"
    if line.endswith(b"\n") or line.endswith(b"\r"):
        return line[:-1], line[-1:]
    return line, b""


def _read_regular_file(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError("meeting upgrade file must not be a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("meeting upgrade file must be a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_existing_directory_components(path: Path) -> None:
    _validate_directory_components(path, create_missing=False)


def _ensure_safe_directory(path: Path) -> None:
    _validate_directory_components(path, create_missing=True)


def _validate_directory_components(path: Path, *, create_missing: bool) -> None:
    absolute_path = path.absolute()
    cursor = Path(absolute_path.anchor)
    for part in absolute_path.parts[1:]:
        cursor = cursor / part
        try:
            status = cursor.lstat()
        except FileNotFoundError:
            if not create_missing:
                raise ValueError("meeting upgrade trusted directory ancestor is missing") from None
            cursor.mkdir()
            _fsync_parent_directory(cursor.parent)
            status = cursor.lstat()
        if stat.S_ISLNK(status.st_mode):
            raise ValueError("meeting upgrade archive path must not traverse a symlink")
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError("meeting upgrade archive ancestor must be a directory")


def _atomic_create_bytes(path: Path, content: bytes) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".archive.tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp_path, path, follow_symlinks=False)
        temp_path.unlink()
        temp_path = None
        _fsync_parent_directory(path.parent)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise ValueError("meeting upgrade output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_parent_directory(path.parent)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _fsync_parent_directory(parent: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
