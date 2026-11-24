"""Simulated battery, EV charger, grid meter and inverter.

The models enforce the same classes of limits a real BMS/inverter does:
hardware power caps, ramp limits, SOC cut-offs with tapering, and charge/
discharge efficiency. VoltWise writes setpoints; the simulation decides what
power was actually achieved and feeds that back as HA sensor states.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Battery:
    capacity_kwh: float = 10.0
    soc_pct: float = 50.0
    initial_soc_pct: float = 50.0
    soc_min_pct: float = 10.0
    soc_max_pct: float = 95.0
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    ramp_kw_per_hour: float = 100.0
    previous_kw: float = 0.0
    last_applied_kw: float = 0.0
    bms_cutoffs: int = 0

    def __post_init__(self) -> None:
        self.soc_pct = max(self.soc_min_pct, min(self.soc_max_pct, self.soc_pct))

    def apply(self, requested_kw: float, dt_hours: float) -> float:
        """Apply a setpoint like a real inverter: clamp, ramp, taper, integrate."""
        if not math.isfinite(requested_kw):
            requested_kw = 0.0
        if dt_hours <= 0:
            return self.last_applied_kw
        power = max(-self.max_discharge_kw, min(self.max_charge_kw, requested_kw))
        power = max(
            self.previous_kw - self.ramp_kw_per_hour * dt_hours,
            min(self.previous_kw + self.ramp_kw_per_hour * dt_hours, power),
        )
        energy = self.capacity_kwh * self.soc_pct / 100.0
        hard_min = self.capacity_kwh * self.soc_min_pct / 100.0
        hard_max = self.capacity_kwh * self.soc_max_pct / 100.0
        # Taper to available headroom instead of rejecting the whole command.
        if power > 0:
            headroom = hard_max - energy
            limit = headroom / (dt_hours * self.charge_efficiency)
            if limit <= 0:
                self.bms_cutoffs += 1
                power = 0.0
            else:
                power = min(power, limit)
                energy += power * dt_hours * self.charge_efficiency
        elif power < 0:
            headroom = energy - hard_min
            limit = headroom * self.discharge_efficiency / dt_hours
            if limit <= 0:
                self.bms_cutoffs += 1
                power = 0.0
            else:
                power = max(power, -limit)
                energy += power * dt_hours / self.discharge_efficiency
        if power != 0.0:
            self.soc_pct = 100.0 * energy / self.capacity_kwh
        self.previous_kw = power
        self.last_applied_kw = power
        return power


@dataclass
class EvCharger:
    capacity_kwh: float = 60.0
    soc_pct: float = 40.0
    max_charge_kw: float = 11.0
    efficiency: float = 0.95
    last_applied_kw: float = 0.0

    def __post_init__(self) -> None:
        self.soc_pct = max(0.0, min(100.0, self.soc_pct))

    def apply(self, requested_kw: float, dt_hours: float) -> float:
        if not math.isfinite(requested_kw) or requested_kw < 0 or dt_hours <= 0:
            requested_kw = 0.0
        power = min(self.max_charge_kw, requested_kw)
        energy = self.capacity_kwh * self.soc_pct / 100.0
        headroom = max(0.0, self.capacity_kwh - energy)
        power = min(power, headroom / (dt_hours * self.efficiency))
        energy += power * dt_hours * self.efficiency
        self.soc_pct = 100.0 * energy / self.capacity_kwh
        self.last_applied_kw = power
        return power


@dataclass
class Site:
    """Grid meter: combines load, solar and actual device power."""

    load_kw: float = 0.5
    solar_kw: float = 0.0
    measured_import_kw: float = 0.0
    measured_export_kw: float = 0.0
    feed_in_cost: float = 0.0
    energy_cost: float = 0.0

    def update(self, battery_kw: float, ev_kw: float, dt_hours: float, buy_price: float) -> None:
        grid = self.load_kw - self.solar_kw + battery_kw + ev_kw
        self.measured_import_kw = max(0.0, grid)
        self.measured_export_kw = max(0.0, -grid)
        self.energy_cost += (
            self.measured_import_kw * buy_price * dt_hours
            - self.measured_export_kw * self.feed_in_cost * dt_hours
        )


@dataclass
class SimulationState:
    battery: Battery = field(default_factory=Battery)
    ev: EvCharger = field(default_factory=EvCharger)
    site: Site = field(default_factory=Site)
    setpoints: dict[str, float] = field(default_factory=dict)
    target_soc_pct: float = 50.0
    plan_expiry: str = "1970-01-01T00:00:00+00:00"
    watchdog_trips: int = 0
    service_calls: list[tuple[str, str, dict]] = field(default_factory=list)
    failed_service_calls: list[tuple[str, str, dict]] = field(default_factory=list)
    mqtt_messages: list[dict] = field(default_factory=list)

    def apply_setpoint(self, entity_id: str, value: float, dt_hours: float) -> float:
        """Route a helper write to the simulated device."""
        self.setpoints[entity_id] = value
        if "battery_power" in entity_id:
            return self.battery.apply(value, dt_hours)
        if "target_soc" in entity_id:
            self.target_soc_pct = value
            return value
        if "ev_power" in entity_id or "ev_charge_power" in entity_id:
            return self.ev.apply(value, dt_hours)
        return value

    def force_safe(self) -> None:
        """Emulate the HA watchdog automation: write zero to the actual helpers."""
        for entity in (
            "number.voltwise_battery_power",
            "number.voltwise_ev_charge_power",
        ):
            self.setpoints[entity] = 0.0
        self.setpoints["input_number.voltwise_target_soc"] = self.battery.initial_soc_pct
        self.battery.previous_kw = 0.0
        self.battery.last_applied_kw = 0.0
        self.ev.last_applied_kw = 0.0
        self.watchdog_trips += 1
