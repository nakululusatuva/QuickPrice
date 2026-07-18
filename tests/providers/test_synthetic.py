from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quickprice.domain import PricePoint, ProviderQuote
from quickprice.providers.synthetic import (
    SyntheticComponentError,
    SyntheticHistoryProvider,
    SyntheticQuoteProvider,
    SyntheticRecipe,
    synthesize_division,
    synthesize_history,
    synthesize_hkd_cnh,
    synthesize_wbeth,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def make_quote(symbol: str, price: str, *, seconds_old: int = 0, provider: str = "fixture"):
    return ProviderQuote(
        symbol=symbol,
        price=Decimal(price),
        as_of=NOW - timedelta(seconds=seconds_old),
        provider=provider,
        feed="fixed",
    )


def test_wbeth_product_exposes_both_components():
    result = synthesize_wbeth(
        make_quote("WBETH:ETH", "1.035", seconds_old=1),
        make_quote("ETH:USDC", "4000", seconds_old=2),
        now=NOW,
    )

    assert result.price == Decimal("4140.000")
    assert result.symbol == "WBETH:USDC"
    assert result.is_derived is True
    assert result.as_of == NOW - timedelta(seconds=2)
    assert [item.role for item in result.components] == ["multiplicand", "multiplier"]


def test_wbeth_rejects_components_more_than_two_seconds_apart():
    with pytest.raises(SyntheticComponentError, match="skew"):
        synthesize_wbeth(
            make_quote("WBETH:ETH", "1.035", seconds_old=1),
            make_quote("ETH:USDC", "4000", seconds_old=4),
            now=NOW,
        )


def test_usdt_ratio_formula():
    result = synthesize_division(
        "WBETH:USDC",
        make_quote("WBETH:USDT", "4142"),
        make_quote("USDC:USDT", "1.0005", seconds_old=1),
        now=NOW,
    )
    assert result.price == Decimal("4142") / Decimal("1.0005")


def test_hkd_cnh_allows_slow_hkd_leg_up_to_twenty_minutes():
    result = synthesize_hkd_cnh(
        make_quote("USD:CNH", "7.22", seconds_old=120),
        make_quote("USD:HKD", "7.80", seconds_old=19 * 60),
        now=NOW,
    )
    assert result.price == Decimal("7.22") / Decimal("7.80")


def test_hkd_cnh_rejects_hkd_leg_over_twenty_minutes_old():
    with pytest.raises(SyntheticComponentError, match="right component is stale"):
        synthesize_hkd_cnh(
            make_quote("USD:CNH", "7.22", seconds_old=120),
            make_quote("USD:HKD", "7.80", seconds_old=20 * 60 + 1),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_synthetic_provider_resolves_components_concurrently():
    values = {
        "WBETH:ETH": make_quote("WBETH:ETH", "1.035", seconds_old=1),
        "ETH:USDC": make_quote("ETH:USDC", "4000", seconds_old=1),
    }

    async def resolve(symbol: str):
        return values[symbol]

    provider = SyntheticQuoteProvider(
        resolve,
        (SyntheticRecipe.wbeth_primary(),),
        clock=lambda: NOW,
    )

    result = await provider.get_quote("WBETH:USDC")

    assert result.price == Decimal("4140.000")


def test_synthetic_history_uses_last_component_at_or_before_cutoff():
    recipe = SyntheticRecipe.hkd_cnh()
    left = (
        PricePoint("USD:CNH", NOW - timedelta(minutes=2), Decimal("7.20"), "fixture"),
        PricePoint("USD:CNH", NOW, Decimal("7.22"), "fixture"),
    )
    right = (
        PricePoint("USD:HKD", NOW - timedelta(minutes=10), Decimal("7.80"), "fixture"),
        # A future leg must not be used for the NOW left point.
        PricePoint("USD:HKD", NOW + timedelta(minutes=1), Decimal("7.81"), "fixture"),
    )

    result = synthesize_history(recipe, left, right, interval="1m")

    assert len(result) == 2
    assert result[-1].price == Decimal("7.22") / Decimal("7.80")
    assert result[-1].is_derived is True


@pytest.mark.asyncio
async def test_synthetic_history_provider_fetches_extra_leading_right_point():
    calls = []

    async def resolve(symbol, **kwargs):
        calls.append((symbol, kwargs))
        if symbol == "USD:CNH":
            return (PricePoint(symbol, NOW, Decimal("7.22"), "fixture"),)
        return (PricePoint(symbol, NOW - timedelta(minutes=10), Decimal("7.80"), "fixture"),)

    provider = SyntheticHistoryProvider(resolve, (SyntheticRecipe.hkd_cnh(),))
    result = await provider.get_history(
        "HKD:CNH",
        interval="1m",
        start=NOW - timedelta(hours=1),
        end=NOW,
        limit=10,
    )

    assert result[0].price == Decimal("7.22") / Decimal("7.80")
    assert calls[1][1]["start"] == NOW - timedelta(hours=1, minutes=20)
    assert calls[1][1]["limit"] == 11
