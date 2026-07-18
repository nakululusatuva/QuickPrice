from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from quickprice.domain import (
    AccrualIndexPoint,
    DividendEvent,
    PricePoint,
    ProviderQuote,
    RewardAccrualMode,
    YieldMetric,
    YieldQuality,
    YieldRateType,
)
from quickprice.service import QuickPriceService

UTC = UTC
API_KEY = "test-key-with-enough-entropy"
NOW = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)


def seed_complete(service: QuickPriceService, *, missing: set[str] | None = None) -> None:
    missing = missing or set()
    prices = {
        "BTC:USDC": Decimal("120000"),
        "ETH:USDC": Decimal("5000"),
        "WBETH:USDC": Decimal("5500"),
        "QQQM:USD": Decimal("250"),
        "BOXX:USD": Decimal("110"),
        "SGOV:USD": Decimal("100.50"),
        "USD:CNH": Decimal("7.20"),
        "HKD:CNH": Decimal("0.923"),
    }
    for symbol in service.registry:
        price = prices.get(symbol, Decimal("100"))
        history = [
            PricePoint(
                symbol,
                NOW - duration,
                price * Decimal("0.99"),
                "fixture",
            )
            for duration in (
                timedelta(days=31),
                timedelta(days=8),
                timedelta(hours=25),
                timedelta(hours=5),
                timedelta(hours=2),
            )
        ]
        history.append(
            PricePoint(
                symbol,
                NOW - timedelta(days=366),
                price * Decimal("0.98"),
                "fixture",
                interval="1d",
            )
        )
        service.publish_history(history, persist=False)
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
    service.publish_dividend(
        DividendEvent(
            "SGOV:USD",
            date(2026, 7, 1),
            date(2026, 7, 7),
            Decimal("0.40"),
            "USD",
            "monthly",
            "fixture",
        ),
        persist=False,
    )
    service.publish_yield_metric(
        YieldMetric(
            "BOXX:USD",
            Decimal("4.25"),
            NOW - timedelta(days=1),
            "DGS3MO",
            "fred",
            True,
        ),
        persist=False,
    )
    service.publish_yield_metric(
        YieldMetric(
            symbol="WBETH:USDC",
            value=Decimal("3.25"),
            as_of=NOW - timedelta(minutes=1),
            method="onchain_exchange_rate_trailing_apy",
            provider="fixture_ethereum",
            rate_type=YieldRateType.APY,
            observation_window_days=Decimal("7"),
            accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
            underlying_asset="ETH",
            is_estimate=True,
            accrual_index=AccrualIndexPoint(
                symbol="WBETH:ETH",
                underlying_asset="ETH",
                value=Decimal("1.10"),
                as_of=NOW - timedelta(minutes=1),
                provider="fixture_ethereum",
            ),
            quality=YieldQuality(stale=False, staleness_ms=60_000, confidence="high"),
        ),
        persist=False,
    )
    for symbol in service.registry:
        if symbol in missing:
            continue
        service.publish_quote(
            ProviderQuote(
                symbol,
                prices.get(symbol, Decimal("100")),
                NOW,
                "fixture",
                "fixture_feed",
                market_status="open",
                coverage="test",
            ),
            persist=False,
        )
