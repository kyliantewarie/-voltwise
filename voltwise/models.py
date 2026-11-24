"""Immutable domain models for VoltWise.

All energy values are kWh, all powers are kW, all prices are currency/kWh,
all SOC values are percentages (0-100). A positive ``battery_kw`` means the
battery is charging (drawing power from the site), a negative value means it is
discharging (supplying the site).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True, slots=True)
class PricePoint:
    """A single day-ahead price interval."""

    timestamp: datetime
    price: float
    feed_in_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "price": round(self.price, 6),
            "feed_in_price": None if self.feed_in_price is None else round(self.feed_in_price, 6),
        }


@dataclass(frozen=True, slots=True)
class BatterySpec:
    """Physical and safety limits of the home battery."""

    capacity_kwh: float
    initial_soc_pct: float
    soc_min_pct: float = 10.0
    soc_max_pct: float = 95.0
    reserve_soc_pct: float = 20.0
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    degradation_cost_per_kwh: float = 0.02
    degradation_soc_penalty: float = 0.0
    reserve_margin_pct: float = 0.0
    reserve_margin_solar_threshold_kw: float = 100.0
    energy_value_per_kwh: float | None = None  # None => derived from mean spot price
    soc_entity: str | None = None  # optional live SOC sensor

    @property
    def min_energy_kwh(self) -> float:
        return self.capacity_kwh * self.soc_min_pct / 100.0

    @property
    def max_energy_kwh(self) -> float:
        return self.capacity_kwh * self.soc_max_pct / 100.0

    @property
    def reserve_energy_kwh(self) -> float:
        return self.capacity_kwh * self.reserve_soc_pct / 100.0

    @property
    def initial_energy_kwh(self) -> float:
        return self.capacity_kwh * self.initial_soc_pct / 100.0

    def energy_to_soc(self, energy_kwh: float) -> float:
        return 100.0 * energy_kwh / self.capacity_kwh


@dataclass(frozen=True, slots=True)
class SiteSpec:
    """Site-level grid connection limits and export economics."""

    max_grid_import_kw: float = 10.0
    max_grid_export_kw: float = 10.0
    feed_in_factor: float = 0.0  # export price = spot price * factor (0 = no revenue)


@dataclass(frozen=True, slots=True)
class EvSpec:
    """Optional electric-vehicle charging requirement."""

    capacity_kwh: float
    initial_soc_pct: float
    target_soc_pct: float
    deadline_slot: int  # inclusive index of the last slot in which charging may occur
    max_charge_kw: float = 11.0
    charge_efficiency: float = 0.95

    @property
    def required_grid_energy_kwh(self) -> float:
        delta = (self.target_soc_pct - self.initial_soc_pct) / 100.0 * self.capacity_kwh
        return max(0.0, delta / self.charge_efficiency)


@dataclass(frozen=True, slots=True)
class Forecasts:
    """Input series. Every series has exactly ``n_slots`` entries."""

    prices: tuple[PricePoint, ...]
    solar_kw: tuple[float, ...]
    consumption_kw: tuple[float, ...]
    feed_in_prices: tuple[float, ...] | None = None

    @property
    def n_slots(self) -> int:
        return len(self.prices)

    def feed_in_at(self, index: int) -> float:
        if self.feed_in_prices is not None:
            return self.feed_in_prices[index]
        return self.prices[index].price * 0.0  # always use explicit value when provided


@dataclass(frozen=True, slots=True)
class PlanSlot:
    """One scheduled interval of the resulting plan."""

    index: int
    timestamp: datetime
    price: float
    feed_in_price: float
    solar_kw: float
    curtailed_kw: float
    consumption_kw: float
    ev_kw: float
    battery_kw: float
    grid_import_kw: float
    grid_export_kw: float
    battery_soc_pct: float
    ev_soc_pct: float | None
    cost: float

    @property
    def grid_kw(self) -> float:
        return self.grid_import_kw - self.grid_export_kw

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "price": round(self.price, 6),
            "feed_in_price": round(self.feed_in_price, 6),
            "solar_kw": round(self.solar_kw, 4),
            "curtailed_kw": round(self.curtailed_kw, 4),
            "consumption_kw": round(self.consumption_kw, 4),
            "ev_kw": round(self.ev_kw, 4),
            "battery_kw": round(self.battery_kw, 4),
            "grid_import_kw": round(self.grid_import_kw, 4),
            "grid_export_kw": round(self.grid_export_kw, 4),
            "battery_soc_pct": round(self.battery_soc_pct, 4),
            "ev_soc_pct": None if self.ev_soc_pct is None else round(self.ev_soc_pct, 4),
            "cost": round(self.cost, 6),
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """A complete, validated optimisation result."""

    slots: tuple[PlanSlot, ...]
    total_cost: float
    baseline_cost: float
    degradation_cost: float
    status: str  # OPTIMAL | DEGRADED | FALLBACK
    warnings: tuple[str, ...] = ()
    baseline_method: str = "battery_idle_export_capped_economic_curtailment_v1"
    baseline_is_estimate: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def savings(self) -> float:
        return self.baseline_cost - self.total_cost

    @property
    def savings_pct(self) -> float:
        if self.baseline_cost <= 0:
            return 0.0
        return 100.0 * self.savings / self.baseline_cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "baseline_method": self.baseline_method,
            "baseline_is_estimate": self.baseline_is_estimate,
            "created_at": self.created_at.isoformat(),
            "warnings": list(self.warnings),
            "total_cost": round(self.total_cost, 6),
            "baseline_cost": round(self.baseline_cost, 6),
            "degradation_cost": round(self.degradation_cost, 6),
            "savings": round(self.savings, 6),
            "savings_pct": round(self.savings_pct, 3),
            "slots": [s.to_dict() for s in self.slots],
        }


@dataclass(frozen=True, slots=True)
class Violation:
    """A constraint violation found during an audit."""

    code: str
    severity: str  # HARD | SOFT
    slot: int | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Result of re-simulating and checking a plan."""

    passed: bool
    hard_violations: tuple[Violation, ...] = ()
    soft_violations: tuple[Violation, ...] = ()
    checks_run: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks_run": self.checks_run,
            "hard_violations": [v.to_dict() for v in self.hard_violations],
            "soft_violations": [v.to_dict() for v in self.soft_violations],
        }


@dataclass(frozen=True, slots=True)
class ActuationCommand:
    """A single, allow-listed write to Home Assistant."""

    entity_id: str
    value: float
    slot: int
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def slot_timestamps(start: datetime, n_slots: int, interval_minutes: int) -> tuple[datetime, ...]:
    """Return monotonically increasing timestamps for the planning horizon."""
    step = timedelta(minutes=interval_minutes)
    return tuple(start + step * i for i in range(n_slots))
