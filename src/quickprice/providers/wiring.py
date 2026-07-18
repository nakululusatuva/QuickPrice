"""Default free-first provider graph for QuickPrice's built-in plugin."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from importlib import import_module
from typing import Any

from quickprice.config import Settings
from quickprice.fx import FX_HUB_SYMBOLS, FX_SYMBOLS
from quickprice.plugin_api import ProviderInstallContext, YieldStrategy
from quickprice.registry import InstrumentRegistry, build_registry

from .alpaca import AlpacaProvider
from .alpha_vantage import AlphaVantageProvider
from .base import Capability
from .binance import BinanceProvider
from .coingecko import CoinGeckoProvider
from .fred import FredProvider
from .fx import UsdHubFxHistoryProvider, UsdHubFxQuoteProvider
from .kraken import KrakenProvider
from .quota import daily_budget, rolling_month_safe_daily_budget
from .router import ProviderRouter
from .staking import (
    STETH_MARKET_RATIO_SPEC,
    WBETH_MARKET_RATIO_SPEC,
    WSTETH_MARKET_RATIO_SPEC,
    BinanceWbethYieldProvider,
    EthereumExchangeRateYieldProvider,
    LidoAprProvider,
    StakingMarketRatioYieldProvider,
)
from .synthetic import SyntheticHistoryProvider, SyntheticQuoteProvider, SyntheticRecipe
from .twelve_data import TwelveDataProvider


@dataclass(frozen=True, slots=True)
class ProviderGraph:
    router: ProviderRouter
    providers: dict[str, Any]

    async def close(self) -> None:
        await self.router.close()


def install_builtin_provider_routes(context: ProviderInstallContext) -> None:
    """Install the provider graph owned by QuickPrice's built-in plugin."""

    settings = context.settings
    router = context.router
    providers = context.providers

    binance = providers["binance"] = BinanceProvider()
    kraken = providers["kraken"] = KrakenProvider()
    coingecko = None
    if settings.coingecko_api_key:
        coingecko = providers["coingecko"] = CoinGeckoProvider(
            settings.coingecko_api_key,
            quota=rolling_month_safe_daily_budget(settings.coingecko_monthly_credits),
        )

    for symbol in ("BTC:USDC", "ETH:USDC"):
        quote_chain = [binance, kraken]
        history_chain = [binance, kraken]
        if coingecko is not None:
            quote_chain.append(coingecko)
        router.register(symbol, Capability.QUOTE, quote_chain)
        router.register(symbol, Capability.HISTORY, history_chain)

    if coingecko is not None:
        for symbol in ("STETH:USDC", "WSTETH:USDC"):
            router.register(symbol, Capability.QUOTE, [coingecko])
            router.register(symbol, Capability.HISTORY, [coingecko])
        # Internal USD histories keep the 30-day token/ETH yield proxy on a
        # common quote currency without exposing implementation-only symbols.
        for symbol in ("ETH:USD", "STETH:USD", "WSTETH:USD"):
            router.register(symbol, Capability.HISTORY, [coingecko])

    # Internal component symbols are intentionally not part of the public
    # instrument registry. They can only be reached by the synthetic recipes.
    for symbol in ("WBETH:ETH", "WBETH:USDT", "USDC:USDT"):
        router.register(symbol, Capability.QUOTE, [binance])
        router.register(symbol, Capability.HISTORY, [binance])

    wbeth_primary = SyntheticQuoteProvider(
        router.get_quote,
        (SyntheticRecipe.wbeth_primary(),),
    )
    wbeth_alternate = SyntheticQuoteProvider(
        router.get_quote,
        (SyntheticRecipe.wbeth_usdt_fallback(),),
    )
    providers["synthetic_wbeth_primary"] = wbeth_primary
    providers["synthetic_wbeth_alternate"] = wbeth_alternate
    wbeth_history_primary = SyntheticHistoryProvider(
        router.get_history,
        (SyntheticRecipe.wbeth_primary(),),
    )
    wbeth_history_alternate = SyntheticHistoryProvider(
        router.get_history,
        (SyntheticRecipe.wbeth_usdt_fallback(),),
    )
    providers["synthetic_wbeth_history_primary"] = wbeth_history_primary
    providers["synthetic_wbeth_history_alternate"] = wbeth_history_alternate
    wbeth_chain = [wbeth_primary, wbeth_alternate]
    if coingecko is not None:
        wbeth_chain.append(coingecko)
    router.register("WBETH:USDC", Capability.QUOTE, wbeth_chain)
    wbeth_history_chain = [wbeth_history_primary, wbeth_history_alternate]
    router.register("WBETH:USDC", Capability.HISTORY, wbeth_history_chain)

    wbeth_yield_chain: list[Any] = []
    if settings.ethereum_rpc_urls:
        ethereum_yield = providers["ethereum_exchange_rate"] = EthereumExchangeRateYieldProvider(
            settings.ethereum_rpc_urls,
            request_timeout=settings.provider_timeout_seconds,
        )
        wbeth_yield_chain.append(ethereum_yield)
    if settings.binance_api_key and settings.binance_api_secret:
        binance_yield = providers["binance_wbeth_rate"] = BinanceWbethYieldProvider(
            settings.binance_api_key,
            settings.binance_api_secret,
            request_timeout=settings.provider_timeout_seconds,
        )
        wbeth_yield_chain.append(binance_yield)
    market_ratio_yield = providers["staking_market_ratio_proxy"] = StakingMarketRatioYieldProvider(
        router,
        specs=(
            WBETH_MARKET_RATIO_SPEC,
            STETH_MARKET_RATIO_SPEC,
            WSTETH_MARKET_RATIO_SPEC,
        ),
        lookback_days=settings.staking_yield_market_fallback_days,
    )
    wbeth_yield_chain.append(market_ratio_yield)
    router.register("WBETH:USDC", Capability.YIELD, wbeth_yield_chain)

    lido = providers["lido"] = LidoAprProvider(
        request_timeout=settings.provider_timeout_seconds,
    )
    for symbol in ("STETH:USDC", "WSTETH:USDC"):
        router.register(symbol, Capability.YIELD, [lido, market_ratio_yield])

    alpaca = None
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        alpaca = providers["alpaca"] = AlpacaProvider(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            trading_base_url=settings.alpaca_trading_base_url,
        )
    twelve = None
    if settings.twelve_data_api_key:
        twelve = providers["twelve_data"] = TwelveDataProvider(
            settings.twelve_data_api_key,
            usd_cnh_quote_ttl_seconds=settings.usd_cnh_poll_seconds,
            usd_hkd_quote_ttl_seconds=settings.usd_hkd_poll_seconds,
            quota=daily_budget(
                settings.twelve_daily_credits,
                reserve=min(
                    settings.twelve_fx_reserve_credits,
                    settings.twelve_daily_credits - 1,
                ),
            ),
        )
    alpha = None
    if settings.alpha_vantage_api_key:
        alpha = providers["alpha_vantage"] = AlphaVantageProvider(
            settings.alpha_vantage_api_key,
            quota=daily_budget(settings.alpha_vantage_daily_credits),
        )

    for symbol in ("QQQM:USD", "BOXX:USD", "SGOV:USD"):
        chain = [provider for provider in (alpaca, twelve, alpha) if provider is not None]
        if chain:
            router.register(symbol, Capability.QUOTE, chain)
            router.register(symbol, Capability.HISTORY, chain)

    # Alpha Vantage does not classify ordinary versus special distributions.
    # Keep only classified Alpaca events in the annualization route and retain
    # the last valid event from SQLite when Alpaca is unavailable.
    dividend_chain = [provider for provider in (alpaca,) if provider is not None]
    if dividend_chain:
        router.register("QQQM:USD", Capability.DIVIDEND, dividend_chain)
        router.register("SGOV:USD", Capability.DIVIDEND, dividend_chain)

    fx_chain = [provider for provider in (twelve, alpha) if provider is not None]
    if fx_chain:
        for symbol in FX_HUB_SYMBOLS:
            router.register(symbol, Capability.QUOTE, fx_chain)
            router.register(symbol, Capability.HISTORY, fx_chain)
        synthetic_fx = providers["synthetic_fx"] = UsdHubFxQuoteProvider(router.get_quote)
        synthetic_fx_history = providers["synthetic_fx_history"] = UsdHubFxHistoryProvider(
            router.get_history
        )
        for symbol in FX_SYMBOLS:
            if symbol in FX_HUB_SYMBOLS:
                continue
            router.register(symbol, Capability.QUOTE, [synthetic_fx])
            router.register(symbol, Capability.HISTORY, [synthetic_fx_history])

    if settings.fred_api_key:
        fred = providers["fred"] = FredProvider(settings.fred_api_key)
        router.register("BOXX:USD", Capability.YIELD, [fred])


