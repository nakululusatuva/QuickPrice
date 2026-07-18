from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

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
)
from quickprice.storage import (
    SCHEMA_VERSION,
    AggregatePriceRecord,
    DividendEventRecord,
    LatestSnapshotRecord,
    MinutePriceRecord,
    ProviderCheckpointRecord,
    SQLiteStorage,
    StorageBatchError,
    YieldMetricRecord,
)


def run(coroutine):
    return asyncio.run(coroutine)


def minute(symbol: str, timestamp: datetime, index: int = 0) -> MinutePriceRecord:
    return MinutePriceRecord(
        symbol=symbol,
        timestamp=timestamp,
        price=Decimal("100") + Decimal(index) / Decimal("100"),
        provider="fixture",
        source={"sequence": index},
    )


def aggregate(
    symbol: str,
    timestamp: datetime,
    index: int = 0,
    *,
    interval_seconds: int = 300,
) -> AggregatePriceRecord:
    base = Decimal("100") + Decimal(index)
    return AggregatePriceRecord(
        symbol=symbol,
        bucket_start=timestamp,
        interval_seconds=interval_seconds,
        open=base,
        high=base + 2,
        low=base - 1,
        close=base + 1,
        sample_count=5,
        provider="fixture",
    )


def test_schema_and_required_pragmas(tmp_path) -> None:
    database = tmp_path / "quickprice.db"
    storage = SQLiteStorage(database)
    storage.initialize()

    assert storage.schema_version == SCHEMA_VERSION
    connection = storage._connect()  # Verify the exact production connection policy.
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert {
        "schema_version",
        "minute_prices",
        "aggregate_prices",
        "latest_snapshots",
        "dividend_events",
        "yield_metrics",
        "provider_checkpoints",
    } <= tables


def test_concurrent_enqueue_is_batched_and_recovers_after_restart(tmp_path) -> None:
    async def scenario() -> None:
        database = tmp_path / "quickprice.db"
        now = datetime(2026, 7, 20, 6, tzinfo=UTC)
        storage = SQLiteStorage(database, batch_size=100, batch_interval=0.02)
        await storage.start()

        commands = [
            minute("BTC:USDC", now - timedelta(minutes=499 - index), index) for index in range(500)
        ]
        await asyncio.gather(*(storage.enqueue(item) for item in commands))
        await storage.flush()
        metrics = storage.metrics()
        assert metrics.records_committed == 500
        assert 1 <= metrics.batches_committed <= 10
        assert metrics.commit_failures == 0
        await storage.stop()

        restarted = SQLiteStorage(database)
        restored = await restarted.restore(now=now, minute_retention=timedelta(days=1))
        assert len(restored.minute_prices) == 500
        assert restored.minute_prices[0].timestamp < restored.minute_prices[-1].timestamp
        assert restored.minute_prices[-1].price == Decimal("104.99")
        assert await restarted.integrity_check() == "ok"

    run(scenario())


def test_staking_yield_metadata_survives_sqlite_restart(tmp_path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, 6, tzinfo=UTC)
        storage = SQLiteStorage(tmp_path / "quickprice.db", batch_interval=0.01)
        await storage.start()
        index = AccrualIndexPoint(
            "WBETH:ETH",
            "ETH",
            Decimal("1.1"),
            now,
            "ethereum_exchange_rate",
        )
        metric = YieldMetric(
            symbol="WBETH:USDC",
            value=Decimal("3.2"),
            as_of=now,
            method="onchain_exchange_rate_trailing_apy",
            provider="ethereum_exchange_rate",
            components=(
                SourceComponent(
                    "WBETH:ETH",
                    "ethereum_exchange_rate",
                    Decimal("1.1"),
                    now,
                    "ethereum_mainnet",
                    "current_exchange_rate",
                ),
            ),
            rate_type=YieldRateType.APY,
            observation_window_days=Decimal("7"),
            accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
            underlying_asset="ETH",
            is_estimate=True,
            accrual_index=index,
            quality=YieldQuality(staleness_ms=500, confidence="high"),
            fallback_level=2,
        )
        await storage.enqueue_yield(metric)
        await storage.flush()
        await storage.stop()

        restored = await SQLiteStorage(tmp_path / "quickprice.db").restore(now=now)
        result = restored.yield_metrics[0]
        assert result.rate_type is YieldRateType.APY
        assert result.observation_window_days == Decimal("7")
        assert result.accrual_mode is RewardAccrualMode.VALUE_ACCRUING
        assert result.underlying_asset == "ETH"
        assert result.is_estimate is True
        assert result.accrual_index == index
        assert result.quality == YieldQuality(staleness_ms=500, confidence="high")
        assert result.components == metric.components
        assert result.fallback_level == 2

    run(scenario())


