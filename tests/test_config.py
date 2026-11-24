"""Configuration validation tests (fail-closed behaviour)."""

from __future__ import annotations

import math

import pytest
from conftest import make_config_dict

from voltwise.config import ConfigError, VoltWiseConfig


def test_valid_config_is_accepted():
    config = VoltWiseConfig.from_dict(make_config_dict())
    assert config.n_slots == 8
    assert config.battery.capacity_kwh == 10.0


@pytest.mark.parametrize("mutation", [
    {"bogus_key": 1},
    {"battery": {"bogus": 1}},
    {"actuation": {"ha_url": "javascript:alert(1)"}},
    {"actuation": {"require_confirmation": "false"}},
    {"actuation": {"require_confirmation": 0}},
    {"actuation": {"battery_power_entity": "light.kitchen"}},
    {"actuation": {"battery_power_entity": "number.kitchen;rm -rf /"}},
    {"tariff": {"provider": "evil"}},
    {"forecast": {"provider": "cloud"}},
    {"n_slots": 0},
    {"n_slots": 169},
    {"interval_minutes": 7},
    {"soc_levels": 2},
    {"action_steps": 1},
    {"battery": {"capacity_kwh": 201.0}},
    {"battery": {"max_charge_kw": -1.0}},
    {"battery": {"charge_efficiency": 1.5}},
    {"battery": {"soc_min_pct": 95.0, "soc_max_pct": 10.0}},
    {"battery": {"soc_min_pct": 10.0, "soc_max_pct": 95.0, "reserve_soc_pct": 99.0}},
    {"ev": {"capacity_kwh": 60.0, "initial_soc_pct": 80.0, "target_soc_pct": 40.0, "deadline_slot": 4}},
    {"ev": {"capacity_kwh": 60.0, "initial_soc_pct": 40.0, "target_soc_pct": 80.0, "deadline_slot": 99}},
    {"tariff": {"provider": "tibber", "api_key_env": "AWS_SECRET_ACCESS_KEY"}},
    {"tariff": {"provider": "manual", "manual_prices": [0.1]}},
    {"tariff": {"provider": "manual", "manual_prices": [0.1] * 20}},
])
def test_invalid_config_is_rejected(mutation):
    with pytest.raises(ConfigError):
        VoltWiseConfig.from_dict(make_config_dict(**mutation))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "0.1", True, None])
def test_non_numeric_or_non_finite_values_are_rejected(bad):
    data = make_config_dict()
    data["battery"]["capacity_kwh"] = bad
    with pytest.raises(ConfigError):
        VoltWiseConfig.from_dict(data)


def test_starting_below_reserve_is_allowed_for_live_soc():
    data = make_config_dict()
    data["battery"]["initial_soc_pct"] = 15.0
    data["battery"]["reserve_soc_pct"] = 20.0
    config = VoltWiseConfig.from_dict(data)
    assert config.battery.initial_soc_pct < config.battery.reserve_soc_pct


def test_live_soc_entity_is_validated():
    data = make_config_dict()
    data["battery"]["soc_entity"] = "sensor.battery_soc"
    assert VoltWiseConfig.from_dict(data).battery.soc_entity == "sensor.battery_soc"
    data["battery"]["soc_entity"] = "light.battery"
    with pytest.raises(ConfigError):
        VoltWiseConfig.from_dict(data)


def test_negative_prices_are_allowed():
    data = make_config_dict(prices=[-0.05] * 8)
    assert VoltWiseConfig.from_dict(data).tariff.manual_prices[0] < 0


def test_round_trip_serialization():
    original = VoltWiseConfig.from_dict(make_config_dict())
    restored = VoltWiseConfig.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()


def test_finite_helper_rejects_bool():
    with pytest.raises(ConfigError):
        VoltWiseConfig.from_dict(make_config_dict(n_slots=True))


def test_no_nan_sneaks_into_series():
    data = make_config_dict()
    data["tariff"]["manual_prices"][2] = math.nan
    with pytest.raises(ConfigError):
        VoltWiseConfig.from_dict(data)
