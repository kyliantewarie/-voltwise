"""Structured logging and lightweight operational metrics."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, safe for log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, allow_nan=False)


class Metrics:
    """Thread-safe counters and gauges written as JSON on demand."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, float] = {
            "cycles_total": 0,
            "cycles_ok": 0,
            "cycles_failed": 0,
            "actuation_calls": 0,
            "last_cycle_epoch": 0.0,
        }

    def increment(self, key: str, amount: float = 1.0) -> None:
        with self._lock:
            self._data[key] = self._data.get(key, 0.0) + amount

    def set(self, key: str, value: float) -> None:
        with self._lock:
            self._data[key] = value

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._data)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        from .atomic import atomic_write_text

        payload = self.snapshot()
        payload["generated_epoch"] = time.time()
        atomic_write_text(path, json.dumps(payload, indent=2, allow_nan=False) + "\n")
