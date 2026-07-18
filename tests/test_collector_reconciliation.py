from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from quickprice.catalog_runtime import WarmedInstrument
from quickprice.collectors import MarketDataCoordinator
from quickprice.config import Settings
from quickprice.domain import ProviderQuote
from quickprice.managed_config import InstrumentPolicyStore
from quickprice.metrics import Metrics
from quickprice.plugin_api import InstrumentSpec
from quickprice.providers.base import Capability
from quickprice.providers.router import ProviderRouter
from quickprice.providers.wiring import ProviderGraph
from quickprice.registry import build_registry
from quickprice.service import QuickPriceService


def doge_definition() -> dict[str, object]:
    return {
        "symbol": "DOGE:USDC",
        "base": "DOGE",
        "quote": "USDC",
        "name": "Dogecoin",
        "description": "Dogecoin spot market against USD Coin.",
        "asset_class": "crypto",
        "asset_type": "spot_crypto",
        "price_basis": "last_trade",
        "enabled": True,
        "archived": False,
        "aliases": [],
        "market_calendar": "always_open",
        "quote_poll_seconds": 5,
        "stale_after_seconds": 15,
        "history": {"enabled": True, "poll_seconds": 3_600, "backfill_days": 45},
        "routes": [
            {"capability": "quote", "providers": ["binance"]},
            {"capability": "history", "providers": ["binance"]},
        ],
        "provider_symbols": [{"provider": "binance", "symbol": "DOGEUSDC"}],
        "income": None,
        "synthetic": None,
    }


class FixtureRegistry(Mapping[str, InstrumentSpec]):
    def __init__(self, *symbols: str) -> None:
        source = build_registry()
        self._items = {symbol: source[symbol] for symbol in symbols}

    def __getitem__(self, symbol: str) -> InstrumentSpec:
        return self._items[symbol]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._items)

    def resolve(self, symbol: str) -> str | None:
        return symbol if symbol in self._items else None


class StreamProvider:
    def __init__(self, name: str, *supported: str) -> None:
        self.name = name
        self.stream_symbols = tuple(supported)
        self.subscriptions: list[tuple[str, ...]] = []
        self.connected = asyncio.Event()
        self.disconnects = 0
        self.close_calls = 0

    async def get_quote(self, _symbol: str) -> Any:
        await asyncio.Future()

    async def stream_quotes(self, symbols: tuple[str, ...]):
        self.subscriptions.append(symbols)
        self.connected.set()
        try:
            if False:
                yield None
            await asyncio.Future()
        finally:
            self.disconnects += 1

    async def close(self) -> None:
        self.close_calls += 1


class FixtureService:
    def __init__(self) -> None:
        self.metrics = Metrics()
        self.history = None
        self._storage = None

    @staticmethod
    def restored_provider_checkpoints() -> dict[Any, Any]:
        return {}

    @staticmethod
    def publish_quote(_quote: Any, *, generation_id: str | None = None) -> None:
        del generation_id


def graph_for(
    registry: Any,
    providers: Mapping[str, StreamProvider],
) -> ProviderGraph:
    router = ProviderRouter()
    for symbol in registry:
        provider = providers["alpaca"] if symbol == "QQQM:USD" else providers["binance"]
        router.register(symbol, Capability.QUOTE, (provider,))
    return ProviderGraph(router, dict(providers))


def selected_graph(
    registry: Mapping[str, InstrumentSpec],
    providers: Mapping[str, StreamProvider],
) -> ProviderGraph:
    router = ProviderRouter()
    for provider in providers.values():
        for symbol in provider.stream_symbols:
            if symbol in registry:
                router.register(symbol, Capability.QUOTE, (provider,))
    return ProviderGraph(router, dict(providers))


def make_coordinator(
    service: Any,
    registry: Any,
    providers: Mapping[str, StreamProvider],
    generation_id: str,
    *,
    graph: ProviderGraph | None = None,
) -> MarketDataCoordinator:
    coordinator = MarketDataCoordinator(
        service,
        Settings(production=False, background_enabled=False),
        registry,
        generation_id=generation_id,
        graph=graph or graph_for(registry, providers),
    )

    async def idle() -> None:
        await coordinator._stop.wait()

    async def startup_ready() -> bool:
        return True

    async def startup_noop() -> None:
        return None

    coordinator._startup_preseed_fx_daily = startup_ready
    coordinator._materialize_builtin_fx_history = startup_noop
    coordinator._publish_loop = idle
    coordinator._quote_scheduler_loop = idle
    coordinator._metadata_loop = idle
    coordinator._history_loop = idle
    coordinator._quota_metrics_loop = idle
    coordinator._maintenance_loop = idle
    return coordinator


