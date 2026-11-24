"""Guided setup: discover Home Assistant helpers and write a starter config."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .config import VoltWiseConfig
from .ha_client import HomeAssistantError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .ha_client import HomeAssistantClient

_SUGGESTIONS = {
    "battery_power": ("number.voltwise_battery_power", "number.battery_power"),
    "target_soc": ("input_number.voltwise_target_soc", "input_number.battery_target_soc"),
    "ev_power": ("number.voltwise_ev_charge_power", "number.ev_charger_power"),
    "soc_sensor": ("sensor.battery_soc", "sensor.battery_state_of_charge"),
    "consumption": ("sensor.home_consumption", "sensor.power_consumption"),
    "solar": ("sensor.solar_power", "sensor.pv_power"),
}


def discover(client: HomeAssistantClient) -> dict[str, str]:
    """Find the first existing entity for each suggested helper."""
    found: dict[str, str] = {}
    for role, candidates in _SUGGESTIONS.items():
        for entity in candidates:
            try:
                client.get_state(entity)
                found[role] = entity
                break
            except (HomeAssistantError, KeyError, ValueError):
                continue
    return found


def build_starter_config(discovered: dict[str, str]) -> dict:
    config = VoltWiseConfig.from_dict({
        "n_slots": 48,
        "interval_minutes": 60,
        "battery": {"capacity_kwh": 10.0, "initial_soc_pct": 50.0,
                    "soc_entity": discovered.get("soc_sensor")},
        "site": {"max_grid_import_kw": 10.0, "max_grid_export_kw": 10.0},
        "tariff": {"provider": "manual", "manual_prices": [0.1] * 48},
        "forecast": {
            "provider": "home_assistant" if discovered.get("solar") and discovered.get("consumption") else "manual",
            "solar_entity": discovered.get("solar"),
            "consumption_entity": discovered.get("consumption"),
            "solar_kw": [0.0] * 48,
            "consumption_kw": [0.5] * 48,
        },
        "actuation": {
            "enabled": False,
            "battery_power_entity": discovered.get("battery_power", "number.voltwise_battery_power"),
            "target_soc_entity": discovered.get("target_soc", "input_number.voltwise_target_soc"),
            "ev_power_entity": discovered.get("ev_power", "number.voltwise_ev_charge_power"),
        },
    })
    return config.to_dict()


def interactive_setup(client: HomeAssistantClient, output: str | Path,
                      prompt: Callable[[str], str] | None = None) -> dict:
    """Discover entities, ask the operator to confirm, and write the config."""
    discovered = discover(client)
    config = build_starter_config(discovered)
    if prompt is not None:
        answer = prompt(
            "Discovered entities: " + json.dumps(discovered) + "\n"
            "Write this starter config? [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            raise RuntimeError("setup cancelled by operator")
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config
