"""Independent safety checks and fail-close behaviour.

Nothing in this module trusts the optimiser. A plan is only actuated after it
has been re-simulated from the raw inputs and every hard constraint has been
checked again. Any hard violation downgrades the plan to a safe fallback that
requests zero power from every actuator.
"""

from __future__ import annotations

import math
from datetime import UTC

from .config import VoltWiseConfig
from .economics import (
    curtail_solar,
    degradation_cost,
    effective_reserve_pct_with_forecast,
    energy_value,
    feed_in_price,
    inventory_change_kwh,
    slot_cost,
)
from .models import AuditReport, Forecasts, Plan, PlanSlot, Violation, slot_timestamps

_TOL = 1e-6
_SOC_TOL = 1e-4


def _violation(code: str, severity: str, slot: int | None, message: str) -> Violation:
    return Violation(code=code, severity=severity, slot=slot, message=message)


def audit_forecasts(config: VoltWiseConfig, forecasts: Forecasts) -> AuditReport:
    """Validate raw inputs before the optimiser is allowed to run."""
    hard: list[Violation] = []
    checks = 0
    n = config.n_slots

    if forecasts.n_slots != n:
        hard.append(_violation("INPUT_LENGTH", "HARD", None,
                               f"expected {n} price points, got {forecasts.n_slots}"))
        return AuditReport(False, tuple(hard), (), 1)
    if len(forecasts.solar_kw) != n or len(forecasts.consumption_kw) != n:
        hard.append(_violation("FORECAST_LENGTH", "HARD", None, "forecast series length mismatch"))
        return AuditReport(False, tuple(hard), (), 1)
    if forecasts.feed_in_prices is not None and len(forecasts.feed_in_prices) != n:
        hard.append(_violation("FEEDIN_LENGTH", "HARD", None, "feed-in series length mismatch"))

    # Providers and tests may mix aware/naive datetimes; compare in UTC.
    dt = config.interval_minutes / 60.0
    required_energy = sum(forecasts.consumption_kw) * dt
    if config.ev is not None:
        required_energy += config.ev.required_grid_energy_kwh
    available_energy = (
        config.site.max_grid_import_kw * n * dt
        + sum(forecasts.solar_kw) * dt
        + max(0.0, config.battery.initial_energy_kwh - config.battery.min_energy_kwh)
    )
    if required_energy > available_energy + _TOL:
        hard.append(_violation(
            "SITE_INFEASIBLE", "HARD", None,
            "load cannot be served over the horizon by grid, solar and stored energy",
        ))

    max_supply = (
        config.site.max_grid_import_kw + config.battery.max_discharge_kw
    )
    for i in range(n):
        checks += 1
        available = max_supply + forecasts.solar_kw[i]
        if forecasts.consumption_kw[i] > available + _TOL:
            hard.append(_violation(
                "SITE_INFEASIBLE", "HARD", i,
                "fixed load exceeds grid import + battery discharge + solar capacity",
            ))

    timestamps = [
        p.timestamp if p.timestamp.tzinfo is not None
        else p.timestamp.replace(tzinfo=UTC)
        for p in forecasts.prices
    ]
    for i in range(1, n):
        checks += 1
        if timestamps[i] <= timestamps[i - 1]:
            hard.append(_violation("TIME_ORDER", "HARD", i, "price timestamps are not strictly increasing"))

    for i, point in enumerate(forecasts.prices):
        checks += 4
        if not math.isfinite(point.price):
            hard.append(_violation("PRICE_FINITE", "HARD", i, "price is not finite"))
        if point.feed_in_price is not None and not math.isfinite(point.feed_in_price):
            hard.append(_violation("FEEDIN_FINITE", "HARD", i, "feed-in price is not finite"))
        if not math.isfinite(forecasts.solar_kw[i]) or forecasts.solar_kw[i] < -_TOL:
            hard.append(_violation("SOLAR_RANGE", "HARD", i, "solar forecast must be finite and non-negative"))
        if not math.isfinite(forecasts.consumption_kw[i]) or forecasts.consumption_kw[i] < -_TOL:
            hard.append(_violation("LOAD_RANGE", "HARD", i, "consumption forecast must be finite and non-negative"))

    if forecasts.feed_in_prices is not None:
        for i, value in enumerate(forecasts.feed_in_prices):
            checks += 1
            if not math.isfinite(value):
                hard.append(_violation("FEEDIN_FINITE", "HARD", i, "feed-in price is not finite"))

    return AuditReport(not hard, tuple(hard), (), checks)


