from __future__ import annotations

import time
from pathlib import Path

from .processors.meeting_processor import MeetingProcessor


def watch_intake(processor: MeetingProcessor) -> None:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class IntakeEventHandler(FileSystemEventHandler):
        def __init__(self, inner_processor: MeetingProcessor) -> None:
            self.processor = inner_processor

        def on_created(self, event: object) -> None:
            if getattr(event, "is_directory", False):
                return
            path = Path(getattr(event, "src_path"))
            if self.processor.should_process_intake_file(path):
                print(f"Processing {path}")
                self.processor.process_file(path)

    observer = Observer()
    handler = IntakeEventHandler(processor)
    observer.schedule(handler, str(processor.intake_path), recursive=False)
    observer.start()
    print(f"Watching {processor.intake_path}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
