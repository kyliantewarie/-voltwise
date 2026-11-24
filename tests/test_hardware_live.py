"""Optional hardware-in-the-loop contract test.

Set VOLTWISE_HIL_HA_URL and HA_TOKEN to run against a real Home Assistant with
real inverter helper entities. This is the minimum commissioning check before
claiming support for an inverter brand.
"""

from __future__ import annotations

import os

import pytest

from voltwise.ha_client import HomeAssistantClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("VOLTWISE_HIL_HA_URL") or not os.environ.get("HA_TOKEN"),
    reason="hardware-in-the-loop test is opt-in",
)


def test_real_ha_helpers_are_writable():
    client = HomeAssistantClient(
        os.environ["VOLTWISE_HIL_HA_URL"],
        token=os.environ["HA_TOKEN"],
        dry_run=False,
    )
    for entity in ("number.voltwise_battery_power", "number.voltwise_ev_charge_power"):
        state = client.get_state(entity)
        assert float(state["state"]) >= -100
    result = client.set_value("number.voltwise_battery_power", 0.0)
    assert result.success
