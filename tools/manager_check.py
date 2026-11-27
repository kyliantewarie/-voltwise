#!/usr/bin/env python3
"""Git Manager weekly check: secrets, insecure defaults, supply-chain pins.

Stdlib only, read-only. Exits non-zero on findings. Never prints secret values.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_EXT = {
    ".py", ".js", ".ts", ".sh", ".ps1", ".yml", ".yaml", ".json", ".md",
    ".txt", ".toml", ".html", ".conf", ".ini", ".cfg", ".example", ".xml",
}

SECRET_PATTERNS = [
    ("github PAT", re.compile(r"github_pat_[A-Za-z0-9]{20,}")),
    ("classic GitHub token", re.compile(r"\bghp_[A-Za-z0-9]{30,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("token in URL", re.compile(r"access_token=[A-Za-z0-9]{20,}")),
]
PIN = re.compile(r"uses:\s*[^@\s]+@v\d+\b")
CHMOD777 = re.compile(r"chmod\s+-R\s+777")
ANON_TRUE = re.compile(r"allow_anonymous\s+true")
# Files where "allow_anonymous true" is intentional: docs, detectors, tests.
ANON_ALLOW = ("tests/", "test_", "smoke.conf", "TROUBLESHOOTING.md", "docs/", "SECURITY.md", "run_audits.py", "verify-stack.sh")
SELF = "tools/manager_check.py"

findings: list[str] = []


def tracked_files() -> list[Path]:
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [p for p in ROOT.rglob("*") if p.is_file()]
    return [ROOT / p for p in out.splitlines() if p]


def main() -> int:
    files = [f for f in tracked_files() if f.is_file() and f.suffix.lower() in TEXT_EXT]
    print(f"Git Manager check: {len(files)} tracked text files")

    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        if rel == SELF:
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for name, pat in SECRET_PATTERNS:
            if pat.search(text):
                findings.append(f"{rel}: possible {name} (value withheld)")
        if PIN.search(text):
            findings.append(f"{rel}: GitHub Action not pinned to a commit SHA")
        if CHMOD777.search(text):
            findings.append(f"{rel}: chmod -R 777")
        if ANON_TRUE.search(text) and not any(a in rel for a in ANON_ALLOW):
            findings.append(f"{rel}: allow_anonymous true outside test/docs scope")

    gi = ROOT / ".gitignore"
    ignored = gi.read_text(encoding="utf-8", errors="ignore") if gi.exists() else ""
    for marker in (".env", "token", ".key"):
        if marker not in ignored:
            findings.append(f".gitignore: missing ignore rule for {marker}")

    if findings:
        print(f"FAIL: {len(findings)} finding(s)")
        for f in findings:
            print(f"  - {f}")
        return 1
    print("PASS: no secrets, no unpinned actions, no insecure defaults")
    return 0


if __name__ == "__main__":
    sys.exit(main())
