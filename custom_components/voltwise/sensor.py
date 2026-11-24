"""Read-only sensors mirroring VoltWise setpoints and watchdog state."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    BATTERY_POWER_ENTITY, COST_ENTITY, DOMAIN, EV_POWER_ENTITY, EXPIRY_ENTITY,
    SAVINGS_ENTITY, STATUS_ENTITY, TARGET_SOC_ENTITY,
)


class VoltWiseMirrorSensor(SensorEntity):
    """Mirror the state of one helper entity written by VoltWise."""

    _attr_should_poll = True

    def __init__(self, source: str, name: str, unit: str | None) -> None:
        self._source = source
        self._attr_name = f"VoltWise {name}"
        self._attr_unique_id = f"voltwise_{name.lower().replace(' ', '_')}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "voltwise")}, name="VoltWise"
        )

    def update(self) -> None:
        state = self.hass.states.get(self._source)
        self._attr_native_value = state.state if state else None
        self._attr_available = state is not None


def _mirror_sensors():
    return [
        VoltWiseMirrorSensor(BATTERY_POWER_ENTITY, "Battery power", "kW"),
        VoltWiseMirrorSensor(TARGET_SOC_ENTITY, "Target SOC", "%"),
        VoltWiseMirrorSensor(EV_POWER_ENTITY, "EV power", "kW"),
        VoltWiseMirrorSensor(EXPIRY_ENTITY, "Plan expiry", None),
        VoltWiseMirrorSensor(COST_ENTITY, "Total cost", None),
        VoltWiseMirrorSensor(SAVINGS_ENTITY, "Savings", None),
        VoltWiseMirrorSensor(STATUS_ENTITY, "Status code", None),
    ]


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Create the mirror sensors from YAML setup."""
    async_add_entities(_mirror_sensors())


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Create the mirror sensors from the config flow."""
    async_add_entities(_mirror_sensors())
