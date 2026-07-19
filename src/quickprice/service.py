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

from .analytics import (
    boxx_yield,
    calculate_changes,
    calculate_changes_from_references,
    quarterly_dividend,
    sgov_yield,
    treasury_proxy_yield,
)
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
from .plugin_api import YieldStrategy
from .registry import InstrumentRegistry, build_registry
from .runtime import (
    FreeThreadedStatus,
    RuntimeGeneration,
    RuntimeGenerationManager,
    RuntimeRegistryView,
    inspect_free_threaded_runtime,
)
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


@dataclasses.dataclass(slots=True)
class _RuntimeDataState:
    """Generation-owned mutable market state.

    A request captures this object together with its registry. Candidate warm-up
    writes to a clone, so a catalog switch cannot expose definitions from one
    generation with quotes or income metadata from another.
    """

    snapshots: SnapshotStore = dataclasses.field(default_factory=SnapshotStore)
    history: HistoryCache = dataclasses.field(default_factory=HistoryCache)
    dividends: dict[str, DividendEvent] = dataclasses.field(default_factory=dict)
    yield_metrics: dict[str, YieldMetric] = dataclasses.field(default_factory=dict)
    yield_stale_after_seconds: dict[str, float] = dataclasses.field(default_factory=dict)
    last_quotes: dict[str, ProviderQuote] = dataclasses.field(default_factory=dict)
    source_failures: set[str] = dataclasses.field(default_factory=set)
    wire_cache: dict[str, QuoteModel] = dataclasses.field(default_factory=dict)
    complete_symbols: set[str] = dataclasses.field(default_factory=set)
    active_aggregates: dict[str, AggregatePrice] = dataclasses.field(default_factory=dict)

    def clone(self) -> _RuntimeDataState:
        return _RuntimeDataState(
            snapshots=self.snapshots.clone(),
            # History is registry-neutral and potentially much larger than the
            # quote snapshot map. Candidate warm-up stages changed points
            # separately and publishes only those after the pointer switch.
            history=self.history,
            dividends=dict(self.dividends),
            yield_metrics=dict(self.yield_metrics),
            yield_stale_after_seconds=dict(self.yield_stale_after_seconds),
            last_quotes=dict(self.last_quotes),
            source_failures=set(self.source_failures),
            wire_cache=dict(self.wire_cache),
            complete_symbols=set(self.complete_symbols),
            active_aggregates=dict(self.active_aggregates),
        )

    def retain_symbols(self, symbols: tuple[str, ...]) -> None:
        retained = set(symbols)
        self.snapshots.retain_symbols(retained)
        self.history.retain_symbols(retained)
        for values in (
            self.dividends,
            self.yield_metrics,
            self.yield_stale_after_seconds,
            self.last_quotes,
            self.wire_cache,
            self.active_aggregates,
        ):
            for symbol in tuple(values):
                if symbol not in retained:
                    values.pop(symbol, None)
        self.source_failures.intersection_update(retained)
        self.complete_symbols.intersection_update(retained)


