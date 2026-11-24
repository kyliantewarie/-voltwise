"""Tests for the 23-point improvement programme (batch 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import make_config, make_config_dict, make_forecasts

from voltwise.cli import _actuate, safe_stop
from voltwise.config import VoltWiseConfig
from voltwise.economics import effective_reserve_pct_with_forecast, slot_cost
from voltwise.forecasts import apply_live_soc
from voltwise.ha_client import WriteResult
from voltwise.optimizer import optimize
from voltwise.safety import audit_plan


class FakeClient:
    def __init__(self, *, fail_on: str | None = None, states: dict | None = None):
        self.dry_run = False
        self.writes: list[tuple] = []
        self.calls: list[tuple] = []
        self.fail_on = fail_on
        self.states = states or {}

    def get_state(self, entity_id):
        if entity_id in self.states:
            return self.states[entity_id]
        raise KeyError(entity_id)

    def set_value(self, entity_id, value, allowed=None):
        if entity_id == self.fail_on:
            return WriteResult(entity_id, False, True, "injected failure")
        self.writes.append((entity_id, value))
        return WriteResult(entity_id, True, True)

    def set_datetime(self, entity_id, timestamp):
        self.writes.append((entity_id, timestamp))
        return True

    def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))
        return True


def with_actuation(**overrides):
    data = make_config_dict()
    data["actuation"] = {
        "enabled": True,
        "battery_power_entity": "number.voltwise_battery_power",
        "target_soc_entity": "input_number.voltwise_target_soc",
        "ev_power_entity": "number.voltwise_ev_charge_power",
        **overrides,
    }
    return VoltWiseConfig.from_dict(data)


def test_actuation_writes_watchdog_expiry():
    config = with_actuation()
    client = FakeClient()
    plan = optimize(config, make_forecasts(config))
    assert _actuate(config, client, plan)
    expiry = [entry for entry in client.writes if "expiry" in entry[0]]
    assert len(expiry) == 1
    moment = datetime.fromisoformat(expiry[0][1])
    assert moment > datetime.now(UTC)


def test_safe_stop_expires_watchdog():
    config = with_actuation()
    client = FakeClient()
    safe_stop(config, client, "test", allow_actuation=True)
    expiry = [entry for entry in client.writes if "expiry" in entry[0]]
    assert expiry and expiry[0][1].startswith("1970")


def test_actuation_rolls_back_on_partial_failure():
    config = with_actuation()
    previous = {
        "number.voltwise_battery_power": {"state": "1.5"},
        "input_number.voltwise_target_soc": {"state": "42.0"},
        "number.voltwise_ev_charge_power": {"state": "3.0"},
    }
    client = FakeClient(fail_on="input_number.voltwise_target_soc", states=previous)
    plan = optimize(config, make_forecasts(config))
    assert not _actuate(config, client, plan)
    assert ("number.voltwise_battery_power", 1.5) in client.writes


def test_telemetry_script_payload_separates_ev_power():
    from voltwise.forecasts import plan_commands

    data = make_config_dict()
    data["actuation"] = {
        "apply_script": "script.apply", "telemetry_enabled": True,
        "require_confirmation": False,
    }
    config = VoltWiseConfig.from_dict(data)
    plan = optimize(config, make_forecasts(config))
    commands = plan_commands(config, plan)
    setpoints = [c for c in commands if not c.description.startswith("telemetry:")]
    assert setpoints[0].description == "battery AC power (+ charge / - discharge)"
    assert not any("total cost" in c.description for c in setpoints)


def test_missing_telemetry_helpers_do_not_fail_actuation():
    from voltwise.ha_client import WriteResult

    config = with_actuation(telemetry_enabled=True)
    client = FakeClient()

    def set_value(entity_id, value, allowed=None):
        if "total_cost" in entity_id or "savings" in entity_id or "status_code" in entity_id:
            return WriteResult(entity_id, False, True, "not found")
        return WriteResult(entity_id, True, True)

    client.set_value = set_value
    plan = optimize(config, make_forecasts(config))
    assert _actuate(config, client, plan)


def test_telemetry_commands_include_cost_and_savings():
    from voltwise.forecasts import plan_commands

    config = with_actuation(telemetry_enabled=True)
    plan = optimize(config, make_forecasts(config))
    commands = plan_commands(config, plan)
    ids = {command.entity_id for command in commands}
    assert "input_number.voltwise_total_cost" in ids
    assert "input_number.voltwise_savings" in ids
    assert "input_number.voltwise_status_code" in ids


def test_watchdog_exception_refuses_actuation():
    from voltwise.ha_client import HomeAssistantError

    config = with_actuation()
    client = FakeClient()

    def raising(entity_id, timestamp):
        raise HomeAssistantError("helper unavailable")

    client.set_datetime = raising
    plan = optimize(config, make_forecasts(config))
    assert not _actuate(config, client, plan)
    assert not any("set_value" in str(entry) for entry in client.writes)


def test_non_finite_previous_state_does_not_break_rollback():
    from voltwise.ha_client import WriteResult

    config = with_actuation()

    class Client(FakeClient):
        def __init__(self):
            super().__init__()
            self.states = {"number.voltwise_battery_power": {"state": "NaN"}}
            self.fail_second = False

        def get_state(self, entity_id):
            if entity_id in self.states:
                return self.states[entity_id]
            return {"state": "NaN"}

        def set_datetime(self, entity_id, timestamp):
            return True

        def set_value(self, entity_id, value, allowed=None):
            if self.fail_second and entity_id == "input_number.voltwise_target_soc":
                self.fail_second = False
                return WriteResult(entity_id, False, True, "injected failure")
            self.writes.append((entity_id, value))
            return WriteResult(entity_id, True, True)

    client = Client()
    client.fail_second = True
    plan = optimize(config, make_forecasts(config))
    assert not _actuate(config, client, plan)
    assert ("number.voltwise_battery_power", 0.0) in client.writes


def test_atomic_apply_script_is_used():
    config = with_actuation(apply_script="script.voltwise_apply")
    client = FakeClient()
    plan = optimize(config, make_forecasts(config))
    assert _actuate(config, client, plan)
    assert client.calls and client.calls[0][:2] == ("script", "voltwise_apply")
    assert "battery_power_kw" in client.calls[0][2]


def test_mqtt_adapter_renders_topics():
    from voltwise.adapters import create_adapter

    config = with_actuation(adapter="mqtt", adapter_topic_prefix="voltwise/home/battery")
    plan = optimize(config, make_forecasts(config))
    calls = create_adapter("mqtt").render(config, plan_commands_for(config, plan))
    assert calls
    assert all(call.domain == "mqtt" and call.service == "publish" for call in calls)
    assert calls[0].data["topic"].startswith("voltwise/home/battery/")


def plan_commands_for(config, plan):
    from voltwise.forecasts import plan_commands
    return plan_commands(config, plan)


def test_modbus_adapter_renders_register_writes():
    from voltwise.adapters import create_adapter

    config = with_actuation(adapter="modbus", adapter_topic_prefix="voltwise/inverter")
    plan = optimize(config, make_forecasts(config))
    calls = create_adapter("modbus").render(config, plan_commands_for(config, plan))
    assert calls and all(call.domain == "modbus" for call in calls)


def test_unknown_adapter_is_rejected():
    data = make_config_dict()
    data["actuation"] = {"adapter": "zigbee"}
    try:
        VoltWiseConfig.from_dict(data)
        raise AssertionError("invalid adapter accepted")
    except ValueError:
        pass


def test_stale_soc_sensor_is_rejected():
    config = make_config_dict()
    config["battery"]["soc_entity"] = "sensor.battery_soc"
    parsed = VoltWiseConfig.from_dict(config)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    client = FakeClient(states={"sensor.battery_soc": {"state": "50", "last_updated": old}})
    try:
        apply_live_soc(parsed, client, max_age_seconds=900)
        raise AssertionError("stale sensor accepted")
    except ValueError as exc:
        assert "old" in str(exc)


def test_fresh_soc_sensor_is_accepted():
    config = make_config_dict()
    config["battery"]["soc_entity"] = "sensor.battery_soc"
    parsed = VoltWiseConfig.from_dict(config)
    now = datetime.now(UTC).isoformat()
    client = FakeClient(states={"sensor.battery_soc": {"state": "55", "last_updated": now}})
    updated, warnings = apply_live_soc(parsed, client, max_age_seconds=900)
    assert updated.battery.initial_soc_pct == 55.0
    assert not warnings


def test_ramp_limit_is_enforced():
    config = make_config(
        n_slots=6, prices=[-0.5, -0.4, 0.8, 0.9, -0.3, 0.7], max_ramp_kw_per_slot=1.0
    )
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)
    previous = 0.0
    for slot in plan.slots:
        assert abs(slot.battery_kw - previous) <= 1.0 + 1e-6
        previous = slot.battery_kw
    assert audit_plan(plan, config, forecasts).passed
    assert any("ramp" in warning for warning in plan.warnings)


def test_reserve_margin_when_solar_is_volatile():
    flat = make_config(n_slots=4, solar=[1.0] * 4)
    volatile = make_config(
        n_slots=4, solar=[0.0, 10.0, 0.0, 10.0],
        battery={"reserve_margin_pct": 10.0, "reserve_margin_solar_threshold_kw": 5.0},
    )
    assert effective_reserve_pct_with_forecast(
        volatile, make_forecasts(volatile)
    ) > effective_reserve_pct_with_forecast(flat, make_forecasts(flat))


def test_degradation_penalises_deep_discharge():
    config = make_config(n_slots=1, battery={"degradation_soc_penalty": 1.0})
    forecasts = make_forecasts(config)
    high = slot_cost(config, forecasts, 0, 0.5, -2.0, 0.1, 90.0)
    low = slot_cost(config, forecasts, 0, 0.5, -2.0, 0.1, 20.0)
    assert high > low


def test_tariff_fee_is_applied():
    from voltwise.economics import buy_price

    plain = make_config(n_slots=2, prices=[0.1, 0.1])
    data = plain.to_dict()
    data["tariff"]["buy_fee_per_kwh"] = 0.05
    priced = VoltWiseConfig.from_dict(data)
    assert abs(buy_price(priced, make_forecasts(priced), 0) - 0.15) < 1e-9
    assert optimize(priced, make_forecasts(priced)).total_cost > optimize(
        plain, make_forecasts(plain)
    ).total_cost


def test_site_no_longer_accepts_fee_keys():
    data = make_config_dict()
    data["site"]["buy_fee_per_kwh"] = 0.05
    try:
        VoltWiseConfig.from_dict(data)
        raise AssertionError("site fee key still accepted")
    except ValueError:
        pass


def test_solver_budget_reduces_resolution_with_warning():
    config = make_config(
        n_slots=48, soc_levels=401, action_steps=81, solver_budget_pairs=1000,
        prices=[0.1] * 48,
    )
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)
    assert any("resolution reduced" in warning for warning in plan.warnings)
    assert audit_plan(plan, config, forecasts).passed


def test_uncertainty_stress_adds_warning():
    config = make_config(
        n_slots=2, prices=[0.1, 0.2], solar=[0.0, 0.0], consumption=[8.0, 8.0],
        site={"max_grid_import_kw": 10.0, "max_grid_export_kw": 10.0, "feed_in_factor": 0.0},
        forecast={"provider": "manual", "solar_kw": [0.0, 0.0],
                  "consumption_kw": [8.0, 8.0], "uncertainty_pct": 0.5},
    )
    plan = optimize(config, make_forecasts(config))
    assert plan.status in {"DEGRADED", "FALLBACK"}
    if plan.status == "DEGRADED":
        assert any("forecast uncertainty" in warning for warning in plan.warnings)


def test_baseline_method_is_exposed():
    config = make_config(n_slots=2)
    plan = optimize(config, make_forecasts(config))
    payload = plan.to_dict()
    assert payload["baseline_method"].endswith("_v1")