@pytest.mark.asyncio
async def test_reconcile_retains_unrelated_stream_and_replaces_changed_subscription() -> None:
    service = FixtureService()
    current_registry = FixtureRegistry("QQQM:USD", "BTC:USDC")
    alpaca = StreamProvider("alpaca", "QQQM:USD")
    binance = StreamProvider("binance", "BTC:USDC")
    current = make_coordinator(
        service,
        current_registry,
        {"alpaca": alpaca, "binance": binance},
        "old-generation",
    )
    await current.start()
    await asyncio.gather(alpaca.connected.wait(), binance.connected.wait())
    retained_task = current._component_tasks["stream:alpaca"]
    replaced_task = current._component_tasks["stream:binance"]
    future = asyncio.get_running_loop().time() + 60
    current._quote_next_refresh_at = {
        "QQQM:USD": future,
        "BTC:USDC": future,
    }

    candidate_registry = FixtureRegistry("QQQM:USD", "BTC:USDC", "ETH:USDC")
    candidate_alpaca = StreamProvider("alpaca", "QQQM:USD")
    candidate_binance = StreamProvider("binance", "BTC:USDC", "ETH:USDC")
    candidate = make_coordinator(
        service,
        candidate_registry,
        {"alpaca": candidate_alpaca, "binance": candidate_binance},
        "new-generation",
    )
    await candidate.prepare()

    token = await current.reconcile_generation(
        candidate,
        candidate_registry,
        "new-generation",
        ("alpaca",),
    )
    await candidate_binance.connected.wait()

    assert current._component_tasks["stream:alpaca"] is retained_task
    assert not retained_task.done()
    assert alpaca.subscriptions == [("QQQM:USD",)]
    assert candidate_alpaca.subscriptions == []
    assert current.graph.providers["alpaca"] is alpaca
    assert current._quote_next_refresh_at == {"QQQM:USD": future}
    assert current._component_tasks["stream:binance"] is not replaced_task
    assert replaced_task.done()
    assert binance.disconnects == 1
    assert candidate_binance.subscriptions == [("BTC:USDC", "ETH:USDC")]

    await current.finalize_reconciliation(token)
    await candidate.stop(persist_checkpoints=False)
    assert candidate_binance.close_calls == 0
    assert alpaca.close_calls == 0
    await current.stop(persist_checkpoints=False)
    assert candidate_binance.close_calls == 1
    assert alpaca.close_calls == 1


@pytest.mark.parametrize("poll_multiplier", [0.5, 2.0], ids=["faster", "slower"])
@pytest.mark.asyncio
async def test_reconcile_resets_due_time_when_poll_policy_changes(
    poll_multiplier: float,
) -> None:
    service = FixtureService()
    current_registry = FixtureRegistry("QQQM:USD")
    current_instrument = current_registry["QQQM:USD"]
    alpaca = StreamProvider("alpaca", "QQQM:USD")
    current = make_coordinator(
        service,
        current_registry,
        {"alpaca": alpaca},
        "old-generation",
    )
    await current.start()
    await alpaca.connected.wait()
    future = asyncio.get_running_loop().time() + 60
    current._quote_next_refresh_at = {"QQQM:USD": future}

    candidate_registry = FixtureRegistry("QQQM:USD")
    candidate_registry._items["QQQM:USD"] = replace(
        current_instrument,
        quote_poll_seconds=current_instrument.quote_poll_seconds * poll_multiplier,
    )
    candidate_alpaca = StreamProvider("alpaca", "QQQM:USD")
    candidate = make_coordinator(
        service,
        candidate_registry,
        {"alpaca": candidate_alpaca},
        "new-generation",
    )
    await candidate.prepare()

    token = await current.reconcile_generation(
        candidate,
        candidate_registry,
        "new-generation",
        ("alpaca",),
    )

    assert "QQQM:USD" not in current._quote_next_refresh_at
    await current.finalize_reconciliation(token)
    await candidate.stop(persist_checkpoints=False)
    await current.stop(persist_checkpoints=False)


