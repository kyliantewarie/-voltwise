"""Regression tests for the runtime/packaging adversarial findings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_config_dict

from voltwise import service as service_module
from voltwise.webui import render_plan_html, serve


class FakeStop:
    """Standalone stop signal; avoids patching threading.Event globally."""

    def __init__(self):
        self.value = False

    def is_set(self):
        return self.value

    def set(self):
        self.value = True

    def clear(self):
        self.value = False

    def wait(self, timeout=None):
        return self.value


@pytest.mark.parametrize("plan", [
    {"status": "OK", "slots": "corrupted"},
    {"status": "OK", "slots": None},
    {"status": "OK", "slots": [None]},
    {"status": "OK", "slots": ["abc"]},
    {"status": "OK", "slots": {"0": {}}},
    {"status": "OK", "warnings": 5, "slots": []},
    {"status": "OK", "warnings": None, "slots": []},
    {"status": 42, "slots": [{}]},
])
def test_malformed_plan_never_crashes_renderer(plan):
    html = render_plan_html(plan)
    assert "VoltWise" in html


def test_ipv6_loopback_is_not_accepted():
    with pytest.raises(ValueError):
        serve(Path("plan.json"), host="::1")


def test_server_uses_daemon_threads():
    server = serve(Path("plan.json"), port=0)
    assert server.daemon_threads is True
    server.server_close()


def test_cli_main_catches_transient_oserror(tmp_path: Path, monkeypatch):
    from voltwise import cli

    path = tmp_path / "config.json"
    path.write_text(json.dumps(make_config_dict()), encoding="utf-8")
    monkeypatch.setattr(cli, "run_once", lambda args: (_ for _ in ()).throw(OSError("disk busy")))
    assert cli.main(["--config", str(path)]) == 2


def test_service_survives_transient_cycle_error(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(make_config_dict()), encoding="utf-8")
    calls = []

    def failing_cycle(args):
        calls.append(args.ha_client)
        raise OSError("transient")

    monkeypatch.setattr(service_module, "run_once", failing_cycle)
    stop = FakeStop()
    monkeypatch.setattr(service_module, "_STOP", stop)
    stop.wait = lambda timeout=None: stop.set()
    assert service_module.main([
        "--config", str(path), "--output", str(tmp_path / "plan.json"),
        "--web-port", "0", "--interval-seconds", "60",
    ]) == 0
    assert len(calls) == 1


def test_service_reuses_one_ha_client(tmp_path: Path, monkeypatch):
    data = make_config_dict()
    data["actuation"] = {"enabled": True, "ha_url": "http://homeassistant:8123"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    clients = []

    def record_cycle(args):
        clients.append(args.ha_client)
        if len(clients) == 2:
            service_module._STOP.set()
        return 0

    monkeypatch.setattr(service_module, "run_once", record_cycle)
    monkeypatch.setattr(service_module, "_STOP", FakeStop())
    assert service_module.main([
        "--config", str(path), "--output", str(tmp_path / "plan.json"),
        "--web-port", "0", "--apply", "--interval-seconds", "60",
    ]) == 0
    assert len(clients) == 2
    assert clients[0] is not None and clients[0] is clients[1]
