from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quickprice.builtin_plugin import FX_INSTRUMENTS
from quickprice.cache import HistoryCache
from quickprice.collectors import MarketDataCoordinator, derive_cross_history
from quickprice.config import Settings
from quickprice.domain import (
    PricePoint,
    ProviderQuote,
    RewardAccrualMode,
    YieldMetric,
    YieldQuality,
)
from quickprice.fx import FX_HUB_SYMBOLS, FX_SYMBOLS
from quickprice.plugin_api import (
    AssetClass,
    InstrumentPlugin,
    InstrumentSpec,
    MarketCalendar,
    YieldStrategy,
)
from quickprice.provider_factory import (
    create_builtin_alpha_vantage_provider,
    create_builtin_twelve_data_provider,
)
from quickprice.providers.base import (
    Capability,
    NetworkUnavailable,
    ProviderBusy,
    ProviderUnavailable,
)
from quickprice.providers.router import ProviderRouter
from quickprice.providers.wiring import build_provider_graph
from quickprice.registry import InstrumentRegistry
from quickprice.service import QuickPriceService
from quickprice.staking import ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD


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


def _yield_registry() -> InstrumentRegistry:
    instrument = InstrumentSpec(
        symbol="WBETH:USDC",
        base="WBETH",
        quote="USDC",
        name="Wrapped Binance Beacon ETH",
        description="A metadata retry scheduler fixture.",
        asset_class=AssetClass.CRYPTO,
        asset_type="liquid_staking_token",
        price_basis="synthetic_cross",
        yield_strategy=YieldStrategy.STAKING_PROVIDER_METRIC,
        reward_accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
        underlying_asset="ETH",
        history_enabled=False,
    )
    return InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="metadata-retry-fixture",
                version="1",
                instruments=(instrument,),
                provider_installer=lambda _: None,
            ),
        )
    )


def _fx_hub_registry(
    *symbols: str,
    include_non_fx: bool = False,
    include_plugin_fx: bool = False,
) -> InstrumentRegistry:
    instruments = tuple(
        InstrumentSpec(
            symbol=symbol,
            base=symbol.split(":", 1)[0],
            quote=symbol.split(":", 1)[1],
            name=symbol,
            description="An FX collector retry fixture.",
            asset_class=AssetClass.FX,
            asset_type="forex_pair",
            price_basis="vendor_aggregate",
            market_calendar=MarketCalendar.FX_24X5,
            stale_after_seconds=300 if symbol == "USD:CNH" else 1200,
            quote_poll_seconds=240 if symbol == "USD:CNH" else 900,
        )
        for symbol in symbols
    )
    if include_non_fx:
        instruments += (
            InstrumentSpec(
                symbol="TOKEN:USD",
                base="TOKEN",
                quote="USD",
                name="Token",
                description="A non-FX retry isolation fixture.",
                asset_class=AssetClass.CRYPTO,
                asset_type="spot_crypto",
                price_basis="last_trade",
            ),
        )
    if include_plugin_fx:
        instruments += (
            InstrumentSpec(
                symbol="CAD:JPY",
                base="CAD",
                quote="JPY",
                name="Canadian Dollar / Japanese Yen",
                description="A plugin-defined FX retry isolation fixture.",
                asset_class=AssetClass.FX,
                asset_type="forex_pair",
                price_basis="vendor_aggregate",
                market_calendar=MarketCalendar.FX_24X5,
                quote_poll_seconds=900,
            ),
        )
    return InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="fx-retry-fixture",
                version="1",
                instruments=instruments,
                provider_installer=lambda _: None,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_proxy", "fallback_level", "stale"),
    [
        (True, 0, False),
        (False, 1, False),
        (False, 0, True),
    ],
)
async def test_metadata_loop_retries_degraded_yield_then_restores_primary(
    monkeypatch,
    is_proxy: bool,
    fallback_level: int,
    stale: bool,
) -> None:
    import quickprice.collectors as collectors

    as_of = datetime(2026, 7, 20, 15, tzinfo=UTC)
    fallback = YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal("3.2"),
        as_of=as_of,
        method="market_ratio_30d",
        provider="market_fallback",
        is_proxy=is_proxy,
        quality=YieldQuality(stale=stale, confidence="low"),
        fallback_level=fallback_level,
    )
    primary = YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal("2.8"),
        as_of=as_of,
        method="exchange_rate_growth",
        provider="binance",
    )
    settings = Settings(
        background_enabled=False,
        metadata_poll_seconds=21_600,
        metadata_retry_seconds=300,
    )
    registry = _yield_registry()
    service = QuickPriceService(settings, registry)
    coordinator = MarketDataCoordinator(
        service,
        settings,
        registry,
    )

    class Router:
        calls = 0

        def configured(self, *_args):
            return True

        async def get_yield(self, _symbol):
            self.calls += 1
            return fallback if self.calls == 1 else primary

    router = Router()
    coordinator.router = router
    monotonic = 0.0
    sleeps: list[float] = []
    observed_metrics: list[YieldMetric | None] = []

    async def advance(delay: float) -> None:
        nonlocal monotonic
        sleeps.append(delay)
        observed_metrics.append(service._yield_metrics.get("WBETH:USDC"))
        if len(sleeps) == 2:
            raise asyncio.CancelledError
        monotonic += delay

    monkeypatch.setattr(collectors.time, "monotonic", lambda: monotonic)
    monkeypatch.setattr(collectors.asyncio, "sleep", advance)
    try:
        with pytest.raises(asyncio.CancelledError):
            await coordinator._metadata_loop()
    finally:
        await coordinator.graph.close()

    assert router.calls == 2
    assert observed_metrics == [fallback, primary]
    assert service._yield_metrics["WBETH:USDC"] == primary
    assert sleeps == [300, 21_600]


