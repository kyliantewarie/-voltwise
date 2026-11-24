"""Solar and consumption forecast assembly."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .config import VoltWiseConfig
from .models import Forecasts, PricePoint

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .ha_client import HomeAssistantClient


def _number_from_state(state: dict) -> float:
    try:
        value = float(state["state"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("sensor state is not numeric") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("sensor state is not finite")
    return value


def _series_from_entity(client: HomeAssistantClient, entity_id: str, n_slots: int) -> list[float]:
    state = client.get_state(entity_id)
    attributes = state.get("attributes", {}) if isinstance(state, dict) else {}
    forecast = attributes.get("forecast") if isinstance(attributes, dict) else None
    if isinstance(forecast, list) and len(forecast) >= n_slots:
        values: list[float] = []
        for item in forecast[:n_slots]:
            if not isinstance(item, dict):
                raise ValueError("forecast entry is not a mapping")
            raw = item.get("solar") if "solar" in item else item.get("power")
            if raw is None:
                raw = item.get("value")
            if raw is None or isinstance(raw, (dict, list)):
                raise ValueError("forecast entry is not numeric")
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("forecast entry is not numeric") from None
            if not math.isfinite(value):
                raise ValueError("forecast entry is not finite")
            values.append(value)
        return values
    # No structured forecast: hold the current measurement constant, which is
    # conservative and never produces an unsafe schedule.
    return [_number_from_state(state)] * n_slots


def build_forecasts(
    config: VoltWiseConfig,
    prices: list[PricePoint],
    client: HomeAssistantClient | None = None,
    *,
    feed_in_prices: list[float] | None = None,
) -> Forecasts:
    """Combine tariff points with solar/consumption forecasts."""
    n = config.n_slots
    if len(prices) != n:
        raise ValueError(f"expected {n} price points, got {len(prices)}")

    if config.forecast.provider == "manual":
        solar = list(config.forecast.solar_kw)
        consumption = list(config.forecast.consumption_kw)
    else:
        if client is None:
            raise ValueError("home_assistant forecast provider requires a client")
        solar = _series_from_entity(client, config.forecast.solar_entity, n)  # type: ignore[arg-type]
        consumption = _series_from_entity(client, config.forecast.consumption_entity, n)  # type: ignore[arg-type]

    if len(solar) != n or len(consumption) != n:
        raise ValueError("forecast series length mismatch")
    if feed_in_prices is not None and len(feed_in_prices) != n:
        raise ValueError("feed-in series length mismatch")
    if feed_in_prices is None and all(p.feed_in_price is not None for p in prices):
        feed_in_prices = [float(p.feed_in_price) for p in prices]  # type: ignore[arg-type]
        if len(feed_in_prices) != n:
            raise ValueError("provider feed-in series length mismatch")

    return Forecasts(
        prices=tuple(prices),
        solar_kw=tuple(max(0.0, v) for v in solar),
        consumption_kw=tuple(max(0.0, v) for v in consumption),
        feed_in_prices=None if feed_in_prices is None else tuple(feed_in_prices),
    )


def apply_live_soc(
    config: VoltWiseConfig, client: HomeAssistantClient | None, *, max_age_seconds: int = 900
) -> tuple[VoltWiseConfig, list[str]]:
    """Override the configured initial SOC with a live sensor, checking freshness."""
    entity = config.battery.soc_entity
    if entity is None:
        return config, []
    if client is None:
        raise ValueError("live SOC sensor requires a Home Assistant client")
    state = client.get_state(entity)
    if not isinstance(state, dict):
        raise ValueError("live SOC sensor state is not a mapping")
    updated = state.get("last_updated") or state.get("last_changed")
    if not isinstance(updated, str):
        raise ValueError("live SOC sensor has no usable timestamp")
    try:
        moment = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("live SOC sensor timestamp is invalid") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - moment).total_seconds()
    if age < -60 or age > max_age_seconds:
        raise ValueError(f"live SOC sensor is {age:.0f}s old (limit {max_age_seconds}s)")
    value = _number_from_state(state)
    if not (config.battery.soc_min_pct - 1e-6 <= value <= config.battery.soc_max_pct + 1e-6):
        raise ValueError(f"live SOC {value}% outside configured battery limits")
    warnings: list[str] = []
    if value < config.battery.reserve_soc_pct:
        warnings.append(
            f"Live SOC {value:.1f}% is below the {config.battery.reserve_soc_pct:.1f}% reserve; "
            "the optimiser will try to recharge before the horizon ends."
        )
    updated = replace(
        config,
        battery=replace(config.battery, initial_soc_pct=value),
    )
    return updated, warnings


def plan_commands(config: VoltWiseConfig, plan) -> list:
    """Translate the first slot of a plan into allow-listed HA writes."""
    from .models import ActuationCommand

    if not plan.slots:
        return []
    slot = plan.slots[0]
    commands = [
        ActuationCommand(
            entity_id=config.actuation.battery_power_entity,
            value=slot.battery_kw,
            slot=0,
            description="battery AC power (+ charge / - discharge)",
        ),
        ActuationCommand(
            entity_id=config.actuation.target_soc_entity,
            value=slot.battery_soc_pct,
            slot=0,
            description="battery target SOC (%)",
        ),
    ]
    if config.ev is not None:
        commands.append(ActuationCommand(
            entity_id=config.actuation.ev_power_entity,
            value=slot.ev_kw,
            slot=0,
            description="EV charge power (kW)",
        ))
    if config.actuation.telemetry_enabled and all(
        math.isfinite(value) for value in (plan.total_cost, plan.savings)
    ):
        status_code = {"OPTIMAL": 0, "DEGRADED": 1, "FALLBACK": 2}.get(plan.status.upper(), 3)
        commands += [
            ActuationCommand("input_number.voltwise_total_cost", plan.total_cost, 0, "telemetry: total cost"),
            ActuationCommand("input_number.voltwise_savings", plan.savings, 0, "telemetry: savings"),
            ActuationCommand("input_number.voltwise_status_code", float(status_code), 0, "telemetry: status code"),
        ]
    return commands
