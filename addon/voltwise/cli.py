"""Command-line entry point used by the add-on and by local test runs."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, VoltWiseConfig
from .forecasts import apply_live_soc, build_forecasts, plan_commands
from .ha_client import HomeAssistantClient, HomeAssistantError, WriteResult
from .models import AuditReport, Plan
from .optimizer import optimize
from .safety import audit_forecasts
from .tariffs import PriceFetchError, create_provider

LOGGER = logging.getLogger("voltwise")


def load_config(path: str | Path) -> dict[str, Any]:
    """Load JSON or YAML with a safe loader; never construct Python objects."""
    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"configuration file not found: {source}")
    text = source.read_text(encoding="utf-8", errors="strict")
    if source.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise ConfigError(f"invalid JSON: {type(exc).__name__}") from None
    else:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover - CLI dependency guard
            raise ConfigError("YAML configuration requires PyYAML; use JSON instead") from None
        try:
            data = yaml.safe_load(text)  # safe_load only: no arbitrary tags
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML: {type(exc).__name__}") from None
        except RecursionError:
            raise ConfigError("YAML nesting is too deep") from None
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")
    return data


def round_start(value: datetime, minutes: int) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    remainder = (value.minute * 60 + value.second) % (minutes * 60)
    return (value - timedelta(seconds=remainder)).replace(microsecond=0)


def write_plan_atomically(plan: Plan, path: Path) -> None:
    """Write via a temporary file so readers never see a partial JSON document."""
    from .atomic import atomic_write_text

    atomic_write_text(
        path, json.dumps(plan.to_dict(), indent=2, allow_nan=False) + "\n"
    )


def _watchdog_timestamp(config: VoltWiseConfig, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return (moment + timedelta(seconds=2 * config.interval_minutes * 60)).isoformat(timespec="seconds")


def _read_previous(config: VoltWiseConfig, client: HomeAssistantClient) -> dict[str, float]:
    previous: dict[str, float] = {}
    for entity in {
        config.actuation.battery_power_entity,
        config.actuation.target_soc_entity,
        config.actuation.ev_power_entity,
    }:
        try:
            value = float(client.get_state(entity)["state"])
        except (ValueError, KeyError, TypeError, HomeAssistantError):
            continue
        if math.isfinite(value):
            previous[entity] = value
    return previous


def _actuate(
    config: VoltWiseConfig, client: HomeAssistantClient, plan: Plan,
    *, apply_watchdog: bool = True,
) -> bool:
    """Apply a plan transactionally: one script call, or writes with rollback."""
    allowed = {
        config.actuation.battery_power_entity,
        config.actuation.target_soc_entity,
        config.actuation.ev_power_entity,
        "input_number.voltwise_total_cost",
        "input_number.voltwise_savings",
        "input_number.voltwise_status_code",
    }
    commands = plan_commands(config, plan)
    script = config.actuation.apply_script

    # Arm the watchdog BEFORE changing any setpoint: an unavailable watchdog
    # must never leave live actuators unmonitored.
    if apply_watchdog and not client.dry_run:
        try:
            armed = client.set_datetime(config.actuation.expiry_entity, _watchdog_timestamp(config))
        except HomeAssistantError as exc:
            armed = False
            LOGGER.error("watchdog arming failed: %s", exc)
        if not armed:
            LOGGER.error("watchdog could not be armed; refusing to actuate")
            return False

    def expire() -> None:
        if apply_watchdog and not client.dry_run:
            try:
                client.set_datetime(config.actuation.expiry_entity, "1970-01-01T00:00:00+00:00")
            except HomeAssistantError as exc:
                LOGGER.error("watchdog could not be expired: %s", exc)

    if script is not None:
        setpoints = [command for command in commands if command.slot == 0 and not command.description.startswith("telemetry:")]
        payload = {
            "battery_power_kw": setpoints[0].value,
            "target_soc_pct": setpoints[1].value,
            "ev_power_kw": next(
                (command.value for command in setpoints if "EV charge power" in command.description), 0.0
            ),
            "telemetry": {
                command.entity_id.split(".")[1]: command.value
                for command in commands if command.description.startswith("telemetry:")
            },
        }
        success = client.call_service("script", script.split(".", 1)[1], payload)
        if not success:
            LOGGER.error("atomic apply script failed")
            expire()
            return False
    elif config.actuation.adapter != "generic":
        from .adapters import create_adapter

        adapter = create_adapter(config.actuation.adapter)
        setpoint_commands = [
            command for command in commands if not command.description.startswith("telemetry:")
        ]
        for call in adapter.render(config, setpoint_commands):
            if not client.call_service(call.domain, call.service, call.data):
                LOGGER.error("adapter %s failed on %s", adapter.name, call.domain)
                expire()
                return False
        _write_telemetry(config, client, commands, allowed)
    else:
        previous = _read_previous(config, client)
        applied: list[str] = []
        success = True
        for command in commands:
            if command.description.startswith("telemetry:"):
                continue  # best-effort; never rolls back a successful setpoint
            try:
                result: WriteResult = client.set_value(command.entity_id, command.value, allowed=allowed)
            except HomeAssistantError as exc:
                LOGGER.error("actuation raised for %s: %s", command.entity_id, exc)
                success = False
                break
            if not result.success:
                LOGGER.error("actuation failed for %s: %s", command.entity_id, result.detail)
                success = False
                break
            applied.append(command.entity_id)
        if success:
            _write_telemetry(config, client, commands, allowed)
        if not success:
            for entity in reversed(applied):
                restored = previous.get(entity, _safe_restore_value(config, entity))
                try:
                    result = client.set_value(entity, restored, allowed=allowed)
                    if not result.success:
                        LOGGER.error("rollback failed for %s: %s", entity, result.detail)
                except HomeAssistantError as exc:
                    LOGGER.error("rollback raised for %s: %s", entity, exc)
            expire()
            return False

    return True


def _write_telemetry(
    config: VoltWiseConfig,
    client: HomeAssistantClient,
    commands: list,
    allowed: set[str],
) -> None:
    """Best-effort telemetry writes; missing helpers must not fail actuation."""
    for command in commands:
        if not command.description.startswith("telemetry:"):
            continue
        try:
            result = client.set_value(command.entity_id, command.value, allowed=allowed)
        except HomeAssistantError as exc:
            LOGGER.debug("telemetry write skipped for %s: %s", command.entity_id, exc)
            continue
        if not result.success:
            LOGGER.debug("telemetry write skipped for %s: %s", command.entity_id, result.detail)


def _safe_restore_value(config: VoltWiseConfig, entity: str) -> float:
    """Known-safe fallback when the previous entity state was unreadable."""
    if entity == config.actuation.target_soc_entity:
        return config.battery.initial_soc_pct
    return 0.0


def safe_stop(
    config: VoltWiseConfig,
    client: HomeAssistantClient | None,
    reason: str,
    *,
    allow_actuation: bool = False,
) -> bool:
    """Write zero-power setpoints when a cycle cannot produce a trustworthy plan."""
    if (
        client is None
        or not config.actuation.enabled
        or client.dry_run
        or not allow_actuation
    ):
        return True
    from .models import PlanSlot

    # plan_commands only needs slot 0; supply a zero-power placeholder.
    placeholder = PlanSlot(
        index=0, timestamp=datetime.now(UTC), price=0.0, feed_in_price=0.0,
        solar_kw=0.0, curtailed_kw=0.0, consumption_kw=0.0, ev_kw=0.0,
        battery_kw=0.0, grid_import_kw=0.0, grid_export_kw=0.0,
        battery_soc_pct=config.battery.initial_soc_pct,
        ev_soc_pct=None if config.ev is None else config.ev.initial_soc_pct,
        cost=0.0,
    )
    fallback = Plan(slots=(placeholder,), total_cost=0.0, baseline_cost=0.0,
                    degradation_cost=0.0, status="FALLBACK")
    LOGGER.error("safe stop: %s", reason)
    applied = _actuate(config, client, fallback, apply_watchdog=False)
    if applied and not client.dry_run:
        try:
            client.set_datetime(config.actuation.expiry_entity, "1970-01-01T00:00:00+00:00")
        except HomeAssistantError as exc:
            LOGGER.error("safe-stop watchdog expiry failed: %s", exc)
    return applied


def _closed_loop_warning(
    config: VoltWiseConfig, client: HomeAssistantClient | None, output: str | None
) -> str | None:
    """Compare last cycle's predicted grid import with the measured value."""
    entity = config.actuation.measured_import_entity
    if entity is None or client is None or not output:
        return None
    path = Path(output)
    if not path.is_file():
        return None
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        predicted = float(previous["slots"][0]["grid_import_kw"])
        measured = float(client.get_state(entity)["state"])
    except (OSError, ValueError, KeyError, TypeError, IndexError, HomeAssistantError):
        return None
    deviation = abs(measured - predicted)
    tolerance = 0.5 + 0.2 * config.site.max_grid_import_kw
    if deviation > tolerance:
        return (
            f"Closed-loop deviation: measured grid import {measured:.2f} kW vs "
            f"predicted {predicted:.2f} kW. Check sensors and actuator wiring."
        )
    return None


