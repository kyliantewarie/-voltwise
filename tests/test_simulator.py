"""Hardware-in-the-loop tests using the simulated inverter/HA stack."""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simulator.devices import Battery, EvCharger  # noqa: E402
from simulator.runner import base_config, run_simulation  # noqa: E402


def test_full_actuation_cycle_reaches_simulated_battery():
    result = run_simulation(base_config(), cycles=4)
    assert all(c.exit_code == 0 for c in result.cycles)
    # A cheap-price slot must charge the simulated battery.
    assert any(c.battery_kw > 1.0 for c in result.cycles)
    assert max(c.soc_pct for c in result.cycles) > 50.0
    assert result.service_calls > 0


def test_watchdog_forces_safe_state_when_optimiser_stops():
    # A crashed add-on stops refreshing the expiry; every cycle forces safe.
    result = run_simulation(base_config(), cycles=3, stop_optimiser_after=0)
    assert result.watchdog_trips == 3
    assert all(c.battery_kw == 0.0 for c in result.cycles)
    # The HA helper states, not just internal fields, must be forced to zero.
    assert result.setpoints["number.voltwise_battery_power"] == 0.0
    assert result.setpoints["number.voltwise_ev_charge_power"] == 0.0


def test_addon_stop_leaves_zero_setpoints_after_watchdog():
    # Cycle 0 arms expiry at +2 intervals (14:00). The add-on then stops; the
    # watchdog trips from cycle 3 (15:00) when the expiry has actually passed.
    result = run_simulation(base_config(), cycles=5, stop_optimiser_after=1)
    assert result.watchdog_trips >= 2
    assert all(c.battery_kw == 0.0 for c in result.cycles[3:])
    assert result.setpoints["number.voltwise_battery_power"] == 0.0


def test_bms_rejects_commands_outside_soc_limits():
    battery = Battery(capacity_kwh=10, soc_pct=95, soc_max_pct=95)
    applied = battery.apply(5.0, 1.0)
    assert applied == 0.0
    assert battery.bms_cutoffs == 1

    battery = Battery(capacity_kwh=10, soc_pct=10, soc_min_pct=10)
    assert battery.apply(-5.0, 1.0) == 0.0
    assert battery.bms_cutoffs == 1


def test_inverter_respects_ramp_limit():
    battery = Battery(capacity_kwh=20, soc_pct=50, ramp_kw_per_hour=1.0)
    applied = battery.apply(5.0, 1.0)
    assert applied == 1.0
    assert battery.apply(5.0, 1.0) == 2.0


def test_ev_charger_integrates_soc_and_stops_at_full():
    ev = EvCharger(capacity_kwh=10, soc_pct=0.0, max_charge_kw=11.0)
    for _ in range(10):
        ev.apply(11.0, 1.0)
    assert ev.soc_pct <= 100.0
    assert math.isclose(ev.soc_pct, 100.0)


def test_closed_loop_deviation_is_detected():
    from voltwise.cli import _closed_loop_warning
    from voltwise.config import VoltWiseConfig

    class Client:
        def get_state(self, entity):
            return {"state": "9.5"}

    config = VoltWiseConfig.from_dict(base_config())
    plan = {"slots": [{"grid_import_kw": 0.5}]}
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        warning = _closed_loop_warning(config, Client(), str(path))
    assert warning and "deviation" in warning.lower()


def test_service_circuit_breaker_survives_cycles():
    config = base_config()
    config["actuation"]["ha_url"] = "http://homeassistant:8123"
    result = run_simulation(
        config, cycles=3, fail_services={"number.set_value"}
    )
    assert all(c.exit_code == 6 for c in result.cycles)
    # Watchdog writes still happen; setpoints are rolled back / skipped.
    assert result.service_calls > 0


def test_mqtt_adapter_writes_reach_the_simulated_battery():
    config = base_config()
    config["actuation"]["adapter"] = "mqtt"
    config["actuation"]["adapter_topic_prefix"] = "voltwise/inverter"
    result = run_simulation(config, cycles=4)
    assert result.cycles[0].exit_code == 0
    # MQTT setpoints must move the simulated pack, not just be logged.
    assert any(c.battery_kw != 0.0 for c in result.cycles)
    assert max(c.soc_pct for c in result.cycles) > 50.0


def test_modbus_adapter_writes_reach_the_simulated_battery():
    config = base_config()
    config["actuation"]["adapter"] = "modbus"
    config["actuation"]["adapter_topic_prefix"] = "voltwise_inverter"
    result = run_simulation(config, cycles=4)
    assert result.cycles[0].exit_code == 0
    assert any(c.battery_kw != 0.0 for c in result.cycles)


def test_optimiser_uses_live_soc_from_simulated_sensor():
    result = run_simulation(base_config(), cycles=4)
    # Cycle 0 starts at 50%; later cycles must use the measured SOC (47.375%).
    assert result.cycles[0].optimiser_soc == 50.0
    assert result.cycles[1].optimiser_soc != 50.0
    assert abs(result.cycles[1].optimiser_soc - result.cycles[0].soc_pct) < 0.01


def test_sub_hourly_intervals_run_end_to_end():
    for interval in (15, 30):
        config = base_config(n_slots=8, interval_minutes=interval)
        result = run_simulation(config, cycles=2)
        assert all(c.exit_code == 0 for c in result.cycles), (interval, result.cycles)


def test_configured_parameters_reach_the_simulated_devices():
    config = base_config()
    config["battery"]["charge_efficiency"] = 0.60
    config["forecast"]["solar_kw"] = [3.0] * config["n_slots"]
    config["forecast"]["consumption_kw"] = [4.0] * config["n_slots"]
    result = run_simulation(config, cycles=1)
    assert result.cycles[0].measured_import_kw > 0.0
    assert result.cycles[0].exit_code == 0


def test_ev_path_is_simulated_end_to_end():
    config = base_config()
    config["ev"] = {
        "capacity_kwh": 20.0, "initial_soc_pct": 20.0, "target_soc_pct": 80.0,
        "deadline_slot": 3, "max_charge_kw": 7.0, "charge_efficiency": 0.95,
    }
    result = run_simulation(config, cycles=4)
    assert result.cycles[0].exit_code == 0
    assert max(c.ev_soc_pct for c in result.cycles) > 20.0


def test_failed_services_are_recorded_separately():
    result = run_simulation(base_config(), cycles=1, fail_services={"number.set_value"})
    assert result.failed_service_calls > 0
    assert result.service_calls > 0


def test_telemetry_with_missing_helpers_does_not_fail_cycle():
    config = base_config()
    config["actuation"]["telemetry_enabled"] = True
    result = run_simulation(config, cycles=1)
    assert result.cycles[0].exit_code == 0
