"""Dynamic-programming battery and EV scheduler.

The objective is the total *economic* site cost over the planning horizon:
grid purchases minus export revenue, battery degradation, plus the change in
value of energy stored in the battery. The last term prevents the optimiser
from simply draining the battery because its starting energy looks free.

Solar curtailment is a first-class control: when the battery is full and the
grid export limit would be exceeded, the plan reduces solar utilisation rather
than producing an infeasible schedule.
"""

from __future__ import annotations

import math
from dataclasses import replace

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
from .models import Forecasts, Plan, PlanSlot
from .safety import audit_plan, make_fallback_plan, stress_test

_INF = 1e15
_TOL = 1e-9


def schedule_ev(config: VoltWiseConfig, forecasts: Forecasts) -> tuple[list[float], list[float], list[str]]:
    """Allocate EV energy into the cheapest slots before the deadline."""
    ev = config.ev
    n = config.n_slots
    if ev is None:
        return [0.0] * n, [0.0] * n, []

    dt = config.interval_minutes / 60.0
    energy = ev.capacity_kwh * ev.initial_soc_pct / 100.0
    required = ev.required_grid_energy_kwh
    warnings: list[str] = []

    # Work in AC *energy* (kWh) so the units cannot be mixed with power (kW).
    per_slot_cap_kwh: dict[int, float] = {}
    for i in range(n):
        if i > ev.deadline_slot:
            per_slot_cap_kwh[i] = 0.0
            continue
        net_without_ev = forecasts.consumption_kw[i] - forecasts.solar_kw[i]
        headroom = config.site.max_grid_import_kw - net_without_ev
        cap_kw = min(ev.max_charge_kw, max(0.0, headroom))
        remaining_capacity_ac_kwh = max(0.0, (ev.capacity_kwh - energy) / ev.charge_efficiency)
        per_slot_cap_kwh[i] = min(cap_kw * dt, remaining_capacity_ac_kwh)

    allocation = [0.0] * n
    remaining = required
    order = sorted(range(ev.deadline_slot + 1), key=lambda i: (forecasts.prices[i].price, i))
    for i in order:
        if remaining <= _TOL:
            break
        amount = min(per_slot_cap_kwh[i], remaining)
        if amount <= _TOL:
            continue
        allocation[i] = amount / dt
        energy += amount * ev.charge_efficiency
        remaining -= amount

    if remaining > 1e-6:
        warnings.append(
            f"EV target of {ev.target_soc_pct}% cannot be reached by slot {ev.deadline_slot} "
            f"with the available grid power; charging as much as safely possible."
        )

    soc: list[float] = []
    level = ev.capacity_kwh * ev.initial_soc_pct / 100.0
    for i in range(n):
        level += allocation[i] * dt * ev.charge_efficiency
        soc.append(100.0 * level / ev.capacity_kwh)
    return allocation, soc, warnings


