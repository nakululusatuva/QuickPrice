"""Alpaca IEX stock/ETF data and corporate-actions adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import aiohttp

from ._models import date_value, decimal_value, dividend, point, quote, utc_datetime
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderError,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
)


class AlpacaProvider(HttpProvider):
    name = "alpaca"
    data_base_url = "https://data.alpaca.markets/v2"
    corporate_actions_url = "https://data.alpaca.markets/v1/corporate-actions"
    trading_base_url = "https://paper-api.alpaca.markets/v2"
    websocket_url = "wss://stream.data.alpaca.markets/v2/iex"
    feed = "iex"
    symbols: ClassVar[dict[str, str]] = {
        "QQQM:USD": "QQQM",
        "BOXX:USD": "BOXX",
        "SGOV:USD": "SGOV",
    }
    _reverse_symbols: ClassVar[dict[str, str]] = {value: key for key, value in symbols.items()}
    _frequencies: ClassVar[dict[str, str]] = {
        "QQQM:USD": "quarterly",
        "SGOV:USD": "monthly",
    }
    _intervals: ClassVar[dict[str, str]] = {
        "1m": "1Min",
        "5m": "5Min",
        "15m": "15Min",
        "1h": "1Hour",
        "1d": "1Day",
    }

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        trading_base_url: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.api_key = api_key
        self.api_secret = api_secret
        resolved_trading_url = (
            type(self).trading_base_url if trading_base_url is None else trading_base_url
        ).strip()
        if not resolved_trading_url:
            raise ValueError("trading_base_url cannot be empty")
        self.trading_base_url = resolved_trading_url.rstrip("/")
        self._market_status = "unknown"
        self._market_status_observed_at: datetime | None = None
        self._market_status_expires = 0.0
        self._market_status_lock = asyncio.Lock()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

    def _ticker(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        try:
            return self.symbols[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        ticker = self._ticker(normalized)
        payload = await self._request_json(
            "GET",
            f"{self.data_base_url}/stocks/{ticker}/trades/latest",
            params={"feed": self.feed},
            headers=self._headers,
        )
        document = self._document(payload)
        trade = require_mapping(document.get("trade"), self.name, "trade")
        try:
            price = decimal_value(trade["p"])
            as_of = utc_datetime(trade["t"])
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid latest trade") from exc
        market_status, market_status_as_of = await self._get_market_status()
        return quote(
            symbol=normalized,
            price=price,
            as_of=as_of,
            provider=self.name,
            feed=self.feed,
            price_basis="last_trade",
            market_status=market_status,
            is_derived=False,
            components=(),
            fallback_level=0,
            license_scope="personal_internal_no_redistribution",
            coverage="single_venue",
            market_status_as_of=market_status_as_of,
        )

    async def _get_market_status(self) -> tuple[str, datetime | None]:
        now = time.monotonic()
        if now < self._market_status_expires:
            return self._market_status, self._market_status_observed_at
        async with self._market_status_lock:
            now = time.monotonic()
            if now < self._market_status_expires:
                return self._market_status, self._market_status_observed_at
            try:
                payload = await self._request_json(
                    "GET",
                    f"{self.trading_base_url}/clock",
                    headers=self._headers,
                )
                document = self._document(payload)
                is_open = document.get("is_open")
                status = "open" if is_open is True else "closed" if is_open is False else "unknown"
                observed_at = (
                    utc_datetime(document["timestamp"])
                    if document.get("timestamp") is not None
                    else datetime.now(UTC)
                )
            except ProviderError:
                # A clock failure must not discard an otherwise valid IEX trade.
                status = self._market_status
                observed_at = self._market_status_observed_at
            self._market_status = status
            self._market_status_observed_at = observed_at
            self._market_status_expires = now + 30.0
            return status, observed_at

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
        ticker = self._ticker(normalized)
        try:
            timeframe = self._intervals[interval.lower()]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported interval {interval}") from exc
        remaining = max(1, min(limit or 10_000, 10_000))
        page_token: str | None = None
        result = []
        while remaining > 0:
            params = {
                "timeframe": timeframe,
                "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "adjustment": "raw",
                "feed": self.feed,
                "sort": "asc",
                "limit": min(remaining, 10_000),
            }
            if page_token:
                params["page_token"] = page_token
            payload = await self._request_json(
                "GET",
                f"{self.data_base_url}/stocks/{ticker}/bars",
                params=params,
                headers=self._headers,
            )
            document = self._document(payload)
            bars = document.get("bars")
            if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes)):
                raise MalformedResponse(self.name, "bars must be an array")
            for bar in bars:
                if not isinstance(bar, Mapping):
                    raise MalformedResponse(self.name, "invalid bar")
                try:
                    result.append(
                        point(
                            symbol=normalized,
                            timestamp=utc_datetime(bar["t"]),
                            price=decimal_value(bar["c"]),
                            provider=self.name,
                            interval=interval.lower(),
                            is_derived=False,
                        )
                    )
                except (KeyError, ValueError) as exc:
                    raise MalformedResponse(self.name, "invalid bar value") from exc
            remaining -= len(bars)
            page_token_value = document.get("next_page_token")
            page_token = str(page_token_value) if page_token_value else None
            if not page_token or not bars:
                break
        return tuple(result)

    async def get_latest_dividend(self, symbol: str):
        normalized = symbol.strip().upper()
        ticker = self._ticker(normalized)
        if normalized not in self._frequencies:
            raise UnsupportedInstrument(self.name, f"no dividend policy for {normalized}")
        today = datetime.now(UTC).date()
        payload = await self._request_json(
            "GET",
            self.corporate_actions_url,
            params={
                "symbols": ticker,
                "types": "cash_dividend",
                "start": (today - timedelta(days=550)).isoformat(),
                "end": today.isoformat(),
                "sort": "desc",
                "limit": 1000,
            },
            headers=self._headers,
        )
        document = self._document(payload)
        rows = document.get("cash_dividends", ())
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise MalformedResponse(self.name, "cash_dividends must be an array")
        regular = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            # The current market-data API supplies an explicit ``special``
            # flag. Missing classification is rejected rather than treating a
            # capital-gain/special distribution as ordinary income.
            if row.get("special") is not False or row.get("foreign") is True:
                continue
            subtype = str(row.get("sub_type") or "").strip().lower()
            allowed_subtypes = {"", "interest"} if normalized == "SGOV:USD" else {""}
            if subtype not in allowed_subtypes:
                # ``return_of_capital`` must never be presented as a regular
                # dividend. Unknown future subtypes also fail closed.
                continue
            if str(row.get("symbol", ticker)).upper() != ticker:
                continue
            try:
                ex_date = date_value(row.get("ex_date"))
                amount = decimal_value(row.get("rate"))
            except ValueError:
                continue
            if ex_date is None or ex_date > today or amount <= 0:
                continue
            regular.append((ex_date, row, amount))
        if not regular:
            return None
        ex_date, row, amount = max(regular, key=lambda item: item[0])
        return dividend(
            symbol=normalized,
            ex_date=ex_date,
            payment_date=date_value(row.get("payable_date")),
            amount=amount,
            currency="USD",
            frequency=self._frequencies[normalized],
            provider=self.name,
            event_type="regular_cash",
            declared_date=None,
        )

    def _document(self, payload: Any) -> Mapping[str, Any]:
        document = require_mapping(payload, self.name)
        if (
            "code" in document
            and "message" in document
            and "trade" not in document
            and "bars" not in document
        ):
            raise ProviderUnavailable(self.name, "upstream returned an error")
        return document

    async def stream_quotes(self, symbols: Sequence[str]) -> AsyncIterator[Any]:
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        tickers = [self._ticker(symbol) for symbol in normalized]
        session = await self._ensure_session()
        try:
            async with session.ws_connect(
                self.websocket_url, heartbeat=20, receive_timeout=60
            ) as websocket:
                await websocket.send_json(
                    {"action": "auth", "key": self.api_key, "secret": self.api_secret}
                )
                await websocket.send_json({"action": "subscribe", "trades": tickers})
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
                    rows = payload if isinstance(payload, Sequence) else (payload,)
                    for row in rows:
                        if not isinstance(row, Mapping):
                            continue
                        if row.get("T") == "error":
                            raise ProviderUnavailable(
                                self.name, "stream authentication/subscription failed"
                            )
                        if row.get("T") != "t":
                            continue
                        output_symbol = self._reverse_symbols.get(str(row.get("S", "")).upper())
                        if output_symbol is None:
                            continue
                        try:
                            yield quote(
                                symbol=output_symbol,
                                price=decimal_value(row["p"]),
                                as_of=utc_datetime(row["t"]),
                                provider=self.name,
                                feed=self.feed,
                                price_basis="last_trade",
                                # A trade can arrive outside the regular session,
                                # so it is not an independent market-clock signal.
                                market_status="unknown",
                                is_derived=False,
                                components=(),
                                fallback_level=0,
                                license_scope="personal_internal_no_redistribution",
                                coverage="single_venue",
                                market_status_as_of=None,
                            )
                        except KeyError, ValueError:
                            continue
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise ProviderUnavailable(self.name, type(exc).__name__) from None
