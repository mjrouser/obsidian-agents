from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

MarkerKind = Literal["pending", "processed", "malformed", "unknown"]
ProcessingSourceKind = Literal["fallback", "transcript", "manual", "unknown"]
UpgradeState = Literal["awaiting_transcript", "terminal", "manual_review_required", "unknown"]

_PENDING_SOURCE_TYPE = "meeting_sync_pending"
_LEGACY_PROCESSED_SOURCE_TYPE = "meeting_sync_identity"
_PROCESSED_SOURCE_TYPE = "meeting_bundle_processed"
_PROCESSED_SOURCE_TYPES = frozenset({_LEGACY_PROCESSED_SOURCE_TYPE, _PROCESSED_SOURCE_TYPE})
_DEFAULT_RETRY_WINDOW = timedelta(hours=24)
_ACTIVE_TRANSACTIONS = threading.local()


@dataclass(frozen=True, slots=True)
class MeetingIdentityState:
    marker_kind: MarkerKind
    processing_source_kind: ProcessingSourceKind
    upgrade_state: UpgradeState
    retry_until: datetime
    payload: dict[str, object]

    def is_terminal(self, *, now: datetime | None = None) -> bool:
        if self.marker_kind in {"malformed", "unknown"}:
            return True
        if self.marker_kind == "pending":
            return False
        if self.processing_source_kind != "fallback":
            return True
        if self.upgrade_state != "awaiting_transcript":
            return True
        effective_now = _as_aware_utc(now or datetime.now(tz=UTC))
        return effective_now > self.retry_until


def load_identity_state(payload: dict[str, object], scheduled_end: datetime) -> MeetingIdentityState:
    normalized_scheduled_end = _as_aware_utc(scheduled_end)
    marker_kind = _marker_kind(payload.get("source_type"))
    processing_source_kind = _processing_source_kind(payload)
    upgrade_state = _upgrade_state(payload)
    parsed_retry_until = _parse_datetime(payload.get("retry_until"))

    if marker_kind == "pending":
        retry_until = parsed_retry_until or normalized_scheduled_end + _DEFAULT_RETRY_WINDOW
    elif marker_kind == "processed" and processing_source_kind == "fallback":
        if _is_schema_v2(payload):
            retry_until = parsed_retry_until or normalized_scheduled_end
            if parsed_retry_until is None or upgrade_state == "unknown":
                upgrade_state = "manual_review_required"
        elif "schema_version" in payload:
            retry_until = parsed_retry_until or normalized_scheduled_end
            upgrade_state = "manual_review_required"
        else:
            retry_until = parsed_retry_until or normalized_scheduled_end + _DEFAULT_RETRY_WINDOW
            if upgrade_state == "unknown":
                upgrade_state = "awaiting_transcript"
    else:
        retry_until = parsed_retry_until or normalized_scheduled_end

    return MeetingIdentityState(
        marker_kind=marker_kind,
        processing_source_kind=processing_source_kind,
        upgrade_state=upgrade_state,
        retry_until=retry_until,
        payload=dict(payload),
    )


def read_identity_state(path: Path, scheduled_end: datetime) -> MeetingIdentityState:
    normalized_scheduled_end = _as_aware_utc(scheduled_end)
    if not identity_marker_entry_exists(path):
        return _non_payload_state("unknown", normalized_scheduled_end)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _non_payload_state("malformed", normalized_scheduled_end)
    if not isinstance(payload, dict):
        return _non_payload_state("malformed", normalized_scheduled_end)
    return load_identity_state(payload, normalized_scheduled_end)


def identity_marker_entry_exists(path: Path) -> bool:
    try:
        return path.is_symlink() or path.exists()
    except OSError:
        return True


def write_identity_marker(path: Path, payload: dict[str, object]) -> None:
    with identity_marker_transaction(path) as transaction:
        transaction.write(payload)


def compare_and_write_identity_marker(
    path: Path,
    expected_bytes_or_missing: bytes | None,
    payload: dict[str, object],
) -> None:
    with identity_marker_transaction(path) as transaction:
        transaction.compare_and_write(expected_bytes_or_missing, payload)