@pytest.mark.asyncio
async def test_reconcile_stops_stream_collection_for_archived_symbol() -> None:
    service = FixtureService()
    current_registry = FixtureRegistry("QQQM:USD", "BTC:USDC")
    alpaca = StreamProvider("alpaca", "QQQM:USD")
    binance = StreamProvider("binance", "BTC:USDC")
    current = make_coordinator(
        service,
        current_registry,
        {"alpaca": alpaca, "binance": binance},
        "old-generation",
    )
    await current.start()
    await asyncio.gather(alpaca.connected.wait(), binance.connected.wait())

    candidate_registry = FixtureRegistry("QQQM:USD")
    candidate_alpaca = StreamProvider("alpaca", "QQQM:USD")
    candidate = make_coordinator(
        service,
        candidate_registry,
        {"alpaca": candidate_alpaca},
        "new-generation",
    )
    await candidate.prepare()

    token = await current.reconcile_generation(
        candidate,
        candidate_registry,
        "new-generation",
        ("alpaca",),
    )

    assert "BTC:USDC" not in current.registry
    assert "stream:binance" not in current._component_tasks
    assert binance.disconnects == 1
    assert current._component_tasks["stream:alpaca"] is token.retained_stream_tasks["alpaca"]

    await current.finalize_reconciliation(token)
    await candidate.stop(persist_checkpoints=False)
    await current.stop(persist_checkpoints=False)


@pytest.mark.asyncio
async def test_immediate_reconciled_task_failure_restores_old_graph_and_streams() -> None:
    service = FixtureService()
    current_registry = FixtureRegistry("QQQM:USD", "BTC:USDC")
    alpaca = StreamProvider("alpaca", "QQQM:USD")
    binance = StreamProvider("binance", "BTC:USDC")
    current = make_coordinator(
        service,
        current_registry,
        {"alpaca": alpaca, "binance": binance},
        "old-generation",
    )
    await current.start()
    await asyncio.gather(alpaca.connected.wait(), binance.connected.wait())
    old_graph = current.graph
    retained_task = current._component_tasks["stream:alpaca"]

    candidate_registry = FixtureRegistry("QQQM:USD", "BTC:USDC", "ETH:USDC")
    candidate_alpaca = StreamProvider("alpaca", "QQQM:USD")
    candidate_binance = StreamProvider("binance", "BTC:USDC", "ETH:USDC")
    candidate = make_coordinator(
        service,
        candidate_registry,
        {"alpaca": candidate_alpaca, "binance": candidate_binance},
        "new-generation",
    )
    await candidate.prepare()

    scheduler_calls = 0

    async def fail_quote_scheduler_once() -> None:
        nonlocal scheduler_calls
        scheduler_calls += 1
        if scheduler_calls == 1:
            raise RuntimeError("injected reconciled scheduler failure")
        await current._stop.wait()

    current._quote_scheduler_loop = fail_quote_scheduler_once
    with pytest.raises(RuntimeError, match="failed during graph reconciliation"):
        await current.reconcile_generation(
            candidate,
            candidate_registry,
            "new-generation",
            ("alpaca",),
        )

    assert current.graph is old_graph
    assert current.is_running is True
    assert current.registry is current_registry
    assert current.generation_id == "old-generation"
    assert current._component_tasks["stream:alpaca"] is retained_task
    assert not retained_task.done()
    assert candidate._owns_graph is True
    assert candidate.graph.providers["alpaca"] is candidate_alpaca
    assert scheduler_calls == 2
    await candidate.stop(persist_checkpoints=False)
    await current.stop(persist_checkpoints=False)


