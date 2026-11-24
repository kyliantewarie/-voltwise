"""Tariff provider tests, including mocked network hardening."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from voltwise.config import TariffConfig
from voltwise.tariffs import (
    ManualTariffProvider,
    NordPoolProvider,
    OctopusProvider,
    PriceFetchError,
    TibberProvider,
    create_provider,
)

START = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def config(provider="nordpool", **overrides):
    data = {"provider": provider}
    if provider == "manual":
        data["manual_prices"] = [0.1, 0.2]
    data.update(overrides)
    return TariffConfig.from_dict(data, n_slots=2)


def test_manual_provider_returns_requested_points():
    provider = create_provider(config("manual"))
    points = provider.fetch(START, 2, 60)
    assert [p.price for p in points] == [0.1, 0.2]
    assert points[1].timestamp - points[0].timestamp == __import__("datetime").timedelta(minutes=60)


def test_manual_provider_rejects_wrong_length():
    provider = ManualTariffProvider(TariffConfig(provider="manual", manual_prices=(0.1,)))
    with pytest.raises(PriceFetchError):
        provider.fetch(START, 2, 60)


@pytest.mark.parametrize("url", ["http://example.com/api", "ftp://example.com", "file:///etc/passwd"])
def test_non_https_urls_are_rejected(url):
    with pytest.raises(PriceFetchError, match="HTTPS"):
        NordPoolProvider(config())._get_json(url)


def test_oversized_response_is_rejected():
    response = FakeResponse(b"{}", {"Content-Length": "999999999"})
    with patch("voltwise.tariffs.urllib.request.urlopen", return_value=response):
        with pytest.raises(PriceFetchError, match="maximum allowed size"):
            NordPoolProvider(config())._get_json("https://data.nordpoolgroup.com/api")


def test_invalid_json_is_rejected():
    response = FakeResponse(b"<html>not json</html>")
    with patch("voltwise.tariffs.urllib.request.urlopen", return_value=response):
        with pytest.raises(PriceFetchError, match="invalid JSON"):
            NordPoolProvider(config())._get_json("https://data.nordpoolgroup.com/api")


def test_tibber_requires_environment_key(monkeypatch):
    monkeypatch.delenv("VOLTWISE_TARIFF_API_KEY", raising=False)
    with pytest.raises(PriceFetchError, match="missing"):
        TibberProvider(config("tibber")).fetch(START, 2, 60)


def test_octopus_requires_environment_key(monkeypatch):
    monkeypatch.delenv("VOLTWISE_TARIFF_API_KEY", raising=False)
    with pytest.raises(PriceFetchError, match="missing"):
        OctopusProvider(config("octopus")).fetch(START, 2, 60)


def test_home_assistant_tariff_provider_reads_ha_sensor():
    from voltwise.tariffs import HomeAssistantTariffProvider

    class Client:
        def get_state(self, entity):
            return {"attributes": {"data": [
                {"start": "2026-11-20T12:00:00Z", "price": 100.0},
                {"start": "2026-11-20T13:00:00Z", "price": 200.0},
                {"start": "2026-11-20T14:00:00Z", "price": 300.0},
            ]}}

    cfg = TariffConfig(provider="home_assistant", price_entity="sensor.nord_pool_nl_today")
    points = HomeAssistantTariffProvider(cfg, Client()).fetch(START, 2, 60)
    assert [p.price for p in points] == [0.1, 0.2]


def test_home_assistant_tariff_requires_entity():
    from voltwise.config import ConfigError

    with pytest.raises(ConfigError):
        TariffConfig.from_dict({"provider": "home_assistant"}, n_slots=2)


def test_nordpool_selects_requested_window_from_full_day():
    from datetime import timedelta

    entries = []
    base = datetime(2026, 11, 19, 23, 0, tzinfo=UTC)
    for i in range(96):
        when = base + timedelta(minutes=15 * i)
        entries.append({
            "deliveryStart": when.isoformat().replace("+00:00", "Z"),
            "entryPerArea": {"NL": 100.0 + i},
        })
    payload = {"multiAreaEntries": entries}
    with patch("voltwise.tariffs.urllib.request.urlopen", return_value=FakeResponse(
        __import__("json").dumps(payload).encode()
    )):
        points = NordPoolProvider(config()).fetch(START, 2, 60)
    assert [round(p.price, 6) for p in points] == [0.1535, 0.1575]


@pytest.mark.parametrize("attributes", ["hello", None, 42, []])
def test_ha_tariff_malformed_attributes_are_rejected(attributes):
    from voltwise.tariffs import HomeAssistantTariffProvider

    cfg = TariffConfig(provider="home_assistant", price_entity="sensor.price")

    class Client:
        def get_state(self, entity):
            return {"attributes": attributes}

    with pytest.raises(PriceFetchError):
        HomeAssistantTariffProvider(cfg, Client()).fetch(START, 2, 60)


def test_ha_tariff_huge_price_is_rejected():
    from voltwise.tariffs import HomeAssistantTariffProvider

    cfg = TariffConfig(provider="home_assistant", price_entity="sensor.price")

    class Client:
        def get_state(self, entity):
            return {"attributes": {"data": [
                {"start": "2026-11-20T12:00:00Z", "price": 10 ** 400},
            ]}}

    with pytest.raises(PriceFetchError):
        HomeAssistantTariffProvider(cfg, Client()).fetch(START, 2, 60)


def test_ha_tariff_alignment_rejects_old_rows():
    from voltwise.tariffs import HomeAssistantTariffProvider

    cfg = TariffConfig(provider="home_assistant", price_entity="sensor.price")

    class Client:
        def get_state(self, entity):
            return {"attributes": {"data": [
                {"start": "2026-11-20T12:00:00Z", "price": 100.0},
                {"start": "2019-01-01T00:00:00Z", "price": -999000.0},
            ]}}

    with pytest.raises(PriceFetchError):
        HomeAssistantTariffProvider(cfg, Client()).fetch(START, 2, 60)


def test_nordpool_area_is_validated():
    with pytest.raises(PriceFetchError, match="area"):
        NordPoolProvider(config(area="../../evil")).fetch(START, 2, 60)


def test_unknown_provider_factory_is_rejected():
    with pytest.raises(PriceFetchError, match="unknown"):
        create_provider(TariffConfig(provider="unknown"))


def test_nordpool_parses_real_api_payload():
    entry = lambda start, price: {  # noqa: E731
        "deliveryStart": start, "entryPerArea": {"NL": price}
    }
    payload = {"multiAreaEntries": [
        entry("2026-11-20T12:00:00Z", 50.0), entry("2026-11-20T12:15:00Z", 50.0),
        entry("2026-11-20T12:30:00Z", 50.0), entry("2026-11-20T12:45:00Z", 50.0),
        entry("2026-11-20T13:00:00Z", 100.0), entry("2026-11-20T13:15:00Z", 100.0),
        entry("2026-11-20T13:30:00Z", 100.0), entry("2026-11-20T13:45:00Z", 100.0),
    ]}
    with patch("voltwise.tariffs.urllib.request.urlopen", return_value=FakeResponse(
        __import__("json").dumps(payload).encode()
    )):
        points = NordPoolProvider(config()).fetch(START, 2, 60)
    assert [round(p.price, 6) for p in points] == [0.05, 0.1]
