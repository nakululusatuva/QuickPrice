"""Pure calculation rules used by collectors and tests."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal

from .domain import (
    ChangeValue,
    DividendEvent,
    DividendMetric,
    PricePoint,
    YieldEstimate,
    YieldMetric,
    decimal,
)

WINDOWS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(days=7),
    "1mo": timedelta(days=30),
    "1y": timedelta(days=365),
}


def calculate_changes(
    current_price: Decimal,
    current_as_of: datetime,
    points: Iterable[PricePoint],
) -> dict[str, ChangeValue | None]:
    """Use the latest valid observation at or before each rolling cutoff."""
    current_price = decimal(current_price)
    ordered = sorted(
        (point for point in points if point.timestamp <= current_as_of),
        key=lambda point: point.timestamp,
    )
    result: dict[str, ChangeValue | None] = {}
    for name, duration in WINDOWS.items():
        cutoff = current_as_of - duration
        reference = next((point for point in reversed(ordered) if point.timestamp <= cutoff), None)
        if reference is None:
            result[name] = None
            continue
        percent = (current_price / reference.price - Decimal(1)) * Decimal(100)
        result[name] = ChangeValue(percent, reference.price, reference.timestamp)
    return result


def annualize_dividend(
    event: DividendEvent,
    current_price: Decimal,
    periods_per_year: int,
    method: str,
) -> DividendMetric:
    current_price = decimal(current_price)
    if current_price <= 0:
        raise ValueError("current price must be positive")
    if event.event_type != "regular_cash":
        raise ValueError("only regular cash dividends may be annualized")
    yield_percent = event.amount * Decimal(periods_per_year) / current_price * Decimal(100)
    return DividendMetric(
        yield_percent=yield_percent,
        ex_date=event.ex_date,
        payment_date=event.payment_date,
        amount=event.amount,
        currency=event.currency,
        frequency=event.frequency,
        method=method,
        provider=event.provider,
    )


def qqqm_dividend(event: DividendEvent, current_price: Decimal) -> DividendMetric:
    return annualize_dividend(event, current_price, 4, "latest_regular_cash_annualized_x4")


def sgov_yield(event: DividendEvent, current_price: Decimal, as_of: datetime) -> YieldEstimate:
    dividend = annualize_dividend(event, current_price, 12, "latest_distribution_annualized")
    return YieldEstimate(
        percent=dividend.yield_percent,
        as_of=as_of,
        method="latest_distribution_annualized",
        provider=event.provider,
        is_proxy=False,
        inputs={
            "latest_distribution": float(event.amount),
            "periods_per_year": 12,
            "price": float(decimal(current_price)),
            "ex_date": event.ex_date.isoformat(),
        },
    )


BOXX_NET_EXPENSE_PERCENT = Decimal("0.1949")


def boxx_yield(metric: YieldMetric) -> YieldEstimate:
    already_derived = metric.method == "treasury_3m_proxy_minus_expense"
    value = metric.value if already_derived else metric.value - BOXX_NET_EXPENSE_PERCENT
    treasury_value = (
        metric.components[0].price
        if already_derived and metric.components
        else metric.value + BOXX_NET_EXPENSE_PERCENT
        if already_derived
        else metric.value
    )
    return YieldEstimate(
        percent=value,
        as_of=metric.as_of,
        method="treasury_3m_proxy_minus_expense",
        provider=metric.provider,
        is_proxy=True,
        components=metric.components,
        fallback_level=metric.fallback_level,
        inputs={
            "fred_series": "DGS3MO",
            "treasury_3m_percent": float(treasury_value),
            "net_expense_percent": float(BOXX_NET_EXPENSE_PERCENT),
        },
    )
