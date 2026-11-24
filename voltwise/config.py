"""Strict, fail-closed configuration parsing and validation.

Every numeric input is bounded by a hard cap before it reaches the optimiser.
Unknown keys are rejected so that typos cannot silently change behaviour.
"""

from __future__ import annotations

import math
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import BatterySpec, EvSpec, SiteSpec

# Hard caps. Operator input is untrusted, even when it comes from an add-on UI.
MAX_SLOTS = 168
MAX_CAPACITY_KWH = 200.0
MAX_POWER_KW = 100.0
MAX_SOC_LEVELS = 401
MAX_ACTION_STEPS = 81
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 60
MAX_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 5_000_000

_ENTITY_RE = re.compile(r"^(input_number|number|switch|select)\.[a-z0-9_]+$")
_READ_ENTITY_RE = re.compile(r"^(sensor|input_number|number)\.[a-z0-9_]+$")
_SCRIPT_ENTITY_RE = re.compile(r"^script\.[a-z0-9_]+$")
_DATETIME_ENTITY_RE = re.compile(r"^input_datetime\.[a-z0-9_]+$")
_TOKEN_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_ENV_NAME_RE = re.compile(r"^VOLTWISE_[A-Z0-9_]{1,32}$")
_HA_HOSTS = frozenset({"homeassistant", "supervisor", "hassio", "localhost", "127.0.0.1", "::1"})
_PROVIDERS = frozenset({"manual", "nordpool", "tibber", "octopus", "home_assistant"})
_FORECAST_PROVIDERS = frozenset({"manual", "home_assistant"})
_INTERVALS = frozenset({15, 30, 60})


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed or out of bounds."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {type(value).__name__}")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ConfigError(f"{name} is too large to represent as a finite number") from None
    if not math.isfinite(result):
        raise ConfigError(f"{name} must be finite (no NaN/Infinity)")
    return result


def _bounded(value: Any, name: str, low: float, high: float) -> float:
    result = _finite(value, name)
    if not low <= result <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {result}")
    return result