@pytest.mark.asyncio
async def test_metadata_loop_keeps_normal_cadence_for_current_daily_onchain_index(
    monkeypatch,
) -> None:
    import quickprice.collectors as collectors

    now = datetime(2026, 7, 20, 16, tzinfo=UTC)
    metric = YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal("2.4187"),
        as_of=now - timedelta(hours=16),
        method=ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD,
        provider="ethereum_exchange_rate",
        quality=YieldQuality(
            stale=False,
            staleness_ms=16 * 60 * 60 * 1000,
            confidence="high",
        ),
    )
    settings = Settings(
        background_enabled=False,
        metadata_poll_seconds=21_600,
        metadata_retry_seconds=300,
    )
    registry = _yield_registry()
    service = QuickPriceService(settings, registry)
    coordinator = MarketDataCoordinator(service, settings, registry)

    class Router:
        calls = 0

        def configured(self, *_args):
            return True

        async def get_yield(self, _symbol):
            self.calls += 1
            return metric

    router = Router()
    coordinator.router = router
    sleeps: list[float] = []

    async def stop_after_scheduled_delay(delay: float) -> None:
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(collectors.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(collectors.asyncio, "sleep", stop_after_scheduled_delay)
    try:
        with pytest.raises(asyncio.CancelledError):
            await coordinator._metadata_loop()
    finally:
        await coordinator.graph.close()

    assert router.calls == 1
    assert service._yield_metrics["WBETH:USDC"] == metric
    assert sleeps == [21_600]


@pytest.mark.asyncio
async def test_metadata_loop_retries_fetch_failure_without_busy_loop(monkeypatch) -> None:
    import quickprice.collectors as collectors

    primary = YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal("2.8"),
        as_of=datetime(2026, 7, 20, 15, tzinfo=UTC),
        method="exchange_rate_growth",
        provider="binance",
    )
    settings = Settings(
        background_enabled=False,
        metadata_poll_seconds=21_600,
        metadata_retry_seconds=300,
    )
    registry = _yield_registry()
    service = QuickPriceService(settings, registry)
    coordinator = MarketDataCoordinator(
        service,
        settings,
        registry,
    )

    class Router:
        calls = 0

        def configured(self, *_args):
            return True

        async def get_yield(self, _symbol):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary upstream failure")
            return primary

    router = Router()
    coordinator.router = router
    monotonic = 0.0
    sleeps: list[float] = []
    observed_metrics: list[YieldMetric | None] = []

    async def advance(delay: float) -> None:
        nonlocal monotonic
        sleeps.append(delay)
        observed_metrics.append(service._yield_metrics.get("WBETH:USDC"))
        if len(sleeps) == 2:
            raise asyncio.CancelledError
        monotonic += delay

    monkeypatch.setattr(collectors.time, "monotonic", lambda: monotonic)
    monkeypatch.setattr(collectors.asyncio, "sleep", advance)
    try:
        with pytest.raises(asyncio.CancelledError):
            await coordinator._metadata_loop()
    finally:
        await coordinator.graph.close()

    assert router.calls == 2
    assert observed_metrics == [None, primary]
    assert service._yield_metrics["WBETH:USDC"] == primary
    assert sleeps == [300, 21_600]
    assert "yield:WBETH:USDC" not in coordinator._last_errors


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
async def test_fx_backfill_keeps_only_hubs_and_registers_virtual_crosses() -> None:
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

    class Service:
        def __init__(self) -> None:
            self.history = HistoryCache()
            self.published_sizes: list[int] = []
            self.persist_flags: list[bool] = []

        async def publish_history_async(self, points, *, persist=True):
            self.published_sizes.append(len(points))
            self.persist_flags.append(persist)
            self.history.load(list(points))

    service = Service()
    coordinator = MarketDataCoordinator(
        service,
        Settings(background_enabled=False),
        registry,
    )
    upstream_calls: list[str] = []

    async def backfill(symbol: str) -> None:
        upstream_calls.append(symbol)
        service.history.load(
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
    for symbol in FX_HUB_SYMBOLS:
        for interval in ("1m", "5m", "1d"):
            assert service.history.points_for_interval(symbol, interval)
    derived_symbols = set(FX_SYMBOLS) - set(FX_HUB_SYMBOLS)
    assert all(
        not service.history.points_for_interval(symbol, interval)
        for symbol in derived_symbols
        for interval in ("1m", "5m", "1d")
    )
    reference_at = timestamp + timedelta(days=366)
    assert service.history.change_references("GBP:CNH", reference_at)["1y"].price == (
        Decimal("7.20") / Decimal("0.75")
    )
    assert service.history.change_references("EUR:USD", reference_at)["1y"].price == (
        Decimal(1) / Decimal("0.90")
    )

    await coordinator._register_builtin_fx_history()
    assert service.published_sizes == []
    assert service.persist_flags == []


@pytest.mark.asyncio
async def test_restored_fx_tails_are_replaced_by_virtual_one_year_references(
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)
    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="fx-restore-prefix-fixture",
                version="1",
                instruments=FX_INSTRUMENTS,
                provider_installer=lambda _: None,
            ),
        )
    )
    settings = Settings(background_enabled=False)
    service = QuickPriceService(settings, registry)
    old_at = now - timedelta(days=366)
    recent_at = now - timedelta(days=1)
    hub_prices = {
        "USD:EUR": Decimal("0.90"),
        "USD:GBP": Decimal("0.75"),
        "USD:HKD": Decimal("7.80"),
        "USD:SGD": Decimal("1.25"),
        "USD:CNH": Decimal("7.20"),
    }
    restored = [
        PricePoint(symbol, timestamp, price, "twelve_data", interval="1d")
        for symbol, price in hub_prices.items()
        for timestamp in (old_at, recent_at)
    ]
    # Simulate a restart with complete persisted hubs but only a recent tail
    # for every derived inverse/cross.
    for symbol in set(FX_SYMBOLS) - set(FX_HUB_SYMBOLS):
        restored.append(
            PricePoint(
                symbol,
                recent_at,
                Decimal("1"),
                "synthetic_fx",
                is_derived=True,
                interval="1d",
            )
        )
    service.history.load(restored)
    coordinator = MarketDataCoordinator(service, settings, registry)
    monkeypatch.setattr("quickprice.collectors.utc_now", lambda: now)
    try:
        await coordinator._register_builtin_fx_history()
    finally:
        await coordinator.graph.close()

    derived_symbols = set(FX_SYMBOLS) - set(FX_HUB_SYMBOLS)
    assert len(derived_symbols) == 25
    for symbol in derived_symbols:
        assert service.history.points_for_interval(symbol, "1d") == ()
        assert service.history.change_references(symbol, now)["1y"] is not None


