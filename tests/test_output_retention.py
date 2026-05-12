from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from obsidian_intake_agent.output_retention import apply_output_retention


class OutputRetentionTests(unittest.TestCase):
    def test_keeps_four_newest_by_filename_date_and_moves_older_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            filenames = [
                "2026-01-05 fifth.md",
                "2026-01-04 fourth.md",
                "2026-01-03 third.md",
                "2026-01-02 second.md",
                "2026-01-01 first.md",
            ]
            for filename in filenames:
                (root / filename).write_text(filename, encoding="utf-8")

            summary = apply_output_retention(
                root,
                archive_dir_name="Archive",
                keep_count=4,
            )

            self.assertEqual(summary.would_move, [])
            self.assertEqual(summary.skipped_existing, [])
            self.assertEqual(summary.moved, [root / "2026-01-01 first.md"])
            self.assertTrue((root / "2026-01-05 fifth.md").exists())
            self.assertTrue((root / "2026-01-04 fourth.md").exists())
            self.assertTrue((root / "2026-01-03 third.md").exists())
            self.assertTrue((root / "2026-01-02 second.md").exists())
            self.assertFalse((root / "2026-01-01 first.md").exists())
            self.assertEqual(
                (root / "Archive" / "2026-01-01 first.md").read_text(encoding="utf-8"),
                "2026-01-01 first.md",
            )

    def test_uses_filename_date_not_modified_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            old_by_name = root / "2026-01-01 old-name.md"
            new_by_name = root / "2026-01-02 new-name.md"
            old_by_name.write_text("old by name", encoding="utf-8")
            new_by_name.write_text("new by name", encoding="utf-8")
            recent_time = datetime.now().timestamp()
            stale_time = (datetime.now() - timedelta(days=30)).timestamp()
            os.utime(old_by_name, (recent_time, recent_time))
            os.utime(new_by_name, (stale_time, stale_time))

            summary = apply_output_retention(
                root,
                archive_dir_name="Archive",
                keep_count=1,
            )

            self.assertEqual(summary.moved, [root / "2026-01-01 old-name.md"])
            self.assertTrue(new_by_name.exists())
            self.assertFalse(old_by_name.exists())

    def test_ignores_undated_non_markdown_and_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            nested = root / "nested"
            nested.mkdir()
            (root / "2026-01-03 keep.md").write_text("keep", encoding="utf-8")
            (root / "2026-01-02 move.md").write_text("move", encoding="utf-8")
            (root / "notes.md").write_text("undated", encoding="utf-8")
            (root / "2026-01-01.txt").write_text("not markdown", encoding="utf-8")
            (root / "2026-01-01not-boundary.md").write_text("bad date", encoding="utf-8")
            (nested / "2026-01-01 nested.md").write_text("nested", encoding="utf-8")

            summary = apply_output_retention(
                root,
                archive_dir_name="Archive",
                keep_count=1,
            )

            self.assertEqual(summary.moved, [root / "2026-01-02 move.md"])
            self.assertTrue((root / "notes.md").exists())
            self.assertTrue((root / "2026-01-01.txt").exists())
            self.assertTrue((root / "2026-01-01not-boundary.md").exists())
            self.assertTrue((nested / "2026-01-01 nested.md").exists())

    def test_collision_leaves_source_file_in_root_and_reports_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            archive = root / "Archive"
            archive.mkdir()
            source = root / "2026-01-01 duplicate.md"
            source.write_text("source", encoding="utf-8")
            (root / "2026-01-02 keep.md").write_text("keep", encoding="utf-8")
            (archive / "2026-01-01 duplicate.md").write_text("existing", encoding="utf-8")

            summary = apply_output_retention(
                root,
                archive_dir_name="Archive",
                keep_count=1,
            )

            self.assertEqual(summary.moved, [])
            self.assertEqual(summary.skipped_existing, [source])
            self.assertTrue(source.exists())
            self.assertEqual(
                (archive / "2026-01-01 duplicate.md").read_text(encoding="utf-8"),
                "existing",
            )

    def test_dry_run_reports_candidates_without_moving_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "2026-01-01 move.md"
            source.write_text("move", encoding="utf-8")
            (root / "2026-01-02 keep.md").write_text("keep", encoding="utf-8")

            summary = apply_output_retention(
                root,
                archive_dir_name="Archive",
                keep_count=1,
                dry_run=True,
            )

            self.assertEqual(summary.moved, [])
            self.assertEqual(summary.would_move, [source])
            self.assertEqual(summary.skipped_existing, [])
            self.assertTrue(source.exists())
            self.assertFalse((root / "Archive").exists())

    def test_absolute_archive_dir_name_raises_without_moving_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "2026-01-01 move.md"
            source.write_text("move", encoding="utf-8")
            (root / "2026-01-02 keep.md").write_text("keep", encoding="utf-8")

            with self.assertRaises(ValueError):
                apply_output_retention(
                    root,
                    archive_dir_name="/tmp/archive",
                    keep_count=1,
                )

            self.assertTrue(source.exists())

    def test_traversing_archive_dir_name_raises_without_moving_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "2026-01-01 move.md"
            source.write_text("move", encoding="utf-8")
            (root / "2026-01-02 keep.md").write_text("keep", encoding="utf-8")

            with self.assertRaises(ValueError):
                apply_output_retention(
                    root,
                    archive_dir_name="../Archive",
                    keep_count=1,
                )

            self.assertTrue(source.exists())

    def test_allows_nested_relative_archive_dir_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "2026-01-01 move.md"
            source.write_text("move", encoding="utf-8")
            (root / "2026-01-02 keep.md").write_text("keep", encoding="utf-8")

            summary = apply_output_retention(
                root,
                archive_dir_name="Archives/Actions Archive",
                keep_count=1,
            )

            self.assertEqual(summary.moved, [source])
            self.assertFalse(source.exists())
            self.assertTrue((root / "Archives" / "Actions Archive" / source.name).exists())

    def test_file_root_dir_returns_empty_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "root-file.md"
            root.write_text("not a directory", encoding="utf-8")

            summary = apply_output_retention(
                root,
                archive_dir_name="Archive",
                keep_count=1,
            )

            self.assertEqual(summary.moved, [])
            self.assertEqual(summary.would_move, [])
            self.assertEqual(summary.skipped_existing, [])

    @unittest.skipUnless(hasattr(os, "symlink"), "os.symlink is unavailable")
    def test_archive_symlink_outside_root_is_rejected_without_moving_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "root"
            outside = Path(tmp_dir) / "outside"
            root.mkdir()
            outside.mkdir()
            source = root / "2026-01-01 move.md"
            source.write_text("move", encoding="utf-8")
            (root / "2026-01-02 keep.md").write_text("keep", encoding="utf-8")
            os.symlink(outside, root / "Archive", target_is_directory=True)

            with self.assertRaises(ValueError):
                apply_output_retention(
                    root,
                    archive_dir_name="Archive",
                    keep_count=1,
                )

            self.assertTrue(source.exists())
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