def _battery_power_schedule(
    config: VoltWiseConfig, forecasts: Forecasts, ev_kw: list[float]
) -> tuple[list[float], list[float]] | None:
    """Return (battery AC power, solar curtailment) per slot, or None if infeasible."""
    battery = config.battery
    n = config.n_slots
    dt = config.interval_minutes / 60.0
    levels = config.soc_levels
    e_floor = battery.capacity_kwh * effective_reserve_pct_with_forecast(config, forecasts) / 100.0
    e_min = min(e_floor, battery.initial_energy_kwh)
    e_max = battery.max_energy_kwh
    span = e_max - e_min
    if span <= 0:
        return None
    value = energy_value(config, forecasts)

    grid_energy = [e_min + span * i / (levels - 1) for i in range(levels)]
    start_idx = min(range(levels), key=lambda k: abs(grid_energy[k] - battery.initial_energy_kwh))
    grid_energy[start_idx] = battery.initial_energy_kwh

    def nearest_index(level: float) -> int:
        return min(levels - 1, max(0, round((level - e_min) / span * (levels - 1))))

    actions: set[float] = {0.0}
    for k in range(config.action_steps + 1):
        fraction = k / config.action_steps
        actions.add(round(fraction * battery.max_charge_kw, 9))
        actions.add(round(-fraction * battery.max_discharge_kw, 9))
    action_list = sorted(actions)

    # When the battery starts below its reserve, forcing a recharge can cost
    # more than the whole optimisation saves. Use the same effective floor the
    # auditor applies; the caller warns that the reserve target is not met.
    reserve_target = e_floor
    dp: list[float] = [
        0.0 if level >= reserve_target - 1e-9 else _INF
        for level in grid_energy
    ]
    parents: list[list[tuple[int, float, float] | None]] = [[None] * levels for _ in range(n)]

    for t in range(n - 1, -1, -1):
        net_without_solar = forecasts.consumption_kw[t] + ev_kw[t]
        next_dp: list[float] = [_INF] * levels

        for i, level in enumerate(grid_energy):
            best_cost = _INF
            best: tuple[int, float, float] | None = None
            for action in action_list:
                delta = inventory_change_kwh(config, action, dt)
                target = level + delta
                if target < e_min - _TOL or target > e_max + _TOL:
                    continue
                j = nearest_index(target)
                actual_delta = grid_energy[j] - level
                if actual_delta > 0:
                    actual_b = actual_delta / (dt * battery.charge_efficiency)
                elif actual_delta < 0:
                    actual_b = actual_delta * battery.discharge_efficiency / dt
                else:
                    actual_b = 0.0
                # Snapping to the energy grid must not break hardware limits.
                if actual_b > battery.max_charge_kw + _TOL or actual_b < -battery.max_discharge_kw - _TOL:
                    continue
                curtailed = curtail_solar(config, forecasts, t, net_without_solar, actual_b)
                if curtailed < 0:
                    continue
                effective_solar = forecasts.solar_kw[t] - curtailed
                net = net_without_solar - effective_solar
                soc_pct = battery.energy_to_soc(grid_energy[j])
                cost = slot_cost(config, forecasts, t, net, actual_b, value, soc_pct)
                candidate = cost + dp[j]
                if candidate < best_cost - 1e-12:
                    best_cost = candidate
                    best = (j, actual_b, curtailed)
            next_dp[i] = best_cost
            parents[t][i] = best
        dp = next_dp

    if dp[start_idx] >= _INF / 2:
        return None

    battery_kw = [0.0] * n
    curtailed_kw = [0.0] * n
    state = start_idx
    for t in range(n):
        transition = parents[t][state]
        if transition is None:
            return None
        state, battery_kw[t], curtailed_kw[t] = transition
    return battery_kw, curtailed_kw


def _simulate(
    config: VoltWiseConfig,
    forecasts: Forecasts,
    ev_kw: list[float],
    battery_kw: list[float],
    ev_soc: list[float],
    curtailed_kw: list[float],
    value: float,
) -> tuple[list[PlanSlot], float, float]:
    battery = config.battery
    dt = config.interval_minutes / 60.0
    energy = battery.initial_energy_kwh
    slots: list[PlanSlot] = []
    total = 0.0
    degradation = 0.0

    for i, point in enumerate(forecasts.prices):
        b = battery_kw[i]
        curtailed = curtailed_kw[i]
        solar = forecasts.solar_kw[i] - curtailed
        net = forecasts.consumption_kw[i] - solar + ev_kw[i]
        grid = net + b
        imp = max(0.0, grid)
        exp = max(0.0, -grid)
        feed_in = feed_in_price(config, forecasts, i)
        energy += inventory_change_kwh(config, b, dt)
        soc_pct = battery.energy_to_soc(energy)
        cost = slot_cost(config, forecasts, i, net, b, value, soc_pct)
        total += cost
        degradation += degradation_cost(config, b, dt, soc_pct)
        slots.append(PlanSlot(
            index=i, timestamp=point.timestamp, price=point.price, feed_in_price=feed_in,
            solar_kw=solar, curtailed_kw=curtailed,
            consumption_kw=forecasts.consumption_kw[i], ev_kw=ev_kw[i], battery_kw=b,
            grid_import_kw=imp, grid_export_kw=exp,
            battery_soc_pct=battery.energy_to_soc(energy),
            ev_soc_pct=None if config.ev is None else ev_soc[i],
            cost=cost,
        ))
    return slots, total, degradation


def _safe_fallback(config: VoltWiseConfig, forecasts: Forecasts, reason: str) -> Plan:
    """Build a fail-safe and guarantee it is internally consistent."""
    from dataclasses import replace as dataclass_replace

    fallback = make_fallback_plan(config, forecasts, reason)
    if not audit_plan(fallback, config, forecasts).passed:
        fallback = dataclass_replace(
            fallback,
            total_cost=0.0,
            baseline_cost=0.0,
            warnings=tuple([*fallback.warnings,
                            "Non-finite costs detected; economic values suppressed."]),
        )
    return fallback


def _apply_ramp_limit(powers: list[float], limit: float, warnings: list[str]) -> tuple[list[float], bool]:
    """Clip setpoint changes to a maximum per-slot ramp."""
    result: list[float] = []
    previous = 0.0
    changed = False
    for power in powers:
        clipped = max(previous - limit, min(previous + limit, power))
        if abs(clipped - power) > 1e-9:
            changed = True
        result.append(clipped)
        previous = clipped
    if changed:
        warnings.append(f"Battery power ramp was limited to {limit:.2f} kW per slot.")
    return result, changed


