from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quickprice.builtin_plugin import FX_INSTRUMENTS
from quickprice.collectors import MarketDataCoordinator, derive_cross_history
from quickprice.config import Settings
from quickprice.domain import PricePoint, ProviderQuote
from quickprice.fx import FX_HUB_SYMBOLS, FX_SYMBOLS
from quickprice.plugin_api import AssetClass, InstrumentPlugin, InstrumentSpec
from quickprice.providers.base import Capability
from quickprice.providers.wiring import build_provider_graph
from quickprice.registry import InstrumentRegistry
from quickprice.service import QuickPriceService


def _large_registry(count: int, *, history_enabled: bool) -> InstrumentRegistry:
    instruments = tuple(
        InstrumentSpec(
            symbol=f"ASSET{index}:USD",
            base=f"ASSET{index}",
            quote="USD",
            name=f"Asset {index}",
            description="A collection scheduler scalability fixture.",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            price_basis="last_trade",
            history_enabled=history_enabled,
            quote_poll_seconds=60,
        )
        for index in range(count)
    )

    class Provider:
        name = "scheduler_fixture"

        async def get_quote(self, symbol):
            return ProviderQuote(symbol, Decimal("1"), datetime.now(UTC), self.name, "fixture")

        async def get_history(self, symbol, **_):
            return ()

    def install(context):
        provider = context.add_provider("scheduler_fixture", Provider())
        for instrument in instruments:
            context.register(instrument.symbol, Capability.QUOTE, [provider])
            if instrument.history_enabled:
                context.register(instrument.symbol, Capability.HISTORY, [provider])

    return InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="scheduler-fixture",
                version="1",
                provider_installer=install,
                instruments=instruments,
            ),
        )
    )


def test_cross_history_aligns_and_divides_components():
    now = datetime(2026, 7, 20, 4, tzinfo=UTC)
    left = [PricePoint("USD:CNH", now, Decimal("7.2"), "twelve")]
    right = [PricePoint("USD:HKD", now - timedelta(minutes=10), Decimal("7.8"), "twelve")]
    result = derive_cross_history(
        "HKD:CNH",
        left,
        right,
        operation="divide",
        max_skew=timedelta(minutes=20),
        provider="synthetic",
        interval="5m",
    )
    assert len(result) == 1
    assert result[0].price == Decimal("7.2") / Decimal("7.8")
    assert result[0].timestamp == now
    assert result[0].is_derived is True


def test_cross_history_rejects_excessive_component_skew():
    now = datetime(2026, 7, 20, 4, tzinfo=UTC)
    result = derive_cross_history(
        "WBETH:USDC",
        [PricePoint("WBETH:ETH", now, Decimal("1.1"), "binance")],
        [PricePoint("ETH:USDC", now - timedelta(seconds=3), Decimal("4000"), "binance")],
        operation="multiply",
        max_skew=timedelta(seconds=2),
        provider="synthetic",
        interval="1m",
    )
    assert result == ()


def test_cross_history_never_uses_a_future_component():
    now = datetime(2026, 7, 20, 4, tzinfo=UTC)
    result = derive_cross_history(
        "GBP:EUR",
        [PricePoint("USD:EUR", now, Decimal("0.90"), "twelve")],
        [
            PricePoint("USD:GBP", now - timedelta(minutes=2), Decimal("0.75"), "twelve"),
            PricePoint("USD:GBP", now + timedelta(minutes=1), Decimal("0.80"), "twelve"),
        ],
        operation="divide",
        max_skew=timedelta(minutes=20),
        provider="synthetic_fx",
        interval="1m",
    )

    assert result[0].price == Decimal("0.90") / Decimal("0.75")
    assert result[0].timestamp == now


