"""OKX public spot-market and staking-rate adapters."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from quickprice.domain import (
    RewardAccrualMode,
    SourceComponent,
    YieldMetric,
    YieldQuality,
    YieldRateType,
    ensure_utc,
)

from ._models import component, decimal_value, point, quote, utc_datetime
from .base import (
    HttpProvider,
    MalformedResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
    require_sequence,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _okx_data(payload: Any, provider: str, context: str) -> Sequence[Any]:
    document = require_mapping(payload, provider)
    code = str(document.get("code", ""))
    if code in {"50011", "50040"}:
        raise ProviderRateLimited(provider, "upstream quota exceeded")
    if code != "0":
        raise ProviderUnavailable(provider, "upstream returned an error")
    return require_sequence(document.get("data"), provider, context)


class OkxMarketProvider(HttpProvider):
    """Read current OKX spot books and historical candles for configured markets."""

    name = "okx"
    base_url = "https://www.okx.com"
    feed = "okx_spot_ticker"
    history_feed = "okx_spot_candles"
    page_size = 100
    maximum_pages = 50
    minimum_quote_poll_seconds = 1.0

    _canonical_markets: ClassVar[dict[str, str]] = {}
    _internal_aliases: ClassVar[dict[str, str]] = {}
    _intervals: ClassVar[dict[str, str]] = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "1h": "1H",
        "4h": "4H",
        "1d": "1Dutc",
    }

    def __init__(
        self,
        *args: Any,
        market_bindings: Mapping[str, str] | None = None,
        internal_aliases: Mapping[str, str] | None = None,
        wall_clock: Callable[[], datetime] = _utc_now,
        monotonic_clock: Callable[[], float] = time.monotonic,
        minimum_request_interval_seconds: float = 0.125,
        maximum_relative_book_spread: Decimal = Decimal("0.005"),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._canonical_markets = {
            symbol.strip().upper(): instrument_id.strip().upper()
            for symbol, instrument_id in (market_bindings or {}).items()
        }
        self._internal_aliases = {
            alias.strip().upper(): canonical.strip().upper()
            for alias, canonical in (internal_aliases or {}).items()
        }
        if any(
            canonical not in self._canonical_markets
            for canonical in self._internal_aliases.values()
        ):
            raise ValueError("OKX internal alias references an unknown market binding")
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self.minimum_request_interval_seconds = float(minimum_request_interval_seconds)
        self.maximum_relative_book_spread = decimal_value(maximum_relative_book_spread)
        if self.minimum_request_interval_seconds < 0:
            raise ValueError("minimum_request_interval_seconds cannot be negative")
        if self.maximum_relative_book_spread <= 0:
            raise ValueError("maximum_relative_book_spread must be positive")
        self._request_gate = asyncio.Lock()
        self._next_request_at = 0.0

    def _market(self, symbol: str) -> tuple[str, str]:
        normalized = symbol.strip().upper()
        canonical = self._internal_aliases.get(normalized, normalized)
        try:
            return canonical, self._canonical_markets[canonical]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc

    async def _public_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any],
    ) -> Any:
        async with self._request_gate:
            delay = self._next_request_at - self._monotonic_clock()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = self._monotonic_clock() + self.minimum_request_interval_seconds
        return await self._request_json(
            "GET",
            f"{self.base_url}{path}",
            params=params,
        )

    async def get_quote(self, symbol: str):
        canonical, instrument_id = self._market(symbol)
        payload = await self._public_json(
            "/api/v5/market/ticker",
            params={"instId": instrument_id},
        )
        rows = _okx_data(payload, self.name, "ticker data")
        if len(rows) != 1:
            raise ProviderUnavailable(self.name, "ticker is unavailable")
        row = require_mapping(rows[0], self.name, "ticker")
        if str(row.get("instId", "")).upper() != instrument_id:
            raise MalformedResponse(self.name, "unexpected ticker instrument")
        try:
            bid = decimal_value(row["bidPx"])
            ask = decimal_value(row["askPx"])
            utc_datetime(row["ts"], milliseconds=True)
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid ticker") from exc
        if bid <= 0 or ask <= 0 or ask < bid:
            raise MalformedResponse(self.name, "invalid book spread")
        midpoint = (bid + ask) / 2
        if (ask - bid) / midpoint > self.maximum_relative_book_spread:
            raise ProviderUnavailable(self.name, "book spread exceeds safety limit")
        observed_at = ensure_utc(self._wall_clock())
        return quote(
            symbol=canonical,
            price=midpoint,
            as_of=observed_at,
            provider=self.name,
            feed=self.feed,
            price_basis="midpoint",
            market_status="open",
            is_derived=True,
            components=(
                component(
                    symbol=canonical,
                    provider=self.name,
                    price=bid,
                    as_of=observed_at,
                    feed=self.feed,
                    role="best_bid",
                ),
                component(
                    symbol=canonical,
                    provider=self.name,
                    price=ask,
                    as_of=observed_at,
                    feed=self.feed,
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
        canonical, instrument_id = self._market(symbol)
        normalized_interval = interval.strip().lower()
        try:
            okx_interval = self._intervals[normalized_interval]
        except KeyError as exc:
            raise UnsupportedInstrument(
                self.name, f"unsupported interval {normalized_interval}"
            ) from exc
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        if start_utc >= end_utc:
            raise ValueError("history start must be before end")
        requested_limit = max(1, min(limit or 300, self.page_size * self.maximum_pages))
        cursor_after = int(end_utc.timestamp() * 1000) + 1
        before = int(start_utc.timestamp() * 1000) - 1
        points_by_timestamp: dict[datetime, Any] = {}

        for _ in range(
            min(self.maximum_pages, (requested_limit + self.page_size - 1) // self.page_size)
        ):
            remaining = requested_limit - len(points_by_timestamp)
            if remaining <= 0:
                break
            page_limit = min(self.page_size, remaining)
            payload = await self._public_json(
                "/api/v5/market/history-candles",
                params={
                    "instId": instrument_id,
                    "bar": okx_interval,
                    "after": str(cursor_after),
                    "before": str(before),
                    "limit": str(page_limit),
                },
            )
            rows = _okx_data(payload, self.name, "candle data")
            if not rows:
                break
            oldest_timestamp: datetime | None = None
            for item in rows:
                if (
                    not isinstance(item, Sequence)
                    or isinstance(item, (str, bytes))
                    or len(item) < 9
                ):
                    raise MalformedResponse(self.name, "invalid candle")
                try:
                    timestamp = utc_datetime(item[0], milliseconds=True)
                    close = decimal_value(item[4])
                    int(str(item[8]))
                except (ValueError, TypeError) as exc:
                    raise MalformedResponse(self.name, "invalid candle value") from exc
                oldest_timestamp = (
                    timestamp if oldest_timestamp is None else min(oldest_timestamp, timestamp)
                )
                if start_utc <= timestamp <= end_utc:
                    if close <= 0:
                        raise MalformedResponse(self.name, "candle close must be positive")
                    points_by_timestamp[timestamp] = point(
                        symbol=canonical,
                        timestamp=timestamp,
                        price=close,
                        provider=self.name,
                        interval=normalized_interval,
                        is_derived=False,
                    )
            if oldest_timestamp is None or oldest_timestamp <= start_utc or len(rows) < page_limit:
                break
            next_after = int(oldest_timestamp.timestamp() * 1000)
            if next_after >= cursor_after:
                raise MalformedResponse(self.name, "candle pagination did not advance")
            cursor_after = next_after

        ordered = [points_by_timestamp[key] for key in sorted(points_by_timestamp)]
        if limit is not None:
            ordered = ordered[-max(0, limit) :]
        return tuple(ordered)


class OkxBethYieldProvider(HttpProvider):
    """Read OKX's public, provider-reported annualized staking rate."""

    name = "okx_beth_yield"
    base_url = "https://www.okx.com"
    path = "/api/v5/finance/staking-defi/eth/apy-history"

    def __init__(
        self,
        *args: Any,
        yield_policies: Mapping[str, Mapping[str, Any]] | None = None,
        days: int = 30,
        clock: Callable[[], datetime] = _utc_now,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        self.yield_policies = {
            symbol.strip().upper(): dict(policy)
            for symbol, policy in (yield_policies or {}).items()
        }
        self.days = days
        self._clock = clock

    async def get_yield(self, symbol: str) -> YieldMetric:
        normalized = symbol.strip().upper()
        try:
            policy = self.yield_policies[normalized]
            component_symbol = str(policy["component_symbol"]).strip().upper()
            underlying_asset = str(policy["underlying_asset"]).strip().upper()
            accrual_mode = RewardAccrualMode(policy["accrual_mode"])
            method = str(policy["method"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise UnsupportedInstrument(
                self.name,
                f"unsupported yield symbol {normalized}",
            ) from exc
        payload = await self._request_json(
            "GET",
            f"{self.base_url}{self.path}",
            params={"days": str(self.days)},
        )
        rows = _okx_data(payload, self.name, "staking-rate data")
        if not rows:
            raise ProviderUnavailable(self.name, "staking-rate history is empty")
        observations: list[tuple[datetime, Decimal]] = []
        for item in rows:
            row = require_mapping(item, self.name, "staking-rate observation")
            try:
                as_of = utc_datetime(row["ts"], milliseconds=True)
                rate_fraction = decimal_value(row["rate"])
            except (KeyError, ValueError) as exc:
                raise MalformedResponse(self.name, "invalid staking-rate observation") from exc
            if rate_fraction < 0:
                raise MalformedResponse(self.name, "staking rate cannot be negative")
            observations.append((as_of, rate_fraction))
        as_of, rate_fraction = max(observations, key=lambda item: item[0])
        now = ensure_utc(self._clock())
        staleness_ms = max(0, int((now - as_of).total_seconds() * 1000))
        return YieldMetric(
            symbol=normalized,
            # OKX encodes the already annualized rate as a fraction.
            value=rate_fraction * Decimal(100),
            as_of=as_of,
            method=method,
            provider=self.name,
            is_proxy=False,
            components=(
                SourceComponent(
                    symbol=component_symbol,
                    provider=self.name,
                    price=rate_fraction,
                    as_of=as_of,
                    feed="okx_eth_staking",
                    role="provider_reported_apr_fraction",
                ),
            ),
            rate_type=YieldRateType.APR,
            accrual_mode=accrual_mode,
            underlying_asset=underlying_asset,
            is_estimate=False,
            quality=YieldQuality(
                stale=staleness_ms > 2 * 24 * 60 * 60 * 1000,
                staleness_ms=staleness_ms,
                confidence="high",
            ),
            fallback_level=0,
        )


__all__ = ["OkxBethYieldProvider", "OkxMarketProvider"]
