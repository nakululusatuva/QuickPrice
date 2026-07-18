"""Central metadata for built-in US-listed stocks, ETFs, and dividend policies."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

QUARTERLY_DIVIDEND_STRATEGY = "latest_regular_cash_annualized_x4"


@dataclass(frozen=True, slots=True)
class CommonStockMetadata:
    symbol: str
    ticker: str
    name: str
    description: str
    dividend_frequency: Literal["quarterly"] | None = None


COMMON_STOCKS = (
    CommonStockMetadata(
        symbol="AAPL:USD",
        ticker="AAPL",
        name="Apple Inc.",
        description="A consumer technology company designing devices, software, and services.",
        dividend_frequency="quarterly",
    ),
    CommonStockMetadata(
        symbol="MSFT:USD",
        ticker="MSFT",
        name="Microsoft Corporation",
        description="A technology company providing software, cloud, and computing products.",
        dividend_frequency="quarterly",
    ),
    CommonStockMetadata(
        symbol="AMZN:USD",
        ticker="AMZN",
        name="Amazon.com, Inc.",
        description="A commerce, cloud computing, logistics, and digital services company.",
    ),
    CommonStockMetadata(
        symbol="GOOGL:USD",
        ticker="GOOGL",
        name="Alphabet Inc. Class A",
        description="A technology holding company operating Google and related businesses.",
        dividend_frequency="quarterly",
    ),
    CommonStockMetadata(
        symbol="META:USD",
        ticker="META",
        name="Meta Platforms, Inc. Class A",
        description="A technology company operating social platforms and computing products.",
        dividend_frequency="quarterly",
    ),
    CommonStockMetadata(
        symbol="NVDA:USD",
        ticker="NVDA",
        name="NVIDIA Corporation",
        description="A computing company developing accelerated processors and software platforms.",
        dividend_frequency="quarterly",
    ),
    CommonStockMetadata(
        symbol="TSLA:USD",
        ticker="TSLA",
        name="Tesla, Inc.",
        description="An electric vehicle, energy generation, and energy storage company.",
    ),
    CommonStockMetadata(
        symbol="SPCX:USD",
        ticker="SPCX",
        name="Space Exploration Technologies Corp.",
        description=(
            "A space transportation and communications company listed on Nasdaq after its "
            "June 2026 initial public offering."
        ),
    ),
    CommonStockMetadata(
        symbol="MSTR:USD",
        ticker="MSTR",
        name="Strategy Inc. Class A",
        description="An enterprise analytics and Bitcoin treasury company.",
    ),
    CommonStockMetadata(
        symbol="CRCL:USD",
        ticker="CRCL",
        name="Circle Internet Group, Inc. Class A",
        description="A financial technology company providing stablecoin infrastructure.",
    ),
)

COMMON_STOCK_BY_SYMBOL = MappingProxyType({item.symbol: item for item in COMMON_STOCKS})
COMMON_STOCK_SYMBOLS = tuple(item.symbol for item in COMMON_STOCKS)
QUARTERLY_STOCK_DIVIDEND_SYMBOLS = tuple(
    item.symbol for item in COMMON_STOCKS if item.dividend_frequency == "quarterly"
)

FUND_TICKERS = MappingProxyType(
    {
        "QQQM:USD": "QQQM",
        "BOXX:USD": "BOXX",
        "SGOV:USD": "SGOV",
    }
)
LISTED_TICKERS = MappingProxyType(
    {
        **FUND_TICKERS,
        **{item.symbol: item.ticker for item in COMMON_STOCKS},
    }
)
LISTED_SYMBOLS = tuple(LISTED_TICKERS)
DIVIDEND_FREQUENCIES = MappingProxyType(
    {
        "QQQM:USD": "quarterly",
        "SGOV:USD": "monthly",
        **{
            item.symbol: item.dividend_frequency
            for item in COMMON_STOCKS
            if item.dividend_frequency is not None
        },
    }
)
DIVIDEND_SYMBOLS = tuple(DIVIDEND_FREQUENCIES)


__all__ = [
    "COMMON_STOCKS",
    "COMMON_STOCK_BY_SYMBOL",
    "COMMON_STOCK_SYMBOLS",
    "DIVIDEND_FREQUENCIES",
    "DIVIDEND_SYMBOLS",
    "FUND_TICKERS",
    "LISTED_SYMBOLS",
    "LISTED_TICKERS",
    "QUARTERLY_DIVIDEND_STRATEGY",
    "QUARTERLY_STOCK_DIVIDEND_SYMBOLS",
    "CommonStockMetadata",
]
