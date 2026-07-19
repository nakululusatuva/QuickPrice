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
from .compiler import (
    CompiledRoutePlan,
    RouteCompileError,
    build_compiled_provider_graph,
    compile_catalog_route_plan,
)
from .descriptors import provider_catalog_snapshot, search_provider_symbols
from .finnhub import FinnhubProvider
from .fred import FredProvider
from .kraken import KrakenProvider
from .okx import OkxBethYieldProvider, OkxMarketProvider
from .router import ProviderRouter
from .staking import (
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
    synthesize_inverse,
    synthesize_multiplication,
)
from .twelve_data import TwelveDataProvider
from .wiring import ProviderGraph, build_provider_graph

__all__ = [
    "AccrualIndexProvider",
    "AllProvidersFailed",
    "AlpacaProvider",
    "AlphaVantageProvider",
    "BinanceProvider",
    "BinanceWbethYieldProvider",
    "Capability",
    "CoinGeckoProvider",
    "CompiledRoutePlan",
    "DividendProvider",
    "EthereumExchangeRateSpec",
    "EthereumExchangeRateYieldProvider",
    "FinnhubProvider",
    "FredProvider",
    "HistoryProvider",
    "KrakenProvider",
    "MalformedResponse",
    "OkxBethYieldProvider",
    "OkxMarketProvider",
    "ProviderError",
    "ProviderGraph",
    "ProviderRateLimited",
    "ProviderRouter",
    "ProviderUnavailable",
    "QuoteProvider",
    "RouteCompileError",
    "StakingMarketRatioSpec",
    "StakingMarketRatioYieldProvider",
    "SyntheticHistoryProvider",
    "SyntheticQuoteProvider",
    "SyntheticRecipe",
    "TwelveDataProvider",
    "UnsupportedInstrument",
    "YieldProvider",
    "build_compiled_provider_graph",
    "build_provider_graph",
    "compile_catalog_route_plan",
    "provider_catalog_snapshot",
    "search_provider_symbols",
    "synthesize_division",
    "synthesize_history",
    "synthesize_inverse",
    "synthesize_multiplication",
]
