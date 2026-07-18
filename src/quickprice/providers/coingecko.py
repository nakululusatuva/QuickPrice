"""CoinGecko Demo adapter used only as a slow crypto fallback."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from ._models import component, decimal_value, point, quote, utc_datetime
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
    require_sequence,
)
from .quota import rolling_month_safe_daily_budget


class CoinGeckoProvider(HttpProvider):
    name = "coingecko"
    base_url = "https://api.coingecko.com/api/v3"
    feed = "coingecko_aggregated"
    coin_ids: ClassVar[dict[str, str]] = {
        "BTC:USDC": "bitcoin",
        "ETH:USDC": "ethereum",
        "WBETH:USDC": "wrapped-beacon-eth",
        "STETH:USDC": "staked-ether",
        "WSTETH:USDC": "wrapped-steth",
        "ETH:USD": "ethereum",
        "STETH:USD": "staked-ether",
        "WSTETH:USD": "wrapped-steth",
    }
    history_symbols: ClassVar[frozenset[str]] = frozenset(
        {
            "STETH:USDC",
            "WSTETH:USDC",
            "ETH:USD",
            "STETH:USD",
            "WSTETH:USD",
        }
    )
    component_skew_limits: ClassVar[dict[str, timedelta]] = {
        "WBETH:USDC": timedelta(seconds=2),
        "STETH:USDC": timedelta(seconds=60),
        "WSTETH:USDC": timedelta(seconds=60),
    }

    def __init__(
        self,
        api_key: str | None = None,
        *,
        coin_ids: Mapping[str, str] | None = None,
        cache_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        kwargs.setdefault("quota", rolling_month_safe_daily_budget(9_000))
        super().__init__(**kwargs)
        self.api_key = api_key
        self.coin_ids = {
            key.strip().upper(): value for key, value in (coin_ids or type(self).coin_ids).items()
        }
        self._cache_ttl_seconds = max(1.0, cache_ttl_seconds)
        self._clock = clock
        self._refresh_lock = asyncio.Lock()
        self._price_cache: Mapping[str, object] | None = None
        self._cache_expires_at = 0.0
        self._refresh_error: str | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-cg-demo-api-key": self.api_key} if self.api_key else {}

    def _coin(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        try:
            return self.coin_ids[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        coin_id = self._coin(normalized)
        document = await self._simple_prices()
        try:
            asset = require_mapping(document[coin_id], self.name, coin_id)
            usdc = require_mapping(document["usd-coin"], self.name, "usd-coin")
            asset_price = decimal_value(asset["usd"])
            usdc_price = decimal_value(usdc["usd"])
            asset_time = utc_datetime(asset["last_updated_at"])
            usdc_time = utc_datetime(usdc["last_updated_at"])
            skew_limit = self.component_skew_limits.get(normalized)
            if skew_limit is not None and abs(asset_time - usdc_time) > skew_limit:
                raise MalformedResponse(
                    self.name,
                    "staking-token and USDC component timestamps exceed the configured limit",
                )
            result_price = asset_price / usdc_price
        except MalformedResponse:
            raise
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            raise MalformedResponse(self.name, "invalid simple-price response") from exc
        components = (
            component(
                symbol=f"{normalized.split(':', 1)[0]}:USD",
                provider=self.name,
                price=asset_price,
                as_of=asset_time,
                feed=self.feed,
                role="numerator",
            ),
            component(
                symbol="USDC:USD",
                provider=self.name,
                price=usdc_price,
                as_of=usdc_time,
                feed=self.feed,
                role="denominator",
            ),
        )
        return quote(
            symbol=normalized,
            price=result_price,
            as_of=min(asset_time, usdc_time),
            provider=self.name,
            feed=self.feed,
            price_basis="aggregated_spot_ratio",
            market_status="open",
            is_derived=True,
            components=components,
            fallback_level=0,
            license_scope="personal_internal",
            coverage="aggregated",
        )

    async def _simple_prices(self) -> Mapping[str, object]:
        now = self._clock()
        if now < self._cache_expires_at:
            if self._price_cache is not None:
                return self._price_cache
            raise ProviderUnavailable(
                self.name,
                self._refresh_error or "simple-price refresh is in backoff",
            )

        async with self._refresh_lock:
            now = self._clock()
            if now < self._cache_expires_at:
                if self._price_cache is not None:
                    return self._price_cache
                raise ProviderUnavailable(
                    self.name,
                    self._refresh_error or "simple-price refresh is in backoff",
                )

            # Advance the deadline before I/O. A failed refresh is negative-cached,
            # so concurrent symbol requests cannot consume one credit each.
            self._cache_expires_at = now + self._cache_ttl_seconds
            self._price_cache = None
            ids = sorted({*self.coin_ids.values(), "usd-coin"})
            try:
                payload = await self._request_json(
                    "GET",
                    f"{self.base_url}/simple/price",
                    params={
                        "ids": ",".join(ids),
                        "vs_currencies": "usd",
                        "include_last_updated_at": "true",
                    },
                    headers=self._headers,
                )
                document = require_mapping(payload, self.name)
            except Exception as exc:
                self._refresh_error = str(exc) or type(exc).__name__
                raise
            self._price_cache = document
            self._refresh_error = None
            return document

    async def get_history(
        self,
        symbol: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ):
        normalized = symbol.strip().upper()
        if normalized not in self.history_symbols:
            raise UnsupportedInstrument(
                self.name,
                "CoinGecko history is enabled only for configured liquid-staking assets",
            )
        normalized_interval = interval.strip().lower()
        if normalized_interval not in {"1m", "5m", "1d"}:
            raise UnsupportedInstrument(
                self.name,
                f"unsupported interval {normalized_interval}",
            )
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        if start_utc >= end_utc:
            raise ValueError("history start must be before end")

        coin_id = self._coin(normalized)
        params: dict[str, str | int] = {
            "vs_currency": "usd",
            "from": int(start_utc.timestamp()),
            "to": int(end_utc.timestamp()),
            "precision": "full",
        }
        # Daily granularity is public. Intraday requests deliberately use
        # automatic granularity because five-minute data is plan-dependent.
        if normalized_interval == "1d":
            params["interval"] = "daily"
        payload = await self._request_json(
            "GET",
            f"{self.base_url}/coins/{coin_id}/market_chart/range",
            params=params,
            headers=self._headers,
        )
        document = require_mapping(payload, self.name)
        prices = require_sequence(document.get("prices"), self.name, "historical prices")
        quoted_in_usdc = normalized.endswith(":USDC")
        current_usdc_usd = (
            await self._history_usdc_normalization_price() if quoted_in_usdc else decimal_value(1)
        )

        result = []
        for row in prices:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise MalformedResponse(self.name, "invalid historical price row")
            try:
                timestamp = utc_datetime(row[0], milliseconds=True)
                price_value = decimal_value(row[1]) / current_usdc_usd
            except (ValueError, ZeroDivisionError) as exc:
                raise MalformedResponse(self.name, "invalid historical price value") from exc
            if start_utc <= timestamp <= end_utc:
                result.append(
                    point(
                        symbol=normalized,
                        timestamp=timestamp,
                        price=price_value,
                        provider=self.name,
                        interval=normalized_interval,
                        is_derived=quoted_in_usdc,
                    )
                )
        if limit is not None:
            result = result[-max(0, limit) :]
        return tuple(result)

    async def _history_usdc_normalization_price(self):
        # Historical changes are invariant to a constant quote-currency
        # normalization. Reuse the most recently observed USDC/USD value even
        # after the live-price TTL expires so hourly backfills do not consume a
        # second monthly credit stream. The first history request still obtains
        # a real observation if no quote has populated the cache yet.
        document = self._price_cache
        if document is None:
            document = await self._simple_prices()
        try:
            usdc = require_mapping(document["usd-coin"], self.name, "usd-coin")
            value = decimal_value(usdc["usd"])
            if value <= 0:
                raise ValueError("USDC price must be positive")
            return value
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid USDC normalization price") from exc
