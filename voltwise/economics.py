"""Shared economic model.

Keeping the cost formula in one module means the optimiser and the independent
auditor cannot silently drift apart. ``slot_cost`` reports an *economic* cost:
grid cash flow plus the change in value of energy stored plus a state-dependent
battery degradation estimate.
"""

from __future__ import annotations

import math

from .config import VoltWiseConfig
from .models import BatterySpec, Forecasts


def effective_reserve_pct(battery: BatterySpec) -> float:
    """Operational discharge floor for this run.

    Normally the reserve is the floor at every slot. If the battery starts
    below its reserve, the current SOC becomes the temporary floor so the
    fail-safe remains achievable.
    """
    return min(max(battery.soc_min_pct, battery.reserve_soc_pct), battery.initial_soc_pct)


def effective_reserve_pct_with_forecast(config: VoltWiseConfig, forecasts: Forecasts) -> float:
    """Add a configurable margin when the solar forecast is volatile."""
    base = effective_reserve_pct(config.battery)
    solar = forecasts.solar_kw
    spread = max(solar) - min(solar) if solar else 0.0
    margin = 0.0
    if spread > config.battery.reserve_margin_solar_threshold_kw:
        margin = config.battery.reserve_margin_pct
    return min(base + margin, config.battery.initial_soc_pct, config.battery.soc_max_pct)


def buy_price(config: VoltWiseConfig, forecasts: Forecasts, index: int) -> float:
    return forecasts.prices[index].price + config.tariff.buy_fee_per_kwh


def feed_in_price(config: VoltWiseConfig, forecasts: Forecasts, index: int) -> float:
    fee = config.tariff.sell_fee_per_kwh
    if forecasts.feed_in_prices is not None:
        return forecasts.feed_in_prices[index] - fee
    return forecasts.prices[index].price * config.site.feed_in_factor - fee


def energy_value(config: VoltWiseConfig, forecasts: Forecasts) -> float:
    """Value of 1 kWh stored, derived from the mean buy price when unset."""
    configured = config.battery.energy_value_per_kwh
    if configured is not None:
        return configured
    mean_price = sum(buy_price(config, forecasts, i) for i in range(forecasts.n_slots)) / max(1, forecasts.n_slots)
    if not math.isfinite(mean_price):
        return 0.0
    return min(1e6, max(0.0, mean_price) / config.battery.discharge_efficiency)


def inventory_change_kwh(config: VoltWiseConfig, battery_kw: float, dt_hours: float) -> float:
    battery = config.battery
    if battery_kw > 0:
        return battery_kw * dt_hours * battery.charge_efficiency
    if battery_kw < 0:
        return battery_kw * dt_hours / battery.discharge_efficiency
    return 0.0


def degradation_cost(config: VoltWiseConfig, battery_kw: float, dt_hours: float, soc_pct: float) -> float:
    """Throughput cost weighted by state of charge (deep cycles cost more)."""
    weight = 1.0 + config.battery.degradation_soc_penalty * (max(0.0, min(100.0, soc_pct)) / 100.0) ** 2
    return abs(battery_kw) * dt_hours * config.battery.degradation_cost_per_kwh * weight


def slot_cost(
    config: VoltWiseConfig,
    forecasts: Forecasts,
    index: int,
    net_load_kw: float,
    battery_kw: float,
    value_per_kwh: float,
    soc_pct: float,
) -> float:
    """Economic cost of one interval."""
    dt = config.interval_minutes / 60.0
    grid = net_load_kw + battery_kw
    cash = max(0.0, grid) * buy_price(config, forecasts, index) * dt
    cash -= max(0.0, -grid) * feed_in_price(config, forecasts, index) * dt
    degradation = degradation_cost(config, battery_kw, dt, soc_pct)
    inventory = -value_per_kwh * inventory_change_kwh(config, battery_kw, dt)
    return cash + degradation + inventory


def curtail_solar(
    config: VoltWiseConfig, forecasts: Forecasts, index: int,
    net_without_solar_kw: float, battery_kw: float,
) -> float:
    """Minimum plus economically justified solar curtailment (kW)."""
    solar = forecasts.solar_kw[index]
    grid_uncurtailed = net_without_solar_kw - solar + battery_kw
    needed = max(0.0, -config.site.max_grid_export_kw - grid_uncurtailed)
    if needed > solar + 1e-9:
        return -1.0
    feed_in = feed_in_price(config, forecasts, index)
    remaining_export = max(0.0, -(grid_uncurtailed + needed))
    economic = remaining_export if feed_in < 0 else 0.0
    total = min(solar, needed + economic)
    grid = grid_uncurtailed + total
    if grid > config.site.max_grid_import_kw + 1e-9:
        return -1.0
    return total