@pytest.mark.asyncio
async def test_fx_startup_fetches_all_hub_daily_histories_before_intraday(monkeypatch) -> None:
    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="fx-daily-priority-fixture",
                version="1",
                instruments=FX_INSTRUMENTS,
                provider_installer=lambda _: None,
            ),
        )
    )
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)

    class History:
        def points_for_interval(self, _symbol, _interval):
            return ()

    coordinator = MarketDataCoordinator(
        SimpleNamespace(history=History()),
        Settings(background_enabled=False),
        registry,
    )
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    calls: list[tuple[str, str]] = []

    async def fetch(symbol, interval, start, end):
        del end
        calls.append((symbol, interval))
        return (PricePoint(symbol, start, Decimal("1"), "twelve_data", interval=interval),)

    coordinator._fetch_history_pages = fetch
    monkeypatch.setattr("quickprice.collectors.utc_now", lambda: now)
    try:
        complete = await coordinator._backfill_history(include_fx=True)
    finally:
        await coordinator.graph.close()

    assert complete is True
    assert calls[:5] == [(symbol, "1d") for symbol in FX_HUB_SYMBOLS]
    assert all(interval != "1d" for _, interval in calls[5:])


@pytest.mark.asyncio
async def test_run_awaits_all_fx_daily_preseed_before_quote_and_history_tasks(
    monkeypatch,
) -> None:
    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="fx-startup-lifecycle-fixture",
                version="1",
                instruments=FX_INSTRUMENTS,
                provider_installer=lambda _: None,
            ),
        )
    )
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)

    history = HistoryCache()

    async def publish_history(points, *, persist=True):
        del persist
        history.load(list(points))

    coordinator = MarketDataCoordinator(
        SimpleNamespace(history=history, publish_history_async=publish_history),
        Settings(background_enabled=False),
        registry,
    )
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    events: list[tuple[str, str | None]] = []

    async def fetch(symbol, interval, start, end):
        del end
        events.append(("daily", symbol))
        points = (
            PricePoint(symbol, start, Decimal("1"), "twelve_data", interval=interval),
            PricePoint(
                symbol,
                now - timedelta(days=1),
                Decimal("1.01"),
                "twelve_data",
                interval=interval,
            ),
        )
        history.load(list(points))
        return points

    async def quote_loop():
        events.append(("quote", None))
        await coordinator._stop.wait()

    async def history_loop():
        events.append(("intraday", None))
        await coordinator._stop.wait()

    async def idle_loop():
        await coordinator._stop.wait()

    coordinator._fetch_history_pages = fetch
    coordinator._quote_scheduler_loop = quote_loop
    coordinator._history_loop = history_loop
    coordinator._publish_loop = idle_loop
    coordinator._metadata_loop = idle_loop
    coordinator._maintenance_loop = idle_loop
    coordinator._stream_symbols = lambda _provider: ()
    monkeypatch.setattr("quickprice.collectors.utc_now", lambda: now)

    task = asyncio.create_task(coordinator._run())
    try:
        await coordinator._started.wait()
        derived_symbols = set(FX_SYMBOLS) - set(FX_HUB_SYMBOLS)
        for symbol in derived_symbols:
            assert history.points_for_interval(symbol, "1d") == ()
            assert history.change_references(symbol, now)["1y"] is not None
        coordinator._stop.set()
        await task
    finally:
        await coordinator.graph.close()

    assert len(events) == 7
    assert {symbol for kind, symbol in events[:5] if kind == "daily"} == set(FX_HUB_SYMBOLS)
    assert {kind for kind, _ in events[5:]} == {"quote", "intraday"}


@pytest.mark.asyncio
async def test_bounded_fx_preseed_timeout_still_starts_quote_collection() -> None:
    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="fx-startup-timeout-fixture",
                version="1",
                instruments=FX_INSTRUMENTS,
                provider_installer=lambda _: None,
            ),
        )
    )

    class History:
        def points_for_interval(self, _symbol, _interval):
            return ()

    coordinator = MarketDataCoordinator(
        SimpleNamespace(history=History()),
        Settings(background_enabled=False),
        registry,
    )
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    coordinator._fx_startup_preseed_timeout_seconds = 0.01
    never = asyncio.Event()
    quote_started = asyncio.Event()

    async def blocked_fetch(*_args, **_kwargs):
        await never.wait()
        return ()

    async def quote_loop():
        quote_started.set()
        await coordinator._stop.wait()

    async def idle_loop():
        await coordinator._stop.wait()

    coordinator._fetch_history_pages = blocked_fetch
    coordinator._quote_scheduler_loop = quote_loop
    coordinator._history_loop = idle_loop
    coordinator._publish_loop = idle_loop
    coordinator._metadata_loop = idle_loop
    coordinator._maintenance_loop = idle_loop
    coordinator._stream_symbols = lambda _provider: ()

    task = asyncio.create_task(coordinator._run())
    try:
        async with asyncio.timeout(0.2):
            await coordinator._started.wait()
            await quote_started.wait()
        coordinator._stop.set()
        await task
    finally:
        await coordinator.graph.close()

    assert "history-daily:startup" in coordinator._last_errors


@pytest.mark.asyncio
async def test_alpha_compact_fx_prefix_is_deferred_without_minute_refetch(
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)
    monotonic = 100.0

    class History:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], list[PricePoint]] = {}

        def points_for_interval(self, symbol, interval):
            return tuple(self.values.get((symbol, interval), ()))

        def add(self, points):
            for point in points:
                self.values.setdefault((point.symbol, point.interval), []).append(point)

    history = History()
    coordinator = MarketDataCoordinator(
        SimpleNamespace(history=history),
        Settings(background_enabled=False),
        _fx_hub_registry("USD:CNH"),
    )
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    calls: list[str] = []

    async def fetch(symbol, interval, start, end):
        del end
        calls.append(interval)
        point = PricePoint(
            symbol,
            now - timedelta(days=99) if interval == "1d" else start,
            Decimal("7.2"),
            "alpha_vantage" if interval == "1d" else "twelve_data",
            interval=interval,
        )
        history.add((point,))
        return (point,)

    coordinator._fetch_history_pages = fetch
    monkeypatch.setattr("quickprice.collectors.utc_now", lambda: now)
    monkeypatch.setattr("quickprice.collectors.time.monotonic", lambda: monotonic)
    try:
        assert await coordinator._startup_preseed_fx_daily() is False
        assert calls == ["1d"]
        assert coordinator._fx_daily_retry_symbols == set()
        assert coordinator._daily_prefix_retry_at["USD:CNH"] == (monotonic + 24 * 60 * 60)

        assert await coordinator._backfill_history(include_fx=True) is True
    finally:
        await coordinator.graph.close()

    assert calls == ["1d", "1m", "5m"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ProviderBusy, ProviderUnavailable])
