"""Optional live tariff smoke tests.

Set VOLTWISE_LIVE_TARIFF_TESTS=1 to run against real providers. Credentials are
read from the environment; they are never written to plans or logs.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from voltwise.config import TariffConfig
from voltwise.tariffs import OctopusProvider, TibberProvider, create_provider

pytestmark = pytest.mark.skipif(
    os.environ.get("VOLTWISE_LIVE_TARIFF_TESTS") != "1",
    reason="live tariff tests are opt-in",
)

START = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def test_nordpool_live():
    if not os.environ.get("VOLTWISE_LIVE_NORDPOOL_AREA"):
        pytest.skip("no area configured")
    config = TariffConfig(
        provider="nordpool", area=os.environ["VOLTWISE_LIVE_NORDPOOL_AREA"]
    )
    points = create_provider(config).fetch(START, 4, 60)
    assert len(points) == 4 and all(-100 < p.price < 1000 for p in points)


def test_tibber_live():
    if not os.environ.get("VOLTWISE_TARIFF_API_KEY"):
        pytest.skip("no Tibber token")
    config = TariffConfig(provider="tibber")
    points = TibberProvider(config).fetch(START, 4, 60)
    assert len(points) == 4


def test_octopus_live():
    if not os.environ.get("VOLTWISE_TARIFF_API_KEY"):
        pytest.skip("no Octopus token")
    config = TariffConfig(provider="octopus")
    points = OctopusProvider(config).fetch(START - timedelta(hours=2), 4, 60)
    assert len(points) == 4
