"""Minimal config flow for the VoltWise mirror-sensor integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class VoltWiseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Let the user add VoltWise from the UI instead of editing YAML."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="VoltWise", data={})
        return self.async_show_form(step_id="user")
