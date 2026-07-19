"""CoinGecko Demo adapter used only as a slow crypto fallback."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from datetime import time as datetime_time
from decimal import Decimal
from typing import ClassVar

from ._models import component, decimal_value, point, quote, utc_datetime
from ._ttl import AsyncTtlCache
from .base import (
    HttpProvider,
    MalformedResponse,
    NetworkUnavailable,
    ProviderError,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
    require_sequence,
)
from .quota import QuotaBudget, rolling_month_safe_daily_budget

COINGECKO_SHARED_QUOTE_CACHE_SECONDS = 600.0
COINGECKO_MAXIMUM_QUOTE_ERROR_CACHE_SECONDS = COINGECKO_SHARED_QUOTE_CACHE_SECONDS
COINGECKO_NETWORK_ERROR_RETRY_SECONDS = 300.0
COINGECKO_DAILY_QUOTE_RESERVE_CREDITS = math.ceil(86_400 / COINGECKO_SHARED_QUOTE_CACHE_SECONDS) + 1


def coingecko_quota_budget(monthly_credits: int) -> QuotaBudget:
    """Reserve enough of the safe daily slice for the shared price batch."""

    daily_limit = monthly_credits // 31
    reserve = min(COINGECKO_DAILY_QUOTE_RESERVE_CREDITS, max(0, daily_limit - 1))
    return rolling_month_safe_daily_budget(monthly_credits, reserve=reserve)


# CoinGecko documents comma-separated IDs but no hard ID-count ceiling for
# /simple/price. These conservative transport bounds keep both the number of
# IDs and the encoded query component comfortably finite across proxies.
COINGECKO_SIMPLE_PRICE_IDS_PER_REQUEST = 250
COINGECKO_SIMPLE_PRICE_ID_CHARACTERS_PER_REQUEST = 3_500


def coingecko_simple_price_id_batches(
    coin_ids: Sequence[str],
) -> tuple[tuple[str, ...], ...]:
    """Partition unique IDs by count and joined-query character length."""

    normalized = tuple(
        sorted({str(coin_id).strip() for coin_id in coin_ids if str(coin_id).strip()})
    )
    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    current_characters = 0
    for coin_id in normalized:
        if len(coin_id) > COINGECKO_SIMPLE_PRICE_ID_CHARACTERS_PER_REQUEST:
            raise ValueError("CoinGecko coin ID exceeds the safe query length")
        added_characters = len(coin_id) + (1 if current else 0)
        if current and (
            len(current) >= COINGECKO_SIMPLE_PRICE_IDS_PER_REQUEST
            or current_characters + added_characters
            > COINGECKO_SIMPLE_PRICE_ID_CHARACTERS_PER_REQUEST
        ):
            batches.append(tuple(current))
            current = []
            current_characters = 0
            added_characters = len(coin_id)
        current.append(coin_id)
        current_characters += added_characters
    if current:
        batches.append(tuple(current))
    return tuple(batches)


class CoinGeckoProvider(HttpProvider):
    name = "coingecko"
    base_url = "https://api.coingecko.com/api/v3"
    feed = "coingecko_aggregated"
    # Successful dated snapshots are immutable and reusable across adjacent
    # collection cycles. Transient failures must expire well before the
    # collector's 24-hour incomplete-prefix retry.
    daily_snapshot_success_ttl_seconds: ClassVar[float] = 26 * 60 * 60
    daily_snapshot_error_ttl_seconds: ClassVar[float] = 15 * 60
    # The Demo plan exposes at most the trailing 365 days. Clamp every
    # individual request at the adapter boundary so a generic 400-day local
    # retention policy cannot turn an otherwise useful one-year backfill into
    # an HTTP 401 response.
    maximum_history_lookback: ClassVar[timedelta] = timedelta(days=365)
    coin_ids: ClassVar[dict[str, str]] = {}
    history_symbols: ClassVar[frozenset[str]] = frozenset()
    component_skew_limits: ClassVar[dict[str, timedelta]] = {}

    def __init__(
        self,
        api_key: str | None = None,
        *,
        coin_ids: Mapping[str, str] | None = None,
        history_symbols: Sequence[str] | None = None,
        component_skew_limits: Mapping[str, timedelta] | None = None,
        normalization_quote_asset: str | None = None,
        normalization_coin_id: str | None = None,
        normalization_component_symbol: str | None = None,
        cache_ttl_seconds: float = COINGECKO_SHARED_QUOTE_CACHE_SECONDS,
        maximum_error_cache_ttl_seconds: float = COINGECKO_MAXIMUM_QUOTE_ERROR_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        kwargs.setdefault("quota", coingecko_quota_budget(9_000))
        super().__init__(**kwargs)
        self.api_key = api_key
        self.coin_ids = {key.strip().upper(): value for key, value in (coin_ids or {}).items()}
        configured_history_symbols = history_symbols or ()
        self.history_symbols = frozenset(
            symbol.strip().upper()
            for symbol in configured_history_symbols
            if symbol.strip().upper() in self.coin_ids
        )
        self.component_skew_limits = {
            symbol.strip().upper(): value
            for symbol, value in (component_skew_limits or {}).items()
            if symbol.strip().upper() in self.coin_ids
        }
        self.normalization_quote_asset = (
            None if normalization_quote_asset is None else normalization_quote_asset.strip().upper()
        )
        self.normalization_coin_id = (
            None if normalization_coin_id is None else normalization_coin_id.strip().lower()
        )
        self.normalization_component_symbol = (
            None
            if normalization_component_symbol is None
            else normalization_component_symbol.strip().upper()
        )
        normalization_values = (
            self.normalization_quote_asset,
            self.normalization_coin_id,
            self.normalization_component_symbol,
        )
        if any(normalization_values) and not all(normalization_values):
            raise ValueError("CoinGecko quote normalization policy is incomplete")
        self._cache_ttl_seconds = max(1.0, cache_ttl_seconds)
        self._maximum_error_cache_ttl_seconds = max(
            self._cache_ttl_seconds,
            maximum_error_cache_ttl_seconds,
        )
        self._clock = clock
        self._refresh_lock = asyncio.Lock()
        self._price_cache: Mapping[str, object] | None = None
        self._cache_expires_at = 0.0
        self._refresh_error: str | None = None
        self._refresh_error_is_network = False
        self._consecutive_refresh_failures = 0
        self._daily_snapshot_cache = AsyncTtlCache[tuple[str, str], Decimal](clock=clock)

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-cg-demo-api-key": self.api_key} if self.api_key else {}

    def _coin(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        try:
            return self.coin_ids[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc

    def quote_failure_retry_after_seconds(self) -> float | None:
        """Return the remaining shared negative-cache delay, when active."""

        if self._price_cache is not None or self._refresh_error is None:
            return None
        return max(0.0, self._cache_expires_at - self._clock())

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        quote_asset = normalized.partition(":")[2]
        if quote_asset != "USD" and quote_asset != self.normalization_quote_asset:
            raise UnsupportedInstrument(
                self.name,
                f"unsupported quote asset {quote_asset}",
            )
        coin_id = self._coin(normalized)
        document = await self._simple_prices()
        try:
            asset = require_mapping(document[coin_id], self.name, coin_id)
            asset_price = decimal_value(asset["usd"])
            asset_time = utc_datetime(asset["last_updated_at"])
            if quote_asset == self.normalization_quote_asset:
                normalization = require_mapping(
                    document[self.normalization_coin_id],
                    self.name,
                    self.normalization_coin_id,
                )
                normalization_price = decimal_value(normalization["usd"])
                normalization_time = utc_datetime(normalization["last_updated_at"])
                skew_limit = self.component_skew_limits.get(normalized)
                if skew_limit is not None and abs(asset_time - normalization_time) > skew_limit:
                    raise MalformedResponse(
                        self.name,
                        "component timestamps exceed the configured normalization limit",
                    )
                result_price = asset_price / normalization_price
            else:
                result_price = asset_price
                normalization_price = None
                normalization_time = None
        except MalformedResponse:
            raise
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            raise MalformedResponse(self.name, "invalid simple-price response") from exc
        components = (
            (
                component(
                    symbol=f"{normalized.split(':', 1)[0]}:USD",
                    provider=self.name,
                    price=asset_price,
                    as_of=asset_time,
                    feed=self.feed,
                    role="numerator",
                ),
                component(
                    symbol=self.normalization_component_symbol,
                    provider=self.name,
                    price=normalization_price,
                    as_of=normalization_time,
                    feed=self.feed,
                    role="denominator",
                ),
            )
            if normalization_price is not None and normalization_time is not None
            else ()
        )
        return quote(
            symbol=normalized,
            price=result_price,
            as_of=(
                min(asset_time, normalization_time)
                if normalization_time is not None
                else asset_time
            ),
            provider=self.name,
            feed=self.feed,
            price_basis=(
                "aggregated_spot_ratio"
                if quote_asset == self.normalization_quote_asset
                else "aggregated_spot"
            ),
            market_status="open",
            is_derived=quote_asset == self.normalization_quote_asset,
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
            if self._refresh_error is not None:
                error_type = (
                    NetworkUnavailable if self._refresh_error_is_network else ProviderUnavailable
                )
                raise error_type(self.name, self._refresh_error)
            # A refresh is in flight. Wait for its lock instead of treating
            # every concurrent symbol in the shared batch as a failed poll.

        async with self._refresh_lock:
            now = self._clock()
            if now < self._cache_expires_at:
                if self._price_cache is not None:
                    return self._price_cache
                error_type = (
                    NetworkUnavailable if self._refresh_error_is_network else ProviderUnavailable
                )
                raise error_type(
                    self.name,
                    self._refresh_error or "simple-price refresh is in backoff",
                )

            # Advance the deadline before I/O. A failed refresh is negative-cached,
            # so concurrent symbol requests cannot consume one credit each.
            self._cache_expires_at = now + self._cache_ttl_seconds
            self._price_cache = None
            self._refresh_error = None
            self._refresh_error_is_network = False
            ids = list(self.coin_ids.values())
            if self.normalization_coin_id is not None:
                ids.append(self.normalization_coin_id)
            id_batches = coingecko_simple_price_id_batches(ids)
            try:
                merged: dict[str, object] = {}
                for ids in id_batches:
                    payload = await self._request_json(
                        "GET",
                        f"{self.base_url}/simple/price",
                        params={
                            "ids": ",".join(ids),
                            "vs_currencies": "usd",
                            "include_last_updated_at": "true",
                        },
                        headers=self._headers,
                        allow_quota_reserve=True,
                    )
                    document = require_mapping(payload, self.name)
                    merged.update(document)
            except Exception as exc:
                network_failure = isinstance(exc, NetworkUnavailable)
                if network_failure:
                    # Connection, TLS, DNS, proxy, and timeout failures are retried
                    # forever at a bounded cadence. They must not inherit a long
                    # exponential delay intended for valid upstream responses.
                    error_ttl = COINGECKO_NETWORK_ERROR_RETRY_SECONDS
                else:
                    self._consecutive_refresh_failures += 1
                    exponent = min(self._consecutive_refresh_failures - 1, 10)
                    error_ttl = min(
                        self._maximum_error_cache_ttl_seconds,
                        self._cache_ttl_seconds * (2**exponent),
                    )
                self._cache_expires_at = self._clock() + error_ttl
                self._refresh_error = str(exc) or type(exc).__name__
                self._refresh_error_is_network = network_failure
                raise
            self._price_cache = merged
            self._refresh_error = None
            self._refresh_error_is_network = False
            self._consecutive_refresh_failures = 0
            return merged

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
        quote_asset = normalized.partition(":")[2]
        if quote_asset != "USD" and quote_asset != self.normalization_quote_asset:
            raise UnsupportedInstrument(
                self.name,
                f"unsupported quote asset {quote_asset}",
            )
        if normalized not in self.history_symbols:
            raise UnsupportedInstrument(
                self.name,
                "CoinGecko history is enabled only for configured liquid-staking assets",
            )
        normalized_interval = interval.strip().lower()
        if normalized_interval not in {"1m", "5m", "1d"}:
            raise UnsupportedInstrument(
                self.name,
                f"unsupported interval {normalized_interval}",
            )
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        if start_utc >= end_utc:
            raise ValueError("history start must be before end")
        query_start = max(start_utc, end_utc - self.maximum_history_lookback)

        coin_id = self._coin(normalized)
        params: dict[str, str | int] = {
            "vs_currency": "usd",
            "from": int(query_start.timestamp()),
            "to": int(end_utc.timestamp()),
            "precision": "full",
        }
        # Daily granularity is public. Intraday requests deliberately use
        # automatic granularity because five-minute data is plan-dependent.
        if normalized_interval == "1d":
            params["interval"] = "daily"
        payload = await self._request_json(
            "GET",
            f"{self.base_url}/coins/{coin_id}/market_chart/range",
            params=params,
            headers=self._headers,
        )
        document = require_mapping(payload, self.name)
        prices = require_sequence(document.get("prices"), self.name, "historical prices")
        quoted_in_usdc = quote_asset == self.normalization_quote_asset
        current_usdc_usd = (
            await self._history_normalization_price() if quoted_in_usdc else decimal_value(1)
        )

        result = []
        for row in prices:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise MalformedResponse(self.name, "invalid historical price row")
            try:
                timestamp = utc_datetime(row[0], milliseconds=True)
                price_value = decimal_value(row[1]) / current_usdc_usd
            except (ValueError, ZeroDivisionError) as exc:
                raise MalformedResponse(self.name, "invalid historical price value") from exc
            if query_start <= timestamp <= end_utc:
                result.append(
                    point(
                        symbol=normalized,
                        timestamp=timestamp,
                        price=price_value,
                        provider=self.name,
                        interval=normalized_interval,
                        is_derived=quoted_in_usdc,
                    )
                )
        if normalized_interval == "1d" and start_utc < query_start:
            # Range history begins at the next UTC daily bucket, which is
            # later than the exact rolling-365-day cutoff. CoinGecko's dated
            # snapshot endpoint remains available for that boundary day and
            # supplies a real observation at 00:00 UTC, allowing analytics to
            # choose a reference at or before the cutoff.
            boundary_time = datetime.combine(query_start.date(), datetime_time.min, tzinfo=UTC)
            if start_utc <= boundary_time <= end_utc:
                try:
                    boundary_price = await self._daily_snapshot_usd_price(
                        coin_id, query_start.date().strftime("%d-%m-%Y")
                    )
                except ProviderError:
                    # Keep the otherwise valid 365-day range when an optional
                    # boundary lookup is temporarily unavailable.
                    pass
                else:
                    if quoted_in_usdc:
                        boundary_price /= current_usdc_usd
                    result.append(
                        point(
                            symbol=normalized,
                            timestamp=boundary_time,
                            price=boundary_price,
                            provider=self.name,
                            interval=normalized_interval,
                            is_derived=quoted_in_usdc,
                        )
                    )
        result.sort(key=lambda item: item.timestamp)
        if limit is not None:
            result = result[-max(0, limit) :]
        return tuple(result)

    async def _daily_snapshot_usd_price(self, coin_id: str, date_text: str):
        async def load():
            payload = await self._request_json(
                "GET",
                f"{self.base_url}/coins/{coin_id}/history",
                params={"date": date_text, "localization": "false"},
                headers=self._headers,
            )
            document = require_mapping(payload, self.name)
            market_data = require_mapping(document.get("market_data"), self.name, "market data")
            current_price = require_mapping(
                market_data.get("current_price"), self.name, "historical current price"
            )
            try:
                value = decimal_value(current_price["usd"])
            except (KeyError, ValueError) as exc:
                raise MalformedResponse(self.name, "invalid historical daily snapshot") from exc
            if value <= 0:
                raise MalformedResponse(self.name, "historical daily snapshot is not positive")
            return value

        return await self._daily_snapshot_cache.get_or_load(
            (coin_id, date_text),
            self.daily_snapshot_success_ttl_seconds,
            load,
            error_ttl_seconds=self.daily_snapshot_error_ttl_seconds,
        )

    async def _history_normalization_price(self):
        # Historical changes are invariant to a constant quote-currency
        # normalization. Reuse the most recently observed normalization value even
        # after the live-price TTL expires so hourly backfills do not consume a
        # second monthly credit stream. The first history request still obtains
        # a real observation if no quote has populated the cache yet.
        document = self._price_cache
        if document is None:
            document = await self._simple_prices()
        try:
            normalization = require_mapping(
                document[self.normalization_coin_id],
                self.name,
                self.normalization_coin_id,
            )
            value = decimal_value(normalization["usd"])
            if value <= 0:
                raise ValueError("normalization price must be positive")
            return value
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid quote normalization price") from exc