@pytest.mark.asyncio
async def test_fx_backfill_fetches_only_hubs_then_materializes_every_cross() -> None:
    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="fx-history-fixture",
                version="1",
                instruments=FX_INSTRUMENTS,
                provider_installer=lambda _: None,
            ),
        )
    )
    prices = {
        "USD:EUR": Decimal("0.90"),
        "USD:GBP": Decimal("0.75"),
        "USD:HKD": Decimal("7.80"),
        "USD:SGD": Decimal("1.25"),
        "USD:CNH": Decimal("7.20"),
    }
    timestamp = datetime(2026, 7, 20, 4, tzinfo=UTC)

    class History:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], list[PricePoint]] = {}

        def points_for_interval(self, symbol: str, interval: str):
            return tuple(self.values.get((symbol, interval), ()))

        def add(self, points):
            for item in points:
                self.values.setdefault((item.symbol, item.interval), []).append(item)

    class Service:
        def __init__(self) -> None:
            self.history = History()
            self.published_sizes: list[int] = []
            self.persist_flags: list[bool] = []

        async def publish_history_async(self, points, *, persist=True):
            self.published_sizes.append(len(points))
            self.persist_flags.append(persist)
            self.history.add(points)

    service = Service()
    coordinator = MarketDataCoordinator(
        service,
        Settings(background_enabled=False),
        registry,
    )
    upstream_calls: list[str] = []

    async def backfill(symbol: str) -> None:
        upstream_calls.append(symbol)
        service.history.add(
            [
                PricePoint(
                    symbol,
                    timestamp,
                    prices[symbol],
                    "twelve_data",
                    interval=interval,
                )
                for interval in ("1m", "5m", "1d")
            ]
        )

    coordinator._backfill_symbol = backfill
    try:
        await coordinator._backfill_history(include_fx=True)
    finally:
        await coordinator.graph.close()

    assert set(upstream_calls) == set(FX_HUB_SYMBOLS)
    assert len(upstream_calls) == len(FX_HUB_SYMBOLS)
    for symbol in FX_SYMBOLS:
        for interval in ("1m", "5m", "1d"):
            assert service.history.points_for_interval(symbol, interval)
    assert service.history.points_for_interval("GBP:CNH", "1d")[0].price == (
        Decimal("7.20") / Decimal("0.75")
    )
    assert service.history.points_for_interval("EUR:USD", "1d")[0].price == (
        Decimal(1) / Decimal("0.90")
    )

    await coordinator._materialize_builtin_fx_history()
    assert len(service.published_sizes) == 2 * (len(FX_SYMBOLS) - len(FX_HUB_SYMBOLS))
    assert sum(service.published_sizes[-25:]) == 75
    assert service.persist_flags == [False] * len(service.persist_flags)


@pytest.mark.asyncio
async def test_fx_hub_history_refresh_repeats_once_per_day(monkeypatch) -> None:
    import quickprice.collectors as collectors

    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False, history_poll_seconds=3_600),
        _large_registry(1, history_enabled=False),
    )
    now = 0.0
    calls: list[bool] = []

    async def backfill(*, include_fx: bool) -> None:
        calls.append(include_fx)
        if len(calls) == 26:
            raise asyncio.CancelledError

    async def advance(delay: float) -> None:
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

    assert calls[0] is True
    assert calls[1:24] == [False] * 23
    assert calls[24] is True
    assert calls[25] is False


@pytest.mark.asyncio
async def test_trusted_plugin_installer_adds_a_symbol_without_core_changes() -> None:
    symbol = "PLUGIN:USD"

    class Provider:
        name = "plugin_fixture"

        async def get_quote(self, requested_symbol):
            return ProviderQuote(
                requested_symbol,
                Decimal("42"),
                datetime(2026, 7, 20, 4, tzinfo=UTC),
                self.name,
                "fixture",
            )

        async def get_history(self, requested_symbol, **_):
            return (
                PricePoint(
                    requested_symbol,
                    datetime(2026, 7, 19, 4, tzinfo=UTC),
                    Decimal("40"),
                    self.name,
                    interval="1d",
                ),
            )

    def install(context):
        provider = context.add_provider("plugin_fixture", Provider())
        context.register(symbol, Capability.QUOTE, [provider])
        context.register(symbol, Capability.HISTORY, [provider])

    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="provider-test",
                version="1",
                provider_installer=install,
                instruments=(
                    InstrumentSpec(
                        symbol=symbol,
                        base="PLUGIN",
                        quote="USD",
                        name="Plugin Asset",
                        description="A provider installation fixture.",
                        asset_class=AssetClass.CRYPTO,
                        asset_type="spot_crypto",
                        price_basis="last_trade",
                    ),
                ),
            ),
        )
    )
    graph = build_provider_graph(Settings(background_enabled=False), registry)
    try:
        assert (await graph.router.get_quote(symbol)).price == Decimal("42")
        assert graph.router.configured(symbol, Capability.HISTORY)
    finally:
        await graph.close()