def audit_plan(plan: Plan, config: VoltWiseConfig, forecasts: Forecasts) -> AuditReport:
    """Re-simulate a plan against physical, legal and economic limits."""
    hard: list[Violation] = []
    soft: list[Violation] = []
    checks = 0
    battery = config.battery
    site = config.site
    ev = config.ev
    value = energy_value(config, forecasts)
    tolerance = _TOL

    if len(plan.slots) != config.n_slots:
        return AuditReport(False, (_violation("PLAN_LENGTH", "HARD", None,
                                              f"expected {config.n_slots} slots, got {len(plan.slots)}"),), (), 1)

    energy = battery.initial_energy_kwh
    reserve_floor_kwh = battery.capacity_kwh * effective_reserve_pct_with_forecast(config, forecasts) / 100.0
    ev_energy = 0.0 if ev is None else ev.capacity_kwh * ev.initial_soc_pct / 100.0
    total_cost = 0.0
    degradation_total = 0.0
    previous_power = 0.0

    for slot in plan.slots:
        i = slot.index
        checks += 16
        b = slot.battery_kw
        ev_kw = slot.ev_kw
        if abs(slot.consumption_kw - forecasts.consumption_kw[i]) > tolerance:
            hard.append(_violation("CONSUMPTION_MISMATCH", "HARD", i,
                                   "plan consumption does not match the forecast"))
        net = slot.consumption_kw - slot.solar_kw + ev_kw
        grid = net + b
        expected_import = max(0.0, grid)
        expected_export = max(0.0, -grid)

        if abs(slot.grid_import_kw - expected_import) > tolerance:
            hard.append(_violation("POWER_BALANCE", "HARD", i, "grid import does not match site balance"))
        if abs(slot.grid_export_kw - expected_export) > tolerance:
            hard.append(_violation("POWER_BALANCE", "HARD", i, "grid export does not match site balance"))

        original_solar = forecasts.solar_kw[i]
        if slot.curtailed_kw < -tolerance or slot.curtailed_kw > original_solar + tolerance:
            hard.append(_violation("CURTAIL_RANGE", "HARD", i, "curtailment outside available solar"))
        if abs(slot.solar_kw + slot.curtailed_kw - original_solar) > tolerance:
            hard.append(_violation("CURTAIL_BALANCE", "HARD", i, "solar plus curtailment does not match forecast"))

        feed_in = feed_in_price(config, forecasts, i)
        expected_cost = slot_cost(config, forecasts, i, net, b, value, slot.battery_soc_pct)
        if not all(math.isfinite(x) for x in (
            forecasts.prices[i].price, feed_in, slot.cost, expected_cost
        )):
            hard.append(_violation("NON_FINITE_COST", "HARD", i, "price or cost is not finite"))
        elif abs(slot.cost - expected_cost) > max(tolerance, abs(expected_cost) * 1e-6):
            hard.append(_violation("COST_MISMATCH", "HARD", i, "slot cost does not match recomputation"))
        total_cost += expected_cost
        degradation_total += degradation_cost(
            config, b, config.interval_minutes / 60.0, slot.battery_soc_pct
        )

        for name, check_value in (("battery_kw", b), ("ev_kw", ev_kw),
                                  ("import", expected_import), ("export", expected_export),
                                  ("soc", slot.battery_soc_pct), ("curtailed", slot.curtailed_kw),
                                  ("cost", slot.cost), ("price", forecasts.prices[i].price),
                                  ("feed_in", feed_in),
                                  ("consumption", forecasts.consumption_kw[i]),
                                  ("solar", forecasts.solar_kw[i])):
            if not math.isfinite(check_value):
                hard.append(_violation("NON_FINITE", "HARD", i, f"{name} is not finite"))

        if b > battery.max_charge_kw + tolerance:
            hard.append(_violation("CHARGE_LIMIT", "HARD", i, "battery charge power above hardware limit"))
        if -b > battery.max_discharge_kw + tolerance:
            hard.append(_violation("DISCHARGE_LIMIT", "HARD", i, "battery discharge power above hardware limit"))
        if config.max_ramp_kw_per_slot is not None:
            if abs(b - previous_power) > config.max_ramp_kw_per_slot + tolerance:
                hard.append(_violation("RAMP_LIMIT", "HARD", i, "battery power ramp above configured limit"))
        previous_power = b
        # A fail-safe plan deliberately does not actuate; passive load can still
        # breach a limit when the site cannot serve it without control. Record
        # that as informational rather than pretending the fallback is invalid.
        limit_severity = "SOFT" if plan.status == "FALLBACK" else "HARD"
        if expected_import > site.max_grid_import_kw + tolerance:
            target = soft if limit_severity == "SOFT" else hard
            target.append(_violation("IMPORT_LIMIT", limit_severity, i,
                                     "grid import above connection limit"))
        if expected_export > site.max_grid_export_kw + tolerance:
            target = soft if limit_severity == "SOFT" else hard
            target.append(_violation("EXPORT_LIMIT", limit_severity, i,
                                     "grid export above connection limit"))

        energy += inventory_change_kwh(config, b, config.interval_minutes / 60.0)
        if energy < battery.min_energy_kwh - _SOC_TOL:
            hard.append(_violation("SOC_FLOOR", "HARD", i, "battery SOC below hard minimum"))
        if energy < reserve_floor_kwh - _SOC_TOL:
            hard.append(_violation("RESERVE_FLOOR", "HARD", i,
                                   "battery SOC below the operational reserve in a non-final slot"))
        if energy > battery.max_energy_kwh + _SOC_TOL:
            hard.append(_violation("SOC_CEILING", "HARD", i, "battery SOC above hard maximum"))
        expected_soc = battery.energy_to_soc(energy)
        if abs(slot.battery_soc_pct - expected_soc) > _SOC_TOL:
            hard.append(_violation("SOC_MISMATCH", "HARD", i, "reported SOC does not match energy model"))

        if ev is not None:
            if ev_kw > ev.max_charge_kw + tolerance:
                hard.append(_violation("EV_POWER_LIMIT", "HARD", i, "EV charge power above limit"))
            if ev_kw < -tolerance:
                hard.append(_violation("EV_REVERSE", "HARD", i, "EV must not discharge to the site"))
            ev_energy += ev_kw * config.interval_minutes / 60.0 * ev.charge_efficiency
            if ev_energy > ev.capacity_kwh + _SOC_TOL:
                hard.append(_violation("EV_OVERCHARGE", "HARD", i, "EV SOC would exceed 100%"))
            expected_ev_soc = 100.0 * ev_energy / ev.capacity_kwh
            if slot.ev_soc_pct is None or abs(slot.ev_soc_pct - expected_ev_soc) > _SOC_TOL:
                hard.append(_violation("EV_SOC_MISMATCH", "HARD", i, "reported EV SOC does not match energy model"))
            if i == ev.deadline_slot and ev_energy < ev.capacity_kwh * ev.target_soc_pct / 100.0 - _SOC_TOL:
                soft.append(_violation("EV_TARGET_MISSED", "SOFT", i, "EV target SOC not reached by deadline"))

    last_soc = battery.energy_to_soc(energy)
    # Same effective floor as the per-slot check, including any forecast margin.
    effective_reserve = battery.energy_to_soc(reserve_floor_kwh)
    if last_soc < effective_reserve - _SOC_TOL:
        hard.append(_violation("RESERVE_MISSED", "HARD", None,
                               f"final SOC {last_soc:.3f}% below reserve {effective_reserve:.3f}%"))
    if not all(math.isfinite(x) for x in (total_cost, plan.total_cost,
                                          degradation_total, plan.degradation_cost)):
        hard.append(_violation("NON_FINITE_COST", "HARD", None, "plan total cost is not finite"))
    elif abs(total_cost - plan.total_cost) > max(tolerance, abs(plan.total_cost) * 1e-6):
        hard.append(_violation("TOTAL_COST_MISMATCH", "HARD", None, "plan total cost does not match recomputation"))
    if abs(degradation_total - plan.degradation_cost) > max(tolerance, abs(plan.degradation_cost) * 1e-6):
        hard.append(_violation("DEGRADATION_MISMATCH", "HARD", None, "degradation cost mismatch"))

    return AuditReport(not hard, tuple(hard), tuple(soft), checks)


