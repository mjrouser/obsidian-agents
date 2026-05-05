from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "write_automation_failure_note.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("write_automation_failure_note_script", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WriteAutomationFailureNoteScriptTests(unittest.TestCase):
    def test_falls_back_to_local_note_when_config_is_missing(self) -> None:
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            missing_config = tmp_path / "config.yaml"
            stderr_log = tmp_path / "weekly-briefing.stderr.log"
            stderr_log.write_text("FileNotFoundError: missing config.yaml\n", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = module.main(
                    [
                        "--config",
                        str(missing_config),
                        "--job",
                        "weekly-briefing",
                        "--exit-code",
                        "1",
                        "--stderr-log",
                        str(stderr_log),
                    ]
                )

            self.assertEqual(exit_code, 0)
            fallback_dir = tmp_path / "automation-failures"
            latest_note = fallback_dir / "ACTION NEEDED - Latest Automation Failure.md"
            self.assertTrue(latest_note.exists())
            self.assertIn("automation_failure_note_fallback:", stdout.getvalue())
            self.assertIn("warning: wrote fallback automation failure note", stderr.getvalue())
            latest_text = latest_note.read_text(encoding="utf-8")
            self.assertIn("Config load error", latest_text)
            self.assertIn("FileNotFoundError: missing config.yaml", latest_text)


if __name__ == "__main__":
    unittest.main()
