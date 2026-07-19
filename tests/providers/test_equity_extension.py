from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from quickprice.config import Settings
from quickprice.equities import (
    COMMON_STOCK_SYMBOLS,
    DIVIDEND_FREQUENCIES,
    DIVIDEND_SYMBOLS,
    LISTED_SYMBOLS,
    LISTED_TICKERS,
    QUARTERLY_STOCK_DIVIDEND_SYMBOLS,
)
from quickprice.provider_factory import (
    create_builtin_alpaca_provider,
    create_builtin_alpha_vantage_provider,
    create_builtin_finnhub_provider,
    create_builtin_twelve_data_provider,
)
from quickprice.providers.alpaca import AlpacaProvider
from quickprice.providers.alpha_vantage import AlphaVantageProvider
from quickprice.providers.base import Capability, UnsupportedInstrument
from quickprice.providers.finnhub import FinnhubProvider
from quickprice.providers.twelve_data import TwelveDataProvider
from quickprice.providers.wiring import build_provider_graph


def test_builtin_factories_inject_one_canonical_listed_ticker_source() -> None:
    alpaca = create_builtin_alpaca_provider("key", "secret")
    finnhub = create_builtin_finnhub_provider("key")
    alpha = create_builtin_alpha_vantage_provider("key")
    twelve = create_builtin_twelve_data_provider("key")

    assert alpaca.symbols == dict(LISTED_TICKERS)
    assert finnhub.symbols == dict(LISTED_TICKERS)
    assert alpha.equity_symbols == dict(LISTED_TICKERS)
    assert {symbol: twelve.symbols[symbol] for symbol in LISTED_SYMBOLS} == dict(LISTED_TICKERS)
    assert alpaca._frequencies == dict(DIVIDEND_FREQUENCIES)
    assert alpha.dividend_frequencies == dict(DIVIDEND_FREQUENCIES)
    assert not getattr(AlpacaProvider, "symbols", {})
    assert not getattr(FinnhubProvider, "symbols", {})
    assert not getattr(AlphaVantageProvider, "equity_symbols", {})
    assert not getattr(TwelveDataProvider, "symbols", {})
    assert set(QUARTERLY_STOCK_DIVIDEND_SYMBOLS) == {
        "AAPL:USD",
        "MSFT:USD",
        "GOOGL:USD",
        "META:USD",
        "NVDA:USD",
    }


@pytest.mark.asyncio
async def test_alpaca_classifies_a_regular_stock_dividend() -> None:
    provider = create_builtin_alpaca_provider("key", "secret")
    provider._request_json = AsyncMock(
        return_value={
            "cash_dividends": [
                {
                    "symbol": "AAPL",
                    "special": False,
                    "foreign": False,
                    "sub_type": "",
                    "ex_date": "2026-06-12",
                    "payable_date": "2026-06-18",
                    "rate": "0.26",
                }
            ]
        }
    )

    event = await provider.get_latest_dividend("AAPL:USD")

    assert event is not None
    assert event.symbol == "AAPL:USD"
    assert event.amount == Decimal("0.26")
    assert event.frequency == "quarterly"
    with pytest.raises(UnsupportedInstrument, match="no dividend policy"):
        await provider.get_latest_dividend("AMZN:USD")


@pytest.mark.asyncio
async def test_listed_routes_use_all_quote_sources_but_only_alpaca_dividends() -> None:
    graph = build_provider_graph(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            alpaca_api_key="alpaca-key",
            alpaca_api_secret="alpaca-secret",
            finnhub_api_key="finnhub-key",
            twelve_data_api_key="twelve-key",
            alpha_vantage_api_key="alpha-key",
        )
    )
    try:
        for symbol in LISTED_SYMBOLS:
            assert tuple(
                type(provider) for provider in graph.router.providers_for(symbol, Capability.QUOTE)
            ) == (
                AlpacaProvider,
                FinnhubProvider,
                TwelveDataProvider,
                AlphaVantageProvider,
            )
            assert tuple(
                type(provider)
                for provider in graph.router.providers_for(symbol, Capability.HISTORY)
            ) == (AlpacaProvider, TwelveDataProvider, AlphaVantageProvider)
        for symbol in DIVIDEND_SYMBOLS:
            assert graph.router.providers_for(symbol, Capability.DIVIDEND) == (
                graph.providers["alpaca"],
            )
        for symbol in set(COMMON_STOCK_SYMBOLS) - set(DIVIDEND_SYMBOLS):
            assert graph.router.providers_for(symbol, Capability.DIVIDEND) == ()
        assert graph.router.providers_for("BOXX:USD", Capability.DIVIDEND) == ()
    finally:
        await graph.close()
