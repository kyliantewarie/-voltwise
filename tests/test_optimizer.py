"""Functional tests for the dynamic-programming optimiser."""

from __future__ import annotations

import math

from conftest import make_config, make_forecasts

from voltwise.optimizer import optimize, schedule_ev
from voltwise.safety import audit_plan


def test_cheap_night_expensive_evening_produces_savings():
    prices = [0.02, 0.01, 0.03, 0.35, 0.50, 0.45, 0.20, 0.08]
    config = make_config(prices=prices, consumption=[0.5] * 8)
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)

    assert plan.status == "OPTIMAL"
    assert audit_plan(plan, config, forecasts).passed
    assert plan.savings > 0.0
    assert plan.total_cost <= plan.baseline_cost + 1e-9
    assert any(slot.battery_kw > 0.1 for slot in plan.slots)
    assert any(slot.battery_kw < -0.1 for slot in plan.slots)


def test_flat_prices_and_degradation_keep_battery_idle():
    config = make_config(prices=[0.10] * 8)
    plan = optimize(config, make_forecasts(config))
    assert all(abs(slot.battery_kw) < 1e-6 for slot in plan.slots)
    assert math.isclose(plan.savings, 0.0, abs_tol=1e-6)


def test_negative_prices_trigger_charging():
    config = make_config(prices=[-0.10] * 4 + [0.40] * 4)
    plan = optimize(config, make_forecasts(config))
    assert plan.savings > 0.0
    assert audit_plan(plan, config, make_forecasts(config)).passed
    assert plan.slots[0].battery_kw > 0.0


def test_battery_limits_and_soc_are_never_violated():
    prices = [-0.5, -0.4, 0.9, 1.0, -0.3, 0.8, 0.7, 0.6]
    config = make_config(prices=prices, battery={"max_charge_kw": 3.0, "max_discharge_kw": 2.0})
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)

    for slot in plan.slots:
        assert -2.0 - 1e-6 <= slot.battery_kw <= 3.0 + 1e-6
        assert config.battery.soc_min_pct - 1e-3 <= slot.battery_soc_pct <= config.battery.soc_max_pct + 1e-3
        assert slot.grid_import_kw <= config.site.max_grid_import_kw + 1e-6
        assert slot.grid_export_kw <= config.site.max_grid_export_kw + 1e-6
    assert plan.slots[-1].battery_soc_pct >= config.battery.reserve_soc_pct - 1e-3
    assert audit_plan(plan, config, forecasts).passed


def test_ev_reaches_target_before_deadline():
    config = make_config(
        prices=[0.05, 0.02, 0.01, 0.30, 0.40, 0.50, 0.60, 0.20],
        ev={"capacity_kwh": 60.0, "initial_soc_pct": 40.0, "target_soc_pct": 80.0,
            "deadline_slot": 5, "max_charge_kw": 11.0, "charge_efficiency": 0.95},
    )
    plan = optimize(config, make_forecasts(config))
    assert plan.status == "OPTIMAL"
    assert plan.slots[5].ev_soc_pct >= 80.0 - 1e-3
    assert all(s.ev_kw >= -1e-9 for s in plan.slots)
    assert all(s.ev_kw <= 11.0 + 1e-9 for s in plan.slots)
    assert audit_plan(plan, config, make_forecasts(config)).passed


def test_infeasible_ev_degrades_gracefully_but_stays_safe():
    config = make_config(
        ev={"capacity_kwh": 60.0, "initial_soc_pct": 40.0, "target_soc_pct": 80.0,
            "deadline_slot": 0, "max_charge_kw": 1.0, "charge_efficiency": 0.95},
    )
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)
    assert plan.status == "DEGRADED"
    assert any("EV target" in warning for warning in plan.warnings)
    report = audit_plan(plan, config, forecasts)
    assert not report.hard_violations
    assert any(v.code == "EV_TARGET_MISSED" for v in report.soft_violations)


def test_schedule_ev_never_exceeds_capacity():
    config = make_config(
        ev={"capacity_kwh": 60.0, "initial_soc_pct": 95.0, "target_soc_pct": 100.0,
            "deadline_slot": 7, "max_charge_kw": 11.0, "charge_efficiency": 0.95},
    )
    power, soc, warnings = schedule_ev(config, make_forecasts(config))
    assert all(0.0 <= value <= 100.0 + 1e-6 for value in soc)
    assert len(power) == config.n_slots
    assert not warnings


def test_export_limit_is_respected():
    config = make_config(
        prices=[0.10] * 8,
        solar=[8.0] * 8,
        consumption=[0.2] * 8,
        site={"max_grid_import_kw": 10.0, "max_grid_export_kw": 2.0, "feed_in_factor": 0.3},
    )
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)
    assert plan.status != "FALLBACK"
    assert all(slot.grid_export_kw <= 2.0 + 1e-6 for slot in plan.slots)
    assert any(slot.curtailed_kw > 0.0 for slot in plan.slots)
    assert audit_plan(plan, config, forecasts).passed


def test_plan_is_deterministic():
    config = make_config(prices=[0.02, 0.09, 0.30, 0.50, 0.05, 0.04, 0.40, 0.60])
    forecasts = make_forecasts(config)
    first = optimize(config, forecasts)
    second = optimize(config, forecasts)
    assert first.to_dict()["slots"] == second.to_dict()["slots"]
    assert first.status == second.status


def test_solar_surplus_prefers_battery_charging():
    config = make_config(
        prices=[0.30] * 8,
        solar=[3.0] * 8,
        consumption=[0.5] * 8,
        battery={"capacity_kwh": 10.0, "initial_soc_pct": 50.0, "max_charge_kw": 3.0},
    )
    plan = optimize(config, make_forecasts(config))
    assert any(slot.battery_kw > 0.0 for slot in plan.slots)
    assert audit_plan(plan, config, make_forecasts(config)).passed


def test_many_random_valid_scenarios_never_crash_or_violate_limits():
    import random

    random.seed(1234)
    for _ in range(40):
        prices = [round(random.uniform(-0.2, 0.8), 3) for _ in range(6)]
        solar = [round(random.uniform(0, 3), 2) for _ in range(6)]
        load = [round(random.uniform(0.2, 1.5), 2) for _ in range(6)]
        config = make_config(n_slots=6, prices=prices, solar=solar, consumption=load, soc_levels=51)
        forecasts = make_forecasts(config)
        plan = optimize(config, forecasts)
        assert plan.status in {"OPTIMAL", "DEGRADED", "FALLBACK"}
        if plan.status != "FALLBACK":
            assert audit_plan(plan, config, forecasts).passed
