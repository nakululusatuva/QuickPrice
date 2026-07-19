from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quickprice.analytics import calculate_changes, calculate_changes_from_references
from quickprice.cache import HistoryCache
from quickprice.collectors import derive_cross_history
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


def test_cached_change_references_match_full_history_and_invalidate_on_backfill() -> None:
    cache = HistoryCache()
    now = datetime(2026, 7, 20, 12, 34, 45, tzinfo=UTC)
    points = [
        PricePoint(
            "TEST:USD",
            now - timedelta(minutes=minute),
            Decimal(200 - minute / 10_000),
            "fixture",
            interval="1m",
        )
        for minute in range(1, 121)
    ]
    points.extend(
        PricePoint(
            "TEST:USD",
            now - timedelta(minutes=minute),
            Decimal(190 - minute / 100_000),
            "fixture",
            interval="5m",
        )
        for minute in range(125, 45 * 24 * 60, 5)
    )
    points.extend(
        PricePoint(
            "TEST:USD",
            now - timedelta(days=day),
            Decimal(180 - day / 1_000),
            "fixture",
            interval="1d",
        )
        for day in range(46, 390)
    )
    cache.load(points)

    expected = calculate_changes(Decimal("220"), now, cache.points("TEST:USD"))
    references = cache.change_references("TEST:USD", now)
    actual = calculate_changes_from_references(Decimal("220"), references)
    assert actual == expected

    # A current live bucket cannot change any rolling predecessor, so the
    # immutable reference map is reused for the rest of the quote minute.
    cache.add(PricePoint("TEST:USD", now, Decimal("221"), "live"))
    assert cache.change_references("TEST:USD", now + timedelta(seconds=10)) is references

    cutoff = now - timedelta(hours=1)
    cache.add(PricePoint("TEST:USD", cutoff, Decimal("150"), "backfill"))
    refreshed = cache.change_references("TEST:USD", now)
    assert refreshed is not references
    assert refreshed["1h"].price == Decimal("150")


def test_virtual_fx_references_match_materialized_inverse_and_cross_histories() -> None:
    now = datetime(2026, 7, 20, 12, tzinfo=UTC)
    left_symbol = "USD:CNH"
    right_symbol = "USD:GBP"
    intervals = {
        "1m": (timedelta(minutes=0), timedelta(hours=1), timedelta(hours=2)),
        "5m": (
            timedelta(hours=4, minutes=5),
            timedelta(days=1, minutes=5),
            timedelta(days=7, minutes=5),
            timedelta(days=30, minutes=5),
        ),
        "1d": (timedelta(days=365), timedelta(days=380)),
    }
    left: list[PricePoint] = []
    right: list[PricePoint] = []
    for interval, ages in intervals.items():
        for index, age in enumerate(ages):
            timestamp = now - age
            left.append(
                PricePoint(
                    left_symbol,
                    timestamp,
                    Decimal("7.2") + Decimal(index) / Decimal(100),
                    "left",
                    interval=interval,
                )
            )
            right.append(
                PricePoint(
                    right_symbol,
                    timestamp - timedelta(minutes=5),
                    Decimal("0.75") + Decimal(index) / Decimal(1_000),
                    "right",
                    interval=interval,
                )
            )

    materialized = HistoryCache()
    materialized.load(
        [
            PricePoint(
                "CNH:USD",
                point.timestamp,
                Decimal(1) / point.price,
                "synthetic_fx",
                True,
                point.interval,
            )
            for point in left
        ]
    )
    materialized.load(
        list(
            derive_cross_history(
                "GBP:CNH",
                [point for point in left if point.interval == "1m"],
                [point for point in right if point.interval == "1m"],
                operation="divide",
                max_skew=timedelta(minutes=20),
                provider="synthetic_fx",
                interval="1m",
            )
        )
        + list(
            derive_cross_history(
                "GBP:CNH",
                [point for point in left if point.interval == "5m"],
                [point for point in right if point.interval == "5m"],
                operation="divide",
                max_skew=timedelta(minutes=20),
                provider="synthetic_fx",
                interval="5m",
            )
        )
        + list(
            derive_cross_history(
                "GBP:CNH",
                [point for point in left if point.interval == "1d"],
                [point for point in right if point.interval == "1d"],
                operation="divide",
                max_skew=timedelta(minutes=20),
                provider="synthetic_fx",
                interval="1d",
            )
        )
    )

    virtual = HistoryCache()
    virtual.load(left + right)
    virtual.register_synthetic_history(
        "CNH:USD",
        (left_symbol,),
        operation="inverse",
        max_skew=timedelta(minutes=20),
        provider="synthetic_fx",
    )
    virtual.register_synthetic_history(
        "GBP:CNH",
        (left_symbol, right_symbol),
        operation="divide",
        max_skew=timedelta(minutes=20),
        provider="synthetic_fx",
    )

    assert virtual.change_references("CNH:USD", now) == materialized.change_references(
        "CNH:USD", now
    )
    assert virtual.change_references("GBP:CNH", now) == materialized.change_references(
        "GBP:CNH", now
    )
    assert virtual.points("GBP:CNH") == ()


def test_retain_symbols_drops_archived_rings_but_keeps_virtual_dependencies() -> None:
    cache = HistoryCache()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    cache.load(
        [
            PricePoint("USD:EUR", now, Decimal("0.9"), "fixture", interval="1d"),
            PricePoint("OLD:USD", now, Decimal("10"), "fixture", interval="1d"),
        ]
    )
    cache.register_synthetic_history(
        "EUR:USD",
        ("USD:EUR",),
        operation="inverse",
        max_skew=timedelta(minutes=20),
        provider="synthetic_fx",
    )

    assert cache.retain_symbols(("EUR:USD",)) == 1
    assert cache.points("OLD:USD") == ()
    assert cache.points("USD:EUR")
    assert cache.change_references("EUR:USD", now + timedelta(days=366))["1y"] is not None


def test_virtual_cross_skips_unaligned_tail_and_rejects_dependency_cycles() -> None:
    cache = HistoryCache()
    now = datetime(2026, 7, 20, 12, tzinfo=UTC)
    aligned = now - timedelta(hours=1, minutes=40)
    cache.load(
        [
            PricePoint("LEFT:USD", aligned, Decimal("8"), "left"),
            PricePoint("LEFT:USD", now - timedelta(hours=1), Decimal("9"), "left"),
            PricePoint("RIGHT:USD", aligned, Decimal("2"), "right"),
        ]
    )
    cache.register_synthetic_history(
        "CROSS:USD",
        ("LEFT:USD", "RIGHT:USD"),
        operation="divide",
        max_skew=timedelta(minutes=20),
        provider="synthetic",
    )

    reference = cache.change_references("CROSS:USD", now)["1h"]
    assert reference is not None
    assert reference.timestamp == aligned
    assert reference.price == Decimal("4")

    cache.register_synthetic_history(
        "FIRST:USD",
        ("SECOND:USD",),
        operation="inverse",
        max_skew=timedelta(0),
        provider="synthetic",
    )
    with pytest.raises(ValueError, match="dependency cycle"):
        cache.register_synthetic_history(
            "SECOND:USD",
            ("FIRST:USD",),
            operation="inverse",
            max_skew=timedelta(0),
            provider="synthetic",
        )
