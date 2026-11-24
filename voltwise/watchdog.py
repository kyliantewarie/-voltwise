"""Generate the Home Assistant watchdog automation for a concrete configuration.

The shipped `addon/watchdog_automation.yaml` covers the default helpers. Any
installation that renames entities or uses an adapter must generate a matching
automation with `--watchdog-automation <path>`; otherwise the fail-safe would
not cover the real actuators.
"""

from __future__ import annotations

from pathlib import Path

from .config import VoltWiseConfig


def generate_automation(config: VoltWiseConfig) -> str:
    """Return Home Assistant automation YAML matching this configuration."""
    act = config.actuation
    expiry = act.expiry_entity
    lines = [
        "- alias: VoltWise watchdog - force safe setpoints",
        "  id: voltwise_watchdog_force_safe",
        "  mode: single",
        "  trigger:",
        "    - platform: state",
        f"      entity_id: {expiry}",
        '    - platform: time_pattern',
        '      minutes: "/5"',
        "  condition:",
        "    - condition: template",
        f"      value_template: \"{{{{ as_timestamp(states('{expiry}'), 0) < as_timestamp(now()) }}}}\"",
        "  action:",
    ]

    if act.adapter == "generic":
        lines += [
            "    - service: number.set_value",
            "      target:",
            f"        entity_id: {act.battery_power_entity}",
            "      data:",
            "        value: 0",
            "    - service: input_number.set_value",
            "      target:",
            f"        entity_id: {act.target_soc_entity}",
            "      data:",
            f"        value: {config.battery.initial_soc_pct}",
            "    - service: number.set_value",
            "      target:",
            f"        entity_id: {act.ev_power_entity}",
            "      data:",
            "        value: 0",
        ]
    else:
        domain = "mqtt.publish" if act.adapter == "mqtt" else "modbus.write_register"
        prefix = act.adapter_topic_prefix.rstrip("/")
        setpoints = (
            (act.battery_power_entity, 0.0),
            (act.ev_power_entity, 0.0),
            (act.target_soc_entity, config.battery.initial_soc_pct),
        )
        for entity, value in setpoints:
            name = entity.split(".", 1)[1].replace("voltwise_", "")
            target = f"{prefix}/{name}" if act.adapter == "mqtt" else f"{prefix}.{name}"
            lines += [
                f"    - service: {domain}",
                "      data:",
                (f"        topic: {target}\n        payload: '{value}'\n        retain: true"
                 if act.adapter == "mqtt"
                 else f"        entity_id: {target}\n        value: {value}"),
            ]

    lines += [
        "    - service: persistent_notification.create",
        "      data:",
        "        title: VoltWise watchdog",
        "        message: VoltWise plan expired; safe setpoints were forced.",
        "        notification_id: voltwise_watchdog",
        "",
    ]
    return "\n".join(lines)


def write_automation(path: str | Path, config: VoltWiseConfig) -> Path:
    from .atomic import atomic_write_text

    target = Path(path)
    if ".." in target.parts:
        raise ValueError("watchdog path must not contain parent traversal")
    atomic_write_text(target, generate_automation(config))
    return target
