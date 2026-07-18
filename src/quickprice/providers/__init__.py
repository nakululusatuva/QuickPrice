"""Market-data provider adapters and resilient capability routing.

The public surface deliberately exposes capabilities instead of a single large
provider interface.  A provider may therefore implement only the feeds it can
serve accurately (for example FRED implements :class:`YieldProvider` only).
"""

from .alpaca import AlpacaProvider
from .alpha_vantage import AlphaVantageProvider
from .base import (
    AccrualIndexProvider,
    AllProvidersFailed,
    Capability,
    DividendProvider,
    HistoryProvider,
    MalformedResponse,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    QuoteProvider,
    UnsupportedInstrument,
    YieldProvider,
)
from .binance import BinanceProvider
from .coingecko import CoinGeckoProvider
from .finnhub import FinnhubProvider
from .fred import FredProvider
from .kraken import KrakenProvider
from .router import ProviderRouter
from .staking import (
    WBETH_ETHEREUM_SPEC,
    WBETH_MARKET_RATIO_SPEC,
    BinanceWbethYieldProvider,
    EthereumExchangeRateSpec,
    EthereumExchangeRateYieldProvider,
    StakingMarketRatioSpec,
    StakingMarketRatioYieldProvider,
)
from .synthetic import (
    SyntheticHistoryProvider,
    SyntheticQuoteProvider,
    SyntheticRecipe,
    synthesize_division,
    synthesize_history,
    synthesize_hkd_cnh,
    synthesize_multiplication,
    synthesize_wbeth,
)
from .twelve_data import TwelveDataProvider
from .wiring import ProviderGraph, build_provider_graph

__all__ = [
    "WBETH_ETHEREUM_SPEC",
    "WBETH_MARKET_RATIO_SPEC",
    "AccrualIndexProvider",
    "AllProvidersFailed",
    "AlpacaProvider",
    "AlphaVantageProvider",
    "BinanceProvider",
    "BinanceWbethYieldProvider",
    "Capability",
    "CoinGeckoProvider",
    "DividendProvider",
    "EthereumExchangeRateSpec",
    "EthereumExchangeRateYieldProvider",
    "FinnhubProvider",
    "FredProvider",
    "HistoryProvider",
    "KrakenProvider",
    "MalformedResponse",
    "ProviderError",
    "ProviderGraph",
    "ProviderRateLimited",
    "ProviderRouter",
    "ProviderUnavailable",
    "QuoteProvider",
    "StakingMarketRatioSpec",
    "StakingMarketRatioYieldProvider",
    "SyntheticHistoryProvider",
    "SyntheticQuoteProvider",
    "SyntheticRecipe",
    "TwelveDataProvider",
    "UnsupportedInstrument",
    "YieldProvider",
    "build_provider_graph",
    "synthesize_division",
    "synthesize_history",
    "synthesize_hkd_cnh",
    "synthesize_multiplication",
    "synthesize_wbeth",
]
