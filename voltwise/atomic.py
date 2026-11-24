"""Crash- and race-safe text writes shared by all VoltWise JSON outputs."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def atomic_write_text(path: str | Path, content: str, *, retries: int = 10) -> Path:
    """Write text via an exclusive temporary file and an OS rename.

    Uses O_EXCL through ``mkstemp`` (no symlink clobber) and retries the rename
    on Windows, where another reader can briefly hold the destination open.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        for attempt in range(retries):
            try:
                os.replace(temporary, target)
                break
            except (PermissionError, OSError):
                if attempt == retries - 1:
                    raise
                time.sleep(0.01 * (attempt + 1))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target
