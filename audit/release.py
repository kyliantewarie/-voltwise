#!/usr/bin/env python3
"""Build release integrity artifacts (SHA-256SUMS + optional detached GPG signature)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = [
    ROOT / "audit" / "AUDIT_REPORT.md",
    ROOT / "audit" / "sbom.cdx.json",
    ROOT / "addon" / "config.yaml",
    ROOT / "voltwise" / "_version.py",
]


def write_checksums(root: Path | None = None) -> Path:
    base = root or ROOT
    target = base / "SHA256SUMS"
    lines = []
    for path in ARTIFACTS:
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(ROOT).as_posix()}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def sign(target: Path) -> bool:
    key = os.environ.get("VOLTWISE_RELEASE_KEY")
    if not key:
        return False
    signature = target.with_name(target.name + ".asc")
    try:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["gpg", "--batch", "--yes", "--armor", "--local-user", key,
             "--detach-sign", "--output", str(signature), str(target)],
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"warning: signing unavailable ({type(exc).__name__}); artifacts are unsigned")
        return False
    return True


def main() -> None:
    checksums = write_checksums()
    signed = sign(checksums)
    print(f"wrote {checksums}")
    print(f"signed: {signed}")


if __name__ == "__main__":
    main()
