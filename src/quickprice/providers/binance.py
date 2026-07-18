"""Binance Spot public market-data adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import aiohttp

from ._models import component, decimal_value, point, quote, utc_datetime
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_sequence,
)

# Binance documents 1,024 streams per connection and 300 connection attempts
# per five minutes per IP. Smaller shards bound the combined-stream request URI;
# ten shards still cover the managed catalog's 2,000-instrument ceiling while
# keeping a full reconnect well below the provider's connection-attempt limit.
BINANCE_STREAMS_PER_CONNECTION = 250
BINANCE_STREAM_PATH_MAX_CHARACTERS = 8_000
BINANCE_MAX_STREAM_CONNECTIONS = 10


class BinanceProvider(HttpProvider):
    name = "binance"
    rest_base_url = "https://api.binance.com"
    websocket_base_url = "wss://stream.binance.com:9443"
    feed = "binance_spot"

    symbols: ClassVar[dict[str, str]] = {
        "BTC:USDC": "BTCUSDC",
        "ETH:USDC": "ETHUSDC",
        "SOL:USDC": "SOLUSDC",
        "POL:USDC": "POLUSDC",
        "BNB:USDC": "BNBUSDC",
        "TRX:USDC": "TRXUSDC",
        "WBETH:ETH": "WBETHETH",
        "WBETH:USDT": "WBETHUSDT",
        "USDC:USDT": "USDCUSDT",
    }
    _reverse_symbols: ClassVar[dict[str, str]] = {value: key for key, value in symbols.items()}
    _midpoint_symbols: ClassVar[frozenset[str]] = frozenset(
        {"WBETH:ETH", "WBETH:USDT", "USDC:USDT"}
    )
    _intervals: ClassVar[dict[str, str]] = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }

    def __init__(
        self,
        *args,
        symbol_bindings: Mapping[str, str] | None = None,
        midpoint_symbols: Sequence[str] | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        maximum_relative_book_spread: Decimal = Decimal("0.005"),
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.symbols = {
            symbol.strip().upper(): vendor_symbol.strip().upper()
            for symbol, vendor_symbol in (
                type(self).symbols if symbol_bindings is None else symbol_bindings
            ).items()
        }
        if len(set(self.symbols.values())) != len(self.symbols):
            raise ValueError("Binance vendor symbols must be unique")
        self._reverse_symbols = {value: key for key, value in self.symbols.items()}
        configured_midpoints = (
            type(self)._midpoint_symbols if midpoint_symbols is None else midpoint_symbols
        )
        self._midpoint_symbols = frozenset(
            symbol.strip().upper()
            for symbol in configured_midpoints
            if symbol.strip().upper() in self.symbols
        )
        self._wall_clock = wall_clock
        self.maximum_relative_book_spread = decimal_value(maximum_relative_book_spread)
        if self.maximum_relative_book_spread <= 0:
            raise ValueError("maximum_relative_book_spread must be positive")

    def _exchange_symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        try:
            return self.symbols[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc

    def stream_connection_batches(
        self,
        symbols: Sequence[str],
    ) -> tuple[tuple[str, ...], ...]:
        """Return bounded canonical-symbol shards for combined WebSockets."""

        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        batches: list[tuple[str, ...]] = []
        current: list[str] = []
        current_path_characters = 0
        for symbol in normalized:
            stream_name = f"{self._exchange_symbol(symbol).lower()}@trade"
            added_characters = len(stream_name) + (1 if current else 0)
            if current and (
                len(current) >= BINANCE_STREAMS_PER_CONNECTION
                or current_path_characters + added_characters > BINANCE_STREAM_PATH_MAX_CHARACTERS
            ):
                batches.append(tuple(current))
                current = []
                current_path_characters = 0
                added_characters = len(stream_name)
            current.append(symbol)
            current_path_characters += added_characters
        if current:
            batches.append(tuple(current))
        if len(batches) > BINANCE_MAX_STREAM_CONNECTIONS:
            raise ProviderUnavailable(
                self.name,
                "stream subscription exceeds the safe connection-shard limit",
            )
        return tuple(batches)

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        exchange_symbol = self._exchange_symbol(normalized)
        if normalized in self._midpoint_symbols:
            return await self._get_midpoint_quote(normalized, exchange_symbol)
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

    async def _get_midpoint_quote(self, normalized: str, exchange_symbol: str):
        """Observe a current book midpoint for synthetic-only WBETH legs.

        Last trades in these thin component markets can be minutes apart even
        while both books remain live. The book ticker is an observation at
        retrieval time, so concurrent legs can retain the strict two-second
        synthetic skew without relabeling an old trade as current.
        """

        payload = await self._request_json(
            "GET",
            f"{self.rest_base_url}/api/v3/ticker/bookTicker",
            params={"symbol": exchange_symbol},
        )
        if not isinstance(payload, dict):
            raise MalformedResponse(self.name, "book ticker must be an object")
        try:
            bid = decimal_value(payload["bidPrice"])
            ask = decimal_value(payload["askPrice"])
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid book ticker") from exc
        if bid <= 0 or ask <= 0 or ask < bid:
            raise MalformedResponse(self.name, "invalid book spread")
        midpoint = (bid + ask) / 2
        if (ask - bid) / midpoint > self.maximum_relative_book_spread:
            raise ProviderUnavailable(self.name, "book spread exceeds safety limit")
        observed_at = self._wall_clock().astimezone(UTC)
        feed = f"{self.feed}_book"
        return quote(
            symbol=normalized,
            price=midpoint,
            as_of=observed_at,
            provider=self.name,
            feed=feed,
            price_basis="midpoint",
            market_status="open",
            is_derived=True,
            components=(
                component(
                    symbol=normalized,
                    provider=self.name,
                    price=bid,
                    as_of=observed_at,
                    feed=feed,
                    role="best_bid",
                ),
                component(
                    symbol=normalized,
                    provider=self.name,
                    price=ask,
                    as_of=observed_at,
                    feed=feed,
                    role="best_ask",
                ),
            ),
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

    async def _stream_quote_batch(self, symbols: Sequence[str]) -> AsyncIterator[Any]:
        exchange_symbols = [self._exchange_symbol(symbol) for symbol in symbols]
        streams = "/".join(f"{symbol.lower()}@trade" for symbol in exchange_symbols)
        session = await self._ensure_session()
        try:
            async with session.ws_connect(
                f"{self.websocket_base_url}/stream?streams={streams}",
                heartbeat=20,
                receive_timeout=60,
                **self._proxy_request_options(),
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

    async def stream_quotes(self, symbols: Sequence[str]) -> AsyncIterator[Any]:
        """Merge a bounded number of combined-stream connections."""

        batches = self.stream_connection_batches(symbols)
        if not batches:
            return
        if len(batches) == 1:
            async for item in self._stream_quote_batch(batches[0]):
                yield item
            return

        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1_024)

        async def consume(batch: tuple[str, ...]) -> None:
            try:
                async for item in self._stream_quote_batch(batch):
                    await queue.put(("quote", item))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(("error", exc))
            else:
                await queue.put(("closed", None))

        tasks = [
            asyncio.create_task(consume(batch), name=f"binance-stream-shard:{index}")
            for index, batch in enumerate(batches)
        ]
        try:
            while True:
                event, value = await queue.get()
                if event == "quote":
                    yield value
                elif event == "error":
                    raise value
                else:
                    # Reconnect the complete shard set together. Otherwise a
                    # normally closed connection could remain silently absent.
                    return
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
