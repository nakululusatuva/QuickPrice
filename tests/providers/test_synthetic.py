from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import quickprice.providers.synthetic as synthetic_module
from quickprice.domain import PricePoint, ProviderQuote, SourceComponent
from quickprice.providers.synthetic import (
    SyntheticComponentError,
    SyntheticHistoryProvider,
    SyntheticQuoteProvider,
    SyntheticRecipe,
    synthesize_division,
    synthesize_history,
    synthesize_inverse,
    synthesize_multiplication,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

MULTIPLY_RECIPE = SyntheticRecipe(
    symbol="ALPHA:UNIT",
    left_symbol="ALPHA:BRIDGE",
    right_symbol="BRIDGE:UNIT",
    operation="multiply",
    max_skew=timedelta(seconds=2),
    left_max_age=timedelta(seconds=15),
    right_max_age=timedelta(seconds=15),
    provider_name="synthetic_fixture",
)
DIVIDE_RECIPE = SyntheticRecipe(
    symbol="NORTH:SOUTH",
    left_symbol="ANCHOR:SOUTH",
    right_symbol="ANCHOR:NORTH",
    operation="divide",
    max_skew=timedelta(minutes=20),
    left_max_age=timedelta(minutes=5),
    right_max_age=timedelta(minutes=20),
    provider_name="synthetic_fixture",
)
INVERSE_RECIPE = SyntheticRecipe(
    symbol="UNIT:SOURCE",
    left_symbol="SOURCE:UNIT",
    right_symbol="SOURCE:UNIT",
    operation="inverse",
    max_skew=timedelta(0),
    left_max_age=timedelta(seconds=15),
    provider_name="synthetic_fixture",
)


def make_quote(symbol: str, price: str, *, seconds_old: int = 0, provider: str = "fixture"):
    return ProviderQuote(
        symbol=symbol,
        price=Decimal(price),
        as_of=NOW - timedelta(seconds=seconds_old),
        provider=provider,
        feed="fixed",
    )


def test_provider_source_contains_no_embedded_trading_pair_literals():
    source = inspect.getsource(synthetic_module)
    pair_literals = re.findall(r"['\"]([A-Z][A-Z0-9_-]*:[A-Z][A-Z0-9_-]*)['\"]", source)

    assert pair_literals == []


def test_generic_product_exposes_both_components():
    result = synthesize_multiplication(
        MULTIPLY_RECIPE.symbol,
        make_quote(MULTIPLY_RECIPE.left_symbol, "1.035", seconds_old=1),
        make_quote(MULTIPLY_RECIPE.right_symbol, "4000", seconds_old=2),
        max_skew=MULTIPLY_RECIPE.max_skew,
        now=NOW,
        left_max_age=MULTIPLY_RECIPE.left_max_age,
        right_max_age=MULTIPLY_RECIPE.right_max_age,
        provider_name=MULTIPLY_RECIPE.provider_name,
    )

    assert result.price == Decimal("4140.000")
    assert result.symbol == MULTIPLY_RECIPE.symbol
    assert result.is_derived is True
    assert result.as_of == NOW - timedelta(seconds=2)
    assert [item.role for item in result.components] == ["multiplicand", "multiplier"]


def test_generic_product_rejects_components_beyond_configured_skew():
    with pytest.raises(SyntheticComponentError, match="skew"):
        synthesize_multiplication(
            MULTIPLY_RECIPE.symbol,
            make_quote(MULTIPLY_RECIPE.left_symbol, "1.035", seconds_old=1),
            make_quote(MULTIPLY_RECIPE.right_symbol, "4000", seconds_old=4),
            max_skew=MULTIPLY_RECIPE.max_skew,
            now=NOW,
        )


def test_generic_ratio_formula():
    result = synthesize_division(
        DIVIDE_RECIPE.symbol,
        make_quote(DIVIDE_RECIPE.left_symbol, "4142"),
        make_quote(DIVIDE_RECIPE.right_symbol, "1.0005", seconds_old=1),
        now=NOW,
    )

    assert result.price == Decimal("4142") / Decimal("1.0005")


def test_synthetic_quote_preserves_nested_book_provenance():
    left = ProviderQuote(
        MULTIPLY_RECIPE.left_symbol,
        Decimal("4142"),
        NOW,
        "fixture_book",
        "fixture_spot_book",
        price_basis="midpoint",
        is_derived=True,
        components=(
            SourceComponent(
                MULTIPLY_RECIPE.left_symbol,
                "fixture_book",
                Decimal("4141"),
                NOW,
                "fixture_spot_book",
                "best_bid",
            ),
            SourceComponent(
                MULTIPLY_RECIPE.left_symbol,
                "fixture_book",
                Decimal("4143"),
                NOW,
                "fixture_spot_book",
                "best_ask",
            ),
        ),
    )
    right = make_quote(MULTIPLY_RECIPE.right_symbol, "1.0005")

    result = synthesize_division(DIVIDE_RECIPE.symbol, left, right, now=NOW)

    assert [item.role for item in result.components] == [
        "numerator",
        "numerator_best_bid",
        "numerator_best_ask",
        "denominator",
    ]


def test_generic_ratio_allows_each_leg_within_configured_freshness():
    result = synthesize_division(
        DIVIDE_RECIPE.symbol,
        make_quote(DIVIDE_RECIPE.left_symbol, "7.22", seconds_old=120),
        make_quote(DIVIDE_RECIPE.right_symbol, "7.80", seconds_old=19 * 60),
        max_skew=DIVIDE_RECIPE.max_skew,
        now=NOW,
        numerator_max_age=DIVIDE_RECIPE.left_max_age,
        denominator_max_age=DIVIDE_RECIPE.right_max_age,
    )

    assert result.price == Decimal("7.22") / Decimal("7.80")


def test_generic_ratio_rejects_a_leg_beyond_configured_freshness():
    with pytest.raises(SyntheticComponentError, match="right component is stale"):
        synthesize_division(
            DIVIDE_RECIPE.symbol,
            make_quote(DIVIDE_RECIPE.left_symbol, "7.22", seconds_old=120),
            make_quote(DIVIDE_RECIPE.right_symbol, "7.80", seconds_old=20 * 60 + 1),
            max_skew=DIVIDE_RECIPE.max_skew,
            now=NOW,
            numerator_max_age=DIVIDE_RECIPE.left_max_age,
            denominator_max_age=DIVIDE_RECIPE.right_max_age,
        )


@pytest.mark.asyncio
async def test_synthetic_quote_provider_uses_instance_recipe():
    values = {
        MULTIPLY_RECIPE.left_symbol: make_quote(
            MULTIPLY_RECIPE.left_symbol, "1.035", seconds_old=1
        ),
        MULTIPLY_RECIPE.right_symbol: make_quote(
            MULTIPLY_RECIPE.right_symbol, "4000", seconds_old=1
        ),
    }

    async def resolve(symbol: str):
        return values[symbol]

    provider = SyntheticQuoteProvider(resolve, (MULTIPLY_RECIPE,), clock=lambda: NOW)

    result = await provider.get_quote(MULTIPLY_RECIPE.symbol)

    assert result.price == Decimal("4140.000")
    assert result.provider == MULTIPLY_RECIPE.provider_name


@pytest.mark.asyncio
async def test_synthetic_quote_provider_supports_instance_inverse_recipe():
    calls = []

    async def resolve(symbol: str):
        calls.append(symbol)
        return make_quote(symbol, "4", seconds_old=1)

    provider = SyntheticQuoteProvider(resolve, (INVERSE_RECIPE,), clock=lambda: NOW)

    result = await provider.get_quote(INVERSE_RECIPE.symbol)

    assert result.price == Decimal("0.25")
    assert calls == [INVERSE_RECIPE.left_symbol]


def test_generic_inverse_preserves_source_metadata():
    source = make_quote(INVERSE_RECIPE.left_symbol, "8", seconds_old=1)

    result = synthesize_inverse(
        INVERSE_RECIPE.symbol,
        source,
        now=NOW,
        maximum_age=INVERSE_RECIPE.left_max_age,
        provider_name=INVERSE_RECIPE.provider_name,
    )

    assert result.price == Decimal("0.125")
    assert result.components[0].symbol == INVERSE_RECIPE.left_symbol
    assert result.provider == INVERSE_RECIPE.provider_name


def test_synthetic_history_uses_last_component_at_or_before_cutoff():
    left = (
        PricePoint(
            DIVIDE_RECIPE.left_symbol,
            NOW - timedelta(minutes=2),
            Decimal("7.20"),
            "fixture",
        ),
        PricePoint(DIVIDE_RECIPE.left_symbol, NOW, Decimal("7.22"), "fixture"),
    )
    right = (
        PricePoint(
            DIVIDE_RECIPE.right_symbol,
            NOW - timedelta(minutes=10),
            Decimal("7.80"),
            "fixture",
        ),
        # A future leg must not be used for the NOW left point.
        PricePoint(
            DIVIDE_RECIPE.right_symbol,
            NOW + timedelta(minutes=1),
            Decimal("7.81"),
            "fixture",
        ),
    )

    result = synthesize_history(DIVIDE_RECIPE, left, right, interval="1m")

    assert len(result) == 2
    assert result[-1].price == Decimal("7.22") / Decimal("7.80")
    assert result[-1].is_derived is True


def test_synthetic_history_supports_instance_inverse_recipe():
    source = (PricePoint(INVERSE_RECIPE.left_symbol, NOW, Decimal("4"), "fixture"),)

    result = synthesize_history(INVERSE_RECIPE, source, (), interval="1m")

    assert result[0].price == Decimal("0.25")
    assert result[0].symbol == INVERSE_RECIPE.symbol


@pytest.mark.asyncio
async def test_synthetic_history_provider_fetches_extra_leading_right_point():
    calls = []

    async def resolve(symbol, **kwargs):
        calls.append((symbol, kwargs))
        if symbol == DIVIDE_RECIPE.left_symbol:
            return (PricePoint(symbol, NOW, Decimal("7.22"), "fixture"),)
        return (PricePoint(symbol, NOW - timedelta(minutes=10), Decimal("7.80"), "fixture"),)

    provider = SyntheticHistoryProvider(resolve, (DIVIDE_RECIPE,))
    result = await provider.get_history(
        DIVIDE_RECIPE.symbol,
        interval="1m",
        start=NOW - timedelta(hours=1),
        end=NOW,
        limit=10,
    )

    assert result[0].price == Decimal("7.22") / Decimal("7.80")
    assert calls[1][1]["start"] == NOW - timedelta(hours=1) - DIVIDE_RECIPE.max_skew
    assert calls[1][1]["limit"] == 11
