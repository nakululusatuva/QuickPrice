from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quickprice.analytics import boxx_yield
from quickprice.config import Settings
from quickprice.domain import (
    AccrualIndexPoint,
    DividendEvent,
    PricePoint,
    ProviderQuote,
    QuoteSnapshot,
    RewardAccrualMode,
    SourceComponent,
    YieldMetric,
    YieldQuality,
    YieldRateType,
    utc_now,
)
from quickprice.service import DataUnavailableError, QuickPriceService
from quickprice.staking import (
    ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS,
    ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD,
)
from quickprice.storage import (
    DividendEventRecord,
    LatestSnapshotRecord,
    MinutePriceRecord,
    ProviderCheckpointRecord,
    SQLiteStorage,
    YieldMetricRecord,
)


def _wbeth_primary_metric(
    as_of: datetime,
    *,
    value: str = "2.4187",
) -> YieldMetric:
    return YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal(value),
        as_of=as_of,
        method=ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD,
        provider="ethereum_exchange_rate",
        rate_type=YieldRateType.APY,
        observation_window_days=Decimal("7"),
        accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
        underlying_asset="ETH",
        is_estimate=True,
        accrual_index=AccrualIndexPoint(
            symbol="WBETH:ETH",
            underlying_asset="ETH",
            value=Decimal("1.072"),
            as_of=as_of,
            provider="ethereum_exchange_rate",
            kind="protocol_exchange_rate",
        ),
        quality=YieldQuality(stale=False, confidence="high"),
        fallback_level=0,
    )


def _wbeth_binance_metric(
    as_of: datetime,
    *,
    value: str = "2.3",
) -> YieldMetric:
    return YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal(value),
        as_of=as_of,
        method="binance_wbeth_rate_history_apr",
        provider="binance_wbeth_rate",
        rate_type=YieldRateType.APR,
        accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
        underlying_asset="ETH",
        is_estimate=False,
        accrual_index=AccrualIndexPoint(
            symbol="WBETH:ETH",
            underlying_asset="ETH",
            value=Decimal("1.072"),
            as_of=as_of,
            provider="binance_wbeth_rate",
            kind="vendor_exchange_rate",
        ),
        quality=YieldQuality(stale=False, confidence="high"),
        fallback_level=0,
    )


def _wbeth_proxy_metric(
    as_of: datetime,
    *,
    fallback_level: int = 2,
) -> YieldMetric:
    return YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal("3.1"),
        as_of=as_of,
        method="staking_market_ratio_30d_annualized",
        provider="staking_market_ratio_proxy",
        is_proxy=True,
        rate_type=YieldRateType.APY,
        observation_window_days=Decimal("30"),
        accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
        underlying_asset="ETH",
        is_estimate=True,
        accrual_index=AccrualIndexPoint(
            symbol="WBETH:ETH",
            underlying_asset="ETH",
            value=Decimal("1.07"),
            as_of=as_of,
            provider="staking_market_ratio_proxy",
            kind="market_price_ratio",
        ),
        quality=YieldQuality(stale=False, confidence="low"),
        fallback_level=fallback_level,
    )


@pytest.mark.asyncio
async def test_service_persists_and_restores_hot_snapshot(tmp_path):
    settings = Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "service.db",
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
    )
    now = utc_now()
    service = QuickPriceService(settings)
    await service.start()
    service.publish_history(
        [PricePoint("QQQM:USD", now - timedelta(hours=2), Decimal("240"), "fixture")]
    )
    service.publish_dividend(
        DividendEvent(
            "QQQM:USD",
            date.today() - timedelta(days=20),
            date.today() - timedelta(days=15),
            Decimal("0.35"),
            "USD",
            "quarterly",
            "fixture",
        )
    )
    service.publish_quote(ProviderQuote("QQQM:USD", Decimal("250"), now, "fixture", "iex"))
    await service.stop()

    restored = QuickPriceService(settings)
    await restored.start()
    quote = restored.get_quote("QQQM:USD", now=now + timedelta(seconds=1))
    assert quote.price == 250.0
    assert quote.dividend is not None
    assert quote.dividend.amount == 0.35
    assert restored.history.points("QQQM:USD")
    await restored.stop()