async def test_transient_fx_daily_failure_retries_without_repeating_intraday(
    monkeypatch,
    error_type,
) -> None:
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)

    class History:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], list[PricePoint]] = {}

        def points_for_interval(self, symbol, interval):
            return tuple(self.values.get((symbol, interval), ()))

        def add(self, points):
            for point in points:
                self.values.setdefault((point.symbol, point.interval), []).append(point)

    history = History()
    coordinator = MarketDataCoordinator(
        SimpleNamespace(history=history),
        Settings(background_enabled=False),
        _fx_hub_registry("USD:CNH"),
    )
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    calls: list[str] = []
    daily_attempts = 0

    async def fetch(symbol, interval, start, end):
        nonlocal daily_attempts
        del end
        calls.append(interval)
        if interval == "1d":
            daily_attempts += 1
            if daily_attempts == 1:
                raise error_type("twelve_data", "temporary failure")
        point = PricePoint(symbol, start, Decimal("7.2"), "twelve_data", interval=interval)
        history.add((point,))
        return (point,)

    coordinator._fetch_history_pages = fetch
    monkeypatch.setattr("quickprice.collectors.utc_now", lambda: now)
    try:
        assert await coordinator._backfill_history(include_fx=True) is False
        assert calls == ["1d", "1m", "5m"]
        assert coordinator._fx_daily_retry_symbols == {"USD:CNH"}

        coordinator._fx_history_retry_only = True
        assert await coordinator._backfill_history(include_fx=True) is True
    finally:
        await coordinator.graph.close()

    assert calls == ["1d", "1m", "5m", "1d"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ProviderBusy, ProviderUnavailable])
async def test_fx_intraday_failure_retries_only_failed_interval(
    monkeypatch,
    error_type,
) -> None:
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)

    class History:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], list[PricePoint]] = {}

        def points_for_interval(self, symbol, interval):
            return tuple(self.values.get((symbol, interval), ()))

        def add(self, points):
            for point in points:
                self.values.setdefault((point.symbol, point.interval), []).append(point)

    history = History()
    coordinator = MarketDataCoordinator(
        SimpleNamespace(history=history),
        Settings(background_enabled=False),
        _fx_hub_registry("USD:CNH"),
    )
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    calls: list[str] = []
    failed_once = False

    async def fetch(symbol, interval, start, end):
        nonlocal failed_once
        del end
        calls.append(interval)
        if interval == "1m" and not failed_once:
            failed_once = True
            raise error_type("twelve_data", "temporary failure")
        point = PricePoint(symbol, start, Decimal("7.2"), "twelve_data", interval=interval)
        history.add((point,))
        return (point,)

    coordinator._fetch_history_pages = fetch
    monkeypatch.setattr("quickprice.collectors.utc_now", lambda: now)
    try:
        assert await coordinator._backfill_history(include_fx=True) is False
        assert calls == ["1d", "1m", "5m"]
        assert coordinator._fx_failed_history_intervals == {("USD:CNH", "1m")}

        coordinator._fx_history_retry_only = True
        assert await coordinator._backfill_history(include_fx=True) is True
    finally:
        await coordinator.graph.close()

    assert calls == ["1d", "1m", "5m", "1m"]
    assert coordinator._fx_failed_history_intervals == set()


@pytest.mark.asyncio
async def test_fx_retry_only_cycle_skips_non_fx_until_the_next_full_cycle() -> None:
    class History:
        def points_for_interval(self, _symbol, _interval):
            return ()

    coordinator = MarketDataCoordinator(
        SimpleNamespace(history=History()),
        Settings(background_enabled=False),
        _fx_hub_registry(
            "USD:CNH",
            include_non_fx=True,
            include_plugin_fx=True,
        ),
    )
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    coordinator._fx_history_retry_only = True
    coordinator._history_full_cycle = False
    coordinator._fx_failed_history_intervals = {("USD:CNH", "1m")}
    calls: list[str] = []

    async def backfill(symbol):
        calls.append(symbol)
        if symbol == "USD:CNH":
            return {"1m": ProviderUnavailable("twelve_data", "still unavailable")}
        return {}

    coordinator._backfill_symbol = backfill
    try:
        assert await coordinator._backfill_history(include_fx=True) is False
        assert calls == ["USD:CNH"]

        coordinator._history_full_cycle = True
        assert await coordinator._backfill_history(include_fx=True) is False
    finally:
        await coordinator.graph.close()

    assert set(calls[1:]) == {"USD:CNH", "TOKEN:USD", "CAD:JPY"}


@pytest.mark.asyncio
async def test_persistent_fx_failure_keeps_independent_hourly_full_history_cycle(
    monkeypatch,
) -> None:
    import quickprice.collectors as collectors

    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False, history_poll_seconds=3_600),
        _large_registry(1, history_enabled=False),
    )
    now = 0.0
    calls: list[tuple[bool, bool, float]] = []

    async def backfill(*, include_fx: bool) -> bool:
        calls.append((include_fx, coordinator._history_full_cycle, now))
        if now >= 3_660:
            raise asyncio.CancelledError
        return False

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

    assert [(full, timestamp) for _, full, timestamp in calls if full] == [
        (True, 0.0),
        (True, 3_600.0),
    ]
    assert all(
        timestamp % 60 == 0 and not full
        for _, full, timestamp in calls
        if timestamp not in {0.0, 3_600.0}
    )