def _int_bounded(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer, got {type(value).__name__}")
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _string(value: Any, name: str, *, max_len: int = 128, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    if len(value) > max_len:
        raise ConfigError(f"{name} exceeds {max_len} characters")
    if pattern is not None and not pattern.fullmatch(value):
        raise ConfigError(f"{name} has an unsafe format: {value!r}")
    return value


def _series(
    value: Any, name: str, expected: int | None = None,
    max_len: int = MAX_SLOTS, max_magnitude: float = 1e6,
) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{name} must be a list of numbers")
    if len(value) > max_len:
        raise ConfigError(f"{name} has more than {max_len} entries")
    if expected is not None and len(value) != expected:
        raise ConfigError(f"{name} must have exactly {expected} entries, got {len(value)}")
    result = tuple(_finite(v, f"{name}[{i}]") for i, v in enumerate(value))
    if any(abs(v) > max_magnitude for v in result):
        raise ConfigError(f"{name} contains values above {max_magnitude}")
    return result


def _check_keys(data: Any, allowed: frozenset[str], section: str) -> None:
    if not isinstance(data, Mapping):
        raise ConfigError(f"{section} must be a mapping, got {type(data).__name__}")
    unknown = set(data) - allowed
    if unknown:
        # repr() escapes control characters; stringify before sorting so mixed
        # key types cannot raise TypeError.
        labels = sorted(repr(key) for key in unknown)
        raise ConfigError(f"{section} has unknown key(s): {', '.join(labels)}")


@dataclass(frozen=True, slots=True)
class TariffConfig:
    """Where day-ahead prices come from."""

    provider: str
    area: str = "NL"
    currency: str = "EUR"
    api_key_env: str = "VOLTWISE_TARIFF_API_KEY"
    timeout_seconds: float = 10.0
    max_response_bytes: int = 1_000_000
    manual_prices: tuple[float, ...] = ()
    manual_feed_in_prices: tuple[float, ...] = ()
    buy_fee_per_kwh: float = 0.0
    sell_fee_per_kwh: float = 0.0
    octopus_product_code: str = "AGILE-FLEX-22-11-25"
    octopus_tariff_code: str = "E-1R-AGILE-FLEX-22-11-25-A"
    price_entity: str | None = None
    tomorrow_entity: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, n_slots: int) -> TariffConfig:
        allowed = frozenset(
            {
                "provider", "area", "currency", "api_key_env", "timeout_seconds",
                "max_response_bytes", "manual_prices", "manual_feed_in_prices",
                "buy_fee_per_kwh", "sell_fee_per_kwh",
                "octopus_product_code", "octopus_tariff_code",
                "price_entity", "tomorrow_entity",
            }
        )
        _check_keys(data, allowed, "tariff")
        provider = _string(data.get("provider"), "tariff.provider", max_len=32).lower()
        if provider not in _PROVIDERS:
            raise ConfigError(f"tariff.provider must be one of {sorted(_PROVIDERS)}")
        prices = _series(data.get("manual_prices", ()), "tariff.manual_prices")
        feed_in = _series(data.get("manual_feed_in_prices", ()), "tariff.manual_feed_in_prices")
        if feed_in and len(feed_in) != len(prices):
            raise ConfigError("tariff.manual_feed_in_prices must match tariff.manual_prices length")
        if provider == "manual" and len(prices) != n_slots:
            raise ConfigError(f"manual tariff requires exactly {n_slots} prices")
        price_entity = data.get("price_entity")
        tomorrow_entity = data.get("tomorrow_entity")
        if price_entity is not None:
            price_entity = _string(price_entity, "tariff.price_entity", pattern=_READ_ENTITY_RE)
        if tomorrow_entity is not None:
            tomorrow_entity = _string(tomorrow_entity, "tariff.tomorrow_entity", pattern=_READ_ENTITY_RE)
        if provider == "home_assistant" and price_entity is None:
            raise ConfigError("home_assistant tariff requires tariff.price_entity")
        api_key_env = _string(data.get("api_key_env", "VOLTWISE_TARIFF_API_KEY"),
                              "tariff.api_key_env", max_len=64, pattern=_ENV_NAME_RE)
        return cls(
            provider=provider,
            area=_string(data.get("area", "NL"), "tariff.area", max_len=16),
            currency=_string(data.get("currency", "EUR"), "tariff.currency",
                             pattern=re.compile(r"^[A-Z]{3}$")),
            api_key_env=api_key_env,
            timeout_seconds=_bounded(data.get("timeout_seconds", 10.0), "tariff.timeout_seconds", 1.0, MAX_TIMEOUT_SECONDS),
            max_response_bytes=_int_bounded(data.get("max_response_bytes", 1_000_000), "tariff.max_response_bytes", 1024, MAX_RESPONSE_BYTES),
            manual_prices=prices,
            manual_feed_in_prices=feed_in,
            buy_fee_per_kwh=_bounded(data.get("buy_fee_per_kwh", 0.0), "tariff.buy_fee_per_kwh", -10.0, 10.0),
            sell_fee_per_kwh=_bounded(data.get("sell_fee_per_kwh", 0.0), "tariff.sell_fee_per_kwh", -10.0, 10.0),
            octopus_product_code=_string(
                data.get("octopus_product_code", "AGILE-FLEX-22-11-25"),
                "tariff.octopus_product_code", pattern=re.compile(r"^[A-Z0-9-]{3,48}$"),
            ),
            octopus_tariff_code=_string(
                data.get("octopus_tariff_code", "E-1R-AGILE-FLEX-22-11-25-A"),
                "tariff.octopus_tariff_code", pattern=re.compile(r"^[A-Z0-9-]{3,48}$"),
            ),
            price_entity=price_entity,
            tomorrow_entity=tomorrow_entity,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["manual_prices"] = list(self.manual_prices)
        result["manual_feed_in_prices"] = list(self.manual_feed_in_prices)
        return result


@dataclass(frozen=True, slots=True)
class ForecastConfig:
    """Where solar/consumption forecasts come from."""

    provider: str
    solar_kw: tuple[float, ...] = ()
    consumption_kw: tuple[float, ...] = ()
    solar_entity: str | None = None
    consumption_entity: str | None = None
    uncertainty_pct: float = 0.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, n_slots: int) -> ForecastConfig:
        allowed = frozenset({
            "provider", "solar_kw", "consumption_kw", "solar_entity",
            "consumption_entity", "uncertainty_pct",
        })
        _check_keys(data, allowed, "forecast")
        provider = _string(data.get("provider"), "forecast.provider", max_len=32).lower()
        if provider not in _FORECAST_PROVIDERS:
            raise ConfigError(f"forecast.provider must be one of {sorted(_FORECAST_PROVIDERS)}")
        solar = _series(data.get("solar_kw", ()), "forecast.solar_kw")
        consumption = _series(data.get("consumption_kw", ()), "forecast.consumption_kw")
        solar_entity = data.get("solar_entity")
        consumption_entity = data.get("consumption_entity")
        if solar_entity is not None:
            solar_entity = _string(solar_entity, "forecast.solar_entity", pattern=_READ_ENTITY_RE)
        if consumption_entity is not None:
            consumption_entity = _string(consumption_entity, "forecast.consumption_entity", pattern=_READ_ENTITY_RE)
        if provider == "manual":
            if len(solar) != n_slots or len(consumption) != n_slots:
                raise ConfigError(f"manual forecast requires {n_slots} solar and consumption values")
        if provider == "home_assistant":
            if solar_entity is None or consumption_entity is None:
                raise ConfigError("home_assistant forecast requires solar_entity and consumption_entity")
        return cls(
            provider, solar, consumption, solar_entity, consumption_entity,
            _bounded(data.get("uncertainty_pct", 0.0), "forecast.uncertainty_pct", 0.0, 1.0),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["solar_kw"] = list(self.solar_kw)
        result["consumption_kw"] = list(self.consumption_kw)
        return result


@dataclass(frozen=True, slots=True)
class ActuationConfig:
    """How plans are written back to Home Assistant. Disabled by default."""

    enabled: bool = False
    ha_url: str = "http://homeassistant:8123"
    battery_power_entity: str = "number.voltwise_battery_power"
    target_soc_entity: str = "input_number.voltwise_target_soc"
    ev_power_entity: str = "number.voltwise_ev_charge_power"
    require_confirmation: bool = True
    failure_cooldown_seconds: int = 900
    expiry_entity: str = "input_datetime.voltwise_plan_expiry"
    apply_script: str | None = None
    ha_token_env: str | None = None
    max_sensor_age_seconds: int = 900
    measured_import_entity: str | None = None
    adapter: str = "generic"
    adapter_topic_prefix: str = "voltwise"
    telemetry_enabled: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActuationConfig:
        allowed = frozenset(
            {
                "enabled", "ha_url", "battery_power_entity", "target_soc_entity",
                "ev_power_entity", "require_confirmation", "failure_cooldown_seconds",
                "expiry_entity", "apply_script", "ha_token_env",
                "max_sensor_age_seconds", "measured_import_entity",
                "adapter", "adapter_topic_prefix", "telemetry_enabled",
            }
        )
        _check_keys(data, allowed, "actuation")
        enabled = data.get("enabled", False)
        confirmation = data.get("require_confirmation", True)
        if not isinstance(enabled, bool):
            raise ConfigError("actuation.enabled must be a boolean")
        if not isinstance(confirmation, bool):
            raise ConfigError("actuation.require_confirmation must be a boolean")
        ha_url = _string(data.get("ha_url", "http://homeassistant:8123"), "actuation.ha_url", max_len=256)
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in ha_url):
            raise ConfigError("actuation.ha_url contains control characters")
        parsed_url = urllib.parse.urlparse(ha_url)
        if parsed_url.scheme not in {"http", "https"}:
            raise ConfigError("actuation.ha_url must start with http:// or https://")
        if parsed_url.username or parsed_url.password or parsed_url.fragment:
            raise ConfigError("actuation.ha_url must not contain credentials or fragments")
        if (parsed_url.hostname or "").lower() not in _HA_HOSTS:
            # The add-on sends the Supervisor token with every request. Egress
            # is therefore restricted to Home Assistant/Supervisor hosts only.
            raise ConfigError("actuation.ha_url host is not an allowed Home Assistant host")
        apply_script = data.get("apply_script")
        if apply_script is not None:
            apply_script = _string(apply_script, "actuation.apply_script", pattern=_SCRIPT_ENTITY_RE)
        token_env = data.get("ha_token_env")
        if token_env is not None:
            token_env = _string(token_env, "actuation.ha_token_env", pattern=_TOKEN_ENV_RE)
        measured_import = data.get("measured_import_entity")
        if measured_import is not None:
            measured_import = _string(measured_import, "actuation.measured_import_entity", pattern=_READ_ENTITY_RE)
        telemetry = data.get("telemetry_enabled", False)
        if not isinstance(telemetry, bool):
            raise ConfigError("actuation.telemetry_enabled must be a boolean")
        adapter = _string(data.get("adapter", "generic"), "actuation.adapter", max_len=32).lower()
        if adapter not in {"generic", "mqtt", "modbus"}:
            raise ConfigError("actuation.adapter must be generic, mqtt or modbus")
        prefix = _string(data.get("adapter_topic_prefix", "voltwise"),
                         "actuation.adapter_topic_prefix", max_len=128)
        if not re.fullmatch(r"[A-Za-z0-9_/-]+", prefix) or not prefix.startswith("voltwise"):
            raise ConfigError("actuation.adapter_topic_prefix has an unsafe format")
        return cls(
            enabled=enabled,
            ha_url=ha_url,
            battery_power_entity=_string(data.get("battery_power_entity", "number.voltwise_battery_power"), "actuation.battery_power_entity", pattern=_ENTITY_RE),
            target_soc_entity=_string(data.get("target_soc_entity", "input_number.voltwise_target_soc"), "actuation.target_soc_entity", pattern=_ENTITY_RE),
            ev_power_entity=_string(data.get("ev_power_entity", "number.voltwise_ev_charge_power"), "actuation.ev_power_entity", pattern=_ENTITY_RE),
            require_confirmation=confirmation,
            failure_cooldown_seconds=_int_bounded(data.get("failure_cooldown_seconds", 900), "actuation.failure_cooldown_seconds", 60, 86_400),
            expiry_entity=_string(data.get("expiry_entity", "input_datetime.voltwise_plan_expiry"), "actuation.expiry_entity", pattern=_DATETIME_ENTITY_RE),
            apply_script=apply_script,
            ha_token_env=token_env,
            max_sensor_age_seconds=_int_bounded(data.get("max_sensor_age_seconds", 900), "actuation.max_sensor_age_seconds", 60, 86_400),
            measured_import_entity=measured_import,
            adapter=adapter,
            adapter_topic_prefix=prefix,
            telemetry_enabled=telemetry,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LicensingConfig:
    """Optional entitlement enforcement for commercial deployments."""

    require: bool = False
    key_env: str = "VOLTWISE_LICENCE_KEY"
    key: str | None = None
    feature: str = "core"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LicensingConfig:
        allowed = frozenset({"require", "key_env", "key", "feature"})
        _check_keys(data, allowed, "licensing")
        require = data.get("require", False)
        if not isinstance(require, bool):
            raise ConfigError("licensing.require must be a boolean")
        licence_key = data.get("key")
        if licence_key is not None and not isinstance(licence_key, str):
            raise ConfigError("licensing.key must be a string")
        return cls(
            require=require,
            key_env=_string(data.get("key_env", "VOLTWISE_LICENCE_KEY"), "licensing.key_env", pattern=_TOKEN_ENV_RE),
            key=licence_key,
            feature=_string(data.get("feature", "core"), "licensing.feature", max_len=32),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VoltWiseConfig:
    """Top-level configuration, fully validated at construction time."""

    n_slots: int
    interval_minutes: int
    battery: BatterySpec
    site: SiteSpec
    tariff: TariffConfig
    forecast: ForecastConfig
    actuation: ActuationConfig = field(default_factory=ActuationConfig)
    ev: EvSpec | None = None
    soc_levels: int = 201
    action_steps: int = 21
    solver_budget_pairs: int = 2_000_000
    max_ramp_kw_per_slot: float | None = None
    licensing: LicensingConfig = field(default_factory=LicensingConfig)
    name: str = "VoltWise"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VoltWiseConfig:
        if not isinstance(data, Mapping):
            raise ConfigError("configuration must be a mapping")
        allowed = frozenset(
            {
                "name", "n_slots", "interval_minutes", "battery", "site", "ev",
                "tariff", "forecast", "actuation", "soc_levels", "action_steps",
                "solver_budget_pairs", "max_ramp_kw_per_slot", "licensing",
            }
        )
        _check_keys(data, allowed, "root")

        n_slots = _int_bounded(data.get("n_slots", 48), "n_slots", 1, MAX_SLOTS)
        interval = _int_bounded(data.get("interval_minutes", 60), "interval_minutes", MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
        if interval not in _INTERVALS:
            raise ConfigError(f"interval_minutes must be one of {sorted(_INTERVALS)}")

        battery = _battery_from_dict(data.get("battery", {}), n_slots)
        site = _site_from_dict(data.get("site", {}))
        ev = _ev_from_dict(data["ev"], n_slots) if "ev" in data and data["ev"] is not None else None
        tariff = TariffConfig.from_dict(data.get("tariff", {}), n_slots=n_slots)
        forecast = ForecastConfig.from_dict(data.get("forecast", {}), n_slots=n_slots)
        actuation = ActuationConfig.from_dict(data.get("actuation", {}))

        soc_levels = _int_bounded(data.get("soc_levels", 201), "soc_levels", 11, MAX_SOC_LEVELS)
        action_steps = _int_bounded(data.get("action_steps", 21), "action_steps", 5, MAX_ACTION_STEPS)
        name = _string(data.get("name", "VoltWise"), "name", max_len=64)
        solver_budget_pairs = _int_bounded(
            data.get("solver_budget_pairs", 2_000_000), "solver_budget_pairs", 1_000, 50_000_000
        )
        ramp = data.get("max_ramp_kw_per_slot")
        if ramp is not None:
            ramp = _bounded(ramp, "max_ramp_kw_per_slot", 0.01, MAX_POWER_KW)

        return cls(
            n_slots=n_slots,
            interval_minutes=interval,
            battery=battery,
            site=site,
            tariff=tariff,
            forecast=forecast,
            actuation=actuation,
            ev=ev,
            soc_levels=soc_levels,
            action_steps=action_steps,
            solver_budget_pairs=solver_budget_pairs,
            max_ramp_kw_per_slot=ramp,
            licensing=LicensingConfig.from_dict(data.get("licensing", {})),
            name=name,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "n_slots": self.n_slots,
            "interval_minutes": self.interval_minutes,
            "battery": asdict(self.battery),
            "site": asdict(self.site),
            "ev": None if self.ev is None else asdict(self.ev),
            "tariff": self.tariff.to_dict(),
            "forecast": self.forecast.to_dict(),
            "actuation": self.actuation.to_dict(),
            "soc_levels": self.soc_levels,
            "action_steps": self.action_steps,
            "solver_budget_pairs": self.solver_budget_pairs,
            "max_ramp_kw_per_slot": self.max_ramp_kw_per_slot,
            "licensing": self.licensing.to_dict(),
        }


def _battery_from_dict(data: Any, n_slots: int) -> BatterySpec:
    if not isinstance(data, Mapping):
        raise ConfigError("battery must be a mapping")
    allowed = frozenset(
        {
            "capacity_kwh", "initial_soc_pct", "soc_min_pct", "soc_max_pct",
            "reserve_soc_pct", "max_charge_kw", "max_discharge_kw",
            "charge_efficiency", "discharge_efficiency", "degradation_cost_per_kwh",
            "energy_value_per_kwh", "soc_entity", "degradation_soc_penalty",
            "reserve_margin_pct", "reserve_margin_solar_threshold_kw",
        }
    )
    _check_keys(data, allowed, "battery")
    capacity = _bounded(data.get("capacity_kwh"), "battery.capacity_kwh", 0.1, MAX_CAPACITY_KWH)
    soc_min = _bounded(data.get("soc_min_pct", 10.0), "battery.soc_min_pct", 0.0, 100.0)
    soc_max = _bounded(data.get("soc_max_pct", 95.0), "battery.soc_max_pct", 0.0, 100.0)
    if soc_min >= soc_max:
        raise ConfigError("battery.soc_min_pct must be lower than soc_max_pct")
    reserve = _bounded(data.get("reserve_soc_pct", 20.0), "battery.reserve_soc_pct", soc_min, soc_max)
    initial = _bounded(data.get("initial_soc_pct"), "battery.initial_soc_pct", soc_min, soc_max)
    charge = _bounded(data.get("max_charge_kw", 5.0), "battery.max_charge_kw", 0.0, MAX_POWER_KW)
    discharge = _bounded(data.get("max_discharge_kw", 5.0), "battery.max_discharge_kw", 0.0, MAX_POWER_KW)
    if charge <= 0 or discharge <= 0:
        raise ConfigError("battery max charge/discharge power must be greater than zero")
    charge_eff = _bounded(data.get("charge_efficiency", 0.95), "battery.charge_efficiency", 0.5, 1.0)
    discharge_eff = _bounded(data.get("discharge_efficiency", 0.95), "battery.discharge_efficiency", 0.5, 1.0)
    degradation = _bounded(data.get("degradation_cost_per_kwh", 0.02), "battery.degradation_cost_per_kwh", 0.0, 10.0)
    energy_value = data.get("energy_value_per_kwh")
    if energy_value is not None:
        energy_value = _bounded(energy_value, "battery.energy_value_per_kwh", 0.0, 10.0)
    soc_entity = data.get("soc_entity")
    if soc_entity is not None:
        soc_entity = _string(soc_entity, "battery.soc_entity", pattern=_READ_ENTITY_RE)
    soc_penalty = _bounded(data.get("degradation_soc_penalty", 0.0), "battery.degradation_soc_penalty", 0.0, 2.0)
    reserve_margin = _bounded(data.get("reserve_margin_pct", 0.0), "battery.reserve_margin_pct", 0.0, 50.0)
    reserve_threshold = _bounded(
        data.get("reserve_margin_solar_threshold_kw", 100.0),
        "battery.reserve_margin_solar_threshold_kw", 0.0, 1000.0,
    )
    return BatterySpec(
        capacity_kwh=capacity, initial_soc_pct=initial, soc_min_pct=soc_min,
        soc_max_pct=soc_max, reserve_soc_pct=reserve, max_charge_kw=charge,
        max_discharge_kw=discharge, charge_efficiency=charge_eff,
        discharge_efficiency=discharge_eff, degradation_cost_per_kwh=degradation,
        energy_value_per_kwh=energy_value,
        soc_entity=soc_entity,
        degradation_soc_penalty=soc_penalty,
        reserve_margin_pct=reserve_margin,
        reserve_margin_solar_threshold_kw=reserve_threshold,
    )


def _site_from_dict(data: Any) -> SiteSpec:
    if not isinstance(data, Mapping):
        raise ConfigError("site must be a mapping")
    allowed = frozenset({"max_grid_import_kw", "max_grid_export_kw", "feed_in_factor"})
    _check_keys(data, allowed, "site")
    return SiteSpec(
        max_grid_import_kw=_bounded(data.get("max_grid_import_kw", 10.0), "site.max_grid_import_kw", 0.0, MAX_POWER_KW),
        max_grid_export_kw=_bounded(data.get("max_grid_export_kw", 10.0), "site.max_grid_export_kw", 0.0, MAX_POWER_KW),
        feed_in_factor=_bounded(data.get("feed_in_factor", 0.0), "site.feed_in_factor", 0.0, 2.0),
    )


def _ev_from_dict(data: Any, n_slots: int) -> EvSpec:
    if not isinstance(data, Mapping):
        raise ConfigError("ev must be a mapping")
    allowed = frozenset(
        {
            "capacity_kwh", "initial_soc_pct", "target_soc_pct", "deadline_slot",
            "max_charge_kw", "charge_efficiency",
        }
    )
    _check_keys(data, allowed, "ev")
    capacity = _bounded(data.get("capacity_kwh"), "ev.capacity_kwh", 0.1, MAX_CAPACITY_KWH)
    initial = _bounded(data.get("initial_soc_pct"), "ev.initial_soc_pct", 0.0, 100.0)
    target = _bounded(data.get("target_soc_pct"), "ev.target_soc_pct", 0.0, 100.0)
    if target < initial:
        raise ConfigError("ev.target_soc_pct must be greater than or equal to initial_soc_pct")
    deadline = _int_bounded(data.get("deadline_slot"), "ev.deadline_slot", 0, n_slots - 1)
    return EvSpec(
        capacity_kwh=capacity,
        initial_soc_pct=initial,
        target_soc_pct=target,
        deadline_slot=deadline,
        max_charge_kw=_bounded(data.get("max_charge_kw", 11.0), "ev.max_charge_kw", 0.0, MAX_POWER_KW),
        charge_efficiency=_bounded(data.get("charge_efficiency", 0.95), "ev.charge_efficiency", 0.5, 1.0),
    )