@pytest.mark.asyncio
async def test_high_frequency_quotes_persist_one_minute_bucket(tmp_path):
    settings = Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "service.db",
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
        sqlite_batch_ms=10,
    )
    service = QuickPriceService(settings)
    await service.start()
    now = utc_now().replace(second=1, microsecond=0)
    for second in range(1, 60):
        service.publish_quote(
            ProviderQuote(
                "BTC:USDC",
                Decimal(100 + second),
                now.replace(second=second),
                "fixture",
                "stream",
            )
        )
    await service.stop()

    from quickprice.storage import SQLiteStorage

    storage = SQLiteStorage(settings.database_path, batch_interval=0.01)
    restored = await storage.restore(now=now + timedelta(minutes=1))
    btc = [item for item in restored.minute_prices if item.symbol == "BTC:USDC"]
    assert len(btc) == 1
    assert btc[0].timestamp.second == 0
    assert btc[0].price == Decimal("159")


@pytest.mark.asyncio
async def test_daily_fallback_history_persists_and_restores(tmp_path):
    settings = Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "service.db",
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
        sqlite_batch_ms=10,
    )
    now = utc_now().replace(microsecond=0)
    service = QuickPriceService(settings)
    await service.start()
    service.publish_history(
        [
            PricePoint(
                "QQQM:USD",
                now - timedelta(days=30),
                Decimal("220"),
                "alpha_vantage",
                interval="1d",
            ),
            PricePoint(
                "BTC:USDC",
                now - timedelta(days=366),
                Decimal("80"),
                "fixture",
                interval="1d",
            ),
        ]
    )
    service.publish_quote(ProviderQuote("BTC:USDC", Decimal("100"), now, "fixture", "fixture"))
    await service.stop()

    restored = QuickPriceService(settings)
    await restored.start()
    points = restored.history.points("QQQM:USD")
    assert len(points) == 1
    assert points[0].interval == "1d"
    assert points[0].provider == "alpha_vantage"
    btc = restored.get_quote("BTC:USDC", now=now)
    assert btc.changes["1y"] is not None
    assert btc.changes["1y"].percent == 25.0
    await restored.stop()


def test_expired_primary_accepts_proxy_and_recovered_primary_wins(
    settings,
    monkeypatch,
):
    now = datetime(2026, 7, 20, 16, tzinfo=UTC)
    monkeypatch.setattr("quickprice.service.utc_now", lambda: now)
    service = QuickPriceService(settings)
    service.publish_quote(
        ProviderQuote("WBETH:USDC", Decimal("3500"), now, "fixture", "fixture"),
        persist=False,
    )
    service.publish_yield_metric(
        _wbeth_primary_metric(now - timedelta(seconds=ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS + 1)),
        persist=False,
    )
    service.publish_yield_metric(
        _wbeth_proxy_metric(now, fallback_level=0),
        persist=False,
    )

    selected = service.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert selected is not None
    assert selected.provider == "staking_market_ratio_proxy"
    assert selected.fallback_level == 0

    service.publish_yield_metric(
        _wbeth_primary_metric(now - timedelta(hours=1), value="2.35"),
        persist=False,
    )
    recovered = service.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert recovered is not None
    assert recovered.provider == "ethereum_exchange_rate"
    assert recovered.fallback_level == 0
    assert recovered.percent == 2.35

    service.publish_yield_metric(
        _wbeth_primary_metric(now - timedelta(hours=2), value="1.0"),
        persist=False,
    )
    unchanged = service.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert unchanged is not None
    assert unchanged.percent == recovered.percent

    service.publish_yield_metric(
        _wbeth_primary_metric(now, value="2.4"),
        persist=False,
    )
    newer = service.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert newer is not None
    assert newer.percent == 2.4


