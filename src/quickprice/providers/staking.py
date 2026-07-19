"""Liquid-staking accrual-index and annualized-yield providers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
from bisect import bisect_right
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from quickprice.domain import (
    AccrualIndexPoint,
    PricePoint,
    RewardAccrualMode,
    SourceComponent,
    YieldMetric,
    YieldQuality,
    YieldRateType,
    ensure_utc,
    utc_now,
)
from quickprice.staking import (
    ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS,
    ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD,
    annualize_index_growth,
)

from ._models import decimal_value, utc_datetime
from .base import (
    HistoryProvider,
    HttpProvider,
    MalformedResponse,
    ProviderError,
    ProviderUnavailable,
    UnsupportedInstrument,
    require_mapping,
    require_sequence,
)


@dataclass(frozen=True, slots=True)
class EthereumExchangeRateSpec:
    """ABI constants for a value-accruing staking token on an EVM chain."""

    symbol: str
    index_symbol: str
    underlying_asset: str
    contract_address: str
    chain_id: int
    call_data: str
    event_topic: str
    scale: Decimal = Decimal(10**18)
    accrual_mode: RewardAccrualMode = RewardAccrualMode.VALUE_ACCRUING

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError("chain id must be positive")
        if not _is_hex(self.contract_address, bytes_length=20):
            raise ValueError("contract address must be a 20-byte hex value")
        if not _is_hex(self.call_data, bytes_length=4):
            raise ValueError("call data must be a four-byte function selector")
        if not _is_hex(self.event_topic, bytes_length=32):
            raise ValueError("event topic must be a 32-byte hex value")
        object.__setattr__(self, "scale", decimal_value(self.scale))
        if self.scale <= 0:
            raise ValueError("exchange-rate scale must be positive")


def _is_hex(value: str, *, bytes_length: int) -> bool:
    if (
        not isinstance(value, str)
        or len(value) != 2 + bytes_length * 2
        or not value.startswith("0x")
    ):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def _hex_integer(value: Any, provider: str, context: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise MalformedResponse(provider, f"{context} must be a hex integer")
    try:
        result = int(value, 16)
    except ValueError as exc:
        raise MalformedResponse(provider, f"{context} must be a hex integer") from exc
    if result < 0:
        raise MalformedResponse(provider, f"{context} cannot be negative")
    return result


class EthereumExchangeRateYieldProvider(HttpProvider):
    """Derive trailing APY from a staking token's on-chain exchange-rate index."""

    name = "ethereum_exchange_rate"
    _minimum_routing_timeout_seconds = 45.0
    _maximum_routing_timeout_seconds = 300.0
    _routing_request_budget = 8.0

    def __init__(
        self,
        rpc_urls: str | Sequence[str],
        *,
        specs: Sequence[EthereumExchangeRateSpec] = (),
        observation_window_days: int = 7,
        history_padding_days: int = 7,
        max_log_block_span: int = 10_000,
        max_history_block_span: int = 500_000,
        latest_event_lookback_blocks: int = 100_000,
        max_parallel_log_requests: int = 4,
        endpoint_race_width: int = 2,
        block_cache_size: int = 2_048,
        routing_timeout_seconds: float | None = None,
        clock: Callable[[], datetime] = utc_now,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        urls = (rpc_urls,) if isinstance(rpc_urls, str) else tuple(rpc_urls)
        self.rpc_urls = tuple(url.strip() for url in urls if url.strip())
        if not self.rpc_urls:
            raise ValueError("at least one Ethereum JSON-RPC URL is required")
        self.specs = {spec.symbol.strip().upper(): spec for spec in specs}
        if len(self.specs) != len(specs):
            raise ValueError("duplicate Ethereum exchange-rate symbol")
        if observation_window_days <= 0 or history_padding_days < 0:
            raise ValueError("invalid exchange-rate observation window")
        if max_log_block_span <= 0 or max_history_block_span <= 0:
            raise ValueError("Ethereum log block spans must be positive")
        if latest_event_lookback_blocks <= 0:
            raise ValueError("latest-event lookback must be positive")
        if max_parallel_log_requests <= 0 or endpoint_race_width <= 0:
            raise ValueError("Ethereum RPC concurrency limits must be positive")
        if block_cache_size <= 0:
            raise ValueError("Ethereum block cache size must be positive")
        self.observation_window_days = observation_window_days
        self.history_padding_days = history_padding_days
        self.max_log_block_span = max_log_block_span
        self.max_history_block_span = max_history_block_span
        self.latest_event_lookback_blocks = latest_event_lookback_blocks
        self.max_parallel_log_requests = max_parallel_log_requests
        self.endpoint_race_width = min(endpoint_race_width, len(self.rpc_urls))
        self.block_cache_size = block_cache_size
        if routing_timeout_seconds is None:
            # Yield discovery combines a current-rate lookup with two concurrent
            # block-height searches. Give that bounded workflow several HTTP
            # timeout windows without weakening the timeout on any one request.
            routing_timeout_seconds = min(
                self._maximum_routing_timeout_seconds,
                max(
                    self._minimum_routing_timeout_seconds,
                    self.request_timeout * self._routing_request_budget,
                ),
            )
        if not math.isfinite(routing_timeout_seconds) or routing_timeout_seconds <= 0:
            raise ValueError("Ethereum routing timeout must be finite and positive")
        self.routing_timeout_seconds = float(routing_timeout_seconds)
        self._clock = clock
        self._verified_endpoints: set[tuple[str, int]] = set()
        self._block_cache: dict[tuple[str, int], Mapping[str, Any]] = {}
        self._endpoint_offset = 0

    def _spec(self, symbol: str) -> EthereumExchangeRateSpec:
        normalized = symbol.strip().upper()
        exact = self.specs.get(normalized)
        if exact is not None:
            return exact
        base = normalized.partition(":")[0]
        matching = tuple(
            spec for spec in self.specs.values() if spec.symbol.partition(":")[0] == base
        )
        if len(matching) == 1 and normalized.count(":") == 1 and normalized.partition(":")[2]:
            return replace(matching[0], symbol=normalized)
        raise UnsupportedInstrument(self.name, f"unsupported yield symbol {normalized}")

    async def get_accrual_index(self, symbol: str) -> AccrualIndexPoint:
        spec = self._spec(symbol)
        return await self._try_endpoints(
            lambda endpoint: self._current_index_on_endpoint(endpoint, spec)
        )

    async def get_accrual_index_history(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
    ) -> Sequence[AccrualIndexPoint]:
        spec = self._spec(symbol)
        start = ensure_utc(start)
        end = ensure_utc(end)
        if start >= end:
            raise ValueError("accrual index history start must be before end")
        return await self._try_endpoints(
            lambda endpoint: self._index_history_on_endpoint(endpoint, spec, start, end)
        )

    async def get_yield(self, symbol: str) -> YieldMetric:
        spec = self._spec(symbol)
        return await self._try_endpoints(lambda endpoint: self._yield_on_endpoint(endpoint, spec))

    async def _try_endpoints(self, operation: Callable[[str], Awaitable[Any]]) -> Any:
        start = self._endpoint_offset
        endpoints = tuple(
            self.rpc_urls[(start + offset) % len(self.rpc_urls)]
            for offset in range(self.endpoint_race_width)
        )
        self._endpoint_offset = (start + self.endpoint_race_width) % len(self.rpc_urls)
        tasks = tuple(asyncio.create_task(operation(endpoint)) for endpoint in endpoints)
        try:
            for task in asyncio.as_completed(tasks):
                try:
                    result = await task
                except ProviderError:
                    continue
                return result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        endpoint_count = len(endpoints)
        configured_count = len(self.rpc_urls)
        suffix = "" if endpoint_count == configured_count else f" of {configured_count} configured"
        raise ProviderUnavailable(
            self.name,
            f"all {endpoint_count}{suffix} JSON-RPC endpoints failed",
        ) from None

    async def _yield_on_endpoint(
        self,
        endpoint: str,
        spec: EthereumExchangeRateSpec,
    ) -> YieldMetric:
        current = await self._current_index_on_endpoint(endpoint, spec)
        cutoff = current.as_of - timedelta(days=self.observation_window_days)
        history = await self._index_history_on_endpoint(
            endpoint,
            spec,
            cutoff - timedelta(days=self.history_padding_days),
            current.as_of,
        )
        reference = next(
            (point for point in reversed(history) if point.as_of <= cutoff),
            None,
        )
        if reference is None:
            raise ProviderUnavailable(self.name, "no exchange-rate observation before cutoff")
        percent, window_days = annualize_index_growth(reference, current)
        staleness_ms = _staleness_ms(self._clock(), current.as_of)
        return YieldMetric(
            symbol=spec.symbol,
            value=percent,
            as_of=current.as_of,
            method=ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD,
            provider=self.name,
            is_proxy=False,
            components=(
                SourceComponent(
                    symbol=reference.symbol,
                    provider=self.name,
                    price=reference.value,
                    as_of=reference.as_of,
                    feed="ethereum_logs",
                    role="reference_accrual_index",
                ),
                SourceComponent(
                    symbol=current.symbol,
                    provider=self.name,
                    price=current.value,
                    as_of=current.as_of,
                    feed="ethereum_logs",
                    role="current_accrual_index",
                ),
            ),
            rate_type=YieldRateType.APY,
            observation_window_days=window_days,
            accrual_mode=spec.accrual_mode,
            underlying_asset=spec.underlying_asset,
            is_estimate=True,
            accrual_index=current,
            quality=YieldQuality(
                stale=staleness_ms > ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS * 1000,
                staleness_ms=staleness_ms,
                confidence="high",
            ),
            fallback_level=0,
        )

    async def _current_index_on_endpoint(
        self,
        endpoint: str,
        spec: EthereumExchangeRateSpec,
    ) -> AccrualIndexPoint:
        await self._validate_chain(endpoint, spec.chain_id)
        latest_number = _hex_integer(
            await self._rpc(endpoint, "eth_blockNumber", ()),
            self.name,
            "latest block number",
        )
        oldest_number = max(0, latest_number - self.latest_event_lookback_blocks + 1)
        chunk_end = latest_number
        while chunk_end >= oldest_number:
            chunk_start = max(oldest_number, chunk_end - self.max_log_block_span + 1)
            logs = await self._logs_for_block_range(
                endpoint,
                spec,
                chunk_start,
                chunk_end,
            )
            candidates = tuple(log for log in logs if log.get("removed") is not True)
            if candidates:
                latest_log = max(candidates, key=lambda log: _log_position(log, self.name))
                point = await self._point_from_log(endpoint, spec, latest_log)
                raw_rate = await self._rpc(
                    endpoint,
                    "eth_call",
                    (
                        {"to": spec.contract_address, "data": spec.call_data},
                        hex(latest_number),
                    ),
                )
                called_value = (
                    Decimal(_hex_integer(raw_rate, self.name, "exchange rate")) / spec.scale
                )
                if called_value != point.value:
                    raise MalformedResponse(
                        self.name,
                        "latest exchange-rate event does not match current contract state",
                    )
                return point
            chunk_end = chunk_start - 1
        raise ProviderUnavailable(
            self.name,
            "no recent ExchangeRateUpdated event was found within the bounded lookback",
        )

    async def _index_history_on_endpoint(
        self,
        endpoint: str,
        spec: EthereumExchangeRateSpec,
        start: datetime,
        end: datetime,
    ) -> Sequence[AccrualIndexPoint]:
        await self._validate_chain(endpoint, spec.chain_id)
        latest = _hex_integer(
            await self._rpc(endpoint, "eth_blockNumber", ()),
            self.name,
            "latest block number",
        )
        latest_block = await self._block(endpoint, latest)
        from_block, to_block = await asyncio.gather(
            self._block_at_or_before(
                endpoint,
                start,
                latest=latest,
                latest_block=latest_block,
            ),
            self._block_at_or_before(
                endpoint,
                end,
                latest=latest,
                latest_block=latest_block,
            ),
        )
        if to_block - from_block + 1 > self.max_history_block_span:
            raise ProviderUnavailable(
                self.name,
                "requested exchange-rate history exceeds the configured block limit",
            )
        logs = await self._logs_for_block_ranges(endpoint, spec, from_block, to_block)
        parsed_logs: list[Mapping[str, Any]] = []
        for item in logs:
            log = require_mapping(item, self.name, "exchange-rate log")
            if log.get("removed") is True:
                continue
            parsed_logs.append(log)
        points = await asyncio.gather(
            *(self._point_from_log(endpoint, spec, log) for log in parsed_logs)
        )
        filtered: list[AccrualIndexPoint] = []
        for point in points:
            as_of = point.as_of
            if not start <= as_of <= end:
                continue
            filtered.append(point)
        return tuple(sorted(set(filtered), key=lambda point: point.as_of))

    async def _logs_for_block_ranges(
        self,
        endpoint: str,
        spec: EthereumExchangeRateSpec,
        from_block: int,
        to_block: int,
    ) -> Sequence[Mapping[str, Any]]:
        if from_block > to_block:
            return ()
        ranges = tuple(
            (
                block_start,
                min(to_block, block_start + self.max_log_block_span - 1),
            )
            for block_start in range(from_block, to_block + 1, self.max_log_block_span)
        )
        semaphore = asyncio.Semaphore(self.max_parallel_log_requests)

        async def fetch(block_range: tuple[int, int]) -> Sequence[Mapping[str, Any]]:
            async with semaphore:
                return await self._logs_for_block_range(
                    endpoint,
                    spec,
                    block_range[0],
                    block_range[1],
                )

        batches = await asyncio.gather(*(fetch(block_range) for block_range in ranges))
        return tuple(log for batch in batches for log in batch)

    async def _logs_for_block_range(
        self,
        endpoint: str,
        spec: EthereumExchangeRateSpec,
        from_block: int,
        to_block: int,
    ) -> Sequence[Mapping[str, Any]]:
        if from_block < 0 or to_block < from_block:
            raise ValueError("invalid Ethereum log block range")
        if to_block - from_block + 1 > self.max_log_block_span:
            raise ValueError("Ethereum log request exceeds the configured block span")
        payload = await self._rpc(
            endpoint,
            "eth_getLogs",
            (
                {
                    "address": spec.contract_address,
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "topics": [spec.event_topic],
                },
            ),
        )
        logs = require_sequence(payload, self.name, "exchange-rate logs")
        return tuple(require_mapping(item, self.name, "exchange-rate log") for item in logs)

    async def _point_from_log(
        self,
        endpoint: str,
        spec: EthereumExchangeRateSpec,
        log: Mapping[str, Any],
    ) -> AccrualIndexPoint:
        block_number = _hex_integer(log.get("blockNumber"), self.name, "log block number")
        data = log.get("data")
        if not _is_hex(data, bytes_length=32):
            raise MalformedResponse(self.name, "exchange-rate event data is invalid")
        value = Decimal(int(data, 16)) / spec.scale
        if value <= 0:
            raise MalformedResponse(self.name, "exchange-rate event value must be positive")
        block = await self._block(endpoint, block_number)
        return AccrualIndexPoint(
            symbol=spec.index_symbol,
            underlying_asset=spec.underlying_asset,
            value=value,
            as_of=_block_timestamp(block, self.name),
            provider=self.name,
            kind="redemption_rate",
        )

    async def _validate_chain(self, endpoint: str, expected_chain_id: int) -> None:
        key = (endpoint, expected_chain_id)
        if key in self._verified_endpoints:
            return
        actual = _hex_integer(
            await self._rpc(endpoint, "eth_chainId", ()),
            self.name,
            "chain id",
        )
        if actual != expected_chain_id:
            raise ProviderUnavailable(self.name, "JSON-RPC endpoint returned the wrong chain")
        self._verified_endpoints.add(key)

    async def _block_at_or_before(
        self,
        endpoint: str,
        target: datetime,
        *,
        latest: int | None = None,
        latest_block: Mapping[str, Any] | None = None,
    ) -> int:
        target = ensure_utc(target)
        if latest is None:
            latest = _hex_integer(
                await self._rpc(endpoint, "eth_blockNumber", ()),
                self.name,
                "latest block number",
            )
        if latest_block is None:
            latest_block = await self._block(endpoint, latest)
        if _block_timestamp(latest_block, self.name) <= target:
            return latest
        low = 0
        high = latest
        best = 0
        while low <= high:
            middle = (low + high) // 2
            block = await self._block(endpoint, middle)
            if _block_timestamp(block, self.name) <= target:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        return best

    async def _block(self, endpoint: str, number: int) -> Mapping[str, Any]:
        key = (endpoint, number)
        cached = self._block_cache.get(key)
        if cached is not None:
            return cached
        payload = await self._rpc(endpoint, "eth_getBlockByNumber", (hex(number), False))
        block = require_mapping(payload, self.name, "block")
        self._block_cache[key] = block
        while len(self._block_cache) > self.block_cache_size:
            self._block_cache.pop(next(iter(self._block_cache)))
        return block

    async def _rpc(self, endpoint: str, method: str, params: Sequence[Any]) -> Any:
        payload = await self._request_json(
            "POST",
            endpoint,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
        )
        document = require_mapping(payload, self.name, "JSON-RPC response")
        if document.get("error") is not None:
            raise ProviderUnavailable(self.name, "JSON-RPC returned an error")
        if "result" not in document or document["result"] is None:
            raise MalformedResponse(self.name, "JSON-RPC response has no result")
        return document["result"]


def _block_timestamp(block: Mapping[str, Any], provider: str) -> datetime:
    timestamp = _hex_integer(block.get("timestamp"), provider, "block timestamp")
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise MalformedResponse(provider, "block timestamp is invalid") from exc


def _log_position(log: Mapping[str, Any], provider: str) -> tuple[int, int]:
    """Return a deterministic chain position for selecting the newest event."""

    block_number = _hex_integer(log.get("blockNumber"), provider, "log block number")
    log_index = _hex_integer(log.get("logIndex", "0x0"), provider, "log index")
    return block_number, log_index


@dataclass(frozen=True, slots=True)
class _BinanceRateRow:
    as_of: datetime
    apr_fraction: Decimal
    exchange_rate: Decimal


class BinanceWbethYieldProvider(HttpProvider):
    """Read Binance's signed, provider-reported staking APR."""

    name = "binance_wbeth_rate"
    base_url = "https://api.binance.com"
    path = "/sapi/v1/eth-staking/eth/history/rateHistory"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        yield_policies: Mapping[str, Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] = utc_now,
        recv_window_ms: int = 5000,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not api_key or not api_secret:
            raise ValueError("Binance API key and secret are required")
        if not 1 <= recv_window_ms <= 60_000:
            raise ValueError("Binance receive window must be between 1 and 60000 milliseconds")
        self.api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.yield_policies = {
            symbol.strip().upper(): dict(policy)
            for symbol, policy in (yield_policies or {}).items()
        }
        self._clock = clock
        self.recv_window_ms = recv_window_ms

    def _policy(self, symbol: str) -> tuple[str, Mapping[str, Any]]:
        normalized = symbol.strip().upper()
        exact = self.yield_policies.get(normalized)
        if exact is not None:
            return normalized, exact
        base, separator, quote_asset = normalized.partition(":")
        matches = tuple(
            policy
            for configured, policy in self.yield_policies.items()
            if configured.partition(":")[0] == base
        )
        if separator and quote_asset and len(matches) == 1:
            return normalized, matches[0]
        raise UnsupportedInstrument(self.name, f"unsupported yield symbol {normalized}")

    async def get_yield(self, symbol: str) -> YieldMetric:
        normalized, policy = self._policy(symbol)
        index_symbol = str(policy["index_symbol"]).strip().upper()
        underlying_asset = str(policy["underlying_asset"]).strip().upper()
        accrual_mode = RewardAccrualMode(policy["accrual_mode"])
        method = str(policy["method"]).strip()
        now = ensure_utc(self._clock())
        rows = await self._rows(now - timedelta(days=30), now)
        if not rows:
            raise ProviderUnavailable(self.name, "staking-rate history is empty")
        row = rows[-1]
        index = AccrualIndexPoint(
            symbol=index_symbol,
            underlying_asset=underlying_asset,
            value=row.exchange_rate,
            as_of=row.as_of,
            provider=self.name,
            kind="vendor_exchange_rate",
        )
        staleness_ms = _staleness_ms(now, row.as_of)
        return YieldMetric(
            symbol=normalized,
            # Binance encodes APR as a fraction: 0.023 means 2.3 percent.
            # It is already annualized and must not be multiplied by 365.
            value=row.apr_fraction * Decimal(100),
            as_of=row.as_of,
            method=method,
            provider=self.name,
            is_proxy=False,
            components=(
                SourceComponent(
                    symbol=index.symbol,
                    provider=self.name,
                    price=index.value,
                    as_of=index.as_of,
                    feed="binance_eth_staking",
                    role="vendor_exchange_rate",
                ),
            ),
            rate_type=YieldRateType.APR,
            accrual_mode=accrual_mode,
            underlying_asset=underlying_asset,
            is_estimate=False,
            accrual_index=index,
            quality=YieldQuality(
                stale=staleness_ms > 2 * 24 * 60 * 60 * 1000,
                staleness_ms=staleness_ms,
                confidence="high",
            ),
            fallback_level=0,
        )

    async def get_accrual_index(self, symbol: str) -> AccrualIndexPoint:
        metric = await self.get_yield(symbol)
        if metric.accrual_index is None:
            raise ProviderUnavailable(self.name, "staking exchange rate is unavailable")
        return metric.accrual_index

    async def get_accrual_index_history(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
    ) -> Sequence[AccrualIndexPoint]:
        _, policy = self._policy(symbol)
        index_symbol = str(policy["index_symbol"]).strip().upper()
        underlying_asset = str(policy["underlying_asset"]).strip().upper()
        rows = await self._rows(ensure_utc(start), ensure_utc(end))
        return tuple(
            AccrualIndexPoint(
                symbol=index_symbol,
                underlying_asset=underlying_asset,
                value=row.exchange_rate,
                as_of=row.as_of,
                provider=self.name,
                kind="vendor_exchange_rate",
            )
            for row in rows
        )

    async def _rows(self, start: datetime, end: datetime) -> Sequence[_BinanceRateRow]:
        if start >= end:
            raise ValueError("staking-rate history start must be before end")
        if end - start > timedelta(days=93):
            raise ValueError("Binance staking-rate history cannot exceed three months")
        timestamp_ms = int(ensure_utc(self._clock()).timestamp() * 1000)
        params: dict[str, int | str] = {
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000),
            "current": 1,
            "size": 100,
            "recvWindow": self.recv_window_ms,
            "timestamp": timestamp_ms,
        }
        signature_payload = urlencode(params)
        params["signature"] = hmac.new(
            self._api_secret,
            signature_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        payload = await self._request_json(
            "GET",
            f"{self.base_url}{self.path}",
            params=params,
            headers={"X-MBX-APIKEY": self.api_key},
        )
        document = require_mapping(payload, self.name)
        rows = require_sequence(document.get("rows"), self.name, "staking-rate rows")
        result: list[_BinanceRateRow] = []
        for item in rows:
            row = require_mapping(item, self.name, "staking-rate row")
            try:
                as_of = utc_datetime(row["time"], milliseconds=True)
                apr_fraction = decimal_value(row["annualPercentageRate"])
                exchange_rate = decimal_value(row["exchangeRate"])
            except (KeyError, ValueError) as exc:
                raise MalformedResponse(self.name, "invalid staking-rate row") from exc
            if apr_fraction < 0:
                raise MalformedResponse(self.name, "staking APR cannot be negative")
            if exchange_rate <= 0:
                raise MalformedResponse(self.name, "staking exchange rate must be positive")
            if start <= as_of <= end:
                result.append(_BinanceRateRow(as_of, apr_fraction, exchange_rate))
        return tuple(sorted(result, key=lambda row: row.as_of))


class LidoAprProvider(HttpProvider):
    """Read Lido's official seven-day simple-moving-average staking APR."""

    name = "lido"
    base_url = "https://eth-api.lido.fi"
    path = "/v1/protocol/steth/apr/sma"

    def __init__(
        self,
        *,
        yield_policies: Mapping[str, Mapping[str, Any]] | None = None,
        expected_contract_address: str,
        expected_chain_id: int = 1,
        clock: Callable[[], datetime] = utc_now,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.yield_policies = {
            symbol.strip().upper(): dict(policy)
            for symbol, policy in (yield_policies or {}).items()
        }
        self.expected_contract_address = expected_contract_address.strip().lower()
        if not _is_hex(self.expected_contract_address, bytes_length=20):
            raise ValueError("Lido contract address must be a 20-byte hex value")
        if expected_chain_id <= 0:
            raise ValueError("Lido chain id must be positive")
        self.expected_chain_id = expected_chain_id
        self._clock = clock

    def _policy(self, symbol: str) -> Mapping[str, Any]:
        normalized = symbol.strip().upper()
        exact = self.yield_policies.get(normalized)
        if exact is not None:
            return exact
        base, separator, quote_asset = normalized.partition(":")
        matches = tuple(
            policy
            for configured, policy in self.yield_policies.items()
            if configured.partition(":")[0] == base
        )
        if separator and quote_asset and len(matches) == 1:
            return matches[0]
        raise UnsupportedInstrument(self.name, f"unsupported yield symbol {normalized}")

    async def get_yield(self, symbol: str) -> YieldMetric:
        normalized = symbol.strip().upper()
        try:
            policy = self._policy(normalized)
            provider_asset = str(policy["provider_asset"]).strip().lower()
            accrual_mode = RewardAccrualMode(policy["accrual_mode"])
            underlying_asset = str(policy["underlying_asset"]).strip().upper()
        except (KeyError, TypeError, ValueError) as exc:
            raise UnsupportedInstrument(
                self.name, f"unsupported yield symbol {normalized}"
            ) from exc

        payload = await self._request_json("GET", f"{self.base_url}{self.path}")
        document = require_mapping(payload, self.name)
        data = require_mapping(document.get("data"), self.name, "APR data")
        meta = require_mapping(document.get("meta"), self.name, "APR metadata")
        apr_rows = require_sequence(data.get("aprs"), self.name, "APR observations")
        if not apr_rows:
            raise ProviderUnavailable(self.name, "Lido APR observations are empty")
        try:
            chain_id = int(meta.get("chainId", 0))
        except (TypeError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid Lido APR chain id") from exc
        if (
            str(meta.get("symbol", "")).lower() != provider_asset
            or str(meta.get("address", "")).lower() != self.expected_contract_address
            or chain_id != self.expected_chain_id
        ):
            raise MalformedResponse(self.name, "unexpected Lido APR metadata")

        observations: list[datetime] = []
        for item in apr_rows:
            row = require_mapping(item, self.name, "APR observation")
            try:
                observations.append(utc_datetime(row["timeUnix"]))
                decimal_value(row["apr"])
            except (KeyError, ValueError) as exc:
                raise MalformedResponse(self.name, "invalid Lido APR observation") from exc
        try:
            value = decimal_value(data["smaApr"])
        except (KeyError, ValueError) as exc:
            raise MalformedResponse(self.name, "invalid Lido SMA APR") from exc

        as_of = max(observations)
        staleness_ms = _staleness_ms(self._clock(), as_of)
        return YieldMetric(
            symbol=normalized,
            value=value,
            as_of=as_of,
            method="lido_steth_apr_7d_sma",
            provider=self.name,
            is_proxy=False,
            rate_type=YieldRateType.APR,
            observation_window_days=Decimal(7),
            accrual_mode=accrual_mode,
            underlying_asset=underlying_asset,
            is_estimate=True,
            quality=YieldQuality(
                stale=staleness_ms > 2 * 24 * 60 * 60 * 1000,
                staleness_ms=staleness_ms,
                confidence="high",
            ),
            fallback_level=0,
        )


@dataclass(frozen=True, slots=True)
class StakingMarketRatioSpec:
    """Market pairs used by the explicitly low-confidence ratio fallback."""

    symbol: str
    staking_pair: str
    underlying_pair: str
    underlying_asset: str
    accrual_mode: RewardAccrualMode
    lookback_days: int | None = None

    def __post_init__(self) -> None:
        staking_parts = self.staking_pair.split(":")
        underlying_parts = self.underlying_pair.split(":")
        if len(staking_parts) != 2 or len(underlying_parts) != 2:
            raise ValueError("market-ratio pairs must use BASE:QUOTE form")
        if staking_parts[1].upper() != underlying_parts[1].upper():
            raise ValueError("market-ratio pairs must share a quote asset")
        if underlying_parts[0].upper() != self.underlying_asset.upper():
            raise ValueError("underlying pair must match the configured underlying asset")
        if self.accrual_mode is not RewardAccrualMode.VALUE_ACCRUING:
            raise ValueError("market-ratio yield is only valid for value-accruing tokens")
        if self.lookback_days is not None and not 7 <= self.lookback_days <= 365:
            raise ValueError("market-ratio lookback must be between 7 and 365 days")


@dataclass(frozen=True, slots=True)
class _MarketRatioObservation:
    index: AccrualIndexPoint
    staking: PricePoint
    underlying: PricePoint


class StakingMarketRatioYieldProvider:
    """Last-resort 30-day APY proxy from staking-token/underlying market prices."""

    name = "staking_market_ratio_proxy"

    def __init__(
        self,
        history_provider: HistoryProvider,
        *,
        specs: Sequence[StakingMarketRatioSpec] = (),
        clock: Callable[[], datetime] = utc_now,
        lookback_days: int = 30,
        padding_days: int = 3,
        max_component_skew: timedelta = timedelta(hours=12),
    ) -> None:
        if lookback_days <= 0 or padding_days < 0:
            raise ValueError("invalid market-ratio observation window")
        if max_component_skew < timedelta(0):
            raise ValueError("market-ratio component skew cannot be negative")
        self.history_provider = history_provider
        self.specs = {spec.symbol.strip().upper(): spec for spec in specs}
        if len(self.specs) != len(specs):
            raise ValueError("duplicate market-ratio yield symbol")
        self._clock = clock
        self.lookback_days = lookback_days
        self.padding_days = padding_days
        self.max_component_skew = max_component_skew

    async def get_yield(self, symbol: str) -> YieldMetric:
        normalized = symbol.strip().upper()
        try:
            spec = self.specs[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(
                self.name, f"unsupported yield symbol {normalized}"
            ) from exc
        now = ensure_utc(self._clock())
        lookback_days = spec.lookback_days or self.lookback_days
        start = now - timedelta(days=lookback_days + self.padding_days)
        staking_points, underlying_points = await asyncio.gather(
            self.history_provider.get_history(
                spec.staking_pair,
                interval="1d",
                start=start,
                end=now,
                limit=None,
            ),
            self.history_provider.get_history(
                spec.underlying_pair,
                interval="1d",
                start=start,
                end=now,
                limit=None,
            ),
        )
        observations = self._observations(spec, staking_points, underlying_points)
        if not observations:
            raise ProviderUnavailable(self.name, "no aligned market-ratio observations")
        cutoff = now - timedelta(days=lookback_days)
        reference = next(
            (item for item in reversed(observations) if item.index.as_of <= cutoff),
            None,
        )
        current = observations[-1]
        if reference is None or reference.index.as_of >= current.index.as_of:
            raise ProviderUnavailable(self.name, "30-day market-ratio history is incomplete")
        percent, window_days = annualize_index_growth(reference.index, current.index)
        staleness_ms = _staleness_ms(now, current.index.as_of)
        return YieldMetric(
            symbol=spec.symbol,
            value=percent,
            as_of=current.index.as_of,
            method=f"staking_market_ratio_{lookback_days}d_annualized",
            provider=self.name,
            is_proxy=True,
            components=(
                _price_component(reference.staking, "reference_staking_token_price"),
                _price_component(reference.underlying, "reference_underlying_price"),
                _price_component(current.staking, "current_staking_token_price"),
                _price_component(current.underlying, "current_underlying_price"),
            ),
            rate_type=YieldRateType.APY,
            observation_window_days=window_days,
            accrual_mode=spec.accrual_mode,
            underlying_asset=spec.underlying_asset,
            is_estimate=True,
            accrual_index=current.index,
            quality=YieldQuality(
                stale=staleness_ms > 2 * 24 * 60 * 60 * 1000,
                staleness_ms=staleness_ms,
                confidence="low",
            ),
            # Route position, not adapter identity, determines fallback level.
            fallback_level=0,
        )

    def _observations(
        self,
        spec: StakingMarketRatioSpec,
        staking_points: Sequence[PricePoint],
        underlying_points: Sequence[PricePoint],
    ) -> Sequence[_MarketRatioObservation]:
        underlying = tuple(sorted(underlying_points, key=lambda point: point.timestamp))
        underlying_times = tuple(point.timestamp for point in underlying)
        result: list[_MarketRatioObservation] = []
        for staking in sorted(staking_points, key=lambda point: point.timestamp):
            if not underlying:
                break
            match_index = bisect_right(underlying_times, staking.timestamp) - 1
            if match_index < 0:
                continue
            match = underlying[match_index]
            skew = staking.timestamp - match.timestamp
            if skew > self.max_component_skew:
                continue
            as_of = match.timestamp
            index = AccrualIndexPoint(
                symbol=f"{spec.staking_pair.split(':', 1)[0]}:{spec.underlying_asset}",
                underlying_asset=spec.underlying_asset,
                value=staking.price / match.price,
                as_of=as_of,
                provider=self.name,
                kind="market_price_ratio",
            )
            result.append(_MarketRatioObservation(index, staking, match))
        return tuple(result)


def _price_component(point: PricePoint, role: str) -> SourceComponent:
    return SourceComponent(
        symbol=point.symbol,
        provider=point.provider,
        price=point.price,
        as_of=point.timestamp,
        feed=point.interval,
        role=role,
    )


def _staleness_ms(now: datetime, as_of: datetime) -> int:
    return max(0, int((ensure_utc(now) - ensure_utc(as_of)).total_seconds() * 1000))