@pytest.mark.asyncio
async def test_fx_daily_failure_retries_after_one_minute_before_daily_deferral(monkeypatch) -> None:
    import quickprice.collectors as collectors

    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False, history_poll_seconds=3_600),
        _large_registry(1, history_enabled=False),
    )
    now = 0.0
    calls: list[tuple[bool, float]] = []

    async def backfill(*, include_fx: bool) -> bool:
        calls.append((include_fx, now))
        if len(calls) == 1:
            return False
        if len(calls) == 2:
            return True
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

    assert calls == [(True, 0.0), (True, 60.0), (False, 3_600.0)]


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
async def test_equity_fallback_keeps_finnhub_cadence_until_alpaca_recovers(
    monkeypatch,
) -> None:
    import quickprice.collectors as collectors

    symbol = "QQQM:USD"
    clock = 0.0
    observed_at = datetime(2026, 7, 20, 15, 30, tzinfo=UTC)

    class Alpaca:
        name = "alpaca"
        stream_poll_suppression_seconds = 120.0
        closed_market_quote_poll_seconds = 900.0
        calls = 0

        async def get_quote(self, requested_symbol):
            self.calls += 1
            if self.calls == 1:
                raise ProviderUnavailable(self.name, "temporary outage")
            return ProviderQuote(
                requested_symbol,
                Decimal("201"),
                observed_at + timedelta(seconds=20),
                self.name,
                "iex",
                market_status="open",
            )

    class Finnhub:
        name = "finnhub"
        minimum_quote_poll_seconds = 20.0
        closed_market_quote_poll_seconds = 900.0

        async def get_quote(self, _symbol):
            raise ProviderUnavailable(self.name, "temporary outage")

    alpaca = Alpaca()
    finnhub = Finnhub()
    twelve = create_builtin_twelve_data_provider(
        "key",
        quote_cache_clock=lambda: clock,
        wall_clock=lambda: observed_at + timedelta(seconds=clock),
    )
    twelve_calls = 0

    async def twelve_request(*_args, **_kwargs):
        nonlocal twelve_calls
        twelve_calls += 1
        return {
            "close": "200",
            "datetime": observed_at.strftime("%Y-%m-%d %H:%M:%S"),
            "is_market_open": True,
        }

    twelve._request_json = twelve_request
    router = ProviderRouter(
        {(symbol, Capability.QUOTE): [alpaca, finnhub, twelve]},
        failure_threshold=100,
        clock=lambda: clock,
    )
    coordinator = MarketDataCoordinator(SimpleNamespace(), Settings(background_enabled=False))
    coordinator.router = router
    monkeypatch.setattr(
        collectors,
        "utc_now",
        lambda: observed_at + timedelta(seconds=clock),
    )
    try:
        first_interval = await coordinator._poll_quote_once(symbol)
        clock = first_interval
        second_interval = await coordinator._poll_quote_once(symbol)
    finally:
        await router.close()
        await coordinator.graph.close()

    assert first_interval == finnhub.minimum_quote_poll_seconds == 20
    assert second_interval == coordinator.registry[symbol].quote_poll_seconds == 5
    assert alpaca.calls == 2
    assert twelve_calls == 1
    assert coordinator._pending[symbol].provider == "alpaca"


@pytest.mark.asyncio
async def test_sustained_equity_primary_failure_reuses_scarce_fallback_caches(
    monkeypatch,
) -> None:
    import quickprice.collectors as collectors

    symbol = "QQQM:USD"
    seconds = 0.0
    origin = datetime(2026, 7, 20, 15, tzinfo=UTC)  # Monday 11:00 New York

    class FailingPrimary:
        stream_poll_suppression_seconds = 120.0
        closed_market_quote_poll_seconds = 900.0

        def __init__(self, name):
            self.name = name
            self.calls = 0
            if name == "finnhub":
                self.minimum_quote_poll_seconds = 20.0

        async def get_quote(self, _symbol):
            self.calls += 1
            raise ProviderUnavailable(self.name, "sustained outage")

    alpaca = FailingPrimary("alpaca")
    finnhub = FailingPrimary("finnhub")
    twelve = create_builtin_twelve_data_provider(
        "key",
        quote_cache_clock=lambda: seconds,
        wall_clock=lambda: origin + timedelta(seconds=seconds),
    )
    alpha = create_builtin_alpha_vantage_provider(
        "key",
        quote_cache_clock=lambda: seconds,
        wall_clock=lambda: origin + timedelta(seconds=seconds),
    )
    twelve_calls = 0
    alpha_calls = 0

    async def twelve_request(*_args, **_kwargs):
        nonlocal twelve_calls
        twelve_calls += 1
        raise ProviderUnavailable("twelve_data", "sustained outage")

    async def alpha_request(*_args, **_kwargs):
        nonlocal alpha_calls
        alpha_calls += 1
        return {
            "Global Quote": {
                "05. price": "200.00",
                "07. latest trading day": "2026-07-17",
            }
        }

    twelve._request_json = twelve_request
    alpha._request_json = alpha_request
    router = ProviderRouter(
        {(symbol, Capability.QUOTE): [alpaca, finnhub, twelve, alpha]},
        failure_threshold=1_000,
        clock=lambda: seconds,
    )
    coordinator = MarketDataCoordinator(SimpleNamespace(), Settings(background_enabled=False))
    coordinator.router = router
    monkeypatch.setattr(
        collectors,
        "utc_now",
        lambda: origin + timedelta(seconds=seconds),
    )
    try:
        for _ in range(180):
            next_interval = await coordinator._poll_quote_once(symbol)
            assert next_interval == 20
            seconds += next_interval
    finally:
        await router.close()
        await coordinator.graph.close()

    assert alpaca.calls == finnhub.calls == 180
    assert twelve_calls == alpha_calls == 1


@pytest.mark.asyncio
async def test_fx_alpha_fallback_keeps_normal_cadence_until_twelve_recovers() -> None:
    symbol = "USD:CNH"
    first_as_of = datetime(2026, 7, 20, 15, tzinfo=UTC)

    class Twelve:
        name = "twelve_data"
        calls = 0

        async def get_quote(self, requested_symbol):
            self.calls += 1
            if self.calls == 1:
                raise ProviderUnavailable(self.name, "temporary outage")
            return ProviderQuote(
                requested_symbol,
                Decimal("7.21"),
                first_as_of + timedelta(minutes=4),
                self.name,
                "twelve_data_fx",
            )

    class Alpha:
        name = "alpha_vantage"

        async def get_quote(self, requested_symbol):
            return ProviderQuote(
                requested_symbol,
                Decimal("7.20"),
                first_as_of,
                self.name,
                "alpha_vantage_fx",
            )

    twelve = Twelve()
    alpha = Alpha()
    router = ProviderRouter({(symbol, Capability.QUOTE): [twelve, alpha]})
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
        _fx_hub_registry(symbol),
    )
    coordinator.router = router
    try:
        first_interval = await coordinator._poll_quote_once(symbol)
        second_interval = await coordinator._poll_quote_once(symbol)
    finally:
        await router.close()
        await coordinator.graph.close()

    assert first_interval == second_interval == 240
    assert twelve.calls == 2
    assert coordinator._pending[symbol].provider == "twelve_data"


