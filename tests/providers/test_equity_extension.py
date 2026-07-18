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
from quickprice.providers.alpaca import AlpacaProvider
from quickprice.providers.alpha_vantage import AlphaVantageProvider
from quickprice.providers.base import Capability, UnsupportedInstrument
from quickprice.providers.twelve_data import TwelveDataProvider
from quickprice.providers.wiring import build_provider_graph


def test_listed_ticker_maps_share_one_canonical_source() -> None:
    assert AlpacaProvider.symbols == dict(LISTED_TICKERS)
    assert AlphaVantageProvider.equity_symbols == dict(LISTED_TICKERS)
    assert {symbol: TwelveDataProvider.symbols[symbol] for symbol in LISTED_SYMBOLS} == dict(
        LISTED_TICKERS
    )
    assert AlpacaProvider._frequencies == dict(DIVIDEND_FREQUENCIES)
    assert AlphaVantageProvider.dividend_frequencies == dict(DIVIDEND_FREQUENCIES)
    assert set(QUARTERLY_STOCK_DIVIDEND_SYMBOLS) == {
        "AAPL:USD",
        "MSFT:USD",
        "GOOGL:USD",
        "META:USD",
        "NVDA:USD",
    }


@pytest.mark.asyncio
async def test_alpaca_classifies_a_regular_stock_dividend() -> None:
    provider = AlpacaProvider("key", "secret")
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
            twelve_data_api_key="twelve-key",
            alpha_vantage_api_key="alpha-key",
        )
    )
    try:
        for symbol in LISTED_SYMBOLS:
            assert tuple(
                type(provider) for provider in graph.router.providers_for(symbol, Capability.QUOTE)
            ) == (AlpacaProvider, TwelveDataProvider, AlphaVantageProvider)
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