def test_restore_windows_latest_state_and_cleanup(tmp_path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, 6, tzinfo=UTC)
        storage = SQLiteStorage(tmp_path / "quickprice.db", batch_interval=0.01)
        await storage.start()
        records = [
            minute("BTC:USDC", now - timedelta(hours=49), 1),
            minute("BTC:USDC", now - timedelta(hours=47), 2),
            aggregate("BTC:USDC", now - timedelta(days=46), 1),
            aggregate("BTC:USDC", now - timedelta(days=44), 2),
            aggregate(
                "BTC:USDC",
                now - timedelta(days=401),
                3,
                interval_seconds=86_400,
            ),
            aggregate(
                "BTC:USDC",
                now - timedelta(days=399),
                4,
                interval_seconds=86_400,
            ),
            LatestSnapshotRecord(
                symbol="BTC:USDC",
                as_of=now,
                price=Decimal("101.25"),
                payload={"symbol": "BTC:USDC", "price": Decimal("101.25")},
            ),
            DividendEventRecord(
                symbol="SGOV:USD",
                ex_date=date(2026, 7, 1),
                payment_date=date(2026, 7, 7),
                amount=Decimal("0.35"),
                currency="USD",
                frequency="monthly",
                provider="fixture",
            ),
            YieldMetricRecord(
                symbol="BOXX:USD",
                as_of=now - timedelta(days=1),
                annual_percent=Decimal("4.1"),
                method="treasury_3m_proxy_minus_expense",
                provider="fred",
                is_proxy=True,
                source_series="DGS3MO",
            ),
            YieldMetricRecord(
                symbol="BOXX:USD",
                as_of=now,
                annual_percent=Decimal("4.2"),
                method="treasury_3m_proxy_minus_expense",
                provider="fred",
                is_proxy=True,
                source_series="DGS3MO",
            ),
            ProviderCheckpointRecord(
                provider="fixture",
                feed="bars",
                updated_at=now,
                checkpoint={"cursor": "abc"},
            ),
        ]
        await storage.enqueue_many(records)
        await storage.flush()

        restored = await storage.restore(now=now)
        assert [item.timestamp for item in restored.minute_prices] == [now - timedelta(hours=47)]
        assert [item.bucket_start for item in restored.aggregate_prices] == [
            now - timedelta(days=399),
            now - timedelta(days=44),
        ]
        assert restored.snapshots_by_symbol["BTC:USDC"].payload["price"] == "101.25"
        assert len(restored.dividend_events) == 1
        assert [item.annual_percent for item in restored.yield_metric_records] == [Decimal("4.2")]
        assert [item.value for item in restored.yield_metrics] == [Decimal("4.2")]
        assert restored.checkpoints_by_key[("fixture", "bars")].checkpoint == {"cursor": "abc"}

        cleanup = await storage.cleanup(now=now)
        assert cleanup.minute_prices_deleted == 1
        assert cleanup.aggregate_prices_deleted == 2
        connection = sqlite3.connect(storage.path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM minute_prices").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM aggregate_prices").fetchone()[0] == 2
        finally:
            connection.close()
        await storage.stop()

    run(scenario())


def test_yield_restore_and_metadata_cleanup_follow_last_publication_order(tmp_path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, 6, tzinfo=UTC)
        storage = SQLiteStorage(tmp_path / "quickprice.db", batch_interval_ms=10)
        await storage.start()
        await storage.enqueue_yield(
            YieldMetric(
                "BOXX:USD",
                Decimal("4.2"),
                now,
                "primary_method",
                "primary",
                fallback_level=0,
            ),
            wait=True,
        )
        await storage.enqueue_yield(
            YieldMetric(
                "BOXX:USD",
                Decimal("4.1"),
                now - timedelta(days=1),
                "fallback_method",
                "fallback",
                fallback_level=1,
            ),
            wait=True,
        )
        for index in range(3):
            await storage.enqueue_dividend(
                DividendEvent(
                    "SGOV:USD",
                    date(2026, 5 + index, 1),
                    None,
                    Decimal("0.3") + Decimal(index) / Decimal("100"),
                    "USD",
                    "monthly",
                    "fixture",
                ),
                wait=True,
            )

        restored = await storage.restore(now=now)
        assert len(restored.yield_metrics) == 1
        assert restored.yield_metrics[0].provider == "fallback"
        assert restored.yield_metrics[0].fallback_level == 1
        assert len(restored.dividends) == 1
        assert restored.dividends[0].ex_date == date(2026, 7, 1)

        cleanup = await storage.cleanup(now=now)
        assert cleanup.yield_metrics_deleted == 1
        assert cleanup.dividend_events_deleted == 2
        connection = sqlite3.connect(storage.path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM yield_metrics").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM dividend_events").fetchone()[0] == 1
        finally:
            connection.close()
        await storage.stop()

    run(scenario())


def test_checkpoint_and_wal_metrics(tmp_path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, 6, tzinfo=UTC)
        storage = SQLiteStorage(tmp_path / "quickprice.db", batch_interval=0.01)
        await storage.start()
        await storage.enqueue_many(
            [minute("ETH:USDC", now + timedelta(minutes=index), index) for index in range(50)]
        )
        await storage.flush()
        before = storage.metrics()
        assert before.wal_bytes > 0
        result = await storage.checkpoint("TRUNCATE")
        after = storage.metrics()
        assert result.busy == 0
        assert result.mode == "TRUNCATE"
        assert after.last_checkpoint == result
        assert after.wal_bytes <= before.wal_bytes
        await storage.stop()

    run(scenario())


