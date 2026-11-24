"""Day-ahead tariff providers.

Network access is deliberately locked down: endpoints are hard-coded, HTTPS is
mandatory, API keys are read from the environment (never logged or stored in a
plan), responses are size-limited and every request has a timeout.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta

from .config import TariffConfig
from .models import PricePoint

_AREA_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
# Nord Pool's CDN (Cloudflare) rejects the default urllib user agent (HTTP 1010).
USER_AGENT = "VoltWise/0.1.0 (+https://github.com/kyliantewarie/-voltwise)"


class PriceFetchError(RuntimeError):
    """Raised when a tariff source cannot produce trustworthy data."""


class TariffProvider(ABC):
    """Common interface for all price sources."""

    def __init__(self, config: TariffConfig) -> None:
        self.config = config

    @abstractmethod
    def fetch(self, start: datetime, n_slots: int, interval_minutes: int) -> list[PricePoint]:
        """Return exactly n_slots price points starting at ``start``."""

    def _finish(self, raw: list[tuple[datetime, float, float | None]],
                start: datetime, n_slots: int, interval_minutes: int) -> list[PricePoint]:
        if len(raw) < n_slots:
            raise PriceFetchError(f"provider returned {len(raw)} prices, need {n_slots}")
        ordered = sorted(raw, key=lambda item: item[0])
        self._check_alignment(ordered, start, n_slots, interval_minutes)
        points: list[PricePoint] = []
        step = timedelta(minutes=interval_minutes)
        for i in range(n_slots):
            timestamp, price, feed_in = ordered[i]
            if not math.isfinite(price):
                raise PriceFetchError(f"non-finite price at index {i}")
            if feed_in is not None and not math.isfinite(feed_in):
                raise PriceFetchError(f"non-finite feed-in price at index {i}")
            try:
                expected = start + step * i
            except OverflowError:
                raise PriceFetchError("planning horizon is outside the datetime range") from None
            # Providers are aligned to their own clock; keep the requested grid.
            points.append(PricePoint(timestamp=expected, price=price, feed_in_price=feed_in))
        return points

    @staticmethod
    def _check_alignment(raw: list[tuple[datetime, float, float | None]],
                         start: datetime, n_slots: int, interval_minutes: int) -> None:
        """Refuse prices that are not for the requested planning window."""
        expected = start.replace(tzinfo=UTC) if start.tzinfo is None else start
        step = timedelta(minutes=interval_minutes)
        seen: set[datetime] = set()
        for i in range(min(n_slots, len(raw))):
            timestamp = raw[i][0]
            actual = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
            try:
                expected_slot = expected + step * i
            except OverflowError:
                raise PriceFetchError("planning horizon is outside the datetime range") from None
            if actual in seen:
                raise PriceFetchError("provider returned duplicate price timestamps")
            seen.add(actual)
            if abs((actual - expected_slot).total_seconds()) > interval_minutes * 60:
                raise PriceFetchError("provider prices do not align with the requested slots")

    def _get_json(self, url: str, *, headers: dict[str, str] | None = None,
                  data: bytes | None = None, method: str = "GET") -> object:
        if not url.startswith("https://"):
            raise PriceFetchError("only HTTPS tariff endpoints are allowed")
        request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        # urllib verifies TLS certificates by default; no unverified context is
        # ever constructed. Enforce a timeout and an upper bound on the body.
        try:
            # HTTPS is enforced by the caller guard.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=self.config.timeout_seconds
            ) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None and declared.isdigit() and int(declared) > self.config.max_response_bytes:
                    raise PriceFetchError("tariff response exceeds maximum allowed size")
                body = response.read(self.config.max_response_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PriceFetchError(f"tariff endpoint unreachable: {type(exc).__name__}") from None
        if len(body) > self.config.max_response_bytes:
            raise PriceFetchError("tariff response exceeds maximum allowed size")
        try:
            decoded = body.decode("utf-8")
            from .json_safe import JsonTooDeep, ensure_json_depth

            ensure_json_depth(decoded)
            return json.loads(decoded)
        except JsonTooDeep:
            raise PriceFetchError("tariff endpoint returned excessively nested JSON") from None
        except (ValueError, UnicodeDecodeError):
            raise PriceFetchError("tariff endpoint returned invalid JSON") from None
        except RecursionError:
            raise PriceFetchError("tariff endpoint returned excessively nested JSON") from None

    def _api_key(self) -> str | None:
        return os.environ.get(self.config.api_key_env)


class ManualTariffProvider(TariffProvider):
    """Prices supplied directly in configuration (also used by all tests)."""

    def fetch(self, start: datetime, n_slots: int, interval_minutes: int) -> list[PricePoint]:
        prices = self.config.manual_prices
        feed_in = self.config.manual_feed_in_prices
        if len(prices) != n_slots:
            raise PriceFetchError(f"manual tariff needs {n_slots} prices, got {len(prices)}")
        step = timedelta(minutes=interval_minutes)
        values = [
            (start + step * i, p, feed_in[i] if feed_in else None)
            for i, p in enumerate(prices)
        ]
        return self._finish(values, start, n_slots, interval_minutes)


class NordPoolProvider(TariffProvider):
    """Nord Pool day-ahead area prices (EUR/MWh converted to EUR/kWh)."""

    BASE = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"

    def fetch(self, start: datetime, n_slots: int, interval_minutes: int) -> list[PricePoint]:
        area = self.config.area.upper()
        if not _AREA_RE.fullmatch(area):
            raise PriceFetchError("invalid Nord Pool area code")
        currency = self.config.currency
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise PriceFetchError("invalid tariff currency code")
        # Public data portal API: no key required; 15-minute entries in EUR/MWh.
        # Fetch the requested day and the previous day for timezone edge cases.
        raw: list[tuple[datetime, float, float | None]] = []
        for offset in (-1, 0, 1):
            day = (start + timedelta(days=offset)).date()
            url = (
                f"{self.BASE}?date={day.isoformat()}&market=DayAhead"
                f"&deliveryArea={area}&currency={currency}"
            )
            payload = self._get_json(url)
            if not isinstance(payload, dict):
                raise PriceFetchError("unexpected Nord Pool response shape")
            entries = payload.get("multiAreaEntries", [])
            if not isinstance(entries, list):
                raise PriceFetchError("unexpected Nord Pool response shape")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                areas = entry.get("entryPerArea", {})
                price = areas.get(area) if isinstance(areas, dict) else None
                stamp = entry.get("deliveryStart")
                if isinstance(price, bool) or not isinstance(price, (int, float)):
                    continue
                if not isinstance(stamp, str):
                    continue
                try:
                    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    value = float(price) / 1000.0
                except ValueError:
                    continue
                except OverflowError:
                    raise PriceFetchError("Nord Pool returned an oversized price") from None
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                raw.append((when, value, None))
        if not raw:
            raise PriceFetchError("Nord Pool returned no prices for this area")
        # Data is 15-minute; aggregate to the requested slot length.
        aggregated: list[tuple[datetime, float, float | None]] = []
        step = timedelta(minutes=interval_minutes)
        grouped: dict[datetime, list[float]] = {}
        for when, price, _ in raw:
            slot = start + ((when - start) // step) * step
            grouped.setdefault(slot, []).append(price)
        horizon_end = start + step * n_slots
        for slot, values in grouped.items():
            if start <= slot < horizon_end:
                aggregated.append((slot, sum(values) / len(values), None))
        if len(aggregated) < n_slots:
            raise PriceFetchError("Nord Pool returned fewer prices than requested for this window")
        return self._finish(aggregated, start, n_slots, interval_minutes)


class TibberProvider(TariffProvider):
    """Tibber GraphQL API (requires VOLTWISE_TARIFF_API_KEY or configured env var)."""

    ENDPOINT = "https://api.tibber.com/v1-beta/gql"
    QUERY = (
        "{ viewer { home { currentSubscription { priceInfo { "
        "today { startsAt total } tomorrow { startsAt total } } } } } }"
    )

    def fetch(self, start: datetime, n_slots: int, interval_minutes: int) -> list[PricePoint]:
        key = self._api_key()
        if not key:
            raise PriceFetchError(f"Tibber API key missing in environment variable {self.config.api_key_env}")
        body = json.dumps({"query": self.QUERY}).encode("utf-8")
        payload = self._get_json(
            self.ENDPOINT,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        if not isinstance(payload, dict):
            raise PriceFetchError("unexpected Tibber response shape")
        try:
            info = payload["data"]["viewer"]["home"]["currentSubscription"]["priceInfo"]
            rows = list(info["today"]) + list(info["tomorrow"])
        except (KeyError, TypeError):
            raise PriceFetchError("unexpected Tibber response shape") from None
        raw: list[tuple[datetime, float, float | None]] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("total"), (int, float)):
                continue
            try:
                when = datetime.fromisoformat(str(row.get("startsAt", "")).replace("Z", "+00:00"))
                value = float(row["total"])
            except (ValueError, OverflowError):
                raise PriceFetchError("Tibber returned an invalid price") from None
            raw.append((when, value, None))
        return self._finish(raw, start, n_slots, interval_minutes)


class OctopusProvider(TariffProvider):
    """Octopus Energy Agile tariff (requires an API key in the environment)."""

    ENDPOINT = "https://api.octopus.energy/v1/products/{product}/electricity-tariffs/{tariff}/standard-unit-rates/"

    def fetch(self, start: datetime, n_slots: int, interval_minutes: int) -> list[PricePoint]:
        import base64

        key = self._api_key()
        if not key:
            raise PriceFetchError(f"Octopus API key missing in environment variable {self.config.api_key_env}")
        token = base64.b64encode(f"{key}:".encode()).decode("ascii")
        url = self.ENDPOINT.format(
            product=self.config.octopus_product_code,
            tariff=self.config.octopus_tariff_code,
        ) + f"?period_from={start.strftime('%Y-%m-%dT%H:%MZ')}"
        payload = self._get_json(url, headers={"Authorization": f"Basic {token}"})
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise PriceFetchError("unexpected Octopus response shape")
        raw: list[tuple[datetime, float, float | None]] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("value_inc_vat"), (int, float)):
                continue
            try:
                when = datetime.fromisoformat(str(row.get("valid_from", "")).replace("Z", "+00:00"))
                value = float(row["value_inc_vat"]) / 100.0
            except (ValueError, OverflowError):
                raise PriceFetchError("Octopus returned an invalid price") from None
            raw.append((when, value, None))
        return self._finish(raw, start, n_slots, interval_minutes)


class HomeAssistantTariffProvider(TariffProvider):
    """Read prices from the official Home Assistant Nord Pool integration.

    This avoids vendor API keys entirely: HA maintains the connection and the
    price sensors expose a ``data`` attribute with ``start``/``price`` entries
    in EUR/MWh.
    """

    def __init__(self, config: TariffConfig, client=None) -> None:
        super().__init__(config)
        self.client = client

    @staticmethod
    def _rows(state: object) -> list[tuple[datetime, float]]:
        if not isinstance(state, dict):
            raise PriceFetchError("tariff sensor state must be a mapping")
        attributes = state.get("attributes", {})
        if not isinstance(attributes, dict):
            raise PriceFetchError("tariff sensor attributes must be a mapping")
        rows = attributes.get("data") or attributes.get("prices") or []
        if not isinstance(rows, (list, tuple)):
            raise PriceFetchError("tariff sensor price list must be a list")
        result: list[tuple[datetime, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            price = row.get("price")
            stamp = row.get("start") or row.get("startTime")
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                continue
            if not isinstance(stamp, str):
                continue
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                converted = float(price) / 1000.0
            except ValueError:
                continue
            except OverflowError:
                raise PriceFetchError("tariff sensor contains an oversized price") from None
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            result.append((when, converted))
        return result

    def fetch(self, start: datetime, n_slots: int, interval_minutes: int) -> list[PricePoint]:
        if self.client is None:
            raise PriceFetchError("home_assistant tariff provider needs a HA client")
        entities = [self.config.price_entity]
        if self.config.tomorrow_entity:
            entities.append(self.config.tomorrow_entity)
        raw: list[tuple[datetime, float, float | None]] = []
        for entity in entities:
            try:
                state = self.client.get_state(entity)  # type: ignore[union-attr]
            except PriceFetchError:
                raise
            except Exception as exc:
                raise PriceFetchError(f"cannot read tariff sensor {entity}: {type(exc).__name__}") from None
            rows = self._rows(state)
            if len(rows) > n_slots:
                rows = [
                    row for row in rows
                    if start <= row[0] < start + timedelta(minutes=interval_minutes * n_slots)
                ][:n_slots]
            raw.extend((when, price, None) for when, price in rows)
        if not raw:
            raise PriceFetchError("tariff sensor has no price data for the requested window")
        return self._finish(raw, start, n_slots, interval_minutes)


def create_provider(config: TariffConfig, client=None) -> TariffProvider:
    """Factory selected by the validated provider name."""
    providers: dict[str, type[TariffProvider]] = {
        "manual": ManualTariffProvider,
        "nordpool": NordPoolProvider,
        "tibber": TibberProvider,
        "octopus": OctopusProvider,
        "home_assistant": HomeAssistantTariffProvider,
    }
    try:
        if config.provider == "home_assistant":
            return HomeAssistantTariffProvider(config, client)
        return providers[config.provider](config)
    except KeyError:
        raise PriceFetchError(f"unknown tariff provider {config.provider!r}") from None
