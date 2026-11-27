"""Behavioural tests for the HACS integration without a Home Assistant install.

Home Assistant is deliberately not a test dependency, so the integration modules
are loaded against a small in-memory stand-in for the APIs they use. This keeps
the tests offline while exercising the real config flow, sensor and setup code.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from voltwise import __version__

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def hacs(monkeypatch):
    """Load the real integration with minimal Home Assistant stand-ins."""
    monkeypatch.syspath_prepend(str(ROOT))
    configured: set[str] = set()

    class AbortFlow(Exception):
        """Stand-in for Home Assistant's config-entry abort signal."""

    class FakeConfigFlow:
        def __init_subclass__(cls, **kwargs):
            # Home Assistant's ConfigFlow consumes registration keywords such as domain.
            super().__init_subclass__()

        def __init__(self):
            self.unique_ids: list[str] = []
            self.entries: list[tuple[str, dict]] = []

        async def async_set_unique_id(self, unique_id):
            self.unique_ids.append(unique_id)
            return self

        def _abort_if_unique_id_configured(self):
            if self.unique_ids and self.unique_ids[-1] in configured:
                raise AbortFlow("already_configured")

        def async_create_entry(self, *, title, data):
            self.entries.append((title, data))
            if self.unique_ids:
                configured.add(self.unique_ids[-1])
            return {"type": "create_entry", "title": title, "data": data}

        @staticmethod
        def async_show_form(*, step_id):
            return {"type": "form", "step_id": step_id}

    class FakeSensorEntity:
        @property
        def unique_id(self):
            return getattr(self, "_attr_unique_id", None)

        @property
        def name(self):
            return getattr(self, "_attr_name", None)

        @property
        def native_unit_of_measurement(self):
            return getattr(self, "_attr_native_unit_of_measurement", None)

        @property
        def native_value(self):
            return getattr(self, "_attr_native_value", None)

        @property
        def available(self):
            return getattr(self, "_attr_available", True)

        @property
        def device_info(self):
            return getattr(self, "_attr_device_info", None)

    class DeviceInfo(dict):
        def __init__(self, **kwargs):
            super().__init__(kwargs)

    def stub(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    for parent in ("homeassistant", "homeassistant.components", "homeassistant.helpers"):
        stub(parent).__path__ = []

    async def async_load_platform(*args, **kwargs):
        return None

    stub("homeassistant.config_entries", ConfigFlow=FakeConfigFlow, ConfigEntry=object)
    stub("homeassistant.data_entry_flow", FlowResult=dict)
    stub("homeassistant.core", HomeAssistant=object)
    stub("homeassistant.components.sensor", SensorEntity=FakeSensorEntity)
    stub("homeassistant.helpers.entity", DeviceInfo=DeviceInfo)
    stub("homeassistant.helpers.typing", ConfigType=dict, DiscoveryInfoType=dict)
    stub("homeassistant.helpers.discovery", async_load_platform=async_load_platform)

    for name in [name for name in sys.modules if name.startswith("custom_components")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    return SimpleNamespace(
        const=importlib.import_module("custom_components.voltwise.const"),
        config_flow=importlib.import_module("custom_components.voltwise.config_flow"),
        sensor=importlib.import_module("custom_components.voltwise.sensor"),
        integration=importlib.import_module("custom_components.voltwise"),
        AbortFlow=AbortFlow,
    )


def test_manifest_and_translation_schema(hacs):
    root = Path(hacs.config_flow.__file__).parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == hacs.const.DOMAIN == "voltwise"
    assert manifest["config_flow"] is True
    assert manifest["version"] == __version__
    assert manifest["requirements"] == []
    assert hacs.const.PLATFORMS == ["sensor"]

    for name in (
        "BATTERY_POWER_ENTITY", "TARGET_SOC_ENTITY", "EV_POWER_ENTITY",
        "EXPIRY_ENTITY", "COST_ENTITY", "SAVINGS_ENTITY", "STATUS_ENTITY",
    ):
        entity_id = getattr(hacs.const, name)
        assert entity_id.count(".") == 1, (name, entity_id)

    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    assert set(strings) == {"config"}
    assert strings["config"]["step"]["user"]["title"] == "VoltWise"
    assert strings["config"]["abort"]["already_configured"]
    english = json.loads((root / "translations" / "en.json").read_text(encoding="utf-8"))
    assert english == strings


def test_config_flow_generates_stable_unique_id(hacs):
    flow = hacs.config_flow.VoltWiseConfigFlow()
    assert asyncio.run(flow.async_step_user()) == {"type": "form", "step_id": "user"}
    result = asyncio.run(flow.async_step_user({}))
    assert result == {"type": "create_entry", "title": "VoltWise", "data": {}}
    assert flow.unique_ids == [hacs.const.DOMAIN]

    duplicate = hacs.config_flow.VoltWiseConfigFlow()
    with pytest.raises(hacs.AbortFlow):
        asyncio.run(duplicate.async_step_user({}))


def test_mirror_sensors_unique_ids_and_state_conversion(hacs):
    sensors = hacs.sensor._mirror_sensors()
    assert [sensor.unique_id for sensor in sensors] == [
        "voltwise_battery_power",
        "voltwise_target_soc",
        "voltwise_ev_power",
        "voltwise_plan_expiry",
        "voltwise_total_cost",
        "voltwise_savings",
        "voltwise_status_code",
    ]
    assert [sensor._source for sensor in sensors] == [
        hacs.const.BATTERY_POWER_ENTITY,
        hacs.const.TARGET_SOC_ENTITY,
        hacs.const.EV_POWER_ENTITY,
        hacs.const.EXPIRY_ENTITY,
        hacs.const.COST_ENTITY,
        hacs.const.SAVINGS_ENTITY,
        hacs.const.STATUS_ENTITY,
    ]
    battery = sensors[0]
    assert battery.name == "VoltWise Battery power"
    assert battery.native_unit_of_measurement == "kW"
    assert (hacs.const.DOMAIN, "voltwise") in battery.device_info["identifiers"]

    class State:
        def __init__(self, value):
            self.state = value

    battery.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda entity_id: State("2.75"))
    )
    battery.update()
    # Home Assistant states are strings and are mirrored verbatim, not coerced.
    assert battery.native_value == "2.75"
    assert battery.available is True

    battery.hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: None))
    battery.update()
    assert battery.native_value is None
    assert battery.available is False


def test_platform_and_entry_setup(hacs):
    added = []
    asyncio.run(hacs.sensor.async_setup_entry(None, object(), added.extend))
    assert len(added) == 7
    assert all(sensor.unique_id.startswith("voltwise_") for sensor in added)

    class ConfigEntries:
        def __init__(self):
            self.forwarded = []
            self.unloaded = []

        async def async_forward_entry_setups(self, hass, platforms):
            self.forwarded.append(list(platforms))

        async def async_unload_platforms(self, hass, platforms):
            self.unloaded.append(list(platforms))
            return True

    hass = SimpleNamespace(data={}, config_entries=ConfigEntries())
    entry = SimpleNamespace(entry_id="entry-1")
    assert asyncio.run(hacs.integration.async_setup(hass, {})) is True
    assert asyncio.run(hacs.integration.async_setup_entry(hass, entry)) is True
    assert hass.data["voltwise"]["entry-1"] == {}
    assert hass.config_entries.forwarded == [["sensor"]]
    assert asyncio.run(hacs.integration.async_unload_entry(hass, entry)) is True
    assert "entry-1" not in hass.data["voltwise"]
