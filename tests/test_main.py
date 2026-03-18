from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from obsidian_intake_agent.main import main


class MainCliTests(unittest.TestCase):
    def test_run_once_prints_summary_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            (intake_dir / "weekly-sync.md").write_text(
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )
            config_path = repo / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        f'vault_path: "{vault}"',
                        'intake_dir: "00_Intake"',
                        'meetings_dir: "01_Meetings"',
                        'actions_dir: "07_Actions"',
                        'archive_intake_dir: "_Archive/Intake"',
                        'templates_dir: "Templates"',
                        'owner_filter: "Matthew"',
                        "dry_run: true",
                        "include_unassigned: false",
                        'llm_provider: "none"',
                        "codex_model: null",
                        'codex_exec_cmd: ["codex", "exec"]',
                        'extraction_mode: "draft"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(["--config", str(config_path), "run", "--once"])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("processed_files: 1", output)
            self.assertIn("written_meeting_notes: 1", output)

    def test_process_force_reprocesses_and_prints_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "weekly-sync.md"
            intake_file.write_text(
                "STATUS: PROCESSED — see [[01_Meetings/old-note.md]]\n"
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )
            config_path = repo / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        f'vault_path: "{vault}"',
                        'intake_dir: "00_Intake"',
                        'meetings_dir: "01_Meetings"',
                        'actions_dir: "07_Actions"',
                        'archive_intake_dir: "_Archive/Intake"',
                        'templates_dir: "Templates"',
                        'owner_filter: "Matthew"',
                        "dry_run: false",
                        "include_unassigned: false",
                        'llm_provider: "none"',
                        "codex_model: null",
                        'codex_exec_cmd: ["codex", "exec"]',
                        'extraction_mode: "draft"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(["--config", str(config_path), "process", "--force", str(intake_file)])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("Reprocessing (forced)", output)
            self.assertIn("canonical_output_file:", output)

    def test_process_dry_run_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            intake_dir = vault / "00_Intake"
            intake_dir.mkdir(parents=True)
            intake_file = intake_dir / "weekly-sync.md"
            intake_file.write_text(
                "Action: Matthew will complete Codex setup by Friday.\n",
                encoding="utf-8",
            )
            config_path = repo / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        f'vault_path: "{vault}"',
                        'intake_dir: "00_Intake"',
                        'meetings_dir: "01_Meetings"',
                        'actions_dir: "07_Actions"',
                        'archive_intake_dir: "_Archive/Intake"',
                        'templates_dir: "Templates"',
                        'owner_filter: "Matthew"',
                        "dry_run: false",
                        "include_unassigned: false",
                        'llm_provider: "none"',
                        "codex_model: null",
                        'codex_exec_cmd: ["codex", "exec"]',
                        'extraction_mode: "draft"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(["--config", str(config_path), "process", "--dry-run", str(intake_file)])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("DRY RUN — would write:", output)
            self.assertIn("DRY RUN — would update:", output)
            self.assertIn("DRY RUN — would prepend processed marker", output)
            self.assertFalse((vault / "01_Meetings").exists())
            self.assertFalse((vault / "07_Actions").exists())
            self.assertNotIn("STATUS: PROCESSED", intake_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
