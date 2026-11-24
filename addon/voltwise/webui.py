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


_STYLES = """
:root{
  --bg:#111;--card:#1c1c1c;--card2:#282828;--text:#e1e1e1;--muted:#9b9b9b;
  --line:rgba(225,225,225,.12);--grid:#488fc2;--export:#a280db;--solar:#ff9800;
  --bat-out:#4db6ac;--bat-in:#f06292;--good:#0f9d58;--warn:#ffb74d;--bad:#ef5350;
  --radius:16px;--gap:12px;color-scheme:dark;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.app{max-width:1180px;margin:0 auto;padding:20px 20px 84px}
.topbar{display:flex;align-items:center;gap:14px;margin:4px 0 20px}
.logo{width:46px;height:46px;border-radius:50%;display:grid;place-items:center;
  background:rgba(255,152,0,.18);color:var(--solar);font-size:23px}
h1{font-size:24px;margin:0;line-height:1.1;letter-spacing:-.3px}
.subtitle{color:var(--muted);font-size:13px;margin:3px 0 0}
.pill{margin-left:auto;font-size:12px;font-weight:600;letter-spacing:.5px;
  padding:6px 14px;border-radius:999px;background:rgba(255,255,255,.08);color:var(--muted)}
.pill.OPTIMAL{background:rgba(15,157,88,.18);color:#7bd8a3}
.pill.DEGRADED{background:rgba(255,183,77,.18);color:var(--warn)}
.pill.FALLBACK,.pill.INVALID{background:rgba(239,83,80,.18);color:#ff9b98}
.section{background:rgba(255,255,255,.025);border-radius:20px;padding:14px;margin-bottom:14px}
.section>h2{font-size:12px;font-weight:600;letter-spacing:.8px;color:var(--muted);
  text-transform:uppercase;margin:2px 0 12px}
.grid{display:grid;gap:var(--gap);grid-template-columns:repeat(auto-fit,minmax(min(100%,250px),1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px;box-shadow:0 2px 8px rgba(0,0,0,.35)}
.tile{display:flex;flex-direction:column;gap:8px;min-height:132px;--tone:var(--solar)}
.icon{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;
  background:color-mix(in srgb,var(--tone) 22%,transparent);color:var(--tone);font-size:20px}
.label{font-size:12px;letter-spacing:.4px;color:var(--muted)}
.value{font-size:32px;font-weight:600;line-height:1.1;font-variant-numeric:tabular-nums}
.value small{font-size:14px;color:var(--muted);font-weight:400}
.sub{font-size:12px;color:var(--muted)}
.hero{grid-column:span 2}
@media(max-width:700px){.hero{grid-column:span 1}.app{padding:12px 12px 84px}.value{font-size:27px}}
.ringwrap{display:flex;align-items:center;gap:16px}
.ring{width:104px;aspect-ratio:1;border-radius:50%;flex:none;display:grid;place-items:center;
  position:relative;background:conic-gradient(var(--bat-out) calc(var(--soc)*1%),rgba(255,255,255,.08) 0)}
.ring::before{content:"";position:absolute;inset:9px;border-radius:50%;background:var(--card)}
.ring span{position:relative;font-size:23px;font-weight:600;font-variant-numeric:tabular-nums}
.banner{padding:12px 16px;border-radius:12px;margin-bottom:12px;font-size:13px}
.banner.warn{background:rgba(255,183,77,.12);color:var(--warn);border:1px solid rgba(255,183,77,.25)}
.chart{width:100%;height:auto;display:block}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:8px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}
.xaxis{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:2px}
.flow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:center}
.node{min-width:104px;text-align:center;padding:12px;border-radius:14px;
  background:var(--card2);border:1px solid var(--line)}
.node b{display:block;font-size:11px;letter-spacing:.6px;color:var(--muted);font-weight:600}
.node strong{display:block;font-size:18px;font-variant-numeric:tabular-nums;margin-top:4px}
.node small{color:var(--muted);font-size:11px}
.arrow{color:var(--muted);font-size:20px}
table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:500;text-align:right;padding:8px 6px;border-bottom:1px solid var(--line)}
td{padding:8px 6px;border-bottom:1px solid rgba(225,225,225,.06);text-align:right}
th:first-child,td:first-child{text-align:left}
tr.charging td:nth-child(3){color:var(--bat-in)}
tr.discharging td:nth-child(3){color:var(--bat-out)}
.scroll{overflow-x:auto}
details.card summary{cursor:pointer;font-weight:600;color:var(--warn);list-style:none}
details.card summary::-webkit-details-marker{display:none}
details.card ul{margin:10px 0 0;padding-left:18px;color:var(--warn)}
nav{position:sticky;bottom:0;display:flex;justify-content:space-around;gap:4px;
  background:rgba(28,28,28,.92);backdrop-filter:blur(10px);
  border-top:1px solid var(--line);margin:0 -20px -84px;padding:6px 12px}
nav a{min-width:70px;min-height:48px;display:grid;place-items:center;text-decoration:none;
  background:none;color:var(--muted);font-size:11px;border-radius:10px}
nav a:hover{background:rgba(255,255,255,.06);color:var(--text)}
footer{text-align:center;color:var(--muted);font-size:11px;margin-top:18px}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""


def _chart_svg(slots: list[dict[str, Any]]) -> str:
    """Build a zero-JS SVG chart: price area, battery bars, SOC line."""
    if not slots:
        return '<p class="sub">No intervals in this plan.</p>'
    prices = [_safe_float(slot.get("price")) for slot in slots]
    battery = [_safe_float(slot.get("battery_kw")) for slot in slots]
    socs = [_safe_float(slot.get("battery_soc_pct")) for slot in slots]
    low, high = min(prices), max(prices)
    spread = (high - low) or 1.0
    max_battery = max([abs(value) for value in battery] + [1.0])
    step = 1000.0 / len(slots)

    def xpos(index: int) -> float:
        return step * (index + 0.5)

    points = [(xpos(i), 100.0 - (price - low) / spread * 78.0)
              for i, price in enumerate(prices)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = (f"M {points[0][0]:.1f},112 " +
            " ".join(f"L {x:.1f},{y:.1f}" for x, y in points) +
            f" L {points[-1][0]:.1f},112 Z")
    bars = []
    for i, power in enumerate(battery):
        height = abs(power) / max_battery * 34.0
        y = 172.0 - height if power > 0 else 172.0
        colour = "var(--bat-in)" if power > 0 else "var(--bat-out)"
        bars.append(
            f'<rect x="{xpos(i) - step * 0.3:.1f}" y="{y:.1f}" '
            f'width="{step * 0.6:.1f}" height="{height:.1f}" rx="2" '
            f'fill="{colour}" opacity=".85"/>'
        )
    soc_line = " ".join(
        f"{xpos(i):.1f},{208.0 - min(100.0, max(0.0, soc)) / 100.0 * 58.0:.1f}"
        for i, soc in enumerate(socs)
    )
    return f'''<svg class="chart" viewBox="0 0 1000 250" preserveAspectRatio="none"
  role="img" aria-label="Price, battery power and state of charge for each interval">
  <defs><linearGradient id="pricefill" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="var(--solar)" stop-opacity=".38"/>
    <stop offset="1" stop-color="var(--solar)" stop-opacity="0"/>
  </linearGradient></defs>
  <line x1="0" y1="112" x2="1000" y2="112" stroke="var(--line)"/>
  <line x1="0" y1="172" x2="1000" y2="172" stroke="var(--line)"/>
  <path d="{area}" fill="url(#pricefill)"/>
  <polyline points="{line}" fill="none" stroke="var(--solar)" stroke-width="2.5" stroke-linejoin="round"/>
  {''.join(bars)}
  <polyline points="{soc_line}" fill="none" stroke="var(--grid)" stroke-width="2" stroke-dasharray="5 4"/>
  <text x="8" y="18" fill="var(--muted)" font-size="12">Price</text>
  <text x="8" y="150" fill="var(--muted)" font-size="12">Battery power</text>
  <text x="8" y="232" fill="var(--muted)" font-size="12">State of charge</text>
</svg>'''


def render_plan_html(plan: dict[str, Any]) -> str:
    """Render a rich, Mobile-First inspired plan view, robust to corrupt input."""
    plan = _as_dict(plan)
    raw_status = plan.get("status", "UNKNOWN")
    status = _escape(raw_status if isinstance(raw_status, str) else "INVALID")
    raw_warnings = plan.get("warnings", [])
    if not isinstance(raw_warnings, (list, tuple)):
        raw_warnings = []
    warnings = [_escape(str(item)) for item in raw_warnings
                if isinstance(item, (str, int, float))]
    raw_slots = plan.get("slots", [])
    if not isinstance(raw_slots, (list, tuple)):
        raw_slots = []
    slots = [_as_dict(item) for item in raw_slots]

    total_cost = _safe_float(plan.get("total_cost"))
    baseline = _safe_float(plan.get("baseline_cost"))
    savings = _safe_float(plan.get("savings"))
    savings_pct = _safe_float(plan.get("savings_pct"))

    created = plan.get("created_at")
    staleness = ""
    if isinstance(created, str):
        try:
            moment = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - moment).total_seconds() / 60.0
            if age > 30:
                staleness = (f'<div class="banner warn">This plan is {age:.0f} minutes old. '
                             f'Check the add-on log.</div>')
        except ValueError:
            staleness = '<div class="banner warn">Plan timestamp is invalid.</div>'

    soc_now = _safe_float(slots[0].get("battery_soc_pct")) if slots else 0.0
    next_power = _safe_float(slots[0].get("battery_kw")) if slots else 0.0
    next_ev = _safe_float(slots[0].get("ev_kw")) if slots else 0.0
    direction = "Charging" if next_power > 0.05 else ("Discharging" if next_power < -0.05 else "Idle")
    direction_colour = "var(--bat-in)" if next_power > 0.05 else (
        "var(--bat-out)" if next_power < -0.05 else "var(--muted)"
    )
    prices = [_safe_float(s.get("price")) for s in slots]
    mean_price = sum(prices) / len(prices) if prices else 0.0
    peak = max(prices) if prices else 0.0
    off_peak = min(prices) if prices else 0.0

    rows = []
    for slot in slots:
        power = _safe_float(slot.get("battery_kw"))
        css = "charging" if power > 0.05 else ("discharging" if power < -0.05 else "")
        rows.append(
            f'<tr class="{css}"><td>{_escape(slot.get("timestamp", ""))}</td>'
            f'<td>{_safe_float(slot.get("price")):.3f}</td>'
            f'<td>{power:+.2f}</td>'
            f'<td>{_safe_float(slot.get("grid_import_kw")):.2f}</td>'
            f'<td>{_safe_float(slot.get("grid_export_kw")):.2f}</td>'
            f'<td>{_safe_float(slot.get("battery_soc_pct")):.1f}%</td>'
            f'<td>{_safe_float(slot.get("cost")):+.3f}</td></tr>'
        )

    labels = "".join(
        f'<span>{_escape(str(slot.get("timestamp", ""))[11:16])}</span>'
        for slot in slots[::max(1, len(slots) // 8)] if slot.get("timestamp")
    )

    warning_section = ""
    if warnings:
        items = "".join(f"<li>{warning}</li>" for warning in warnings)
        warning_section = (
            f'<details class="card" open><summary>{len(warnings)} notice(s)</summary>'
            f'<ul>{items}</ul></details>'
        )

    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VoltWise — Energy optimiser</title><style>{_STYLES}</style></head>
<body><div class="app">
  <header class="topbar">
    <div class="logo">&#9889;</div>
    <div><h1>VoltWise</h1><p class="subtitle">Dynamic-tariff battery &amp; EV optimiser</p></div>
    <span class="pill {status}">{status}</span>
  </header>
  {staleness}
  {warning_section}
  <section class="section" id="overview">
    <h2>Overview</h2>
    <div class="grid">
      <article class="card tile hero" style="--tone:var(--good)">
        <div class="row"><span class="icon">&#128200;</span></div>
        <div class="label">Estimated savings</div>
        <div class="value">{savings:.2f} <small>vs baseline</small></div>
        <div class="sub">{savings_pct:.1f}% cheaper than the reference plan</div>
      </article>
      <article class="card tile" style="--tone:var(--grid)">
        <div class="label">Planned cost</div>
        <div class="value">{total_cost:.2f}</div>
        <div class="sub">reference {baseline:.2f}</div>
      </article>
      <article class="card tile" style="--tone:var(--bat-out)">
        <div class="label">Battery state of charge</div>
        <div class="ringwrap">
          <div class="ring" style="--soc:{min(100.0, max(0.0, soc_now)):.1f}">
            <span>{soc_now:.0f}%</span>
          </div>
          <div><div class="value" style="font-size:22px">{next_power:+.2f} <small>kW</small></div>
          <div class="sub" style="color:{direction_colour}">{direction}</div>
          <div class="sub">EV {next_ev:+.2f} kW</div></div>
        </div>
      </article>
      <article class="card tile" style="--tone:var(--solar)">
        <div class="label">Tariff</div>
        <div class="value">{mean_price:.3f}</div>
        <div class="sub">peak {peak:.3f} &middot; low {off_peak:.3f}</div>
      </article>
    </div>
  </section>
  <section class="section" id="schedule">
    <h2>Schedule</h2>
    <div class="card">
      {_chart_svg(slots)}
      <div class="xaxis">{labels}</div>
      <div class="legend">
        <span><i class="dot" style="background:var(--solar)"></i>Price</span>
        <span><i class="dot" style="background:var(--bat-in)"></i>Charging</span>
        <span><i class="dot" style="background:var(--bat-out)"></i>Discharging</span>
        <span><i class="dot" style="background:var(--grid)"></i>State of charge</span>
      </div>
    </div>
  </section>
  <section class="section" id="flow">
    <h2>Power flow — next interval</h2>
    <div class="card flow">
      <div class="node"><b>GRID</b><strong>{_safe_float(slots[0].get("grid_import_kw")) if slots else 0:.2f} kW</strong><small>import</small></div>
      <span class="arrow">&#8594;</span>
      <div class="node"><b>HOME</b><strong>{_safe_float(slots[0].get("consumption_kw")) if slots else 0:.2f} kW</strong><small>load</small></div>
      <span class="arrow">&#8592;</span>
      <div class="node"><b>SOLAR</b><strong>{_safe_float(slots[0].get("solar_kw")) if slots else 0:.2f} kW</strong><small>production</small></div>
      <span class="arrow">&#8594;</span>
      <div class="node"><b>BATTERY</b><strong>{next_power:+.2f} kW</strong><small>{direction}</small></div>
    </div>
  </section>
  <section class="section" id="intervals">
    <h2>Intervals</h2>
    <div class="card scroll"><table>
      <thead><tr><th>Time</th><th>Price</th><th>Battery kW</th><th>Import kW</th>
      <th>Export kW</th><th>SOC</th><th>Cost</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></div>
  </section>
  <footer>Reference method: {_escape(str(plan.get("baseline_method", "modelled estimate")))}</footer>
</div>
<nav>
  <a href="#overview">Overview</a><a href="#schedule">Schedule</a>
  <a href="#flow">Flow</a><a href="#intervals">Intervals</a>
</nav>
</body></html>'''


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
