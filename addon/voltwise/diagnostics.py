"""Sanitised support bundle for customer diagnostics."""

from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

from . import __version__
from .config import VoltWiseConfig
from .models import Plan


def _redact(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                return value.replace(secret, "***REDACTED***")
        return value
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    return value


def build_bundle(config: VoltWiseConfig, plan: Plan | None, metrics: dict | None = None) -> dict:
    """Return a JSON-serialisable bundle with all credentials removed."""
    import os

    secrets = {
        os.environ.get(name, "")
        for name in {
            config.actuation.ha_token_env or "HA_TOKEN",
            config.tariff.api_key_env,
            config.licensing.key_env,
        }
        if os.environ.get(name)
    }
    secrets.add(os.environ.get("SUPERVISOR_TOKEN", ""))
    if config.licensing.key:
        secrets.add(config.licensing.key)
    return {
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "config": _redact(config.to_dict(), secrets),
        "plan": None if plan is None else _redact(plan.to_dict(), secrets),
        "metrics": metrics or {},
    }


def write_bundle(path: str | Path, config: VoltWiseConfig, plan: Plan | None,
                 metrics: dict | None = None) -> Path:
    raw = str(path)
    if ".." in Path(raw).parts:
        raise ValueError("diagnostics path must not contain parent traversal")
    from .atomic import atomic_write_text

    target = Path(raw)
    atomic_write_text(
        target,
        json.dumps(build_bundle(config, plan, metrics), indent=2, allow_nan=False) + "\n",
    )
    return target
