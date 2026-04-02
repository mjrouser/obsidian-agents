from __future__ import annotations

import time
from pathlib import Path

from .automation import IntakeAutomationWatcher, append_log
from .processors.meeting_processor import MeetingProcessor


def watch_intake(processor: MeetingProcessor) -> None:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class IntakeEventHandler(FileSystemEventHandler):
        def __init__(self, inner_processor: MeetingProcessor) -> None:
            self.processor = inner_processor
            self.watcher = IntakeAutomationWatcher(inner_processor)

        def _queue_event(self, event: object) -> None:
            if getattr(event, "is_directory", False):
                return
            path = Path(getattr(event, "src_path"))
            if not self.processor._is_under_intake(path):
                return
            self.watcher.mark_pending(path)

        def on_created(self, event: object) -> None:
            self._queue_event(event)

        def on_modified(self, event: object) -> None:
            self._queue_event(event)

        def on_moved(self, event: object) -> None:
            if getattr(event, "is_directory", False):
                return
            destination = getattr(event, "dest_path", None)
            if destination is None:
                return
            path = Path(destination)
            if not self.processor._is_under_intake(path):
                return
            self.watcher.mark_pending(path)

    observer = Observer()
    handler = IntakeEventHandler(processor)
    observer.schedule(handler, str(processor.intake_path), recursive=False)
    observer.start()
    print(f"Watching {processor.intake_path}")
    append_log(handler.watcher.log_path, f"watching {processor.intake_path}")

    try:
        while True:
            handler.watcher.flush_ready()
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
