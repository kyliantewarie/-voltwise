"""Read-only web UI tests (escaping, routes, loopback binding)."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from voltwise.webui import _is_allowed_client, create_handler, render_plan_html, serve


def test_html_escapes_untrusted_values():
    html = render_plan_html({
        "status": "FALLBACK",
        "warnings": ["<script>alert(1)</script>"],
        "slots": [{"timestamp": "<img src=x>", "price": 1, "battery_kw": 1,
                   "grid_import_kw": 1, "grid_export_kw": 0, "battery_soc_pct": 50, "cost": 1}],
    })
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html
    assert "&lt;img" in html


def test_missing_plan_is_handled():
    assert "NO_PLAN" in render_plan_html({"status": "NO_PLAN", "warnings": [], "slots": []})


def test_server_routes_and_headers(tmp_path: Path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({"status": "OPTIMAL", "warnings": [], "slots": []}), encoding="utf-8")
    server = serve(plan_file, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"status": "ok"}
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("Content-Security-Policy")

        connection.request("GET", "/api/plan")
        response = connection.getresponse()
        assert json.loads(response.read())["status"] == "OPTIMAL"

        connection.request("GET", "/")
        response = connection.getresponse()
        assert b"VoltWise" in response.read()

        connection.request("GET", "/etc/passwd")
        assert connection.getresponse().status == 404
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_container_bind_requires_explicit_mode():
    with pytest.raises(ValueError, match="container_mode"):
        serve(Path("plan.json"), host="0.0.0.0")
    server = serve(Path("plan.json"), host="0.0.0.0", container_mode=True)
    assert server.server_address[1] > 0
    server.server_close()


def test_source_ip_filter():
    assert _is_allowed_client("127.0.0.1")
    assert _is_allowed_client("172.30.0.4")
    assert not _is_allowed_client("8.8.8.8")
    assert not _is_allowed_client("not-an-ip")


def test_malformed_plan_does_not_crash_renderer():
    html = render_plan_html({
        "status": "OPTIMAL", "warnings": [],
        "slots": [{"timestamp": "bad", "price": None, "battery_kw": "oops",
                   "grid_import_kw": {}, "grid_export_kw": [], "battery_soc_pct": "x",
                   "cost": float("nan")}],
    })
    assert "VoltWise" in html


def test_dashboard_has_hero_chart_and_navigation():
    html = render_plan_html({
        "status": "OPTIMAL", "warnings": [], "baseline_method": "test_v1",
        "slots": [{"timestamp": "2026-11-24T10:00:00+00:00", "price": 0.1,
                   "battery_kw": 1.0, "battery_soc_pct": 50,
                   "grid_import_kw": 0.5, "grid_export_kw": 0,
                   "solar_kw": 0, "consumption_kw": 0.5, "cost": 0.1}],
    })
    assert "Estimated savings" in html
    assert "conic-gradient" in html          # SOC donut ring
    assert "<svg" in html and "Battery power" in html  # schedule chart
    assert 'href="#schedule"' in html        # sticky section navigation
    assert "Power flow" in html
    assert "test_v1" in html


def test_handler_factory_is_callable():
    assert callable(create_handler(Path("plan.json")))
