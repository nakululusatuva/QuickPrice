"""Cache-first application service; HTTP handlers never wait on I/O."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from .analytics import boxx_yield, calculate_changes, quarterly_dividend, sgov_yield
from .cache import HistoryCache, SnapshotStore
from .config import Settings
from .domain import (
    AggregatePrice,
    DividendEvent,
    PricePoint,
    ProviderQuote,
    QuoteSnapshot,
    YieldMetric,
    utc_now,
)
from .market import most_recent_scheduled_close, scheduled_market_status
from .metrics import Metrics
from .plugin_api import AssetClass, YieldStrategy
from .registry import InstrumentRegistry, build_registry
from .runtime import FreeThreadedStatus, inspect_free_threaded_runtime
from .schemas import QualityModel, QuoteModel, snapshot_to_wire
from .staking import (
    ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS,
    ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD,
    estimate_from_staking_metric,
)

_LOGGER = logging.getLogger(__name__)


class DataUnavailableError(Exception):
    def __init__(self, symbol: str, reason: str, code: str = "data_unavailable") -> None:
        super().__init__(reason)
        self.symbol = symbol
        self.reason = reason
        self.code = code


class QuickPriceService:
    def __init__(
        self,
        settings: Settings,
        registry: InstrumentRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.registry = build_registry(settings.enabled_plugins) if registry is None else registry
        self.snapshots = SnapshotStore()
        self.history = HistoryCache()
        self.metrics = Metrics()
        self.runtime_status: FreeThreadedStatus | None = None
        self._metadata_lock = RLock()
        self._dividends: dict[str, DividendEvent] = {}
        self._yield_metrics: dict[str, YieldMetric] = {}
        self._yield_stale_after_seconds: dict[str, float] = {}
        self._last_quotes: dict[str, ProviderQuote] = {}
        self._source_failures: set[str] = set()
        self._wire_cache: dict[str, QuoteModel] = {}
        self._complete_symbols: set[str] = set()
        self._active_aggregates: dict[str, AggregatePrice] = {}
        self._provider_checkpoints: dict[tuple[str, str], Any] = {}
        self._storage: Any = None
        self._coordinator: Any = None
        self._collector_start_error: BaseException | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._persistence_tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        self._storage_ready = False
        self._api_key_configured: Any = None

    @property
    def storage(self) -> Any:
        """Expose the initialized persistence boundary to control-plane managers."""

        return self._storage

    def bind_api_key_state(self, configured: Any) -> None:
        """Bind a zero-I/O callable used by readiness after durable key bootstrap."""

        if not callable(configured):
            raise TypeError("configured must be callable")
        self._api_key_configured = configured

    def _has_active_api_key(self) -> bool:
        if self._api_key_configured is not None:
            return bool(self._api_key_configured())
        return bool(self.settings.api_key_hashes)

    async def start(self) -> None:
        if self._started:
            return
        # Import the complete production dependency graph before inspecting the
        # GIL. An incompatible extension can enable the GIL during import.
        import aiohttp
        import fastapi
        import pydantic
        import uvicorn

        from . import providers, storage

        _ = (aiohttp, fastapi, pydantic, uvicorn, providers, storage)
        await self._start_storage()
        if self.settings.background_enabled:
            await self._start_collectors()
        self.runtime_status = inspect_free_threaded_runtime()
        self._tasks.append(asyncio.create_task(self._monitor_event_loop(), name="event-loop-lag"))
        self._started = True

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._coordinator is not None:
            stop = getattr(self._coordinator, "stop", None)
            if stop is not None:
                await stop()
        if self._persistence_tasks:
            await asyncio.gather(*tuple(self._persistence_tasks), return_exceptions=True)
        if self._storage is not None:
            stop = getattr(self._storage, "stop", None)
            if stop is not None:
                result = stop()
                if asyncio.iscoroutine(result):
                    await result
        self._started = False

    async def _start_storage(self) -> None:
        """Import lazily so storage remains independently contract-testable."""
        stage = "initialize"
        try:
            from .storage import SQLiteStorage

            self._storage = SQLiteStorage(
                self.settings.database_path,
                batch_size=self.settings.sqlite_batch_size,
                batch_interval_ms=self.settings.sqlite_batch_ms,
            )
            stage = "start"
            result = self._storage.start()
            if asyncio.iscoroutine(result):
                await result
            stage = "restore"
            restore = getattr(self._storage, "restore", None)
            if restore is not None:
                restored = restore()
                if asyncio.iscoroutine(restored):
                    restored = await restored
                stage = "apply_restore"
                await self._apply_restored_state(restored)
            self._storage_ready = True
        except Exception as exc:
            # The API can still serve an already-populated memory cache, but
            # readiness exposes the failed durability invariant.
            self._storage_ready = False
            # Exception messages can contain database paths or upstream values.
            # Keep the dashboard event diagnostic but deliberately message-free.
            _LOGGER.error(
                "Storage startup failed stage=%s error_type=%s",
                stage,
                type(exc).__name__,
            )

    async def _apply_restored_state(self, restored: Any) -> None:
        if restored is None:
            return
        restored = self._filter_restored_state(restored)
        points = getattr(restored, "price_points", None) or getattr(restored, "history", None)
        if points:
            await asyncio.to_thread(
                self.history.load,
                [point for point in points if self._is_active_symbol(point.symbol)],
            )
        dividends = getattr(restored, "dividends", None)
        if dividends:
            for event in dividends:
                if isinstance(event, DividendEvent) and self._is_active_symbol(event.symbol):
                    self._dividends[event.symbol] = event
        yield_records = {
            record.symbol: record
            for record in (getattr(restored, "yield_metric_records", None) or ())
            if self._is_active_symbol(record.symbol)
        }
        yields = getattr(restored, "yield_metrics", None)
        if yields:
            for metric in yields:
                if isinstance(metric, YieldMetric) and self._is_active_symbol(metric.symbol):
                    self._yield_metrics[metric.symbol] = metric
                    record = yield_records.get(metric.symbol)
                    persisted_threshold = (
                        record.raw.get("stale_after_seconds") if record is not None else None
                    )
                    self._yield_stale_after_seconds[metric.symbol] = (
                        self._restored_yield_stale_after_seconds(
                            metric,
                            persisted_threshold,
                        )
                    )
        checkpoints = getattr(restored, "provider_checkpoints", None)
        if checkpoints:
            active_checkpoints = filter(
                None, (self._filter_checkpoint(item) for item in checkpoints)
            )
            self._provider_checkpoints = {
                (item.provider, item.feed): item for item in active_checkpoints
            }
        quotes = getattr(restored, "quotes", None)
        if quotes:
            for quote in quotes:
                if isinstance(quote, ProviderQuote) and self._is_active_symbol(quote.symbol):
                    self.publish_quote(quote, persist=False)

    def _is_active_symbol(self, symbol: Any) -> bool:
        """Require a persisted symbol to remain a canonical active instrument."""

        return isinstance(symbol, str) and symbol in self.registry.symbols

    def _filter_restored_state(self, restored: Any) -> Any:
        """Remove records owned by disabled plugins before domain reconstruction."""

        if not dataclasses.is_dataclass(restored) or isinstance(restored, type):
            return restored
        active = set(self.registry.symbols)
        updates: dict[str, Any] = {}
        for field_name in ("minute_prices", "aggregate_prices", "latest_snapshots"):
            records = getattr(restored, field_name, None)
            if records is not None:
                updates[field_name] = tuple(
                    record for record in records if getattr(record, "symbol", None) in active
                )
        for field_name in ("dividend_events", "yield_metric_records"):
            records = getattr(restored, field_name, None)
            if records is not None:
                updates[field_name] = tuple(
                    record for record in records if getattr(record, "symbol", None) in active
                )
        checkpoints = getattr(restored, "provider_checkpoints", None)
        if checkpoints is not None:
            updates["provider_checkpoints"] = tuple(
                item
                for record in checkpoints
                if (item := self._filter_checkpoint(record)) is not None
            )
        return dataclasses.replace(restored, **updates) if updates else restored

    def _filter_checkpoint(self, record: Any) -> Any | None:
        """Strip disabled symbols from symbol-scoped provider checkpoints."""

        checkpoint = getattr(record, "checkpoint", None)
        if not isinstance(checkpoint, Mapping):
            return None
        value = dict(checkpoint)
        symbols = value.get("symbols")
        if symbols is not None:
            if not isinstance(symbols, Mapping):
                return None
            filtered = {
                symbol: state for symbol, state in symbols.items() if self._is_active_symbol(symbol)
            }
            if not filtered:
                return None
            value["symbols"] = filtered
        scoped_symbol = value.get("symbol")
        if scoped_symbol is not None and not self._is_active_symbol(scoped_symbol):
            return None
        if value == dict(checkpoint):
            return record
        try:
            return dataclasses.replace(record, checkpoint=value)
        except TypeError, ValueError:
            return None

    async def _start_collectors(self) -> None:
        try:
            from .collectors import MarketDataCoordinator

            self._coordinator = MarketDataCoordinator(self, self.settings, self.registry)
            await self._coordinator.start()
            self._collector_start_error = None
            self._tasks.append(
                asyncio.create_task(
                    self._monitor_collector_run(self._coordinator),
                    name="collector-runtime-monitor",
                )
            )
        except Exception as exc:
            # Missing credentials are expected during first boot; readiness lists
            # the unavailable instruments instead of fabricating data.
            self._coordinator = None
            self._collector_start_error = exc
            # Do not include exception text: plugin/provider exceptions may embed
            # authenticated URLs or credential-bearing configuration values.
            _LOGGER.error(
                "Collector startup failed error_type=%s",
                type(exc).__name__,
            )

    @staticmethod
    async def _monitor_collector_run(coordinator: Any) -> None:
        """Report one terminal coordinator failure without exposing its message."""

        supervisor = getattr(coordinator, "_supervisor", None)
        if not isinstance(supervisor, asyncio.Task):
            return
        failure: BaseException | None = None
        try:
            await asyncio.shield(supervisor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
        failure = getattr(coordinator, "fatal_error", None) or failure
        if failure is not None:
            _LOGGER.error(
                "Collector runtime failed error_type=%s",
                type(failure).__name__,
            )

    async def _monitor_event_loop(self) -> None:
        interval = 1.0
        loop = asyncio.get_running_loop()
        expected = loop.time() + interval
        while True:
            await asyncio.sleep(interval)
            now = loop.time()
            self.metrics.set_event_loop_lag((now - expected) * 1000)
            expected = now + interval

    def publish_history(self, points: list[PricePoint], *, persist: bool = True) -> None:
        self._validate_history_timestamps(points)
        self.history.load(points)
        self._finish_history_publication(points, persist=persist)

    async def publish_history_async(
        self, points: list[PricePoint], *, persist: bool = True
    ) -> None:
        """Merge a large backfill without blocking the HTTP event loop."""

        self._validate_history_timestamps(points)
        await asyncio.to_thread(self.history.load, points)
        self._finish_history_publication(points, persist=persist)

    def _finish_history_publication(self, points: list[PricePoint], *, persist: bool) -> None:
        if persist and points:
            self._persist_history(points)
        for symbol in {point.symbol for point in points}:
            self._rebuild(symbol, persist=False)

    def publish_quote(self, quote: ProviderQuote, *, persist: bool = True) -> None:
        instrument = self.registry.resolve(quote.symbol)
        if instrument is None or quote.symbol != instrument.symbol:
            raise ValueError(f"unsupported symbol: {quote.symbol}")
        if quote.as_of > utc_now() + timedelta(seconds=60):
            raise ValueError("quote timestamp is more than 60 seconds in the future")
        with self._metadata_lock:
            current = self._last_quotes.get(quote.symbol)
            if current is not None and quote.as_of < current.as_of:
                return
            self._last_quotes[quote.symbol] = quote
            self._source_failures.discard(quote.symbol)
        minute = quote.as_of.replace(second=0, microsecond=0)
        point = PricePoint(
            quote.symbol,
            minute,
            quote.price,
            quote.provider,
            quote.is_derived,
            "1m",
        )
        self.history.add(point)
        self._rebuild(quote.symbol, persist=persist)
        if persist:
            self._persist("enqueue_price", point)
            self._persist("enqueue_aggregate_price", self._update_aggregate(point))

    def _validate_history_timestamps(self, points: list[PricePoint]) -> None:
        future_limit = utc_now() + timedelta(seconds=60)
        if any(point.timestamp > future_limit for point in points):
            raise ValueError("history contains a timestamp more than 60 seconds in the future")
        invalid = [
            point.symbol
            for point in points
            if (item := self.registry.resolve(point.symbol)) is None or item.symbol != point.symbol
        ]
        if invalid:
            raise ValueError(
                f"history contains unsupported symbols: {', '.join(sorted(set(invalid)))}"
            )

    def restored_provider_checkpoints(self) -> dict[tuple[str, str], Any]:
        return dict(self._provider_checkpoints)

    @staticmethod
    def _five_minute_bucket(timestamp: datetime) -> datetime:
        minute = timestamp.minute - timestamp.minute % 5
        return timestamp.replace(minute=minute, second=0, microsecond=0)

    @classmethod
    def _aggregate_bucket(cls, point: PricePoint) -> tuple[datetime, int]:
        if point.interval == "5m":
            return cls._five_minute_bucket(point.timestamp), 300
        if point.interval == "1d":
            return point.timestamp, 86_400
        raise ValueError(f"unsupported aggregate interval: {point.interval}")

    def _update_aggregate(self, point: PricePoint) -> AggregatePrice:
        bucket = self._five_minute_bucket(point.timestamp)
        current = self._active_aggregates.get(point.symbol)
        if current is None or current.bucket_start != bucket:
            aggregate = AggregatePrice(
                point.symbol,
                bucket,
                300,
                point.price,
                point.price,
                point.price,
                point.price,
                1,
                point.provider,
                point.is_derived,
            )
        else:
            aggregate = AggregatePrice(
                point.symbol,
                bucket,
                300,
                current.open,
                max(current.high, point.price),
                min(current.low, point.price),
                point.price,
                current.sample_count + 1,
                point.provider,
                point.is_derived,
            )
        self._active_aggregates[point.symbol] = aggregate
        return aggregate

    def publish_dividend(self, event: DividendEvent, *, persist: bool = True) -> None:
        instrument = self.registry.resolve(event.symbol)
        if (
            instrument is None
            or event.symbol != instrument.symbol
            or (
                instrument.dividend_strategy is None
                and instrument.yield_strategy is not YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED
            )
        ):
            raise ValueError(f"dividend is not configured for {event.symbol}")
        if event.event_type != "regular_cash":
            return
        with self._metadata_lock:
            current = self._dividends.get(event.symbol)
            if current is not None and event.ex_date < current.ex_date:
                return
            self._dividends[event.symbol] = event
        self._rebuild(event.symbol, persist=persist)
        if persist:
            self._persist("enqueue_dividend", event)

    def publish_yield_metric(self, metric: YieldMetric, *, persist: bool = True) -> None:
        instrument = self.registry.resolve(metric.symbol)
        if (
            instrument is None
            or metric.symbol != instrument.symbol
            or instrument.yield_strategy is None
        ):
            raise ValueError(f"external yield metric is not configured for {metric.symbol}")
        stale_after_seconds = self._default_yield_stale_after_seconds(metric)
        with self._metadata_lock:
            current = self._yield_metrics.get(metric.symbol)
            if current is not None:
                same_source = (
                    metric.provider.casefold() == current.provider.casefold()
                    and metric.method == current.method
                )
                current_rank = self._yield_source_rank(current)
                incoming_rank = self._yield_source_rank(metric)
                if (same_source or incoming_rank == current_rank) and metric.as_of < current.as_of:
                    return
                is_downgrade = not same_source and incoming_rank > current_rank
                if is_downgrade:
                    current_stale_after_seconds = self._yield_stale_after_seconds.get(
                        current.symbol
                    )
                    if current_stale_after_seconds is None:
                        current_stale_after_seconds = self._default_yield_stale_after_seconds(
                            current
                        )
                    now = utc_now()
                    current_age_seconds = max(
                        0.0,
                        (now - current.as_of).total_seconds(),
                    )
                    if current_age_seconds <= current_stale_after_seconds:
                        return
            self._yield_metrics[metric.symbol] = metric
            self._yield_stale_after_seconds[metric.symbol] = stale_after_seconds
        self._rebuild(metric.symbol, persist=persist)
        if persist:
            self._persist_yield_metric(metric, stale_after_seconds)

    @staticmethod
    def _coerce_yield_stale_after_seconds(value: Any) -> float | None:
        try:
            result = float(value)
        except TypeError, ValueError:
            return None
        return result if result > 0 else None

    @staticmethod
    def _yield_source_rank(metric: YieldMetric) -> tuple[int, int]:
        """Return a stable semantic tier followed by the current route level.

        Route levels are configuration-relative and therefore unsafe as the
        first comparison after restart. Provider-reported non-proxy rates rank
        above protocol/index estimates, which rank above market proxies.
        """

        semantic_tier = 2 if metric.is_proxy else 1 if metric.is_estimate else 0
        return semantic_tier, metric.fallback_level

    def _default_yield_stale_after_seconds(self, metric: YieldMetric) -> float:
        """Choose and persist a metadata freshness SLA for a yield observation."""

        # Two collection cycles tolerate one transient failure. FRED is a daily
        # business-day series, so a seven-day floor safely spans long weekends.
        threshold = max(300.0, self.settings.metadata_poll_seconds * 2)
        if metric.provider.casefold() == "fred" or metric.method == "DGS3MO":
            threshold = max(threshold, 7 * 24 * 60 * 60)
        if metric.method == "latest_distribution_annualized":
            threshold = max(threshold, 45 * 24 * 60 * 60)
        if metric.method == ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD:
            threshold = max(threshold, ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS)
        return threshold

    def _restored_yield_stale_after_seconds(
        self,
        metric: YieldMetric,
        persisted_value: Any,
    ) -> float:
        """Restore a persisted SLA while applying mandatory policy migrations."""

        persisted = self._coerce_yield_stale_after_seconds(persisted_value)
        if persisted is None:
            return self._default_yield_stale_after_seconds(metric)
        if metric.method == ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD:
            # Databases written before the daily-index policy stored the generic
            # 12-hour SLA. Never restore that obsolete value after an upgrade.
            return max(persisted, self._default_yield_stale_after_seconds(metric))
        return persisted

    def _persist_yield_metric(self, metric: YieldMetric, stale_after_seconds: float) -> None:
        """Persist the effective route plus its freshness policy as one record."""

        try:
            from .storage import YieldMetricRecord

            record = YieldMetricRecord.from_domain(metric)
            raw = dict(record.raw)
            raw["stale_after_seconds"] = stale_after_seconds
            self._persist("enqueue_yield", dataclasses.replace(record, raw=raw))
        except Exception:
            self._storage_ready = False

    def _rebuild(self, symbol: str, *, persist: bool) -> None:
        with self._metadata_lock:
            quote = self._last_quotes.get(symbol)
            event = self._dividends.get(symbol)
            yield_metric = self._yield_metrics.get(symbol)
        if quote is None:
            return
        instrument = self.registry[symbol]
        dividend = None
        annual_yield = None
        if (
            instrument.dividend_strategy == "latest_regular_cash_annualized_x4"
            and event is not None
        ):
            dividend = quarterly_dividend(event, quote.price)
        if (
            instrument.yield_strategy is YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED
            and event is not None
        ):
            annual_yield = sgov_yield(event, quote.price, quote.as_of)
        elif (
            instrument.yield_strategy is YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE
            and yield_metric is not None
        ):
            annual_yield = boxx_yield(yield_metric)
        elif (
            instrument.yield_strategy is YieldStrategy.STAKING_PROVIDER_METRIC
            and yield_metric is not None
        ):
            annual_yield = estimate_from_staking_metric(yield_metric)
        snapshot = QuoteSnapshot(
            quote=quote,
            changes=calculate_changes(quote.price, quote.as_of, self.history.points(symbol)),
            dividend=dividend,
            estimated_annual_yield=annual_yield,
        )
        self.snapshots.publish(snapshot)
        self._wire_cache[symbol] = snapshot_to_wire(
            snapshot,
            instrument,
            now=quote.as_of,
            stale_after_seconds=instrument.stale_after_seconds,
            yield_stale_after_seconds=self._yield_stale_after_seconds.get(symbol),
        )
        complete = (instrument.yield_strategy is None or annual_yield is not None) and (
            instrument.dividend_strategy is None or dividend is not None
        )
        with self._metadata_lock:
            if complete:
                self._complete_symbols.add(symbol)
            else:
                self._complete_symbols.discard(symbol)
        if persist:
            self._persist("enqueue_snapshot", snapshot)

    def _persist(self, method_name: str, value: Any) -> None:
        if self._storage is None:
            return
        method = getattr(self._storage, method_name, None)
        if method is None:
            return
        try:
            if inspect.iscoroutinefunction(method):
                loop = asyncio.get_running_loop()
                task = loop.create_task(method(value, wait=True), name=f"sqlite:{method_name}")
                self._persistence_tasks.add(task)
                task.add_done_callback(self._persistence_done)
            else:
                result = method(value)
                if inspect.isawaitable(result):
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(result, name=f"sqlite:{method_name}")
                    self._persistence_tasks.add(task)
                    task.add_done_callback(self._persistence_done)
        except Exception:
            self._storage_ready = False

    def _persist_history(self, points: list[PricePoint]) -> None:
        if self._storage is None:
            return

        async def enqueue_batch() -> None:
            for point in points:
                if point.interval in {"5m", "1d"}:
                    bucket_start, interval_seconds = self._aggregate_bucket(point)
                    value: Any = AggregatePrice(
                        point.symbol,
                        bucket_start,
                        interval_seconds,
                        point.price,
                        point.price,
                        point.price,
                        point.price,
                        1,
                        point.provider,
                        point.is_derived,
                    )
                    await self._storage.enqueue_aggregate_price(value)
                else:
                    await self._storage.enqueue_price(point)
            # A barrier turns a fire-and-forget batch rollback (for example,
            # disk-full) into an exception observed by ``_persistence_done``.
            await self._storage.flush()

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(enqueue_batch(), name="sqlite:history-batch")
            self._persistence_tasks.add(task)
            task.add_done_callback(self._persistence_done)
        except Exception:
            self._storage_ready = False

    def _persistence_done(self, task: asyncio.Task[Any]) -> None:
        self._persistence_tasks.discard(task)
        if task.cancelled():
            self._storage_ready = False
            return
        try:
            task.result()
        except Exception:
            self._storage_ready = False

    def _effective_market_status(
        self, snapshot: QuoteSnapshot, asset_class: AssetClass, now: datetime
    ) -> str:
        instrument = self.registry[snapshot.quote.symbol]
        scheduled = scheduled_market_status(instrument.market_calendar, now)
        observed_at = snapshot.quote.market_status_as_of
        if observed_at is not None:
            status_age = (now - observed_at).total_seconds()
            if 0 <= status_age <= 300 and snapshot.quote.market_status != "unknown":
                # Market status is observed independently from the last trade.
                # This preserves holidays and early closes even when the last
                # valid trade is naturally many hours old.
                return snapshot.quote.market_status
        return scheduled

    def _quality(
        self, snapshot: QuoteSnapshot, asset_class: AssetClass, now: datetime
    ) -> tuple[str, QualityModel]:
        quote = snapshot.quote
        instrument = self.registry[quote.symbol]
        staleness_ms = max(0, int((now - quote.as_of).total_seconds() * 1000))
        threshold_seconds = instrument.stale_after_seconds
        status = self._effective_market_status(snapshot, asset_class, now)
        stale = staleness_ms > int(threshold_seconds * 1000)
        status_observation_age = (
            None
            if quote.market_status_as_of is None
            else (now - quote.market_status_as_of).total_seconds()
        )
        provider_confirms_closed = (
            quote.market_status == "closed"
            and status_observation_age is not None
            and 0 <= status_observation_age <= 300
        )
        if status == "closed" and not provider_confirms_closed:
            last_close = most_recent_scheduled_close(instrument.market_calendar, now)
            if last_close is not None:
                stale = quote.as_of < last_close - timedelta(seconds=threshold_seconds)
        elif provider_confirms_closed:
            # A fresh exchange clock is authoritative for holidays and early
            # closes; comparing a 13:00 early-close trade with the regular
            # 16:00 schedule would incorrectly mark it stale all weekend.
            stale = False
        with self._metadata_lock:
            stale = stale or quote.symbol in self._source_failures
        return status, QualityModel(stale=stale, staleness_ms=staleness_ms)

    def mark_source_failed(self, *symbols: str) -> None:
        with self._metadata_lock:
            self._source_failures.update(symbol for symbol in symbols if symbol in self.registry)

    def get_quote(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
        require_complete_metadata: bool = True,
    ) -> QuoteModel:
        """Project a snapshot with dynamic quote and yield freshness.

        Public market-data routes keep ``require_complete_metadata=True`` so
        mandatory dividend and yield fields remain strict. Authenticated
        operational views may relax that gate while retaining the same market
        status, quote quality, and annual-yield quality calculations.
        """

        instrument = self.registry[symbol]
        snapshot = self.snapshots.get(symbol)
        if snapshot is None:
            raise DataUnavailableError(symbol, "no valid price has ever been received")
        if require_complete_metadata:
            if instrument.yield_strategy is not None and snapshot.estimated_annual_yield is None:
                raise DataUnavailableError(
                    symbol,
                    "required estimated annual yield is unavailable",
                )
            if instrument.dividend_strategy is not None and snapshot.dividend is None:
                raise DataUnavailableError(
                    symbol,
                    "required latest regular dividend is unavailable",
                )
        cached = self._wire_cache[symbol]
        now = utc_now() if now is None else now
        market_status, quality = self._quality(snapshot, instrument.asset_class, now)
        annual_yield = cached.estimated_annual_yield
        if annual_yield is not None:
            threshold = self._yield_stale_after_seconds.get(symbol)
            if threshold is None:
                metric = self._yield_metrics.get(symbol)
                threshold = (
                    self._default_yield_stale_after_seconds(metric)
                    if metric is not None
                    else max(300.0, self.settings.metadata_poll_seconds * 2)
                )
            staleness_ms = max(
                0,
                int((now - snapshot.estimated_annual_yield.as_of).total_seconds() * 1000),
            )
            previous_quality = snapshot.estimated_annual_yield.quality
            yield_quality = annual_yield.quality.model_copy(
                update={
                    "stale": bool(previous_quality and previous_quality.stale)
                    or staleness_ms > int(threshold * 1000),
                    "staleness_ms": staleness_ms,
                    "stale_after_seconds": threshold,
                }
            )
            annual_yield = annual_yield.model_copy(update={"quality": yield_quality})
        return cached.model_copy(
            update={
                "market_status": market_status,
                "quality": quality,
                "estimated_annual_yield": annual_yield,
            }
        )

    def is_ready(self) -> bool:
        """Return public readiness without scanning the instrument catalog."""

        runtime = self.runtime_status or inspect_free_threaded_runtime()
        runtime_ok = runtime.ready or not self.settings.require_free_threaded
        collectors_running = self._coordinator is not None and bool(
            getattr(self._coordinator, "is_running", True)
        )
        with self._metadata_lock:
            complete_count = len(self._complete_symbols)
        return (
            runtime_ok
            and self._has_active_api_key()
            and self._storage_ready
            and (collectors_running or not self.settings.background_enabled)
            and complete_count == len(self.registry)
        )

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        runtime = self.runtime_status or inspect_free_threaded_runtime()
        missing: list[str] = []
        incomplete: list[str] = []
        for symbol in self.registry:
            if self.snapshots.get(symbol) is None:
                missing.append(symbol)
                continue
            try:
                self.get_quote(symbol)
            except DataUnavailableError:
                incomplete.append(symbol)
        collectors_running = self._coordinator is not None and bool(
            getattr(self._coordinator, "is_running", True)
        )
        collector_failure = (
            getattr(self._coordinator, "fatal_error", None)
            if self._coordinator is not None
            else self._collector_start_error
        )
        details: dict[str, Any] = {
            "ready": False,
            "runtime": runtime.as_dict(),
            "free_threaded_required": self.settings.require_free_threaded,
            "api_key_configured": self._has_active_api_key(),
            "storage_ready": self._storage_ready,
            "collectors_running": collectors_running,
            "collector_failure": (
                None
                if collector_failure is None
                else {
                    "type": type(collector_failure).__name__,
                    "message": str(collector_failure),
                }
            ),
            "missing_prices": missing,
            "incomplete_metadata": incomplete,
        }
        ready = self.is_ready()
        details["ready"] = ready
        return ready, details

    def operational_metrics(self) -> dict[str, Any]:
        result = self.metrics.snapshot()
        now = utc_now()
        with self._metadata_lock:
            result["source_failures"] = sorted(self._source_failures)
        result["snapshot_age_ms"] = {
            symbol: max(0, int((now - snapshot.quote.as_of).total_seconds() * 1000))
            for symbol, snapshot in self.snapshots.all().items()
        }
        result["history_ring_points"] = self.history.sizes()
        if self._storage is not None:
            storage_metrics = getattr(self._storage, "metrics", None)
            if storage_metrics is not None:
                value = storage_metrics()
                result["sqlite"] = (
                    dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value
                )
        if self._coordinator is not None:
            coordinator_metrics = getattr(self._coordinator, "metrics", None)
            if coordinator_metrics is not None:
                result["providers"] = coordinator_metrics()
        return result
