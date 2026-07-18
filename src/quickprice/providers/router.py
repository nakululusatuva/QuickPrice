"""Capability-aware provider routing with singleflight and circuit breakers."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar

from quickprice.domain import DividendEvent, PricePoint, ProviderQuote, YieldMetric

from ._models import replace_metadata
from .base import (
    AllProvidersFailed,
    Capability,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    UnsupportedInstrument,
)


@dataclass(slots=True)
class _Circuit:
    consecutive_failures: int = 0
    open_count: int = 0
    open_until: float = 0.0
    probing: bool = False


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    provider: str
    symbol: str
    capability: str
    state: str
    consecutive_failures: int
    open_count: int
    retry_in_seconds: float


class ProviderRouter:
    """Route a symbol/capability to an ordered provider chain.

    Concurrent identical calls share one task. A breaker opens after three
    consecutive failures. Its first half-open probe occurs after 60 seconds;
    failed probes exponentially increase that cooldown up to 15 minutes.
    """

    _METHODS: ClassVar[dict[Capability, str]] = {
        Capability.QUOTE: "get_quote",
        Capability.HISTORY: "get_history",
        Capability.DIVIDEND: "get_latest_dividend",
        Capability.YIELD: "get_yield",
    }

    def __init__(
        self,
        routes: Mapping[tuple[str, Capability | str], Sequence[Any]] | None = None,
        *,
        timeout_seconds: float = 8.0,
        failure_threshold: int = 3,
        half_open_after_seconds: float = 60.0,
        max_backoff_seconds: float = 900.0,
        clock: Any = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        self.timeout_seconds = timeout_seconds
        self.failure_threshold = failure_threshold
        self.half_open_after_seconds = half_open_after_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._clock = clock
        self._routes: dict[tuple[str, Capability], tuple[Any, ...]] = {}
        self._circuits: dict[tuple[int, str, Capability], _Circuit] = defaultdict(_Circuit)
        self._flights: dict[Hashable, asyncio.Task[Any]] = {}
        self._flights_lock = asyncio.Lock()
        self._fallbacks: dict[tuple[str, Capability, str], int] = defaultdict(int)
        if routes:
            for (symbol, capability), providers in routes.items():
                self.register(symbol, Capability(capability), providers)

    @staticmethod
    def _symbol(symbol: str) -> str:
        return symbol.strip().upper()

    def register(
        self,
        symbol: str,
        capability: Capability | str,
        providers: Sequence[Any],
    ) -> None:
        cap = Capability(capability)
        chain = tuple(providers)
        if not chain:
            raise ValueError(f"provider chain cannot be empty: {symbol}/{cap.value}")
        method = self._METHODS[cap]
        for provider in chain:
            if not callable(getattr(provider, method, None)):
                raise TypeError(f"{getattr(provider, 'name', provider)!r} lacks {method}")
        key = (self._symbol(symbol), cap)
        if key in self._routes:
            raise ValueError(f"duplicate provider route: {key[0]}/{cap.value}")
        self._routes[key] = chain

    def configured(self, symbol: str, capability: Capability | str) -> bool:
        return (self._symbol(symbol), Capability(capability)) in self._routes

    def providers_for(
        self,
        symbol: str,
        capability: Capability | str,
    ) -> tuple[Any, ...]:
        """Return the immutable preference chain for collection orchestration."""

        return self._routes.get((self._symbol(symbol), Capability(capability)), ())

    async def get_quote(self, symbol: str) -> ProviderQuote:
        return await self._execute(symbol, Capability.QUOTE)

    async def get_history(
        self,
        symbol: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> Sequence[PricePoint]:
        return await self._execute(
            symbol,
            Capability.HISTORY,
            interval=interval,
            start=start,
            end=end,
            limit=limit,
        )

    async def get_latest_dividend(self, symbol: str) -> DividendEvent | None:
        return await self._execute(symbol, Capability.DIVIDEND)

    async def get_yield(self, symbol: str) -> YieldMetric:
        return await self._execute(symbol, Capability.YIELD)

    async def _execute(self, symbol: str, capability: Capability, **kwargs: Any) -> Any:
        normalized = self._symbol(symbol)
        key = (
            normalized,
            capability,
            tuple(sorted((name, self._hashable(value)) for name, value in kwargs.items())),
        )
        async with self._flights_lock:
            task = self._flights.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._dispatch(normalized, capability, kwargs),
                    name=f"provider:{capability.value}:{normalized}",
                )
                self._flights[key] = task
                task.add_done_callback(
                    lambda completed, flight_key=key: self._schedule_flight_cleanup(
                        flight_key, completed
                    )
                )
        try:
            # Cancellation of one HTTP/client task must not cancel the shared fetch.
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._flights_lock:
                    if self._flights.get(key) is task:
                        self._flights.pop(key, None)

    def _schedule_flight_cleanup(self, key: Hashable, task: asyncio.Task[Any]) -> None:
        try:
            asyncio.get_running_loop().create_task(self._cleanup_flight(key, task))
        except RuntimeError:
            # Event-loop shutdown will release the router and its task map together.
            return

    async def _cleanup_flight(self, key: Hashable, task: asyncio.Task[Any]) -> None:
        # Retrieve an orphaned exception when every waiter was cancelled. Awaiting
        # callers still receive exactly the same exception from the shared task.
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        async with self._flights_lock:
            if self._flights.get(key) is task:
                self._flights.pop(key, None)

    @classmethod
    def _hashable(cls, value: Any) -> Hashable:
        if isinstance(value, Mapping):
            return tuple(sorted((str(k), cls._hashable(v)) for k, v in value.items()))
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(cls._hashable(item) for item in value)
        try:
            hash(value)
        except TypeError:
            return repr(value)
        return value

    async def _dispatch(
        self,
        symbol: str,
        capability: Capability,
        kwargs: Mapping[str, Any],
    ) -> Any:
        providers = self._routes.get((symbol, capability))
        if not providers:
            raise AllProvidersFailed(symbol, capability, (("router", "not configured"),))

        attempts: list[tuple[str, str]] = []
        history_segments: list[Sequence[PricePoint]] = []
        method_name = self._METHODS[capability]
        for fallback_level, provider in enumerate(providers):
            name = str(getattr(provider, "name", provider.__class__.__name__))
            state_key = (id(provider), symbol, capability)
            circuit = self._circuits[state_key]
            if not self._allow_call(circuit):
                attempts.append((name, "circuit_open"))
                continue

            try:
                method = getattr(provider, method_name)
                async with asyncio.timeout(self.timeout_seconds):
                    result = await method(symbol, **kwargs)
            except TimeoutError:
                error: ProviderError = ProviderUnavailable(name, "timeout")
                self._record_failure(circuit)
                attempts.append((name, error.message))
                continue
            except UnsupportedInstrument as error:
                circuit.probing = False
                attempts.append((name, error.message))
                continue
            except (ProviderRateLimited, ProviderError) as error:
                self._record_failure(circuit)
                attempts.append((name, error.message))
                continue
            except Exception as error:  # Keep malformed adapters from killing collectors.
                self._record_failure(circuit)
                attempts.append((name, type(error).__name__))
                continue

            self._record_success(circuit)
            if capability is Capability.DIVIDEND and result is None:
                attempts.append((name, "no_data"))
                continue
            if capability is Capability.HISTORY and not result:
                attempts.append((name, "no_data"))
                continue
            if capability is Capability.HISTORY:
                attached = self._attach_fallback(result, fallback_level)
                if getattr(
                    provider, "history_prefix_limited", False
                ) and not self._history_covers_start(attached, kwargs):
                    history_segments.append(attached)
                    if fallback_level:
                        self._fallbacks[(symbol, capability, name)] += 1
                    attempts.append((name, "incomplete_prefix"))
                    continue
                if history_segments:
                    history_segments.append(attached)
                    if fallback_level:
                        self._fallbacks[(symbol, capability, name)] += 1
                    return self._merge_history(history_segments, kwargs.get("limit"))
                if fallback_level:
                    self._fallbacks[(symbol, capability, name)] += 1
                return attached
            if fallback_level:
                self._fallbacks[(symbol, capability, name)] += 1
            return self._attach_fallback(result, fallback_level)

        if history_segments:
            return self._merge_history(history_segments, kwargs.get("limit"))
        raise AllProvidersFailed(symbol, capability, attempts)

    @staticmethod
    def _history_covers_start(points: Sequence[PricePoint], kwargs: Mapping[str, Any]) -> bool:
        if not points:
            return False
        start = kwargs.get("start")
        if not isinstance(start, datetime):
            return True
        interval = str(kwargs.get("interval", "1m")).lower()
        interval_seconds = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "1h": 3600,
            "4h": 14_400,
            "1d": 86_400,
        }.get(interval, 60)
        earliest = min(point.timestamp for point in points)
        return earliest <= start + timedelta(seconds=interval_seconds * 2)

    @staticmethod
    def _merge_history(
        segments: Sequence[Sequence[PricePoint]], limit: Any
    ) -> tuple[PricePoint, ...]:
        # Earlier segments are higher-priority providers and win overlaps.
        merged: dict[datetime, PricePoint] = {}
        for segment in segments:
            for point in segment:
                merged.setdefault(point.timestamp, point)
        ordered = tuple(merged[key] for key in sorted(merged))
        if isinstance(limit, int) and limit >= 0:
            return ordered[:limit] if limit else ()
        return ordered

    def _allow_call(self, circuit: _Circuit) -> bool:
        now = self._clock()
        if circuit.open_until <= 0:
            return True
        if now < circuit.open_until or circuit.probing:
            return False
        circuit.probing = True
        return True

    def _record_failure(self, circuit: _Circuit) -> None:
        circuit.probing = False
        circuit.consecutive_failures += 1
        if circuit.consecutive_failures < self.failure_threshold and circuit.open_until <= 0:
            return
        circuit.open_count += 1
        exponent = max(0, circuit.open_count - 1)
        delay = min(
            self.max_backoff_seconds,
            self.half_open_after_seconds * (2**exponent),
        )
        circuit.open_until = self._clock() + delay

    @staticmethod
    def _record_success(circuit: _Circuit) -> None:
        circuit.consecutive_failures = 0
        circuit.open_count = 0
        circuit.open_until = 0.0
        circuit.probing = False

    @staticmethod
    def _attach_fallback(value: Any, fallback_level: int) -> Any:
        if isinstance(value, list):
            return [ProviderRouter._attach_fallback(item, fallback_level) for item in value]
        if isinstance(value, tuple):
            return tuple(ProviderRouter._attach_fallback(item, fallback_level) for item in value)
        if value is None:
            return None
        # Synthetic results may already carry a fallback from one of their
        # component routes. Never erase that provenance at the outer route.
        existing = getattr(value, "fallback_level", 0)
        return replace_metadata(value, fallback_level=max(existing, fallback_level))

    def circuit_snapshots(self) -> tuple[CircuitSnapshot, ...]:
        now = self._clock()
        snapshots: list[CircuitSnapshot] = []
        for (provider_id, symbol, capability), circuit in self._circuits.items():
            provider_name = "unknown"
            for provider in self._routes.get((symbol, capability), ()):
                if id(provider) == provider_id:
                    provider_name = str(getattr(provider, "name", provider.__class__.__name__))
                    break
            if circuit.open_until <= 0:
                state = "closed"
            elif now < circuit.open_until:
                state = "open"
            elif circuit.probing:
                state = "half_open"
            else:
                state = "probe_ready"
            snapshots.append(
                CircuitSnapshot(
                    provider=provider_name,
                    symbol=symbol,
                    capability=capability.value,
                    state=state,
                    consecutive_failures=circuit.consecutive_failures,
                    open_count=circuit.open_count,
                    retry_in_seconds=max(0.0, circuit.open_until - now),
                )
            )
        return tuple(snapshots)

    def fallback_counts(self) -> dict[str, int]:
        return {
            f"{symbol}|{capability.value}|{provider}": count
            for (symbol, capability, provider), count in self._fallbacks.items()
        }

    async def close(self) -> None:
        async with self._flights_lock:
            flights = tuple(set(self._flights.values()))
            self._flights.clear()
        for task in flights:
            task.cancel()
        if flights:
            await asyncio.gather(*flights, return_exceptions=True)
        seen: set[int] = set()
        for providers in self._routes.values():
            for provider in providers:
                if id(provider) in seen:
                    continue
                seen.add(id(provider))
                close = getattr(provider, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
