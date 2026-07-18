from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quickprice.catalog_runtime import (
    CatalogJobState,
    CatalogRuntimeError,
    InstrumentCatalogRuntime,
    WarmedInstrument,
)
from quickprice.collectors import MarketDataCoordinator
from quickprice.domain import PricePoint, ProviderQuote
from quickprice.managed_config import InstrumentPolicyStore, RevisionConflictError
from quickprice.providers.compiler import RouteCompileError
from quickprice.providers.quota import QuotaBudget
from quickprice.registry import build_registry
from quickprice.service import QuickPriceService
from quickprice.storage import ProviderCheckpointRecord
from tests.helpers import seed_complete


def _doge_definition() -> dict[str, object]:
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


def test_catalog_transition_restore_requires_current_file_revision(tmp_path) -> None:
    path = tmp_path / "instruments.json"
    store = InstrumentPolicyStore(path, build_registry())
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    assert created["restart_required"] is False
    token = store.capture_transition(created["revision"])

    with pytest.raises(RevisionConflictError):
        store.restore_transition(token, "0" * 64)
    with pytest.raises(RevisionConflictError, match="activation is in progress"):
        store.update_instrument(
            store.staged_generation().by_symbol()["DOGE:USDC"].id,
            {"description": "A concurrent draft mutation."},
            created["revision"],
        )

    assert store.catalog_snapshot()["revision"] == created["revision"]
    store.abort_transition(token)


@pytest.mark.asyncio
async def test_validate_requires_a_staged_catalog(settings, tmp_path) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)

    with pytest.raises(CatalogRuntimeError, match="no staged catalog"):
        await service.validate_instrument_catalog(store.snapshot()["revision"])


@pytest.mark.asyncio
async def test_validate_reports_safe_diff_credit_plan_and_binding_verification(
    settings, tmp_path, monkeypatch
) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])

    async def verify(_plan, symbols):
        assert symbols == ("DOGE:USDC",)
        return {
            "verified": True,
            "binding_count": 1,
            "binding_set_sha256": "0" * 64,
            "providers": {"binance": {"bindings": 1, "requests": 1}},
            "warnings": [],
        }

    monkeypatch.setattr(service._catalog_runtime, "_verify_bindings", verify)

    result = await service.validate_instrument_catalog(created["revision"])

    assert result["diff"] == {
        "added": ["DOGE:USDC"],
        "changed": [],
        "archived_or_disabled": [],
        "counts": {
            "added": 1,
            "changed": 0,
            "archived_or_disabled": 0,
            "total": 1,
        },
    }
    assert result["credit_plan"]["within_budget"] is True
    assert result["credit_plan"]["within_hard_budget"] is True
    assert result["credit_plan"]["fallback_requests_hard_gated"] is True
    assert result["credit_plan"]["admission_basis"] == "committed_primary_demand"
    assert result["binding_verification"] == {
        "verified": True,
        "binding_count": 1,
        "binding_set_sha256": "0" * 64,
        "providers": {"binance": {"bindings": 1, "requests": 1}},
        "warnings": [],
    }
    assert set(result["credit_plan"]) == {
        "estimated_daily_credits",
        "active_daily_credits",
        "limits",
        "grandfathered_unchanged_daily_credits",
        "effective_admission_limits",
        "grandfathered_unchanged_daily_credits_by_scope",
        "effective_admission_limits_by_scope",
        "worst_case_daily_credits",
        "hard_capped_daily_credits",
        "admission_basis",
        "fallback_requests_hard_gated",
        "worst_case_within_configured_budget",
        "within_hard_budget",
        "within_budget",
    }


