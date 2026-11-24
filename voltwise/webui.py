"""Read-only ingress web UI.

The server binds to loopback only (Home Assistant ingress proxies to it), serves
no files from disk, and renders every value through ``html.escape``. There are
no write endpoints: actuation happens exclusively through the audited CLI path.
"""

from __future__ import annotations

import html
import ipaddress
import json
import socket
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_SAFE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Never let a malformed plan file crash the monitoring UI."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def _is_allowed_client(address: str) -> bool:
    """Only loopback and RFC1918 clients (the Supervisor ingress proxy) may connect."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def render_plan_html(plan: dict[str, Any]) -> str:
    """Render even a partially corrupted plan without raising."""
    plan = _as_dict(plan)
    raw_status = plan.get("status", "UNKNOWN")
    status = _escape(raw_status if isinstance(raw_status, str) else "INVALID")
    raw_warnings = plan.get("warnings", [])
    if not isinstance(raw_warnings, (list, tuple)):
        raw_warnings = []
    warnings = [str(item) for item in raw_warnings if isinstance(item, (str, int, float))]
    raw_slots = plan.get("slots", [])
    if not isinstance(raw_slots, (list, tuple)):
        raw_slots = []
    rows: list[str] = []
    for slot in (_as_dict(item) for item in raw_slots):
        rows.append(
            "<tr><td>{}</td><td>{:.2f}</td><td>{:.2f}</td><td>{:.2f}</td>"
            "<td>{:.2f}</td><td>{:.1f}</td><td>{:.2f}</td></tr>".format(
                _escape(slot.get("timestamp", "")),
                _safe_float(slot.get("price", 0)),
                _safe_float(slot.get("battery_kw", 0)),
                _safe_float(slot.get("grid_import_kw", 0)),
                _safe_float(slot.get("grid_export_kw", 0)),
                _safe_float(slot.get("battery_soc_pct", 0)),
                _safe_float(slot.get("cost", 0)),
            )
        )
    warning_html = "".join(f"<li>{_escape(w)}</li>" for w in warnings)

    created = plan.get("created_at")
    staleness = ""
    if isinstance(created, str):
        try:
            moment = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            age_minutes = (datetime.now(UTC) - moment).total_seconds() / 60.0
            if age_minutes > 30:
                staleness = f'<p class="warn">Plan is {age_minutes:.0f} minutes old. Check the add-on log.</p>'
        except ValueError:
            staleness = '<p class="warn">Plan timestamp is invalid.</p>'

    first = [item for item in raw_slots if isinstance(item, dict)]
    next_slot = ""
    if first:
        slot = _as_dict(first[0])
        next_slot = (
            "<p>Next setpoint: battery <strong>{:.2f} kW</strong> ({}), "
            "EV <strong>{:.2f} kW</strong>, target SOC <strong>{:.1f}%</strong></p>"
        ).format(
            _safe_float(slot.get("battery_kw", 0)),
            "charge" if _safe_float(slot.get("battery_kw", 0)) > 0 else "discharge/idle",
            _safe_float(slot.get("ev_kw", 0)),
            _safe_float(slot.get("battery_soc_pct", 0)),
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VoltWise plan</title>
<style>
body{{font-family:system-ui,sans-serif;margin:1.5rem;background:#0f1115;color:#e6e8ee}}
table{{border-collapse:collapse;width:100%;font-size:0.85rem}}
th,td{{padding:0.35rem 0.5rem;border-bottom:1px solid #2a2f3a;text-align:right}}
th:first-child,td:first-child{{text-align:left}}
.badge{{padding:0.2rem 0.6rem;border-radius:0.4rem;background:#1d6b45}}
.warn{{color:#ffcf70}}
</style></head><body>
<h1>VoltWise <span class="badge">{status}</span></h1>
<p>Total cost: {_safe_float(plan.get('total_cost', 0)):.2f} &nbsp; Baseline: {_safe_float(plan.get('baseline_cost', 0)):.2f}
&nbsp; Savings: <strong>{_safe_float(plan.get('savings', 0)):.2f}</strong> ({_safe_float(plan.get('savings_pct', 0)):.2f}%)</p>
{staleness}{next_slot}
{f'<ul class="warn">{warning_html}</ul>' if warning_html else ''}
<table><thead><tr><th>Time</th><th>Price</th><th>Battery kW</th><th>Import kW</th>
<th>Export kW</th><th>SOC %</th><th>Cost</th></tr></thead><tbody>
{''.join(rows)}</tbody></table>
</body></html>"""


