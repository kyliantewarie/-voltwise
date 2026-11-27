#!/usr/bin/env python3
"""Vendor the Python sources into the Home Assistant add-on build context.

The Supervisor uses the add-on directory as the Docker build context, so every
COPY source must live inside ``addon/``. The repository package remains the
single source of truth; this script copies it (and requirements.txt) into the
build context. ``run_audits.py`` calls it before the packaging audit so drift
is impossible while audits pass.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "voltwise"
CONTEXT = ROOT / "addon"


def sync(context: Path | None = None) -> list[str]:
    """Copy sources into a build context (``addon/`` by default).

    Passing ``context`` lets tests stage a throw-away copy instead of rewriting
    the tracked add-on files.
    """
    target_root = context if context is not None else CONTEXT
    base = ROOT if context is None else target_root
    written: list[str] = []
    target_package = target_root / "voltwise"
    if target_package.exists():
        shutil.rmtree(target_package)
    target_package.mkdir(parents=True, exist_ok=True)
    for source in sorted(SOURCE.glob("*.py")):
        destination = target_package / source.name
        shutil.copy2(source, destination)
        written.append(destination.relative_to(base).as_posix())
    requirements = ROOT / "requirements.txt"
    shutil.copy2(requirements, target_root / "requirements.txt")
    written.append((target_root / "requirements.txt").relative_to(base).as_posix())
    return written


def fingerprints(context: Path | None = None) -> dict[str, str]:
    """Hash source and vendored files so the audit can prove they match."""
    target_root = context if context is not None else CONTEXT
    result: dict[str, str] = {}
    for source in sorted(SOURCE.glob("*.py")):
        vendored = target_root / "voltwise" / source.name
        if vendored.is_file():
            result[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
            result[source.name + "@addon"] = hashlib.sha256(vendored.read_bytes()).hexdigest()
    source_req = ROOT / "requirements.txt"
    vendored_req = target_root / "requirements.txt"
    if source_req.is_file() and vendored_req.is_file():
        result["requirements.txt"] = hashlib.sha256(source_req.read_bytes()).hexdigest()
        result["requirements.txt@addon"] = hashlib.sha256(vendored_req.read_bytes()).hexdigest()
    return result


if __name__ == "__main__":
    for path in sync():
        print(path)
