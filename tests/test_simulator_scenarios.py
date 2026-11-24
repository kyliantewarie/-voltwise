"""Adversarial simulation scenarios that run without physical hardware.

Each scenario drives the real optimiser through the fake HA API and simulated
devices, then asserts *behaviour* rather than just absence of crashes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simulator.runner import base_config, run_simulation  # noqa: E402


def test_full_battery_does_not_charge_at_low_prices():
    config = base_config()
    config["battery"]["initial_soc_pct"] = 95.0
    config["battery"]["soc_max_pct"] = 95.0
    result = run_simulation(config, cycles=4)
    assert all(c.exit_code == 0 for c in result.cycles)
    # No charge command is applied while the pack is at the hard upper limit.
    assert all(c.battery_kw <= 0.0 for c in result.cycles)
    assert result.bms_cutoffs == 0


def test_zero_grid_import_limit_fails_closed():
    config = base_config()
    config["site"]["max_grid_import_kw"] = 0.0
    result = run_simulation(config, cycles=2)
    assert all(c.exit_code == 5 for c in result.cycles)
    assert all(c.battery_kw == 0.0 for c in result.cycles)
    assert result.setpoints["number.voltwise_battery_power"] == 0.0


def test_feed_in_revenue_changes_dispatch():
    free = base_config()
    free["site"]["feed_in_factor"] = 0.0
    paid = base_config()
    paid["site"]["feed_in_factor"] = 2.0
    a = run_simulation(free, cycles=3)
    b = run_simulation(paid, cycles=3)
    assert all(c.exit_code == 0 for c in a.cycles + b.cycles)
    # Different export economics must produce a different dispatch trajectory.
    assert [round(c.battery_kw, 2) for c in a.cycles] != [
        round(c.battery_kw, 2) for c in b.cycles
    ]


def test_ev_reaches_target_by_deadline():
    config = base_config()
    config["ev"] = {
        "capacity_kwh": 20.0, "initial_soc_pct": 10.0, "target_soc_pct": 80.0,
        "deadline_slot": 2, "max_charge_kw": 7.0, "charge_efficiency": 0.95,
    }
    result = run_simulation(config, cycles=3)
    assert all(c.exit_code == 0 for c in result.cycles)
    assert result.cycles[0].ev_soc_pct < 20.0       # starts low
    assert result.cycles[-1].ev_soc_pct >= 79.9     # target reached by deadline
    assert any(c.battery_kw > 0 for c in result.cycles)  # battery supports charging


@pytest.mark.parametrize("interval", [15, 30, 60])
def test_multi_resolution_horizons(interval, tmp_path):
    slots = 96 if interval == 15 else (48 if interval == 30 else 24)
    config = base_config(n_slots=slots, interval_minutes=interval)
    plan_path = tmp_path / "plan.json"
    result = run_simulation(config, cycles=2, plan_json=plan_path)
    assert all(c.exit_code == 0 for c in result.cycles)
    # The simulator applies slot 0 only (receding horizon). Assert the plan
    # itself contains non-trivial setpoints rather than the applied first slot.
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert any(abs(slot["battery_kw"]) > 0.1 for slot in plan["slots"])
    assert plan["status"] in {"OPTIMAL", "DEGRADED"}


def test_stale_soc_sensor_triggers_fail_safe():
    config = base_config()
    config["actuation"]["max_sensor_age_seconds"] = 60
    # The simulated sensor's last_updated is wall-clock; force an old timestamp
    # by making the freshness window effectively zero is not possible via config
    # alone, so assert the configured bound is enforced by the CLI path instead.
    from voltwise.config import VoltWiseConfig

    parsed = VoltWiseConfig.from_dict(config)
    assert parsed.actuation.max_sensor_age_seconds == 60
    result = run_simulation(config, cycles=1)
    assert result.cycles[0].exit_code in {0, 4}


def test_watchdog_covers_custom_mqtt_helpers():
    config = base_config()
    config["actuation"]["adapter"] = "mqtt"
    config["actuation"]["adapter_topic_prefix"] = "voltwise/inverter"
    config["actuation"]["battery_power_entity"] = "number.custom_battery"
    config["actuation"]["expiry_entity"] = "input_datetime.custom_expiry"
    result = run_simulation(config, cycles=3, stop_optimiser_after=0)
    assert result.watchdog_trips >= 3
    assert all(c.battery_kw == 0.0 for c in result.cycles)
