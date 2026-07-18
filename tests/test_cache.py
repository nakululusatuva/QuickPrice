from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quickprice.cache import HistoryCache
from quickprice.domain import PricePoint


def test_large_older_backfill_merges_with_live_tail_and_prunes_once() -> None:
    cache = HistoryCache()
    live = datetime(2026, 7, 20, 12, tzinfo=UTC)
    cache.add(PricePoint("BTC:USDC", live, Decimal("120"), "live"))
    points = [
        PricePoint(
            "BTC:USDC",
            live - timedelta(minutes=index),
            Decimal(120 - index / 10_000),
            "history",
            interval="5m",
        )
        for index in range(1, 45 * 24 * 12 + 500)
    ]

    cache.load(points)

    merged = cache.points("BTC:USDC")
    assert merged == tuple(sorted(merged, key=lambda point: point.timestamp))
    assert merged[-1].timestamp == live
    assert merged[-1].provider == "live"
    assert merged[0].timestamp >= live - timedelta(days=45)


def test_daily_fallback_fills_the_old_prefix_without_entering_intraday_rings() -> None:
    cache = HistoryCache()
    now = datetime(2026, 7, 20, 20, tzinfo=UTC)
    cache.load(
        [
            PricePoint(
                "QQQM:USD",
                now - timedelta(days=day),
                Decimal(200 + day),
                "alpha_vantage",
                interval="1d",
            )
            for day in range(1, 41)
        ]
    )
    cache.add(
        PricePoint(
            "QQQM:USD",
            now - timedelta(minutes=5),
            Decimal("250"),
            "alpaca",
            interval="5m",
        )
    )

    points = cache.points("QQQM:USD")
    assert points[0].interval == "1d"
    assert points[-1].interval == "5m"
    assert cache.sizes()["QQQM:USD"] == {"1m": 0, "5m": 1, "1d": 40}


def test_daily_and_five_minute_rings_use_independent_retention_windows() -> None:
    cache = HistoryCache()
    now = datetime(2026, 7, 20, 20, tzinfo=UTC)
    cache.load(
        [
            PricePoint("TEST:USD", now, Decimal("100"), "fixture", interval="1d"),
            PricePoint(
                "TEST:USD",
                now - timedelta(days=399),
                Decimal("80"),
                "fixture",
                interval="1d",
            ),
            PricePoint(
                "TEST:USD",
                now - timedelta(days=401),
                Decimal("70"),
                "fixture",
                interval="1d",
            ),
            PricePoint(
                "TEST:USD",
                now - timedelta(days=44),
                Decimal("98"),
                "fixture",
                interval="5m",
            ),
            PricePoint(
                "TEST:USD",
                now - timedelta(days=46),
                Decimal("97"),
                "fixture",
                interval="5m",
            ),
        ]
    )

    sizes = cache.sizes()["TEST:USD"]
    assert sizes == {"1m": 0, "5m": 1, "1d": 2}
    assert min(point.timestamp for point in cache.points("TEST:USD")) == now - timedelta(days=399)


def test_global_prune_expires_archived_symbol_without_new_observations() -> None:
    cache = HistoryCache()
    archived_at = datetime(2026, 1, 1, tzinfo=UTC)
    cache.load(
        [
            PricePoint("ARCHIVED:USD", archived_at, Decimal("1"), "fixture", interval="1m"),
            PricePoint("ARCHIVED:USD", archived_at, Decimal("1"), "fixture", interval="5m"),
            PricePoint("ARCHIVED:USD", archived_at, Decimal("1"), "fixture", interval="1d"),
        ]
    )

    assert cache.prune(archived_at + timedelta(hours=49)) == 1
    assert cache.sizes()["ARCHIVED:USD"] == {"1m": 0, "5m": 1, "1d": 1}

    assert cache.prune(archived_at + timedelta(days=46)) == 1
    assert cache.sizes()["ARCHIVED:USD"] == {"1m": 0, "5m": 0, "1d": 1}

    assert cache.prune(archived_at + timedelta(days=401)) == 1
    assert "ARCHIVED:USD" not in cache.sizes()
    assert cache.points("ARCHIVED:USD") == ()
