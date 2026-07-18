from __future__ import annotations

import pytest

from quickprice.config import Settings
from quickprice.plugin_api import AssetClass
from quickprice.providers.base import Capability
from quickprice.providers.binance import BinanceProvider
from quickprice.providers.coingecko import CoinGeckoProvider
from quickprice.providers.kraken import KrakenProvider
from quickprice.providers.wiring import build_provider_graph
from quickprice.registry import INSTRUMENTS


def test_sol_and_xmr_are_plain_spot_crypto_instruments() -> None:
    sol = INSTRUMENTS["SOL:USDC"]
    xmr = INSTRUMENTS["XMR:USDC"]

    assert sol.name == "Solana"
    assert xmr.name == "Monero"
    assert sol.asset_class is xmr.asset_class is AssetClass.CRYPTO
    assert sol.asset_type == xmr.asset_type == "spot_crypto"
    assert sol.price_basis == xmr.price_basis == "last_trade"
    assert sol.yield_strategy is xmr.yield_strategy is None
    assert sol.reward_accrual_mode is xmr.reward_accrual_mode is None


@pytest.mark.asyncio
async def test_sol_and_xmr_routes_keep_aggregator_out_of_history() -> None:
    graph = build_provider_graph(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            coingecko_api_key="demo-key",
        )
    )
    try:
        assert tuple(
            type(provider) for provider in graph.router.providers_for("SOL:USDC", Capability.QUOTE)
        ) == (BinanceProvider, KrakenProvider, CoinGeckoProvider)
        assert tuple(
            type(provider)
            for provider in graph.router.providers_for("SOL:USDC", Capability.HISTORY)
        ) == (BinanceProvider, KrakenProvider)
        assert tuple(
            type(provider) for provider in graph.router.providers_for("XMR:USDC", Capability.QUOTE)
        ) == (KrakenProvider, CoinGeckoProvider)
        assert tuple(
            type(provider)
            for provider in graph.router.providers_for("XMR:USDC", Capability.HISTORY)
        ) == (KrakenProvider,)
    finally:
        await graph.close()
