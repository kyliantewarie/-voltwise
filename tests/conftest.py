"""Shared test fixtures and builders."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from voltwise.config import VoltWiseConfig
from voltwise.forecasts import build_forecasts
from voltwise.models import Forecasts, PricePoint, slot_timestamps

START = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def make_config_dict(n_slots: int = 8, *, prices=None, solar=None, consumption=None, **overrides) -> dict:
    prices = list(prices) if prices is not None else [0.10] * n_slots
    solar = list(solar) if solar is not None else [0.0] * n_slots
    consumption = list(consumption) if consumption is not None else [0.5] * n_slots
    base = {
        "name": "test",
        "n_slots": n_slots,
        "interval_minutes": 60,
        "soc_levels": 101,
        "action_steps": 11,
        "battery": {
            "capacity_kwh": 10.0,
            "initial_soc_pct": 50.0,
            "soc_min_pct": 10.0,
            "soc_max_pct": 95.0,
            "reserve_soc_pct": 20.0,
            "max_charge_kw": 5.0,
            "max_discharge_kw": 5.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "degradation_cost_per_kwh": 0.02,
        },
        "site": {"max_grid_import_kw": 10.0, "max_grid_export_kw": 10.0, "feed_in_factor": 0.0},
        "tariff": {"provider": "manual", "manual_prices": prices},
        "forecast": {"provider": "manual", "solar_kw": solar, "consumption_kw": consumption},
    }

    def merge(target, source):
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = value

    merge(base, overrides)
    return base


def make_config(**kwargs) -> VoltWiseConfig:
    return VoltWiseConfig.from_dict(make_config_dict(**kwargs))


def make_forecasts(config: VoltWiseConfig, *, start=START, feed_in_prices=None) -> Forecasts:
    timestamps = slot_timestamps(start, config.n_slots, config.interval_minutes)
    points = [
        PricePoint(timestamp=timestamps[i], price=config.tariff.manual_prices[i])
        for i in range(config.n_slots)
    ]
    return build_forecasts(config, points, feed_in_prices=feed_in_prices)


@pytest.fixture
def start() -> datetime:
    return START


@pytest.fixture(autouse=True)
def valid_licence(request, monkeypatch):
    """Give every test a valid pro key except tests marked ``licence_real_key``."""
    if request.node.get_closest_marker("licence_real_key"):
        return
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:  # cryptography is optional for the licence-free core
        return
    import time

    import voltwise.licensing as licensing

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", licensing._b64url(public))
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key = licensing.encode_licence({
        "sub": "test", "features": ["core", "pro"],
        "exp": time.time() + 3600,
    }, pem)
    monkeypatch.setenv("VOLTWISE_LICENCE_KEY", key)
