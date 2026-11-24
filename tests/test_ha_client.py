"""Home Assistant client security and failure-mode tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from voltwise.ha_client import CircuitOpen, HomeAssistantClient, HomeAssistantError


class FakeResponse:
    def __init__(self, body=b'{}'):
        self.body = body
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def make_client(**overrides):
    return HomeAssistantClient("http://homeassistant:8123", token="supersecret-token-123", **overrides)


@pytest.mark.parametrize("entity", [
    "light.kitchen", "number.kitchen;rm -rf /", "number.UPPER", "number.a" * 3,
    "", "number.a.b", "switch.a\nb",
])
def test_entity_ids_are_strictly_validated(entity):
    with pytest.raises(HomeAssistantError, match="unsafe"):
        HomeAssistantClient.validate_entity_id(entity)


def test_allow_list_is_enforced():
    client = make_client(dry_run=True)
    with pytest.raises(HomeAssistantError, match="allow-list"):
        client.set_value("number.other", 1.0, allowed={"number.allowed"})


def test_plain_http_only_for_local_hosts():
    with pytest.raises(HomeAssistantError, match="local"):
        HomeAssistantClient("http://example.com:8123", token="x")
    HomeAssistantClient("http://homeassistant:8123", token="x")


def test_credentials_in_url_are_rejected():
    with pytest.raises(HomeAssistantError):
        HomeAssistantClient("http://user:pass@homeassistant:8123", token="x")


def test_dry_run_never_calls_network():
    client = make_client(dry_run=True)
    with patch("voltwise.ha_client.urllib.request.urlopen", side_effect=AssertionError("network call")):
        result = client.set_value("number.voltwise_battery_power", 1.5)
    assert result.attempted is False
    assert result.success is True
    assert result.detail == "dry-run"


def test_non_finite_value_is_rejected():
    client = make_client(dry_run=True)
    with pytest.raises(HomeAssistantError, match="finite"):
        client.set_value("number.voltwise_battery_power", float("nan"))


def test_token_is_redacted_from_errors():
    client = make_client()
    message = client.redact("failed with supersecret-token-123 in header")
    assert "supersecret-token-123" not in message
    assert "***REDACTED***" in message


def test_write_success_path():
    client = make_client(dry_run=False)
    with patch("voltwise.ha_client.urllib.request.urlopen", return_value=FakeResponse()):
        result = client.set_value("number.voltwise_battery_power", 1.5)
    assert result.success and result.attempted


def test_circuit_breaker_opens_after_repeated_failures():
    client = make_client(dry_run=False, failure_cooldown_seconds=3600)
    with patch("voltwise.ha_client.urllib.request.urlopen", side_effect=OSError("down")):
        for _ in range(3):
            with pytest.raises(HomeAssistantError):
                client.get_state("sensor.solar_forecast")
        with pytest.raises(CircuitOpen):
            client.get_state("sensor.solar_forecast")
    client.reset_breaker()
    with patch("voltwise.ha_client.urllib.request.urlopen", return_value=FakeResponse(b'{"state":"1"}')):
        assert client.get_state("sensor.solar_forecast")["state"] == "1"


def test_get_state_rejects_unsupported_entity():
    client = make_client()
    with pytest.raises(HomeAssistantError):
        client.get_state("light.kitchen")


def test_sensor_entities_are_readable():
    client = make_client()
    with patch("voltwise.ha_client.urllib.request.urlopen", return_value=FakeResponse(b'{"state":"2.5"}')):
        assert client.get_state("sensor.solar_forecast")["state"] == "2.5"
