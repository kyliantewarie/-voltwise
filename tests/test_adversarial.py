"""Regression tests for every confirmed adversarial finding."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_config_dict

from voltwise.cli import load_config, main
from voltwise.config import ConfigError, VoltWiseConfig
from voltwise.tariffs import NordPoolProvider, PriceFetchError, TariffConfig


def test_external_ha_url_is_rejected():
    data = make_config_dict()
    data["actuation"] = {"ha_url": "https://attacker.example"}
    with pytest.raises(ConfigError, match="not an allowed"):
        VoltWiseConfig.from_dict(data)


def test_crlf_in_ha_url_is_rejected():
    data = make_config_dict()
    data["actuation"] = {"ha_url": "http://homeassistant:8123\r\nX-Evil: 1"}
    with pytest.raises(ConfigError, match="control characters"):
        VoltWiseConfig.from_dict(data)


def test_local_ha_url_still_allowed():
    data = make_config_dict()
    data["actuation"] = {"ha_url": "http://homeassistant:8123"}
    assert VoltWiseConfig.from_dict(data).actuation.ha_url == "http://homeassistant:8123"


def test_huge_integer_becomes_config_error():
    data = make_config_dict()
    data["battery"]["capacity_kwh"] = 10 ** 400
    with pytest.raises(ConfigError, match="too large"):
        VoltWiseConfig.from_dict(data)


def test_unknown_key_log_injection_is_escaped():
    key = "EVIL\n2026-11-20 ERROR [FAKE] injected"
    data = make_config_dict()
    data[key] = 1
    with pytest.raises(ConfigError) as excinfo:
        VoltWiseConfig.from_dict(data)
    message = str(excinfo.value)
    assert "\nERROR" not in message
    assert "\\n" in message


@pytest.mark.parametrize("bad_section", [float("-inf"), True, 42, "", ["x"]])
def test_non_mapping_sections_raise_config_error(bad_section):
    data = make_config_dict()
    data["tariff"] = bad_section
    with pytest.raises(ConfigError):
        VoltWiseConfig.from_dict(data)


def test_mixed_type_unknown_keys_raise_config_error():
    data = make_config_dict()
    data[7] = 1
    with pytest.raises(ConfigError):
        VoltWiseConfig.from_dict(data)


def test_deeply_nested_ha_json_is_handled():
    from voltwise.ha_client import HomeAssistantClient, HomeAssistantError

    client = HomeAssistantClient("http://homeassistant:8123", token="x", dry_run=False)

    class Response:
        headers = {}
        body = (b"[" * 3000) + b"0" + (b"]" * 3000)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            return self.body if size < 0 else self.body[:size]

    with patch("voltwise.ha_client.urllib.request.urlopen", return_value=Response()):
        with pytest.raises(HomeAssistantError):
            client.get_state("sensor.test")


def test_malicious_yaml_tag_is_config_error(tmp_path: Path):
    path = tmp_path / "evil.yaml"
    path.write_text("!!python/object/apply:os.system ['echo hacked']\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_deeply_nested_yaml_is_config_error(tmp_path: Path):
    path = tmp_path / "deep.yaml"
    path.write_text("[" * 5000 + "]" * 5000 + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_deeply_nested_tariff_json_is_price_fetch_error():
    class Response:
        headers = {}
        body = (b"[" * 5000) + b"0" + (b"]" * 5000)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            return self.body if size < 0 else self.body[:size]

    with patch("voltwise.tariffs.urllib.request.urlopen", return_value=Response()):
        with pytest.raises(PriceFetchError, match="nested"):
            NordPoolProvider(TariffConfig(provider="nordpool"))._get_json(
                "https://data.nordpoolgroup.com/api"
            )


def test_far_future_start_is_price_fetch_error():
    from datetime import datetime

    provider = NordPoolProvider(TariffConfig(provider="nordpool"))
    raw = [(datetime(9999, 12, 31, 23, tzinfo=UTC), 0.1, None)] * 4
    with pytest.raises(PriceFetchError, match="datetime range"):
        provider._finish(raw, raw[0][0], 4, 60)


def test_require_confirmation_is_enforced(tmp_path: Path, monkeypatch):
    from voltwise.ha_client import WriteResult

    data = make_config_dict()
    data["actuation"] = {
        "enabled": True,
        "require_confirmation": True,
        "battery_power_entity": "number.voltwise_battery_power",
        "target_soc_entity": "input_number.voltwise_target_soc",
        "ev_power_entity": "number.voltwise_ev_charge_power",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    writes: list = []

    class Client:
        dry_run = False

        def set_value(self, entity_id, value, allowed=None):
            writes.append((entity_id, value))
            return WriteResult(entity_id, True, True)

        def get_state(self, entity_id):
            raise KeyError(entity_id)

        def set_datetime(self, entity_id, timestamp):
            writes.append((entity_id, timestamp))
            return True

        def call_service(self, domain, service, data):
            return True

    monkeypatch.setattr("voltwise.cli.HomeAssistantClient", lambda *a, **k: Client())
    assert main(["--config", str(path), "--apply"]) == 2
    assert not writes
    assert main(["--config", str(path), "--apply", "--confirm"]) == 0
    assert writes