def optimize(config: VoltWiseConfig, forecasts: Forecasts) -> Plan:
    """Produce a validated, safe plan. Never raises on bad input: fails closed."""
    ev_kw, ev_soc, warnings = schedule_ev(config, forecasts)
    if config.battery.initial_soc_pct < config.battery.reserve_soc_pct:
        warnings.append(
            f"Battery starts below the {config.battery.reserve_soc_pct:.1f}% reserve; "
            "the optimiser will not force an uneconomic recharge."
        )
    value = energy_value(config, forecasts)
    solver_config = config
    pairs = config.n_slots * config.soc_levels * (2 * config.action_steps + 3)
    # Hard ceiling independent of configuration: a valid config must not be
    # able to pin a CPU core for tens of seconds every cycle.
    budget = min(config.solver_budget_pairs, 500_000)
    if pairs > budget:
        levels, steps = config.soc_levels, config.action_steps
        for _ in range(20):
            current = config.n_slots * levels * (2 * steps + 3)
            if current <= budget or (levels <= 11 and steps <= 5):
                break
            scale = math.sqrt(budget / current)
            new_levels = max(11, int(levels * scale))
            new_steps = max(5, int(steps * scale))
            if new_levels == levels and new_steps == steps:
                break
            levels, steps = new_levels, new_steps
        solver_config = replace(config, soc_levels=levels, action_steps=steps)
        warnings.append(
            f"Solver resolution reduced to {levels} SOC levels / "
            f"{steps} power steps to respect the time budget."
        )
    result = _battery_power_schedule(solver_config, forecasts, ev_kw)

    if result is None:
        return _safe_fallback(config, forecasts, "no feasible battery schedule")

    battery_kw, curtailed_kw = result
    if config.max_ramp_kw_per_slot is not None:
        battery_kw, ramped = _apply_ramp_limit(battery_kw, config.max_ramp_kw_per_slot, warnings)
        if ramped:
            recalculated: list[float] = []
            for i, power in enumerate(battery_kw):
                net_without_solar = forecasts.consumption_kw[i] + ev_kw[i]
                needed = curtail_solar(config, forecasts, i, net_without_solar, power)
                if needed < 0:
                    return _safe_fallback(config, forecasts, "grid limits after ramp limiting")
                recalculated.append(needed)
            curtailed_kw = recalculated
    slots, total, degradation = _simulate(
        config, forecasts, ev_kw, battery_kw, ev_soc, curtailed_kw, value
    )

    baseline_curtailment: list[float] = []
    baseline_infeasible = False
    for i in range(config.n_slots):
        net_without_solar = forecasts.consumption_kw[i] + ev_kw[i]
        amount = curtail_solar(config, forecasts, i, net_without_solar, 0.0)
        if amount < 0:
            baseline_infeasible = True
            baseline_curtailment.append(forecasts.solar_kw[i])
        else:
            baseline_curtailment.append(amount)
    _, baseline, _ = _simulate(
        config, forecasts, ev_kw, [0.0] * config.n_slots, ev_soc, baseline_curtailment, value
    )
    if baseline_infeasible:
        # A no-battery reference cannot respect the grid limits in this
        # configuration; claiming a saving against it would be misleading.
        baseline = total
        warnings.append("No-battery baseline is infeasible under the grid limits; savings not meaningful.")
    if total > baseline + 1e-9:
        # Ramp/degradation constraints can make the optimised plan costlier
        # than doing nothing. Never claim negative savings.
        baseline = total
        warnings.append("Constraints made the optimised plan no cheaper than the baseline; no saving claimed.")

    plan = Plan(
        slots=tuple(slots),
        total_cost=total,
        baseline_cost=baseline,
        degradation_cost=degradation,
        status="DEGRADED" if warnings else "OPTIMAL",
        warnings=tuple(warnings),
    )

    audit = audit_plan(plan, config, forecasts)
    if not audit.passed:
        details = "; ".join(v.code for v in audit.hard_violations[:5])
        return _safe_fallback(config, forecasts, f"post-optimisation safety audit failed: {details}")

    if config.forecast.uncertainty_pct > 0:
        stressed = stress_test(plan, config, forecasts, config.forecast.uncertainty_pct)
        if not stressed.passed:
            plan = replace(
                plan,
                warnings=tuple([*plan.warnings,
                                "Plan exceeds grid limits under forecast uncertainty; "
                                "consider a larger reserve or tighter ramp limits."]),
                status="DEGRADED",
            )
    return plan