def stress_test(plan: Plan, config: VoltWiseConfig, forecasts: Forecasts, uncertainty_pct: float) -> AuditReport:
    """Re-check the plan against a worse-than-forecast load/solar scenario."""
    violations: list[Violation] = []
    for slot in plan.slots:
        i = slot.index
        load = forecasts.consumption_kw[i] * (1.0 + uncertainty_pct)
        stressed_solar = max(0.0, forecasts.solar_kw[i] * (1.0 - uncertainty_pct) - slot.curtailed_kw)
        grid = load - stressed_solar + slot.ev_kw + slot.battery_kw
        if grid > config.site.max_grid_import_kw + _TOL:
            violations.append(_violation("STRESS_IMPORT_LIMIT", "HARD", i,
                                         "plan exceeds import limit under forecast uncertainty"))
        if -grid > config.site.max_grid_export_kw + _TOL:
            violations.append(_violation("STRESS_EXPORT_LIMIT", "HARD", i,
                                         "plan exceeds export limit under forecast uncertainty"))
    return AuditReport(not violations, tuple(violations), (), len(plan.slots) * 2)


def make_fallback_plan(config: VoltWiseConfig, forecasts: Forecasts, reason: str) -> Plan:
    """Build a plan that requests zero power everywhere and preserves SOC."""
    battery = config.battery
    value = energy_value(config, forecasts)
    slots: list[PlanSlot] = []
    ev_energy = 0.0 if config.ev is None else config.ev.capacity_kwh * config.ev.initial_soc_pct / 100.0
    cost = 0.0
    over_limit = False

    for i, point in enumerate(forecasts.prices):
        solar_forecast = forecasts.solar_kw[i]
        curtailed = curtail_solar(config, forecasts, i, forecasts.consumption_kw[i], 0.0)
        if curtailed < 0:
            curtailed = solar_forecast
        solar = solar_forecast - curtailed
        net = forecasts.consumption_kw[i] - solar
        grid = net
        if grid > config.site.max_grid_import_kw + _TOL:
            over_limit = True
        slot_cost_value = slot_cost(config, forecasts, i, net, 0.0, value, battery.initial_soc_pct)
        cost += slot_cost_value
        slots.append(PlanSlot(
            index=i, timestamp=point.timestamp, price=point.price,
            feed_in_price=feed_in_price(config, forecasts, i),
            solar_kw=solar, curtailed_kw=curtailed,
            consumption_kw=forecasts.consumption_kw[i], ev_kw=0.0, battery_kw=0.0,
            grid_import_kw=max(0.0, grid), grid_export_kw=max(0.0, -grid),
            battery_soc_pct=battery.energy_to_soc(battery.initial_energy_kwh),
            ev_soc_pct=None if config.ev is None else 100.0 * ev_energy / config.ev.capacity_kwh,
            cost=slot_cost_value,
        ))

    warnings = [f"Fail-safe activated: {reason}. No actuation requested."]
    if over_limit:
        warnings.append(
            "Passive load exceeds the grid import limit; the fail-safe cannot "
            "correct this without actuation."
        )
    return Plan(
        slots=tuple(slots),
        total_cost=cost,
        baseline_cost=cost,
        degradation_cost=0.0,
        status="FALLBACK",
        warnings=tuple(warnings),
    )


def plan_timestamps(config: VoltWiseConfig, start) -> tuple:
    """Convenience wrapper used by the CLI when building synthetic forecasts."""
    return slot_timestamps(start, config.n_slots, config.interval_minutes)
