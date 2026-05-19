from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = REPO_ROOT / "scripts" / "run_web_clipper_server.sh"
HEALTH_CHECK_PATH = REPO_ROOT / "scripts" / "check_launch_agents.sh"


class WebClipperLaunchWrapperTests(unittest.TestCase):
    def test_wrapper_loads_token_env_file_before_starting_server(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")

        self.assertIn("OBSIDIAN_WEB_CLIPPER_ENV_FILE", wrapper)
        self.assertIn("$HOME/.config/obsidian-agents/web-clipper.env", wrapper)
        self.assertIn('source "$ENV_FILE"', wrapper)
        self.assertIn("OBSIDIAN_WEB_CLIPPER_TOKEN is required", wrapper)
        self.assertIn("exit 2", wrapper)

    def test_wrapper_runs_capture_server_through_automation_command(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")

        self.assertIn("scripts/run_automation_command.sh", wrapper)
        self.assertIn('"web-clipper"', wrapper)
        self.assertIn('"logs/web-clipper.stdout.log"', wrapper)
        self.assertIn('"logs/web-clipper.stderr.log"', wrapper)
        self.assertIn('"$REPO_ROOT/.venv/bin/obsidian-agent" web-clips serve', wrapper)

    def test_health_check_includes_web_clipper_job_and_logs(self) -> None:
        health_check = HEALTH_CHECK_PATH.read_text(encoding="utf-8")

        self.assertIn('"com.obsidian.agent.web-clipper"', health_check)
        self.assertIn('"web-clipper.stdout.log"', health_check)
        self.assertIn('"web-clipper.stderr.log"', health_check)

    def test_wrapper_logs_clear_error_when_token_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scripts_dir = tmp_path / "scripts"
            scripts_dir.mkdir()
            wrapper_path = scripts_dir / "run_web_clipper_server.sh"
            shutil.copy2(WRAPPER_PATH, wrapper_path)
            wrapper_path.chmod(0o755)

            env = os.environ.copy()
            env["HOME"] = str(tmp_path / "home")
            env["OBSIDIAN_WEB_CLIPPER_ENV_FILE"] = str(tmp_path / "missing.env")

            result = subprocess.run(
                [str(wrapper_path)],
                check=False,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("OBSIDIAN_WEB_CLIPPER_TOKEN is required", result.stderr)
            self.assertIn(
                "OBSIDIAN_WEB_CLIPPER_TOKEN is required",
                (tmp_path / "logs" / "web-clipper.stderr.log").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
