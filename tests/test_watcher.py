from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from obsidian_intake_agent.watcher import watch_intake


class WatcherTests(unittest.TestCase):
    def test_rename_into_intake_queues_destination_path(self) -> None:
        intake_path = Path("/tmp/vault/00_Intake")
        queued_paths: list[Path] = []
        scheduled_handler = None

        class FakeAutomationWatcher:
            def __init__(self, inner_processor: object) -> None:
                self.processor = inner_processor
                self.log_path = Path("/tmp/intake-watcher.log")

            def mark_pending(self, path: Path) -> None:
                queued_paths.append(path)

            def flush_ready(self) -> None:
                raise KeyboardInterrupt

        class FakeObserver:
            def schedule(self, handler: object, path: str, recursive: bool = False) -> None:
                nonlocal scheduled_handler
                scheduled_handler = handler

            def start(self) -> None:
                event = types.SimpleNamespace(
                    is_directory=False,
                    src_path=str(intake_path / "Untitled.md"),
                    dest_path=str(intake_path / "2026-04-02 - Teams - Renamed Note.md"),
                )
                assert scheduled_handler is not None
                scheduled_handler.on_moved(event)

            def stop(self) -> None:
                return None

            def join(self) -> None:
                return None

        fake_watchdog_events = types.SimpleNamespace(FileSystemEventHandler=object)
        fake_watchdog_observers = types.SimpleNamespace(Observer=FakeObserver)

        processor = types.SimpleNamespace(
            intake_path=intake_path,
            _is_under_intake=lambda path: Path(path).parent == intake_path,
        )

        with patch.dict(
            sys.modules,
            {
                "watchdog.events": fake_watchdog_events,
                "watchdog.observers": fake_watchdog_observers,
            },
        ):
            with patch("obsidian_intake_agent.watcher.IntakeAutomationWatcher", FakeAutomationWatcher):
                with patch("obsidian_intake_agent.watcher.append_log"):
                    with patch("builtins.print"):
                        watch_intake(processor)

        self.assertEqual(
            queued_paths,
            [intake_path / "2026-04-02 - Teams - Renamed Note.md"],
        )
