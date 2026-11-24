"""Real-HTTP contract test against a local fake Home Assistant (no urlopen mocks)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from voltwise.ha_client import HomeAssistantClient


class FakeHAHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, dict | None]] = []
    fail = False

    def log_message(self, *args):
        return

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.calls.append(("GET", self.path, None))
        if self.path.startswith("/api/states/"):
            self._send(200, {
                "entity_id": self.path.rsplit("/", 1)[1],
                "state": "55.5",
                "last_updated": "2026-11-20T12:00:00+00:00",
            })
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw or b"{}")
        self.calls.append(("POST", self.path, payload))
        if self.fail:
            self._send(500, {"message": "boom"})
        else:
            self._send(200, [])


def make_server():
    FakeHAHandler.calls = []
    FakeHAHandler.fail = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHAHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_real_http_read_write_and_watchdog():
    server, port = make_server()
    try:
        client = HomeAssistantClient(
            f"http://127.0.0.1:{port}", token="test-token", dry_run=False
        )
        state = client.get_state("sensor.battery_soc")
        assert state["state"] == "55.5"
        assert client.set_value("number.voltwise_battery_power", 1.25).success
        assert client.set_datetime("input_datetime.voltwise_plan_expiry", "2030-01-01T00:00:00+00:00")
        assert client.call_service("script", "voltwise_apply", {"battery_power_kw": 1.0})
        posts = [call for call in FakeHAHandler.calls if call[0] == "POST"]
        paths = {path for _, path, _ in posts}
        assert "/api/services/number/set_value" in paths
        assert "/api/services/input_datetime/set_datetime" in paths
        assert "/api/services/script/voltwise_apply" in paths
        payloads = [payload for _, _, payload in posts if payload]
        assert all("test-token" not in json.dumps(payload) for payload in payloads)
    finally:
        server.shutdown()
        server.server_close()


def test_real_http_breaker_opens():
    server, port = make_server()
    FakeHAHandler.fail = True
    try:
        client = HomeAssistantClient(
            f"http://127.0.0.1:{port}", token="test-token", dry_run=False, max_failures=2
        )
        assert not client.set_value("number.voltwise_battery_power", 1.0).success
        assert not client.set_value("number.voltwise_battery_power", 2.0).success
        assert not client.set_value("number.voltwise_battery_power", 3.0).success
        assert len(FakeHAHandler.calls) == 2  # third attempt blocked before HTTP
    finally:
        server.shutdown()
        server.server_close()