def test_unknown_market_status_is_conservatively_closed_outside_equity_hours(monkeypatch):
    import quickprice.collectors as collectors

    monkeypatch.setattr(
        collectors,
        "utc_now",
        lambda: datetime(2026, 7, 19, 16, tzinfo=UTC),  # Sunday noon New York
    )
    quote = ProviderQuote(
        "QQQM:USD",
        Decimal("250"),
        datetime(2026, 7, 17, 20, tzinfo=UTC),
        "fallback",
        "eod",
        market_status="unknown",
    )
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
    )
    normalized = coordinator._normalize_market_status(quote)
    assert normalized.market_status == "closed"


@pytest.mark.asyncio
async def test_finnhub_quote_uses_live_quota_safe_cadence_instead_of_daily_fallback(
    monkeypatch,
) -> None:
    import quickprice.collectors as collectors

    monkeypatch.setattr(
        collectors,
        "utc_now",
        lambda: datetime(2026, 7, 20, 15, 30, tzinfo=UTC),
    )
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(
            production=False,
            require_free_threaded=False,
            background_enabled=False,
            finnhub_api_key="finnhub-key",
        ),
    )
    try:
        provider = coordinator.graph.providers["finnhub"]
        provider._request_json = AsyncMock(return_value={"c": "245.18", "t": 1784561400})

        next_interval = await coordinator._poll_quote_once("QQQM:USD")

        assert next_interval == provider.minimum_quote_poll_seconds == 20
        assert coordinator._pending["QQQM:USD"].provider == "finnhub"
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_finnhub_fresh_stream_suppresses_rest_polling(monkeypatch) -> None:
    import quickprice.collectors as collectors

    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(
            production=False,
            require_free_threaded=False,
            background_enabled=False,
            finnhub_api_key="finnhub-key",
        ),
    )
    try:
        provider = coordinator.graph.providers["finnhub"]
        provider._request_json = AsyncMock()
        monkeypatch.setattr(collectors.time, "monotonic", lambda: 100.0)
        coordinator._stream_observed_at[(id(provider), "QQQM:USD")] = 99.0

        next_interval = await coordinator._poll_quote_once("QQQM:USD")

        assert next_interval == provider.minimum_quote_poll_seconds == 20
        provider._request_json.assert_not_awaited()
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_finnhub_stale_stream_observation_allows_rest_fallback(monkeypatch) -> None:
    import quickprice.collectors as collectors

    monkeypatch.setattr(
        collectors,
        "utc_now",
        lambda: datetime(2026, 7, 20, 15, 30, tzinfo=UTC),
    )
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(
            production=False,
            require_free_threaded=False,
            background_enabled=False,
            finnhub_api_key="finnhub-key",
        ),
    )
    try:
        provider = coordinator.graph.providers["finnhub"]
        provider._request_json = AsyncMock(return_value={"c": "245.18", "t": 1784561400})
        monkeypatch.setattr(collectors.time, "monotonic", lambda: 221.0)
        coordinator._stream_observed_at[(id(provider), "QQQM:USD")] = 100.0

        next_interval = await coordinator._poll_quote_once("QQQM:USD")

        assert next_interval == provider.minimum_quote_poll_seconds == 20
        provider._request_json.assert_awaited_once()
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_finnhub_closed_market_rest_polling_is_reduced(monkeypatch) -> None:
    import quickprice.collectors as collectors

    monkeypatch.setattr(
        collectors,
        "utc_now",
        lambda: datetime(2026, 7, 19, 16, tzinfo=UTC),
    )
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(
            production=False,
            require_free_threaded=False,
            background_enabled=False,
            finnhub_api_key="finnhub-key",
        ),
    )
    try:
        provider = coordinator.graph.providers["finnhub"]
        provider._request_json = AsyncMock(return_value={"c": "245.18", "t": 1784318400})

        next_interval = await coordinator._poll_quote_once("QQQM:USD")

        assert next_interval == provider.closed_market_quote_poll_seconds == 900
        assert coordinator._pending["QQQM:USD"].market_status == "closed"
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_provider_quota_usage_is_restored_from_sqlite(tmp_path):
    settings = Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "quota.db",
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
        twelve_data_api_key="fixture-key",
        sqlite_batch_ms=10,
    )
    first_service = QuickPriceService(settings)
    await first_service.start()
    first = MarketDataCoordinator(first_service, settings)
    await first._restore_and_bind_provider_state()
    assert await first.graph.providers["twelve_data"].quota.acquire(3)
    await first.graph.close()
    await first_service.stop()

    restored_service = QuickPriceService(settings)
    await restored_service.start()
    restored = MarketDataCoordinator(restored_service, settings)
    await restored._restore_and_bind_provider_state()
    snapshot = await restored.graph.providers["twelve_data"].quota.snapshot()
    assert snapshot.used == 3
    await restored.graph.close()
    await restored_service.stop()


