from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = REPO_ROOT / "scripts" / "run_meeting_sync.sh"


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_file(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_exit(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_running(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} was still running")


class MeetingSyncLaunchWrapperTests(unittest.TestCase):
    def test_wrapper_uses_lockf_and_forwards_signals_to_descendants(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")

        self.assertIn("lockf -t 0", wrapper)
        self.assertIn("meeting-sync.lockfile", wrapper)
        self.assertIn("terminate_tree", wrapper)
        self.assertIn("pgrep -P", wrapper)
        self.assertIn("exit 143", wrapper)
        self.assertIn("exit 130", wrapper)

    @unittest.skipUnless(shutil.which("lockf"), "lockf is required for wrapper lock tests")
    def test_wrapper_logs_skip_when_lock_is_already_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = self._copy_wrapper_repo(Path(tmp_dir))
            lock_file = repo / "logs" / "locks" / "meeting-sync.lockfile"

            holder = subprocess.Popen(
                ["lockf", str(lock_file), "sleep", "5"],
                cwd=repo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                time.sleep(0.25)
                result = subprocess.run(
                    [str(repo / "scripts" / "run_meeting_sync.sh")],
                    cwd=repo,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                holder.terminate()
                holder.wait(timeout=5)

            self.assertEqual(result.returncode, 0)
            self.assertIn("previous run still active", result.stdout)
            self.assertIn(
                "previous run still active",
                (repo / "logs" / "meeting-sync.stdout.log").read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(shutil.which("lockf"), "lockf is required for wrapper lock tests")
    def test_wrapper_terminates_automation_descendants_on_term(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = self._copy_wrapper_repo(Path(tmp_dir))
            self._write_fake_pgrep(repo)
            self._write_descendant_run_automation(repo)
            env = os.environ.copy()
            env["PATH"] = f"{repo / 'bin'}:{env['PATH']}"

            process = subprocess.Popen(
                [str(repo / "scripts" / "run_meeting_sync.sh")],
                cwd=repo,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                child_pid_path = repo / "logs" / "child-shell.pid"
                grandchild_pid_path = repo / "logs" / "grandchild.pid"
                _wait_for_file(child_pid_path)
                _wait_for_file(grandchild_pid_path)
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))

                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=5)

                self.assertEqual(process.returncode, 143)
                _wait_for_exit(child_pid)
                _wait_for_exit(grandchild_pid)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def _copy_wrapper_repo(self, parent: Path) -> Path:
        repo = parent / "repo"
        scripts = repo / "scripts"
        venv_bin = repo / ".venv" / "bin"
        scripts.mkdir(parents=True)
        venv_bin.mkdir(parents=True)
        (repo / "logs" / "locks").mkdir(parents=True)

        wrapper = scripts / "run_meeting_sync.sh"
        shutil.copy2(WRAPPER_PATH, wrapper)
        wrapper.chmod(0o755)

        python = venv_bin / "python"
        python.write_text(f'#!/bin/bash\nexec {sys.executable!r} "$@"\n', encoding="utf-8")
        python.chmod(0o755)

        agent = venv_bin / "obsidian-agent"
        agent.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        agent.chmod(0o755)

        run_automation = scripts / "run_automation_command.sh"
        run_automation.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        run_automation.chmod(0o755)
        return repo

    def _write_fake_pgrep(self, repo: Path) -> None:
        bin_dir = repo / "bin"
        bin_dir.mkdir()
        pgrep = bin_dir / "pgrep"
        pgrep.write_text(
            f"""#!/bin/bash
set -euo pipefail
if [ "$#" -ne 2 ] || [ "$1" != "-P" ]; then
  exit 2
fi
parent="$2"
run_pid="$(cat {repo / "logs" / "run-automation.pid"} 2>/dev/null || true)"
child_pid="$(cat {repo / "logs" / "child-shell.pid"} 2>/dev/null || true)"
grandchild_pid="$(cat {repo / "logs" / "grandchild.pid"} 2>/dev/null || true)"
if [ -n "$child_pid" ] && [ "$parent" = "$child_pid" ]; then
  [ -n "$grandchild_pid" ] && echo "$grandchild_pid"
elif [ -n "$run_pid" ] && [ "$parent" = "$run_pid" ]; then
  [ -n "$child_pid" ] && echo "$child_pid"
elif [ -n "$grandchild_pid" ] && [ "$parent" = "$grandchild_pid" ]; then
  exit 0
elif [ -n "$run_pid" ]; then
  echo "$run_pid"
fi
""",
            encoding="utf-8",
        )
        pgrep.chmod(0o755)

    def _write_descendant_run_automation(self, repo: Path) -> None:
        run_automation = repo / "scripts" / "run_automation_command.sh"
        run_automation.write_text(
            """#!/bin/bash
set -euo pipefail
mkdir -p logs
echo "$$" > logs/run-automation.pid
bash -c 'sleep 30 & echo "$!" > logs/grandchild.pid; wait' &
echo "$!" > logs/child-shell.pid
wait "$!"
""",
            encoding="utf-8",
        )
        run_automation.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