def run_once(args: argparse.Namespace) -> int:
    # Licence checks run before any other operation, including setup/export, so
    # a blocked installation cannot sidestep them with a different flag.
    config_source: dict = {}
    if Path(args.config).is_file():
        config_source = load_config(args.config)
    config = VoltWiseConfig.from_dict(config_source)

    from .licensing import check_licence, current_hwid

    key = os.environ.get(config.licensing.key_env)
    hwid = current_hwid(args.config)
    if config.licensing.require:
        allowed, reason = check_licence(
            key, config.licensing.feature, require=True, hwid=hwid
        )
        if not allowed:
            LOGGER.error("licence check failed: %s", reason)
            return 2
    premium = config.actuation.adapter != "generic" or config.forecast.uncertainty_pct > 0
    if premium:
        # Premium features are NEVER free, regardless of the require flag.
        pro_ok, pro_reason = check_licence(key, "pro", require=True, hwid=hwid)
        if not pro_ok:
            LOGGER.error("premium feature requires a pro licence: %s", pro_reason)
            return 2

    if getattr(args, "setup", False):
        from .ha_client import HomeAssistantClient as Client
        from .setup import interactive_setup

        token = os.environ.get("HA_TOKEN")
        setup_client = Client("http://homeassistant:8123", token=token, dry_run=True)
        interactive_setup(setup_client, args.config)
        LOGGER.info("starter configuration written to %s", args.config)
        return 0
    if args.watchdog_automation:
        from .watchdog import write_automation

        write_automation(args.watchdog_automation, config)
        LOGGER.info("watchdog automation written to %s", args.watchdog_automation)
        return 0

    act = config.actuation
    custom_watchdog = (
        act.adapter != "generic"
        or act.expiry_entity != "input_datetime.voltwise_plan_expiry"
        or act.battery_power_entity != "number.voltwise_battery_power"
        or act.target_soc_entity != "input_number.voltwise_target_soc"
        or act.ev_power_entity != "number.voltwise_ev_charge_power"
    )
    if custom_watchdog:
        LOGGER.warning(
            "Custom actuation entities or adapter detected: regenerate the watchdog "
            "automation with --watchdog-automation so the fail-safe covers them."
        )
    complexity = config.n_slots * config.soc_levels * (2 * config.action_steps + 3)
    if complexity > 1_000_000:
        LOGGER.warning(
            "large planning problem (%s state/action pairs); this cycle may take tens of seconds",
            complexity,
        )

    if args.start:
        try:
            start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigError(f"invalid --start: {exc}") from None
    else:
        start = round_start(datetime.now(UTC), config.interval_minutes)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)

    needs_client = (
        config.tariff.provider == "home_assistant"
        or config.forecast.provider == "home_assistant"
        or config.battery.soc_entity is not None
        or (config.actuation.enabled and not args.dry_run)
    )
    # A long-running service passes one shared client so its circuit breaker
    # survives across optimisation cycles.
    client: HomeAssistantClient | None = getattr(args, "ha_client", None)
    if client is None and needs_client:
        token = None
        if config.actuation.ha_token_env:
            token = os.environ.get(config.actuation.ha_token_env)
        client = HomeAssistantClient(
            config.actuation.ha_url,
            token=token,
            timeout_seconds=config.tariff.timeout_seconds,
            dry_run=args.dry_run,
            failure_cooldown_seconds=config.actuation.failure_cooldown_seconds,
        )

    if config.battery.soc_entity is not None:
        try:
            config, live_warnings = apply_live_soc(
                config, client, max_age_seconds=config.actuation.max_sensor_age_seconds
            )
            for warning in live_warnings:
                LOGGER.warning("%s", warning)
        except (ValueError, HomeAssistantError) as exc:
            LOGGER.error("live SOC read failed: %s", exc)
            safe_stop(config, client, "live SOC unavailable", allow_actuation=args.apply)
            return 4

    provider = create_provider(config.tariff, client)
    try:
        prices = provider.fetch(start, config.n_slots, config.interval_minutes)
    except PriceFetchError as exc:
        LOGGER.error("price fetch failed: %s", exc)
        safe_stop(config, client, "tariff unavailable", allow_actuation=args.apply)
        return 3

    try:
        forecasts = build_forecasts(config, prices, client)
    except (ValueError, HomeAssistantError) as exc:
        LOGGER.error("forecast assembly failed: %s", exc)
        safe_stop(config, client, "forecast unavailable", allow_actuation=args.apply)
        return 3

    input_audit: AuditReport = audit_forecasts(config, forecasts)
    if not input_audit.passed:
        for violation in input_audit.hard_violations:
            LOGGER.error("input validation: %s (slot %s)", violation.message, violation.slot)
        safe_stop(config, client, "invalid inputs", allow_actuation=args.apply)
        return 4

    if args.validate_only:
        LOGGER.info("configuration and inputs are valid")
        return 0

    closed_loop = _closed_loop_warning(config, client, args.output)
    if closed_loop:
        LOGGER.warning("%s", closed_loop)

    plan = optimize(config, forecasts)
    if closed_loop and plan.status != "FALLBACK":
        from dataclasses import replace as dataclass_replace

        plan = dataclass_replace(
            plan,
            warnings=tuple([*plan.warnings, closed_loop]),
            status="DEGRADED",
        )
    if args.output:
        write_plan_atomically(plan, Path(args.output))
    if args.diagnostics:
        from .diagnostics import write_bundle

        write_bundle(args.diagnostics, config, plan)

    LOGGER.info(
        "status=%s cost=%.4f baseline=%.4f savings=%.4f (%0.2f%%)",
        plan.status, plan.total_cost, plan.baseline_cost, plan.savings, plan.savings_pct,
    )
    for warning in plan.warnings:
        LOGGER.warning("%s", warning)

    if plan.status == "FALLBACK":
        LOGGER.error("fail-safe plan generated; no optimisation will be applied")
        safe_stop(config, client, "safety audit failed", allow_actuation=args.apply)
        return 5

    if config.actuation.enabled and not args.dry_run:
        if config.actuation.require_confirmation and not args.confirm:
            LOGGER.error("actuation requires --confirm (require_confirmation is enabled)")
            return 2
        if client is None:
            LOGGER.error("actuation requested without a Home Assistant client")
            return 6
        if not _actuate(config, client, plan):
            return 6
    else:
        LOGGER.info("dry-run: no Home Assistant writes performed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voltwise", description="Dynamic-tariff battery optimiser")
    parser.add_argument("--config", required=True, help="path to config.yaml or config.json")
    parser.add_argument("--start", help="ISO-8601 start of the planning horizon (UTC)")
    parser.add_argument("--dry-run", action="store_true", help="never write to Home Assistant")
    parser.add_argument("--apply", action="store_true", help="allow writes when enabled in config")
    parser.add_argument("--confirm", action="store_true",
                        help="confirm actuation when require_confirmation is enabled")
    parser.add_argument("--validate-only", action="store_true", help="validate config and inputs only")
    parser.add_argument("--setup", action="store_true", help="discover HA helpers and write a starter config")
    parser.add_argument("--diagnostics", help="write a sanitised support bundle to this path")
    parser.add_argument("--watchdog-automation",
                        help="write a HA watchdog automation matching this config")
    parser.add_argument("--output", help="write the resulting plan as JSON")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.apply and args.dry_run:
        LOGGER.error("--apply and --dry-run are mutually exclusive")
        return 2
    args.dry_run = not args.apply
    try:
        return run_once(args)
    except (ConfigError, ValueError, OverflowError, RecursionError, OSError,
            HomeAssistantError) as exc:
        LOGGER.error("configuration error: %s: %s", type(exc).__name__, exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