def test_credit_admission_grandfathers_only_unchanged_committed_demand(settings, tmp_path) -> None:
    settings = replace(
        settings,
        managed_instruments_path=tmp_path / "instruments.json",
        twelve_daily_credits=10,
        twelve_fx_reserve_credits=8,
    )
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    runtime = service._catalog_runtime

    def line(
        symbol: str,
        value: str,
        *,
        provider: str = "alpha_vantage",
        scopes: tuple[str, ...] = ("general",),
    ):
        return SimpleNamespace(
            provider=provider,
            capability=SimpleNamespace(value="quote"),
            symbol=symbol,
            bases=("direct_route",),
            committed=True,
            estimated_credits_per_day=Decimal(value),
            quota_scopes=scopes,
        )

    def plan(*lines):
        totals: dict[str, Decimal] = {}
        by_scope: dict[str, dict[str, Decimal]] = {}
        for item in lines:
            totals[item.provider] = totals.get(item.provider, Decimal(0)) + (
                item.estimated_credits_per_day
            )
            provider_scopes = by_scope.setdefault(item.provider, {})
            for scope in item.quota_scopes:
                provider_scopes[scope] = provider_scopes.get(scope, Decimal(0)) + (
                    item.estimated_credits_per_day
                )
        return SimpleNamespace(
            credit_estimates=lines,
            committed_daily_credits=totals,
            committed_daily_credits_by_scope=by_scope,
        )

    active = plan(line("LEGACY:USD", "30"))
    assert runtime._incremental_budget_errors(active, active) == ()

    added = runtime._incremental_budget_errors(
        active,
        plan(line("LEGACY:USD", "30"), line("NEW:USD", "1")),
    )
    assert len(added) == 1
    assert added[0].limit == 25.0
    assert added[0].effective_limit == 30.0
    assert added[0].grandfathered_unchanged == 30.0

    replaced = runtime._incremental_budget_errors(
        active,
        plan(line("REPLACEMENT:USD", "30")),
    )
    assert len(replaced) == 1
    assert replaced[0].effective_limit == 25.0
    assert replaced[0].grandfathered_unchanged == 0.0

    assert (
        runtime._incremental_budget_errors(
            active,
            plan(line("LEGACY:USD", "20"), line("NEW:USD", "5")),
        )
        == ()
    )

    twelve_active = plan(line("USD:JPY", "3", provider="twelve_data"))
    assert runtime._incremental_budget_errors(twelve_active, twelve_active) == ()
    twelve_added = runtime._incremental_budget_errors(
        twelve_active,
        plan(
            line("USD:JPY", "3", provider="twelve_data"),
            line("AAPL:USD", "1", provider="twelve_data"),
        ),
    )
    assert len(twelve_added) == 1
    assert twelve_added[0].scope == "general"
    assert twelve_added[0].limit == 2.0
    assert twelve_added[0].effective_limit == 3.0
    assert twelve_added[0].grandfathered_unchanged == 3.0


@pytest.mark.asyncio
async def test_stale_activation_request_performs_no_warm_or_compile(
    settings, tmp_path, monkeypatch
) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    first = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    definition = store.staged_generation().by_symbol()["DOGE:USDC"]
    store.update_instrument(
        definition.id,
        {"description": "A newer concurrent draft."},
        first["revision"],
    )
    compile_calls = 0
    warm_calls = 0

    def compile_fixture(*args, **kwargs):
        nonlocal compile_calls
        compile_calls += 1
        raise AssertionError("stale activation must not compile")

    async def warm_fixture(*args, **kwargs):
        nonlocal warm_calls
        warm_calls += 1
        raise AssertionError("stale activation must not warm")

    monkeypatch.setattr(service._catalog_runtime, "_compile", compile_fixture)
    monkeypatch.setattr(service._catalog_runtime, "_warm", warm_fixture)
    with pytest.raises(RevisionConflictError):
        await service.activate_instrument_catalog(first["revision"])

    assert compile_calls == 0
    assert warm_calls == 0
    assert service._catalog_runtime._active_task is None


