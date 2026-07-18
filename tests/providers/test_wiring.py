from __future__ import annotations

import pytest

from quickprice.config import Settings
from quickprice.fx import FX_HUB_SYMBOLS, FX_SYMBOLS
from quickprice.plugin_api import (
    AssetClass,
    InstrumentPlugin,
    InstrumentSpec,
    ProviderBinding,
)
from quickprice.providers.alpaca import AlpacaProvider
from quickprice.providers.base import Capability
from quickprice.providers.coingecko import CoinGeckoProvider
from quickprice.providers.fx import UsdHubFxHistoryProvider, UsdHubFxQuoteProvider
from quickprice.providers.staking import (
    BinanceWbethYieldProvider,
    EthereumExchangeRateYieldProvider,
    LidoAprProvider,
    StakingMarketRatioYieldProvider,
)
from quickprice.providers.twelve_data import TwelveDataProvider
from quickprice.providers.wiring import build_provider_graph
from quickprice.registry import InstrumentRegistry


@pytest.mark.asyncio
async def test_wbeth_yield_route_prefers_onchain_then_binance_then_market_ratio() -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        ethereum_rpc_urls=("https://ethereum-mainnet.invalid",),
        binance_api_key="read-only-key",
        binance_api_secret="signing-secret",
        staking_yield_market_fallback_days=30,
    )
    graph = build_provider_graph(settings)
    try:
        chain = graph.router.providers_for("WBETH:USDC", Capability.YIELD)

        assert tuple(type(provider) for provider in chain) == (
            EthereumExchangeRateYieldProvider,
            BinanceWbethYieldProvider,
            StakingMarketRatioYieldProvider,
        )
        assert tuple(provider.name for provider in chain) == (
            "ethereum_exchange_rate",
            "binance_wbeth_rate",
            "staking_market_ratio_proxy",
        )
        assert chain[-1].lookback_days == 30
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_wbeth_yield_route_keeps_market_ratio_as_final_fallback_without_credentials() -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        staking_yield_market_fallback_days=37,
    )
    graph = build_provider_graph(settings)
    try:
        chain = graph.router.providers_for("WBETH:USDC", Capability.YIELD)

        assert len(chain) == 1
        assert isinstance(chain[0], StakingMarketRatioYieldProvider)
        assert chain[0].name == "staking_market_ratio_proxy"
        assert chain[0].lookback_days == 37
        assert "ethereum_exchange_rate" not in graph.providers
        assert "binance_wbeth_rate" not in graph.providers
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_lido_tokens_use_coingecko_prices_and_official_apr_before_ratio_fallback() -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        coingecko_api_key="coingecko-demo-key",
        staking_yield_market_fallback_days=30,
    )
    graph = build_provider_graph(settings)
    try:
        for symbol in ("STETH:USDC", "WSTETH:USDC"):
            quote_chain = graph.router.providers_for(symbol, Capability.QUOTE)
            history_chain = graph.router.providers_for(symbol, Capability.HISTORY)
            yield_chain = graph.router.providers_for(symbol, Capability.YIELD)

            assert len(quote_chain) == len(history_chain) == 1
            assert isinstance(quote_chain[0], CoinGeckoProvider)
            assert history_chain[0] is quote_chain[0]
            assert tuple(type(provider) for provider in yield_chain) == (
                LidoAprProvider,
                StakingMarketRatioYieldProvider,
            )
            assert yield_chain[-1].lookback_days == 30
        for internal_symbol in ("ETH:USD", "STETH:USD", "WSTETH:USD"):
            assert graph.router.providers_for(internal_symbol, Capability.HISTORY) == (
                graph.providers["coingecko"],
            )
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_provider_graph_wires_fx_cache_cadences_and_alpaca_clock_url() -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        twelve_data_api_key="twelve-key",
        usd_cnh_poll_seconds=260,
        usd_hkd_poll_seconds=1_800,
        alpaca_api_key="alpaca-key",
        alpaca_api_secret="alpaca-secret",
        alpaca_trading_base_url="https://clock.example.invalid/v2/",
    )
    graph = build_provider_graph(settings)
    try:
        twelve = graph.providers["twelve_data"]
        alpaca = graph.providers["alpaca"]

        assert isinstance(twelve, TwelveDataProvider)
        assert twelve.quote_cache_ttl_seconds == {
            "USD:EUR": 1_800,
            "USD:GBP": 1_800,
            "USD:HKD": 1_800,
            "USD:SGD": 1_800,
            "USD:CNH": 260,
        }
        for symbol in FX_HUB_SYMBOLS:
            assert graph.router.providers_for(symbol, Capability.QUOTE) == (twelve,)
            assert graph.router.providers_for(symbol, Capability.HISTORY) == (twelve,)
        synthetic_quote = graph.providers["synthetic_fx"]
        synthetic_history = graph.providers["synthetic_fx_history"]
        assert isinstance(synthetic_quote, UsdHubFxQuoteProvider)
        assert isinstance(synthetic_history, UsdHubFxHistoryProvider)
        for symbol in set(FX_SYMBOLS) - set(FX_HUB_SYMBOLS):
            assert graph.router.providers_for(symbol, Capability.QUOTE) == (synthetic_quote,)
            assert graph.router.providers_for(symbol, Capability.HISTORY) == (synthetic_history,)
        assert isinstance(alpaca, AlpacaProvider)
        assert alpaca.trading_base_url == "https://clock.example.invalid/v2"
    finally:
        await graph.close()


def test_strict_graph_validation_reports_missing_public_capabilities() -> None:
    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="incomplete",
                version="1",
                provider_installer=lambda _: None,
                instruments=(
                    InstrumentSpec(
                        symbol="TEST:USD",
                        base="TEST",
                        quote="USD",
                        name="Test Asset",
                        description="An intentionally incomplete provider graph fixture.",
                        asset_class=AssetClass.CRYPTO,
                        asset_type="spot_crypto",
                        price_basis="last_trade",
                    ),
                ),
            ),
        )
    )

    with pytest.raises(RuntimeError, match="TEST:USD/quote"):
        build_provider_graph(Settings(background_enabled=False), registry, strict=True)


def test_declarative_binding_rejects_an_unavailable_provider_name() -> None:
    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="missing-provider",
                version="1",
                provider_installer=lambda _: None,
                instruments=(),
                provider_bindings=(
                    ProviderBinding(
                        symbol="INTERNAL:USD",
                        capability="quote",
                        providers=("not-installed",),
                    ),
                ),
            ),
        )
    )

    with pytest.raises(ValueError, match="not-installed"):
        build_provider_graph(Settings(background_enabled=False), registry)