@pytest.mark.asyncio
async def test_route_upgrade_prefers_older_provider_reported_rate_after_restart(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "service.db"
    base_settings = dict(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=database_path,
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
        sqlite_batch_ms=10,
    )
    without_binance = Settings(**base_settings)
    with_binance = Settings(
        **base_settings,
        binance_api_key="read-only-key",
        binance_api_secret="signing-secret",
    )
    now = datetime(2026, 7, 20, 16, tzinfo=UTC)
    monkeypatch.setattr("quickprice.service.utc_now", lambda: now)

    service = QuickPriceService(without_binance)
    await service.start()
    service.publish_quote(ProviderQuote("WBETH:USDC", Decimal("3500"), now, "fixture", "fixture"))
    service.publish_yield_metric(_wbeth_primary_metric(now - timedelta(hours=1)))
    await service.stop()

    upgraded = QuickPriceService(with_binance)
    await upgraded.start()
    restored = upgraded.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert restored is not None
    assert restored.provider == "ethereum_exchange_rate"
    assert restored.fallback_level == 0

    upgraded.publish_yield_metric(_wbeth_binance_metric(now - timedelta(hours=2)))
    selected = upgraded.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert selected is not None
    assert selected.provider == "binance_wbeth_rate"
    assert selected.percent == 2.3
    assert selected.fallback_level == 0
    await upgraded.stop()

    restarted = QuickPriceService(with_binance)
    await restarted.start()
    persisted = restarted.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert persisted is not None
    assert persisted.provider == "binance_wbeth_rate"
    await restarted.stop()


@pytest.mark.asyncio
async def test_key_removal_retains_fresh_reported_rate_then_accepts_expired_estimate(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "service.db"
    base_settings = dict(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=database_path,
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
        sqlite_batch_ms=10,
    )
    with_binance = Settings(
        **base_settings,
        binance_api_key="read-only-key",
        binance_api_secret="signing-secret",
    )
    without_binance = Settings(**base_settings)
    clock = [datetime(2026, 7, 20, 16, tzinfo=UTC)]
    monkeypatch.setattr("quickprice.service.utc_now", lambda: clock[0])
    reported_as_of = clock[0] - timedelta(hours=1)

    service = QuickPriceService(with_binance)
    await service.start()
    service.publish_quote(
        ProviderQuote("WBETH:USDC", Decimal("3500"), clock[0], "fixture", "fixture")
    )
    service.publish_yield_metric(_wbeth_binance_metric(reported_as_of))
    await service.stop()

    downgraded = QuickPriceService(without_binance)
    await downgraded.start()
    downgraded.publish_yield_metric(_wbeth_primary_metric(clock[0]))
    retained = downgraded.get_quote("WBETH:USDC", now=clock[0]).estimated_annual_yield
    assert retained is not None and retained.quality is not None
    assert retained.provider == "binance_wbeth_rate"

    clock[0] = reported_as_of + timedelta(seconds=retained.quality.stale_after_seconds + 1)
    downgraded.publish_yield_metric(_wbeth_primary_metric(clock[0]))
    accepted = downgraded.get_quote("WBETH:USDC", now=clock[0]).estimated_annual_yield
    assert accepted is not None
    assert accepted.provider == "ethereum_exchange_rate"
    await downgraded.stop()


@pytest.mark.asyncio
async def test_effective_fallback_yield_and_freshness_policy_survive_restart(tmp_path):
    settings = Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "service.db",
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
        sqlite_batch_ms=10,
    )
    now = utc_now().replace(microsecond=0)
    service = QuickPriceService(settings)
    await service.start()
    service.publish_quote(ProviderQuote("BOXX:USD", Decimal("110"), now, "fixture", "iex"))
    service.publish_yield_metric(
        YieldMetric(
            "BOXX:USD",
            Decimal("4.2"),
            now - timedelta(days=8),
            "DGS3MO",
            "primary",
        )
    )
    service.publish_yield_metric(
        YieldMetric(
            "BOXX:USD",
            Decimal("4.1"),
            now,
            "cached_treasury_proxy",
            "fallback",
            fallback_level=1,
        )
    )
    await service.stop()

    restored = QuickPriceService(settings)
    await restored.start()
    selected = restored.get_quote("BOXX:USD", now=now).estimated_annual_yield
    assert selected is not None
    assert selected.provider == "fallback"
    assert selected.fallback_level == 1
    assert selected.quality is not None
    threshold = selected.quality.stale_after_seconds
    assert threshold == settings.metadata_poll_seconds * 2

    advanced = restored.get_quote(
        "BOXX:USD",
        now=selected.as_of + timedelta(seconds=threshold + 1),
    ).estimated_annual_yield
    assert advanced is not None and advanced.quality is not None
    assert advanced.quality.stale is True
    assert advanced.quality.staleness_ms == int((threshold + 1) * 1000)
    await restored.stop()


@pytest.mark.asyncio
async def test_daily_onchain_yield_freshness_policy_survives_restart(tmp_path):
    settings = Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "service.db",
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
        metadata_poll_seconds=21_600,
        sqlite_batch_ms=10,
    )
    now = datetime(2026, 7, 20, 16, tzinfo=UTC)
    event_as_of = now - timedelta(hours=16)
    service = QuickPriceService(settings)
    await service.start()
    service.publish_quote(ProviderQuote("WBETH:USDC", Decimal("3500"), now, "fixture", "fixture"))
    service.publish_yield_metric(
        YieldMetric(
            symbol="WBETH:USDC",
            value=Decimal("2.4187"),
            as_of=event_as_of,
            method=ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD,
            provider="ethereum_exchange_rate",
            rate_type=YieldRateType.APY,
            observation_window_days=Decimal("7"),
            accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
            underlying_asset="ETH",
            is_estimate=True,
            accrual_index=AccrualIndexPoint(
                symbol="WBETH:ETH",
                underlying_asset="ETH",
                value=Decimal("1.072"),
                as_of=event_as_of,
                provider="ethereum_exchange_rate",
                kind="protocol_exchange_rate",
            ),
            quality=YieldQuality(
                stale=False,
                staleness_ms=16 * 60 * 60 * 1000,
                confidence="high",
            ),
        )
    )
    await service.stop()

    restored = QuickPriceService(settings)
    await restored.start()
    selected = restored.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert selected is not None and selected.quality is not None
    assert selected.quality.stale_after_seconds == ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS
    assert selected.quality.staleness_ms == 16 * 60 * 60 * 1000
    assert selected.quality.stale is False

    expired = restored.get_quote(
        "WBETH:USDC",
        now=event_as_of + timedelta(seconds=ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS + 1),
    ).estimated_annual_yield
    assert expired is not None and expired.quality is not None
    assert expired.quality.stale is True
    assert expired.quality.staleness_ms == (ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS + 1) * 1000
    await restored.stop()