@pytest.mark.asyncio
async def test_sustained_twelve_failure_reuses_six_hour_alpha_fx_cache() -> None:
    clock = 0.0
    alpha_upstream_calls: list[str] = []

    class Twelve:
        name = "twelve_data"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_quote(self, symbol):
            self.calls.append(symbol)
            raise ProviderUnavailable(self.name, "sustained outage")

    twelve = Twelve()
    alpha = create_builtin_alpha_vantage_provider(
        "key",
        quote_cache_clock=lambda: clock,
    )

    async def alpha_request(_method, _url, *, params, **_kwargs):
        counter = str(params["to_currency"])
        alpha_upstream_calls.append(counter)
        return {
            "Realtime Currency Exchange Rate": {
                "5. Exchange Rate": "7.20",
                "6. Last Refreshed": "2026-07-20 15:00:00",
            }
        }

    alpha._request_json = alpha_request
    router = ProviderRouter(
        {(symbol, Capability.QUOTE): [twelve, alpha] for symbol in FX_HUB_SYMBOLS},
        failure_threshold=100,
        clock=lambda: clock,
    )
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
        _fx_hub_registry(*FX_HUB_SYMBOLS),
    )
    coordinator.router = router
    events = sorted(
        (second, symbol, coordinator.registry[symbol].quote_poll_seconds)
        for symbol in FX_HUB_SYMBOLS
        for second in range(
            0,
            3_600,
            int(coordinator.registry[symbol].quote_poll_seconds),
        )
    )
    try:
        for second, symbol, expected_interval in events:
            clock = float(second)
            assert await coordinator._poll_quote_once(symbol) == expected_interval
    finally:
        await router.close()
        await coordinator.graph.close()

    assert len(twelve.calls) == len(events) == 31
    assert len(alpha_upstream_calls) == len(FX_HUB_SYMBOLS) == 5
    assert set(alpha_upstream_calls) == {symbol.split(":", 1)[1] for symbol in FX_HUB_SYMBOLS}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "expected_interval"),
    [("USD:CNH", 240), ("USD:HKD", 900)],
)
async def test_all_failed_fx_quote_keeps_normal_primary_probe_cadence(
    symbol: str,
    expected_interval: int,
) -> None:
    class FailingProvider:
        def __init__(self, name):
            self.name = name

        async def get_quote(self, _symbol):
            raise ProviderUnavailable(self.name, "temporary outage")

    router = ProviderRouter(
        {
            (symbol, Capability.QUOTE): [
                FailingProvider("twelve_data"),
                FailingProvider("alpha_vantage"),
            ]
        }
    )
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
        _fx_hub_registry(symbol),
    )
    coordinator.router = router
    try:
        next_interval = await coordinator._poll_quote_once(symbol)
    finally:
        await router.close()
        await coordinator.graph.close()

    assert next_interval == expected_interval
    assert f"quote:{symbol}" in coordinator._last_errors


@pytest.mark.asyncio
async def test_coingecko_negative_cache_retries_slow_staking_quote_at_expiry() -> None:
    monotonic = [100.0]
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False, coingecko_api_key="coingecko-key"),
    )
    provider = coordinator.graph.providers["coingecko"]
    provider._clock = lambda: monotonic[0]
    provider._request_json = AsyncMock(
        side_effect=NetworkUnavailable("coingecko", "temporary outage")
    )
    try:
        next_interval = await coordinator._poll_quote_once("WSTETH:USDC")
    finally:
        await coordinator.graph.close()

    assert coordinator.registry["WSTETH:USDC"].quote_poll_seconds == 660
    assert provider._cache_ttl_seconds == 600
    assert next_interval == 300
    assert provider._request_json.await_count == 1
    assert "simple-price refresh" not in coordinator._last_errors["quote:WSTETH:USDC"]["reason"]


