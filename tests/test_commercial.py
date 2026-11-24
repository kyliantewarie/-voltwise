"""Tests for licensing, onboarding, diagnostics and localisation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml
from conftest import make_config_dict, make_forecasts

from voltwise.config import VoltWiseConfig
from voltwise.diagnostics import build_bundle, write_bundle
from voltwise.forecasts import apply_live_soc
from voltwise.ha_client import HomeAssistantError
from voltwise.licensing import check_licence, decode_licence, encode_licence
from voltwise.observability import JsonFormatter, Metrics
from voltwise.optimizer import optimize
from voltwise.setup import build_starter_config, discover


class FakeClient:
    def __init__(self, entities):
        self.entities = entities
        self.dry_run = False

    def get_state(self, entity_id):
        if entity_id in self.entities:
            return {"state": str(self.entities[entity_id])}
        raise HomeAssistantError("not found")


def _test_keypair(monkeypatch):

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import voltwise.licensing as licensing

    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", licensing._b64url(public))
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return private


def test_licence_round_trip(monkeypatch):
    private = _test_keypair(monkeypatch)
    key = encode_licence({
        "sub": "customer-1", "features": ["core", "pro"],
        "exp": time.time() + 3600,
    }, private)
    parsed = decode_licence(key)
    assert parsed is not None and parsed.allows("pro")
    assert check_licence(key, "core")[0]
    body, signature = key.split(".", 1)
    tampered_body = ("A" if body[0] != "A" else "B") + body[1:]
    assert not check_licence(tampered_body + "." + signature, "core")[0]


def test_licence_is_bound_to_device(monkeypatch):
    private = _test_keypair(monkeypatch)
    key = encode_licence({
        "sub": "customer-1", "features": ["pro"],
        "exp": time.time() + 3600, "hwid": "device-A",
    }, private)
    assert check_licence(key, "pro", hwid="device-A") == (True, "valid")
    assert not check_licence(key, "pro", hwid="device-B")[0]


@pytest.mark.parametrize("bad_exp", [float("nan"), float("inf"), 10 ** 400])
def test_non_finite_or_huge_expiry_is_rejected(monkeypatch, bad_exp):
    private = _test_keypair(monkeypatch)
    if bad_exp != 10 ** 400:
        with pytest.raises(ValueError):
            encode_licence({"sub": "x", "features": ["pro"], "exp": bad_exp}, private)
        return
    key = encode_licence({"sub": "x", "features": ["pro"], "exp": bad_exp}, private)
    assert decode_licence(key) is None


@pytest.mark.parametrize("bad_hwid", ["", 0, False])
def test_falsy_hwid_claim_is_rejected(monkeypatch, bad_hwid):
    private = _test_keypair(monkeypatch)
    key = encode_licence({
        "sub": "x", "features": ["pro"], "exp": time.time() + 3600, "hwid": bad_hwid,
    }, private)
    assert decode_licence(key) is None


def test_key_malleability_is_rejected(monkeypatch):
    private = _test_keypair(monkeypatch)
    key = encode_licence({
        "sub": "x", "features": ["pro"], "exp": time.time() + 3600,
    }, private)
    body, signature = key.split(".", 1)
    assert not check_licence(body + "." + signature + ".", "pro")[0]
    assert not check_licence(body + "." + signature.replace("-", "+"), "pro")[0] if "-" in signature else True


@pytest.mark.parametrize("bad_key", [123, [], {}, True, 1.5])
def test_non_string_licence_key_is_rejected(bad_key):
    assert not check_licence(bad_key, "pro", require=True)[0]


def test_premium_gating_ignores_require_flag(tmp_path, monkeypatch):
    from voltwise.cli import main

    data = make_config_dict()
    data["actuation"] = {"adapter": "mqtt", "adapter_topic_prefix": "voltwise/x"}
    data["licensing"] = {"require": False}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.delenv("VOLTWISE_LICENCE_KEY", raising=False)
    assert main(["--config", str(path), "--validate-only"]) == 2


@pytest.mark.licence_real_key
def test_shipped_public_key_rejects_attacker_key(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


    monkeypatch.delenv("VOLTWISE_LICENCE_KEY", raising=False)
    attacker = Ed25519PrivateKey.generate()
    pem = attacker.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key = encode_licence({"sub": "x", "features": ["pro"], "exp": time.time() + 3600}, pem)
    assert decode_licence(key) is None
    assert check_licence(key, "pro", require=True)[0] is False


def test_licence_missing_key_is_denied():
    assert not check_licence(None, "core", require=True)[0]


def test_licence_enforcement_off_without_key(monkeypatch):
    monkeypatch.delenv("VOLTWISE_LICENCE_KEY", raising=False)
    assert check_licence(None, "core")[0]


def test_watchdog_is_armed_before_setpoints_and_expired_on_failure():

    from voltwise.cli import _actuate

    data = make_config_dict()
    data["actuation"] = {
        "enabled": True,
        "battery_power_entity": "number.voltwise_battery_power",
        "target_soc_entity": "input_number.voltwise_target_soc",
        "ev_power_entity": "number.voltwise_ev_charge_power",
    }
    config = VoltWiseConfig.from_dict(data)
    plan = optimize(config, make_forecasts(config))

    class Client(FakeClient):
        def __init__(self, *, fail_datetime=False):
            super().__init__({})
            self.fail_datetime = fail_datetime
            self.order: list[str] = []

        def set_datetime(self, entity_id, timestamp):
            self.order.append("watchdog")
            return not self.fail_datetime

        def set_value(self, entity_id, value, allowed=None):
            self.order.append("setpoint")
            return __import__("voltwise.ha_client", fromlist=["WriteResult"]).WriteResult(
                entity_id, True, True
            )

    ok_client = Client()
    assert _actuate(config, ok_client, plan)
    assert ok_client.order[0] == "watchdog"

    bad_client = Client(fail_datetime=True)
    assert not _actuate(config, bad_client, plan)
    assert not any(item == "setpoint" for item in bad_client.order)


@pytest.mark.parametrize("bad_timestamp", [1234567890, 1.5, True, ["x"], {"a": 1}, None])
def test_non_string_soc_timestamp_is_rejected(bad_timestamp):
    data = make_config_dict()
    data["battery"]["soc_entity"] = "sensor.battery_soc"
    config = VoltWiseConfig.from_dict(data)
    client = FakeClient({"sensor.battery_soc": {"state": "90", "last_updated": bad_timestamp}})
    try:
        apply_live_soc(config, client, max_age_seconds=900)
        raise AssertionError("non-string timestamp accepted")
    except ValueError:
        pass


def test_web_ui_survives_deeply_nested_plan(tmp_path):
    import http.client
    import threading

    plan = tmp_path / "plan.json"
    plan.write_text('{"status":"OK","slots":' + "[" * 3000 + "0" + "]" * 3000 + "}", encoding="utf-8")
    import voltwise.webui as webui

    server = webui.serve(plan, port=0, host="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_file_is_atomic_and_windows_safe(tmp_path, monkeypatch):
    from voltwise.observability import Metrics

    target = tmp_path / "metrics.json"
    metrics = Metrics()
    metrics.write(target)
    assert target.is_file()
    assert not list(tmp_path.glob("*.tmp"))
    assert "generated_epoch" in target.read_text(encoding="utf-8")


def test_hacs_integration_has_config_flow_and_strings():
    root = Path(__file__).resolve().parents[1] / "custom_components" / "voltwise"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["config_flow"] is True
    assert (root / "config_flow.py").is_file()
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    assert "config" in strings
    assert (root / "translations" / "en.json").is_file()


def test_release_script_produces_checksums(tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audit"))
    from release import write_checksums

    target = write_checksums(tmp_path)
    assert target.is_file()
    assert "addon/config.yaml" in target.read_text(encoding="utf-8")


def test_plan_flags_baseline_as_estimate():
    config = VoltWiseConfig.from_dict(make_config_dict())
    plan = optimize(config, make_forecasts(config))
    assert plan.to_dict()["baseline_is_estimate"] is True


def test_watchdog_automation_covers_configured_entities(tmp_path):
    from voltwise.watchdog import generate_automation, write_automation

    data = make_config_dict()
    data["actuation"] = {
        "enabled": True,
        "adapter": "mqtt",
        "adapter_topic_prefix": "voltwise/myinv",
        "expiry_entity": "input_datetime.my_expiry",
    }
    config = VoltWiseConfig.from_dict(data)
    text = generate_automation(config)
    assert "input_datetime.my_expiry" in text
    assert "mqtt.publish" in text and "voltwise/myinv" in text
    assert "payload: '" in text  # zero setpoints for every adapter command
    path = write_automation(tmp_path / "automation.yaml", config)
    assert path.is_file()


def test_invalid_calendar_timestamp_is_rejected():
    from voltwise.ha_client import HomeAssistantClient, HomeAssistantError

    client = HomeAssistantClient("http://homeassistant:8123", token="x", dry_run=False)
    try:
        client.set_datetime("input_datetime.foo", "2026-13-99T99:99:99")
        raise AssertionError("invalid timestamp accepted")
    except HomeAssistantError:
        pass


def test_diagnostics_rejects_parent_traversal(tmp_path):
    from voltwise.cli import main

    data = make_config_dict()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert main(["--config", str(path), "--diagnostics", str(tmp_path / ".." / "escape.json")]) == 2


def test_cli_denies_unlicensed_run(tmp_path, monkeypatch):
    from voltwise.cli import main

    data = make_config_dict()
    data["licensing"] = {"require": True}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("VOLTWISE_LICENCE_SECRET", "test-secret")
    monkeypatch.delenv("VOLTWISE_LICENCE_KEY", raising=False)
    assert main(["--config", str(path), "--validate-only"]) == 2


def test_discovery_and_starter_config():
    client = FakeClient({
        "number.voltwise_battery_power": 0,
        "sensor.battery_soc": 50,
        "sensor.solar_power": 2,
        "sensor.home_consumption": 1,
    })
    found = discover(client)
    assert found["battery_power"] == "number.voltwise_battery_power"
    assert found["soc_sensor"] == "sensor.battery_soc"
    config = build_starter_config(found)
    assert VoltWiseConfig.from_dict(config).battery.soc_entity == "sensor.battery_soc"


def test_diagnostics_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLTWISE_TARIFF_API_KEY", "super-secret-key")
    data = make_config_dict()
    config = VoltWiseConfig.from_dict(data)
    bundle = build_bundle(config, None)
    text = json.dumps(bundle)
    assert "super-secret-key" not in text
    output = write_bundle(tmp_path / "bundle.json", config, None)
    assert output.is_file()


def test_metrics_and_json_formatter():
    metrics = Metrics()
    metrics.increment("cycles_total")
    metrics.set("last_ok", 1)
    assert metrics.snapshot()["cycles_total"] == 1
    import logging

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    assert json.loads(JsonFormatter().format(record))["message"] == "hello"


def test_translations_are_valid_supervisor_mappings():
    root = Path(__file__).resolve().parents[1] / "addon" / "translations"
    for name in ("en.yaml", "nl.yaml", "de.yaml"):
        data = yaml.safe_load((root / name).read_text(encoding="utf-8"))
        assert isinstance(data["configuration"], dict)
        for key, value in data["configuration"].items():
            assert isinstance(value, dict) and "name" in value and "description" in value, (name, key)