@pytest.mark.asyncio
async def test_legacy_onchain_sla_migrates_and_fresh_primary_rejects_proxy(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "service.db",
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
        metadata_poll_seconds=21_600,
        sqlite_batch_ms=10,
    )
    now = datetime(2026, 7, 20, 16, tzinfo=UTC)
    monkeypatch.setattr("quickprice.service.utc_now", lambda: now)
    primary = _wbeth_primary_metric(now - timedelta(hours=16))
    quote = ProviderQuote(
        "WBETH:USDC",
        Decimal("3500"),
        now,
        "fixture",
        "fixture",
    )
    record = YieldMetricRecord.from_domain(primary)
    legacy_record = replace(
        record,
        raw={**record.raw, "stale_after_seconds": 12 * 60 * 60},
    )
    storage = SQLiteStorage(settings.database_path, batch_interval_ms=10)
    await storage.start()
    await storage.enqueue_snapshot(QuoteSnapshot(quote, {}), wait=True)
    await storage.enqueue_yield(legacy_record, wait=True)
    await storage.stop()

    service = QuickPriceService(settings)
    await service.start()
    selected = service.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert selected is not None and selected.quality is not None
    assert selected.provider == "ethereum_exchange_rate"
    assert selected.quality.stale_after_seconds == ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS
    assert selected.quality.stale is False

    service.publish_yield_metric(_wbeth_proxy_metric(now, fallback_level=0))
    retained = service.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert retained is not None
    assert retained.provider == "ethereum_exchange_rate"
    assert retained.fallback_level == 0
    await service.stop()

    restarted = QuickPriceService(settings)
    await restarted.start()
    persisted = restarted.get_quote("WBETH:USDC", now=now).estimated_annual_yield
    assert persisted is not None
    assert persisted.provider == "ethereum_exchange_rate"
    assert persisted.fallback_level == 0
    await restarted.stop()