def _resolve_installer(value: str | Any) -> Any:
    if not isinstance(value, str):
        return value
    module_name, separator, attribute = value.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"invalid provider installer reference: {value!r}")
    installer = getattr(import_module(module_name), attribute)
    if not callable(installer):
        raise TypeError(f"provider installer is not callable: {value!r}")
    return installer


def build_provider_graph(
    settings: Settings,
    registry: InstrumentRegistry | None = None,
    *,
    strict: bool = False,
) -> ProviderGraph:
    """Construct routes declared by the enabled trusted plugins."""

    if registry is None:
        registry = build_registry(settings.enabled_plugins)
    router = ProviderRouter(
        timeout_seconds=settings.provider_timeout_seconds,
        failure_threshold=settings.circuit_failure_threshold,
        half_open_after_seconds=settings.circuit_open_seconds,
    )
    context = ProviderInstallContext(
        settings=settings,
        registry=registry,
        router=router,
    )
    for plugin in registry.plugins:
        if plugin.provider_installer is None:
            continue
        installer = _resolve_installer(plugin.provider_installer)
        installer(context)
    for plugin in registry.plugins:
        for binding in plugin.provider_bindings:
            missing = [name for name in binding.providers if name not in context.providers]
            if missing:
                raise ValueError(
                    f"plugin {plugin.plugin_id} references unavailable providers for "
                    f"{binding.symbol}/{binding.capability}: {', '.join(missing)}"
                )
            chain = [context.providers[name] for name in binding.providers]
            context.register(binding.symbol, binding.capability, chain)

    synthetic_groups: dict[str, list[Any]] = defaultdict(list)
    for plugin in registry.plugins:
        for declaration in plugin.synthetic_recipes:
            synthetic_groups[declaration.symbol.strip().upper()].append(declaration)
    for symbol, declarations in synthetic_groups.items():
        quote_chain: list[Any] = []
        history_chain: list[Any] = []
        for index, declaration in enumerate(declarations):
            recipe = SyntheticRecipe(
                symbol=symbol,
                left_symbol=declaration.left_symbol,
                right_symbol=declaration.right_symbol,
                operation=declaration.operation,
                max_skew=timedelta(seconds=declaration.max_skew_seconds),
                left_max_age=(
                    None
                    if declaration.left_max_age_seconds is None
                    else timedelta(seconds=declaration.left_max_age_seconds)
                ),
                right_max_age=(
                    None
                    if declaration.right_max_age_seconds is None
                    else timedelta(seconds=declaration.right_max_age_seconds)
                ),
                provider_name=declaration.provider_name,
            )
            if declaration.quote_enabled:
                provider = SyntheticQuoteProvider(router.get_quote, (recipe,))
                context.providers[f"{declaration.provider_name}_quote_{symbol}_{index}"] = provider
                quote_chain.append(provider)
            if declaration.history_enabled:
                provider = SyntheticHistoryProvider(router.get_history, (recipe,))
                context.providers[f"{declaration.provider_name}_history_{symbol}_{index}"] = (
                    provider
                )
                history_chain.append(provider)
        if quote_chain:
            router.register(symbol, Capability.QUOTE, quote_chain)
        if history_chain:
            router.register(symbol, Capability.HISTORY, history_chain)
    graph = ProviderGraph(router=router, providers=context.providers)
    if strict:
        validate_provider_graph(graph, registry)
    return graph


def validate_provider_graph(graph: ProviderGraph, registry: InstrumentRegistry) -> None:
    """Require every declared public capability to have an installed route."""

    missing: list[str] = []
    for instrument in registry.values():
        required = [Capability.QUOTE]
        if instrument.history_enabled:
            required.append(Capability.HISTORY)
        if (
            instrument.dividend_strategy is not None
            or instrument.yield_strategy is YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED
        ):
            required.append(Capability.DIVIDEND)
        if instrument.yield_strategy not in {
            None,
            YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED,
        }:
            required.append(Capability.YIELD)
        for capability in required:
            if not graph.router.configured(instrument.symbol, capability):
                missing.append(f"{instrument.symbol}/{capability.value}")
    if missing:
        raise RuntimeError("provider graph is incomplete: " + ", ".join(missing))
