"""Default free-first provider graph for QuickPrice's built-in plugin."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from importlib import import_module
from typing import Any

from quickprice.config import Settings
from quickprice.instrument_policy import (
    BUILTIN_COINGECKO_HISTORY_SYMBOLS,
    BUILTIN_PROVIDER_ROUTES,
    BUILTIN_SYNTHETIC_RECIPES,
    BUILTIN_TWELVE_FX_POLL_SETTING,
)
from quickprice.metrics import Metrics
from quickprice.plugin_api import ProviderInstallContext, YieldStrategy
from quickprice.provider_factory import (
    builtin_fx_max_ages,
    builtin_fx_requirements,
    create_builtin_alpaca_provider,
    create_builtin_alpha_vantage_provider,
    create_builtin_binance_provider,
    create_builtin_binance_yield_provider,
    create_builtin_coingecko_provider,
    create_builtin_ethereum_yield_provider,
    create_builtin_finnhub_provider,
    create_builtin_fred_provider,
    create_builtin_kraken_provider,
    create_builtin_lido_provider,
    create_builtin_okx_market_provider,
    create_builtin_okx_yield_provider,
    create_builtin_staking_ratio_provider,
    create_builtin_synthetic_recipe,
    create_builtin_twelve_data_provider,
)
from quickprice.registry import InstrumentRegistry, build_registry

from .base import Capability
from .fx import UsdHubFxHistoryProvider, UsdHubFxQuoteProvider
from .quota import daily_budget, minute_budget, rolling_month_safe_daily_budget
from .router import ProviderRouter
from .synthetic import SyntheticHistoryProvider, SyntheticQuoteProvider, SyntheticRecipe


@dataclass(frozen=True, slots=True)
class ProviderGraph:
    router: ProviderRouter
    providers: dict[str, Any]

    async def close(self, *, exclude_providers: Iterable[Any] = ()) -> None:
        await self.router.close(exclude_providers=exclude_providers)


def _proxy_options(settings: Settings, provider_name: str) -> dict[str, str]:
    proxy_url = settings.proxy_url_for_provider(provider_name)
    return {"proxy_url": proxy_url} if proxy_url else {}


def install_builtin_provider_routes(context: ProviderInstallContext) -> None:
    """Install the provider graph owned by QuickPrice's built-in plugin."""

    settings = context.settings
    router = context.router
    providers = context.providers

    providers["binance"] = create_builtin_binance_provider(**_proxy_options(settings, "binance"))
    providers["kraken"] = create_builtin_kraken_provider(**_proxy_options(settings, "kraken"))
    providers["okx"] = create_builtin_okx_market_provider(
        request_timeout=settings.provider_timeout_seconds,
        **_proxy_options(settings, "okx"),
    )
    providers["okx_beth_yield"] = create_builtin_okx_yield_provider(
        request_timeout=settings.provider_timeout_seconds,
        **_proxy_options(settings, "okx"),
    )
    providers["lido"] = create_builtin_lido_provider(
        request_timeout=settings.provider_timeout_seconds,
        **_proxy_options(settings, "lido"),
    )
    providers["staking_market_ratio_proxy"] = create_builtin_staking_ratio_provider(
        router,
        lookback_days=settings.staking_yield_market_fallback_days,
    )

    if settings.binance_api_key and settings.binance_api_secret:
        providers["binance_wbeth_rate"] = create_builtin_binance_yield_provider(
            settings.binance_api_key,
            settings.binance_api_secret,
            request_timeout=settings.provider_timeout_seconds,
            **_proxy_options(settings, "binance_wbeth_rate"),
        )
    if settings.ethereum_rpc_urls:
        providers["ethereum_exchange_rate"] = create_builtin_ethereum_yield_provider(
            settings.ethereum_rpc_urls,
            request_timeout=settings.provider_timeout_seconds,
            **_proxy_options(settings, "ethereum_exchange_rate"),
        )
    if settings.coingecko_api_key:
        providers["coingecko"] = create_builtin_coingecko_provider(
            settings.coingecko_api_key,
            quota=rolling_month_safe_daily_budget(settings.coingecko_monthly_credits),
            **_proxy_options(settings, "coingecko"),
        )
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        providers["alpaca"] = create_builtin_alpaca_provider(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            trading_base_url=settings.alpaca_trading_base_url,
            stream_symbol_limit=settings.alpaca_stream_symbol_limit,
            rest_calls_per_minute=settings.alpaca_rest_calls_per_minute,
            **_proxy_options(settings, "alpaca"),
        )
    if settings.finnhub_api_key:
        providers["finnhub"] = create_builtin_finnhub_provider(
            settings.finnhub_api_key,
            quota=minute_budget(settings.finnhub_calls_per_minute),
            **_proxy_options(settings, "finnhub"),
        )
    if settings.twelve_data_api_key:
        fx_ttls = {
            symbol: float(getattr(settings, setting_name))
            for symbol, setting_name in BUILTIN_TWELVE_FX_POLL_SETTING.items()
        }
        providers["twelve_data"] = create_builtin_twelve_data_provider(
            settings.twelve_data_api_key,
            fx_quote_ttl_seconds=fx_ttls,
            calls_per_minute=settings.twelve_calls_per_minute,
            rate_gate_timeout_seconds=settings.twelve_rate_gate_timeout_seconds,
            request_timeout=settings.provider_timeout_seconds,
            quota=daily_budget(
                settings.twelve_daily_credits,
                reserve=min(
                    settings.twelve_fx_reserve_credits,
                    settings.twelve_daily_credits - 1,
                ),
            ),
            **_proxy_options(settings, "twelve_data"),
        )
    if settings.alpha_vantage_api_key:
        providers["alpha_vantage"] = create_builtin_alpha_vantage_provider(
            settings.alpha_vantage_api_key,
            quota=daily_budget(settings.alpha_vantage_daily_credits),
            **_proxy_options(settings, "alpha_vantage"),
        )
    if settings.fred_api_key:
        providers["fred"] = create_builtin_fred_provider(
            settings.fred_api_key,
            **_proxy_options(settings, "fred"),
        )

    # Internal components remain unreachable from the public registry.  Their
    # routes are derived from the same managed recipe policies as public routes.
    for recipe_name, policy in BUILTIN_SYNTHETIC_RECIPES.items():
        source_name = "okx" if policy.provider_name.endswith("okx") else "binance"
        source = providers[source_name]
        for dependency in policy.inputs:
            router.replace(dependency, Capability.QUOTE, (source,))
            router.replace(dependency, Capability.HISTORY, (source,))
        recipe = create_builtin_synthetic_recipe(recipe_name)
        stem, _, variant = recipe_name.rpartition("_")
        quote_name = f"synthetic_{recipe_name}"
        history_name = f"synthetic_{stem}_history_{variant}"
        providers[quote_name] = SyntheticQuoteProvider(router.get_quote, (recipe,))
        providers[history_name] = SyntheticHistoryProvider(router.get_history, (recipe,))

    coingecko = providers.get("coingecko")
    if coingecko is not None:
        for symbol in BUILTIN_COINGECKO_HISTORY_SYMBOLS:
            if symbol not in BUILTIN_PROVIDER_ROUTES:
                router.replace(symbol, Capability.HISTORY, (coingecko,))

    fx_sources = tuple(
        providers[name] for name in ("twelve_data", "alpha_vantage") if name in providers
    )
    if fx_sources:
        providers["synthetic_fx"] = UsdHubFxQuoteProvider(
            router.get_quote,
            requirements=builtin_fx_requirements(),
            max_ages=builtin_fx_max_ages(),
        )
        providers["synthetic_fx_history"] = UsdHubFxHistoryProvider(
            router.get_history,
            requirements=builtin_fx_requirements(),
        )

    for symbol, route_policy in BUILTIN_PROVIDER_ROUTES.items():
        for raw_capability, provider_names in route_policy.items():
            capability = Capability(raw_capability)
            chain = tuple(providers[name] for name in provider_names if name in providers)
            if chain:
                router.replace(symbol, capability, chain)


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
    metrics: Metrics | None = None,
) -> ProviderGraph:
    """Construct routes declared by the enabled trusted plugins."""

    if registry is None:
        registry = build_registry(settings.enabled_plugins)
    router = ProviderRouter(
        timeout_seconds=settings.provider_timeout_seconds,
        failure_threshold=settings.circuit_failure_threshold,
        half_open_after_seconds=settings.circuit_open_seconds,
        metrics=metrics,
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
    if metrics is not None:
        for provider in context.providers.values():
            provider_name = str(getattr(provider, "name", provider.__class__.__name__))
            metrics.register_provider(provider_name)
            set_metrics = getattr(provider, "set_metrics", None)
            if callable(set_metrics):
                set_metrics(metrics)
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