class QuickPriceService:
    def __init__(
        self,
        settings: Settings,
        registry: InstrumentRegistry | None = None,
        *,
        runtime_revision: str | None = None,
        runtime_catalog: Any = None,
    ) -> None:
        self.settings = settings
        initial_registry = (
            build_registry(settings.enabled_plugins) if registry is None else registry
        )
        initial_state = _RuntimeDataState()
        self._generations = RuntimeGenerationManager(
            initial_registry,
            revision=runtime_revision,
            catalog=runtime_catalog,
            data_state=initial_state,
        )
        self._registry_view = RuntimeRegistryView(self._generations)
        self.metrics = Metrics()
        self.runtime_status: FreeThreadedStatus | None = None
        self._metadata_lock = RLock()
        self._bind_runtime_state(initial_state)
        self._provider_checkpoints: dict[tuple[str, str], Any] = {}
        self._provider_quota_lock = RLock()
        self._provider_quotas: dict[str, Any] = {}
        self._storage: Any = None
        self._coordinator: Any = None
        self._collector_start_error: BaseException | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._persistence_tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        self._storage_ready = False
        self._api_key_configured: Any = None
        self._instrument_catalog: Any = None
        self._catalog_runtime: Any = None
        self._activation_lock = asyncio.Lock()
        self._activation_jobs: dict[str, Any] = {}

    @property
    def registry(self) -> InstrumentRegistry:
        """Return the immutable registry in the currently active generation."""

        return self._generations.capture().registry

    @property
    def registry_view(self) -> RuntimeRegistryView:
        """Expose a compatibility mapping that follows future activations."""

        return self._registry_view

    def capture_generation(self) -> RuntimeGeneration:
        """Capture one consistent data-plane view for a complete request."""

        return self._generations.capture()

    def _bind_runtime_state(self, state: _RuntimeDataState) -> None:
        """Update compatibility attributes after an atomic generation switch."""

        self.snapshots = state.snapshots
        self.history = state.history
        self._dividends = state.dividends
        self._yield_metrics = state.yield_metrics
        self._yield_stale_after_seconds = state.yield_stale_after_seconds
        self._last_quotes = state.last_quotes
        self._source_failures = state.source_failures
        self._wire_cache = state.wire_cache
        self._complete_symbols = state.complete_symbols
        self._active_aggregates = state.active_aggregates

    def _state_for_generation(
        self,
        generation: RuntimeGeneration | None = None,
    ) -> _RuntimeDataState:
        generation = generation or self._generations.capture()
        state = generation.data_state
        if not isinstance(state, _RuntimeDataState):
            raise RuntimeError("runtime generation has no market state")
        return state

    def is_generation_current(self, generation_id: str | None) -> bool:
        """Allow collectors to fence durable writes during a handoff."""

        return self._generations.is_current(generation_id)

    def bind_instrument_catalog(
        self,
        store: Any,
        *,
        audit_sink: Any = None,
    ) -> None:
        """Attach the authenticated control plane to the runtime generation manager."""

        from .catalog_runtime import InstrumentCatalogRuntime

        self._instrument_catalog = store
        self._catalog_runtime = InstrumentCatalogRuntime(
            self,
            self.settings,
            store,
            audit_sink=audit_sink,
        )

    async def validate_instrument_catalog(self, expected_revision: str) -> dict[str, Any]:
        if self._catalog_runtime is None:
            raise RuntimeError("instrument catalog runtime is not configured")
        return await self._catalog_runtime.validate(expected_revision)

    async def activate_instrument_catalog(
        self,
        expected_revision: str,
        *,
        audit: Any = None,
    ) -> dict[str, Any]:
        if self._catalog_runtime is None:
            raise RuntimeError("instrument catalog runtime is not configured")
        return await self._catalog_runtime.request_activation(expected_revision, audit=audit)

    async def rollback_instrument_catalog(
        self,
        expected_revision: str,
        *,
        audit: Any = None,
    ) -> dict[str, Any]:
        if self._catalog_runtime is None:
            raise RuntimeError("instrument catalog runtime is not configured")
        return await self._catalog_runtime.request_rollback(expected_revision, audit=audit)

    def instrument_catalog_job(self, job_id: str) -> dict[str, Any]:
        if self._catalog_runtime is None:
            raise RuntimeError("instrument catalog runtime is not configured")
        return self._catalog_runtime.job(job_id)

    def provider_catalog_snapshot(self) -> dict[str, Any]:
        from .providers.descriptors import provider_catalog_snapshot

        return provider_catalog_snapshot(self.settings)

    async def search_provider_symbols(
        self,
        provider: str,
        query: str,
        *,
        asset_class: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        from .providers.base import ProviderRateLimited, ProviderUnavailable
        from .providers.descriptors import search_provider_symbols

        started = asyncio.get_running_loop().time()
        outcome = "unexpected"
        try:
            result = await search_provider_symbols(
                self.settings,
                provider,
                query,
                asset_class=asset_class,
                limit=limit,
                credit_reserver=self.reserve_provider_search_credit,
            )
        except ProviderRateLimited:
            outcome = "rate_limited"
            raise
        except ProviderUnavailable as exc:
            outcome = "rate_limited" if exc.status == 429 else "unavailable"
            raise
        except TimeoutError:
            outcome = "timeout"
            raise
        except Exception:
            outcome = "unexpected"
            raise
        else:
            outcome = "success"
            return result
        finally:
            elapsed_ms = (asyncio.get_running_loop().time() - started) * 1_000
            self.metrics.observe_provider_operation(provider, "other", outcome, elapsed_ms)

    async def reserve_provider_search_credit(self, provider: str, cost: int) -> bool:
        """Reserve admin-search traffic from the collectors' durable quota ledger."""

        if cost <= 0:
            raise ValueError("provider search credit cost must be positive")
        with self._provider_quota_lock:
            quota = self._provider_quotas.get(provider)
        if quota is None:
            from .providers.base import ProviderUnavailable

            raise ProviderUnavailable(
                provider,
                "shared provider quota ledger is not initialized",
                status=503,
            )
        return bool(await quota.acquire(cost, allow_reserve=False))

    def _activate_runtime_generation(
        self,
        registry: InstrumentRegistry,
        *,
        revision: str,
        catalog: Any = None,
        route_plan: Any = None,
        data_state: _RuntimeDataState | None = None,
        generation_id: str | None = None,
    ) -> tuple[RuntimeGeneration, RuntimeGeneration]:
        """Commit a generation already validated and warmed by the control plane."""

        if data_state is None:
            data_state = self._state_for_generation()
        return self._generations.activate(
            registry,
            revision=revision,
            catalog=catalog,
            route_plan=route_plan,
            data_state=data_state,
            generation_id=generation_id,
        )

    def _install_warmed_instruments(
        self,
        registry: InstrumentRegistry,
        warmed: tuple[Any, ...],
        *,
        state: _RuntimeDataState | None = None,
    ) -> None:
        """Install a validated warm bundle into one isolated runtime state."""

        state = state or self._state_for_generation()

        for item in warmed:
            definition = item.definition
            instrument = registry.resolve(definition.symbol)
            if instrument is None or instrument.symbol != definition.symbol:
                raise ValueError("warm bundle contains an inactive instrument")
            quote = item.quote
            if not isinstance(quote, ProviderQuote):
                raise TypeError("warm quote is not a ProviderQuote")
            if quote.symbol != definition.symbol:
                raise ValueError("warm quote symbol does not match its instrument")
            if quote.as_of > utc_now() + timedelta(seconds=60):
                raise ValueError("warm quote timestamp is more than 60 seconds in the future")
            with self._metadata_lock:
                if item.dividend is not None:
                    if not isinstance(item.dividend, DividendEvent):
                        raise TypeError("warm dividend is not a DividendEvent")
                    if item.dividend.symbol != definition.symbol:
                        raise ValueError("warm dividend symbol does not match its instrument")
                    state.dividends[definition.symbol] = item.dividend
                if item.yield_metric is not None:
                    if not isinstance(item.yield_metric, YieldMetric):
                        raise TypeError("warm yield is not a YieldMetric")
                    if item.yield_metric.symbol != definition.symbol:
                        raise ValueError("warm yield symbol does not match its instrument")
                    state.yield_metrics[definition.symbol] = item.yield_metric
                    state.yield_stale_after_seconds[definition.symbol] = (
                        self._default_yield_stale_after_seconds(item.yield_metric)
                    )
                state.last_quotes[definition.symbol] = quote
                state.source_failures.discard(definition.symbol)
            minute = quote.as_of.replace(second=0, microsecond=0)
            point = PricePoint(
                definition.symbol,
                minute,
                quote.price,
                quote.provider,
                quote.is_derived,
                "1m",
            )
            self._rebuild(
                definition.symbol,
                persist=False,
                registry=registry,
                state=state,
                additional_history=(point,),
            )
            self._update_aggregate(point, state=state)
            if definition.symbol not in state.complete_symbols:
                raise ValueError("warm bundle is missing mandatory income metadata")

    async def _prepare_runtime_state(
        self,
        registry: InstrumentRegistry,
        warmed: tuple[Any, ...],
        *,
        retained_symbols: tuple[str, ...] = (),
    ) -> _RuntimeDataState:
        """Build all candidate quote state before publishing its generation."""

        state = self._state_for_generation().clone()
        if retained_symbols:
            await self._restore_retained_candidate_state(state, retained_symbols)
        self._install_warmed_instruments(registry, warmed, state=state)
        return state

    async def _restore_retained_candidate_state(
        self,
        state: _RuntimeDataState,
        symbols: tuple[str, ...],
    ) -> None:
        """Restore archived state into an unpublished candidate generation.

        The history cache is intentionally shared between generations because
        cloning up to 400 days for every activation would make activation cost
        proportional to the complete catalog. Only symbols absent from the
        active registry reach this method, so their restored rings cannot be
        resolved through the old public generation. Candidate metadata maps
        remain isolated, and the subsequently warmed values win.
        """

        storage = self._storage
        restore = None if storage is None else getattr(storage, "restore", None)
        if not callable(restore):
            return
        restored = restore(symbols=symbols)
        if inspect.isawaitable(restored):
            restored = await restored
        if restored is None:
            return
        allowed = frozenset(symbols)
        points = [
            point
            for point in (getattr(restored, "price_points", None) or ())
            if isinstance(point, PricePoint) and point.symbol in allowed
        ]
        if points:
            await asyncio.to_thread(state.history.load, points)

        yield_records = {
            record.symbol: record
            for record in (getattr(restored, "yield_metric_records", None) or ())
            if getattr(record, "symbol", None) in allowed
        }
        with self._metadata_lock:
            for event in getattr(restored, "dividends", None) or ():
                if not isinstance(event, DividendEvent) or event.symbol not in allowed:
                    continue
                current = state.dividends.get(event.symbol)
                if current is None or event.ex_date > current.ex_date:
                    state.dividends[event.symbol] = event
            for metric in getattr(restored, "yield_metrics", None) or ():
                if not isinstance(metric, YieldMetric) or metric.symbol not in allowed:
                    continue
                current = state.yield_metrics.get(metric.symbol)
                if current is not None and metric.as_of <= current.as_of:
                    continue
                state.yield_metrics[metric.symbol] = metric
                record = yield_records.get(metric.symbol)
                persisted_threshold = (
                    None if record is None else record.raw.get("stale_after_seconds")
                )
                state.yield_stale_after_seconds[metric.symbol] = (
                    self._restored_yield_stale_after_seconds(
                        metric,
                        persisted_threshold,
                    )
                )
            for quote in getattr(restored, "quotes", None) or ():
                if not isinstance(quote, ProviderQuote) or quote.symbol not in allowed:
                    continue
                current = state.last_quotes.get(quote.symbol)
                if current is None or quote.as_of > current.as_of:
                    state.last_quotes[quote.symbol] = quote

    @staticmethod
    def _publish_warmed_history(
        warmed: tuple[Any, ...],
        state: _RuntimeDataState,
    ) -> None:
        """Append only changed warm points after the generation switch."""

        for item in warmed:
            quote = item.quote
            state.history.add(
                PricePoint(
                    item.definition.symbol,
                    quote.as_of.replace(second=0, microsecond=0),
                    quote.price,
                    quote.provider,
                    quote.is_derived,
                    "1m",
                )
            )

    def _persist_warmed_instruments(
        self,
        warmed: tuple[Any, ...],
        state: _RuntimeDataState,
    ) -> None:
        """Queue persistence only after the prepared generation is visible."""

        for item in warmed:
            symbol = item.definition.symbol
            quote = item.quote
            point = PricePoint(
                symbol,
                quote.as_of.replace(second=0, microsecond=0),
                quote.price,
                quote.provider,
                quote.is_derived,
                "1m",
            )
            self._persist("enqueue_price", point)
            aggregate = state.active_aggregates.get(symbol)
            if aggregate is not None:
                self._persist("enqueue_aggregate_price", aggregate)
            if item.dividend is not None:
                self._persist("enqueue_dividend", item.dividend)
            if item.yield_metric is not None:
                self._persist_yield_metric(
                    item.yield_metric,
                    state.yield_stale_after_seconds[symbol],
                )
            snapshot = state.snapshots.get(symbol)
            if snapshot is not None:
                self._persist("enqueue_snapshot", snapshot)

    async def _commit_catalog_activation(
        self,
        *,
        operation: str,
        expected_file_revision: str,
        target: Any,
        registry: InstrumentRegistry,
        route_plan: Any,
        generation_id: str,
        warmed: tuple[Any, ...],
        candidate_coordinator: Any,
        reconcile_provider_names: tuple[str, ...] | None = None,
    ) -> None:
        """Commit a prepared disk/runtime generation with one pointer publication."""

        if self._instrument_catalog is None:
            raise RuntimeError("instrument catalog runtime is not configured")
        previous = self._generations.capture()
        previous_coordinator = self._coordinator
        previous_collector_start_error = self._collector_start_error
        reusing_coordinator = (
            candidate_coordinator is not None and candidate_coordinator is previous_coordinator
        )
        reconciling_coordinator = (
            reconcile_provider_names is not None
            and candidate_coordinator is not None
            and previous_coordinator is not None
            and candidate_coordinator is not previous_coordinator
        )
        previous_coordinator_target: tuple[InstrumentRegistry, str | None] | None = None
        reconciliation_token: Any = None
        active_coordinator = candidate_coordinator
        file_committed = False
        file_snapshot: dict[str, Any] | None = None
        transition_token: object | None = None
        async with self._activation_lock:
            try:
                lease_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._instrument_catalog.capture_transition,
                        expected_file_revision,
                    ),
                    name="catalog-transition-lease",
                )
                try:
                    transition_token = await asyncio.shield(lease_task)
                except asyncio.CancelledError:
                    # Acquiring the in-process lease is a worker-thread action;
                    # join it so cancellation cannot orphan an unknown lease.
                    transition_token = await lease_task
                    raise
                retained_symbols = tuple(
                    sorted(set(registry.symbols) - set(previous.registry.symbols))
                )
                candidate_state = await self._prepare_runtime_state(
                    registry,
                    warmed,
                    retained_symbols=retained_symbols,
                )
                if (
                    candidate_coordinator is not None
                    and not reusing_coordinator
                    and not reconciling_coordinator
                ):
                    # Prove the fresh supervisor can start while both the
                    # durable catalog and public runtime still point at the old
                    # generation. Early publications carry the candidate id and
                    # are therefore rejected by generation fencing.
                    candidate_coordinator.activate(gated=True)
                    candidate_coordinator.release_activation()
                    await candidate_coordinator.wait_started_or_failed()
                if operation == "activate":
                    transition = asyncio.create_task(
                        asyncio.to_thread(
                            self._instrument_catalog.activate_staged,
                            expected_file_revision,
                            target.revision,
                            transition_token=transition_token,
                        ),
                        name="catalog-file-activate",
                    )
                elif operation == "rollback":
                    transition = asyncio.create_task(
                        asyncio.to_thread(
                            self._instrument_catalog.rollback,
                            expected_file_revision,
                            transition_token=transition_token,
                        ),
                        name="catalog-file-rollback",
                    )
                else:
                    raise ValueError("unsupported catalog activation operation")
                try:
                    file_snapshot = await asyncio.shield(transition)
                except asyncio.CancelledError:
                    # A worker-thread filesystem transition cannot be killed.
                    # Join it so the outer rollback sees a definite revision.
                    file_snapshot = await transition
                    file_committed = True
                    raise
                file_committed = True
                active = await asyncio.to_thread(self._instrument_catalog.active_generation)
                if active.revision != target.revision:
                    raise RuntimeError("persisted catalog does not match the warmed generation")
                file_revision = file_snapshot.get("revision") if file_snapshot is not None else None
                if not isinstance(file_revision, str) or not file_revision:
                    raise RuntimeError("catalog commit did not return an exact file revision")
                await asyncio.to_thread(
                    self._instrument_catalog.mark_runtime_applied,
                    file_revision,
                )
                if candidate_coordinator is not None:
                    if reusing_coordinator:
                        previous_coordinator_target = candidate_coordinator.adopt_generation(
                            registry,
                            generation_id,
                        )
                        active_coordinator = candidate_coordinator
                    elif reconciling_coordinator:
                        reconciliation_token = await previous_coordinator.reconcile_generation(
                            candidate_coordinator,
                            registry,
                            generation_id,
                            reconcile_provider_names,
                        )
                        active_coordinator = previous_coordinator
                self._activate_runtime_generation(
                    registry,
                    revision=target.revision,
                    catalog=target,
                    route_plan=route_plan,
                    data_state=candidate_state,
                    generation_id=generation_id,
                )
                self._bind_runtime_state(candidate_state)
                self._coordinator = active_coordinator
                self._collector_start_error = None
                if (
                    active_coordinator is candidate_coordinator
                    and candidate_coordinator is not None
                    and not reusing_coordinator
                    and not reconciling_coordinator
                ):
                    self._tasks.append(
                        asyncio.create_task(
                            self._monitor_collector_run(candidate_coordinator),
                            name=f"collector-runtime-monitor:{generation_id}",
                        )
                    )
                if reconciliation_token is not None:
                    # Reconciled tasks are failure-gated until the transition
                    # lease is released. Check once more after pointer
                    # publication so a delayed startup failure can still restore
                    # both the runtime and exact catalog file.
                    await previous_coordinator.confirm_reconciliation(reconciliation_token)
                # This bounded local operation performs one revision check,
                # reads at most the manifest size limit, and releases an RLock.
                # Keeping it synchronous closes the confirm-to-commit race:
                # guarded component tasks cannot fail between the final health
                # check and the activation point of no return.
                self._instrument_catalog.commit_transition(
                    transition_token,
                    file_revision,
                )
                transition_token = None

                # The transition lease is now committed and a concurrent admin
                # draft may be created. Resource retirement is best-effort from
                # this point and must never roll the active catalog backward.
                if reconciliation_token is not None:
                    reconciliation_failure = getattr(
                        reconciliation_token,
                        "failure",
                        None,
                    )
                    if (
                        isinstance(reconciliation_failure, asyncio.Future)
                        and reconciliation_failure.done()
                    ):
                        _LOGGER.error("Collector reconciliation failed after transition commit")
                    cleanup_task = asyncio.create_task(
                        previous_coordinator.finalize_reconciliation(reconciliation_token),
                        name="catalog-reconciliation-finalize",
                    )
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        await cleanup_task
                        current_task = asyncio.current_task()
                        if current_task is not None:
                            current_task.uncancel()
                    except Exception as finalize_exc:
                        _LOGGER.warning(
                            "Collector reconciliation cleanup failed error_type=%s",
                            type(finalize_exc).__name__,
                        )
                elif (
                    previous_coordinator is not None
                    and previous_coordinator is not active_coordinator
                ):
                    cleanup_task = asyncio.create_task(
                        self._retire_coordinator(previous_coordinator),
                        name="catalog-retired-coordinator-cleanup",
                    )
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        await cleanup_task
                        current_task = asyncio.current_task()
                        if current_task is not None:
                            current_task.uncancel()
                    except Exception as retire_exc:
                        _LOGGER.warning(
                            "Retired collector cleanup failed error_type=%s",
                            type(retire_exc).__name__,
                        )

                # Everything below is local, non-blocking publication into the
                # active generation; it must not reopen the disk transaction.
                try:
                    self._publish_warmed_history(warmed, candidate_state)
                    candidate_state.retain_symbols(registry.symbols)
                    self._persist_warmed_instruments(warmed, candidate_state)
                except Exception as finalize_exc:
                    self._storage_ready = False
                    _LOGGER.error(
                        "Catalog activation local finalization failed error_type=%s",
                        type(finalize_exc).__name__,
                    )
            except BaseException:
                if self._generations.capture().generation_id != previous.generation_id:
                    self._generations.activate(
                        previous.registry,
                        revision=previous.revision,
                        catalog=previous.catalog,
                        route_plan=previous.route_plan,
                        data_state=previous.data_state,
                        generation_id=previous.generation_id,
                    )
                    self._bind_runtime_state(self._state_for_generation())
                previous.data_state.history.retain_symbols(previous.registry.symbols)
                if reusing_coordinator and previous_coordinator_target is not None:
                    old_registry, old_generation_id = previous_coordinator_target
                    try:
                        candidate_coordinator.adopt_generation(
                            old_registry,
                            old_generation_id,
                        )
                    except Exception:
                        candidate_coordinator.registry = old_registry
                        candidate_coordinator.generation_id = old_generation_id
                if reconciliation_token is not None:
                    try:
                        await asyncio.shield(
                            previous_coordinator.rollback_reconciliation(reconciliation_token)
                        )
                    except Exception as reconcile_exc:
                        _LOGGER.critical(
                            "Collector reconciliation rollback failed error_type=%s",
                            type(reconcile_exc).__name__,
                        )
                self._coordinator = previous_coordinator
                self._collector_start_error = previous_collector_start_error
                if (
                    candidate_coordinator is not None
                    and candidate_coordinator is not previous_coordinator
                ):
                    try:
                        await candidate_coordinator.stop(persist_checkpoints=False)
                    except Exception as stop_exc:
                        _LOGGER.error(
                            "Candidate collector shutdown failed error_type=%s",
                            type(stop_exc).__name__,
                        )
                if file_committed and file_snapshot is not None and transition_token is not None:
                    try:
                        restored_file = await asyncio.to_thread(
                            self._instrument_catalog.restore_transition,
                            transition_token,
                            file_snapshot["revision"],
                        )
                        await asyncio.to_thread(
                            self._instrument_catalog.mark_runtime_applied,
                            restored_file["revision"],
                        )
                        transition_token = None
                    except Exception as rollback_exc:
                        _LOGGER.critical(
                            "Instrument catalog file rollback failed error_type=%s",
                            type(rollback_exc).__name__,
                        )
                if transition_token is not None:
                    try:
                        await asyncio.to_thread(
                            self._instrument_catalog.abort_transition,
                            transition_token,
                        )
                        transition_token = None
                    except Exception as abort_exc:
                        _LOGGER.critical(
                            "Instrument catalog lease release failed error_type=%s",
                            type(abort_exc).__name__,
                        )
                raise

    @staticmethod
    async def _retire_coordinator(coordinator: Any) -> None:
        """Boundedly stop a retired collector and prove no task remains live."""

        stop_error: BaseException | None = None
        try:
            await coordinator.stop(persist_checkpoints=False)
        except asyncio.CancelledError as exc:
            stop_error = exc
        except Exception as exc:
            stop_error = exc
        supervisor = getattr(coordinator, "_supervisor", None)
        if isinstance(supervisor, asyncio.Task) and not supervisor.done():
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
            try:
                coordinator._supervisor = None
            except AttributeError:
                pass
        if bool(getattr(coordinator, "is_running", False)):
            raise RuntimeError("retired collector remained active after forced shutdown")
        if isinstance(stop_error, asyncio.CancelledError):
            raise stop_error
        if stop_error is not None:
            graph = getattr(coordinator, "graph", None)
            close = getattr(graph, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception as close_exc:
                    _LOGGER.warning(
                        "Retired collector resource cleanup failed error_type=%s",
                        type(close_exc).__name__,
                    )
            _LOGGER.warning(
                "Retired collector required forced shutdown error_type=%s",
                type(stop_error).__name__,
            )

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
        if self._catalog_runtime is not None:
            await self._catalog_runtime.stop()
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
                restore_history = getattr(self._storage, "restore_history_into", None)
                if callable(restore_history):
                    stage = "restore_history"
                    history_result = restore_history(
                        self.history,
                        symbols=self.registry.symbols,
                    )
                    if inspect.isawaitable(history_result):
                        await history_result
                    stage = "restore_metadata"
                    restored = restore(include_history=False)
                else:
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
        graph = None
        coordinator = None
        try:
            from .collectors import MarketDataCoordinator

            generation = self._generations.capture()
            if generation.catalog is not None:
                from .providers.compiler import build_compiled_provider_graph

                graph, route_plan = build_compiled_provider_graph(
                    self.settings,
                    generation.registry,
                    generation.catalog.definitions,
                    metrics=self.metrics,
                    strict=self.settings.production and self.settings.background_enabled,
                )
                self._generations.activate(
                    generation.registry,
                    revision=generation.revision,
                    catalog=generation.catalog,
                    route_plan=route_plan,
                    data_state=generation.data_state,
                    generation_id=generation.generation_id,
                )
            coordinator = MarketDataCoordinator(
                self,
                self.settings,
                generation.registry,
                generation_id=generation.generation_id,
                graph=graph,
                catalog=generation.catalog,
            )
            self._coordinator = coordinator
            await coordinator.start()
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
            if coordinator is not None:
                try:
                    await coordinator.stop()
                except Exception:
                    pass
            elif graph is not None:
                try:
                    await graph.close()
                except Exception:
                    pass
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

    def _publication_context(
        self,
        generation_id: str | None,
    ) -> tuple[RuntimeGeneration, _RuntimeDataState] | None:
        generation = self._generations.capture()
        if generation_id is not None and generation.generation_id != generation_id:
            return None
        return generation, self._state_for_generation(generation)

    def publish_history(
        self,
        points: list[PricePoint],
        *,
        persist: bool = True,
        generation_id: str | None = None,
    ) -> bool:
        context = self._publication_context(generation_id)
        if context is None:
            return False
        generation, state = context
        registry = generation.registry
        self._validate_history_timestamps(points, registry=registry)
        state.history.load(points)
        if not self._generations.is_current(generation.generation_id):
            return False
        self._finish_history_publication(
            points,
            persist=persist,
            registry=registry,
            state=state,
        )
        return True

    async def publish_history_async(
        self,
        points: list[PricePoint],
        *,
        persist: bool = True,
        generation_id: str | None = None,
    ) -> bool:
        """Merge a large backfill without blocking the HTTP event loop."""

        async with self._activation_lock:
            context = self._publication_context(generation_id)
            if context is None:
                return False
            generation, state = context
            registry = generation.registry
            self._validate_history_timestamps(points, registry=registry)
            await asyncio.to_thread(state.history.load, points)
            if not self._generations.is_current(generation.generation_id):
                return False
            self._finish_history_publication(
                points,
                persist=persist,
                registry=registry,
                state=state,
            )
            return True

    def _finish_history_publication(
        self,
        points: list[PricePoint],
        *,
        persist: bool,
        registry: InstrumentRegistry,
        state: _RuntimeDataState,
    ) -> None:
        if persist and points:
            self._persist_history(points)
        for symbol in {point.symbol for point in points}:
            self._rebuild(symbol, persist=False, registry=registry, state=state)

    def publish_quote(
        self,
        quote: ProviderQuote,
        *,
        persist: bool = True,
        generation_id: str | None = None,
    ) -> bool:
        context = self._publication_context(generation_id)
        if context is None:
            return False
        generation, state = context
        registry = generation.registry
        instrument = registry.resolve(quote.symbol)
        if instrument is None or quote.symbol != instrument.symbol:
            raise ValueError(f"unsupported symbol: {quote.symbol}")
        if quote.as_of > utc_now() + timedelta(seconds=60):
            raise ValueError("quote timestamp is more than 60 seconds in the future")
        with self._metadata_lock:
            current = state.last_quotes.get(quote.symbol)
            if current is not None and quote.as_of < current.as_of:
                return False
            if not self._generations.is_current(generation.generation_id):
                return False
            state.last_quotes[quote.symbol] = quote
            state.source_failures.discard(quote.symbol)
        minute = quote.as_of.replace(second=0, microsecond=0)
        point = PricePoint(
            quote.symbol,
            minute,
            quote.price,
            quote.provider,
            quote.is_derived,
            "1m",
        )
        state.history.add(point)
        self._rebuild(
            quote.symbol,
            persist=persist,
            registry=registry,
            state=state,
        )
        if persist:
            self._persist("enqueue_price", point)
            self._persist(
                "enqueue_aggregate_price",
                self._update_aggregate(point, state=state),
            )
        return True

    def _validate_history_timestamps(
        self,
        points: list[PricePoint],
        *,
        registry: InstrumentRegistry | None = None,
    ) -> None:
        registry = registry or self.registry
        future_limit = utc_now() + timedelta(seconds=60)
        if any(point.timestamp > future_limit for point in points):
            raise ValueError("history contains a timestamp more than 60 seconds in the future")
        invalid = [
            point.symbol
            for point in points
            if (item := registry.resolve(point.symbol)) is None or item.symbol != point.symbol
        ]
        if invalid:
            raise ValueError(
                f"history contains unsupported symbols: {', '.join(sorted(set(invalid)))}"
            )

    def restored_provider_checkpoints(self) -> dict[tuple[str, str], Any]:
        with self._provider_quota_lock:
            return dict(self._provider_checkpoints)

    def share_provider_quota(self, provider_name: str, candidate: Any) -> tuple[Any, bool]:
        """Return one process-wide ledger for an upstream provider.

        Shadow warm-up and the active collector can overlap. Sharing the quota
        object prevents either side from restoring or persisting an older
        counter over credits consumed by the other.
        """

        with self._provider_quota_lock:
            current = self._provider_quotas.get(provider_name)
            if current is None:
                self._provider_quotas[provider_name] = candidate
                return candidate, True
            candidate_policy = (
                getattr(candidate, "limit", None),
                getattr(candidate, "period_seconds", None),
                getattr(candidate, "reserve", None),
            )
            current_policy = (
                getattr(current, "limit", None),
                getattr(current, "period_seconds", None),
                getattr(current, "reserve", None),
            )
            if candidate_policy != current_policy:
                raise RuntimeError("provider quota policy changed during runtime handoff")
            return current, False

    def remember_provider_checkpoint(self, record: Any) -> None:
        """Keep the in-memory restore image at least as new as durable storage."""

        with self._provider_quota_lock:
            key = (record.provider, record.feed)
            current = self._provider_checkpoints.get(key)
            if current is None or record.updated_at >= current.updated_at:
                self._provider_checkpoints[key] = record

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

    def _update_aggregate(
        self,
        point: PricePoint,
        *,
        state: _RuntimeDataState | None = None,
    ) -> AggregatePrice:
        state = state or self._state_for_generation()
        bucket = self._five_minute_bucket(point.timestamp)
        current = state.active_aggregates.get(point.symbol)
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
        state.active_aggregates[point.symbol] = aggregate
        return aggregate

    def publish_dividend(
        self,
        event: DividendEvent,
        *,
        persist: bool = True,
        generation_id: str | None = None,
    ) -> bool:
        context = self._publication_context(generation_id)
        if context is None:
            return False
        generation, state = context
        registry = generation.registry
        instrument = registry.resolve(event.symbol)
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
            return False
        with self._metadata_lock:
            current = state.dividends.get(event.symbol)
            if current is not None and event.ex_date < current.ex_date:
                return False
            if not self._generations.is_current(generation.generation_id):
                return False
            state.dividends[event.symbol] = event
        self._rebuild(
            event.symbol,
            persist=persist,
            registry=registry,
            state=state,
        )
        if persist:
            self._persist("enqueue_dividend", event)
        return True

    def publish_yield_metric(
        self,
        metric: YieldMetric,
        *,
        persist: bool = True,
        generation_id: str | None = None,
    ) -> bool:
        context = self._publication_context(generation_id)
        if context is None:
            return False
        generation, state = context
        registry = generation.registry
        instrument = registry.resolve(metric.symbol)
        if (
            instrument is None
            or metric.symbol != instrument.symbol
            or instrument.yield_strategy is None
        ):
            raise ValueError(f"external yield metric is not configured for {metric.symbol}")
        stale_after_seconds = self._default_yield_stale_after_seconds(metric)
        with self._metadata_lock:
            current = state.yield_metrics.get(metric.symbol)
            if current is not None:
                same_source = (
                    metric.provider.casefold() == current.provider.casefold()
                    and metric.method == current.method
                )
                current_rank = self._yield_source_rank(current)
                incoming_rank = self._yield_source_rank(metric)
                if (same_source or incoming_rank == current_rank) and metric.as_of < current.as_of:
                    return False
                is_downgrade = not same_source and incoming_rank > current_rank
                if is_downgrade:
                    current_stale_after_seconds = state.yield_stale_after_seconds.get(
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
                        return False
            if not self._generations.is_current(generation.generation_id):
                return False
            state.yield_metrics[metric.symbol] = metric
            state.yield_stale_after_seconds[metric.symbol] = stale_after_seconds
        self._rebuild(
            metric.symbol,
            persist=persist,
            registry=registry,
            state=state,
        )
        if persist:
            self._persist_yield_metric(metric, stale_after_seconds)
        return True

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

    def _rebuild(
        self,
        symbol: str,
        *,
        persist: bool,
        registry: InstrumentRegistry | None = None,
        state: _RuntimeDataState | None = None,
        additional_history: tuple[PricePoint, ...] = (),
    ) -> None:
        state = state or self._state_for_generation()
        with self._metadata_lock:
            quote = state.last_quotes.get(symbol)
            event = state.dividends.get(symbol)
            yield_metric = state.yield_metrics.get(symbol)
        if quote is None:
            return
        instrument = (registry or self.registry)[symbol]
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
            instrument.yield_strategy is YieldStrategy.TREASURY_PROXY_MINUS_EXPENSE
            and yield_metric is not None
        ):
            annual_yield = treasury_proxy_yield(yield_metric)
        elif (
            instrument.yield_strategy is YieldStrategy.STAKING_PROVIDER_METRIC
            and yield_metric is not None
        ):
            annual_yield = estimate_from_staking_metric(yield_metric)
        changes = (
            calculate_changes(
                quote.price,
                quote.as_of,
                (*state.history.points(symbol), *additional_history),
            )
            if additional_history
            else calculate_changes_from_references(
                quote.price,
                state.history.change_references(symbol, quote.as_of),
            )
        )
        snapshot = QuoteSnapshot(
            quote=quote,
            changes=changes,
            dividend=dividend,
            estimated_annual_yield=annual_yield,
        )
        state.snapshots.publish(snapshot)
        state.wire_cache[symbol] = snapshot_to_wire(
            snapshot,
            instrument,
            now=quote.as_of,
            stale_after_seconds=instrument.stale_after_seconds,
            yield_stale_after_seconds=state.yield_stale_after_seconds.get(symbol),
        )
        complete = (instrument.yield_strategy is None or annual_yield is not None) and (
            instrument.dividend_strategy is None or dividend is not None
        )
        with self._metadata_lock:
            if complete:
                state.complete_symbols.add(symbol)
            else:
                state.complete_symbols.discard(symbol)
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
        self, snapshot: QuoteSnapshot, instrument: Any, now: datetime
    ) -> str:
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
        self,
        snapshot: QuoteSnapshot,
        instrument: Any,
        now: datetime,
        *,
        state: _RuntimeDataState | None = None,
    ) -> tuple[str, QualityModel]:
        state = state or self._state_for_generation()
        quote = snapshot.quote
        staleness_ms = max(0, int((now - quote.as_of).total_seconds() * 1000))
        threshold_seconds = instrument.stale_after_seconds
        status = self._effective_market_status(snapshot, instrument, now)
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
            stale = stale or quote.symbol in state.source_failures
        return status, QualityModel(stale=stale, staleness_ms=staleness_ms)

    def mark_source_failed(
        self,
        *symbols: str,
        generation_id: str | None = None,
    ) -> bool:
        context = self._publication_context(generation_id)
        if context is None:
            return False
        generation, state = context
        registry = generation.registry
        with self._metadata_lock:
            if not self._generations.is_current(generation.generation_id):
                return False
            state.source_failures.update(symbol for symbol in symbols if symbol in registry)
        return True

    def get_quote(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
        require_complete_metadata: bool = True,
        generation: RuntimeGeneration | None = None,
    ) -> QuoteModel:
        """Project a snapshot with dynamic quote and yield freshness.

        Public market-data routes keep ``require_complete_metadata=True`` so
        mandatory dividend and yield fields remain strict. Authenticated
        operational views may relax that gate while retaining the same market
        status, quote quality, and annual-yield quality calculations.
        """

        generation = generation or self._generations.capture()
        state = self._state_for_generation(generation)
        instrument = generation.registry[symbol]
        snapshot = state.snapshots.get(symbol)
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
        cached = state.wire_cache[symbol]
        now = utc_now() if now is None else now
        market_status, quality = self._quality(snapshot, instrument, now, state=state)
        annual_yield = cached.estimated_annual_yield
        if annual_yield is not None:
            threshold = state.yield_stale_after_seconds.get(symbol)
            if threshold is None:
                metric = state.yield_metrics.get(symbol)
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
        generation = self._generations.capture()
        state = self._state_for_generation(generation)
        with self._metadata_lock:
            all_complete = all(symbol in state.complete_symbols for symbol in generation.registry)
        return (
            runtime_ok
            and self._has_active_api_key()
            and self._storage_ready
            and (collectors_running or not self.settings.background_enabled)
            and all_complete
        )

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        runtime = self.runtime_status or inspect_free_threaded_runtime()
        missing: list[str] = []
        incomplete: list[str] = []
        generation = self._generations.capture()
        state = self._state_for_generation(generation)
        for symbol in generation.registry:
            if state.snapshots.get(symbol) is None:
                missing.append(symbol)
                continue
            try:
                self.get_quote(symbol, generation=generation)
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
            "active_instrument_count": len(generation.registry.symbols),
            "intentionally_empty_catalog": not generation.registry.symbols,
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
                    "message": "collector runtime failed",
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
        generation = self._generations.capture()
        state = self._state_for_generation(generation)
        with self._metadata_lock:
            result["source_failures"] = sorted(state.source_failures)
        result["snapshot_age_ms"] = {
            symbol: max(0, int((now - snapshot.quote.as_of).total_seconds() * 1000))
            for symbol, snapshot in state.snapshots.all().items()
            if symbol in generation.registry
        }
        result["history_ring_points"] = {
            symbol: sizes
            for symbol, sizes in state.history.sizes().items()
            if symbol in generation.registry
        }
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