@pytest.mark.asyncio
async def test_service_transition_failure_rolls_back_live_reconciliation(
    tmp_path, monkeypatch
) -> None:
    settings = Settings(
        production=False,
        background_enabled=True,
        managed_instruments_path=tmp_path / "instruments.json",
        database_path=tmp_path / "quickprice.sqlite3",
    )
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    active = store.active_generation()
    btc = active.by_symbol()["BTC:USDC"]
    staged_snapshot = store.update_instrument(
        btc.id,
        {"quote_poll_seconds": btc.quote_poll_seconds + 1},
        store.snapshot()["revision"],
    )
    target = store.staged_generation()
    assert target is not None
    before_document = store.catalog_snapshot()
    before_runtime = service.capture_generation()

    alpaca = StreamProvider("alpaca", "QQQM:USD")
    binance = StreamProvider("binance", "BTC:USDC")
    current = make_coordinator(
        service,
        active.to_registry(),
        {"alpaca": alpaca, "binance": binance},
        before_runtime.generation_id,
    )
    service._coordinator = current
    await current.start()
    await asyncio.gather(alpaca.connected.wait(), binance.connected.wait())
    retained_task = current._component_tasks["stream:alpaca"]
    old_graph = current.graph

    candidate_alpaca = StreamProvider("alpaca", "QQQM:USD")
    candidate_binance = StreamProvider("binance", "BTC:USDC", "ETH:USDC")
    candidate = make_coordinator(
        service,
        target.to_registry(),
        {"alpaca": candidate_alpaca, "binance": candidate_binance},
        "candidate-generation",
    )
    await candidate.prepare()

    def fail_transition_commit(*_args, **_kwargs):
        raise RuntimeError("injected transition commit failure")

    monkeypatch.setattr(store, "commit_transition", fail_transition_commit)
    with pytest.raises(RuntimeError, match="transition commit failure"):
        await service._commit_catalog_activation(
            operation="activate",
            expected_file_revision=staged_snapshot["revision"],
            target=target,
            registry=target.to_registry(),
            route_plan=before_runtime.route_plan,
            generation_id="candidate-generation",
            warmed=(),
            candidate_coordinator=candidate,
            reconcile_provider_names=("alpaca",),
        )

    for _ in range(10):
        if len(binance.subscriptions) >= 2:
            break
        await asyncio.sleep(0)
    assert service.capture_generation().generation_id == before_runtime.generation_id
    assert service._coordinator is current
    assert current.graph is old_graph
    assert current.is_running is True
    assert current.generation_id == before_runtime.generation_id
    assert current._component_tasks["stream:alpaca"] is retained_task
    assert not retained_task.done()
    assert binance.subscriptions == [("BTC:USDC",), ("BTC:USDC",)]
    assert candidate._closed is True
    assert candidate_alpaca.close_calls == 1
    assert candidate_binance.close_calls == 1
    restored = store.catalog_snapshot()
    assert restored["revision"] == before_document["revision"]
    assert restored["active"] == before_document["active"]
    assert restored["staged"] == before_document["staged"]
    assert restored["last_known_good"] == before_document["last_known_good"]

    await current.stop(persist_checkpoints=False)