class IdentityMarkerTransaction:
    """One non-reentrant transaction guarded by a stable adjacent marker lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor = -1
        self._active_key: str | None = None
        self._used = False

    def __enter__(self) -> Self:
        if self._used:
            raise RuntimeError("identity marker transactions are single-use and not reentrant")
        self._used = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        active_paths = _active_transaction_paths()
        active_key = os.path.abspath(self.path)
        if active_key in active_paths:
            raise RuntimeError("identity marker transactions are not reentrant")
        active_paths.add(active_key)
        self._active_key = active_key
        try:
            self._descriptor = _open_and_lock_identity_marker_lock(self.path)
        except Exception:
            active_paths.remove(active_key)
            self._active_key = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        descriptor = self._descriptor
        self._descriptor = -1
        try:
            if descriptor >= 0:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if self._active_key is not None:
                _active_transaction_paths().discard(self._active_key)
                self._active_key = None

    def read_bytes_or_missing(self) -> bytes | None:
        self._require_active()
        return _read_identity_marker_bytes_or_missing(self.path)

    def compare(self, expected_bytes_or_missing: bytes | None) -> None:
        current = self.read_bytes_or_missing()
        if current != expected_bytes_or_missing:
            raise ValueError("identity marker changed before compare-and-write")

    def write(self, payload: dict[str, object]) -> None:
        current = self.read_bytes_or_missing()
        _validate_identity_marker_transition(current, payload)
        _atomic_write_identity_marker(self.path, payload)

    def compare_and_write(
        self,
        expected_bytes_or_missing: bytes | None,
        payload: dict[str, object],
    ) -> None:
        current = self.read_bytes_or_missing()
        if current != expected_bytes_or_missing:
            raise ValueError("identity marker changed before compare-and-write")
        _validate_identity_marker_transition(current, payload)
        _atomic_write_identity_marker(self.path, payload)

    def _require_active(self) -> None:
        if self._descriptor < 0:
            raise RuntimeError("identity marker transaction is not active")


def identity_marker_transaction(path: Path) -> IdentityMarkerTransaction:
    return IdentityMarkerTransaction(path)


def _active_transaction_paths() -> set[str]:
    active_paths = getattr(_ACTIVE_TRANSACTIONS, "paths", None)
    if active_paths is None:
        active_paths = set()
        _ACTIVE_TRANSACTIONS.paths = active_paths
    return active_paths


def _open_and_lock_identity_marker_lock(path: Path) -> int:
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ValueError("identity marker lock file could not be opened safely") from exc
    try:
        _validate_identity_marker_lock_descriptor(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        descriptor_status = os.fstat(descriptor)
        path_status = os.stat(lock_path, follow_symlinks=False)
        if (
            descriptor_status.st_dev != path_status.st_dev
            or descriptor_status.st_ino != path_status.st_ino
            or not stat.S_ISREG(path_status.st_mode)
            or path_status.st_nlink != 1
        ):
            raise ValueError("identity marker lock file changed while it was acquired")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _validate_identity_marker_lock_descriptor(descriptor: int) -> None:
    lock_status = os.fstat(descriptor)
    if not stat.S_ISREG(lock_status.st_mode) or lock_status.st_nlink != 1:
        raise ValueError("identity marker lock file must be an unaliased regular file")


def _read_identity_marker_bytes_or_missing(path: Path) -> bytes | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        if identity_marker_entry_exists(path):
            raise ValueError("identity marker could not be read safely") from exc
        return None
    except OSError as exc:
        raise ValueError("identity marker could not be read safely") from exc
    try:
        marker_status = os.fstat(descriptor)
        if not stat.S_ISREG(marker_status.st_mode):
            raise ValueError("identity marker is not a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_identity_marker_transition(
    current: bytes | None,
    payload: dict[str, object],
) -> None:
    replacement_source_type = payload.get("source_type")
    if current is None:
        return
    existing_source_type: object = None
    try:
        existing_payload = json.loads(current.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        existing_payload = None
    if isinstance(existing_payload, dict):
        existing_source_type = existing_payload.get("source_type")
    if replacement_source_type == _PENDING_SOURCE_TYPE:
        if existing_source_type == _PENDING_SOURCE_TYPE:
            return
        if existing_source_type in _PROCESSED_SOURCE_TYPES:
            raise ValueError("refusing to downgrade an existing processed identity marker")
        raise ValueError("pending identity marker refresh requires a recognized pending identity marker")
    if existing_source_type in _PROCESSED_SOURCE_TYPES and replacement_source_type not in _PROCESSED_SOURCE_TYPES:
        raise ValueError("refusing to downgrade an existing processed identity marker")


def _atomic_write_identity_marker(path: Path, payload: dict[str, object]) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
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


def _non_payload_state(marker_kind: Literal["malformed", "unknown"], scheduled_end: datetime) -> MeetingIdentityState:
    return MeetingIdentityState(
        marker_kind=marker_kind,
        processing_source_kind="unknown",
        upgrade_state="unknown",
        retry_until=scheduled_end,
        payload={},
    )


def _marker_kind(source_type: object) -> MarkerKind:
    if source_type == _PENDING_SOURCE_TYPE:
        return "pending"
    if source_type in _PROCESSED_SOURCE_TYPES:
        return "processed"
    return "unknown"


def _processing_source_kind(payload: dict[str, object]) -> ProcessingSourceKind:
    if payload.get("source_type") == _LEGACY_PROCESSED_SOURCE_TYPE:
        return "unknown"
    explicit_kind = payload.get("processing_source_kind")
    if explicit_kind == "fallback":
        return "fallback"
    if explicit_kind == "transcript":
        return "transcript"
    if explicit_kind == "manual":
        return "manual"
    if explicit_kind == "unknown":
        return "unknown"
    if "schema_version" in payload:
        return "unknown"

    source_name = payload.get("preferred_input_source_name")
    if not isinstance(source_name, str):
        return "unknown"
    normalized_source_name = " ".join(source_name.casefold().split())
    if "copilot recap" in normalized_source_name or "ai summary" in normalized_source_name:
        return "fallback"
    if "transcript" in normalized_source_name or ".vtt" in normalized_source_name:
        return "transcript"
    if "manual" in normalized_source_name:
        return "manual"
    return "unknown"


def _upgrade_state(payload: dict[str, object]) -> UpgradeState:
    explicit_state = payload.get("upgrade_state")
    if explicit_state == "awaiting_transcript":
        return "awaiting_transcript"
    if explicit_state == "terminal":
        return "terminal"
    if explicit_state == "manual_review_required":
        return "manual_review_required"
    if explicit_state == "unknown":
        return "unknown"
    return "unknown"


def _is_schema_v2(payload: dict[str, object]) -> bool:
    schema_version = payload.get("schema_version")
    return type(schema_version) is int and schema_version == 2


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