@pytest.mark.asyncio
async def test_fresh_alpaca_stream_observation_suppresses_rest_poll(monkeypatch) -> None:
    import quickprice.collectors as collectors

    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(
            production=False,
            background_enabled=False,
            alpaca_api_key="key",
            alpaca_api_secret="secret",
        ),
    )
    try:
        provider = coordinator.graph.providers["alpaca"]
        provider._request_json = AsyncMock()
        monkeypatch.setattr(collectors.time, "monotonic", lambda: 100.0)
        coordinator._stream_observed_at[(id(provider), "QQQM:USD")] = 99.0

        next_interval = await coordinator._poll_quote_once("QQQM:USD")

        assert coordinator.registry["QQQM:USD"].quote_poll_seconds == 5
        assert next_interval == provider.minimum_quote_poll_seconds == 20
        provider._request_json.assert_not_awaited()
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_name", "symbol", "expected_interval"),
    [
        ("binance", "BTC:USDC", 9.0),
        ("kraken", "XMR:USDC", 9.0),
    ],
)
async def test_fresh_crypto_stream_observation_suppresses_duplicate_rest_poll(
    monkeypatch,
    provider_name: str,
    symbol: str,
    expected_interval: float,
) -> None:
    import quickprice.collectors as collectors

    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
    )
    try:
        provider = coordinator.graph.providers[provider_name]
        provider.get_quote = AsyncMock()
        monkeypatch.setattr(collectors.time, "monotonic", lambda: 100.0)
        coordinator._stream_observed_at[(id(provider), symbol)] = 99.0

        next_interval = await coordinator._poll_quote_once(symbol)

        assert next_interval == pytest.approx(expected_interval)
        provider.get_quote.assert_not_awaited()
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_stream_suppression_rechecks_before_quote_becomes_stale(monkeypatch) -> None:
    import quickprice.collectors as collectors

    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
    )
    try:
        provider = coordinator.graph.providers["binance"]
        provider.get_quote = AsyncMock()
        monkeypatch.setattr(collectors.time, "monotonic", lambda: 100.0)
        coordinator._stream_observed_at[(id(provider), "BTC:USDC")] = 90.5

        next_interval = await coordinator._poll_quote_once("BTC:USDC")

        assert next_interval == pytest.approx(0.5)
        provider.get_quote.assert_not_awaited()
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_stream_suppression_honors_dynamic_long_poll_staleness_deadline(
    monkeypatch,
) -> None:
    import quickprice.collectors as collectors

    instrument = InstrumentSpec(
        symbol="DYNAMIC:USDC",
        base="DYNAMIC",
        quote="USDC",
        name="Dynamic asset",
        description="A long-poll stream suppression fixture.",
        asset_class=AssetClass.CRYPTO,
        asset_type="spot_crypto",
        price_basis="last_trade",
        stale_after_seconds=120.0,
        quote_poll_seconds=120.0,
    )

    class StreamProvider:
        name = "stream_fixture"
        stream_poll_suppression_seconds = 120.0
        stream_poll_recheck_seconds = 10.0

        async def get_quote(self, symbol):
            return ProviderQuote(
                symbol=symbol,
                price=Decimal("100"),
                as_of=datetime.now(UTC),
                provider=self.name,
                feed="fixture",
            )

    provider = StreamProvider()

    def install(context):
        context.add_provider(provider.name, provider)
        context.register(instrument.symbol, Capability.QUOTE, [provider])

    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="long-poll-stream-fixture",
                version="1",
                provider_installer=install,
                instruments=(instrument,),
            ),
        )
    )
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
        registry,
    )
    try:
        monkeypatch.setattr(collectors.time, "monotonic", lambda: 100.0)
        coordinator._stream_observed_at[(id(provider), instrument.symbol)] = 99.0

        next_interval = await coordinator._poll_quote_once(instrument.symbol)

        assert next_interval == pytest.approx(119.0)
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_name", "symbol"),
    [
        ("binance", "BTC:USDC"),
        ("kraken", "XMR:USDC"),
    ],
)
async def test_expired_crypto_stream_observation_restores_rest_polling(
    monkeypatch,
    provider_name: str,
    symbol: str,
) -> None:
    import quickprice.collectors as collectors

    observed_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
    )
    try:
        provider = coordinator.graph.providers[provider_name]
        provider.get_quote = AsyncMock(
            return_value=ProviderQuote(
                symbol=symbol,
                price=Decimal("100"),
                as_of=observed_at,
                provider=provider_name,
                feed="fixture",
            )
        )
        monkeypatch.setattr(collectors, "utc_now", lambda: observed_at)
        monkeypatch.setattr(collectors.time, "monotonic", lambda: 110.0)
        coordinator._stream_observed_at[(id(provider), symbol)] = 100.0

        await coordinator._poll_quote_once(symbol)

        provider.get_quote.assert_awaited_once_with(symbol)
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_fallback_stream_observation_cannot_suppress_primary_poll(monkeypatch) -> None:
    import quickprice.collectors as collectors

    observed_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
    )
    try:
        primary = coordinator.graph.providers["binance"]
        fallback = coordinator.graph.providers["kraken"]
        primary.get_quote = AsyncMock(
            return_value=ProviderQuote(
                symbol="BTC:USDC",
                price=Decimal("100"),
                as_of=observed_at,
                provider="binance",
                feed="fixture",
            )
        )
        monkeypatch.setattr(collectors, "utc_now", lambda: observed_at)
        monkeypatch.setattr(collectors.time, "monotonic", lambda: 100.0)
        coordinator._stream_observed_at[(id(fallback), "BTC:USDC")] = 99.0

        await coordinator._poll_quote_once("BTC:USDC")

        primary.get_quote.assert_awaited_once_with("BTC:USDC")
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_closed_alpaca_quote_uses_fifteen_minute_floor(monkeypatch) -> None:
    import quickprice.collectors as collectors

    now = datetime(2026, 7, 19, 16, tzinfo=UTC)
    monkeypatch.setattr(collectors, "utc_now", lambda: now)
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(
            production=False,
            background_enabled=False,
            alpaca_api_key="key",
            alpaca_api_secret="secret",
        ),
    )
    try:
        provider = coordinator.graph.providers["alpaca"]
        provider._request_json = AsyncMock(
            side_effect=[
                {"trade": {"p": "200", "t": "2026-07-17T19:59:00Z"}},
                {"is_open": False, "timestamp": now.isoformat()},
            ]
        )

        next_interval = await coordinator._poll_quote_once("QQQM:USD")

        assert next_interval == provider.closed_market_quote_poll_seconds == 900
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_closed_all_failed_equity_route_uses_route_wide_floor(monkeypatch) -> None:
    import quickprice.collectors as collectors

    now = datetime(2026, 7, 19, 16, tzinfo=UTC)
    monkeypatch.setattr(collectors, "utc_now", lambda: now)

    class FailingProvider:
        def __init__(self, name, *, closed_floor=0.0, minimum=0.0):
            self.name = name
            self.closed_market_quote_poll_seconds = closed_floor
            if minimum:
                self.minimum_quote_poll_seconds = minimum

        async def get_quote(self, _symbol):
            raise ProviderUnavailable(self.name, "weekend outage")

    router = ProviderRouter(
        {
            ("QQQM:USD", Capability.QUOTE): [
                FailingProvider("alpaca", closed_floor=900),
                FailingProvider("finnhub", closed_floor=900, minimum=20),
                FailingProvider("twelve_data"),
                FailingProvider("alpha_vantage"),
            ]
        }
    )
    coordinator = MarketDataCoordinator(SimpleNamespace(), Settings(background_enabled=False))
    coordinator.router = router
    try:
        next_interval = await coordinator._poll_quote_once("QQQM:USD")
    finally:
        await router.close()
        await coordinator.graph.close()

    assert next_interval == 900


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
async def test_fatal_startup_is_surfaced_and_stop_does_not_reraise(tmp_path):
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
    with pytest.raises(RuntimeError, match="collector failed during startup"):
        await coordinator.start()
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
async def test_daily_backfill_recovers_old_prefix_before_resuming_tail_updates(monkeypatch) -> None:
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)
    recent = PricePoint(
        "BOXX:USD",
        now - timedelta(days=100),
        Decimal("110"),
        "alpha_vantage",
        interval="1d",
    )

    class History:
        daily = (recent,)

        def points_for_interval(self, symbol, interval):
            assert symbol == "BOXX:USD"
            return self.daily if interval == "1d" else ()

    service = SimpleNamespace(history=History())
    coordinator = MarketDataCoordinator(service, Settings(background_enabled=False))
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    calls: list[tuple[str, datetime, datetime]] = []

    async def fetch(symbol, interval, start, end):
        calls.append((interval, start, end))
        return ()

    coordinator._fetch_history_pages = fetch
    monkeypatch.setattr("quickprice.collectors.utc_now", lambda: now)
    try:
        await coordinator._backfill_symbol("BOXX:USD")
        daily_call = next(item for item in calls if item[0] == "1d")
        assert daily_call[1] == now - timedelta(days=400)

        calls.clear()
        service.history.daily = (
            PricePoint(
                "BOXX:USD",
                now - timedelta(days=399),
                Decimal("100"),
                "twelve_data",
                interval="1d",
            ),
            PricePoint(
                "BOXX:USD",
                now - timedelta(days=1),
                Decimal("111"),
                "twelve_data",
                interval="1d",
            ),
        )
        await coordinator._backfill_symbol("BOXX:USD")
        daily_call = next(item for item in calls if item[0] == "1d")
        assert daily_call[1] == now - timedelta(days=2)
    finally:
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_daily_boundary_at_analytics_cutoff_uses_tail_only_across_cycles(
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)
    boundary = (now - timedelta(days=365)).replace(hour=0, minute=0, second=0, microsecond=0)

    class History:
        def points_for_interval(self, symbol, interval):
            assert symbol == "STETH:USDC"
            if interval != "1d":
                return ()
            return (
                PricePoint(symbol, boundary, Decimal("1800"), "coingecko", interval="1d"),
                PricePoint(
                    symbol,
                    now - timedelta(days=1),
                    Decimal("1900"),
                    "coingecko",
                    interval="1d",
                ),
            )

    coordinator = MarketDataCoordinator(
        SimpleNamespace(history=History()),
        Settings(background_enabled=False),
    )
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    calls: list[tuple[str, datetime]] = []

    async def fetch(symbol, interval, start, end):
        del symbol, end
        calls.append((interval, start))
        return ()

    coordinator._fetch_history_pages = fetch
    monkeypatch.setattr("quickprice.collectors.utc_now", lambda: now)
    try:
        for _ in range(3):
            await coordinator._backfill_symbol("STETH:USDC")
    finally:
        await coordinator.graph.close()

    daily_starts = [start for interval, start in calls if interval == "1d"]
    assert daily_starts == [now - timedelta(days=2)] * 3
    assert all(start > now - timedelta(days=365) for start in daily_starts)


