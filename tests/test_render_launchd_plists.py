from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_launchd_plists.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("render_launchd_plists_script", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RenderLaunchdPlistsTests(unittest.TestCase):
    def test_build_jobs_includes_web_clipper_launch_agent(self) -> None:
        module = _load_script_module()

        jobs = module.build_jobs(
            label_prefix="com.obsidian.agent",
            scripts_dir=Path("/repo/scripts"),
        )

        web_clipper_jobs = [job for job in jobs if job[0] == "com.obsidian.agent.web-clipper"]
        self.assertEqual(len(web_clipper_jobs), 1)
        label, script_path, schedule = web_clipper_jobs[0]
        self.assertEqual(label, "com.obsidian.agent.web-clipper")
        self.assertEqual(script_path, Path("/repo/scripts/run_web_clipper_server.sh"))
        self.assertEqual(
            schedule,
            {
                "RunAtLoad": "<true/>",
                "KeepAlive": "<true/>",
            },
        )

    def test_build_jobs_includes_meeting_sync_launch_agent(self) -> None:
        module = _load_script_module()

        jobs = module.build_jobs(
            label_prefix="com.obsidian.agent",
            scripts_dir=Path("/repo/scripts"),
        )

        meeting_jobs = [job for job in jobs if job[0] == "com.obsidian.agent.meeting-sync"]
        self.assertEqual(len(meeting_jobs), 1)
        label, script_path, schedule = meeting_jobs[0]
        self.assertEqual(label, "com.obsidian.agent.meeting-sync")
        self.assertEqual(script_path, Path("/repo/scripts/run_meeting_sync.sh"))
        self.assertIn("StartCalendarInterval", schedule)
        self.assertIn("<array>", schedule["StartCalendarInterval"])
        self.assertIn(
            "<key>Hour</key><integer>8</integer><key>Minute</key><integer>35</integer>",
            schedule["StartCalendarInterval"],
        )
        self.assertIn(
            "<key>Hour</key><integer>18</integer><key>Minute</key><integer>5</integer>",
            schedule["StartCalendarInterval"],
        )
        self.assertEqual(schedule["StartCalendarInterval"].count("<key>Weekday</key><integer>1</integer>"), 20)
        self.assertEqual(schedule["StartCalendarInterval"].count("<dict>"), 100)

    def test_rendered_web_clipper_plist_uses_wrapper_and_stays_alive(self) -> None:
        module = _load_script_module()

        plist = module.render_plist(
            label="com.obsidian.agent.web-clipper",
            script_path=Path("/repo/scripts/run_web_clipper_server.sh"),
            schedule={
                "RunAtLoad": "<true/>",
                "KeepAlive": "<true/>",
            },
        )

        self.assertIn("<string>com.obsidian.agent.web-clipper</string>", plist)
        self.assertIn("<string>/repo/scripts/run_web_clipper_server.sh</string>", plist)
        self.assertIn("<key>RunAtLoad</key>\n  <true/>", plist)
        self.assertIn("<key>KeepAlive</key>\n  <true/>", plist)

    def test_rendered_meeting_sync_plist_uses_calendar_intervals(self) -> None:
        module = _load_script_module()

        plist = module.render_plist(
            label="com.obsidian.agent.meeting-sync",
            script_path=Path("/repo/scripts/run_meeting_sync.sh"),
            schedule={
                "StartCalendarInterval": module.meeting_sync_start_calendar_interval(),
            },
        )

        self.assertIn("<string>com.obsidian.agent.meeting-sync</string>", plist)
        self.assertIn("<string>/repo/scripts/run_meeting_sync.sh</string>", plist)
        self.assertIn("<key>StartCalendarInterval</key>", plist)
        self.assertIn("<array>", plist)
        self.assertNotIn("<key>RunAtLoad</key>", plist)
        self.assertNotIn("<key>KeepAlive</key>", plist)


if __name__ == "__main__":
    unittest.main()