def test_injected_commit_failure_rolls_back_and_writer_recovers(tmp_path) -> None:
    injected = False

    def fail_once(phase, commands) -> None:
        nonlocal injected
        if phase == "before_commit" and not injected:
            injected = True
            raise OSError("simulated disk full")

    async def scenario() -> None:
        now = datetime(2026, 7, 20, 6, tzinfo=UTC)
        storage = SQLiteStorage(
            tmp_path / "quickprice.db",
            batch_interval=0.01,
            fault_injector=fail_once,
        )
        await storage.start()
        with pytest.raises(StorageBatchError, match="rolled back"):
            await storage.enqueue(minute("BTC:USDC", now, 1), wait=True)

        await storage.enqueue(minute("ETH:USDC", now, 2), wait=True)
        restored = await storage.restore(now=now)
        assert [item.symbol for item in restored.minute_prices] == ["ETH:USDC"]
        metrics = storage.metrics()
        assert metrics.commit_failures == 1
        assert metrics.records_committed == 1
        await storage.stop()

    run(scenario())


def test_fire_and_forget_commit_failure_is_reported_on_shutdown(tmp_path) -> None:
    def fail_commit(phase, commands) -> None:
        if phase == "before_commit":
            raise OSError("simulated disk full")

    async def scenario() -> None:
        now = datetime(2026, 7, 20, 6, tzinfo=UTC)
        storage = SQLiteStorage(
            tmp_path / "quickprice.db",
            batch_interval=0.01,
            fault_injector=fail_commit,
        )
        await storage.start()
        await storage.enqueue(minute("BTC:USDC", now, 1))
        with pytest.raises(StorageBatchError, match="before shutdown"):
            await storage.stop()
        assert storage.is_running is False

    run(scenario())


def test_out_of_order_latest_records_do_not_regress(tmp_path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, 6, tzinfo=UTC)
        storage = SQLiteStorage(tmp_path / "quickprice.db", batch_interval=0.01)
        await storage.start()
        await storage.enqueue(
            LatestSnapshotRecord("BTC:USDC", now, {"version": "new"}, Decimal("2")),
            wait=True,
        )
        outcome = await storage.enqueue(
            LatestSnapshotRecord(
                "BTC:USDC",
                now - timedelta(seconds=1),
                {"version": "old"},
                Decimal("1"),
            ),
            wait=True,
        )
        assert outcome.rows_affected == 0

        await storage.enqueue(
            ProviderCheckpointRecord("binance", "trades", now, {"sequence": 20}),
            wait=True,
        )
        stale = await storage.enqueue(
            ProviderCheckpointRecord(
                "binance", "trades", now - timedelta(seconds=1), {"sequence": 10}
            ),
            wait=True,
        )
        assert stale.rows_affected == 0
        restored = await storage.restore(now=now)
        assert restored.snapshots_by_symbol["BTC:USDC"].payload == {"version": "new"}
        assert restored.checkpoints_by_key[("binance", "trades")].checkpoint == {"sequence": 20}
        await storage.stop()

    run(scenario())


def test_domain_models_round_trip_through_service_aliases(tmp_path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 20, 6, tzinfo=UTC)
        storage = SQLiteStorage(tmp_path / "quickprice.db", batch_interval_ms=10)
        await storage.start()
        quote = ProviderQuote(
            symbol="BOXX:USD",
            price=Decimal("115.42"),
            as_of=now,
            provider="alpaca",
            feed="iex",
            market_status="closed",
            coverage="single_venue",
        )
        await storage.enqueue_price(PricePoint("BOXX:USD", now, quote.price, "alpaca"), wait=True)
        await storage.enqueue_snapshot(QuoteSnapshot(quote=quote, changes={}), wait=True)
        await storage.enqueue_dividend(
            DividendEvent(
                symbol="SGOV:USD",
                ex_date=date(2026, 7, 1),
                payment_date=date(2026, 7, 7),
                amount=Decimal("0.35"),
                currency="USD",
                frequency="monthly",
                provider="alpaca",
                declared_date=date(2026, 6, 25),
            ),
            wait=True,
        )
        await storage.enqueue_yield(
            YieldMetric(
                symbol="BOXX:USD",
                value=Decimal("4.12"),
                as_of=now,
                method="treasury_3m_proxy_minus_expense",
                provider="fred",
                is_proxy=True,
            ),
            wait=True,
        )

        restored = await storage.restore(now=now)
        assert restored.price_points[0] == PricePoint("BOXX:USD", now, Decimal("115.42"), "alpaca")
        assert restored.quotes == (quote,)
        assert restored.dividends[0].declared_date == date(2026, 6, 25)
        assert restored.yield_metrics[0].value == Decimal("4.12")
        await storage.stop()

    run(scenario())
