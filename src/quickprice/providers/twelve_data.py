"""Twelve Data REST adapter with the free-tier 790-credit safety ceiling."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, ClassVar

from quickprice.equities import LISTED_TICKERS
from quickprice.fx import FX_HUB_SYMBOLS
from quickprice.market import seconds_until_next_us_equity_open

from ._models import decimal_value, point, quote, utc_datetime
from ._ttl import AsyncTtlCache
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderBusy,
    ProviderRateLimited,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
)
from .quota import SlidingWindowRateGate, daily_budget


class _LocalRateGateAdmissionTimeout(ProviderBusy):
    """The local pacing queue saturated before an upstream request began."""


class TwelveDataProvider(HttpProvider):
    name = "twelve_data"
    base_url = "https://api.twelvedata.com"
    feed = "twelve_data_rest"
    symbols: ClassVar[dict[str, str]] = {
        **LISTED_TICKERS,
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
        symbol_bindings: Mapping[str, str] | None = None,
        fx_symbols: Sequence[str] | None = None,
        usd_cnh_quote_ttl_seconds: float = 240.0,
        usd_hkd_quote_ttl_seconds: float = 900.0,
        fx_quote_ttl_seconds: Mapping[str, float] | None = None,
        quote_cache_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        calls_per_minute: int = 8,
        rate_gate: SlidingWindowRateGate | None = None,
        rate_gate_timeout_seconds: float = 5.0,
        **kwargs,
    ):
        kwargs.setdefault("quota", daily_budget(790))
        super().__init__(**kwargs)
        self.api_key = api_key
        self.symbols = {
            symbol.strip().upper(): vendor_symbol.strip().upper()
            for symbol, vendor_symbol in (
                type(self).symbols if symbol_bindings is None else symbol_bindings
            ).items()
        }
        requested_fx_symbols = (
            FX_HUB_SYMBOLS
            if fx_symbols is None and symbol_bindings is None
            else (
                tuple(
                    symbol for symbol, vendor_symbol in self.symbols.items() if "/" in vendor_symbol
                )
                if fx_symbols is None
                else fx_symbols
            )
        )
        self.fx_symbols = frozenset(symbol.strip().upper() for symbol in requested_fx_symbols)
        if any(symbol not in self.symbols for symbol in self.fx_symbols):
            raise ValueError("Twelve Data FX symbol is not present in symbol bindings")
        self.equity_symbols = frozenset(self.symbols) - self.fx_symbols
        self.fx_quote_ttl_floors_seconds = MappingProxyType(
            {symbol: 240.0 if symbol == "USD:CNH" else 900.0 for symbol in self.fx_symbols}
        )
        if rate_gate_timeout_seconds <= 0:
            raise ValueError("rate_gate_timeout_seconds must be positive")
        self.rate_gate_timeout_seconds = float(rate_gate_timeout_seconds)
        self.routing_timeout_seconds = self.rate_gate_timeout_seconds + self.request_timeout + 1.0
        self._rate_gate = rate_gate or SlidingWindowRateGate(calls_per_minute, 60.0)
        requested_ttls = {
            symbol: (
                usd_cnh_quote_ttl_seconds if symbol == "USD:CNH" else usd_hkd_quote_ttl_seconds
            )
            for symbol in self.fx_symbols
        }
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
            for symbol in self.fx_symbols
        }
        self._quote_cache = AsyncTtlCache[str, Any](clock=quote_cache_clock)
        self._wall_clock = wall_clock

    async def _request_json(self, *args, **kwargs):
        # Twelve applies a short-window ceiling independently from its daily
        # credit budget. Serialize every adapter request through one provider-
        # wide gate so concurrent startup quote/history work cannot burst 429s.
        try:
            async with asyncio.timeout(self.rate_gate_timeout_seconds):
                await self._rate_gate.acquire()
        except TimeoutError:
            raise _LocalRateGateAdmissionTimeout(
                self.name, "local short-window rate gate admission timed out"
            ) from None
        return await super()._request_json(*args, **kwargs)

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
        if ttl_seconds is None and normalized in self.equity_symbols:
            ttl_seconds = seconds_until_next_us_equity_open(self._wall_clock())
        if ttl_seconds is not None:
            try:
                return await self._quote_cache.get_or_load(
                    normalized,
                    ttl_seconds,
                    lambda: self._get_quote_uncached(normalized),
                )
            except _LocalRateGateAdmissionTimeout:
                # No vendor request occurred, so this transient local error
                # must not suppress the next poll for the positive 240/900s
                # quote TTL. Other provider errors remain negative-cached.
                self._quote_cache.discard(normalized)
                raise
        return await self._get_quote_uncached(normalized)

    async def _get_quote_uncached(self, normalized: str):
        vendor_symbol = self._vendor_symbol(normalized)
        if normalized in self.fx_symbols:
            return await self._get_latest_fx_bar(normalized, vendor_symbol)
        payload = await self._request_json(
            "GET",
            f"{self.base_url}/quote",
            params={"symbol": vendor_symbol, "apikey": self.api_key, "timezone": "UTC"},
            allow_quota_reserve=normalized in self.fx_symbols,
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
            price_basis="exchange_rate" if normalized in self.fx_symbols else "last",
            market_status=market_status,
            is_derived=False,
            components=(),
            fallback_level=0,
            license_scope="personal_internal",
            coverage="vendor_aggregate",
            market_status_as_of=self._wall_clock() if is_open in {True, False} else None,
        )

    async def _get_latest_fx_bar(self, normalized: str, vendor_symbol: str):
        """Use Twelve's latest timestamped minute bar for an FX quote.

        The free ``/quote`` response can retain an old timestamp while the
        ``/time_series`` feed continues to advance. A single one-row request
        costs the same credit as ``/quote`` and keeps the five cached USD hub
        legs within the existing daily budget. The returned bar timestamp is
        preserved verbatim; callers can therefore reject or mark it stale
        without QuickPrice inventing freshness.
        """

        payload = await self._request_json(
            "GET",
            f"{self.base_url}/time_series",
            params={
                "symbol": vendor_symbol,
                "interval": "1min",
                "outputsize": 1,
                "order": "DESC",
                "adjust": "none",
                "timezone": "UTC",
                "apikey": self.api_key,
            },
            allow_quota_reserve=True,
        )
        document = self._document(payload)
        rows = document.get("values")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
            raise MalformedResponse(self.name, "latest FX time-series values must be an array")
        try:
            row = max(
                rows,
                key=lambda item: (
                    utc_datetime(item["datetime"])
                    if isinstance(item, Mapping)
                    else datetime.min.replace(tzinfo=UTC)
                ),
            )
            if not isinstance(row, Mapping):
                raise KeyError("row")
            price = decimal_value(row["close"])
            as_of = utc_datetime(row["datetime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid latest FX time-series value") from exc
        return quote(
            symbol=normalized,
            price=price,
            as_of=as_of,
            provider=self.name,
            feed=f"{self.feed}_time_series",
            price_basis="time_series_close",
            market_status="unknown",
            is_derived=False,
            components=(),
            fallback_level=0,
            license_scope="personal_internal",
            coverage="vendor_aggregate",
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
            allow_quota_reserve=normalized in self.fx_symbols,
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
