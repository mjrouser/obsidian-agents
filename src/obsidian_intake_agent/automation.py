from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import hashlib
import os
import time

from .processors.meeting_processor import MeetingProcessor


@dataclass(slots=True)
class FileSignature:
    size: int
    mtime_ns: int


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.write(self._fd, str(os.getpid()).encode("utf-8"))
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise RuntimeError(f"Lock already held: {self.path}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


def current_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def append_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{current_timestamp()} {message}\n")


def signature_for_path(path: Path) -> FileSignature | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return FileSignature(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def wait_for_stable_file(path: Path, *, stable_seconds: int) -> bool:
    first = signature_for_path(path)
    if first is None:
        return False
    time.sleep(max(stable_seconds, 0))
    second = signature_for_path(path)
    return first == second and second is not None


def lock_name_for_path(path: Path) -> str:
    digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()
    return f"{digest}.lock"


def run_with_lock(lock_path: Path, callback: Callable[[], None]) -> bool:
    lock = FileLock(lock_path)
    if not lock.acquire():
        return False
    try:
        callback()
    finally:
        lock.release()
    return True


class IntakeAutomationWatcher:
    def __init__(self, processor: MeetingProcessor, *, log_path: Path | None = None) -> None:
        self.processor = processor
        self.debounce_seconds = max(int(processor.config.watcher_settle_seconds), 1)
        self.stable_seconds = max(int(processor.config.watcher_stable_seconds), 0)
        self.log_path = log_path or Path(processor.config.automation_log_dir) / "intake-watcher.log"
        self.lock_dir = Path(processor.config.automation_log_dir) / "locks"
        self._pending: dict[Path, float] = {}

    def mark_pending(self, path: Path) -> None:
        self._pending[path] = time.monotonic()
        append_log(self.log_path, f"queued {path}")

    def flush_ready(self) -> None:
        now = time.monotonic()
        ready = [path for path, seen_at in self._pending.items() if now - seen_at >= self.debounce_seconds]
        for path in ready:
            self._pending.pop(path, None)
            self._process_path(path)

    def _process_path(self, path: Path) -> None:
        if not path.exists() or path.is_dir():
            append_log(self.log_path, f"skip missing {path}")
            return
        if not self.processor.should_process_intake_file(path):
            append_log(self.log_path, f"skip ineligible {path}")
            return
        if not wait_for_stable_file(path, stable_seconds=self.stable_seconds):
            append_log(self.log_path, f"reschedule unstable {path}")
            self.mark_pending(path)
            return

        lock_path = self.lock_dir / lock_name_for_path(path)

        def _run() -> None:
            append_log(self.log_path, f"processing {path}")
            try:
                result = self.processor.process_file(path)
                append_log(
                    self.log_path,
                    f"processed={result.processed} path={path} note={result.canonical_note_path}",
                )
                if result.processed and not self.processor.config.dry_run:
                    from .main import _maybe_auto_commit

                    _maybe_auto_commit(self.processor.config, vault_source_name=path.name)
            except Exception as exc:
                append_log(self.log_path, f"error path={path} detail={exc!r}")
                raise

        if not run_with_lock(lock_path, _run):
            append_log(self.log_path, f"skip locked {path}")
