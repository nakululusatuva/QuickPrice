from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from quickprice.config import Settings
from quickprice.plugin_api import AssetClass
from quickprice.providers.base import Capability
from quickprice.providers.binance import BinanceProvider
from quickprice.providers.coingecko import CoinGeckoProvider
from quickprice.providers.kraken import KrakenProvider
from quickprice.providers.wiring import build_provider_graph
from quickprice.registry import INSTRUMENTS


@pytest.mark.parametrize(
    ("symbol", "name", "description"),
    [
        ("SOL:USDC", "Solana", "Solana's native token spot price quoted in USD Coin."),
        (
            "XMR:USDC",
            "Monero",
            "Monero's privacy-focused native asset spot price quoted in USD Coin.",
        ),
        (
            "POL:USDC",
            "Polygon Ecosystem Token",
            "Polygon's native ecosystem token spot price quoted in USD Coin.",
        ),
        ("BNB:USDC", "BNB", "BNB Chain's native token spot price quoted in USD Coin."),
        ("TRX:USDC", "TRON", "TRON's native token spot price quoted in USD Coin."),
    ],
)
def test_extended_crypto_assets_are_plain_spot_instruments(
    symbol: str, name: str, description: str
) -> None:
    instrument = INSTRUMENTS[symbol]

    assert instrument.name == name
    assert instrument.description == description
    assert instrument.asset_class is AssetClass.CRYPTO
    assert instrument.asset_type == "spot_crypto"
    assert instrument.price_basis == "last_trade"
    assert instrument.yield_strategy is None
    assert instrument.reward_accrual_mode is None


@pytest.mark.asyncio
async def test_extended_crypto_routes_keep_aggregator_out_of_history() -> None:
    graph = build_provider_graph(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            coingecko_api_key="demo-key",
        )
    )
    try:
        for symbol in ("SOL:USDC", "BNB:USDC"):
            assert tuple(
                type(provider) for provider in graph.router.providers_for(symbol, Capability.QUOTE)
            ) == (BinanceProvider, KrakenProvider, CoinGeckoProvider)
            assert tuple(
                type(provider)
                for provider in graph.router.providers_for(symbol, Capability.HISTORY)
            ) == (BinanceProvider, KrakenProvider)
        for symbol in ("POL:USDC", "TRX:USDC"):
            assert tuple(
                type(provider) for provider in graph.router.providers_for(symbol, Capability.QUOTE)
            ) == (BinanceProvider, CoinGeckoProvider)
            assert tuple(
                type(provider)
                for provider in graph.router.providers_for(symbol, Capability.HISTORY)
            ) == (BinanceProvider,)
        assert tuple(
            type(provider) for provider in graph.router.providers_for("XMR:USDC", Capability.QUOTE)
        ) == (KrakenProvider, CoinGeckoProvider)
        assert tuple(
            type(provider)
            for provider in graph.router.providers_for("XMR:USDC", Capability.HISTORY)
        ) == (KrakenProvider,)
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_xmr_stale_kraken_trade_falls_back_to_fresh_coingecko_quote() -> None:
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)
    graph = build_provider_graph(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            coingecko_api_key="demo-key",
        )
    )
    try:
        kraken = graph.providers["kraken"]
        kraken._wall_clock = lambda: now
        kraken._request_json = AsyncMock(
            return_value={
                "error": [],
                "result": {
                    "XMRUSDC": [
                        ["325", "1", (now - timedelta(minutes=6)).timestamp(), "b", "m", ""]
                    ],
                    "last": "fixture",
                },
            }
        )
        coingecko = graph.providers["coingecko"]
        coingecko._request_json = AsyncMock(
            return_value={
                "monero": {"usd": 326, "last_updated_at": int(now.timestamp())},
                "usd-coin": {"usd": 1, "last_updated_at": int(now.timestamp())},
            }
        )

        result = await graph.router.get_quote("XMR:USDC")

        assert result.provider == "coingecko"
        assert result.price == 326
        assert result.fallback_level == 1
    finally:
        await graph.close()
