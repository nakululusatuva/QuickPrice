"""Kraken public REST and WebSocket v2 market-data adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import aiohttp

from ._models import decimal_value, point, quote, utc_datetime
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
)


class KrakenProvider(HttpProvider):
    name = "kraken"
    # Kraken OHLC exposes at most the latest 720 entries regardless of the
    # requested ``since`` value. The router may fetch an older prefix from the
    # next provider when this adapter cannot cover the requested start.
    history_prefix_limited = True
    rest_base_url = "https://api.kraken.com/0/public"
    websocket_url = "wss://ws.kraken.com/v2"
    feed = "kraken_spot"

    symbols: ClassVar[dict[str, tuple[str, str]]] = {
        "BTC:USDC": ("XBTUSDC", "BTC/USDC"),
        "ETH:USDC": ("ETHUSDC", "ETH/USDC"),
        "SOL:USDC": ("SOLUSDC", "SOL/USDC"),
        "XMR:USDC": ("XMRUSDC", "XMR/USDC"),
    }
    _ws_reverse: ClassVar[dict[str, str]] = {
        ws: canonical for canonical, (_, ws) in symbols.items()
    }
    _intervals: ClassVar[dict[str, int]] = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "1h": 60,
        "4h": 240,
        "1d": 1440,
    }

    def _pair(self, symbol: str) -> tuple[str, str]:
        normalized = symbol.strip().upper()
        try:
            return self.symbols[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc

    def _result(self, payload: Any) -> Mapping[str, Any]:
        document = require_mapping(payload, self.name)
        errors = document.get("error")
        if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
            message = str(errors[0])
            if "Rate limit" in message:
                from .base import ProviderRateLimited

                raise ProviderRateLimited(self.name, "upstream quota exceeded")
            raise ProviderUnavailable(self.name, "upstream returned an error")
        return require_mapping(document.get("result"), self.name, "result")

    @staticmethod
    def _dynamic_rows(result: Mapping[str, Any]) -> Any:
        for key, value in result.items():
            if key != "last":
                return value
        return None

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        pair, _ = self._pair(normalized)
        payload = await self._request_json(
            "GET",
            f"{self.rest_base_url}/Trades",
            params={"pair": pair, "count": 1},
        )
        rows = self._dynamic_rows(self._result(payload))
        if not isinstance(rows, Sequence) or not rows:
            raise MalformedResponse(self.name, "trades are empty")
        trade = rows[-1]
        if not isinstance(trade, Sequence) or isinstance(trade, (str, bytes)) or len(trade) < 3:
            raise MalformedResponse(self.name, "invalid trade")
        try:
            price = decimal_value(trade[0])
            as_of = utc_datetime(trade[2])
        except ValueError as exc:
            raise MalformedResponse(self.name, "invalid trade value") from exc
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
        pair, _ = self._pair(normalized)
        try:
            minutes = self._intervals[interval.lower()]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported interval {interval}") from exc
        payload = await self._request_json(
            "GET",
            f"{self.rest_base_url}/OHLC",
            params={
                "pair": pair,
                "interval": minutes,
                "since": int(start.astimezone(UTC).timestamp()),
            },
        )
        rows = self._dynamic_rows(self._result(payload))
        if not isinstance(rows, Sequence):
            raise MalformedResponse(self.name, "invalid OHLC result")
        result = []
        for row in rows:
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 5:
                raise MalformedResponse(self.name, "invalid OHLC row")
            timestamp = utc_datetime(row[0])
            if timestamp > end.astimezone(UTC):
                continue
            try:
                result.append(
                    point(
                        symbol=normalized,
                        timestamp=timestamp,
                        price=decimal_value(row[4]),
                        provider=self.name,
                        interval=interval.lower(),
                        is_derived=False,
                    )
                )
            except ValueError as exc:
                raise MalformedResponse(self.name, "invalid OHLC value") from exc
        if limit is not None:
            result = result[-max(0, limit) :]
        return tuple(result)

    async def stream_quotes(self, symbols: Sequence[str]) -> AsyncIterator[Any]:
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        ws_symbols = [self._pair(symbol)[1] for symbol in normalized]
        session = await self._ensure_session()
        try:
            async with session.ws_connect(
                self.websocket_url, heartbeat=20, receive_timeout=60
            ) as websocket:
                await websocket.send_json(
                    {
                        "method": "subscribe",
                        "params": {"channel": "trade", "symbol": ws_symbols, "snapshot": False},
                    }
                )
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
                        payload = message.json()
                    except ValueError as exc:
                        raise MalformedResponse(self.name, "invalid stream JSON") from exc
                    if not isinstance(payload, dict) or payload.get("channel") != "trade":
                        continue
                    data = payload.get("data", ())
                    if not isinstance(data, Sequence):
                        continue
                    for trade in data:
                        if not isinstance(trade, Mapping):
                            continue
                        output_symbol = self._ws_reverse.get(str(trade.get("symbol", "")))
                        if output_symbol is None:
                            continue
                        try:
                            yield quote(
                                symbol=output_symbol,
                                price=decimal_value(trade["price"]),
                                as_of=utc_datetime(trade["timestamp"]),
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
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise ProviderUnavailable(self.name, type(exc).__name__) from None
