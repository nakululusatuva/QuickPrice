"""Finnhub quote adapter for US-listed stocks and ETFs."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, ClassVar

import aiohttp

from quickprice.equities import LISTED_TICKERS

from ._models import decimal_value, quote, utc_datetime
from ._ttl import AsyncTtlCache
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
    require_sequence,
)
from .quota import SlidingWindowRateGate, minute_budget


class FinnhubProvider(HttpProvider):
    """Quote-only Finnhub adapter suitable for the personal free tier."""

    name = "finnhub"
    base_url = "https://api.finnhub.io/api/v1"
    websocket_url = "wss://ws.finnhub.io"
    feed = "finnhub_rest"
    stream_feed = "finnhub_websocket"
    stream_poll_suppression_seconds = 120.0
    closed_market_quote_poll_seconds = 900.0
    symbols: ClassVar[dict[str, str]] = dict(LISTED_TICKERS)
    # Finnhub's personal free tier accepts at most 50 WebSocket symbols. Any
    # future symbols beyond this prefix continue to use quota-bounded REST.
    stream_symbols: ClassVar[tuple[str, ...]] = tuple(symbols)[:50]
    _reverse_symbols: ClassVar[dict[str, str]] = {
        ticker: symbol for symbol, ticker in symbols.items()
    }

    def __init__(
        self,
        api_key: str,
        *,
        quote_cache_ttl_seconds: float | None = None,
        quote_cache_clock: Callable[[], float] = time.monotonic,
        burst_gate: SlidingWindowRateGate | None = None,
        **kwargs: Any,
    ) -> None:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("api_key cannot be empty")
        kwargs.setdefault("quota", minute_budget(60))
        super().__init__(**kwargs)
        self.api_key = normalized_key
        quota_limit = self.quota.limit if self.quota is not None else 60
        # Leave meaningful headroom below the upstream minute ceiling. The
        # dynamic floor also scales safely if the canonical catalog grows.
        usable_calls_per_minute = max(1.0, quota_limit * 0.8)
        safe_cadence = max(20.0, len(self.symbols) * 60.0 / usable_calls_per_minute)
        self.minimum_quote_poll_seconds = safe_cadence
        self.quote_cache_ttl_seconds = max(
            safe_cadence,
            quote_cache_ttl_seconds if quote_cache_ttl_seconds is not None else safe_cadence,
        )
        self._quote_cache = AsyncTtlCache[str, Any](clock=quote_cache_clock)
        # The upstream global ceiling is 30 calls/second. Keep one call of
        # headroom and enforce a true sliding window, including cold starts.
        self._burst_gate = burst_gate or SlidingWindowRateGate(29, 1.0)

    @property
    def _headers(self) -> dict[str, str]:
        # Header authentication keeps the credential out of REST URLs and logs.
        return {"X-Finnhub-Token": self.api_key}

    def _ticker(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        try:
            return self.symbols[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        self._ticker(normalized)
        return await self._quote_cache.get_or_load(
            normalized,
            self.quote_cache_ttl_seconds,
            lambda: self._get_quote(normalized),
        )

    async def _get_quote(self, normalized: str):
        ticker = self._ticker(normalized)
        await self._burst_gate.acquire()
        payload = await self._request_json(
            "GET",
            f"{self.base_url}/quote",
            params={"symbol": ticker},
            headers=self._headers,
        )
        document = self._document(payload)
        try:
            price = decimal_value(document["c"])
            if price <= 0:
                raise ProviderUnavailable(self.name, "no current quote data")
            raw_timestamp = decimal_value(document["t"])
            if raw_timestamp <= 0:
                raise ProviderUnavailable(self.name, "no current quote data")
            as_of = utc_datetime(raw_timestamp)
        except ProviderUnavailable:
            raise
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid current quote") from exc
        return quote(
            symbol=normalized,
            price=price,
            as_of=as_of,
            provider=self.name,
            feed=self.feed,
            price_basis="last_trade",
            market_status="unknown",
            is_derived=False,
            components=(),
            fallback_level=0,
            license_scope="personal_internal_no_redistribution",
            coverage="us_realtime_unspecified",
            market_status_as_of=None,
        )

    def _document(self, payload: Any) -> Mapping[str, Any]:
        document = require_mapping(payload, self.name)
        if document.get("error") not in (None, ""):
            raise ProviderUnavailable(self.name, "upstream returned an error")
        return document

    async def stream_quotes(self, symbols: Sequence[str]) -> AsyncIterator[Any]:
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        tickers = [self._ticker(symbol) for symbol in normalized]
        session = await self._ensure_session()
        try:
            async with session.ws_connect(
                self.websocket_url,
                params={"token": self.api_key},
                heartbeat=20,
                receive_timeout=60,
                **self._proxy_request_options(),
            ) as websocket:
                for ticker in tickers:
                    await websocket.send_json({"type": "subscribe", "symbol": ticker})
                async for message in websocket:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        if message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            break
                        continue
                    try:
                        document = require_mapping(message.json(), self.name, "stream message")
                    except ValueError as exc:
                        raise MalformedResponse(self.name, "invalid stream JSON") from exc
                    if document.get("type") == "error":
                        raise ProviderUnavailable(
                            self.name, "stream authentication/subscription failed"
                        )
                    if document.get("type") != "trade":
                        continue
                    rows = require_sequence(document.get("data"), self.name, "stream trades")
                    for raw_row in rows:
                        if not isinstance(raw_row, Mapping):
                            continue
                        output_symbol = self._reverse_symbols.get(str(raw_row.get("s", "")).upper())
                        if output_symbol is None:
                            continue
                        try:
                            price = decimal_value(raw_row["p"])
                            raw_timestamp = decimal_value(raw_row["t"])
                            if price <= 0 or raw_timestamp <= 0:
                                continue
                            as_of = utc_datetime(raw_timestamp, milliseconds=True)
                        except KeyError, ValueError:
                            continue
                        yield quote(
                            symbol=output_symbol,
                            price=price,
                            as_of=as_of,
                            provider=self.name,
                            feed=self.stream_feed,
                            price_basis="last_trade",
                            market_status="unknown",
                            is_derived=False,
                            components=(),
                            fallback_level=0,
                            license_scope="personal_internal_no_redistribution",
                            coverage="us_realtime_unspecified",
                            market_status_as_of=None,
                        )
        except (TimeoutError, aiohttp.ClientError) as exc:
            # aiohttp errors may embed a WebSocket URL. Suppress their cause so
            # the authentication token cannot leak into an exception traceback.
            raise ProviderUnavailable(self.name, type(exc).__name__) from None
