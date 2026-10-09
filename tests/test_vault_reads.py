from __future__ import annotations

import errno
import unittest
from pathlib import Path
from unittest.mock import patch

from obsidian_intake_agent.utils.vault_reads import TransientVaultReadError, read_text_with_retry


class VaultReadTests(unittest.TestCase):
    def test_retries_transient_provider_lock_until_read_succeeds(self) -> None:
        path = Path("vault-note.md")
        with (
            patch.object(
                Path,
                "read_text",
                side_effect=[
                    OSError(errno.EDEADLK, "Resource deadlock avoided"),
                    OSError(errno.EDEADLK, "Resource deadlock avoided"),
                    "ready\\n",
                ],
            ) as read_text,
            patch("obsidian_intake_agent.utils.vault_reads.time.sleep") as sleep,
        ):
            self.assertEqual(read_text_with_retry(path), "ready\\n")

        self.assertEqual(read_text.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_raises_path_specific_error_after_transient_retry_exhaustion(self) -> None:
        path = Path("locked-note.md")
        with (
            patch.object(
                Path,
                "read_text",
                side_effect=OSError(errno.EDEADLK, "Resource deadlock avoided"),
            ) as read_text,
            patch("obsidian_intake_agent.utils.vault_reads.time.sleep"),
            self.assertRaises(TransientVaultReadError) as exc,
        ):
            read_text_with_retry(path)

        self.assertEqual(exc.exception.path, path)
        self.assertIsInstance(exc.exception.__cause__, OSError)
        self.assertEqual(read_text.call_count, 3)

    def test_does_not_retry_unrelated_io_error(self) -> None:
        path = Path("forbidden-note.md")
        with (
            patch.object(Path, "read_text", side_effect=PermissionError("denied")) as read_text,
            patch("obsidian_intake_agent.utils.vault_reads.time.sleep") as sleep,
            self.assertRaises(PermissionError),
        ):
            read_text_with_retry(path)

        read_text.assert_called_once()
        sleep.assert_not_called()
