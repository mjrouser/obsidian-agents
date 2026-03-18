from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from obsidian_intake_agent.utils.fs import prepend_status_marker


class StatusMarkerTests(unittest.TestCase):
    def test_prepends_status_marker_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "note.md"
            path.write_text("Body\n", encoding="utf-8")

            prepend_status_marker(path, "PROCESSED — see [[01_Meetings/Example.md]]")
            prepend_status_marker(path, "PROCESSED — see [[01_Meetings/Example.md]]")

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "STATUS: PROCESSED — see [[01_Meetings/Example.md]]\nBody\n",
            )


if __name__ == "__main__":
    unittest.main()
