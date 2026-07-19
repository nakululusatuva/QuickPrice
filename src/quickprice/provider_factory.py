"""Compatibility factories for QuickPrice's immutable built-in catalog.

Provider adapters intentionally have no built-in instrument knowledge.  These
helpers translate the managed policy layer into instance constructor inputs so
legacy callers can still request the shipped catalog without coupling adapters
to canonical symbols.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from .instrument_policy import (
    BUILTIN_ALPACA_ALLOWED_DIVIDEND_SUBTYPES,
    BUILTIN_ALPACA_DIVIDEND_FREQUENCIES,
    BUILTIN_BINANCE_MIDPOINT_SYMBOLS,
    BUILTIN_BINANCE_STAKING_RATE_POLICIES,
    BUILTIN_BINANCE_SYMBOLS,
    BUILTIN_COINGECKO_COIN_IDS,
    BUILTIN_COINGECKO_COMPONENT_SKEW_SECONDS,
    BUILTIN_COINGECKO_HISTORY_SYMBOLS,
    BUILTIN_COINGECKO_NORMALIZATION_COIN_ID,
    BUILTIN_COINGECKO_NORMALIZATION_COMPONENT_SYMBOL,
    BUILTIN_ETHEREUM_EXCHANGE_RATE_POLICIES,
    BUILTIN_FRED_POLICIES,
    BUILTIN_FX_HUB_MAX_AGE_SECONDS,
    BUILTIN_FX_PROVIDER_SYMBOLS,
    BUILTIN_FX_REQUIREMENTS,
    BUILTIN_KRAKEN_MAX_QUOTE_AGE_SECONDS,
    BUILTIN_KRAKEN_SYMBOLS,
    BUILTIN_LIDO_CHAIN_ID,
    BUILTIN_LIDO_CONTRACT_ADDRESS,
    BUILTIN_LIDO_YIELD_POLICIES,
    BUILTIN_LISTED_PROVIDER_SYMBOLS,
    BUILTIN_OKX_INTERNAL_ALIASES,
    BUILTIN_OKX_MARKETS,
    BUILTIN_OKX_YIELD_SYMBOLS,
    BUILTIN_STAKING_RATIO_POLICIES,
    BUILTIN_SYNTHETIC_RECIPES,
    BUILTIN_TWELVE_FX_CACHE_FLOOR_SECONDS,
)


def _default(kwargs: dict[str, Any], name: str, value: Any) -> None:
    if name not in kwargs:
        kwargs[name] = value


def create_builtin_binance_provider(**kwargs: Any) -> Any:
    from .providers.binance import BinanceProvider

    _default(kwargs, "symbol_bindings", BUILTIN_BINANCE_SYMBOLS)
    _default(kwargs, "midpoint_symbols", BUILTIN_BINANCE_MIDPOINT_SYMBOLS)
    return BinanceProvider(**kwargs)


def create_builtin_kraken_provider(**kwargs: Any) -> Any:
    from .providers.kraken import KrakenProvider

    _default(kwargs, "symbol_bindings", BUILTIN_KRAKEN_SYMBOLS)
    _default(
        kwargs,
        "max_quote_ages",
        {
            symbol: timedelta(seconds=seconds)
            for symbol, seconds in BUILTIN_KRAKEN_MAX_QUOTE_AGE_SECONDS.items()
        },
    )
    return KrakenProvider(**kwargs)


def create_builtin_coingecko_provider(api_key: str | None = None, **kwargs: Any) -> Any:
    from .providers.coingecko import CoinGeckoProvider

    _default(kwargs, "coin_ids", BUILTIN_COINGECKO_COIN_IDS)
    _default(kwargs, "history_symbols", BUILTIN_COINGECKO_HISTORY_SYMBOLS)
    _default(
        kwargs,
        "component_skew_limits",
        {
            symbol: timedelta(seconds=seconds)
            for symbol, seconds in BUILTIN_COINGECKO_COMPONENT_SKEW_SECONDS.items()
        },
    )
    normalization_asset, _, _ = BUILTIN_COINGECKO_NORMALIZATION_COMPONENT_SYMBOL.partition(":")
    _default(kwargs, "normalization_quote_asset", normalization_asset)
    _default(kwargs, "normalization_coin_id", BUILTIN_COINGECKO_NORMALIZATION_COIN_ID)
    _default(
        kwargs,
        "normalization_component_symbol",
        BUILTIN_COINGECKO_NORMALIZATION_COMPONENT_SYMBOL,
    )
    return CoinGeckoProvider(api_key, **kwargs)


def create_builtin_okx_market_provider(**kwargs: Any) -> Any:
    from .providers.okx import OkxMarketProvider

    _default(kwargs, "market_bindings", BUILTIN_OKX_MARKETS)
    _default(kwargs, "internal_aliases", BUILTIN_OKX_INTERNAL_ALIASES)
    return OkxMarketProvider(**kwargs)


def create_builtin_okx_yield_provider(**kwargs: Any) -> Any:
    from .providers.okx import OkxBethYieldProvider

    _default(kwargs, "yield_policies", BUILTIN_OKX_YIELD_SYMBOLS)
    return OkxBethYieldProvider(**kwargs)


def create_builtin_alpaca_provider(api_key: str, api_secret: str, **kwargs: Any) -> Any:
    from .providers.alpaca import AlpacaProvider

    _default(kwargs, "symbol_bindings", BUILTIN_LISTED_PROVIDER_SYMBOLS["alpaca"])
    _default(kwargs, "dividend_frequencies", BUILTIN_ALPACA_DIVIDEND_FREQUENCIES)
    _default(
        kwargs,
        "regular_dividend_subtypes",
        BUILTIN_ALPACA_ALLOWED_DIVIDEND_SUBTYPES,
    )
    return AlpacaProvider(api_key, api_secret, **kwargs)


def create_builtin_finnhub_provider(api_key: str, **kwargs: Any) -> Any:
    from .providers.finnhub import FinnhubProvider

    _default(kwargs, "symbol_bindings", BUILTIN_LISTED_PROVIDER_SYMBOLS["finnhub"])
    return FinnhubProvider(api_key, **kwargs)


def create_builtin_twelve_data_provider(api_key: str, **kwargs: Any) -> Any:
    from .providers.twelve_data import TwelveDataProvider

    symbols = {
        **BUILTIN_LISTED_PROVIDER_SYMBOLS["twelve_data"],
        **BUILTIN_FX_PROVIDER_SYMBOLS,
    }
    _default(kwargs, "symbol_bindings", symbols)
    _default(kwargs, "fx_symbols", tuple(BUILTIN_FX_PROVIDER_SYMBOLS))
    _default(
        kwargs,
        "fx_quote_ttl_floors_seconds",
        BUILTIN_TWELVE_FX_CACHE_FLOOR_SECONDS,
    )
    return TwelveDataProvider(api_key, **kwargs)


def create_builtin_alpha_vantage_provider(api_key: str, **kwargs: Any) -> Any:
    from .providers.alpha_vantage import AlphaVantageProvider

    _default(
        kwargs,
        "equity_symbol_bindings",
        BUILTIN_LISTED_PROVIDER_SYMBOLS["alpha_vantage"],
    )
    _default(kwargs, "fx_symbol_bindings", BUILTIN_FX_PROVIDER_SYMBOLS)
    _default(kwargs, "dividend_frequencies", BUILTIN_ALPACA_DIVIDEND_FREQUENCIES)
    return AlphaVantageProvider(api_key, **kwargs)


def create_builtin_fred_provider(api_key: str, **kwargs: Any) -> Any:
    from .providers.fred import FredProvider

    _default(
        kwargs,
        "series_bindings",
        {symbol: str(policy["series"]) for symbol, policy in BUILTIN_FRED_POLICIES.items()},
    )
    _default(
        kwargs,
        "expense_ratios",
        {
            symbol: Decimal(str(policy["expense_ratio_percentage_points"]))
            for symbol, policy in BUILTIN_FRED_POLICIES.items()
        },
    )
    _default(
        kwargs,
        "method_bindings",
        {symbol: str(policy["method"]) for symbol, policy in BUILTIN_FRED_POLICIES.items()},
    )
    _default(
        kwargs,
        "component_role_bindings",
        {symbol: str(policy["component_role"]) for symbol, policy in BUILTIN_FRED_POLICIES.items()},
    )
    return FredProvider(api_key, **kwargs)


def create_builtin_binance_yield_provider(
    api_key: str,
    api_secret: str,
    **kwargs: Any,
) -> Any:
    from .providers.staking import BinanceWbethYieldProvider

    _default(kwargs, "yield_policies", BUILTIN_BINANCE_STAKING_RATE_POLICIES)
    return BinanceWbethYieldProvider(api_key, api_secret, **kwargs)


def create_builtin_ethereum_yield_provider(rpc_urls: Any, **kwargs: Any) -> Any:
    from .providers.staking import EthereumExchangeRateSpec, EthereumExchangeRateYieldProvider

    _default(
        kwargs,
        "specs",
        tuple(
            EthereumExchangeRateSpec(
                symbol=policy.symbol,
                index_symbol=policy.index_symbol,
                underlying_asset=policy.underlying_asset,
                contract_address=policy.contract_address,
                chain_id=policy.chain_id,
                call_data=policy.call_data,
                event_topic=policy.event_topic,
            )
            for policy in BUILTIN_ETHEREUM_EXCHANGE_RATE_POLICIES
        ),
    )
    return EthereumExchangeRateYieldProvider(rpc_urls, **kwargs)


def create_builtin_lido_provider(**kwargs: Any) -> Any:
    from .providers.staking import LidoAprProvider

    _default(kwargs, "yield_policies", BUILTIN_LIDO_YIELD_POLICIES)
    _default(kwargs, "expected_contract_address", BUILTIN_LIDO_CONTRACT_ADDRESS)
    _default(kwargs, "expected_chain_id", BUILTIN_LIDO_CHAIN_ID)
    return LidoAprProvider(**kwargs)


def create_builtin_staking_ratio_provider(history_provider: Any, **kwargs: Any) -> Any:
    from .providers.staking import StakingMarketRatioSpec, StakingMarketRatioYieldProvider

    _default(
        kwargs,
        "specs",
        tuple(
            StakingMarketRatioSpec(
                symbol=policy.symbol,
                staking_pair=policy.staking_pair,
                underlying_pair=policy.underlying_pair,
                underlying_asset=policy.underlying_asset,
                accrual_mode=policy.accrual_mode,
            )
            for policy in BUILTIN_STAKING_RATIO_POLICIES
        ),
    )
    return StakingMarketRatioYieldProvider(history_provider, **kwargs)


def create_builtin_synthetic_recipe(name: str) -> Any:
    from .providers.synthetic import SyntheticRecipe

    policy = BUILTIN_SYNTHETIC_RECIPES[name]
    ages = policy.input_max_age_seconds or (None,) * len(policy.inputs)
    return SyntheticRecipe(
        symbol=policy.symbol,
        left_symbol=policy.inputs[0],
        right_symbol=policy.inputs[-1],
        operation=policy.operation,
        max_skew=timedelta(seconds=policy.max_skew_seconds),
        left_max_age=(None if ages[0] is None else timedelta(seconds=ages[0])),
        right_max_age=(None if ages[-1] is None else timedelta(seconds=ages[-1])),
        provider_name=policy.provider_name,
    )


def builtin_fx_requirements() -> dict[str, tuple[str, ...]]:
    return {symbol: tuple(dependencies) for symbol, dependencies in BUILTIN_FX_REQUIREMENTS.items()}


def builtin_fx_max_ages() -> dict[str, timedelta]:
    return {
        symbol: timedelta(seconds=seconds)
        for symbol, seconds in BUILTIN_FX_HUB_MAX_AGE_SECONDS.items()
    }


__all__ = [
    "builtin_fx_max_ages",
    "builtin_fx_requirements",
    "create_builtin_alpaca_provider",
    "create_builtin_alpha_vantage_provider",
    "create_builtin_binance_provider",
    "create_builtin_binance_yield_provider",
    "create_builtin_coingecko_provider",
    "create_builtin_ethereum_yield_provider",
    "create_builtin_finnhub_provider",
    "create_builtin_fred_provider",
    "create_builtin_kraken_provider",
    "create_builtin_lido_provider",
    "create_builtin_okx_market_provider",
    "create_builtin_okx_yield_provider",
    "create_builtin_staking_ratio_provider",
    "create_builtin_synthetic_recipe",
    "create_builtin_twelve_data_provider",
]
