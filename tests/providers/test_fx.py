from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quickprice.domain import PricePoint, ProviderQuote
from quickprice.fx import FX_CURRENCIES, FX_HUB_SYMBOLS, FX_SYMBOLS, fx_hub_requirements
from quickprice.provider_factory import builtin_fx_max_ages, builtin_fx_requirements
from quickprice.providers.base import UnsupportedInstrument
from quickprice.providers.fx import (
    SyntheticComponentError,
    UsdHubFxHistoryProvider,
    UsdHubFxQuoteProvider,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def make_quote(
    symbol: str,
    price: str,
    *,
    minutes_old: int = 0,
    provider: str = "twelve_data",
    fallback_level: int = 0,
) -> ProviderQuote:
    return ProviderQuote(
        symbol=symbol,
        price=Decimal(price),
        as_of=NOW - timedelta(minutes=minutes_old),
        provider=provider,
        feed=f"{provider}_fx",
        fallback_level=fallback_level,
    )


def test_fx_topology_has_every_directed_pair_and_only_five_vendor_roots() -> None:
    assert len(FX_SYMBOLS) == len(FX_CURRENCIES) * (len(FX_CURRENCIES) - 1) == 30
    assert len(set(FX_SYMBOLS)) == 30
    assert FX_HUB_SYMBOLS == (
        "USD:EUR",
        "USD:GBP",
        "USD:HKD",
        "USD:SGD",
        "USD:CNH",
    )
    assert fx_hub_requirements("EUR:USD") == ("USD:EUR",)
    assert fx_hub_requirements("GBP:CNH") == ("USD:CNH", "USD:GBP")
    assert all(set(fx_hub_requirements(symbol)).issubset(FX_HUB_SYMBOLS) for symbol in FX_SYMBOLS)


@pytest.mark.asyncio
async def test_fx_quote_provider_inverts_a_single_hub_with_provenance() -> None:
    calls: list[str] = []

    async def resolve(symbol: str) -> ProviderQuote:
        calls.append(symbol)
        return make_quote("USD:EUR", "0.80", provider="alpha_vantage", fallback_level=1)

    provider = UsdHubFxQuoteProvider(
        resolve,
        requirements=builtin_fx_requirements(),
        max_ages=builtin_fx_max_ages(),
        clock=lambda: NOW,
    )
    result = await provider.get_quote("EUR:USD")

    assert calls == ["USD:EUR"]
    assert result.price == Decimal("1.25")
    assert result.as_of == NOW
    assert result.is_derived is True
    assert result.fallback_level == 1
    assert result.price_basis == "synthetic_inverse"
    assert [(item.symbol, item.role) for item in result.components] == [("USD:EUR", "denominator")]


@pytest.mark.asyncio
async def test_fx_quote_provider_uses_usd_quote_over_usd_base() -> None:
    values = {
        "USD:CNH": make_quote("USD:CNH", "7.20", minutes_old=1),
        "USD:GBP": make_quote(
            "USD:GBP",
            "0.75",
            minutes_old=2,
            provider="alpha_vantage",
            fallback_level=1,
        ),
    }
    calls: list[str] = []

    async def resolve(symbol: str) -> ProviderQuote:
        calls.append(symbol)
        return values[symbol]

    provider = UsdHubFxQuoteProvider(
        resolve,
        requirements=builtin_fx_requirements(),
        max_ages=builtin_fx_max_ages(),
        clock=lambda: NOW,
    )
    result = await provider.get_quote("GBP:CNH")

    assert set(calls) == {"USD:CNH", "USD:GBP"}
    assert result.price == Decimal("9.6")
    assert result.as_of == NOW - timedelta(minutes=2)
    assert result.fallback_level == 1
    assert result.is_derived is True
    assert [(item.symbol, item.role) for item in result.components] == [
        ("USD:CNH", "numerator"),
        ("USD:GBP", "denominator"),
    ]


@pytest.mark.asyncio
async def test_fx_quote_provider_rejects_stale_and_direct_components() -> None:
    async def stale_cnh(symbol: str) -> ProviderQuote:
        assert symbol == "USD:CNH"
        return make_quote(symbol, "7.20", minutes_old=6)

    provider = UsdHubFxQuoteProvider(
        stale_cnh,
        requirements=builtin_fx_requirements(),
        max_ages=builtin_fx_max_ages(),
        clock=lambda: NOW,
    )
    with pytest.raises(SyntheticComponentError, match="stale"):
        await provider.get_quote("CNH:USD")
    with pytest.raises(UnsupportedInstrument, match="direct providers"):
        await provider.get_quote("USD:CNH")

    max_ages = builtin_fx_max_ages()
    assert max_ages["USD:CNH"] == timedelta(minutes=5)
    assert max_ages["USD:HKD"] == timedelta(minutes=20)


@pytest.mark.asyncio
async def test_fx_history_never_uses_a_future_denominator() -> None:
    calls: list[tuple[str, dict]] = []

    async def resolve(symbol: str, **kwargs):
        calls.append((symbol, kwargs))
        if symbol == "USD:EUR":
            return (PricePoint(symbol, NOW, Decimal("0.90"), "fixture"),)
        return (
            PricePoint(symbol, NOW - timedelta(minutes=3), Decimal("0.75"), "fixture"),
            PricePoint(symbol, NOW + timedelta(minutes=1), Decimal("0.80"), "fixture"),
        )

    provider = UsdHubFxHistoryProvider(
        resolve,
        requirements=builtin_fx_requirements(),
    )
    result = await provider.get_history(
        "GBP:EUR",
        interval="1m",
        start=NOW - timedelta(hours=1),
        end=NOW,
        limit=10,
    )

    assert result[0].price == Decimal("0.90") / Decimal("0.75")
    assert result[0].timestamp == NOW
    assert result[0].is_derived is True
    assert calls[0][0] == "USD:EUR"
    assert calls[1][0] == "USD:GBP"
    assert calls[1][1]["start"] == NOW - timedelta(hours=1, minutes=20)
    assert calls[1][1]["limit"] == 11


@pytest.mark.asyncio
async def test_fx_inverse_history_preserves_source_timestamps() -> None:
    source = (
        PricePoint("USD:SGD", NOW - timedelta(minutes=1), Decimal("1.25"), "fixture"),
        PricePoint("USD:SGD", NOW, Decimal("1.20"), "fixture"),
    )

    async def resolve(symbol: str, **kwargs):
        del kwargs
        assert symbol == "USD:SGD"
        return source

    provider = UsdHubFxHistoryProvider(
        resolve,
        requirements=builtin_fx_requirements(),
    )
    result = await provider.get_history(
        "SGD:USD",
        interval="1m",
        start=NOW - timedelta(hours=1),
        end=NOW,
        limit=1,
    )

    assert len(result) == 1
    assert result[0].timestamp == NOW
    assert result[0].price == Decimal(1) / Decimal("1.20")
