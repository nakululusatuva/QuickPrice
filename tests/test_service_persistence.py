from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quickprice.analytics import boxx_yield
from quickprice.config import Settings
from quickprice.domain import (
    DividendEvent,
    PricePoint,
    ProviderQuote,
    QuoteSnapshot,
    SourceComponent,
    YieldMetric,
    utc_now,
)
from quickprice.service import DataUnavailableError, QuickPriceService
from quickprice.storage import (
    DividendEventRecord,
    LatestSnapshotRecord,
    MinutePriceRecord,
    ProviderCheckpointRecord,
    SQLiteStorage,
    YieldMetricRecord,
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


def test_yield_route_change_replaces_newer_observation_but_same_route_does_not(settings):
    now = utc_now()
    service = QuickPriceService(settings)
    service.publish_quote(
        ProviderQuote("BOXX:USD", Decimal("110"), now, "fixture", "iex"),
        persist=False,
    )
    service.publish_yield_metric(
        YieldMetric("BOXX:USD", Decimal("4.2"), now, "DGS3MO", "primary"),
        persist=False,
    )
    service.publish_yield_metric(
        YieldMetric(
            "BOXX:USD",
            Decimal("4.1"),
            now - timedelta(hours=1),
            "cached_treasury_proxy",
            "fallback",
            fallback_level=1,
        ),
        persist=False,
    )

    selected = service.get_quote("BOXX:USD", now=now).estimated_annual_yield
    assert selected is not None
    assert selected.provider == "fallback"
    assert selected.fallback_level == 1

    service.publish_yield_metric(
        YieldMetric(
            "BOXX:USD",
            Decimal("1.0"),
            now - timedelta(hours=2),
            "cached_treasury_proxy",
            "fallback",
            fallback_level=1,
        ),
        persist=False,
    )
    unchanged = service.get_quote("BOXX:USD", now=now).estimated_annual_yield
    assert unchanged is not None
    assert unchanged.percent == selected.percent


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
    service.publish_yield_metric(YieldMetric("BOXX:USD", Decimal("4.2"), now, "DGS3MO", "primary"))
    service.publish_yield_metric(
        YieldMetric(
            "BOXX:USD",
            Decimal("4.1"),
            now - timedelta(days=1),
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
