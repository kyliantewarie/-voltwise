"""Regression tests for the confirmed optimiser/economics breaks."""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import make_config, make_config_dict, make_forecasts

from voltwise.config import VoltWiseConfig
from voltwise.models import PricePoint
from voltwise.optimizer import optimize, schedule_ev
from voltwise.safety import audit_plan


@pytest.mark.parametrize("interval", [15, 30, 60])
def test_ev_charging_is_interval_independent(interval):
    config = make_config(
        n_slots=8, interval_minutes=interval,
        ev={"capacity_kwh": 10.0, "initial_soc_pct": 0.0, "target_soc_pct": 100.0,
            "deadline_slot": 7, "max_charge_kw": 11.0, "charge_efficiency": 0.95},
    )
    forecasts = make_forecasts(config)
    power, soc, warnings = schedule_ev(config, forecasts)
    assert not warnings
    assert soc[-1] >= 100.0 - 1e-6
    assert abs(sum(power) * interval / 60.0 - 10.0 / 0.95) < 1e-6
    plan = optimize(config, forecasts)
    assert plan.status == "OPTIMAL"
    assert plan.slots[-1].ev_soc_pct >= 100.0 - 1e-6
    assert audit_plan(plan, config, forecasts).passed


def test_baseline_includes_ev_in_curtailment():
    config = make_config(
        n_slots=1, prices=[0.5], solar=[2.0], consumption=[0.5],
        site={"max_grid_import_kw": 10.0, "max_grid_export_kw": 0.0, "feed_in_factor": 1.0},
        battery={"capacity_kwh": 0.1, "initial_soc_pct": 50.0, "reserve_soc_pct": 20.0,
                 "max_charge_kw": 0.1, "max_discharge_kw": 0.1},
        ev={"capacity_kwh": 10.0, "initial_soc_pct": 0.0, "target_soc_pct": 19.0,
            "deadline_slot": 0, "max_charge_kw": 2.0, "charge_efficiency": 0.95},
    )
    plan = optimize(config, make_forecasts(config))
    assert abs(plan.baseline_cost - 0.25) < 1e-6
    assert plan.savings >= -1e-9
    assert audit_plan(plan, config, make_forecasts(config)).passed


def test_infeasible_baseline_does_not_create_negative_savings():
    config = make_config(
        n_slots=1, prices=[-0.5], solar=[0.5], consumption=[2.0],
        site={"max_grid_import_kw": 0.0, "max_grid_export_kw": 0.0, "feed_in_factor": 0.0},
        battery={"capacity_kwh": 10.0, "initial_soc_pct": 80.0, "reserve_soc_pct": 20.0,
                 "max_charge_kw": 5.0, "max_discharge_kw": 5.0},
    )
    plan = optimize(config, make_forecasts(config))
    assert plan.savings >= -1e-9
    assert any("baseline is infeasible" in w for w in plan.warnings)
    assert plan.baseline_cost >= plan.total_cost - 1e-9


def test_negative_feed_in_triggers_economic_curtailment():
    config = make_config(
        n_slots=8, prices=[0.3] * 8, solar=[10.0] * 8, consumption=[0.0] * 8,
        site={"max_grid_import_kw": 10.0, "max_grid_export_kw": 10.0, "feed_in_factor": 0.0},
        battery={"capacity_kwh": 0.1, "initial_soc_pct": 50.0, "reserve_soc_pct": 20.0,
                 "max_charge_kw": 0.1, "max_discharge_kw": 0.1},
    )
    forecasts = make_forecasts(config, feed_in_prices=[-0.5] * 8)
    plan = optimize(config, forecasts)
    assert all(slot.grid_export_kw < 1e-6 for slot in plan.slots)
    assert all(slot.curtailed_kw > 9.0 for slot in plan.slots)
    assert plan.savings >= -1e-9
    assert audit_plan(plan, config, forecasts).passed


def test_nan_price_is_rejected_by_plan_audit():
    config = make_config(n_slots=4)
    forecasts = make_forecasts(config)
    points = list(forecasts.prices)
    points[0] = PricePoint(points[0].timestamp, float("nan"))
    poisoned = replace(forecasts, prices=tuple(points))
    plan = optimize(config, poisoned)
    report = audit_plan(plan, config, poisoned)
    assert not report.passed
    assert any(v.code == "NON_FINITE_COST" for v in report.hard_violations)


def test_impossible_site_load_is_rejected_before_optimisation():
    from voltwise.safety import audit_forecasts

    config = make_config(
        n_slots=1, consumption=[50.0], solar=[0.0],
        site={"max_grid_import_kw": 5.0, "max_grid_export_kw": 5.0, "feed_in_factor": 0.0},
        battery={"max_discharge_kw": 5.0},
    )
    report = audit_forecasts(config, make_forecasts(config))
    assert not report.passed
    assert any(v.code == "SITE_INFEASIBLE" for v in report.hard_violations)


def test_extreme_finite_prices_are_rejected():
    data = make_config_dict(n_slots=2, prices=[1e308, 1e308])
    try:
        VoltWiseConfig.from_dict(data)
        raise AssertionError("extreme price accepted")
    except ValueError:
        pass


def test_fallback_costs_are_finite():
    import math

    config = make_config(n_slots=2, prices=[1e6, 1e6], battery={"capacity_kwh": 0.1,
                         "initial_soc_pct": 50.0, "max_charge_kw": 0.1,
                         "max_discharge_kw": 0.1})
    plan = optimize(config, make_forecasts(config))
    assert math.isfinite(plan.total_cost) and math.isfinite(plan.savings)
    assert math.isfinite(plan.baseline_cost)


