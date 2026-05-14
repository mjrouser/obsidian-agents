from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from obsidian_intake_agent.automation import IntakeAutomationWatcher
from obsidian_intake_agent.processors.meeting_processor import ProcessResult


class IntakeAutomationWatcherTests(unittest.TestCase):
    def test_logs_processor_warnings_after_successful_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            intake_file = root / "vault" / "00_Intake" / "2026-05-13 - Teams - Long Meeting.vtt"
            intake_file.parent.mkdir(parents=True)
            intake_file.write_text("WEBVTT\n", encoding="utf-8")
            log_path = root / "logs" / "intake-watcher.log"
            processor = _ProcessorStub(
                result=ProcessResult(
                    processed=True,
                    canonical_note_path=root / "vault" / "01_Meetings" / "2026-05-13 - Teams - Long Meeting.md",
                    processing_warnings=["vtt_extraction_fallback reason=Codex CLI timed out"],
                )
            )
            watcher = IntakeAutomationWatcher(processor, log_path=log_path)

            watcher._process_path(intake_file)

            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn(
                f"warning path={intake_file} detail=vtt_extraction_fallback reason=Codex CLI timed out",
                log_text,
            )


class _ProcessorStub:
    def __init__(self, *, result: ProcessResult) -> None:
        self.result = result
        self.config = SimpleNamespace(
            watcher_settle_seconds=1,
            watcher_stable_seconds=0,
            automation_log_dir="logs",
            dry_run=True,
        )

    def should_process_intake_file(self, path: Path) -> bool:
        del path
        return True

    def process_file(self, path: Path) -> ProcessResult:
        del path
        return self.result


if __name__ == "__main__":
    unittest.main()
