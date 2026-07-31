from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from obsidian_intake_agent.meetings.meeting_upgrade import (
    archive_canonical_note,
    canonical_matches_hash,
    capture_file_snapshots,
    migrate_action_backlinks,
    restore_file_snapshots,
    sha256_path,
)


class MeetingUpgradeTests(unittest.TestCase):
    def test_canonical_hash_requires_valid_expected_hash_and_unchanged_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            canonical = Path(tmp_dir) / "meeting.md"
            canonical.write_bytes(b"original fallback\n")
            expected = hashlib.sha256(b"original fallback\n").hexdigest()

            self.assertEqual(sha256_path(canonical), expected)
            self.assertTrue(canonical_matches_hash(canonical, expected))

            canonical.write_bytes(b"edited fallback\n")
            self.assertFalse(canonical_matches_hash(canonical, expected))
            self.assertFalse(canonical_matches_hash(canonical, None))
            self.assertFalse(canonical_matches_hash(canonical, "not-a-sha256"))
            self.assertFalse(canonical_matches_hash(canonical.with_name("missing.md"), expected))

    def test_archive_is_exact_collision_safe_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            canonical = root / "01_Meetings" / "meeting.md"
            archive_root = root / "_Archive" / "Intake" / "Meeting Upgrades"
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(b"fallback bytes\r\n")
            expected = hashlib.sha256(canonical.read_bytes()).hexdigest()

            first = archive_canonical_note(canonical, archive_root, expected_sha256=expected)
            second = archive_canonical_note(canonical, archive_root, expected_sha256=expected)
            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), b"fallback bytes\r\n")

            first.write_bytes(b"unrelated collision\n")
            collision = archive_canonical_note(canonical, archive_root, expected_sha256=expected)
            self.assertNotEqual(collision, first)
            self.assertEqual(collision.read_bytes(), b"fallback bytes\r\n")
            self.assertEqual(
                archive_canonical_note(canonical, archive_root, expected_sha256=expected),
                collision,
            )
            self.assertEqual(len(tuple(archive_root.glob("meeting*"))), 2)

    def test_archive_rejects_symlink_source_or_archive_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            canonical = root / "meeting.md"
            canonical.write_bytes(b"fallback\n")
            source_link = root / "meeting-link.md"
            source_link.symlink_to(canonical)
            with self.assertRaises(ValueError):
                archive_canonical_note(
                    source_link,
                    root / "archive",
                    expected_sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                )

            real_archive = root / "real-archive"
            real_archive.mkdir()
            archive_link = root / "archive-link"
            archive_link.symlink_to(real_archive, target_is_directory=True)
            with self.assertRaises(ValueError):
                archive_canonical_note(
                    canonical,
                    archive_link,
                    expected_sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                )

    def test_archive_rejects_symlinked_intermediate_even_when_archive_root_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            canonical = root / "meeting.md"
            canonical.write_bytes(b"fallback\n")
            real_parent = root / "real-parent"
            archive_root = real_parent / "Meeting Upgrades"
            archive_root.mkdir(parents=True)
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaises(ValueError):
                archive_canonical_note(
                    canonical,
                    linked_parent / "Meeting Upgrades",
                    expected_sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                )

    def test_archive_rejects_shared_trusted_root_below_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            real_shared = root / "real" / "shared"
            real_shared.mkdir(parents=True)
            linked_shared = root / "linked"
            linked_shared.symlink_to(real_shared, target_is_directory=True)
            canonical = linked_shared / "vault" / "01_Meetings" / "meeting.md"
            archive_root = linked_shared / "vault" / "_Archive" / "Intake" / "Meeting Upgrades"
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(b"fallback\n")

            with self.assertRaises(ValueError):
                archive_canonical_note(
                    canonical,
                    archive_root,
                    expected_sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                )

    def test_migrate_action_backlinks_changes_only_exact_meeting_links(self) -> None:
        old = Path("01_Meetings/2026-05-04 - Teams - Delivery Review.md")
        new = Path("01_Meetings/2026-05-04 - Teams - Delivery Review Clean.md")
        original = (
            "# Actions\r\n"
            "- [x] Done — Source: 2026-05-04 [[2026-05-04 - Teams - Delivery Review.md]]\r\n"
            "- [ ] Open — Source: 2026-05-04 [[2026-05-04 - Teams - Delivery Review#Actions|review]]\r\n"
            "- [X] Qualified — Source: 2026-05-04 "
            "[[01_Meetings/2026-05-04 - Teams - Delivery Review.md|Delivery]]\r\n"
            "- [ ] Other dir [[02_Other/2026-05-04 - Teams - Delivery Review.md]]\r\n"
            "- [ ] Similar [[2026-05-04 - Teams - Delivery Review Extra.md]]\r\n"
            "Paragraph [[2026-05-04 - Teams - Delivery Review.md]] stays unchanged.\r\n"
        ).encode()

        migrated = migrate_action_backlinks(original, old_note=old, clean_note=new)

        self.assertEqual(
            migrated,
            (
                "# Actions\r\n"
                "- [x] Done — Source: 2026-05-04 [[2026-05-04 - Teams - Delivery Review Clean.md]]\r\n"
                "- [ ] Open — Source: 2026-05-04 "
                "[[2026-05-04 - Teams - Delivery Review Clean.md#Actions|review]]\r\n"
                "- [X] Qualified — Source: 2026-05-04 "
                "[[01_Meetings/2026-05-04 - Teams - Delivery Review Clean.md|Delivery]]\r\n"
                "- [ ] Other dir [[02_Other/2026-05-04 - Teams - Delivery Review.md]]\r\n"
                "- [ ] Similar [[2026-05-04 - Teams - Delivery Review Extra.md]]\r\n"
                "Paragraph [[2026-05-04 - Teams - Delivery Review.md]] stays unchanged.\r\n"
            ).encode(),
        )

    def test_migrate_action_backlinks_dedupes_equivalent_action_preserving_completion(self) -> None:
        old = Path("01_Meetings/Delivery Review (fallback).md")
        new = Path("01_Meetings/Delivery Review.md")
        original = (
            "# Actions\n\n"
            "## This Week\n\n"
            "- [x] send update (Owner: Matthew) — Source: 2026-05-04 [[Delivery Review (fallback).md]]\n"
            "- [ ] send update (Owner: Matthew) — Source: 2026-05-04 [[Delivery Review.md]]\n"
            "- [ ] unrelated text [[Delivery Review (fallback).md]]\n"
        ).encode()

        migrated = migrate_action_backlinks(original, old_note=old, clean_note=new)

        self.assertEqual(
            migrated,
            (
                "# Actions\n\n"
                "## This Week\n\n"
                "- [x] send update (Owner: Matthew) — Source: 2026-05-04 [[Delivery Review.md]]\n"
                "- [ ] unrelated text [[Delivery Review (fallback).md]]\n"
            ).encode(),
        )

    def test_snapshot_restore_is_exact_and_deletes_new_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            existing = root / "existing.md"
            created = root / "created.md"
            existing.write_bytes(b"before\r\n")
            snapshots = capture_file_snapshots((existing, created))

            existing.write_bytes(b"after\n")
            created.write_bytes(b"new\n")
            restore_file_snapshots(snapshots)

            self.assertEqual(existing.read_bytes(), b"before\r\n")
            self.assertFalse(created.exists())

    def test_snapshot_rejects_symlink_and_restore_does_not_follow_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "target.md"
            target.write_bytes(b"private\n")
            link = root / "link.md"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                capture_file_snapshots((link,))

            output = root / "output.md"
            snapshots = capture_file_snapshots((output,))
            output.symlink_to(target)
            with self.assertRaises(ValueError):
                restore_file_snapshots(snapshots)
            self.assertEqual(target.read_bytes(), b"private\n")


if __name__ == "__main__":
    unittest.main()