def test_solver_ceiling_is_respected():
    config = make_config(n_slots=168, soc_levels=401, action_steps=5,
                         solver_budget_pairs=500_000, prices=[0.1] * 168)
    plan = optimize(config, make_forecasts(config))
    warning = next(w for w in plan.warnings if "resolution reduced" in w)
    levels = int(warning.split("to ")[1].split(" ")[0])
    steps = int(warning.split("/ ")[1].split(" ")[0])
    assert config.n_slots * levels * (2 * steps + 3) <= 500_000 or (levels <= 11 and steps <= 5)


def test_auditor_rejects_plan_that_lies_about_consumption():
    config = make_config(n_slots=2, prices=[0.2, 0.2], consumption=[0.5, 0.5])
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)
    assert audit_plan(plan, config, forecasts).passed
    tampered = replace(
        plan,
        slots=tuple([
            replace(plan.slots[0], consumption_kw=0.0, grid_import_kw=0.0, cost=0.0),
            *plan.slots[1:],
        ]),
    )
    report = audit_plan(tampered, config, forecasts)
    assert any(v.code == "CONSUMPTION_MISMATCH" for v in report.hard_violations)


def test_stress_test_accounts_for_curtailment():
    from voltwise.safety import stress_test

    config = make_config(
        n_slots=1, prices=[0.3], solar=[10.0], consumption=[2.0],
        site={"max_grid_import_kw": 3.0, "max_grid_export_kw": 0.0, "feed_in_factor": 1.0},
        battery={"capacity_kwh": 0.1, "initial_soc_pct": 50.0, "reserve_soc_pct": 20.0,
                 "max_charge_kw": 0.1, "max_discharge_kw": 0.1},
        forecast={"provider": "manual", "solar_kw": [10.0], "consumption_kw": [2.0]},
    )
    forecasts = make_forecasts(config, feed_in_prices=[-0.5])
    plan = optimize(config, forecasts)
    assert any(slot.curtailed_kw > 0 for slot in plan.slots)
    report = stress_test(plan, config, forecasts, 0.9)
    assert any(v.code == "STRESS_IMPORT_LIMIT" for v in report.hard_violations)


def test_reserve_margin_never_exceeds_starting_soc():
    from voltwise.economics import effective_reserve_pct_with_forecast

    config = make_config(
        n_slots=4, solar=[0.0, 10.0, 0.0, 10.0],
        battery={"initial_soc_pct": 25.0, "reserve_soc_pct": 20.0,
                 "reserve_margin_pct": 10.0, "reserve_margin_solar_threshold_kw": 5.0},
    )
    forecast = make_forecasts(config)
    assert effective_reserve_pct_with_forecast(config, forecast) <= 25.0
    plan = optimize(config, forecast)
    assert audit_plan(plan, config, forecast).passed


def test_tariff_level_fees_are_applied():
    from voltwise.economics import buy_price

    config = make_config(n_slots=2, prices=[0.1, 0.1])
    data = config.to_dict()
    data["tariff"]["buy_fee_per_kwh"] = 0.05
    priced = VoltWiseConfig.from_dict(data)
    assert buy_price(priced, make_forecasts(priced), 0) > buy_price(config, make_forecasts(config), 0)


def test_mixed_naive_aware_timestamps_do_not_crash_input_audit():
    from voltwise.safety import audit_forecasts

    config = make_config(n_slots=3)
    forecasts = make_forecasts(config)
    points = list(forecasts.prices)
    points[0] = replace(points[0], timestamp=points[0].timestamp.replace(tzinfo=None))
    mixed = replace(forecasts, prices=tuple(points))
    assert audit_forecasts(config, mixed).passed


def test_starting_below_reserve_does_not_create_negative_savings():
    config = make_config(
        n_slots=1, prices=[1.0], solar=[10.0], consumption=[1.0],
        site={"max_grid_import_kw": 100.0, "max_grid_export_kw": 100.0, "feed_in_factor": 0.0},
        battery={"capacity_kwh": 10.0, "initial_soc_pct": 20.0, "soc_min_pct": 0.0,
                 "soc_max_pct": 100.0, "reserve_soc_pct": 80.0,
                 "max_charge_kw": 10.0, "max_discharge_kw": 10.0,
                 "charge_efficiency": 1.0, "discharge_efficiency": 1.0,
                 "degradation_cost_per_kwh": 5.0},
    )
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)
    assert plan.savings >= -1e-9
    assert any("below the" in warning for warning in plan.warnings)
    assert audit_plan(plan, config, forecasts).passed


def test_fallback_audit_treats_unavoidable_grid_limits_as_informational():
    config = make_config(
        n_slots=2, prices=[0.2, 0.2], solar=[0.0, 0.0], consumption=[0.5, 0.5],
        site={"max_grid_import_kw": 0.0, "max_grid_export_kw": 10.0, "feed_in_factor": 0.0},
    )
    from voltwise.safety import make_fallback_plan

    forecasts = make_forecasts(config)
    fallback = make_fallback_plan(config, forecasts, "test")
    report = audit_plan(fallback, config, forecasts)
    assert report.passed
    limits = [v for v in report.hard_violations + report.soft_violations
              if v.code in {"IMPORT_LIMIT", "EXPORT_LIMIT"}]
    assert limits and all(v.severity == "SOFT" for v in limits)