@pytest.mark.asyncio
async def test_delayed_reconciled_task_failure_prevents_transition_commit(
    tmp_path, monkeypatch
) -> None:
    settings = Settings(
        production=False,
        background_enabled=True,
        managed_instruments_path=tmp_path / "instruments.json",
        database_path=tmp_path / "quickprice.sqlite3",
    )
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    active = store.active_generation()
    btc = active.by_symbol()["BTC:USDC"]
    staged_snapshot = store.update_instrument(
        btc.id,
        {"quote_poll_seconds": btc.quote_poll_seconds + 1},
        store.snapshot()["revision"],
    )
    target = store.staged_generation()
    assert target is not None
    before_document = store.catalog_snapshot()
    before_runtime = service.capture_generation()

    alpaca = StreamProvider("alpaca", "QQQM:USD")
    binance = StreamProvider("binance", "BTC:USDC")
    current = make_coordinator(
        service,
        active.to_registry(),
        {"alpaca": alpaca, "binance": binance},
        before_runtime.generation_id,
    )
    service._coordinator = current
    await current.start()
    await asyncio.gather(alpaca.connected.wait(), binance.connected.wait())
    old_graph = current.graph
    retained_task = current._component_tasks["stream:alpaca"]

    candidate_alpaca = StreamProvider("alpaca", "QQQM:USD")
    candidate_binance = StreamProvider("binance", "BTC:USDC", "ETH:USDC")
    candidate = make_coordinator(
        service,
        target.to_registry(),
        {"alpaca": candidate_alpaca, "binance": candidate_binance},
        "candidate-generation",
    )
    await candidate.prepare()

    fail_after_pointer = asyncio.Event()
    scheduler_calls = 0

    async def fail_once_after_pointer() -> None:
        nonlocal scheduler_calls
        scheduler_calls += 1
        if scheduler_calls == 1:
            await fail_after_pointer.wait()
            raise RuntimeError("injected delayed scheduler failure")
        await current._stop.wait()

    current._quote_scheduler_loop = fail_once_after_pointer
    original_activate_runtime = service._activate_runtime_generation

    def activate_and_release_failure(*args, **kwargs):
        result = original_activate_runtime(*args, **kwargs)
        fail_after_pointer.set()
        return result

    commit_calls = 0
    original_commit = store.commit_transition

    def track_commit(*args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(service, "_activate_runtime_generation", activate_and_release_failure)
    monkeypatch.setattr(store, "commit_transition", track_commit)
    with pytest.raises(RuntimeError, match="failed during graph reconciliation"):
        await service._commit_catalog_activation(
            operation="activate",
            expected_file_revision=staged_snapshot["revision"],
            target=target,
            registry=target.to_registry(),
            route_plan=before_runtime.route_plan,
            generation_id="candidate-generation",
            warmed=(),
            candidate_coordinator=candidate,
            reconcile_provider_names=("alpaca",),
        )

    assert commit_calls == 0
    assert scheduler_calls == 2
    assert service.capture_generation().generation_id == before_runtime.generation_id
    assert service._coordinator is current
    assert current.graph is old_graph
    assert current.is_running is True
    assert current._component_tasks["stream:alpaca"] is retained_task
    assert not retained_task.done()
    assert candidate._closed is True
    assert candidate_alpaca.close_calls == 1
    assert candidate_binance.close_calls == 1
    restored = store.catalog_snapshot()
    assert restored["revision"] == before_document["revision"]
    assert restored["active"] == before_document["active"]
    assert restored["staged"] == before_document["staged"]
    assert restored["last_known_good"] == before_document["last_known_good"]

    await current.stop(persist_checkpoints=False)


@pytest.mark.asyncio
async def test_catalog_runtime_reconciles_only_changed_provider_streams(
    tmp_path, monkeypatch
) -> None:
    settings = Settings(
        production=False,
        background_enabled=True,
        managed_instruments_path=tmp_path / "instruments.json",
        database_path=tmp_path / "quickprice.sqlite3",
    )
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    runtime = service._catalog_runtime
    active = store.active_generation()
    active_registry, compiled_active_graph, active_plan = runtime._compile(
        active,
        strict=False,
    )
    await compiled_active_graph.close()
    initial_runtime = service.capture_generation()
    service._activate_runtime_generation(
        active_registry,
        revision=active.revision,
        catalog=active,
        route_plan=active_plan,
        generation_id=initial_runtime.generation_id,
    )

    kraken = StreamProvider("kraken", "XMR:USDC")
    binance = StreamProvider("binance", "BTC:USDC")
    current_graph = selected_graph(
        active_registry,
        {"kraken": kraken, "binance": binance},
    )
    current = make_coordinator(
        service,
        active_registry,
        {"kraken": kraken, "binance": binance},
        initial_runtime.generation_id,
        graph=current_graph,
    )
    service._coordinator = current
    await current.start()
    await asyncio.gather(kraken.connected.wait(), binance.connected.wait())
    retained_task = current._component_tasks["stream:kraken"]
    replaced_task = current._component_tasks["stream:binance"]

    created = store.create_instrument(doge_definition(), store.snapshot()["revision"])
    target = store.staged_generation()
    assert target is not None
    target_registry, compiled_target_graph, target_plan = runtime._compile(
        target,
        strict=False,
    )
    await compiled_target_graph.close()
    definition = target.by_symbol()["DOGE:USDC"]

    candidate_kraken = StreamProvider("kraken", "XMR:USDC")
    candidate_binance = StreamProvider("binance", "BTC:USDC", "DOGE:USDC")
    candidate_graph = selected_graph(
        target_registry,
        {"kraken": candidate_kraken, "binance": candidate_binance},
    )

    def compile_fixture(generation, *, strict):
        del strict
        if generation.revision == target.revision:
            return target_registry, candidate_graph, target_plan
        disposable_kraken = StreamProvider("kraken", "XMR:USDC")
        disposable_binance = StreamProvider("binance", "BTC:USDC")
        return (
            active_registry,
            selected_graph(
                active_registry,
                {"kraken": disposable_kraken, "binance": disposable_binance},
            ),
            active_plan,
        )

    async def warm(_graph, _generation, symbols):
        assert symbols == ("DOGE:USDC",)
        return (
            WarmedInstrument(
                definition,
                ProviderQuote(
                    "DOGE:USDC",
                    Decimal("0.25"),
                    datetime.now(UTC),
                    "binance",
                    "book_ticker",
                ),
            ),
        )

    monkeypatch.setattr(runtime, "_compile", compile_fixture)
    monkeypatch.setattr(runtime, "_warm", warm)
    queued = await service.activate_instrument_catalog(created["revision"])
    await runtime._active_task

    job = service.instrument_catalog_job(queued["job_id"])
    assert job["state"] == "succeeded"
    assert job["collector_handoff"] == "reconciled"
    assert service._coordinator is current
    assert current._component_tasks["stream:kraken"] is retained_task
    assert current.graph.providers["kraken"] is kraken
    assert kraken.subscriptions == [("XMR:USDC",)]
    assert candidate_kraken.subscriptions == []
    assert candidate_kraken.close_calls == 1
    assert current._component_tasks["stream:binance"] is not replaced_task
    assert replaced_task.done()
    assert binance.disconnects == 1
    assert binance.close_calls == 1
    assert candidate_binance.subscriptions == [("BTC:USDC", "DOGE:USDC")]
    assert candidate_binance.close_calls == 0

    await current.stop(persist_checkpoints=False)
    assert kraken.close_calls == 1
    assert candidate_binance.close_calls == 1