@pytest.mark.asyncio
async def test_fatal_supervisor_state_is_visible_and_stop_does_not_reraise(tmp_path):
    settings = Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "unused.db",
    )
    coordinator = MarketDataCoordinator(
        SimpleNamespace(restored_provider_checkpoints=lambda: {}, _storage=None), settings
    )

    async def fail_after_start():
        coordinator._started.set()
        raise RuntimeError("collector exploded")

    coordinator._run = fail_after_start
    await coordinator.start()
    await asyncio.sleep(0)
    assert coordinator.is_running is False
    assert isinstance(coordinator.fatal_error, RuntimeError)
    await coordinator.stop()


@pytest.mark.asyncio
async def test_history_fetch_uses_forward_windows_no_larger_than_5000_bars(tmp_path):
    del tmp_path
    start = datetime(2026, 6, 5, tzinfo=UTC)
    end = start + timedelta(days=45)
    step = timedelta(minutes=5)

    class Router:
        def __init__(self):
            self.calls = []

        async def get_history(self, symbol, *, interval, start, end, limit):
            self.calls.append((start, end, limit))
            count = int((end - start) / step) + 1
            assert count <= 5_000
            return tuple(
                PricePoint(
                    symbol, start + step * index, Decimal("7.2"), "twelve_data", interval=interval
                )
                for index in range(count)
            )

    class Service:
        def __init__(self):
            self.received = []

        async def publish_history_async(self, points):
            self.received.extend(points)

    coordinator = MarketDataCoordinator(Service(), Settings(background_enabled=False))
    router = Router()
    coordinator.router = router
    result = await coordinator._fetch_history_pages("USD:CNH", "5m", start, end)

    assert result[0].timestamp == start
    assert result[-1].timestamp == end
    assert len(router.calls) == 3
    assert all(limit == 5_000 for _, _, limit in router.calls)
    await coordinator.graph.close()


@pytest.mark.asyncio
async def test_quote_scheduler_does_not_wait_for_the_slowest_inflight_symbol() -> None:
    registry = _large_registry(33, history_enabled=False)
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
        registry,
    )
    blocked = asyncio.Event()
    observed_last_symbol = asyncio.Event()
    called: list[str] = []

    async def poll(symbol: str) -> float:
        called.append(symbol)
        if symbol == "ASSET32:USD":
            observed_last_symbol.set()
        if symbol == "ASSET0:USD":
            await blocked.wait()
        return 60

    coordinator._poll_quote_once = poll
    task = asyncio.create_task(coordinator._quote_scheduler_loop())
    try:
        async with asyncio.timeout(1):
            await observed_last_symbol.wait()
        assert blocked.is_set() is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_history_backfill_uses_a_fixed_worker_pool() -> None:
    registry = _large_registry(50, history_enabled=True)
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
        registry,
    )
    active = 0
    maximum_active = 0
    completed: list[str] = []

    async def backfill(symbol: str) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        completed.append(symbol)
        active -= 1

    coordinator._backfill_symbol = backfill
    try:
        await coordinator._backfill_history(include_fx=True)
    finally:
        await coordinator.graph.close()

    assert len(completed) == 50
    assert maximum_active == 2
