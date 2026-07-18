"""Long-lived upstream collectors feeding the in-memory publication path.

All network and SQLite work remains outside HTTP request handling. Streaming
feeds are primary where the free plans allow it; capability routing supplies
REST fallback and circuit breaking.
"""

from __future__ import annotations

import asyncio
import dataclasses
import heapq
import itertools
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .config import Settings
from .domain import PricePoint, ProviderQuote, utc_now
from .fx import FX_HUB_SYMBOLS, FX_SYMBOLS, fx_hub_requirements
from .market import scheduled_market_status
from .plugin_api import AssetClass, MarketCalendar, YieldStrategy
from .providers.base import AllProvidersFailed, Capability, ProviderError
from .providers.fx import FX_MAX_SKEW
from .providers.wiring import ProviderGraph, build_provider_graph
from .registry import InstrumentRegistry, build_registry

FX_HISTORY_REFRESH_SECONDS = 24 * 60 * 60
FX_HISTORY_RETRY_SECONDS = 60
FX_STARTUP_PRESEED_TIMEOUT_SECONDS = 15.0
DAILY_PREFIX_RETRY_SECONDS = 24 * 60 * 60


def derive_cross_history(
    symbol: str,
    left: Sequence[PricePoint],
    right: Sequence[PricePoint],
    *,
    operation: str,
    max_skew: timedelta,
    provider: str,
    interval: str,
) -> tuple[PricePoint, ...]:
    """Align each left bar to the last right bar at or before it."""
    left_sorted = sorted(left, key=lambda item: item.timestamp)
    right_sorted = sorted(right, key=lambda item: item.timestamp)
    if not left_sorted or not right_sorted:
        return ()
    output: dict[datetime, PricePoint] = {}
    index = 0
    last_right: PricePoint | None = None
    for left_point in left_sorted:
        while index < len(right_sorted) and right_sorted[index].timestamp <= left_point.timestamp:
            last_right = right_sorted[index]
            index += 1
        if last_right is None or left_point.timestamp - last_right.timestamp > max_skew:
            continue
        try:
            price = (
                left_point.price * last_right.price
                if operation == "multiply"
                else left_point.price / last_right.price
            )
        except ArithmeticError, ZeroDivisionError:
            continue
        if price <= Decimal(0):
            continue
        output[left_point.timestamp] = PricePoint(
            symbol,
            left_point.timestamp,
            price,
            provider,
            True,
            interval,
        )
    return tuple(output[key] for key in sorted(output))


