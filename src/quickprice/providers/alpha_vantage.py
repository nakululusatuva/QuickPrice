"""Alpha Vantage low-frequency emergency and dividend adapter."""

from __future__ import annotations

import asyncio
import time as monotonic_time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, time
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from quickprice.market import seconds_until_next_us_equity_open

from ._models import date_value, decimal_value, dividend, point, quote, utc_datetime
from ._ttl import AsyncTtlCache
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
)
from .quota import QuotaBudget, daily_budget

ALPHA_VANTAGE_FX_QUOTE_TTL_SECONDS = 21_600.0
ALPHA_VANTAGE_DEFAULT_FX_QUOTE_RESERVE_CREDITS = 20


def alpha_vantage_quota_budget(daily_credits: int, fx_symbol_count: int) -> QuotaBudget:
    refreshes_per_day = ceil(86_400 / ALPHA_VANTAGE_FX_QUOTE_TTL_SECONDS)
    desired_reserve = refreshes_per_day * max(0, fx_symbol_count)
    reserve = min(desired_reserve, max(0, daily_credits - 1))
    return daily_budget(daily_credits, reserve=reserve)


class AlphaVantageProvider(HttpProvider):
    name = "alpha_vantage"
    base_url = "https://www.alphavantage.co/query"
    feed = "alpha_vantage_eod"
    history_prefix_limited = True
    _new_york = ZoneInfo("America/New_York")

    def __init__(
        self,
        api_key: str,
        *,
        equity_symbol_bindings: Mapping[str, str] | None = None,
        fx_symbol_bindings: Mapping[str, str | tuple[str, str]] | None = None,
        dividend_frequencies: Mapping[str, str] | None = None,
        fx_quote_ttl_seconds: float = ALPHA_VANTAGE_FX_QUOTE_TTL_SECONDS,
        quote_cache_clock: Callable[[], float] = monotonic_time.monotonic,
        request_clock: Callable[[], float] = monotonic_time.monotonic,
        request_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        minimum_request_interval_seconds: float = 12.5,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        **kwargs,
    ):
        raw_fx_symbols = fx_symbol_bindings or {}
        kwargs.setdefault("quota", alpha_vantage_quota_budget(25, len(raw_fx_symbols)))
        super().__init__(**kwargs)
        self.api_key = api_key
        self.equity_symbols = {
            symbol.strip().upper(): ticker.strip().upper()
            for symbol, ticker in (equity_symbol_bindings or {}).items()
        }
        self.fx_symbols = {}
        for raw_symbol, raw_pair in raw_fx_symbols.items():
            symbol = raw_symbol.strip().upper()
            if isinstance(raw_pair, str):
                parts = tuple(
                    part.strip().upper() for part in raw_pair.replace(":", "/").split("/")
                )
            else:
                parts = tuple(part.strip().upper() for part in raw_pair)
            if len(parts) != 2:
                raise ValueError("Alpha Vantage FX binding must contain one currency pair")
            self.fx_symbols[symbol] = (parts[0], parts[1])
        self.dividend_frequencies = {
            symbol.strip().upper(): frequency.strip().lower()
            for symbol, frequency in (dividend_frequencies or {}).items()
            if symbol.strip().upper() in self.equity_symbols
        }
        self.fx_quote_ttl_seconds = max(
            ALPHA_VANTAGE_FX_QUOTE_TTL_SECONDS,
            fx_quote_ttl_seconds,
        )
        self._quote_cache = AsyncTtlCache[str, Any](clock=quote_cache_clock)
        self._wall_clock = wall_clock
        if minimum_request_interval_seconds < 0:
            raise ValueError("minimum_request_interval_seconds cannot be negative")
        self.minimum_request_interval_seconds = float(minimum_request_interval_seconds)
        self._request_clock = request_clock
        self._request_sleeper = request_sleeper
        self._next_request_at = 0.0
        self._request_lock = asyncio.Lock()
        self.routing_timeout_seconds = max(
            90.0,
            len(self.fx_symbols) * self.minimum_request_interval_seconds
            + self.request_timeout
            + 1.0,
        )

    async def _request_json(self, *args, **kwargs):
        # Alpha's free endpoint is sensitive to concurrent bursts. Keep one
        # in-flight request per key so the five emergency FX spokes do not
        # rate-limit one another during startup.
        quota_cost = int(kwargs.get("quota_cost", 1))
        allow_quota_reserve = bool(kwargs.get("allow_quota_reserve", False))
        if self.quota is not None and not await self.quota.can_acquire(
            quota_cost,
            allow_reserve=allow_quota_reserve,
        ):
            raise ProviderRateLimited(self.name, "local quota exhausted")
        async with self._request_lock:
            # Recheck after admission to the pacing queue. Concurrent callers
            # may have consumed the last eligible credit while this task was
            # waiting for the lock.
            if self.quota is not None and not await self.quota.can_acquire(
                quota_cost,
                allow_reserve=allow_quota_reserve,
            ):
                raise ProviderRateLimited(self.name, "local quota exhausted")
            delay = self._next_request_at - self._request_clock()
            if delay > 0:
                await self._request_sleeper(delay)
            self._next_request_at = self._request_clock() + self.minimum_request_interval_seconds
            return await super()._request_json(*args, **kwargs)

    def _document(self, payload: Any) -> Mapping[str, Any]:
        document = require_mapping(payload, self.name)
        if "Note" in document or "Information" in document:
            raise ProviderRateLimited(self.name, "upstream quota exceeded")
        if "Error Message" in document:
            raise ProviderUnavailable(self.name, "upstream returned an error")
        return document

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        if normalized in self.fx_symbols:
            return await self._quote_cache.get_or_load(
                normalized,
                self.fx_quote_ttl_seconds,
                lambda: self._get_fx_quote(normalized),
            )
        if normalized in self.equity_symbols:
            return await self._quote_cache.get_or_load(
                normalized,
                seconds_until_next_us_equity_open(self._wall_clock()),
                lambda: self._get_equity_quote(normalized),
            )
        raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}")

    async def _get_fx_quote(self, symbol: str):
        base, counter = self.fx_symbols[symbol]
        payload = await self._request_json(
            "GET",
            self.base_url,
            params={
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": base,
                "to_currency": counter,
                "apikey": self.api_key,
            },
            allow_quota_reserve=True,
        )
        document = self._document(payload)
        row = require_mapping(
            document.get("Realtime Currency Exchange Rate"), self.name, "exchange rate"
        )
        try:
            price = decimal_value(row["5. Exchange Rate"])
            as_of = utc_datetime(row["6. Last Refreshed"])
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid exchange rate") from exc
        return quote(
            symbol=symbol,
            price=price,
            as_of=as_of,
            provider=self.name,
            feed="alpha_vantage_fx",
            price_basis="exchange_rate",
            market_status="unknown",
            is_derived=False,
            components=(),
            fallback_level=0,
            license_scope="personal_internal",
            coverage="vendor_aggregate",
        )

    async def _get_equity_quote(self, symbol: str):
        ticker = self.equity_symbols[symbol]
        payload = await self._request_json(
            "GET",
            self.base_url,
            params={"function": "GLOBAL_QUOTE", "symbol": ticker, "apikey": self.api_key},
        )
        document = self._document(payload)
        row = require_mapping(document.get("Global Quote"), self.name, "global quote")
        try:
            price = decimal_value(row["05. price"])
            as_of = self._equity_close_timestamp(
                row["07. latest trading day"],
                not_after=self._wall_clock(),
            )
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid global quote") from exc
        return quote(
            symbol=symbol,
            price=price,
            as_of=as_of,
            provider=self.name,
            feed=self.feed,
            price_basis="end_of_day",
            market_status="closed",
            is_derived=False,
            components=(),
            fallback_level=0,
            license_scope="personal_internal",
            coverage="end_of_day",
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
        if interval.lower() != "1d":
            raise UnsupportedInstrument(self.name, "free fallback provides daily history only")
        normalized = symbol.strip().upper()
        if normalized in self.fx_symbols:
            base, counter = self.fx_symbols[normalized]
            params = {
                "function": "FX_DAILY",
                "from_symbol": base,
                "to_symbol": counter,
                "outputsize": "compact",
                "apikey": self.api_key,
            }
            series_key = "Time Series FX (Daily)"
        elif normalized in self.equity_symbols:
            params = {
                "function": "TIME_SERIES_DAILY",
                "symbol": self.equity_symbols[normalized],
                "outputsize": "compact",
                "apikey": self.api_key,
            }
            series_key = "Time Series (Daily)"
        else:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}")
        payload = await self._request_json("GET", self.base_url, params=params)
        document = self._document(payload)
        series = require_mapping(document.get(series_key), self.name, "daily series")
        result = []
        start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
        for date_text, values in series.items():
            if not isinstance(values, Mapping):
                raise MalformedResponse(self.name, "invalid daily series row")
            try:
                timestamp = (
                    utc_datetime(date_text)
                    if normalized in self.fx_symbols
                    else self._equity_close_timestamp(
                        date_text,
                        not_after=self._wall_clock(),
                    )
                )
                if timestamp < start_utc or timestamp > end_utc:
                    continue
                result.append(
                    point(
                        symbol=normalized,
                        timestamp=timestamp,
                        price=decimal_value(values["4. close"]),
                        provider=self.name,
                        interval="1d",
                        is_derived=False,
                    )
                )
            except (KeyError, ValueError) as exc:
                raise MalformedResponse(self.name, "invalid daily series value") from exc
        result.sort(key=lambda item: item.timestamp)
        if limit is not None:
            result = result[-max(0, limit) :]
        return tuple(result)

    @classmethod
    def _equity_close_timestamp(
        cls,
        value: Any,
        *,
        not_after: datetime | None = None,
    ) -> datetime:
        """Map Alpha's date-only daily value to the regular US close.

        Alpha Vantage does not include the actual close time. On an early-close
        day, QuickPrice therefore waits until the regular 16:00 New York close
        instead of inventing a precise early-close timestamp or returning a
        timestamp in the future.
        """

        trading_date = datetime.fromisoformat(str(value)[:10]).date()
        timestamp = datetime.combine(trading_date, time(16), tzinfo=cls._new_york).astimezone(UTC)
        if not_after is not None:
            if not_after.tzinfo is None:
                raise ValueError("not_after must be timezone-aware")
            if timestamp > not_after.astimezone(UTC):
                raise ProviderUnavailable(
                    cls.name,
                    "date-only daily close cannot be timestamped safely before regular close",
                )
        return timestamp

    async def get_latest_dividend(self, symbol: str):
        normalized = symbol.strip().upper()
        if normalized not in self.dividend_frequencies:
            raise UnsupportedInstrument(self.name, f"no dividend policy for {normalized}")
        ticker = self.equity_symbols[normalized]
        payload = await self._request_json(
            "GET",
            self.base_url,
            params={"function": "DIVIDENDS", "symbol": ticker, "apikey": self.api_key},
        )
        document = self._document(payload)
        rows = document.get("data")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise MalformedResponse(self.name, "dividend data must be an array")
        today = datetime.now(UTC).date()
        regular = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            raw_type = row.get("dividend_type", row.get("type"))
            distribution_type = str(raw_type).strip().lower().replace(" ", "_")
            if distribution_type not in {
                "regular",
                "regular_cash",
                "ordinary",
                "ordinary_cash",
                "cash",
                "cash_dividend",
            }:
                continue
            try:
                ex_date = date_value(row.get("ex_dividend_date"))
                amount = decimal_value(row.get("amount"))
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
            payment_date=date_value(row.get("payment_date")),
            amount=amount,
            currency="USD",
            frequency=self.dividend_frequencies[normalized],
            provider=self.name,
            event_type="regular_cash",
            declared_date=date_value(row.get("declaration_date")),
        )
