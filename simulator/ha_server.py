"""A small fake Home Assistant REST API wired to simulated devices."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .devices import SimulationState


class FakeHAHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: SimulationState
    dt_hours: float = 1.0
    fail_services: set[str] = set()

    def log_message(self, *args) -> None:
        return

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _state_of(self, entity_id: str) -> dict:
        now = datetime.now(UTC).isoformat()
        sim = self.state
        if entity_id == "sensor.battery_soc":
            value = sim.battery.soc_pct
        elif entity_id == "sensor.ev_soc":
            value = sim.ev.soc_pct
        elif entity_id == "sensor.voltwise_measured_import":
            value = sim.site.measured_import_kw
        elif entity_id == "sensor.battery_power":
            value = sim.battery.last_applied_kw
        elif entity_id in {
            "number.voltwise_battery_power", "input_number.voltwise_target_soc",
            "number.voltwise_ev_charge_power",
        }:
            return {
                "entity_id": entity_id,
                "state": str(self.state.setpoints.get(entity_id, 0.0)),
                "last_updated": now,
            }
        elif entity_id == "input_datetime.voltwise_plan_expiry":
            return {"entity_id": entity_id, "state": sim.plan_expiry, "last_updated": now}
        elif entity_id == "sensor.nord_pool_nl_today":
            rows = [
                {
                    "start": (self._price_start + timedelta(minutes=self._price_step * i))
                    .isoformat().replace("+00:00", "Z"),
                    "price": price * 1000.0,  # HA Nord Pool sensor uses EUR/MWh
                }
                for i, price in enumerate(self._prices)
            ]
            return {"entity_id": entity_id, "state": str(self._prices[0]),
                    "attributes": {"data": rows}, "last_updated": now}
        else:
            return {"entity_id": entity_id, "state": "unknown", "last_updated": now}
        return {"entity_id": entity_id, "state": str(value), "last_updated": now}

    def do_GET(self) -> None:
        if self.path.startswith("/api/states/"):
            entity_id = unquote(self.path[len("/api/states/"):])
            self._send(200, self._state_of(entity_id))
        elif self.path in {"/healthz", "/"}:
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length)) if length else {}
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "services"]:
            domain, service = parts[2], parts[3]
            key = f"{domain}.{service}"
            sim = self.state
            sim.service_calls.append((domain, service, payload))
            if key in self.fail_services:
                sim.failed_service_calls.append((domain, service, payload))
                self._send(500, {"message": "simulated failure"})
                return
            if domain in {"number", "input_number"} and service == "set_value":
                entity_id = payload.get("entity_id", "")
                sim.apply_setpoint(entity_id, float(payload.get("value", 0.0)), self.dt_hours)
            elif domain == "input_datetime" and service == "set_datetime":
                sim.plan_expiry = payload.get("datetime", sim.plan_expiry)
            elif domain == "mqtt" and service == "publish":
                sim.mqtt_messages.append(payload)
                topic = str(payload.get("topic", ""))
                name = topic.rsplit("/", 1)[-1]
                entity = {
                    "battery_power": "number.voltwise_battery_power",
                    "target_soc": "input_number.voltwise_target_soc",
                    "ev_power": "number.voltwise_ev_charge_power",
                }.get(name.replace("charge_", ""))
                if entity:
                    try:
                        sim.apply_setpoint(entity, float(payload.get("payload", 0)), self.dt_hours)
                    except (TypeError, ValueError):
                        pass
            elif domain == "modbus" and service == "write_register":
                entity_id = payload.get("entity_id", "")
                try:
                    sim.apply_setpoint(entity_id, float(payload.get("value", 0)), self.dt_hours)
                except (TypeError, ValueError):
                    pass
            elif domain == "script":
                for name, key in (
                    ("battery_power_kw", "number.voltwise_battery_power"),
                    ("target_soc_pct", "input_number.voltwise_target_soc"),
                    ("ev_power_kw", "number.voltwise_ev_charge_power"),
                ):
                    if name in payload:
                        sim.apply_setpoint(key, float(payload[name]), self.dt_hours)
            self._send(200, [])
            return
        self._send(404, {"message": "not found"})


def start_fake_ha(
    sim: SimulationState,
    *,
    prices: list[float] | None = None,
    dt_hours: float = 1.0,
    fail_services: set[str] | None = None,
    price_start: datetime | None = None,
    price_step_minutes: int = 60,
) -> tuple[ThreadingHTTPServer, str]:
    """Start the simulated HA server; return (server, base_url)."""
    handler = type("BoundHandler", (FakeHAHandler,), {
        "state": sim,
        "dt_hours": dt_hours,
        "fail_services": fail_services or set(),
        "_prices": prices or [0.1] * 48,
        "_price_start": price_start or datetime.now(UTC).replace(minute=0, second=0, microsecond=0),
        "_price_step": price_step_minutes,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="fake-ha").start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
