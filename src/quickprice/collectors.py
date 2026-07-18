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
                selected_provider = self.graph.providers.get(quote.provider)
                minimum_poll_seconds = getattr(
                    selected_provider, "minimum_quote_poll_seconds", None
                )
                if minimum_poll_seconds is not None:
                    next_interval = max(next_interval, float(minimum_poll_seconds))
                elif "alpaca" not in self.graph.providers or quote.provider != "alpaca":
                    next_interval = max(next_interval, 24 * 60 * 60)
                if quote.market_status == "closed":
                    next_interval = max(
                        next_interval,
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
            if quote.provider == "alpha_vantage":
                next_interval = max(next_interval, 6 * 60 * 60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, AllProvidersFailed) and any(
                provider == "alpha_vantage" for provider, _ in exc.attempts
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
        while True:
            instruments = iter(self.registry.values())

            async def worker(items: Any = instruments) -> None:
                for instrument in items:
                    await self._refresh_metadata(instrument)

            async with asyncio.TaskGroup() as group:
                for index in range(min(4, len(self.registry))):
                    group.create_task(worker(), name=f"metadata-worker:{index}")
            await asyncio.sleep(self.settings.metadata_poll_seconds)

    async def _refresh_metadata(self, instrument: Any) -> None:
        symbol = instrument.symbol
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"yield:{symbol}", exc)

    async def _history_loop(self) -> None:
        while True:
            include_fx = time.monotonic() >= self._next_fx_history_refresh_at
            await self._backfill_history(include_fx=include_fx)
            if include_fx:
                self._next_fx_history_refresh_at = time.monotonic() + FX_HISTORY_REFRESH_SECONDS
            until_fx = max(1.0, self._next_fx_history_refresh_at - time.monotonic())
            await asyncio.sleep(min(self.settings.history_poll_seconds, until_fx))

    async def _backfill_history(self, *, include_fx: bool) -> None:
        symbols = iter(
            instrument.symbol
            for instrument in self.registry.values()
            if instrument.history_enabled
            and (
                instrument.asset_class is not AssetClass.FX
                or (
                    include_fx
                    and (instrument.symbol not in FX_SYMBOLS or instrument.symbol in FX_HUB_SYMBOLS)
                )
            )
        )

        async def worker() -> None:
            for symbol in symbols:
                try:
                    await self._backfill_symbol(symbol)
                    self._last_errors.pop(f"history:{symbol}", None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._record_error(f"history:{symbol}", exc)

        async with asyncio.TaskGroup() as group:
            for index in range(min(2, len(self.registry))):
                group.create_task(worker(), name=f"history-worker:{index}")
        if include_fx:
            await self._materialize_builtin_fx_history()

    async def _materialize_builtin_fx_history(self) -> None:
        """Build every public FX inverse and cross from cached USD-spoke rings."""

        history = getattr(self.service, "history", None)
        publish = getattr(self.service, "publish_history_async", None)
        if history is None or not callable(publish):
            return
        points_for_interval = getattr(history, "points_for_interval", None)
        if not callable(points_for_interval):
            return

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

    async def _backfill_symbol(self, symbol: str) -> None:
        instrument = self.registry[symbol]
        equity_symbol = instrument.asset_class in {AssetClass.EQUITY, AssetClass.BOND}
        if equity_symbol:
            last_fallback = self._equity_history_fallback_at.get(symbol)
            if last_fallback is not None and time.monotonic() - last_fallback < 24 * 60 * 60:
                return
        now = utc_now()
        used_sparse_fallback = False
        last_error: BaseException | None = None
        published_any = False
        for interval, duration in (("1m", timedelta(hours=48)), ("5m", timedelta(days=45))):
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
                continue
            published_any = published_any or bool(fetched)
            if equity_symbol:
                if any(point.provider != "alpaca" for point in fetched):
                    used_sparse_fallback = True
        existing_daily = self.service.history.points_for_interval(symbol, "1d")
        daily_retention_start = now - timedelta(days=400)
        daily_start = (
            max(
                daily_retention_start,
                max(point.timestamp for point in existing_daily) - timedelta(days=1),
            )
            if existing_daily
            else daily_retention_start
        )
        if self.router.configured(symbol, Capability.HISTORY):
            try:
                daily = await self._fetch_history_pages(symbol, "1d", daily_start, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
            else:
                published_any = published_any or bool(daily)
                if any(point.provider != "alpaca" for point in daily):
                    used_sparse_fallback = True
        if used_sparse_fallback:
            self._equity_history_fallback_at[symbol] = time.monotonic()
        if last_error is not None and not published_any:
            raise last_error

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
