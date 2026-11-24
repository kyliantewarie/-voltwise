"""Tests for the second-pass functional additions."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import START, make_config, make_config_dict, make_forecasts

from voltwise.cli import main
from voltwise.config import VoltWiseConfig
from voltwise.forecasts import apply_live_soc, build_forecasts
from voltwise.models import PricePoint, slot_timestamps
from voltwise.tariffs import PriceFetchError, TariffConfig
from voltwise.webui import render_plan_html

NOW_ISO = datetime.now(UTC).isoformat()


class FakeClient:
    def __init__(self, states: dict[str, str] | None = None, *, dry_run: bool = True):
        self.states = states or {}
        self.dry_run = dry_run
        self.writes: list[tuple[str, float]] = []

    def get_state(self, entity_id):
        value = self.states[entity_id]
        return value if isinstance(value, dict) else {"state": value}

    def set_value(self, entity_id, value, allowed=None):
        self.writes.append((entity_id, value))
        from voltwise.ha_client import WriteResult
        return WriteResult(entity_id, True, not self.dry_run, "dry-run" if self.dry_run else "")

    def set_datetime(self, entity_id, timestamp):
        self.writes.append((entity_id, timestamp))
        return True

    def call_service(self, domain, service, data):
        self.writes.append((f"{domain}.{service}", data))
        return True


def test_apply_live_soc_updates_initial_soc():
    data = make_config_dict()
    data["battery"]["soc_entity"] = "sensor.battery_soc"
    config = VoltWiseConfig.from_dict(data)
    updated, warnings = apply_live_soc(config, FakeClient({"sensor.battery_soc": {"state": "35.5", "last_updated": NOW_ISO}}))
    assert updated.battery.initial_soc_pct == 35.5
    assert not warnings


def test_apply_live_soc_warns_below_reserve():
    data = make_config_dict()
    data["battery"]["soc_entity"] = "sensor.battery_soc"
    config = VoltWiseConfig.from_dict(data)
    updated, warnings = apply_live_soc(config, FakeClient({"sensor.battery_soc": {"state": "15", "last_updated": NOW_ISO}}))
    assert updated.battery.initial_soc_pct == 15.0
    assert any("below" in warning for warning in warnings)


def test_apply_live_soc_rejects_out_of_range():
    data = make_config_dict()
    data["battery"]["soc_entity"] = "sensor.battery_soc"
    config = VoltWiseConfig.from_dict(data)
    with pytest.raises(ValueError, match="outside"):
        apply_live_soc(config, FakeClient({"sensor.battery_soc": {"state": "200", "last_updated": NOW_ISO}}))


def test_provider_feed_in_prices_are_honoured():
    config = make_config()
    stamps = slot_timestamps(START, config.n_slots, 60)
    points = [PricePoint(stamps[i], 0.1, feed_in_price=0.04) for i in range(config.n_slots)]
    forecasts = build_forecasts(config, points)
    assert forecasts.feed_in_prices == (0.04,) * config.n_slots


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def test_tariff_from_wrong_window_is_rejected():
    from voltwise.tariffs import HomeAssistantTariffProvider

    cfg = TariffConfig(provider="home_assistant", price_entity="sensor.price")

    class Client:
        def get_state(self, entity):
            return {"attributes": {"data": [
                {"start": "2019-01-01T00:00:00Z", "price": 50.0},
            ]}}

    with pytest.raises(PriceFetchError):
        HomeAssistantTariffProvider(cfg, Client()).fetch(START, 1, 60)


def test_atomic_plan_write_leaves_no_temporary_file(tmp_path: Path):
    config_path = Path(__file__).resolve().parents[1] / "config.example.json"
    output = tmp_path / "plan.json"
    assert main(["--config", str(config_path), "--dry-run", "--output", str(output)]) == 0
    assert output.is_file()
    assert not (tmp_path / "plan.json.tmp").exists()


def test_safe_stop_writes_zero_power_on_fallback(tmp_path: Path, monkeypatch):
    from voltwise.safety import make_fallback_plan

    data = make_config_dict()
    data["ev"] = {"capacity_kwh": 60.0, "initial_soc_pct": 40.0,
                  "target_soc_pct": 80.0, "deadline_slot": 7,
                  "max_charge_kw": 11.0, "charge_efficiency": 0.95}
    data["actuation"] = {
        "enabled": True,
        "require_confirmation": False,
        "battery_power_entity": "number.voltwise_battery_power",
        "target_soc_entity": "input_number.voltwise_target_soc",
        "ev_power_entity": "number.voltwise_ev_charge_power",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    config = VoltWiseConfig.from_dict(data)
    forecasts = make_forecasts(config)
    fallback = make_fallback_plan(config, forecasts, "forced test failure")

    fake = FakeClient(dry_run=False)
    monkeypatch.setattr("voltwise.cli.HomeAssistantClient", lambda *a, **k: fake)
    monkeypatch.setattr("voltwise.cli.optimize", lambda *a, **k: fallback)

    assert main(["--config", str(config_path), "--apply"]) == 5
    assert ("number.voltwise_battery_power", 0.0) in fake.writes
    assert ("number.voltwise_ev_charge_power", 0.0) in fake.writes
    assert all(
        value == 0.0 or "target_soc" in entity or "expiry" in entity
        for entity, value in fake.writes
    )


def test_concurrent_plan_writes_do_not_collide(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "plan.json"
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "voltwise", "--config",
             str(root / "config.example.json"), "--dry-run", "--output", str(output)],
            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        for _ in range(6)
    ]
    errors = []
    for process in processes:
        _, stderr = process.communicate(timeout=120)
        if process.returncode != 0:
            errors.append(stderr.decode(errors="replace")[-500:])
    assert not errors, f"concurrent writes failed: {errors}"
    for _ in range(50):
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert len(payload["slots"]) == 8
    assert not list(tmp_path.glob("*.tmp"))


def test_web_ui_marks_stale_plans():
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    html = render_plan_html({"status": "OPTIMAL", "warnings": [], "slots": [], "created_at": old})
    assert "minutes old" in html
    assert "Next setpoint" not in html
