"""Minimal, hardened Home Assistant REST client.

Writes are allow-listed, rate-limited by a circuit breaker and disabled in
dry-run mode. The access token is loaded from the environment and redacted from
every error message. There is no shell access, dynamic evaluation or arbitrary
URL/service construction.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

_ENTITY_RE = re.compile(r"^(sensor|input_number|number|switch|select)\.[a-z0-9_]+$")
_WRITE_DOMAINS = frozenset({"input_number", "number", "switch", "select"})
_SERVICE_DOMAINS = frozenset({"script", "input_datetime", "homeassistant", "mqtt", "modbus"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "homeassistant", "supervisor"})


class HomeAssistantError(RuntimeError):
    """Raised for transport or protocol failures."""


class CircuitOpen(HomeAssistantError):
    """Raised when repeated failures have temporarily disabled actuation."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    entity_id: str
    success: bool
    attempted: bool
    detail: str = ""


class HomeAssistantClient:
    """Small REST client with fail-closed writes."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout_seconds: float = 10.0,
        dry_run: bool = True,
        failure_cooldown_seconds: int = 900,
        max_failures: int = 3,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise HomeAssistantError("base_url must be http(s)")
        if parsed.scheme == "http" and (parsed.hostname or "").lower() not in _LOCAL_HOSTS:
            raise HomeAssistantError("plain HTTP is only allowed for local Home Assistant hosts")
        if parsed.username or parsed.password or parsed.fragment:
            raise HomeAssistantError("base_url must not contain credentials or fragments")
        self.base_url = base_url.rstrip("/")
        self.token = token if token is not None else (
            os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN") or ""
        )
        self.timeout_seconds = timeout_seconds
        self.dry_run = dry_run
        self.cooldown = failure_cooldown_seconds
        self.max_failures = max_failures
        self._consecutive_failures = 0
        self._open_until = 0.0

    def redact(self, text: str) -> str:
        """Remove the token from any string before it is logged or returned."""
        if self.token:
            return text.replace(self.token, "***REDACTED***")
        return text

    @staticmethod
    def validate_entity_id(entity_id: str, allowed: set[str] | None = None) -> str:
        if not isinstance(entity_id, str) or not _ENTITY_RE.fullmatch(entity_id):
            raise HomeAssistantError(f"unsafe or unsupported entity_id: {entity_id!r}")
        if allowed is not None and entity_id not in allowed:
            raise HomeAssistantError(f"entity_id not in allow-list: {entity_id!r}")
        return entity_id

    def _require_open_circuit(self) -> None:
        if time.monotonic() < self._open_until:
            raise CircuitOpen("actuation disabled after repeated failures (circuit open)")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        self._require_open_circuit()
        if not self.token:
            raise HomeAssistantError("no Home Assistant token available")
        url = self.base_url + path
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            # Scheme and host are validated before this call.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=self.timeout_seconds
            ) as response:
                body = response.read(1_000_001)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._record_failure()
            raise HomeAssistantError(
                f"Home Assistant request failed: {type(exc).__name__}"
            ) from None
        if len(body) > 1_000_000:
            self._record_failure()
            raise HomeAssistantError("Home Assistant response too large")
        self._consecutive_failures = 0
        if not body:
            return {}
        try:
            result = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            self._record_failure()
            raise HomeAssistantError("Home Assistant returned invalid JSON") from None
        # HA service calls return a JSON array of changed entities; state reads
        # return an object. Both are valid, successful responses.
        if isinstance(result, list):
            return {}
        if not isinstance(result, dict):
            raise HomeAssistantError("Home Assistant returned unexpected JSON type")
        return result

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.max_failures:
            self._open_until = time.monotonic() + self.cooldown
            self._consecutive_failures = 0

    def get_state(self, entity_id: str) -> dict:
        self.validate_entity_id(entity_id)
        return self._request("GET", "/api/states/" + urllib.parse.quote(entity_id, safe="."))

    def set_value(self, entity_id: str, value: float, *, allowed: set[str] | None = None) -> WriteResult:
        """Write a numeric value to an input_number/number entity."""
        self.validate_entity_id(entity_id, allowed)
        if not math.isfinite(value):
            raise HomeAssistantError("refusing to write a non-finite value")
        if self.dry_run:
            return WriteResult(entity_id, success=True, attempted=False, detail="dry-run")
        domain = entity_id.split(".", 1)[0]
        if domain not in _WRITE_DOMAINS:
            raise HomeAssistantError(f"domain {domain!r} is not writable")
        try:
            payload = {"entity_id": entity_id, "value": float(value)}
            self._request("POST", f"/api/services/{domain}/set_value", payload)
        except HomeAssistantError as exc:
            return WriteResult(entity_id, success=False, attempted=True, detail=self.redact(str(exc)))
        return WriteResult(entity_id, success=True, attempted=True)

    def call_service(self, domain: str, service: str, data: dict) -> bool:
        """Call an allow-listed Home Assistant service (scripts, helpers)."""
        if domain not in _SERVICE_DOMAINS:
            raise HomeAssistantError(f"service domain {domain!r} is not allowed")
        if not re.fullmatch(r"[a-z0-9_]+", service):
            raise HomeAssistantError("unsafe service name")
        if self.dry_run:
            return True
        try:
            self._request("POST", f"/api/services/{domain}/{service}", data)
        except HomeAssistantError:
            return False
        return True

    def set_datetime(self, entity_id: str, timestamp: str) -> bool:
        """Set an input_datetime helper (used by the stale-plan watchdog)."""
        if not re.fullmatch(r"input_datetime\.[a-z0-9_]+", entity_id):
            raise HomeAssistantError(f"unsafe or unsupported entity_id: {entity_id!r}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2})?", timestamp):
            raise HomeAssistantError("invalid watchdog timestamp")
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            raise HomeAssistantError("invalid watchdog timestamp") from None
        return self.call_service("input_datetime", "set_datetime", {
            "entity_id": entity_id, "datetime": timestamp,
        })

    def reset_breaker(self) -> None:
        """Manual reset, used by the operator after fixing connectivity."""
        self._open_until = 0.0
        self._consecutive_failures = 0
