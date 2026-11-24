"""End-to-end simulation runner.

Drives VoltWise through a real HTTP client against the fake HA server, advancing
device physics every cycle, enforcing the watchdog, and collecting evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import voltwise.cli as cli_module
from voltwise.cli import run_once
from voltwise.config import VoltWiseConfig
from voltwise.ha_client import HomeAssistantClient

from .devices import Battery, EvCharger, SimulationState, Site
from .ha_server import start_fake_ha

# Guards the module-level watchdog timestamp patch so concurrent simulations
# cannot restore each other's closure.
_PATCH_LOCK = threading.Lock()


@dataclass
class CycleResult:
    index: int
    exit_code: int
    soc_pct: float
    ev_soc_pct: float
    battery_kw: float
    measured_import_kw: float
    expired: bool
    optimiser_soc: float = 50.0


@dataclass
class SimulationResult:
    cycles: list[CycleResult] = field(default_factory=list)
    watchdog_trips: int = 0
    bms_cutoffs: int = 0
    energy_cost: float = 0.0
    service_calls: int = 0
    failed_service_calls: int = 0
    setpoints: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "cycles": [vars(c) for c in self.cycles],
            "watchdog_trips": self.watchdog_trips,
            "bms_cutoffs": self.bms_cutoffs,
            "energy_cost": round(self.energy_cost, 4),
            "service_calls": self.service_calls,
            "failed_service_calls": self.failed_service_calls,
            "setpoints": self.setpoints,
        }


def base_config(n_slots: int = 4, interval_minutes: int = 60) -> dict:
    return {
        "n_slots": n_slots,
        "interval_minutes": interval_minutes,
        "battery": {
            "capacity_kwh": 10.0, "initial_soc_pct": 50.0,
            "soc_min_pct": 10.0, "soc_max_pct": 95.0, "reserve_soc_pct": 20.0,
            "max_charge_kw": 5.0, "max_discharge_kw": 5.0,
            "soc_entity": "sensor.battery_soc",
        },
        "site": {
            "max_grid_import_kw": 10.0, "max_grid_export_kw": 10.0,
            "feed_in_factor": 0.3,
        },
        "tariff": {"provider": "home_assistant", "price_entity": "sensor.nord_pool_nl_today"},
        "forecast": {"provider": "manual", "solar_kw": [0.0] * n_slots,
                     "consumption_kw": [0.5] * n_slots},
        "actuation": {
            "enabled": True, "require_confirmation": False,
            "ha_token_env": "VOLTWISE_SIM_TOKEN",
            "measured_import_entity": "sensor.voltwise_measured_import",
            "max_sensor_age_seconds": 900,
        },
        "licensing": {"require": False},
    }


def _devices_from_config(config: VoltWiseConfig, interval_minutes: int) -> SimulationState:
    dt = interval_minutes / 60.0
    ramp_per_hour = (config.max_ramp_kw_per_slot or 100.0) / dt
    battery = Battery(
        capacity_kwh=config.battery.capacity_kwh,
        soc_pct=config.battery.initial_soc_pct,
        initial_soc_pct=config.battery.initial_soc_pct,
        soc_min_pct=config.battery.soc_min_pct,
        soc_max_pct=config.battery.soc_max_pct,
        max_charge_kw=config.battery.max_charge_kw,
        max_discharge_kw=config.battery.max_discharge_kw,
        charge_efficiency=config.battery.charge_efficiency,
        discharge_efficiency=config.battery.discharge_efficiency,
        ramp_kw_per_hour=ramp_per_hour,
    )
    ev = EvCharger()
    if config.ev is not None:
        ev = EvCharger(
            capacity_kwh=config.ev.capacity_kwh,
            soc_pct=config.ev.initial_soc_pct,
            max_charge_kw=config.ev.max_charge_kw,
            efficiency=config.ev.charge_efficiency,
        )
    consumption = config.forecast.consumption_kw
    solar = config.forecast.solar_kw
    site = Site(
        load_kw=consumption[0] if consumption else 0.5,
        solar_kw=solar[0] if solar else 0.0,
        feed_in_cost=config.site.feed_in_factor,
    )
    return SimulationState(battery=battery, ev=ev, site=site)


def run_simulation(
    config_dict: dict,
    cycles: int = 4,
    *,
    stop_optimiser_after: int | None = None,
    fail_services: set[str] | None = None,
    plan_json: str | Path | None = None,
    past_expiry: bool = False,
) -> SimulationResult:
    """Run the optimiser against simulated hardware for ``cycles`` intervals."""
    config = VoltWiseConfig.from_dict(config_dict)
    descriptor, config_name = tempfile.mkstemp(prefix="voltwise_sim_", suffix=".json")
    config_path = Path(config_name)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(config_dict))

    sim = _devices_from_config(config, config.interval_minutes)
    step = timedelta(minutes=config.interval_minutes)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    raw_prices = [0.05, 0.04, 0.03, 0.40, 0.50, 0.45, 0.20, 0.08]
    prices = raw_prices * max(1, int(config.n_slots / len(raw_prices)) + 1)
    server, base_url = start_fake_ha(
        sim, prices=prices[:config.n_slots + cycles + 2],
        dt_hours=config.interval_minutes / 60.0,
        fail_services=fail_services, price_start=now,
        price_step_minutes=config.interval_minutes,
    )
    output = Path(plan_json) if plan_json else None
    # Token and cooldown come from the validated configuration.
    token_env = config.actuation.ha_token_env or "HA_TOKEN"
    os.environ.setdefault(token_env, "sim-token")
    token = os.environ.get(token_env)
    client = HomeAssistantClient(
        base_url, token=token, dry_run=False,
        failure_cooldown_seconds=config.actuation.failure_cooldown_seconds,
    )
    result = SimulationResult()
    optimiser_socs: list[float] = []
    original_optimize = cli_module.optimize

    def recording_optimize(cfg, forecasts):
        optimiser_socs.append(cfg.battery.initial_soc_pct)
        return original_optimize(cfg, forecasts)

    original_timestamp = cli_module._watchdog_timestamp
    with _PATCH_LOCK:
        try:
            cli_module._watchdog_timestamp = lambda cfg, _now=now: (
                _now + timedelta(seconds=2 * cfg.interval_minutes * 60)
            ).isoformat(timespec="seconds")
            cli_module.optimize = recording_optimize
            for i in range(cycles):
                if past_expiry:
                    sim.plan_expiry = "2020-01-01T00:00:00+00:00"
                expired = datetime.fromisoformat(sim.plan_expiry) < now
                if expired:
                    sim.force_safe()

                sim.site.load_kw = (
                    config.forecast.consumption_kw[i % len(config.forecast.consumption_kw)]
                    if config.forecast.consumption_kw else 0.5
                )
                sim.site.solar_kw = (
                    config.forecast.solar_kw[i % len(config.forecast.solar_kw)]
                    if config.forecast.solar_kw else 0.0
                )

                stopped = stop_optimiser_after is not None and i >= stop_optimiser_after
                if not stopped:
                    args = SimpleNamespace(
                        config=str(config_path), start=now.isoformat(), apply=True,
                        confirm=True, dry_run=False, validate_only=False,
                        output=str(output) if output else None, setup=False,
                        diagnostics=None, watchdog_automation=None, ha_client=client,
                    )
                    code = run_once(args)
                else:
                    code = -1

                sim.site.update(
                    sim.battery.last_applied_kw, sim.ev.last_applied_kw,
                    config.interval_minutes / 60.0, prices[i % len(prices)],
                )
                result.cycles.append(CycleResult(
                    index=i, exit_code=code, soc_pct=sim.battery.soc_pct,
                    ev_soc_pct=sim.ev.soc_pct, battery_kw=sim.battery.last_applied_kw,
                    measured_import_kw=sim.site.measured_import_kw, expired=expired,
                    optimiser_soc=optimiser_socs[-1] if optimiser_socs else config.battery.initial_soc_pct,
                ))
                now += step
        finally:
            cli_module._watchdog_timestamp = original_timestamp
            cli_module.optimize = original_optimize
            server.shutdown()
            server.server_close()
            config_path.unlink(missing_ok=True)
    result.watchdog_trips = sim.watchdog_trips
    result.bms_cutoffs = sim.battery.bms_cutoffs
    result.energy_cost = sim.site.energy_cost
    result.service_calls = len(sim.service_calls)
    result.failed_service_calls = len(sim.failed_service_calls)
    result.setpoints = dict(sim.setpoints)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the VoltWise hardware simulator")
    parser.add_argument("--cycles", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(run_simulation(base_config(), cycles=args.cycles).to_dict(), indent=2))


if __name__ == "__main__":
    main()
