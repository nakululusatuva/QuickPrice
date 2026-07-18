"""Twelve Data REST adapter with the free-tier 790-credit safety ceiling."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, ClassVar

from quickprice.fx import FX_HUB_SYMBOLS

from ._models import decimal_value, point, quote, utc_datetime
from ._ttl import AsyncTtlCache
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
)
from .quota import daily_budget


class TwelveDataProvider(HttpProvider):
    name = "twelve_data"
    base_url = "https://api.twelvedata.com"
    feed = "twelve_data_rest"
    symbols: ClassVar[dict[str, str]] = {
        "QQQM:USD": "QQQM",
        "BOXX:USD": "BOXX",
        "SGOV:USD": "SGOV",
        **{symbol: symbol.replace(":", "/") for symbol in FX_HUB_SYMBOLS},
    }
    fx_quote_ttl_floors_seconds: ClassVar[Mapping[str, float]] = MappingProxyType(
        {symbol: 240.0 if symbol == "USD:CNH" else 900.0 for symbol in FX_HUB_SYMBOLS}
    )
    _intervals: ClassVar[dict[str, str]] = {
        "1m": "1min",
        "5m": "5min",
        "15m": "15min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1day",
    }

    def __init__(
        self,
        api_key: str,
        *,
        usd_cnh_quote_ttl_seconds: float = 240.0,
        usd_hkd_quote_ttl_seconds: float = 900.0,
        fx_quote_ttl_seconds: Mapping[str, float] | None = None,
        quote_cache_clock: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        kwargs.setdefault("quota", daily_budget(790))
        super().__init__(**kwargs)
        self.api_key = api_key
        requested_ttls = {symbol: usd_hkd_quote_ttl_seconds for symbol in FX_HUB_SYMBOLS}
        requested_ttls["USD:CNH"] = usd_cnh_quote_ttl_seconds
        if fx_quote_ttl_seconds is not None:
            normalized_ttls = {
                symbol.strip().upper(): float(ttl) for symbol, ttl in fx_quote_ttl_seconds.items()
            }
            unknown = normalized_ttls.keys() - self.fx_quote_ttl_floors_seconds.keys()
            if unknown:
                raise ValueError(f"unsupported FX cache policy: {', '.join(sorted(unknown))}")
            requested_ttls.update(normalized_ttls)
        # Five shared USD-spoke caches consume at most 744 quote credits per
        # UTC day at their default floors: 360 for CNH plus 96 for each of the
        # other four spokes. All 30 public pairs reuse these cache entries.
        self.quote_cache_ttl_seconds = {
            symbol: max(self.fx_quote_ttl_floors_seconds[symbol], requested_ttls[symbol])
            for symbol in FX_HUB_SYMBOLS
        }
        self._quote_cache = AsyncTtlCache[str, Any](clock=quote_cache_clock)

    def _vendor_symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        try:
            return self.symbols[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc

    def _document(self, payload: Any) -> Mapping[str, Any]:
        document = require_mapping(payload, self.name)
        if str(document.get("status", "")).lower() == "error" or (
            "code" in document and "message" in document
        ):
            code = document.get("code")
            if code in (429, "429") or "credits" in str(document.get("message", "")).lower():
                raise ProviderRateLimited(self.name, "upstream quota exceeded")
            raise ProviderUnavailable(self.name, "upstream returned an error")
        return document

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        ttl_seconds = self.quote_cache_ttl_seconds.get(normalized)
        if ttl_seconds is not None:
            return await self._quote_cache.get_or_load(
                normalized,
                ttl_seconds,
                lambda: self._get_quote_uncached(normalized),
            )
        return await self._get_quote_uncached(normalized)

    async def _get_quote_uncached(self, normalized: str):
        vendor_symbol = self._vendor_symbol(normalized)
        payload = await self._request_json(
            "GET",
            f"{self.base_url}/quote",
            params={"symbol": vendor_symbol, "apikey": self.api_key, "timezone": "UTC"},
            allow_quota_reserve=normalized in FX_HUB_SYMBOLS,
        )
        document = self._document(payload)
        try:
            price = decimal_value(document.get("close", document.get("price")))
            if document.get("timestamp") is not None:
                as_of = utc_datetime(document["timestamp"])
            else:
                as_of = utc_datetime(document["datetime"])
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid quote") from exc
        is_open = document.get("is_market_open")
        market_status = "open" if is_open is True else "closed" if is_open is False else "unknown"
        return quote(
            symbol=normalized,
            price=price,
            as_of=as_of,
            provider=self.name,
            feed=self.feed,
            price_basis="exchange_rate" if normalized in FX_HUB_SYMBOLS else "last",
            market_status=market_status,
            is_derived=False,
            components=(),
            fallback_level=0,
            license_scope="personal_internal",
            coverage="vendor_aggregate",
            market_status_as_of=datetime.now(UTC) if is_open in {True, False} else None,
        )

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
        vendor_symbol = self._vendor_symbol(normalized)
        try:
            vendor_interval = self._intervals[interval.lower()]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported interval {interval}") from exc
        output_size = max(1, min(limit or 5_000, 5_000))
        payload = await self._request_json(
            "GET",
            f"{self.base_url}/time_series",
            params={
                "symbol": vendor_symbol,
                "interval": vendor_interval,
                "start_date": start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "outputsize": output_size,
                "order": "ASC",
                "adjust": "none",
                "timezone": "UTC",
                "apikey": self.api_key,
            },
            allow_quota_reserve=normalized in FX_HUB_SYMBOLS,
        )
        document = self._document(payload)
        rows = document.get("values")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise MalformedResponse(self.name, "time-series values must be an array")
        result = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise MalformedResponse(self.name, "invalid time-series row")
            try:
                result.append(
                    point(
                        symbol=normalized,
                        timestamp=utc_datetime(row["datetime"]),
                        price=decimal_value(row["close"]),
                        provider=self.name,
                        interval=interval.lower(),
                        is_derived=False,
                    )
                )
            except (KeyError, ValueError) as exc:
                raise MalformedResponse(self.name, "invalid time-series value") from exc
        return tuple(sorted(result, key=lambda item: item.timestamp))