@pytest.mark.asyncio
async def test_recent_listing_full_prefix_is_retried_only_once_per_day(monkeypatch) -> None:
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)
    listed_at = now - timedelta(days=30)

    class History:
        def points_for_interval(self, symbol, interval):
            assert symbol == "CRCL:USD"
            if interval != "1d":
                return ()
            return (PricePoint(symbol, listed_at, Decimal("100"), "alpaca", interval="1d"),)

    coordinator = MarketDataCoordinator(
        SimpleNamespace(history=History()),
        Settings(background_enabled=False),
    )
    coordinator.router = SimpleNamespace(configured=lambda *_: True)
    monotonic = [100.0]
    calls: list[tuple[str, datetime]] = []

    async def fetch(symbol, interval, start, end):
        del end
        calls.append((interval, start))
        if interval == "1d":
            return (PricePoint(symbol, listed_at, Decimal("100"), "alpaca", interval="1d"),)
        return ()

    coordinator._fetch_history_pages = fetch
    monkeypatch.setattr("quickprice.collectors.utc_now", lambda: now)
    monkeypatch.setattr("quickprice.collectors.time.monotonic", lambda: monotonic[0])
    try:
        for _ in range(3):
            await coordinator._backfill_symbol("CRCL:USD")
        assert [item for item in calls if item[0] == "1d"] == [("1d", now - timedelta(days=400))]

        monotonic[0] += 24 * 60 * 60 + 1
        await coordinator._backfill_symbol("CRCL:USD")
    finally:
        await coordinator.graph.close()

    assert [item for item in calls if item[0] == "1d"] == [
        ("1d", now - timedelta(days=400)),
        ("1d", now - timedelta(days=400)),
    ]


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


@pytest.mark.asyncio
async def test_maintenance_prunes_inactive_memory_history_with_supplied_clock() -> None:
    archived_at = datetime(2025, 1, 1, tzinfo=UTC)
    history = HistoryCache()
    history.load([PricePoint("ARCHIVED:USD", archived_at, Decimal("1"), "fixture", interval="1d")])
    coordinator = object.__new__(MarketDataCoordinator)
    coordinator.service = SimpleNamespace(history=history, _storage=None)

    await coordinator._run_maintenance(archived_at + timedelta(days=401))

    assert history.sizes() == {}


@pytest.mark.asyncio
async def test_restarted_rest_schedulers_preserve_unchanged_due_times() -> None:
    registry = _large_registry(2, history_enabled=True)
    coordinator = MarketDataCoordinator(
        SimpleNamespace(),
        Settings(background_enabled=False),
        registry,
    )
    future = asyncio.get_running_loop().time() + 60

    quote_calls: list[str] = []
    quote_called = asyncio.Event()

    async def poll_quote(symbol: str) -> float:
        quote_calls.append(symbol)
        quote_called.set()
        return 60

    coordinator._quote_next_refresh_at = {"ASSET0:USD": future}
    coordinator._poll_quote_once = poll_quote
    quote_task = asyncio.create_task(coordinator._quote_scheduler_loop())
    try:
        async with asyncio.timeout(1):
            await quote_called.wait()
        await asyncio.sleep(0)
    finally:
        quote_task.cancel()
        await asyncio.gather(quote_task, return_exceptions=True)
    assert quote_calls == ["ASSET1:USD"]

    metadata_calls: list[str] = []
    metadata_called = asyncio.Event()

    async def refresh_metadata(instrument) -> bool:
        metadata_calls.append(instrument.symbol)
        metadata_called.set()
        return False

    coordinator._metadata_next_refresh_at = {"ASSET0:USD": future}
    coordinator._refresh_metadata = refresh_metadata
    metadata_task = asyncio.create_task(coordinator._metadata_loop())
    try:
        async with asyncio.timeout(1):
            await metadata_called.wait()
        await asyncio.sleep(0)
    finally:
        metadata_task.cancel()
        await asyncio.gather(metadata_task, return_exceptions=True)
    assert metadata_calls == ["ASSET1:USD"]

    history_due: list[set[str] | None] = []
    history_called = asyncio.Event()

    async def backfill_history(*, include_fx: bool) -> bool:
        assert include_fx is False
        history_due.append(coordinator._history_due_symbols)
        history_called.set()
        return True

    coordinator._history_next_regular_refresh_at = {"ASSET0:USD": future}
    coordinator._next_fx_history_refresh_at = future
    coordinator._backfill_history = backfill_history
    history_task = asyncio.create_task(coordinator._history_loop())
    try:
        async with asyncio.timeout(1):
            await history_called.wait()
        await asyncio.sleep(0)
    finally:
        history_task.cancel()
        await asyncio.gather(history_task, return_exceptions=True)
        await coordinator.graph.close()
    assert history_due == [{"ASSET1:USD"}]
