"""CoinGecko Demo adapter used only as a slow crypto fallback."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import ClassVar

from ._models import component, decimal_value, quote, utc_datetime
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
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
            if normalized == "WBETH:USDC" and abs(asset_time - usdc_time) > timedelta(seconds=2):
                raise MalformedResponse(
                    self.name,
                    "WBETH and USDC component timestamps exceed two seconds",
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
        _ = (symbol, interval, start, end, limit)
        raise UnsupportedInstrument(
            self.name,
            "CoinGecko fallback history does not guarantee QuickPrice intraday intervals",
        )