@pytest.mark.asyncio
async def test_queued_activation_rechecks_revision_before_any_provider_work(
    settings, tmp_path, monkeypatch
) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    definition = store.staged_generation().by_symbol()["DOGE:USDC"]
    runtime = service._catalog_runtime
    compile_calls = 0
    prepare_calls = 0
    verification_calls = 0
    warm_calls = 0

    def compile_fixture(*args, **kwargs):
        nonlocal compile_calls
        compile_calls += 1
        raise AssertionError("stale queued activation must not compile")

    async def prepare_fixture(*args, **kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        raise AssertionError("stale queued activation must not prepare providers")

    async def verification_fixture(*args, **kwargs):
        nonlocal verification_calls
        verification_calls += 1
        raise AssertionError("stale queued activation must not verify providers")

    async def warm_fixture(*args, **kwargs):
        nonlocal warm_calls
        warm_calls += 1
        raise AssertionError("stale queued activation must not warm")

    monkeypatch.setattr(runtime, "_compile", compile_fixture)
    monkeypatch.setattr(MarketDataCoordinator, "prepare", prepare_fixture)
    monkeypatch.setattr(runtime, "_verify_bindings", verification_fixture)
    monkeypatch.setattr(runtime, "_warm", warm_fixture)
    queued = await service.activate_instrument_catalog(created["revision"])
    # The job task is queued but cannot run until this coroutine yields.
    store.update_instrument(
        definition.id,
        {"description": "A replacement draft queued before job execution."},
        created["revision"],
    )
    await runtime._active_task

    job = service.instrument_catalog_job(queued["job_id"])
    assert job["state"] == "failed"
    assert job["error"]["type"] == "RevisionConflictError"
    assert job["error"]["stage"] == "validating"
    assert (compile_calls, prepare_calls, verification_calls, warm_calls) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_successful_activation_warms_then_atomically_switches_generation(
    settings, tmp_path
) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    before = service.capture_generation()
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["DOGE:USDC"]
    runtime = service._catalog_runtime

    async def warm(_graph, _generation, symbols):
        assert symbols == ("DOGE:USDC",)
        return (
            WarmedInstrument(
                definition=definition,
                quote=ProviderQuote(
                    "DOGE:USDC",
                    Decimal("0.25"),
                    datetime.now(UTC),
                    "binance",
                    "book_ticker",
                ),
            ),
        )

    runtime._warm = warm
    queued = await service.activate_instrument_catalog(created["revision"])
    await runtime._active_task

    completed = service.instrument_catalog_job(queued["job_id"])
    after = service.capture_generation()
    assert completed["state"] == "succeeded"
    assert after.generation_id != before.generation_id
    assert after.revision == staged.revision
    assert "DOGE:USDC" in after.registry
    assert store.staged_generation() is None
    assert store.active_generation().revision == staged.revision
    assert service.get_quote("DOGE:USDC").price == Decimal("0.25")
    assert (
        service.publish_quote(
            ProviderQuote(
                "BTC:USDC",
                Decimal("1"),
                datetime.now(UTC),
                "late_old_provider",
                "fixture",
            ),
            persist=False,
            generation_id=before.generation_id,
        )
        is False
    )


@pytest.mark.asyncio
async def test_newly_active_symbol_restores_retained_history_before_publication(
    settings, tmp_path
) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    previous = service.capture_generation()
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["DOGE:USDC"]
    now = datetime.now(UTC)
    retained = PricePoint(
        "DOGE:USDC",
        now - timedelta(days=366),
        Decimal("0.10"),
        "retained_fixture",
        False,
        "1d",
    )

    class Storage:
        def __init__(self):
            self.restore_calls: list[tuple[str, ...]] = []

        async def restore(self, *, symbols):
            self.restore_calls.append(tuple(symbols))
            return SimpleNamespace(
                price_points=(retained,),
                dividends=(),
                yield_metrics=(),
                yield_metric_records=(),
                quotes=(),
            )

    storage = Storage()
    service._storage = storage

    async def warm(_graph, _generation, _symbols):
        return (
            WarmedInstrument(
                definition,
                ProviderQuote(
                    "DOGE:USDC",
                    Decimal("0.30"),
                    now,
                    "binance",
                    "book_ticker",
                ),
            ),
        )

    service._catalog_runtime._warm = warm
    queued = await service.activate_instrument_catalog(created["revision"])
    await service._catalog_runtime._active_task

    assert previous.registry.resolve("DOGE:USDC") is None
    with pytest.raises(KeyError):
        service.get_quote("DOGE:USDC", generation=previous)
    assert service.instrument_catalog_job(queued["job_id"])["state"] == "succeeded"
    assert storage.restore_calls == [("DOGE:USDC",)]
    quote = service.get_quote("DOGE:USDC")
    assert quote.changes["1y"] is not None
    assert quote.changes["1y"].reference_price == 0.10
    assert quote.changes["1y"].percent == 200.0


@pytest.mark.asyncio
async def test_failed_shadow_warm_keeps_active_generation_and_staged_revision(
    settings, tmp_path
) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    before = service.capture_generation()
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    staged = store.staged_generation()
    assert staged is not None
    runtime = service._catalog_runtime

    async def fail_warm(_graph, _generation, _symbols):
        raise TimeoutError("fixture upstream timeout")

    runtime._warm = fail_warm
    queued = await service.activate_instrument_catalog(created["revision"])
    await runtime._active_task

    failed = service.instrument_catalog_job(queued["job_id"])
    after = service.capture_generation()
    assert failed["state"] == "failed"
    assert failed["error"] == {
        "code": "catalog_warm_timeout",
        "message": "catalog validation, warm-up, or activation failed",
        "type": "TimeoutError",
        "stage": "warming",
    }
    assert after.generation_id == before.generation_id
    assert store.staged_generation().revision == staged.revision
    assert "DOGE:USDC" not in after.registry


@pytest.mark.asyncio
async def test_transition_release_failure_restores_exact_catalog_and_runtime(
    settings, tmp_path, monkeypatch
) -> None:
    settings = replace(
        settings,
        background_enabled=True,
        managed_instruments_path=tmp_path / "instruments.json",
    )
    service = QuickPriceService(settings)
    seed_complete(service)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    before_generation = service.capture_generation()
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    before_document = store.catalog_snapshot()
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["DOGE:USDC"]
    runtime = service._catalog_runtime

    async def warm(_graph, _generation, _symbols):
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

    async def idle_run(self):
        self._started.set()
        await self._stop.wait()

    stopped: list[MarketDataCoordinator] = []
    original_stop = MarketDataCoordinator.stop

    async def tracked_stop(self, *, persist_checkpoints=True):
        stopped.append(self)
        await original_stop(self, persist_checkpoints=persist_checkpoints)

    concurrent_write_rejected = False

    def fail_transition_release(_token, _expected_revision):
        nonlocal concurrent_write_rejected
        with pytest.raises(RevisionConflictError, match="activation is in progress"):
            store.update_instrument(
                definition.id,
                {"description": "A racy replacement draft."},
                store.catalog_snapshot()["revision"],
            )
        concurrent_write_rejected = True
        raise RuntimeError("injected post-pointer failure")

    runtime._warm = warm
    monkeypatch.setattr(MarketDataCoordinator, "stop", tracked_stop)
    monkeypatch.setattr(MarketDataCoordinator, "_run", idle_run)
    monkeypatch.setattr(store, "commit_transition", fail_transition_release)
    queued = await service.activate_instrument_catalog(created["revision"])
    await runtime._active_task

    assert service.instrument_catalog_job(queued["job_id"])["state"] == "failed"
    assert service.capture_generation().generation_id == before_generation.generation_id
    restored = store.catalog_snapshot()
    assert restored["revision"] == before_document["revision"]
    assert restored["active"] == before_document["active"]
    assert restored["staged"] == before_document["staged"]
    assert restored["last_known_good"] == before_document["last_known_good"]
    assert service._coordinator is None
    assert concurrent_write_rejected is True
    assert stopped and all(item._closed for item in stopped)


@pytest.mark.asyncio
async def test_transition_commit_failure_keeps_live_previous_collector(
    settings, tmp_path, monkeypatch
) -> None:
    settings = replace(
        settings,
        background_enabled=True,
        managed_instruments_path=tmp_path / "instruments.json",
    )
    service = QuickPriceService(settings)
    seed_complete(service)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    before_generation = service.capture_generation()
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    before_document = store.catalog_snapshot()
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["DOGE:USDC"]

    class PreviousCoordinator:
        is_running = True
        fatal_error = None

        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop(self, *, persist_checkpoints=True):
            self.stop_calls += 1
            self.is_running = False

    previous = PreviousCoordinator()
    service._coordinator = previous
    runtime = service._catalog_runtime

    async def warm(_graph, _generation, _symbols):
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

    async def idle_run(self):
        self._started.set()
        await self._stop.wait()

    def fail_transition_commit(*_args, **_kwargs):
        raise RuntimeError("injected transition commit failure")

    runtime._warm = warm
    monkeypatch.setattr(MarketDataCoordinator, "_run", idle_run)
    monkeypatch.setattr(store, "commit_transition", fail_transition_commit)

    queued = await service.activate_instrument_catalog(created["revision"])
    await runtime._active_task

    assert service.instrument_catalog_job(queued["job_id"])["state"] == "failed"
    assert service.capture_generation().generation_id == before_generation.generation_id
    assert service._coordinator is previous
    assert previous.is_running is True
    assert previous.stop_calls == 0
    restored = store.catalog_snapshot()
    assert restored["revision"] == before_document["revision"]
    assert restored["active"] == before_document["active"]
    assert restored["staged"] == before_document["staged"]
    assert restored["last_known_good"] == before_document["last_known_good"]


@pytest.mark.asyncio
async def test_catalog_mutation_is_rejected_while_activation_lease_is_held(
    settings, tmp_path, monkeypatch
) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["DOGE:USDC"]
    runtime = service._catalog_runtime

    async def warm(_graph, _generation, _symbols):
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

    entered = threading.Event()
    release = threading.Event()
    original_activate = store.activate_staged

    def blocked_activate(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("activation fixture was not released")
        return original_activate(*args, **kwargs)

    runtime._warm = warm
    monkeypatch.setattr(store, "activate_staged", blocked_activate)
    queued = await service.activate_instrument_catalog(created["revision"])
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        with pytest.raises(RevisionConflictError, match="activation is in progress"):
            await asyncio.to_thread(
                store.update_instrument,
                definition.id,
                {"description": "A concurrent activation write."},
                created["revision"],
            )
    finally:
        release.set()
    await runtime._active_task

    assert service.instrument_catalog_job(queued["job_id"])["state"] == "succeeded"


@pytest.mark.asyncio
async def test_candidate_startup_failure_keeps_previous_collector_and_rolls_back(
    settings, tmp_path, monkeypatch
) -> None:
    settings = replace(
        settings,
        background_enabled=True,
        managed_instruments_path=tmp_path / "instruments.json",
    )
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    before_generation = service.capture_generation()
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    before_document = store.catalog_snapshot()
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["DOGE:USDC"]

    class PreviousCoordinator:
        is_running = True
        fatal_error = None

        def __init__(self):
            self.stop_calls = 0

        async def stop(self, *, persist_checkpoints=True):
            self.stop_calls += 1
            self.is_running = False

    previous = PreviousCoordinator()
    service._coordinator = previous
    runtime = service._catalog_runtime

    async def warm(_graph, _generation, _symbols):
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

    async def fail_startup(_self):
        assert service.capture_generation().generation_id == before_generation.generation_id
        assert service._coordinator is previous
        raise RuntimeError("injected collector startup failure")

    stopped: list[MarketDataCoordinator] = []
    original_stop = MarketDataCoordinator.stop

    async def tracked_stop(self, *, persist_checkpoints=True):
        stopped.append(self)
        await original_stop(self, persist_checkpoints=persist_checkpoints)

    runtime._warm = warm
    activation_writes = 0
    original_activate = store.activate_staged

    def track_activate(*args, **kwargs):
        nonlocal activation_writes
        activation_writes += 1
        return original_activate(*args, **kwargs)

    monkeypatch.setattr(store, "activate_staged", track_activate)
    monkeypatch.setattr(MarketDataCoordinator, "_startup_preseed_fx_daily", fail_startup)
    monkeypatch.setattr(MarketDataCoordinator, "stop", tracked_stop)
    queued = await service.activate_instrument_catalog(created["revision"])
    await runtime._active_task

    job = service.instrument_catalog_job(queued["job_id"])
    assert job["state"] == "failed"
    assert job["error"]["stage"] == "activating"
    assert service.capture_generation().generation_id == before_generation.generation_id
    assert service._coordinator is previous
    assert previous.is_running is True
    assert previous.stop_calls == 0
    restored = store.catalog_snapshot()
    assert restored["revision"] == before_document["revision"]
    assert restored["active"] == before_document["active"]
    assert restored["staged"] == before_document["staged"]
    assert restored["last_known_good"] == before_document["last_known_good"]
    assert restored["staged_revision"] == staged.revision
    assert activation_writes == 0
    assert stopped and all(item._closed for item in stopped)


@pytest.mark.asyncio
async def test_candidate_startup_is_acknowledged_before_generation_publication(
    settings, tmp_path, monkeypatch
) -> None:
    settings = replace(
        settings,
        background_enabled=True,
        managed_instruments_path=tmp_path / "instruments.json",
    )
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    previous_generation = service.capture_generation()
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["DOGE:USDC"]

    class PreviousCoordinator:
        is_running = True
        fatal_error = None

        def __init__(self):
            self.stop_calls = 0

        async def stop(self, *, persist_checkpoints=True):
            self.stop_calls += 1
            self.is_running = False

    previous = PreviousCoordinator()
    service._coordinator = previous
    runtime = service._catalog_runtime
    entered = asyncio.Event()
    release = asyncio.Event()

    async def warm(_graph, _generation, _symbols):
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

    async def controlled_run(self):
        entered.set()
        await release.wait()
        self._started.set()
        await self._stop.wait()

    runtime._warm = warm
    monkeypatch.setattr(MarketDataCoordinator, "_run", controlled_run)
    queued = await service.activate_instrument_catalog(created["revision"])
    await entered.wait()
    assert service.capture_generation().generation_id == previous_generation.generation_id
    assert service._coordinator is previous
    assert previous.stop_calls == 0

    release.set()
    await runtime._active_task

    job = service.instrument_catalog_job(queued["job_id"])
    assert job["state"] == "succeeded"
    assert service.capture_generation().generation_id != previous_generation.generation_id
    assert service._coordinator is not previous
    assert previous.stop_calls == 1
    await service._coordinator.stop(persist_checkpoints=False)


@pytest.mark.asyncio
async def test_retired_collector_cleanup_failure_does_not_rollback_committed_catalog(
    settings, tmp_path, monkeypatch
) -> None:
    settings = replace(
        settings,
        background_enabled=True,
        managed_instruments_path=tmp_path / "instruments.json",
    )
    service = QuickPriceService(settings)
    seed_complete(service)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    before_generation = service.capture_generation()
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["DOGE:USDC"]

    class StubbornCoordinator:
        is_running = True

        def __init__(self):
            self.stop_calls = 0

        async def stop(self, *, persist_checkpoints=True):
            self.stop_calls += 1
            raise RuntimeError("injected live collector shutdown failure")

    previous = StubbornCoordinator()
    service._coordinator = previous
    runtime = service._catalog_runtime

    async def warm(_graph, _generation, _symbols):
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

    async def idle_run(self):
        self._started.set()
        await self._stop.wait()

    runtime._warm = warm
    monkeypatch.setattr(MarketDataCoordinator, "_run", idle_run)
    queued = await service.activate_instrument_catalog(created["revision"])
    await runtime._active_task

    assert service.instrument_catalog_job(queued["job_id"])["state"] == "succeeded"
    assert service.capture_generation().generation_id != before_generation.generation_id
    assert service._coordinator is not previous
    assert previous.is_running is True
    assert previous.stop_calls == 1
    assert store.active_generation().revision == staged.revision
    assert store.staged_generation() is None
    await service._coordinator.stop(persist_checkpoints=False)


@pytest.mark.asyncio
async def test_retired_collector_task_is_forcibly_joined() -> None:
    blocker = asyncio.Event()
    supervisor = asyncio.create_task(blocker.wait())

    class RetiredCoordinator:
        def __init__(self):
            self._supervisor = supervisor
            self.graph = SimpleNamespace(close=self.close_graph)
            self.graph_closed = False

        @property
        def is_running(self):
            return self._supervisor is not None and not self._supervisor.done()

        async def stop(self, *, persist_checkpoints=True):
            raise RuntimeError("injected cleanup failure")

        async def close_graph(self):
            self.graph_closed = True

    coordinator = RetiredCoordinator()
    await QuickPriceService._retire_coordinator(coordinator)

    assert supervisor.done()
    assert coordinator.is_running is False
    assert coordinator.graph_closed is True


@pytest.mark.asyncio
async def test_metadata_only_activation_reuses_running_coordinator(settings, tmp_path) -> None:
    settings = replace(
        settings,
        background_enabled=True,
        managed_instruments_path=tmp_path / "instruments.json",
    )
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    runtime = service._catalog_runtime
    active = store.active_generation()
    registry, graph, plan = runtime._compile(active, strict=False)
    await graph.close()
    initial = service.capture_generation()
    service._activate_runtime_generation(
        registry,
        revision=active.revision,
        catalog=active,
        route_plan=plan,
        generation_id=initial.generation_id,
    )

    class ReusableCoordinator:
        def __init__(self):
            self.registry = registry
            self.generation_id = initial.generation_id
            self.is_running = True
            self.fatal_error = None
            self.adoptions = 0
            self.stop_calls = 0

        def adopt_generation(self, replacement, generation_id):
            previous = self.registry, self.generation_id
            self.registry = replacement
            self.generation_id = generation_id
            self.adoptions += 1
            return previous

        async def stop(self, *, persist_checkpoints=True):
            self.stop_calls += 1
            self.is_running = False

    coordinator = ReusableCoordinator()
    service._coordinator = coordinator
    bitcoin = active.by_symbol()["BTC:USDC"]
    updated = store.update_instrument(
        bitcoin.id,
        {"stale_after_seconds": bitcoin.stale_after_seconds + 5},
        store.snapshot()["revision"],
    )
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["BTC:USDC"]

    async def warm(_graph, _generation, symbols):
        assert symbols == ("BTC:USDC",)
        return (
            WarmedInstrument(
                definition,
                ProviderQuote(
                    "BTC:USDC",
                    Decimal("120000"),
                    datetime.now(UTC),
                    "binance",
                    "book_ticker",
                ),
            ),
        )

    runtime._warm = warm
    queued = await service.activate_instrument_catalog(updated["revision"])
    await runtime._active_task

    job = service.instrument_catalog_job(queued["job_id"])
    current = service.capture_generation()
    assert job["state"] == "succeeded"
    assert job["collector_handoff"] == "reused_metadata_only"
    assert service._coordinator is coordinator
    assert coordinator.adoptions == 1
    assert coordinator.stop_calls == 0
    assert coordinator.generation_id == current.generation_id
    assert coordinator.registry is current.registry

    current_catalog = store.active_generation()
    disabled = store.update_instrument(
        current_catalog.by_symbol()["BTC:USDC"].id,
        {"enabled": False},
        store.snapshot()["revision"],
    )
    reconnect_target = store.staged_generation()
    assert reconnect_target is not None
    _, reconnect_graph, reconnect_plan = runtime._compile(reconnect_target, strict=False)
    try:
        assert not runtime._can_reuse_coordinator(
            current_catalog,
            reconnect_target,
            reconnect_plan,
        )
    finally:
        await reconnect_graph.close()
    assert disabled["staged_revision"] == reconnect_target.revision


@pytest.mark.asyncio
async def test_captured_generation_keeps_its_own_quote_state(settings, tmp_path) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    seed_complete(service)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    old_generation = service.capture_generation()
    old_price = service.get_quote("BTC:USDC", generation=old_generation).price
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    staged = store.staged_generation()
    assert staged is not None
    definition = staged.by_symbol()["DOGE:USDC"]
    runtime = service._catalog_runtime

    async def warm(_graph, _generation, _symbols):
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

    runtime._warm = warm
    await service.activate_instrument_catalog(created["revision"])
    await runtime._active_task
    assert service.publish_quote(
        ProviderQuote(
            "BTC:USDC",
            Decimal("130000"),
            datetime.now(UTC),
            "fixture",
            "fixture",
        ),
        persist=False,
    )
    assert service.get_quote("BTC:USDC").price == Decimal("130000")
    assert service.get_quote("BTC:USDC", generation=old_generation).price == old_price


@pytest.mark.asyncio
async def test_warm_failure_cancels_and_joins_sibling(settings, tmp_path) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    generation = store.staged_generation()
    assert generation is not None
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    class Router:
        calls = 0

        async def get_quote(self, _symbol):
            self.calls += 1
            if self.calls == 1:
                await sibling_started.wait()
                raise RuntimeError("primary fixture failed")
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sibling_cancelled.set()

    with pytest.raises(ExceptionGroup):
        await service._catalog_runtime._warm(
            SimpleNamespace(router=Router()),
            generation,
            ("DOGE:USDC", "DOGE:USDC"),
        )
    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_warm_has_one_total_deadline(settings, tmp_path) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    generation = store.staged_generation()
    assert generation is not None
    cancelled = asyncio.Event()

    class Router:
        async def get_quote(self, _symbol):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    service._catalog_runtime.warm_timeout_seconds = 0.01
    with pytest.raises(TimeoutError):
        await service._catalog_runtime._warm(
            SimpleNamespace(router=Router()),
            generation,
            ("DOGE:USDC",),
        )
    assert cancelled.is_set()


def test_max_scale_warm_plan_accounts_for_provider_rate_gate_without_waiting(
    settings,
    tmp_path,
) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    symbols = tuple(f"ASSET{index}:USD" for index in range(2_000))
    definitions = {symbol: SimpleNamespace(symbol=symbol, income=None) for symbol in symbols}
    provider = SimpleNamespace(name="twelve_data", routing_timeout_seconds=14.0)

    class Router:
        timeout_seconds = 8.0

        @staticmethod
        def providers_for(_symbol, _capability):
            return (provider,)

    plan = service._catalog_runtime._warm_execution_plan(
        SimpleNamespace(router=Router()),
        SimpleNamespace(by_symbol=lambda: definitions),
        symbols,
    )

    assert plan.symbol_count == 2_000
    assert plan.capability_operations == 2_000
    assert plan.concurrency == 32
    assert dict(plan.provider_attempts) == {"twelve_data": 2_000}
    assert dict(plan.provider_rate_floor_seconds)["twelve_data"] == 15_000
    assert plan.effective_timeout_seconds > plan.configured_timeout_seconds
    assert plan.effective_timeout_seconds >= 15_029


def test_warm_contract_rejects_wrong_types_symbols_and_stale_values(settings, tmp_path) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    service.bind_instrument_catalog(store)
    store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    definition = store.staged_generation().by_symbol()["DOGE:USDC"]
    runtime = service._catalog_runtime

    with pytest.raises(TypeError, match="domain value"):
        runtime._validate_quote(definition, SimpleNamespace(symbol="DOGE:USDC"))
    with pytest.raises(ValueError, match="wrong symbol"):
        runtime._validate_quote(
            definition,
            ProviderQuote(
                "BTC:USDC",
                Decimal("1"),
                datetime.now(UTC),
                "fixture",
                "fixture",
            ),
        )
    with pytest.raises(ValueError, match="too stale"):
        runtime._validate_quote(
            definition,
            ProviderQuote(
                "DOGE:USDC",
                Decimal("0.25"),
                datetime.now(UTC) - timedelta(seconds=30),
                "fixture",
                "fixture",
            ),
        )
    with pytest.raises(TypeError, match="invalid domain value"):
        runtime._validate_dividend("QQQM:USD", None)
    with pytest.raises(TypeError, match="invalid domain value"):
        runtime._validate_yield("WBETH:USDC", None)


def test_compile_job_error_has_safe_actionable_context() -> None:
    result = InstrumentCatalogRuntime._safe_error(
        RouteCompileError("provider symbol is missing: DOGE:USDC/binance"),
        stage="validating",
    )
    assert result["code"] == "provider_binding_invalid"
    assert result["symbol"] == "DOGE:USDC"
    assert result["provider"] == "binance"
    assert "DOGE:USDC" not in result["message"]


@pytest.mark.asyncio
async def test_service_stop_marks_activation_cancelled_and_audits(settings, tmp_path) -> None:
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    audit_actions: list[str] = []

    async def audit(action, _target, _details, _context):
        audit_actions.append(action)

    service.bind_instrument_catalog(store, audit_sink=audit)
    created = store.create_instrument(_doge_definition(), store.snapshot()["revision"])
    warming = asyncio.Event()

    async def block_warm(_graph, _generation, _symbols):
        warming.set()
        await asyncio.Event().wait()

    service._catalog_runtime._warm = block_warm
    queued = await service.activate_instrument_catalog(created["revision"])
    await warming.wait()
    await service.stop()

    job = service.instrument_catalog_job(queued["job_id"])
    assert job["state"] == CatalogJobState.CANCELLED.value
    assert job["error"]["code"] == "catalog_activation_cancelled"
    assert audit_actions == ["instrument_catalog.activate_cancelled"]


@pytest.mark.asyncio
async def test_shadow_coordinators_share_one_restored_quota_ledger(settings) -> None:
    service = QuickPriceService(settings)

    class Storage:
        def __init__(self):
            self.records = []

        async def enqueue_checkpoint_record(self, record, *, wait=False):
            self.records.append((record, wait))

    class Graph:
        def __init__(self, quota):
            self.router = SimpleNamespace()
            self.providers = {"twelve_data": SimpleNamespace(quota=quota)}
            self.closed = False

        async def close(self):
            self.closed = True

    seed = QuotaBudget(10, 86_400)
    assert await seed.acquire(3)
    stale_checkpoint = ProviderCheckpointRecord(
        "twelve_data",
        "quota",
        datetime.now(UTC),
        await seed.checkpoint(),
    )
    service._storage = Storage()
    service.remember_provider_checkpoint(stale_checkpoint)
    generation_id = service.capture_generation().generation_id
    first_graph = Graph(QuotaBudget(10, 86_400))
    first = MarketDataCoordinator(
        service,
        settings,
        generation_id=generation_id,
        graph=first_graph,
    )
    await first.prepare()
    first_quota = first_graph.providers["twelve_data"].quota
    assert (await first_quota.snapshot()).used == 3
    assert await first_quota.acquire()
    assert (await first_quota.snapshot()).used == 4

    # Simulate an older restore image; a shadow graph must adopt the live
    # ledger instead of restoring this checkpoint over credits already spent.
    service._provider_checkpoints[("twelve_data", "quota")] = stale_checkpoint
    second_graph = Graph(QuotaBudget(10, 86_400))
    second = MarketDataCoordinator(
        service,
        settings,
        generation_id="candidate-generation",
        graph=second_graph,
    )
    await second.prepare()
    assert second_graph.providers["twelve_data"].quota is first_quota
    assert (await first_quota.snapshot()).used == 4
    service._storage.records.clear()
    second._checkpoint_state[("twelve_data", "quotes")] = {
        "BTC:USDC": datetime.now(UTC).isoformat()
    }
    await second._persist_provider_checkpoints()
    assert service._storage.records == []

    await second.stop(persist_checkpoints=False)
    await first.stop(persist_checkpoints=False)


@pytest.mark.asyncio
async def test_history_scheduler_uses_per_instrument_poll_policy(settings, monkeypatch) -> None:
    import quickprice.collectors as collectors

    registry = build_registry()
    catalog = SimpleNamespace(
        definitions=(
            SimpleNamespace(
                symbol="BTC:USDC",
                history=SimpleNamespace(poll_seconds=10.0, backfill_days=None),
            ),
            SimpleNamespace(
                symbol="ETH:USDC",
                history=SimpleNamespace(poll_seconds=20.0, backfill_days=None),
            ),
        )
    )
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        settings,
        registry,
        catalog=catalog,
    )
    now = 0.0
    calls: list[tuple[float, set[str]]] = []

    async def backfill(*, include_fx):
        calls.append((now, set(coordinator._history_due_symbols or ())))
        if now >= 20:
            raise asyncio.CancelledError
        return True

    async def advance(delay):
        nonlocal now
        now += delay

    coordinator._backfill_history = backfill
    monkeypatch.setattr(collectors.time, "monotonic", lambda: now)
    monkeypatch.setattr(collectors.asyncio, "sleep", advance)
    try:
        with pytest.raises(asyncio.CancelledError):
            await coordinator._history_loop()
    finally:
        await coordinator.graph.close()

    assert "BTC:USDC" in calls[0][1] and "ETH:USDC" in calls[0][1]
    assert calls[1] == (10.0, {"BTC:USDC"})
    assert calls[2] == (20.0, {"BTC:USDC", "ETH:USDC"})


@pytest.mark.asyncio
async def test_history_backfill_policy_caps_intraday_but_preserves_one_year(
    settings, tmp_path
) -> None:
    payload = _doge_definition()
    payload["history"] = {
        "enabled": True,
        "poll_seconds": 17,
        "backfill_days": 1,
    }
    settings = replace(settings, managed_instruments_path=tmp_path / "instruments.json")
    service = QuickPriceService(settings)
    store = InstrumentPolicyStore(settings.managed_instruments_path, build_registry())
    store.create_instrument(payload, store.snapshot()["revision"])
    generation = store.staged_generation()
    assert generation is not None
    registry = generation.to_registry()
    runtime = InstrumentCatalogRuntime(service, settings, store)
    _, graph, _ = runtime._compile(generation, strict=False)
    coordinator = MarketDataCoordinator(
        service,
        settings,
        registry,
        generation_id=service.capture_generation().generation_id,
        graph=graph,
        catalog=generation,
    )
    calls: dict[str, datetime] = {}

    class Router:
        @staticmethod
        def configured(_symbol, _capability):
            return True

        @staticmethod
        async def get_history(_symbol, *, interval, start, **_kwargs):
            calls.setdefault(interval, start)
            return ()

    coordinator.router = Router()
    try:
        await coordinator._backfill_symbol("DOGE:USDC")
    finally:
        await coordinator.stop(persist_checkpoints=False)

    now = datetime.now(UTC)
    assert coordinator._history_poll_seconds("DOGE:USDC") == 17
    assert timedelta(hours=23) < now - calls["1m"] < timedelta(hours=25)
    assert timedelta(hours=23) < now - calls["5m"] < timedelta(hours=25)
    assert timedelta(days=365) < now - calls["1d"] < timedelta(days=367)