@pytest.mark.asyncio
async def test_disabled_plugin_records_are_filtered_before_domain_restore(tmp_path):
    settings = Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "service.db",
        api_key_hashes=("sha256:" + "1" * 64,),
        rate_limit_enabled=False,
        sqlite_batch_ms=10,
    )
    now = utc_now().replace(microsecond=0)
    storage = SQLiteStorage(settings.database_path, batch_interval_ms=10)
    await storage.start()
    active_quote = ProviderQuote("BTC:USDC", Decimal("100"), now, "fixture", "fixture")
    await storage.enqueue_many(
        (
            MinutePriceRecord.from_domain(PricePoint("BTC:USDC", now, Decimal("100"), "fixture")),
            MinutePriceRecord.from_domain(PricePoint("REMOVED:USD", now, Decimal("7"), "removed")),
            LatestSnapshotRecord.from_domain(QuoteSnapshot(active_quote, {})),
            LatestSnapshotRecord(
                "REMOVED:USD",
                now,
                {"quote": {"symbol": "REMOVED:USD", "price": "7"}},
                Decimal("7"),
            ),
            DividendEventRecord(
                symbol="REMOVED:USD",
                ex_date=date.today(),
                payment_date=None,
                amount=Decimal("1"),
                currency="USD",
                frequency="annual",
                provider="removed",
                raw={"declared_date": "not-a-date"},
            ),
            YieldMetricRecord(
                "REMOVED:USD",
                now,
                Decimal("3"),
                "removed_method",
                "removed",
                raw={"accrual_index": {"invalid": True}},
            ),
            ProviderCheckpointRecord(
                "fixture",
                "quotes",
                now,
                {"symbols": {"BTC:USDC": "active", "REMOVED:USD": "removed"}},
            ),
            ProviderCheckpointRecord(
                "removed",
                "quotes",
                now,
                {"symbols": {"REMOVED:USD": "removed"}},
            ),
        )
    )
    await storage.flush()
    await storage.stop()

    service = QuickPriceService(settings)
    await service.start()
    assert service._storage_ready is True
    assert service.get_quote("BTC:USDC", now=now).price == 100.0
    assert all(point.symbol != "REMOVED:USD" for point in service.history.points("REMOVED:USD"))
    assert "REMOVED:USD" not in service._dividends
    assert "REMOVED:USD" not in service._yield_metrics
    checkpoints = service.restored_provider_checkpoints()
    assert checkpoints[("fixture", "quotes")].checkpoint == {"symbols": {"BTC:USDC": "active"}}
    assert ("removed", "quotes") not in checkpoints
    await service.stop()


def test_future_quote_cannot_poison_newer_quote_selection(settings):
    service = QuickPriceService(settings)
    now = utc_now()
    with pytest.raises(ValueError, match="future"):
        service.publish_quote(
            ProviderQuote("BTC:USDC", Decimal("999"), now + timedelta(days=1), "bad", "fixture"),
            persist=False,
        )
    service.publish_quote(
        ProviderQuote("BTC:USDC", Decimal("100"), now, "good", "fixture"),
        persist=False,
    )
    assert service.get_quote("BTC:USDC", now=now).price == 100.0


def test_boxx_provider_derived_metric_is_not_charged_expense_twice():
    as_of = utc_now()
    metric = YieldMetric(
        "BOXX:USD",
        Decimal("4.0551"),
        as_of,
        "treasury_3m_proxy_minus_expense",
        "fred",
        True,
        (
            SourceComponent(
                "DGS3MO",
                "fred",
                Decimal("4.25"),
                as_of,
                "fred_daily",
            ),
        ),
    )
    result = boxx_yield(metric)
    assert result.percent == Decimal("4.0551")
    assert result.inputs["treasury_3m_percent"] == 4.25


