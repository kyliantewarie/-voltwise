"""Inverter / charger adapter layer.

VoltWise writes abstract setpoints (battery power, target SOC, EV power).
Adapters translate those setpoints into Home Assistant service calls for
different hardware integration styles.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .config import VoltWiseConfig
from .models import ActuationCommand


@dataclass(frozen=True, slots=True)
class ServiceCall:
    domain: str
    service: str
    data: dict[str, Any]


class InverterAdapter(ABC):
    """Translate setpoints into HA service calls."""

    name: str = "base"

    @abstractmethod
    def render(self, config: VoltWiseConfig, commands: list[ActuationCommand]) -> list[ServiceCall]:
        """Return the service calls that apply the given setpoints."""


class GenericNumberAdapter(InverterAdapter):
    """Writes to number/input_number helpers (default)."""

    name = "generic"

    def render(self, config: VoltWiseConfig, commands: list[ActuationCommand]) -> list[ServiceCall]:
        calls: list[ServiceCall] = []
        for command in commands:
            domain = command.entity_id.split(".", 1)[0]
            calls.append(ServiceCall(domain, "set_value", {
                "entity_id": command.entity_id, "value": command.value,
            }))
        return calls


class MqttAdapter(InverterAdapter):
    """Publishes setpoints to MQTT topics under a configurable prefix."""

    name = "mqtt"

    def render(self, config: VoltWiseConfig, commands: list[ActuationCommand]) -> list[ServiceCall]:
        prefix = config.actuation.adapter_topic_prefix.rstrip("/")
        return [
            ServiceCall("mqtt", "publish", {
                "topic": f"{prefix}/{_short_name(command.entity_id)}",
                "payload": str(command.value),
                "qos": 1,
                "retain": True,
            })
            for command in commands
        ]


class ModbusAdapter(InverterAdapter):
    """Writes setpoints through the Home Assistant Modbus integration."""

    name = "modbus"

    def render(self, config: VoltWiseConfig, commands: list[ActuationCommand]) -> list[ServiceCall]:
        prefix = config.actuation.adapter_topic_prefix.rstrip("/")
        return [
            ServiceCall("modbus", "write_register", {
                "entity_id": f"{prefix}.{_short_name(command.entity_id)}",
                "value": command.value,
            })
            for command in commands
        ]


def _short_name(entity_id: str) -> str:
    return entity_id.split(".", 1)[1].replace("voltwise_", "")


def create_adapter(name: str) -> InverterAdapter:
    adapters: dict[str, type[InverterAdapter]] = {
        "generic": GenericNumberAdapter,
        "mqtt": MqttAdapter,
        "modbus": ModbusAdapter,
    }
    try:
        return adapters[name]()
    except KeyError:
        raise ValueError(f"unknown adapter {name!r}") from None
