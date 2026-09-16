from __future__ import annotations

import errno
import time
from pathlib import Path

VAULT_READ_MAX_ATTEMPTS = 3
VAULT_READ_RETRY_DELAYS_SECONDS = (0.1, 0.25)
_TRANSIENT_VAULT_READ_ERRNOS = frozenset({errno.EAGAIN, errno.EDEADLK})


class TransientVaultReadError(RuntimeError):
    """A cloud-backed vault file remained temporarily unavailable after retries."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Vault file remained temporarily unavailable: {path}")


def read_text_with_retry(path: Path, *, encoding: str = "utf-8") -> str:
    """Read a cloud-backed vault file, retrying only transient provider locks."""
    for attempt in range(VAULT_READ_MAX_ATTEMPTS):
        try:
            return path.read_text(encoding=encoding)
        except OSError as exc:
            if exc.errno not in _TRANSIENT_VAULT_READ_ERRNOS:
                raise
            if attempt == VAULT_READ_MAX_ATTEMPTS - 1:
                raise TransientVaultReadError(path) from exc
            time.sleep(VAULT_READ_RETRY_DELAYS_SECONDS[attempt])

    raise AssertionError("vault read retry loop exited unexpectedly")
