#!/usr/bin/env python3
"""Mint a VoltWise licence key with the vendor's Ed25519 private key.

The private key never enters the repository: it is read from
``VOLTWISE_LICENCE_PRIVATE_KEY`` (a path) or from the maintainer's user profile.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys_path = str(ROOT)
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from voltwise.licensing import encode_licence  # noqa: E402


def private_key_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("VOLTWISE_LICENCE_PRIVATE_KEY")
    if env:
        return Path(env)
    return Path.home() / ".voltwise" / "licence-private.pem"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", default="core,pro")
    parser.add_argument("--subject", default="VoltWise Pro Kit")
    parser.add_argument("--days", type=int, default=3650)
    parser.add_argument("--hwid", default=None,
                        help="optional device fingerprint to bind the key to one install")
    parser.add_argument("--key-file", default=None)
    args = parser.parse_args()

    path = private_key_path(args.key_file)
    if not path.is_file():
        raise SystemExit(f"vendor private key not found: {path}")
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=args.days)
    payload = {
        "sub": args.subject,
        "features": [f.strip() for f in args.features.split(",") if f.strip()],
        "exp": expires.timestamp(),
    }
    if args.hwid:
        payload["hwid"] = args.hwid
    key = encode_licence(payload, path.read_text(encoding="utf-8"))
    print(key)


if __name__ == "__main__":
    main()
