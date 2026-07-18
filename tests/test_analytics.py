from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quickprice.analytics import (
    boxx_yield,
    calculate_changes,
    qqqm_dividend,
    quarterly_dividend,
    sgov_yield,
)
from quickprice.domain import DividendEvent, PricePoint, ProviderQuote, YieldMetric

UTC = UTC


def test_all_six_rolling_change_windows_use_point_at_or_before_cutoff():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    current = Decimal("110")
    points = [
        PricePoint("BTC:USDC", now - duration, Decimal("100"), "fixture")
        for duration in (
            timedelta(days=366),
            timedelta(days=31),
            timedelta(days=8),
            timedelta(hours=25),
            timedelta(hours=5),
            timedelta(hours=2),
        )
    ]
    changes = calculate_changes(current, now, points)
    assert set(changes) == {"1h", "4h", "1d", "1w", "1mo", "1y"}
    assert all(item is not None and item.percent == Decimal("10.0") for item in changes.values())


def test_missing_history_is_explicit_null():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    changes = calculate_changes(Decimal("100"), now, [])
    assert all(value is None for value in changes.values())


def test_one_year_is_a_rolling_365_day_window_across_a_leap_year() -> None:
    now = datetime(2028, 3, 1, 12, tzinfo=UTC)
    before_cutoff = PricePoint(
        "BTC:USDC",
        now - timedelta(days=365, minutes=1),
        Decimal("80"),
        "fixture",
        interval="1d",
    )
    after_cutoff = PricePoint(
        "BTC:USDC",
        now - timedelta(days=364),
        Decimal("90"),
        "fixture",
        interval="1d",
    )
    change = calculate_changes(Decimal("100"), now, (before_cutoff, after_cutoff))["1y"]
    assert change is not None
    assert change.reference_as_of == before_cutoff.timestamp
    assert change.percent == Decimal("25.00")


@pytest.mark.parametrize("value", ["1e10000", "1e-10000"])
def test_prices_must_be_representable_as_finite_json_numbers(value):
    with pytest.raises(ValueError, match="JSON number"):
        ProviderQuote(
            "BTC:USDC",
            Decimal(value),
            datetime(2026, 7, 20, tzinfo=UTC),
            "fixture",
            "fixture",
        )


def test_qqqm_and_sgov_annualization_rules():
    event = DividendEvent(
        "QQQM:USD",
        date(2026, 6, 20),
        date(2026, 6, 25),
        Decimal("0.50"),
        "USD",
        "quarterly",
        "fixture",
    )
    qqqm = qqqm_dividend(event, Decimal("100"))
    assert qqqm.yield_percent == Decimal("2.00")
    assert quarterly_dividend(event, Decimal("100")) == qqqm

    sgov_event = DividendEvent(
        "SGOV:USD",
        date(2026, 7, 1),
        date(2026, 7, 7),
        Decimal("0.40"),
        "USD",
        "monthly",
        "fixture",
    )
    estimate = sgov_yield(sgov_event, Decimal("100"), datetime(2026, 7, 20, tzinfo=UTC))
    assert estimate.percent == Decimal("4.800")
    assert estimate.method == "latest_distribution_annualized"
    assert estimate.is_proxy is False


def test_special_distribution_is_not_annualized():
    event = DividendEvent(
        "QQQM:USD",
        date(2026, 6, 20),
        None,
        Decimal("5"),
        "USD",
        "special",
        "fixture",
        event_type="special_cash",
    )
    with pytest.raises(ValueError, match="regular cash"):
        qqqm_dividend(event, Decimal("100"))


def test_boxx_treasury_proxy_subtracts_net_expense_percentage_points():
    estimate = boxx_yield(
        YieldMetric(
            "BOXX:USD",
            Decimal("4.2500"),
            datetime(2026, 7, 19, tzinfo=UTC),
            "DGS3MO",
            "fred",
            True,
        )
    )
    assert estimate.percent == Decimal("4.0551")
    assert estimate.method == "treasury_3m_proxy_minus_expense"
    assert estimate.is_proxy is True