class VoltWiseHTTPServer(ThreadingHTTPServer):
    """Tracks live connections so shutdown does not leave blocked workers."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # socketserver calls this on the SERVER, not on the request handler.
        # Abrupt disconnects from slow/browsing clients are expected noise.
        if isinstance(sys.exc_info()[1], (TimeoutError, ConnectionResetError,
                                         BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_connections: set[socket.socket] = set()

    def server_close(self) -> None:
        for connection in list(self.active_connections):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        super().server_close()


def create_handler(plan_path: Path, *, restrict_clients: bool = False):
    class PlanHandler(BaseHTTPRequestHandler):
        server_version = "VoltWise/0.1"
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.server.active_connections.add(self.connection)  # type: ignore[attr-defined]

        def handle_one_request(self) -> None:
            # Set immediately before each read: makefile() in setup() captures
            # the socket state, so doing this only in setup() is not enough.
            try:
                self.connection.settimeout(5.0)
            except OSError:
                pass
            super().handle_one_request()

        def finish(self) -> None:
            self.server.active_connections.discard(self.connection)  # type: ignore[attr-defined]
            super().finish()

        def _reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in _SAFE_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _load_plan(self) -> dict[str, Any]:
            if not plan_path.is_file():
                return {"status": "NO_PLAN", "warnings": ["No plan has been generated yet."], "slots": []}
            try:
                data = json.loads(plan_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {"status": "INVALID", "warnings": [], "slots": []}
            except (OSError, ValueError, RecursionError):
                return {"status": "INVALID", "warnings": ["Plan file could not be read."], "slots": []}

        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            if restrict_clients and not _is_allowed_client(self.client_address[0]):
                self._reply(403, b"Forbidden", "text/plain; charset=utf-8")
                return
            path = self.path.split("?", 1)[0]
            if path in {"/", "/index.html"}:
                body = render_plan_html(self._load_plan()).encode("utf-8")
                self._reply(200, body, "text/html; charset=utf-8")
            elif path == "/api/plan":
                body = json.dumps(self._load_plan()).encode("utf-8")
                self._reply(200, body, "application/json; charset=utf-8")
            elif path == "/healthz":
                self._reply(200, b'{"status":"ok"}', "application/json; charset=utf-8")
            else:
                self._reply(404, b"Not found", "text/plain; charset=utf-8")

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib API
            self.do_GET()

        def log_message(self, fmt: str, *args) -> None:
            # Do not log request headers (they may contain an ingress token).
            return

    return PlanHandler


def serve(
    plan_path: Path,
    port: int = 8099,
    host: str = "127.0.0.1",
    *,
    container_mode: bool = False,
) -> ThreadingHTTPServer:
    """Start the read-only UI.

    Local development binds to loopback. Inside the add-on container the server
    must listen on the container interface (0.0.0.0) because the Supervisor
    ingress proxy forwards to the container IP, not to loopback. In that mode a
    source-IP filter restricts access to private/loopback networks.
    """
    if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise ValueError("web UI host must be loopback or the container interface (IPv4 only)")
    if host == "0.0.0.0" and not container_mode:
        raise ValueError("0.0.0.0 requires container_mode with source-IP filtering")
    handler = create_handler(plan_path, restrict_clients=container_mode)
    server = VoltWiseHTTPServer((host, port), handler)
    return server


def main() -> None:  # pragma: no cover - entry point
    import os

    path = Path(os.environ.get("VOLTWISE_PLAN_PATH", "/data/voltwise/plan.json"))
    port = int(os.environ.get("VOLTWISE_WEB_PORT", "8099"))
    server = serve(path, port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
