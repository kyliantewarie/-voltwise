"""Safety audit and fail-safe tests. The auditor must reject tampered plans."""

from __future__ import annotations

from dataclasses import replace

from conftest import make_config, make_forecasts

from voltwise.forecasts import plan_commands
from voltwise.optimizer import optimize
from voltwise.safety import audit_forecasts, audit_plan, make_fallback_plan


def valid_plan_and_inputs():
    config = make_config(prices=[0.02, 0.03, 0.40, 0.50, 0.05, 0.04, 0.30, 0.60])
    forecasts = make_forecasts(config)
    return config, forecasts, optimize(config, forecasts)


def codes(report):
    return {v.code for v in report.hard_violations}


def test_valid_plan_passes_independent_audit():
    config, forecasts, plan = valid_plan_and_inputs()
    report = audit_plan(plan, config, forecasts)
    assert report.passed
    assert report.checks_run > 0


def test_tampered_charge_power_is_detected():
    config, forecasts, plan = valid_plan_and_inputs()
    slot = replace(plan.slots[0], battery_kw=config.battery.max_charge_kw + 1.0)
    tampered = replace(plan, slots=tuple([slot, *plan.slots[1:]]))
    assert "CHARGE_LIMIT" in codes(audit_plan(tampered, config, forecasts))


def test_tampered_soc_is_detected():
    config, forecasts, plan = valid_plan_and_inputs()
    slot = replace(plan.slots[0], battery_soc_pct=plan.slots[0].battery_soc_pct + 5.0)
    tampered = replace(plan, slots=tuple([slot, *plan.slots[1:]]))
    assert "SOC_MISMATCH" in codes(audit_plan(tampered, config, forecasts))


def test_tampered_grid_balance_is_detected():
    config, forecasts, plan = valid_plan_and_inputs()
    slot = replace(plan.slots[0], grid_import_kw=plan.slots[0].grid_import_kw + 2.0)
    tampered = replace(plan, slots=tuple([slot, *plan.slots[1:]]))
    assert "POWER_BALANCE" in codes(audit_plan(tampered, config, forecasts))


def test_tampered_cost_is_detected():
    config, forecasts, plan = valid_plan_and_inputs()
    slot = replace(plan.slots[0], cost=plan.slots[0].cost + 1.0)
    tampered = replace(plan, slots=tuple([slot, *plan.slots[1:]]))
    assert "COST_MISMATCH" in codes(audit_plan(tampered, config, forecasts))


def test_reserve_shortfall_is_detected():
    # Start exactly at the reserve so any discharge breaks the terminal floor.
    config = make_config(
        prices=[0.10] * 8,
        battery={"initial_soc_pct": 50.0, "reserve_soc_pct": 50.0},
    )
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)
    assert all(abs(slot.battery_kw) < 1e-6 for slot in plan.slots)
    last = plan.slots[-1]
    slot = replace(last, battery_kw=-1.0, grid_import_kw=0.0,
                   grid_export_kw=0.5, battery_soc_pct=45.0)
    tampered = replace(plan, slots=tuple([*plan.slots[:-1], slot]))
    assert "RESERVE_MISSED" in codes(audit_plan(tampered, config, forecasts))


def test_reserve_floor_is_enforced_in_every_slot():
    config = make_config(
        prices=[0.05, 0.04, 0.03, 0.40, 0.50, 0.20, 0.10, 0.30],
        battery={"initial_soc_pct": 35.0, "soc_min_pct": 10.0, "reserve_soc_pct": 30.0},
    )
    forecasts = make_forecasts(config)
    plan = optimize(config, forecasts)
    assert all(slot.battery_soc_pct >= 30.0 - 1e-3 for slot in plan.slots)
    assert plan.slots[0].battery_soc_pct < 40.0
    assert audit_plan(plan, config, forecasts).passed

    index = 0
    slot = replace(plan.slots[index], battery_kw=-5.0,
                   battery_soc_pct=plan.slots[index].battery_soc_pct - 40.0)
    tampered = replace(plan, slots=tuple(
        [*plan.slots[:index], slot, *plan.slots[index + 1:]]
    ))
    report = audit_plan(tampered, config, forecasts)
    assert "RESERVE_FLOOR" in codes(report)


def test_planned_truncated_plan_is_detected():
    config, forecasts, plan = valid_plan_and_inputs()
    assert "PLAN_LENGTH" in codes(audit_plan(replace(plan, slots=plan.slots[:-1]), config, forecasts))


def test_non_finite_price_fails_input_audit():
    config, _forecasts, _plan = valid_plan_and_inputs()
    forecasts = replace(_forecasts, prices=tuple(
        [replace(_forecasts.prices[0], price=float("nan")), *_forecasts.prices[1:]]
    ))
    assert not audit_forecasts(config, forecasts).passed


def test_out_of_order_timestamps_fail_input_audit():
    config, _forecasts, _plan = valid_plan_and_inputs()
    points = list(_forecasts.prices)
    points[1] = replace(points[1], timestamp=points[0].timestamp)
    forecasts = replace(_forecasts, prices=tuple(points))
    assert "TIME_ORDER" in {v.code for v in audit_forecasts(config, forecasts).hard_violations}


def test_fallback_plan_is_zero_power_and_passes_audit():
    config, forecasts, _plan = valid_plan_and_inputs()
    fallback = make_fallback_plan(config, forecasts, "test")
    assert fallback.status == "FALLBACK"
    assert all(slot.battery_kw == 0.0 and slot.ev_kw == 0.0 for slot in fallback.slots)
    assert audit_plan(fallback, config, forecasts).passed
    commands = plan_commands(config, fallback)
    assert commands[0].value == 0.0  # battery power
    assert commands[1].value == config.battery.initial_soc_pct  # preserve current SOC
    if config.ev is not None:
        assert commands[2].value == 0.0  # EV power


def test_fallback_warning_is_explicit():
    config, forecasts, _plan = valid_plan_and_inputs()
    fallback = make_fallback_plan(config, forecasts, "sensor disconnected")
    assert any("Fail-safe" in warning and "sensor disconnected" in warning for warning in fallback.warnings)