class MarketDataCoordinator:
    def __init__(
        self,
        service: Any,
        settings: Settings,
        registry: InstrumentRegistry | None = None,
    ) -> None:
        self.service = service
        self.settings = settings
        if registry is None:
            registry = getattr(service, "registry", None)
        if registry is None:
            registry = build_registry(settings.enabled_plugins)
        self.registry = registry
        self.graph: ProviderGraph = build_provider_graph(
            settings,
            self.registry,
            strict=settings.production and settings.background_enabled,
        )
        self.router = self.graph.router
        self._stop = asyncio.Event()
        self._started = asyncio.Event()
        self._supervisor: asyncio.Task[Any] | None = None
        self._fatal_error: BaseException | None = None
        self._pending: dict[str, ProviderQuote] = {}
        self._last_errors: dict[str, dict[str, Any]] = {}
        self._quota_snapshots: dict[str, dict[str, Any]] = {}
        self._websocket_reconnects: dict[str, int] = {}
        self._stream_observed_at: dict[tuple[int, str], float] = {}
        self._checkpoint_state: dict[tuple[str, str], dict[str, str]] = {}
        self._equity_history_fallback_at: dict[str, float] = {}
        self._daily_prefix_retry_at: dict[str, float] = {}
        self._daily_preseeded_symbols: set[str] = set()
        self._fx_daily_retry_symbols: set[str] = set()
        self._fx_failed_history_intervals: set[tuple[str, str]] = set()
        self._fx_history_intervals_for_cycle: dict[str, tuple[str, ...]] = {}
        self._fx_history_retry_only = False
        self._history_full_cycle = True
        self._fx_startup_preseed_timeout_seconds = FX_STARTUP_PRESEED_TIMEOUT_SECONDS
        self._next_fx_history_refresh_at = 0.0

    async def start(self) -> None:
        if self._supervisor is not None:
            return
        await self._restore_and_bind_provider_state()
        self._fatal_error = None
        self._supervisor = asyncio.create_task(self._supervise(), name="market-data-coordinator")
        await self._started.wait()

    async def _supervise(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fatal_error = exc
            self._record_error("coordinator:fatal", exc)

    @property
    def is_running(self) -> bool:
        return self._supervisor is not None and not self._supervisor.done()

    @property
    def fatal_error(self) -> BaseException | None:
        return self._fatal_error

    async def _restore_and_bind_provider_state(self) -> None:
        restored = self.service.restored_provider_checkpoints()
        storage = getattr(self.service, "_storage", None)
        if storage is None:
            if any(
                getattr(provider, "quota", None) is not None
                for provider in self.graph.providers.values()
            ):
                raise RuntimeError("durable storage is required for provider quota enforcement")
            return

        from .storage import ProviderCheckpointRecord

        for (provider_name, feed), record in restored.items():
            if feed == "quota":
                provider = self.graph.providers.get(provider_name)
                quota = getattr(provider, "quota", None)
                if quota is not None:
                    await quota.restore(record.checkpoint)
                continue
            symbols = record.checkpoint.get("symbols")
            if isinstance(symbols, dict):
                self._checkpoint_state[(provider_name, feed)] = {
                    str(symbol): str(as_of) for symbol, as_of in symbols.items()
                }

        for provider_name, provider in self.graph.providers.items():
            quota = getattr(provider, "quota", None)
            if quota is None:
                continue

            async def persist_quota(checkpoint: Any, *, name: str = provider_name) -> None:
                await storage.enqueue_checkpoint_record(
                    ProviderCheckpointRecord(
                        provider=name,
                        feed="quota",
                        updated_at=utc_now(),
                        checkpoint=checkpoint,
                    ),
                    wait=True,
                )

            quota.set_persistence(persist_quota)

    async def stop(self) -> None:
        self._stop.set()
        if self._supervisor is not None:
            await self._supervisor
            self._supervisor = None
        if self._pending:
            self._flush_pending()
        await self._persist_provider_checkpoints()
        await self.graph.close()

    async def _run(self) -> None:
        await self._startup_preseed_fx_daily()
        # Publish synthetic inverses and crosses from the restored/seeded USD
        # hubs before readiness is exposed.  Otherwise the first materialize
        # pass waits behind every regular history worker and the API can be
        # temporarily complete for hub pairs but missing long-horizon changes
        # for all derived FX instruments.
        await self._materialize_builtin_fx_history()
        tasks: list[asyncio.Task[Any]] = []
        async with asyncio.TaskGroup() as group:
            tasks.append(group.create_task(self._publish_loop(), name="publish-coalesced"))
            tasks.append(group.create_task(self._quote_scheduler_loop(), name="quote-scheduler"))
            for provider_name, provider in self.graph.providers.items():
                symbols = self._stream_symbols(provider)
                if symbols:
                    tasks.append(
                        group.create_task(
                            self._provider_stream_loop(provider_name, provider, symbols),
                            name=f"stream:{provider_name}",
                        )
                    )
            tasks.append(group.create_task(self._metadata_loop(), name="metadata"))
            tasks.append(group.create_task(self._history_loop(), name="history"))
            tasks.append(group.create_task(self._maintenance_loop(), name="maintenance"))
            self._started.set()
            await self._stop.wait()
            for task in tasks:
                task.cancel()

    async def _startup_preseed_fx_daily(self) -> bool:
        """Boundedly seed all USD-hub daily histories before competing collectors."""

        history = getattr(self.service, "history", None)
        if history is None:
            return True
        hubs = tuple(
            symbol
            for symbol in FX_HUB_SYMBOLS
            if self.registry.resolve(symbol) is not None
            and self.router.configured(symbol, Capability.HISTORY)
        )
        if not hubs:
            return True

        async def seed(symbol: str) -> bool:
            try:
                _, _, complete = await self._backfill_daily_interval(
                    symbol,
                    utc_now(),
                    refresh_complete=False,
                    force_prefix_retry=True,
                )
                self._update_fx_daily_retry_state(symbol, complete)
                if complete:
                    self._last_errors.pop(f"history-daily:{symbol}", None)
                return complete
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fx_daily_retry_symbols.add(symbol)
                self._record_error(f"history-daily:{symbol}", exc)
                return False

        try:
            async with asyncio.timeout(self._fx_startup_preseed_timeout_seconds):
                results = await asyncio.gather(*(seed(symbol) for symbol in hubs))
        except TimeoutError:
            self._record_error(
                "history-daily:startup",
                TimeoutError("FX daily startup preseed exceeded its bounded deadline"),
            )
            return False
        complete = all(results)
        if complete:
            self._last_errors.pop("history-daily:startup", None)
        return complete

    def _update_fx_daily_retry_state(self, symbol: str, complete: bool) -> None:
        retry_at = self._daily_prefix_retry_at.get(symbol, 0.0)
        if complete or time.monotonic() < retry_at:
            self._fx_daily_retry_symbols.discard(symbol)
        else:
            self._fx_daily_retry_symbols.add(symbol)

    def _queue_quote(self, quote: ProviderQuote) -> None:
        self._note_checkpoint(quote)
        current = self._pending.get(quote.symbol)
        if current is None or quote.as_of >= current.as_of:
            self._pending[quote.symbol] = quote

    def _flush_pending(self) -> None:
        pending, self._pending = self._pending, {}
        for quote in pending.values():
            try:
                self.service.publish_quote(quote)
                self._last_errors.pop(f"publish:{quote.symbol}", None)
            except Exception as exc:
                # One malformed provider event must not terminate the TaskGroup
                # and silently take every collector down with it.
                self._mark_source_failed(quote.symbol)
                self._record_error(f"publish:{quote.symbol}", exc)

    async def _publish_loop(self) -> None:
        interval = self.settings.high_frequency_publish_ms / 1000
        while True:
            await asyncio.sleep(interval)
            self._flush_pending()

    async def _quote_scheduler_loop(self) -> None:
        queue: list[tuple[float, int, str]] = []
        sequence = itertools.count()
        current = time.monotonic()
        for symbol in self.registry:
            if self.router.configured(symbol, Capability.QUOTE):
                heapq.heappush(queue, (current, next(sequence), symbol))
        if not queue:
            await self._stop.wait()
            return
        inflight: dict[asyncio.Task[float], str] = {}
        try:
            while True:
                now = time.monotonic()
                while queue and queue[0][0] <= now and len(inflight) < 32:
                    _, _, symbol = heapq.heappop(queue)
                    task = asyncio.create_task(
                        self._poll_quote_once(symbol),
                        name=f"quote:{symbol}",
                    )
                    inflight[task] = symbol

                if not inflight:
                    await asyncio.sleep(min(max(0.0, queue[0][0] - now), 1.0))
                    continue

                timeout = None
                if len(inflight) < 32 and queue:
                    timeout = min(max(0.0, queue[0][0] - now), 1.0)
                done, _ = await asyncio.wait(
                    inflight,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    symbol = inflight.pop(task)
                    interval = task.result()
                    heapq.heappush(
                        queue,
                        (
                            time.monotonic() + max(0.25, interval),
                            next(sequence),
                            symbol,
                        ),
                    )
        finally:
            for task in inflight:
                task.cancel()
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)

    async def _poll_quote_once(self, symbol: str) -> float:
        instrument = self.registry[symbol]
        next_interval = instrument.quote_poll_seconds
        chain = self.router.providers_for(symbol, Capability.QUOTE)
        primary_provider = chain[0] if chain else None
        fallback_probe_seconds = max(
            (
                next_interval,
                *(
                    float(value)
                    for provider in chain
                    if (value := getattr(provider, "minimum_quote_poll_seconds", None)) is not None
                ),
            )
        )
        fallback_closed_seconds = max(
            (
                0.0,
                *(
                    float(value)
                    for provider in chain
                    if (value := getattr(provider, "closed_market_quote_poll_seconds", None))
                    is not None
                ),
            )
        )
        stream_suppression_seconds = min(
            instrument.stale_after_seconds,
            float(getattr(primary_provider, "stream_poll_suppression_seconds", 0.0)),
        )
        last_stream_observation = self._stream_observed_at.get((id(primary_provider), symbol))
        if (
            stream_suppression_seconds > 0
            and last_stream_observation is not None
            and time.monotonic() - last_stream_observation < stream_suppression_seconds
        ):
            return max(
                next_interval,
                float(getattr(primary_provider, "minimum_quote_poll_seconds", 0.0)),
            )
        try:
            quote = self._normalize_market_status(await self.router.get_quote(symbol))
            self._queue_quote(quote)
            self._last_errors.pop(f"quote:{symbol}", None)
            if instrument.market_calendar is MarketCalendar.US_EQUITY:
                selected_provider = self.graph.providers.get(quote.provider) or next(
                    (
                        provider
                        for provider in chain
                        if getattr(provider, "name", None) == quote.provider
                    ),
                    None,
                )
                is_fallback = quote.fallback_level > 0 or (
                    primary_provider is not None
                    and getattr(primary_provider, "name", None) != quote.provider
                )
                if is_fallback:
                    next_interval = max(next_interval, fallback_probe_seconds)
                minimum_poll_seconds = getattr(
                    selected_provider, "minimum_quote_poll_seconds", None
                )
                if minimum_poll_seconds is not None:
                    next_interval = max(next_interval, float(minimum_poll_seconds))
                scheduled_status = scheduled_market_status(
                    instrument.market_calendar,
                    utc_now(),
                )
                if quote.market_status == "closed" and (
                    not is_fallback or scheduled_status == "closed"
                ):
                    next_interval = max(
                        next_interval,
                        fallback_closed_seconds if is_fallback else 0.0,
                        float(
                            getattr(
                                selected_provider,
                                "closed_market_quote_poll_seconds",
                                0.0,
                            )
                        ),
                    )
            elif quote.provider == "coingecko":
                next_interval = max(next_interval, 300.0)
            elif quote.provider == "kraken":
                next_interval = max(next_interval, 2.0)
            if (
                quote.provider == "alpha_vantage"
                and instrument.asset_class is not AssetClass.FX
                and instrument.market_calendar is not MarketCalendar.US_EQUITY
            ):
                next_interval = max(next_interval, 6 * 60 * 60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if instrument.market_calendar is MarketCalendar.US_EQUITY:
                next_interval = max(next_interval, fallback_probe_seconds)
                if scheduled_market_status(instrument.market_calendar, utc_now()) == "closed":
                    next_interval = max(next_interval, fallback_closed_seconds)
            elif (
                instrument.asset_class is not AssetClass.FX
                and isinstance(exc, AllProvidersFailed)
                and any(provider == "alpha_vantage" for provider, _ in exc.attempts)
            ):
                next_interval = max(next_interval, 6 * 60 * 60)
            self._mark_source_failed(symbol)
            self._record_error(f"quote:{symbol}", exc)
        return next_interval

    def _normalize_market_status(self, quote: ProviderQuote) -> ProviderQuote:
        if quote.market_status != "unknown":
            return quote
        instrument = self.registry.resolve(quote.symbol)
        calendar = (
            instrument.market_calendar if instrument is not None else MarketCalendar.ALWAYS_OPEN
        )
        return dataclasses.replace(
            quote,
            market_status=scheduled_market_status(calendar, utc_now()),
        )

    def _stream_symbols(self, provider: Any) -> tuple[str, ...]:
        if not callable(getattr(provider, "stream_quotes", None)):
            return ()
        declared = getattr(provider, "stream_symbols", None)
        if declared is None:
            declared = getattr(provider, "symbols", ())
        values = declared.keys() if hasattr(declared, "keys") else declared
        supported = {str(symbol).strip().upper() for symbol in values}
        return tuple(
            symbol
            for symbol in self.registry
            if symbol in supported
            and (chain := self.router.providers_for(symbol, Capability.QUOTE))
            and chain[0] is provider
        )

    async def _provider_stream_loop(
        self,
        provider_name: str,
        provider: Any,
        symbols: tuple[str, ...],
    ) -> None:
        delay = 1.0
        while True:
            try:
                async for quote in provider.stream_quotes(symbols):
                    delay = 1.0
                    if quote.symbol in self.registry:
                        instrument = self.registry[quote.symbol]
                        source_age = max(0.0, (utc_now() - quote.as_of).total_seconds())
                        observation_key = (id(provider), quote.symbol)
                        if source_age <= instrument.stale_after_seconds:
                            self._stream_observed_at[observation_key] = time.monotonic()
                        else:
                            self._stream_observed_at.pop(observation_key, None)
                        self._queue_quote(quote)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"stream:{provider_name}", exc)
            for symbol in symbols:
                self._stream_observed_at.pop((id(provider), symbol), None)
            self._websocket_reconnects[provider_name] = (
                self._websocket_reconnects.get(provider_name, 0) + 1
            )
            self.service.metrics.websocket_reconnect(provider_name)
            await asyncio.sleep(delay)
            delay = min(60.0, delay * 2)

    async def _metadata_loop(self) -> None:
        next_refresh_at: dict[str, float] = {}
        while True:
            now = time.monotonic()
            due = tuple(
                instrument
                for instrument in self.registry.values()
                if next_refresh_at.get(instrument.symbol, 0.0) <= now
            )
            instruments = iter(due)

            async def worker(items: Any = instruments) -> None:
                for instrument in items:
                    retry_early = await self._refresh_metadata(instrument)
                    delay = (
                        self.settings.metadata_retry_seconds
                        if retry_early
                        else self.settings.metadata_poll_seconds
                    )
                    next_refresh_at[instrument.symbol] = time.monotonic() + max(1.0, delay)

            if due:
                async with asyncio.TaskGroup() as group:
                    for index in range(min(4, len(due))):
                        group.create_task(worker(), name=f"metadata-worker:{index}")

            upcoming = tuple(
                next_refresh_at.get(instrument.symbol, time.monotonic())
                for instrument in self.registry.values()
            )
            if not upcoming:
                await asyncio.sleep(max(1.0, self.settings.metadata_poll_seconds))
                continue
            await asyncio.sleep(max(1.0, min(upcoming) - time.monotonic()))

    async def _refresh_metadata(self, instrument: Any) -> bool:
        symbol = instrument.symbol
        retry_early = False
        if (
            instrument.dividend_strategy is not None
            or instrument.yield_strategy is YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED
        ) and self.router.configured(symbol, Capability.DIVIDEND):
            try:
                event = await self.router.get_latest_dividend(symbol)
                if event is not None:
                    self.service.publish_dividend(event)
                self._last_errors.pop(f"dividend:{symbol}", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retry_early = True
                self._record_error(f"dividend:{symbol}", exc)
        if (
            instrument.yield_strategy is not None
            and instrument.yield_strategy is not YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED
            and self.router.configured(symbol, Capability.YIELD)
        ):
            try:
                metric = await self.router.get_yield(symbol)
                self.service.publish_yield_metric(metric)
                self._last_errors.pop(f"yield:{symbol}", None)
                retry_early = retry_early or (
                    metric.is_proxy
                    or metric.fallback_level > 0
                    or bool(metric.quality and metric.quality.stale)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retry_early = True
                self._record_error(f"yield:{symbol}", exc)
        return retry_early

    async def _history_loop(self) -> None:
        next_full_refresh_at = 0.0
        while True:
            now = time.monotonic()
            include_fx = now >= self._next_fx_history_refresh_at
            full_refresh = now >= next_full_refresh_at
            if not include_fx and not full_refresh:
                await asyncio.sleep(
                    max(
                        1.0,
                        min(self._next_fx_history_refresh_at, next_full_refresh_at) - now,
                    )
                )
                continue

            self._history_full_cycle = full_refresh
            fx_history_complete = await self._backfill_history(include_fx=include_fx)
            completed_at = time.monotonic()
            if full_refresh:
                next_full_refresh_at = completed_at + self.settings.history_poll_seconds
            if include_fx:
                self._fx_history_retry_only = fx_history_complete is False
                retry_after = (
                    FX_HISTORY_REFRESH_SECONDS
                    if fx_history_complete is not False
                    else FX_HISTORY_RETRY_SECONDS
                )
                self._next_fx_history_refresh_at = completed_at + retry_after
            await asyncio.sleep(
                max(
                    1.0,
                    min(self._next_fx_history_refresh_at, next_full_refresh_at) - time.monotonic(),
                )
            )

    async def _backfill_history(self, *, include_fx: bool) -> bool:
        retry_only = self._fx_history_retry_only
        selected_symbols = tuple(
            instrument.symbol
            for instrument in self.registry.values()
            if instrument.history_enabled
            and (
                (
                    self._history_full_cycle
                    and (
                        instrument.asset_class is not AssetClass.FX
                        or instrument.symbol not in FX_SYMBOLS
                    )
                )
                or (
                    include_fx
                    and instrument.symbol in FX_HUB_SYMBOLS
                    and (
                        not retry_only
                        or (
                            instrument.symbol in self._fx_daily_retry_symbols
                            or any(
                                failed_symbol == instrument.symbol
                                for failed_symbol, _ in self._fx_failed_history_intervals
                            )
                        )
                    )
                )
            )
        )
        fx_hubs = tuple(symbol for symbol in selected_symbols if symbol in FX_HUB_SYMBOLS)
        self._fx_history_intervals_for_cycle = {
            symbol: tuple(
                interval
                for interval in ("1m", "5m")
                if not retry_only or (symbol, interval) in self._fx_failed_history_intervals
            )
            for symbol in fx_hubs
        }
        self._daily_preseeded_symbols = set(fx_hubs)
        if include_fx:
            daily_targets = tuple(
                symbol
                for symbol in fx_hubs
                if not retry_only or symbol in self._fx_daily_retry_symbols
            )
            # Daily work precedes minute bars. A compact fallback or genuinely
            # short history installs its own 24-hour prefix backoff; only a
            # request that produced no usable prefix remains on the 60-second
            # transient retry path.
            for symbol in daily_targets:
                try:
                    _, _, complete = await self._backfill_daily_interval(
                        symbol,
                        utc_now(),
                        refresh_complete=False,
                    )
                    self._update_fx_daily_retry_state(symbol, complete)
                    if complete:
                        self._last_errors.pop(f"history-daily:{symbol}", None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._fx_daily_retry_symbols.add(symbol)
                    self._record_error(f"history-daily:{symbol}", exc)

        symbols = iter(selected_symbols)

        async def worker() -> None:
            for symbol in symbols:
                attempted_fx_intervals = self._fx_history_intervals_for_cycle.get(symbol)
                if symbol in fx_hubs and not attempted_fx_intervals:
                    continue
                try:
                    interval_errors = await self._backfill_symbol(symbol)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if attempted_fx_intervals is not None:
                        self._fx_failed_history_intervals.update(
                            (symbol, interval) for interval in attempted_fx_intervals
                        )
                    self._record_error(f"history:{symbol}", exc)
                    continue

                if attempted_fx_intervals is None:
                    self._last_errors.pop(f"history:{symbol}", None)
                    continue
                errors = interval_errors if isinstance(interval_errors, dict) else {}
                for interval in attempted_fx_intervals:
                    key = (symbol, interval)
                    if interval in errors:
                        self._fx_failed_history_intervals.add(key)
                    else:
                        self._fx_failed_history_intervals.discard(key)
                if errors:
                    self._record_error(f"history:{symbol}", next(iter(errors.values())))
                else:
                    self._last_errors.pop(f"history:{symbol}", None)

        try:
            async with asyncio.TaskGroup() as group:
                for index in range(min(2, len(self.registry))):
                    group.create_task(worker(), name=f"history-worker:{index}")
            if include_fx:
                await self._materialize_builtin_fx_history()
        finally:
            self._daily_preseeded_symbols.clear()
            self._fx_history_intervals_for_cycle.clear()
        return not self._fx_daily_retry_symbols and not self._fx_failed_history_intervals

    async def _materialize_builtin_fx_history(self) -> None:
        """Build every public FX inverse and cross from cached USD-spoke rings."""

        history = getattr(self.service, "history", None)
        publish = getattr(self.service, "publish_history_async", None)
        if history is None or not callable(publish):
            return
        points_for_interval = getattr(history, "points_for_interval", None)
        if not callable(points_for_interval):
            return
        daily_analytics_start = utc_now() - timedelta(days=365)

        for symbol in FX_SYMBOLS:
            instrument = self.registry.resolve(symbol)
            if instrument is None or instrument.symbol != symbol or symbol in FX_HUB_SYMBOLS:
                continue
            requirements = fx_hub_requirements(symbol)
            _, quote_currency = symbol.split(":", 1)
            derived: list[PricePoint] = []
            for interval in ("1m", "5m", "1d"):
                step = {
                    "1m": timedelta(minutes=1),
                    "5m": timedelta(minutes=5),
                    "1d": timedelta(days=1),
                }[interval]
                existing = points_for_interval(symbol, interval)
                cutoff = max((item.timestamp for item in existing), default=None)
                if (
                    interval == "1d"
                    and existing
                    and min(item.timestamp for item in existing) > daily_analytics_start
                ):
                    # A restored synthetic tail must not hide an older prefix
                    # that became available after the USD hubs were backfilled.
                    # Rebuild the complete daily ring until it can support the
                    # rolling 365-day reference, then resume tail updates.
                    cutoff = None
                if cutoff is not None:
                    cutoff -= step
                numerator = tuple(
                    item
                    for item in points_for_interval(requirements[0], interval)
                    if cutoff is None or item.timestamp >= cutoff
                )
                if quote_currency == "USD":
                    derived.extend(
                        PricePoint(
                            symbol=symbol,
                            timestamp=item.timestamp,
                            price=Decimal(1) / item.price,
                            provider="synthetic_fx",
                            is_derived=True,
                            interval=interval,
                        )
                        for item in numerator
                    )
                    continue
                denominator = tuple(
                    item
                    for item in points_for_interval(requirements[1], interval)
                    if cutoff is None or item.timestamp >= cutoff - FX_MAX_SKEW
                )
                derived.extend(
                    derive_cross_history(
                        symbol,
                        numerator,
                        denominator,
                        operation="divide",
                        max_skew=FX_MAX_SKEW,
                        provider="synthetic_fx",
                        interval=interval,
                    )
                )
            if derived:
                await publish(derived, persist=False)

    async def _backfill_symbol(self, symbol: str) -> dict[str, BaseException]:
        instrument = self.registry[symbol]
        equity_symbol = instrument.asset_class in {AssetClass.EQUITY, AssetClass.BOND}
        if equity_symbol:
            last_fallback = self._equity_history_fallback_at.get(symbol)
            if last_fallback is not None and time.monotonic() - last_fallback < 24 * 60 * 60:
                return {}
        now = utc_now()
        used_sparse_fallback = False
        last_error: BaseException | None = None
        published_any = False
        interval_errors: dict[str, BaseException] = {}
        requested_intervals = self._fx_history_intervals_for_cycle.get(symbol, ("1m", "5m"))
        for interval, duration in (("1m", timedelta(hours=48)), ("5m", timedelta(days=45))):
            if interval not in requested_intervals:
                continue
            retention_start = now - duration
            existing = self.service.history.points_for_interval(symbol, interval)
            step = timedelta(minutes=1 if interval == "1m" else 5)
            if (
                existing
                and min(point.timestamp for point in existing) <= retention_start + step * 2
            ):
                start = max(retention_start, max(point.timestamp for point in existing) - step)
            else:
                start = retention_start
            if not self.router.configured(symbol, Capability.HISTORY):
                continue
            try:
                fetched = await self._fetch_history_pages(symbol, interval, start, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                interval_errors[interval] = exc
                continue
            published_any = published_any or bool(fetched)
            if equity_symbol:
                if any(point.provider != "alpaca" for point in fetched):
                    used_sparse_fallback = True
        if symbol not in self._daily_preseeded_symbols:
            try:
                daily_published, daily_sparse, _ = await self._backfill_daily_interval(symbol, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                interval_errors["1d"] = exc
            else:
                published_any = published_any or daily_published
                used_sparse_fallback = used_sparse_fallback or (equity_symbol and daily_sparse)
        if used_sparse_fallback:
            self._equity_history_fallback_at[symbol] = time.monotonic()
        if (
            last_error is not None
            and not published_any
            and instrument.asset_class is not AssetClass.FX
        ):
            raise last_error
        return interval_errors

    async def _backfill_daily_interval(
        self,
        symbol: str,
        now: datetime,
        *,
        refresh_complete: bool = True,
        force_prefix_retry: bool = False,
    ) -> tuple[bool, bool, bool]:
        existing_daily = self.service.history.points_for_interval(symbol, "1d")
        daily_retention_start = now - timedelta(days=400)
        daily_analytics_start = now - timedelta(days=365)
        daily_step = timedelta(days=1)
        # A recent suffix can be restored from SQLite after an earlier sparse
        # fallback. Do not mistake it for a completed backfill: a 1Y change
        # requires an observation at or before the exact 365-day cutoff. Once
        # that cutoff is covered, subsequent cycles only overlap the newest
        # day; recent listings re-probe their unavailable prefix once per day.
        daily_prefix_complete = (
            bool(existing_daily)
            and min(point.timestamp for point in existing_daily) <= daily_analytics_start
        )
        prefix_retry_due = time.monotonic() >= self._daily_prefix_retry_at.get(symbol, 0.0)
        daily_start = (
            max(
                daily_retention_start,
                max(point.timestamp for point in existing_daily) - daily_step,
            )
            if daily_prefix_complete
            else daily_retention_start
        )
        if not self.router.configured(symbol, Capability.HISTORY):
            return False, False, daily_prefix_complete
        if daily_prefix_complete and not refresh_complete:
            return False, False, True
        if not daily_prefix_complete and not prefix_retry_due and not force_prefix_retry:
            return False, False, False

        daily = await self._fetch_history_pages(symbol, "1d", daily_start, now)
        used_sparse_fallback = any(point.provider != "alpaca" for point in daily)
        combined_daily = (*existing_daily, *daily)
        complete = (
            bool(combined_daily)
            and min(point.timestamp for point in combined_daily) <= daily_analytics_start
        )
        if complete:
            self._daily_prefix_retry_at.pop(symbol, None)
        elif combined_daily:
            self._daily_prefix_retry_at[symbol] = time.monotonic() + DAILY_PREFIX_RETRY_SECONDS
        return bool(daily), used_sparse_fallback, complete

    async def _fetch_history_pages(
        self, symbol: str, interval: str, start: datetime, end: datetime
    ) -> tuple[PricePoint, ...]:
        cursor = start
        try:
            step = {
                "1m": timedelta(minutes=1),
                "5m": timedelta(minutes=5),
                "1d": timedelta(days=1),
            }[interval]
        except KeyError as exc:
            raise ValueError(f"unsupported backfill interval: {interval}") from exc
        # Twelve Data applies outputsize from the end of a range. Keeping each
        # forward window at <=5000 bars prevents an old prefix from being
        # silently discarded while remaining compatible with smaller provider
        # page limits (for example Binance's 1000 klines).
        page_bar_limit = 5_000
        output: list[PricePoint] = []
        for _ in range(64):
            page_end = min(end, cursor + step * (page_bar_limit - 1))
            page = tuple(
                await self.router.get_history(
                    symbol,
                    interval=interval,
                    start=cursor,
                    end=page_end,
                    limit=page_bar_limit,
                )
            )
            page = tuple(point for point in page if cursor <= point.timestamp <= page_end)
            if not page:
                if page_end >= end:
                    break
                cursor = page_end + step
                continue
            output.extend(page)
            if interval == "1d":
                # Daily adapters return their requested window in one response.
                # Repeating the request can consume scarce fallback credits.
                break
            latest = max(point.timestamp for point in page)
            next_cursor = latest + step
            if next_cursor <= cursor:
                break
            if next_cursor > page_end:
                next_cursor = page_end + step
            if next_cursor > end:
                break
            cursor = next_cursor
        deduplicated = {point.timestamp: point for point in output}
        result = tuple(deduplicated[key] for key in sorted(deduplicated))
        if result:
            await self.service.publish_history_async(list(result))
        return result

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            await self._update_quota_metrics()
            await self._persist_provider_checkpoints()
            storage = getattr(self.service, "_storage", None)
            if storage is not None:
                try:
                    await storage.cleanup()
                    await storage.checkpoint("PASSIVE")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._record_error("sqlite:maintenance", exc)
            await asyncio.sleep(6 * 60 * 60)

    async def _update_quota_metrics(self) -> None:
        result: dict[str, dict[str, Any]] = {}
        for name, provider in self.graph.providers.items():
            quota = getattr(provider, "quota", None)
            if quota is None:
                continue
            try:
                value = await quota.snapshot()
                result[name] = dataclasses.asdict(value)
            except Exception as exc:
                self._record_error(f"quota:{name}", exc)
        self._quota_snapshots = result

    def _note_checkpoint(self, quote: ProviderQuote) -> None:
        key = (quote.provider, quote.feed)
        symbols = self._checkpoint_state.setdefault(key, {})
        previous = symbols.get(quote.symbol)
        as_of = quote.as_of.isoformat().replace("+00:00", "Z")
        if previous is None or as_of >= previous:
            symbols[quote.symbol] = as_of

    async def _persist_provider_checkpoints(self) -> None:
        storage = getattr(self.service, "_storage", None)
        if storage is None:
            return
        from .storage import ProviderCheckpointRecord

        updated_at = utc_now()
        for (provider, feed), symbols in self._checkpoint_state.items():
            try:
                await storage.enqueue_checkpoint_record(
                    ProviderCheckpointRecord(
                        provider=provider,
                        feed=feed,
                        updated_at=updated_at,
                        checkpoint={"symbols": dict(symbols)},
                    ),
                    wait=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"checkpoint:{provider}:{feed}", exc)
        for provider_name, provider in self.graph.providers.items():
            quota = getattr(provider, "quota", None)
            if quota is None:
                continue
            try:
                await storage.enqueue_checkpoint_record(
                    ProviderCheckpointRecord(
                        provider=provider_name,
                        feed="quota",
                        updated_at=updated_at,
                        checkpoint=await quota.checkpoint(),
                    ),
                    wait=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"checkpoint:{provider_name}:quota", exc)

    def _record_error(self, job: str, exc: BaseException) -> None:
        if isinstance(exc, AllProvidersFailed):
            reason = exc.message
        elif isinstance(exc, ProviderError):
            reason = exc.message
        else:
            reason = type(exc).__name__
        self._last_errors[job] = {"reason": reason, "at": utc_now().isoformat()}

    def _mark_source_failed(self, *symbols: str) -> None:
        marker = getattr(self.service, "mark_source_failed", None)
        if callable(marker):
            marker(*symbols)

    def metrics(self) -> dict[str, Any]:
        return {
            "running": self.is_running,
            "fatal_error": (
                None
                if self._fatal_error is None
                else {"type": type(self._fatal_error).__name__, "message": str(self._fatal_error)}
            ),
            "fallback_counts": self.router.fallback_counts(),
            "circuits": [dataclasses.asdict(item) for item in self.router.circuit_snapshots()],
            "quota": self._quota_snapshots,
            "websocket_reconnects": dict(self._websocket_reconnects),
            "last_errors": dict(self._last_errors),
        }
