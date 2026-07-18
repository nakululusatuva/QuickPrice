"""Binance Spot public market-data adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import aiohttp

from ._models import decimal_value, point, quote, utc_datetime
from .base import HttpProvider, MalformedResponse, UnsupportedInstrument, require_sequence


class BinanceProvider(HttpProvider):
    name = "binance"
    rest_base_url = "https://api.binance.com"
    websocket_base_url = "wss://stream.binance.com:9443"
    feed = "binance_spot"

    symbols: ClassVar[dict[str, str]] = {
        "BTC:USDC": "BTCUSDC",
        "ETH:USDC": "ETHUSDC",
        "SOL:USDC": "SOLUSDC",
        "WBETH:ETH": "WBETHETH",
        "WBETH:USDT": "WBETHUSDT",
        "USDC:USDT": "USDCUSDT",
    }
    _reverse_symbols: ClassVar[dict[str, str]] = {value: key for key, value in symbols.items()}
    _intervals: ClassVar[dict[str, str]] = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }

    def _exchange_symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        try:
            return self.symbols[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        exchange_symbol = self._exchange_symbol(normalized)
        payload = await self._request_json(
            "GET",
            f"{self.rest_base_url}/api/v3/aggTrades",
            params={"symbol": exchange_symbol, "limit": 1},
        )
        rows = require_sequence(payload, self.name, "aggregate trades")
        if not rows or not isinstance(rows[-1], dict):
            raise MalformedResponse(self.name, "aggregate trades are empty")
        row = rows[-1]
        try:
            price = decimal_value(row["p"])
            as_of = utc_datetime(row["T"], milliseconds=True)
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid aggregate trade") from exc
        return quote(
            symbol=normalized,
            price=price,
            as_of=as_of,
            provider=self.name,
            feed=self.feed,
            price_basis="last_trade",
            market_status="open",
            is_derived=False,
            components=(),
            fallback_level=0,
            license_scope="personal_internal",
            coverage="exchange",
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
        exchange_symbol = self._exchange_symbol(normalized)
        try:
            binance_interval = self._intervals[interval.lower()]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported interval {interval}") from exc
        requested_limit = max(1, min(limit or 1000, 1000))
        payload = await self._request_json(
            "GET",
            f"{self.rest_base_url}/api/v3/klines",
            params={
                "symbol": exchange_symbol,
                "interval": binance_interval,
                "startTime": int(start.astimezone(UTC).timestamp() * 1000),
                "endTime": int(end.astimezone(UTC).timestamp() * 1000),
                "limit": requested_limit,
            },
        )
        rows = require_sequence(payload, self.name, "klines")
        result = []
        for row in rows:
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 7:
                raise MalformedResponse(self.name, "invalid kline")
            try:
                result.append(
                    point(
                        symbol=normalized,
                        # Binance pagination filters ``startTime`` by kline open
                        # time (field 0). Persisting close time (field 6) and then
                        # advancing by one interval skips a bar at every page.
                        timestamp=utc_datetime(row[0], milliseconds=True),
                        price=decimal_value(row[4]),
                        provider=self.name,
                        interval=interval.lower(),
                        is_derived=False,
                    )
                )
            except ValueError as exc:
                raise MalformedResponse(self.name, "invalid kline value") from exc
        return tuple(result)

    async def stream_quotes(self, symbols: Sequence[str]) -> AsyncIterator[Any]:
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        exchange_symbols = [self._exchange_symbol(symbol) for symbol in normalized]
        streams = "/".join(f"{symbol.lower()}@trade" for symbol in exchange_symbols)
        session = await self._ensure_session()
        try:
            async with session.ws_connect(
                f"{self.websocket_base_url}/stream?streams={streams}",
                heartbeat=20,
                receive_timeout=60,
            ) as websocket:
                async for message in websocket:
                    if message.type is aiohttp.WSMsgType.TEXT:
                        try:
                            payload = message.json()
                        except ValueError as exc:
                            raise MalformedResponse(self.name, "invalid stream JSON") from exc
                        data = payload.get("data", payload) if isinstance(payload, dict) else None
                        if not isinstance(data, dict) or data.get("e") != "trade":
                            continue
                        exchange_symbol = str(data.get("s", "")).upper()
                        output_symbol = self._reverse_symbols.get(exchange_symbol)
                        if output_symbol is None:
                            continue
                        try:
                            yield quote(
                                symbol=output_symbol,
                                price=decimal_value(data["p"]),
                                as_of=utc_datetime(data["T"], milliseconds=True),
                                provider=self.name,
                                feed=self.feed,
                                price_basis="last_trade",
                                market_status="open",
                                is_derived=False,
                                components=(),
                                fallback_level=0,
                                license_scope="personal_internal",
                                coverage="exchange",
                            )
                        except KeyError, ValueError:
                            continue
                    elif message.type in {
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        break
        except (TimeoutError, aiohttp.ClientError) as exc:  # type: ignore[name-defined]
            from .base import ProviderUnavailable

            raise ProviderUnavailable(self.name, type(exc).__name__) from None