def test_required_bond_yield_missing_is_unavailable(settings):
    service = QuickPriceService(settings)
    service.publish_quote(
        ProviderQuote("BOXX:USD", Decimal("110"), utc_now(), "fixture", "iex"),
        persist=False,
    )
    with pytest.raises(DataUnavailableError, match="yield"):
        service.get_quote("BOXX:USD")


def test_hkd_synthetic_freshness_matches_twenty_minute_component_policy(settings):
    now = utc_now()
    service = QuickPriceService(settings)
    service.publish_quote(
        ProviderQuote("HKD:CNH", Decimal("0.92"), now, "synthetic_fx", "components"),
        persist=False,
    )
    assert service.get_quote("HKD:CNH", now=now + timedelta(minutes=19)).quality.stale is False
    assert service.get_quote("HKD:CNH", now=now + timedelta(minutes=21)).quality.stale is True


def test_closed_equity_cache_becomes_stale_at_next_regular_open(settings):
    service = QuickPriceService(settings)
    friday_close = datetime(2026, 7, 17, 20, tzinfo=UTC)
    service.publish_dividend(
        DividendEvent(
            "QQQM:USD",
            date(2026, 6, 23),
            date(2026, 6, 27),
            Decimal("0.32"),
            "USD",
            "quarterly",
            "fixture",
        ),
        persist=False,
    )
    service.publish_quote(
        ProviderQuote(
            "QQQM:USD",
            Decimal("250"),
            friday_close,
            "fixture",
            "iex",
            market_status="closed",
        ),
        persist=False,
    )

    weekend = service.get_quote("QQQM:USD", now=datetime(2026, 7, 19, 16, tzinfo=UTC))
    assert weekend.market_status == "closed"
    assert weekend.quality.stale is False

    monday_open = service.get_quote("QQQM:USD", now=datetime(2026, 7, 20, 14, tzinfo=UTC))
    assert monday_open.market_status == "open"
    assert monday_open.quality.stale is True


@pytest.mark.parametrize(
    ("observed_at", "last_trade"),
    [
        (
            datetime(2026, 11, 26, 17, tzinfo=UTC),
            datetime(2026, 11, 25, 21, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 27, 21, 1, tzinfo=UTC),
            datetime(2026, 11, 27, 18, tzinfo=UTC),
        ),
    ],
)
def test_fresh_provider_clock_preserves_holiday_and_early_close(
    settings, monkeypatch, observed_at, last_trade
):
    monkeypatch.setattr("quickprice.service.utc_now", lambda: observed_at)
    service = QuickPriceService(settings)
    service.publish_dividend(
        DividendEvent(
            "QQQM:USD",
            date(2026, 9, 21),
            date(2026, 9, 25),
            Decimal("0.32"),
            "USD",
            "quarterly",
            "fixture",
        ),
        persist=False,
    )
    service.publish_quote(
        ProviderQuote(
            "QQQM:USD",
            Decimal("250"),
            last_trade,
            "alpaca",
            "iex",
            market_status="closed",
            market_status_as_of=observed_at,
        ),
        persist=False,
    )

    result = service.get_quote("QQQM:USD", now=observed_at)
    assert result.market_status == "closed"
    assert result.quality.stale is False


def test_full_source_failure_marks_cached_quote_stale_until_next_publish(settings):
    service = QuickPriceService(settings)
    now = utc_now()
    service.publish_quote(
        ProviderQuote("BTC:USDC", Decimal("100"), now, "fixture", "fixture"),
        persist=False,
    )
    assert service.get_quote("BTC:USDC", now=now).quality.stale is False

    service.mark_source_failed("BTC:USDC")
    assert service.get_quote("BTC:USDC", now=now).quality.stale is True

    service.publish_quote(
        ProviderQuote("BTC:USDC", Decimal("101"), now, "recovered", "fixture"),
        persist=False,
    )
    assert service.get_quote("BTC:USDC", now=now).quality.stale is False
