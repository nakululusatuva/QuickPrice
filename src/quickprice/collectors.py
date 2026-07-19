"""Long-lived upstream collectors feeding the in-memory publication path.

All network and SQLite work remains outside HTTP request handling. Streaming
feeds are primary where the free plans allow it; capability routing supplies
REST fallback and circuit breaking.
"""

from __future__ import annotations

import asyncio
import dataclasses
import heapq
import inspect
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
COLLECTOR_STARTUP_TIMEOUT_SECONDS = 20.0
DAILY_PREFIX_RETRY_SECONDS = 24 * 60 * 60
QUOTA_METRICS_REFRESH_SECONDS = 60.0

_GENERATION_TASKS = (
    "publish",
    "quote-scheduler",
    "metadata",
    "history",
    "quota-metrics",
    "maintenance",
)


@dataclasses.dataclass(slots=True)
class CollectorReconciliation:
    """Rollback token for an in-place catalog collector handoff."""

    candidate: Any
    previous_registry: InstrumentRegistry
    previous_generation_id: str | None
    previous_graph: ProviderGraph
    previous_history_policies: dict[str, tuple[float | None, int | None]]
    previous_checkpoint_state: dict[tuple[str, str], dict[str, str]]
    previous_stream_observed_at: dict[tuple[int, str], float]
    previous_quote_next_refresh_at: dict[str, float]
    previous_metadata_next_refresh_at: dict[str, float]
    previous_history_next_regular_refresh_at: dict[str, float]
    previous_history_next_empty_refresh_at: float
    previous_streams: dict[str, tuple[Any, tuple[str, ...]]]
    retained_stream_tasks: dict[str, asyncio.Task[Any]]
    transferred_providers: dict[str, tuple[Any, Any]]
    failure: asyncio.Future[BaseException]
    failure_release: asyncio.Event
    finalized: bool = False


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
        *,
        generation_id: str | None = None,
        graph: ProviderGraph | None = None,
        catalog: Any = None,
    ) -> None:
        self.service = service
        self.settings = settings
        if registry is None:
            registry = getattr(service, "registry", None)
        if registry is None:
            registry = build_registry(settings.enabled_plugins)
        self.registry = registry
        self._history_policies: dict[str, tuple[float | None, int | None]] = {}
        if catalog is not None:
            for definition in getattr(catalog, "definitions", ()):
                history_policy = getattr(definition, "history", None)
                if history_policy is None:
                    continue
                self._history_policies[definition.symbol] = (
                    history_policy.poll_seconds,
                    history_policy.backfill_days,
                )
        capture_generation = getattr(service, "capture_generation", None)
        captured = capture_generation() if callable(capture_generation) else None
        self.generation_id = generation_id or getattr(captured, "generation_id", None)
        self._generation_publishers = {
            name
            for name in (
                "publish_quote",
                "publish_history",
                "publish_history_async",
                "publish_dividend",
                "publish_yield_metric",
            )
            if self._accepts_generation_id(getattr(service, name, None))
        }
        self.graph: ProviderGraph = graph or build_provider_graph(
            settings,
            self.registry,
            strict=settings.production and settings.background_enabled,
            metrics=getattr(service, "metrics", None),
        )
        self.router = self.graph.router
        self._owns_graph = True
        self._stop = asyncio.Event()
        self._started = asyncio.Event()
        self._activation_gate = asyncio.Event()
        self._supervisor: asyncio.Task[Any] | None = None
        self._fatal_error: BaseException | None = None
        self._prepared = False
        self._closed = False
        self._task_group: asyncio.TaskGroup | None = None
        self._component_tasks: dict[str, asyncio.Task[Any]] = {}
        self._stream_task_symbols: dict[str, tuple[str, ...]] = {}
        self._reconciliation_guard: CollectorReconciliation | None = None
        self._pending: dict[str, ProviderQuote] = {}
        self._last_errors: dict[str, dict[str, Any]] = {}
        self._quota_snapshots: dict[str, dict[str, Any]] = {}
        self._quota_updated_at: str | None = None
        self._websocket_reconnects: dict[str, int] = {}
        self._stream_statistics: dict[str, dict[str, Any]] = {}
        self._stream_observed_at: dict[tuple[int, str], float] = {}
        self._quote_next_refresh_at: dict[str, float] = {}
        self._metadata_next_refresh_at: dict[str, float] = {}
        self._history_next_regular_refresh_at: dict[str, float] = {}
        self._history_next_empty_refresh_at = 0.0
        self._checkpoint_state: dict[tuple[str, str], dict[str, str]] = {}
        self._equity_history_fallback_at: dict[str, float] = {}
        self._daily_prefix_retry_at: dict[str, float] = {}
        self._daily_preseeded_symbols: set[str] = set()
        self._fx_daily_retry_symbols: set[str] = set()
        self._fx_failed_history_intervals: set[tuple[str, str]] = set()
        self._fx_history_intervals_for_cycle: dict[str, tuple[str, ...]] = {}
        self._fx_history_retry_only = False
        self._history_full_cycle = True
        self._history_due_symbols: set[str] | None = None
        self._fx_startup_preseed_timeout_seconds = FX_STARTUP_PRESEED_TIMEOUT_SECONDS
        self._next_fx_history_refresh_at = 0.0

    @staticmethod
    def _accepts_generation_id(method: Any) -> bool:
        if not callable(method):
            return False
        try:
            return "generation_id" in inspect.signature(method).parameters
        except TypeError, ValueError:
            return False

    def _publish(self, name: str, value: Any, **kwargs: Any) -> Any:
        method = getattr(self.service, name)
        if name in self._generation_publishers and self.generation_id is not None:
            kwargs["generation_id"] = self.generation_id
        return method(value, **kwargs)

    async def _publish_async(self, name: str, value: Any, **kwargs: Any) -> Any:
        result = self._publish(name, value, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def start(self) -> None:
        if self._supervisor is not None:
            return
        await self.prepare()
        self.activate()
        await self.wait_started_or_failed()

    async def wait_started_or_failed(
        self,
        timeout_seconds: float = COLLECTOR_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        """Wait boundedly for startup acknowledgement or a terminal failure."""

        supervisor = self._supervisor
        if supervisor is None:
            raise RuntimeError("collector coordinator has not been activated")
        if timeout_seconds <= 0:
            raise ValueError("collector startup timeout must be positive")
        started = asyncio.create_task(self._started.wait())
        try:
            async with asyncio.timeout(timeout_seconds):
                done, _ = await asyncio.wait(
                    {started, supervisor},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Let an immediately failing TaskGroup publish its terminal
                # state before accepting a simultaneous `_started` signal.
                if started in done and supervisor not in done:
                    await asyncio.sleep(0)
                if supervisor.done():
                    failure = self._fatal_error
                    if failure is None:
                        raise RuntimeError("collector stopped during startup")
                    raise RuntimeError("collector failed during startup") from failure
                if not self._started.is_set():
                    raise RuntimeError("collector stopped before startup acknowledgement")
        finally:
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)

    async def prepare(self) -> None:
        """Restore durable state without opening streams or publishing data."""

        if self._prepared:
            return
        if self._closed:
            raise RuntimeError("collector coordinator is closed")
        await self._restore_and_bind_provider_state()
        await self._update_quota_metrics()
        self._prepared = True

    def activate(self, *, gated: bool = False) -> None:
        """Create the supervisor, optionally gated until handoff commits."""

        if not self._prepared:
            raise RuntimeError("collector coordinator must be prepared before activation")
        if self._closed:
            raise RuntimeError("collector coordinator is closed")
        if self._supervisor is not None:
            return
        if not gated:
            self._activation_gate.set()
        self._fatal_error = None
        self._supervisor = asyncio.create_task(self._supervise(), name="market-data-coordinator")

    def release_activation(self) -> None:
        """Release a prepared handoff only after its catalog transaction commits."""

        if self._supervisor is None:
            raise RuntimeError("collector coordinator has not been activated")
        self._activation_gate.set()

    def adopt_generation(
        self,
        registry: InstrumentRegistry,
        generation_id: str | None,
    ) -> tuple[InstrumentRegistry, str | None]:
        """Retarget a running coordinator for a metadata-only catalog change."""

        if not self.is_running or self._closed:
            raise RuntimeError("only a running collector can adopt a generation")
        previous = self.registry, self.generation_id
        self.registry = registry
        self.generation_id = generation_id
        return previous

    @staticmethod
    def _provider_depends_on_router(provider: Any, router: Any) -> bool:
        """Return whether transferring an adapter would retain the old graph."""

        try:
            values = vars(provider).values()
        except TypeError:
            return False
        return any(
            value is router or getattr(value, "__self__", None) is router for value in values
        )

    def _transferable_provider_pairs(
        self,
        candidate: MarketDataCoordinator,
        reusable_provider_names: Sequence[str],
    ) -> dict[str, tuple[Any, Any]]:
        pairs: dict[str, tuple[Any, Any]] = {}
        for name in reusable_provider_names:
            previous = self.graph.providers.get(name)
            replacement = candidate.graph.providers.get(name)
            if previous is None or replacement is None:
                continue
            if self._provider_depends_on_router(previous, self.router):
                continue
            pairs[name] = (previous, replacement)
        return pairs

    def can_reconcile(
        self,
        candidate: MarketDataCoordinator,
        reusable_provider_names: Sequence[str],
    ) -> bool:
        """Return whether a prepared graph can reuse live provider ownership."""

        return bool(
            self.is_running
            and self._task_group is not None
            and not self._closed
            and candidate._prepared
            and not candidate._closed
            and self._transferable_provider_pairs(candidate, reusable_provider_names)
        )

    async def _cancel_component_tasks(self, keys: Sequence[str]) -> None:
        selected = {
            key: task for key in keys if (task := self._component_tasks.get(key)) is not None
        }
        for task in selected.values():
            task.cancel()
        if selected:
            await asyncio.gather(*selected.values(), return_exceptions=True)
        for key, task in selected.items():
            if self._component_tasks.get(key) is task:
                self._component_tasks.pop(key, None)
            if key.startswith("stream:"):
                self._stream_task_symbols.pop(key.removeprefix("stream:"), None)

    def _restart_previous_components(
        self,
        previous_streams: dict[str, tuple[Any, tuple[str, ...]]],
        retained_stream_tasks: dict[str, asyncio.Task[Any]],
    ) -> None:
        self._start_generation_tasks()
        for provider_name, (provider, symbols) in previous_streams.items():
            if provider_name in retained_stream_tasks:
                continue
            self._start_stream_task(provider_name, provider, symbols)

    @staticmethod
    def _replace_transferred_providers(
        graph: ProviderGraph,
        transferred: dict[str, tuple[Any, Any]],
        *,
        use_previous: bool,
    ) -> None:
        for name, (previous, replacement) in transferred.items():
            old, new = (replacement, previous) if use_previous else (previous, replacement)
            graph.router.replace_provider_instance(old, new)
            graph.providers[name] = new

    @staticmethod
    def _same_route_instances(
        previous_router: Any,
        candidate_router: Any,
        symbol: str,
        capabilities: Sequence[Capability],
    ) -> bool:
        return all(
            tuple(map(id, previous_router.providers_for(symbol, capability)))
            == tuple(map(id, candidate_router.providers_for(symbol, capability)))
            for capability in capabilities
        )

    def _preserved_scheduler_symbols(
        self,
        candidate: MarketDataCoordinator,
    ) -> tuple[set[str], set[str], set[str]]:
        """Select only symbols whose scheduling inputs and live routes are unchanged."""

        common = set(self.registry) & set(candidate.registry)
        quote: set[str] = set()
        metadata: set[str] = set()
        history: set[str] = set()
        for symbol in common:
            previous = self.registry[symbol]
            replacement = candidate.registry[symbol]
            if (
                previous.quote_poll_seconds == replacement.quote_poll_seconds
                and previous.stale_after_seconds == replacement.stale_after_seconds
                and previous.market_calendar == replacement.market_calendar
                and previous.asset_class == replacement.asset_class
                and self._same_route_instances(
                    self.router,
                    candidate.router,
                    symbol,
                    (Capability.QUOTE,),
                )
            ):
                quote.add(symbol)
            if (
                previous.dividend_strategy == replacement.dividend_strategy
                and previous.yield_strategy == replacement.yield_strategy
                and self._same_route_instances(
                    self.router,
                    candidate.router,
                    symbol,
                    (Capability.DIVIDEND, Capability.YIELD),
                )
            ):
                metadata.add(symbol)
            if (
                previous.history_enabled == replacement.history_enabled
                and previous.asset_class == replacement.asset_class
                and self._history_policies.get(symbol) == candidate._history_policies.get(symbol)
                and self._same_route_instances(
                    self.router,
                    candidate.router,
                    symbol,
                    (Capability.HISTORY,),
                )
            ):
                history.add(symbol)
        return quote, metadata, history

    async def reconcile_generation(
        self,
        candidate: MarketDataCoordinator,
        registry: InstrumentRegistry,
        generation_id: str | None,
        reusable_provider_names: Sequence[str],
    ) -> CollectorReconciliation:
        """Retarget a running coordinator while preserving unaffected streams.

        Generation schedulers and changed provider streams are quiesced before
        the synchronous graph swap.  Unchanged stream tasks keep running, so
        their underlying WebSocket connection is never reopened.
        """

        transferred = self._transferable_provider_pairs(candidate, reusable_provider_names)
        if not self.can_reconcile(candidate, reusable_provider_names) or not transferred:
            raise RuntimeError("collector graph cannot be reconciled in place")

        previous_streams: dict[str, tuple[Any, tuple[str, ...]]] = {}
        for provider_name, symbols in self._stream_task_symbols.items():
            task = self._component_tasks.get(f"stream:{provider_name}")
            provider = self.graph.providers.get(provider_name)
            if task is not None and not task.done() and provider is not None:
                previous_streams[provider_name] = (provider, symbols)

        self._replace_transferred_providers(
            candidate.graph,
            transferred,
            use_previous=True,
        )
        retained_stream_tasks: dict[str, asyncio.Task[Any]] = {}
        for provider_name, (provider, symbols) in previous_streams.items():
            pair = transferred.get(provider_name)
            replacement = candidate.graph.providers.get(provider_name)
            if pair is None or replacement is not provider:
                continue
            if candidate._stream_symbols(provider) != symbols:
                continue
            task = self._component_tasks[f"stream:{provider_name}"]
            retained_stream_tasks[provider_name] = task

        token = CollectorReconciliation(
            candidate=candidate,
            previous_registry=self.registry,
            previous_generation_id=self.generation_id,
            previous_graph=self.graph,
            previous_history_policies=dict(self._history_policies),
            previous_checkpoint_state={
                key: dict(value) for key, value in self._checkpoint_state.items()
            },
            previous_stream_observed_at=dict(self._stream_observed_at),
            previous_quote_next_refresh_at=dict(self._quote_next_refresh_at),
            previous_metadata_next_refresh_at=dict(self._metadata_next_refresh_at),
            previous_history_next_regular_refresh_at=dict(self._history_next_regular_refresh_at),
            previous_history_next_empty_refresh_at=self._history_next_empty_refresh_at,
            previous_streams=previous_streams,
            retained_stream_tasks=retained_stream_tasks,
            transferred_providers=transferred,
            failure=asyncio.get_running_loop().create_future(),
            failure_release=asyncio.Event(),
        )

        cancel_keys = [*_GENERATION_TASKS]
        cancel_keys.extend(
            f"stream:{name}" for name in previous_streams if name not in retained_stream_tasks
        )
        try:
            await self._cancel_component_tasks(cancel_keys)
            if not self.is_running or self._task_group is None:
                raise RuntimeError("collector stopped while preparing graph reconciliation")
            self._flush_pending()
            preserve_quote, preserve_metadata, preserve_history = self._preserved_scheduler_symbols(
                candidate
            )
            self._quote_next_refresh_at = {
                symbol: due_at
                for symbol, due_at in self._quote_next_refresh_at.items()
                if symbol in preserve_quote
            }
            self._metadata_next_refresh_at = {
                symbol: due_at
                for symbol, due_at in self._metadata_next_refresh_at.items()
                if symbol in preserve_metadata
            }
            self._history_next_regular_refresh_at = {
                symbol: due_at
                for symbol, due_at in self._history_next_regular_refresh_at.items()
                if symbol in preserve_history
            }

            # No await is permitted across this assignment block. Stream tasks
            # that remain live therefore observe either the complete old target
            # or the complete new target, never a mixed registry/router pair.
            self.registry = registry
            self.generation_id = generation_id
            self.graph = candidate.graph
            self.router = candidate.router
            self._history_policies = dict(candidate._history_policies)
            self._checkpoint_state = {
                **candidate._checkpoint_state,
                **self._checkpoint_state,
            }
            active_symbols = set(registry.symbols)
            retained_ids = {id(previous) for previous, _ in transferred.values()}
            self._stream_observed_at = {
                key: value
                for key, value in self._stream_observed_at.items()
                if key[0] in retained_ids and key[1] in active_symbols
            }
            candidate._owns_graph = False

            self._reconciliation_guard = token
            try:
                self._start_generation_tasks()
                for provider_name, provider in self.graph.providers.items():
                    if provider_name in retained_stream_tasks:
                        continue
                    symbols = self._stream_symbols(provider)
                    if symbols:
                        self._start_stream_task(provider_name, provider, symbols)
            finally:
                self._reconciliation_guard = None

            # Give newly scheduled loops one turn. An immediately failed task
            # must roll back before the runtime generation is published.
            await asyncio.sleep(0)
            if token.failure.done():
                raise RuntimeError(
                    "collector failed during graph reconciliation"
                ) from token.failure.result()
            if not self.is_running or self.fatal_error is not None:
                raise RuntimeError("collector failed during graph reconciliation")
            return token
        except BaseException:
            await asyncio.shield(self.rollback_reconciliation(token))
            raise

    async def rollback_reconciliation(self, token: CollectorReconciliation) -> None:
        """Restore the exact collector target represented by a handoff token."""

        if token.finalized:
            raise RuntimeError("collector reconciliation is already finalized")
        token.failure_release.set()
        retained_keys = {f"stream:{name}" for name in token.retained_stream_tasks}
        cancel_keys = [*_GENERATION_TASKS]
        cancel_keys.extend(
            key
            for key in tuple(self._component_tasks)
            if key.startswith("stream:") and key not in retained_keys
        )
        await self._cancel_component_tasks(cancel_keys)
        self._pending.clear()

        candidate = token.candidate
        self._replace_transferred_providers(
            candidate.graph,
            token.transferred_providers,
            use_previous=False,
        )
        candidate._owns_graph = True
        self.registry = token.previous_registry
        self.generation_id = token.previous_generation_id
        self.graph = token.previous_graph
        self.router = token.previous_graph.router
        self._history_policies = dict(token.previous_history_policies)
        self._checkpoint_state = {
            key: dict(value) for key, value in token.previous_checkpoint_state.items()
        }
        self._stream_observed_at = dict(token.previous_stream_observed_at)
        self._quote_next_refresh_at = dict(token.previous_quote_next_refresh_at)
        self._metadata_next_refresh_at = dict(token.previous_metadata_next_refresh_at)
        self._history_next_regular_refresh_at = dict(token.previous_history_next_regular_refresh_at)
        self._history_next_empty_refresh_at = token.previous_history_next_empty_refresh_at
        self._restart_previous_components(
            token.previous_streams,
            token.retained_stream_tasks,
        )

    async def confirm_reconciliation(self, token: CollectorReconciliation) -> None:
        """Reject a guarded component failure before releasing the file lease."""

        if token.finalized:
            raise RuntimeError("collector reconciliation is already finalized")
        await asyncio.sleep(0)
        if token.failure.done():
            raise RuntimeError(
                "collector failed during graph reconciliation"
            ) from token.failure.result()
        if not self.is_running or self.fatal_error is not None:
            raise RuntimeError("collector failed during graph reconciliation")

    @staticmethod
    async def _close_provider_instances(providers: Sequence[Any]) -> None:
        seen: set[int] = set()
        for provider in providers:
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            close = getattr(provider, "close", None)
            if not callable(close):
                continue
            result = close()
            if inspect.isawaitable(result):
                await result

    async def finalize_reconciliation(self, token: CollectorReconciliation) -> None:
        """Release providers retired by a committed graph reconciliation."""

        if token.finalized:
            return
        token.finalized = True
        token.failure_release.set()
        retained = tuple(previous for previous, _ in token.transferred_providers.values())
        await token.previous_graph.close(exclude_providers=retained)
        await self._close_provider_instances(
            tuple(replacement for _, replacement in token.transferred_providers.values())
        )

    async def _supervise(self) -> None:
        try:
            await self._activation_gate.wait()
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

        quota_owners: dict[str, bool] = {}
        share_quota = getattr(self.service, "share_provider_quota", None)
        for provider_name, provider in self.graph.providers.items():
            quota = getattr(provider, "quota", None)
            if quota is None:
                continue
            if callable(share_quota):
                quota, is_owner = share_quota(provider_name, quota)
                provider.quota = quota
                quota_owners[provider_name] = is_owner
            else:
                quota_owners[provider_name] = True

        for (provider_name, feed), record in restored.items():
            if feed == "quota":
                provider = self.graph.providers.get(provider_name)
                quota = getattr(provider, "quota", None)
                if quota is not None and quota_owners.get(provider_name, True):
                    await quota.restore(record.checkpoint)
                continue
            symbols = record.checkpoint.get("symbols")
            if isinstance(symbols, dict):
                self._checkpoint_state[(provider_name, feed)] = {
                    str(symbol): str(as_of) for symbol, as_of in symbols.items()
                }

        for provider_name, provider in self.graph.providers.items():
            quota = getattr(provider, "quota", None)
            if quota is None or not quota_owners.get(provider_name, True):
                continue

            async def persist_quota(checkpoint: Any, *, name: str = provider_name) -> None:
                record = ProviderCheckpointRecord(
                    provider=name,
                    feed="quota",
                    updated_at=utc_now(),
                    checkpoint=checkpoint,
                )
                await storage.enqueue_checkpoint_record(
                    record,
                    wait=True,
                )
                remember = getattr(self.service, "remember_provider_checkpoint", None)
                if callable(remember):
                    remember(record)

            quota.set_persistence(persist_quota)

    async def stop(self, *, persist_checkpoints: bool = True) -> None:
        if self._closed:
            return
        self._stop.set()
        self._activation_gate.set()
        if self._supervisor is not None:
            try:
                async with asyncio.timeout(15):
                    await self._supervisor
            except TimeoutError:
                self._supervisor.cancel()
                await asyncio.gather(self._supervisor, return_exceptions=True)
            self._supervisor = None
        if self._pending:
            self._flush_pending()
        if persist_checkpoints:
            await self._persist_provider_checkpoints()
        if self._owns_graph:
            await self.graph.close()
        self._closed = True

    async def _run(self) -> None:
        if self._stop.is_set():
            self._started.set()
            return
        await self._startup_preseed_fx_daily()
        if self._stop.is_set():
            self._started.set()
            return
        # Publish synthetic inverses and crosses from the restored/seeded USD
        # hubs before readiness is exposed.  Otherwise the first materialize
        # pass waits behind every regular history worker and the API can be
        # temporarily complete for hub pairs but missing long-horizon changes
        # for all derived FX instruments.
        await self._materialize_builtin_fx_history()
        if self._stop.is_set():
            self._started.set()
            return
        try:
            async with asyncio.TaskGroup() as group:
                self._task_group = group
                self._start_generation_tasks()
                self._start_stream_tasks()
                self._started.set()
                await self._stop.wait()
                for task in tuple(self._component_tasks.values()):
                    task.cancel()
        finally:
            self._task_group = None
            self._component_tasks.clear()
            self._stream_task_symbols.clear()

    def _create_component_task(
        self,
        key: str,
        coroutine: Any,
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        group = self._task_group
        if group is None:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            raise RuntimeError("collector task group is not running")
        guard = self._reconciliation_guard
        if guard is not None:
            coroutine = self._guard_reconciled_component(coroutine, guard, name)
        task = group.create_task(coroutine, name=name)
        self._component_tasks[key] = task
        return task

    @staticmethod
    async def _guard_reconciled_component(
        coroutine: Any,
        token: CollectorReconciliation,
        name: str,
    ) -> None:
        """Hold a new task failure until activation chooses commit or rollback."""

        try:
            await coroutine
            failure: BaseException = RuntimeError(
                f"collector component stopped unexpectedly: {name}"
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = exc
        if not token.failure.done():
            token.failure.set_result(failure)
        await token.failure_release.wait()
        if token.finalized:
            raise failure

    def _start_generation_tasks(self) -> None:
        self._create_component_task("publish", self._publish_loop(), name="publish-coalesced")
        self._create_component_task(
            "quote-scheduler",
            self._quote_scheduler_loop(),
            name="quote-scheduler",
        )
        self._create_component_task("metadata", self._metadata_loop(), name="metadata")
        self._create_component_task("history", self._history_loop(), name="history")
        self._create_component_task(
            "quota-metrics",
            self._quota_metrics_loop(),
            name="quota-metrics",
        )
        self._create_component_task(
            "maintenance",
            self._maintenance_loop(),
            name="maintenance",
        )

    def _start_stream_tasks(self) -> None:
        for provider_name, provider in self.graph.providers.items():
            symbols = self._stream_symbols(provider)
            if symbols:
                self._start_stream_task(provider_name, provider, symbols)

    def _start_stream_task(
        self,
        provider_name: str,
        provider: Any,
        symbols: tuple[str, ...],
    ) -> asyncio.Task[Any]:
        key = f"stream:{provider_name}"
        task = self._create_component_task(
            key,
            self._provider_stream_loop(provider_name, provider, symbols),
            name=key,
        )
        self._stream_task_symbols[provider_name] = symbols
        return task

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
                self._publish("publish_quote", quote)
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
        active_symbols = {
            symbol for symbol in self.registry if self.router.configured(symbol, Capability.QUOTE)
        }
        self._quote_next_refresh_at = {
            symbol: due_at
            for symbol, due_at in self._quote_next_refresh_at.items()
            if symbol in active_symbols
        }
        for symbol in self.registry:
            if symbol not in active_symbols:
                continue
            due_at = self._quote_next_refresh_at.setdefault(symbol, current)
            heapq.heappush(queue, (due_at, next(sequence), symbol))
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
                    due_at = time.monotonic() + max(0.25, interval)
                    self._quote_next_refresh_at[symbol] = due_at
                    heapq.heappush(
                        queue,
                        (
                            due_at,
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
        stream_observation_age = (
            None if last_stream_observation is None else time.monotonic() - last_stream_observation
        )
        if (
            stream_suppression_seconds > 0
            and stream_observation_age is not None
            and stream_observation_age < stream_suppression_seconds
        ):
            next_interval = max(
                next_interval,
                float(getattr(primary_provider, "minimum_quote_poll_seconds", 0.0)),
            )
            stream_recheck_seconds = float(
                getattr(primary_provider, "stream_poll_recheck_seconds", 0.0)
            )
            if stream_recheck_seconds > 0:
                remaining_fresh_seconds = stream_suppression_seconds - stream_observation_age
                next_interval = min(
                    max(next_interval, stream_recheck_seconds),
                    remaining_fresh_seconds,
                )
            return next_interval
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
            if isinstance(exc, AllProvidersFailed):
                attempted_providers = {provider for provider, _ in exc.attempts}
                retry_hints = tuple(
                    float(delay)
                    for provider in chain
                    if str(getattr(provider, "name", provider.__class__.__name__))
                    in attempted_providers
                    and callable(
                        retry_after := getattr(
                            provider,
                            "quote_failure_retry_after_seconds",
                            None,
                        )
                    )
                    and (delay := retry_after()) is not None
                )
                if retry_hints:
                    next_interval = min(next_interval, max(0.25, min(retry_hints)))
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
        requested_symbols = frozenset(symbols)
        delay = 1.0
        statistics = self._stream_statistics.setdefault(
            provider_name,
            {
                "state": "idle",
                "connect_attempts": 0,
                "successful_connections": 0,
                "messages": 0,
                "last_connect_attempt_at": None,
                "connected_at": None,
                "last_message_at": None,
                "last_disconnect_at": None,
            },
        )
        while True:
            attempt_started = time.perf_counter()
            observed_message = False
            statistics["state"] = "connecting"
            statistics["connect_attempts"] += 1
            statistics["last_connect_attempt_at"] = utc_now().isoformat().replace("+00:00", "Z")
            try:
                async for quote in provider.stream_quotes(symbols):
                    delay = 1.0
                    if not observed_message:
                        observed_message = True
                        statistics["state"] = "connected"
                        statistics["successful_connections"] += 1
                        statistics["connected_at"] = utc_now().isoformat().replace("+00:00", "Z")
                        self.service.metrics.observe_provider_operation(
                            provider_name,
                            "stream",
                            "success",
                            (time.perf_counter() - attempt_started) * 1000,
                        )
                    statistics["messages"] += 1
                    statistics["last_message_at"] = utc_now().isoformat().replace("+00:00", "Z")
                    if quote.symbol in requested_symbols and quote.symbol in self.registry:
                        instrument = self.registry[quote.symbol]
                        source_age = max(0.0, (utc_now() - quote.as_of).total_seconds())
                        observation_key = (id(provider), quote.symbol)
                        if source_age <= instrument.stale_after_seconds:
                            self._stream_observed_at[observation_key] = (
                                time.monotonic() - source_age
                            )
                        else:
                            self._stream_observed_at.pop(observation_key, None)
                        self._queue_quote(quote)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"stream:{provider_name}", exc)
            if not observed_message:
                self.service.metrics.observe_provider_operation(
                    provider_name,
                    "stream",
                    "unavailable",
                    (time.perf_counter() - attempt_started) * 1000,
                )
            statistics["state"] = "disconnected"
            statistics["last_disconnect_at"] = utc_now().isoformat().replace("+00:00", "Z")
            for symbol in symbols:
                self._stream_observed_at.pop((id(provider), symbol), None)
            self._websocket_reconnects[provider_name] = (
                self._websocket_reconnects.get(provider_name, 0) + 1
            )
            self.service.metrics.websocket_reconnect(provider_name)
            await asyncio.sleep(delay)
            delay = min(60.0, delay * 2)

    async def _quota_metrics_loop(self) -> None:
        while True:
            await asyncio.sleep(QUOTA_METRICS_REFRESH_SECONDS)
            await self._update_quota_metrics()

    async def _metadata_loop(self) -> None:
        active_symbols = set(self.registry.symbols)
        self._metadata_next_refresh_at = {
            symbol: due_at
            for symbol, due_at in self._metadata_next_refresh_at.items()
            if symbol in active_symbols
        }
        next_refresh_at = self._metadata_next_refresh_at
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
                    self._publish("publish_dividend", event)
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
                self._publish("publish_yield_metric", metric)
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

    def _history_poll_seconds(self, symbol: str, *, fx: bool = False) -> float:
        configured = self._history_policies.get(symbol, (None, None))[0]
        if configured is not None:
            return max(1.0, float(configured))
        return (
            float(FX_HISTORY_REFRESH_SECONDS)
            if fx
            else max(1.0, float(self.settings.history_poll_seconds))
        )

    def _history_backfill_days(self, symbol: str) -> int | None:
        return self._history_policies.get(symbol, (None, None))[1]

    async def _history_loop(self) -> None:
        regular_symbols = tuple(
            instrument.symbol
            for instrument in self.registry.values()
            if instrument.history_enabled
            and (instrument.asset_class is not AssetClass.FX or instrument.symbol not in FX_SYMBOLS)
        )
        regular_symbol_set = set(regular_symbols)
        self._history_next_regular_refresh_at = {
            symbol: due_at
            for symbol, due_at in self._history_next_regular_refresh_at.items()
            if symbol in regular_symbol_set
        }
        for symbol in regular_symbols:
            self._history_next_regular_refresh_at.setdefault(symbol, 0.0)
        next_regular_refresh_at = self._history_next_regular_refresh_at
        while True:
            now = time.monotonic()
            include_fx = now >= self._next_fx_history_refresh_at
            due_symbols = {
                symbol for symbol, due_at in next_regular_refresh_at.items() if now >= due_at
            }
            empty_full_refresh = (
                not next_regular_refresh_at and now >= self._history_next_empty_refresh_at
            )
            full_refresh = bool(due_symbols) or empty_full_refresh
            if not include_fx and not full_refresh:
                next_regular = min(
                    next_regular_refresh_at.values(),
                    default=self._history_next_empty_refresh_at,
                )
                await asyncio.sleep(
                    max(
                        1.0,
                        min(self._next_fx_history_refresh_at, next_regular) - now,
                    )
                )
                continue

            self._history_full_cycle = full_refresh
            self._history_due_symbols = due_symbols if next_regular_refresh_at else None
            fx_history_complete = await self._backfill_history(include_fx=include_fx)
            completed_at = time.monotonic()
            if full_refresh:
                if due_symbols:
                    for symbol in due_symbols:
                        next_regular_refresh_at[symbol] = completed_at + self._history_poll_seconds(
                            symbol
                        )
                else:
                    self._history_next_empty_refresh_at = completed_at + max(
                        1.0, float(self.settings.history_poll_seconds)
                    )
            if include_fx:
                self._fx_history_retry_only = fx_history_complete is False
                if fx_history_complete is False:
                    retry_after = FX_HISTORY_RETRY_SECONDS
                else:
                    fx_symbols = (
                        symbol
                        for symbol in FX_HUB_SYMBOLS
                        if self.registry.resolve(symbol) is not None
                    )
                    retry_after = min(
                        (self._history_poll_seconds(symbol, fx=True) for symbol in fx_symbols),
                        default=float(FX_HISTORY_REFRESH_SECONDS),
                    )
                self._next_fx_history_refresh_at = completed_at + retry_after
            next_regular = min(
                next_regular_refresh_at.values(),
                default=self._history_next_empty_refresh_at,
            )
            await asyncio.sleep(
                max(
                    1.0,
                    min(self._next_fx_history_refresh_at, next_regular) - time.monotonic(),
                )
            )

    async def _backfill_history(self, *, include_fx: bool) -> bool:
        retry_only = self._fx_history_retry_only
        due_symbols = self._history_due_symbols
        selected_symbols = tuple(
            instrument.symbol
            for instrument in self.registry.values()
            if instrument.history_enabled
            and (
                (
                    self._history_full_cycle
                    and (due_symbols is None or instrument.symbol in due_symbols)
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
                if "publish_history_async" in self._generation_publishers:
                    await publish(
                        derived,
                        persist=False,
                        generation_id=self.generation_id,
                    )
                else:
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
        configured_backfill_days = self._history_backfill_days(symbol)
        intraday_durations = {
            "1m": timedelta(
                days=min(2, configured_backfill_days) if configured_backfill_days is not None else 2
            ),
            "5m": timedelta(
                days=min(45, configured_backfill_days)
                if configured_backfill_days is not None
                else 45
            ),
        }
        for interval, duration in intraday_durations.items():
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
        configured_backfill_days = self._history_backfill_days(symbol)
        daily_backfill_days = (
            400
            if configured_backfill_days is None
            # A one-day buffer is required to find an observation at or
            # before the rolling 365-day cutoff across market/session gaps.
            else max(366, min(400, configured_backfill_days))
        )
        daily_retention_start = now - timedelta(days=daily_backfill_days)
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
            await self._publish_async("publish_history_async", list(result))
        return result

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            await self._run_maintenance(utc_now())
            await asyncio.sleep(6 * 60 * 60)

    async def _run_maintenance(self, now: datetime) -> None:
        """Run one bounded memory and durable-storage maintenance pass."""

        await self._persist_provider_checkpoints()
        history = getattr(self.service, "history", None)
        prune_history = getattr(history, "prune", None)
        if callable(prune_history):
            try:
                await asyncio.to_thread(prune_history, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error("history:maintenance", exc)
        storage = getattr(self.service, "_storage", None)
        if storage is not None:
            try:
                await storage.cleanup()
                await storage.checkpoint("PASSIVE")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error("sqlite:maintenance", exc)

    async def _update_quota_metrics(self) -> None:
        result: dict[str, dict[str, Any]] = {}
        for name, provider in self.graph.providers.items():
            quota = getattr(provider, "quota", None)
            if quota is None:
                result[name] = {
                    "tracked": False,
                    "accounting": "untracked",
                    "provider_reported": False,
                    "unit": None,
                    "limit": None,
                    "used": None,
                    "remaining": None,
                    "resets_at": None,
                    "reserve": None,
                    "usable_limit": None,
                    "usable_remaining": None,
                    "period_seconds": None,
                }
                continue
            try:
                value = await quota.snapshot()
                snapshot = dataclasses.asdict(value)
                reserve = int(getattr(quota, "reserve", 0))
                usable_limit = max(0, value.limit - reserve)
                result[name] = {
                    **snapshot,
                    "tracked": True,
                    "accounting": "local_request_reservations",
                    "provider_reported": False,
                    "unit": "credits",
                    "reserve": reserve,
                    "usable_limit": usable_limit,
                    "usable_remaining": max(0, usable_limit - value.used),
                    "period_seconds": float(getattr(quota, "period_seconds", 0.0)),
                }
            except Exception as exc:
                self._record_error(f"quota:{name}", exc)
                result[name] = {
                    "tracked": True,
                    "accounting": "local_request_reservations",
                    "provider_reported": False,
                    "unit": "credits",
                    "limit": getattr(quota, "limit", None),
                    "used": None,
                    "remaining": None,
                    "resets_at": None,
                    "reserve": getattr(quota, "reserve", None),
                    "usable_limit": None,
                    "usable_remaining": None,
                    "period_seconds": getattr(quota, "period_seconds", None),
                    "status": "temporarily_unavailable",
                }
        self._quota_snapshots = result
        self._quota_updated_at = utc_now().isoformat().replace("+00:00", "Z")

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
        is_current = getattr(self.service, "is_generation_current", None)
        if callable(is_current) and not is_current(self.generation_id):
            return
        from .storage import ProviderCheckpointRecord

        updated_at = utc_now()
        for (provider, feed), symbols in self._checkpoint_state.items():
            if callable(is_current) and not is_current(self.generation_id):
                return
            try:
                record = ProviderCheckpointRecord(
                    provider=provider,
                    feed=feed,
                    updated_at=updated_at,
                    checkpoint={"symbols": dict(symbols)},
                )
                await storage.enqueue_checkpoint_record(
                    record,
                    wait=True,
                )
                remember = getattr(self.service, "remember_provider_checkpoint", None)
                if callable(remember):
                    remember(record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"checkpoint:{provider}:{feed}", exc)
        for provider_name, provider in self.graph.providers.items():
            if callable(is_current) and not is_current(self.generation_id):
                return
            quota = getattr(provider, "quota", None)
            if quota is None:
                continue
            try:
                record = ProviderCheckpointRecord(
                    provider=provider_name,
                    feed="quota",
                    updated_at=updated_at,
                    checkpoint=await quota.checkpoint(),
                )
                await storage.enqueue_checkpoint_record(
                    record,
                    wait=True,
                )
                remember = getattr(self.service, "remember_provider_checkpoint", None)
                if callable(remember):
                    remember(record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"checkpoint:{provider_name}:quota", exc)

    def _record_error(self, job: str, exc: BaseException) -> None:
        if isinstance(exc, AllProvidersFailed):
            reason = "all_providers_failed"
        elif isinstance(exc, ProviderError):
            reason = type(exc).__name__
        else:
            reason = type(exc).__name__
        self._last_errors[job] = {"reason": reason, "at": utc_now().isoformat()}

    def _mark_source_failed(self, *symbols: str) -> None:
        marker = getattr(self.service, "mark_source_failed", None)
        if callable(marker):
            kwargs: dict[str, Any] = {}
            if self._accepts_generation_id(marker) and self.generation_id is not None:
                kwargs["generation_id"] = self.generation_id
            marker(*symbols, **kwargs)

    def metrics(self) -> dict[str, Any]:
        return {
            "running": self.is_running,
            "fatal_error": (
                None
                if self._fatal_error is None
                else {
                    "type": type(self._fatal_error).__name__,
                    "message": "collector runtime failed",
                }
            ),
            "fallback_counts": self.router.fallback_counts(),
            "circuits": [dataclasses.asdict(item) for item in self.router.circuit_snapshots()],
            "quota": self._quota_snapshots,
            "quota_updated_at": self._quota_updated_at,
            "websocket_reconnects": dict(self._websocket_reconnects),
            "streams": {name: dict(value) for name, value in self._stream_statistics.items()},
            "last_errors": dict(self._last_errors),
        }
