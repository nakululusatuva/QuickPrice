from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import aiohttp
import pytest

from quickprice.analytics import calculate_changes
from quickprice.domain import PricePoint, ProviderQuote
from quickprice.provider_factory import (
    create_builtin_alpaca_provider,
    create_builtin_alpha_vantage_provider,
    create_builtin_binance_provider,
    create_builtin_coingecko_provider,
    create_builtin_finnhub_provider,
    create_builtin_fred_provider,
    create_builtin_kraken_provider,
    create_builtin_twelve_data_provider,
)
from quickprice.providers.alpaca import AlpacaProvider
from quickprice.providers.alpha_vantage import AlphaVantageProvider
from quickprice.providers.base import (
    AllProvidersFailed,
    Capability,
    HttpProvider,
    MalformedResponse,
    ProviderUnavailable,
    UnsupportedInstrument,
)
from quickprice.providers.binance import (
    BINANCE_MAX_STREAM_CONNECTIONS,
    BINANCE_STREAM_PATH_MAX_CHARACTERS,
    BINANCE_STREAMS_PER_CONNECTION,
    BinanceProvider,
)
from quickprice.providers.coingecko import (
    COINGECKO_SIMPLE_PRICE_ID_CHARACTERS_PER_REQUEST,
    COINGECKO_SIMPLE_PRICE_IDS_PER_REQUEST,
    CoinGeckoProvider,
    coingecko_simple_price_id_batches,
)
from quickprice.providers.finnhub import FinnhubProvider
from quickprice.providers.kraken import KRAKEN_SYMBOLS_PER_SUBSCRIPTION, KrakenProvider
from quickprice.providers.router import ProviderRouter


@pytest.mark.asyncio
async def test_binance_quote_and_unadjusted_kline_contract(fixture_json):
    provider = create_builtin_binance_provider()
    provider._request_json = AsyncMock(
        side_effect=[fixture_json("binance_quote.json"), fixture_json("binance_history.json")]
    )

    latest = await provider.get_quote("BTC:USDC")
    points = await provider.get_history(
        "BTC:USDC",
        interval="1m",
        start=datetime(2026, 7, 20, tzinfo=UTC),
        end=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert latest.price == Decimal("118250.50")
    assert latest.feed == "binance_spot"
    assert latest.price_basis == "last_trade"
    assert [point.price for point in points] == [Decimal("118200.25"), Decimal("118250.50")]
    assert points[0].timestamp.second == 0
    assert points[1].timestamp - points[0].timestamp == timedelta(minutes=1)


def test_crypto_streams_suppress_duplicate_rest_only_while_fresh() -> None:
    for provider in (create_builtin_binance_provider(), create_builtin_kraken_provider()):
        assert provider.stream_poll_suppression_seconds == 120.0
        assert provider.stream_poll_recheck_seconds == 10.0


@pytest.mark.asyncio
async def test_binance_internal_wbeth_leg_uses_current_book_midpoint():
    observed_at = datetime(2026, 7, 20, 15, 30, tzinfo=UTC)
    provider = create_builtin_binance_provider(wall_clock=lambda: observed_at)
    provider._request_json = AsyncMock(
        return_value={"symbol": "WBETHETH", "bidPrice": "1.1022", "askPrice": "1.1024"}
    )

    result = await provider.get_quote("WBETH:ETH")

    assert result.price == Decimal("1.1023")
    assert result.as_of == observed_at
    assert result.price_basis == "midpoint"
    assert result.feed == "binance_spot_book"
    assert result.is_derived is True
    assert [item.role for item in result.components] == ["best_bid", "best_ask"]
    assert [item.price for item in result.components] == [Decimal("1.1022"), Decimal("1.1024")]
    assert provider._request_json.await_args.args[1].endswith("/api/v3/ticker/bookTicker")


@pytest.mark.asyncio
async def test_binance_wide_internal_book_is_rejected_and_routed_to_fallback():
    observed_at = datetime(2026, 7, 20, 15, 30, tzinfo=UTC)
    provider = create_builtin_binance_provider(wall_clock=lambda: observed_at)
    provider._request_json = AsyncMock(
        return_value={"symbol": "WBETHETH", "bidPrice": "1.00", "askPrice": "1.02"}
    )

    class Backup:
        name = "backup"

        async def get_quote(self, symbol):
            return ProviderQuote(
                symbol,
                Decimal("1.01"),
                observed_at,
                self.name,
                "fixture",
            )

    router = ProviderRouter({("WBETH:ETH", Capability.QUOTE): [provider, Backup()]})
    try:
        result = await router.get_quote("WBETH:ETH")
    finally:
        await router.close()

    assert result.provider == "backup"
    assert result.fallback_level == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "exchange_symbol"),
    [
        ("SOL:USDC", "SOLUSDC"),
        ("POL:USDC", "POLUSDC"),
        ("BNB:USDC", "BNBUSDC"),
        ("TRX:USDC", "TRXUSDC"),
    ],
)
async def test_binance_extended_crypto_uses_canonical_usdc_markets(
    fixture_json, symbol: str, exchange_symbol: str
):
    provider = create_builtin_binance_provider()
    provider._request_json = AsyncMock(
        side_effect=[fixture_json("binance_quote.json"), fixture_json("binance_history.json")]
    )

    latest = await provider.get_quote(symbol)
    points = await provider.get_history(
        symbol,
        interval="5m",
        start=datetime(2026, 7, 20, tzinfo=UTC),
        end=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert latest.symbol == symbol
    assert all(point.symbol == symbol for point in points)
    assert provider._request_json.await_args_list[0].kwargs["params"]["symbol"] == exchange_symbol
    assert provider._request_json.await_args_list[1].kwargs["params"]["symbol"] == exchange_symbol


@pytest.mark.asyncio
async def test_binance_rejects_xmr_without_a_supported_market():
    provider = create_builtin_binance_provider()

    with pytest.raises(UnsupportedInstrument):
        await provider.get_quote("XMR:USDC")


def test_binance_shards_two_thousand_dynamic_stream_bindings_safely() -> None:
    bindings = {f"B{index:04d}:USDC": f"B{index:04d}USDC" for index in range(2_000)}
    provider = BinanceProvider(symbol_bindings=bindings)

    batches = provider.stream_connection_batches(tuple(bindings))

    assert len(batches) <= BINANCE_MAX_STREAM_CONNECTIONS
    assert tuple(symbol for batch in batches for symbol in batch) == tuple(bindings)
    for batch in batches:
        assert len(batch) <= BINANCE_STREAMS_PER_CONNECTION
        stream_path = "/".join(
            f"{provider._exchange_symbol(symbol).lower()}@trade" for symbol in batch
        )
        assert len(stream_path) <= BINANCE_STREAM_PATH_MAX_CHARACTERS


@pytest.mark.asyncio
async def test_kraken_trade_contract(fixture_json):
    provider = create_builtin_kraken_provider()
    provider._request_json = AsyncMock(return_value=fixture_json("kraken_quote.json"))

    result = await provider.get_quote("BTC:USDC")

    assert result.provider == "kraken"
    assert result.price == Decimal("118249.90")
    assert result.as_of.tzinfo is UTC


@pytest.mark.asyncio
async def test_kraken_sol_and_xmr_use_canonical_rest_pairs(fixture_json):
    provider = create_builtin_kraken_provider(
        wall_clock=lambda: datetime(2026, 7, 20, 2, 1, tzinfo=UTC)
    )
    timestamp = int(datetime(2026, 7, 20, 1, tzinfo=UTC).timestamp())
    provider._request_json = AsyncMock(
        side_effect=[
            fixture_json("kraken_quote.json"),
            {
                "error": [],
                "result": {
                    "SOLUSDC": [[timestamp, "180", "181", "179", "180.5", "180.2", "10", 5]],
                    "last": timestamp,
                },
            },
        ]
    )

    latest = await provider.get_quote("XMR:USDC")
    points = await provider.get_history(
        "SOL:USDC",
        interval="5m",
        start=datetime(2026, 7, 20, tzinfo=UTC),
        end=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert latest.symbol == "XMR:USDC"
    assert points[0].symbol == "SOL:USDC"
    assert points[0].price == Decimal("180.5")
    assert provider._request_json.await_args_list[0].kwargs["params"]["pair"] == "XMRUSDC"
    assert provider._request_json.await_args_list[1].kwargs["params"]["pair"] == "SOLUSDC"
    assert provider.symbols["SOL:USDC"][1] == "SOL/USDC"
    assert provider.symbols["XMR:USDC"][1] == "XMR/USDC"


def test_kraken_bnb_uses_canonical_usdc_pair() -> None:
    provider = create_builtin_kraken_provider()

    assert provider.symbols["BNB:USDC"] == ("BNBUSDC", "BNB/USDC")


def test_kraken_batches_two_thousand_subscriptions_on_one_connection() -> None:
    bindings = {
        f"K{index:04d}:USDC": (f"K{index:04d}USDC", f"K{index:04d}/USDC") for index in range(2_000)
    }
    provider = KrakenProvider(symbol_bindings=bindings)

    batches = provider.stream_subscription_batches(tuple(bindings))

    assert len(batches) == 2_000 // KRAKEN_SYMBOLS_PER_SUBSCRIPTION
    assert all(len(batch) <= KRAKEN_SYMBOLS_PER_SUBSCRIPTION for batch in batches)
    assert tuple(symbol for batch in batches for symbol in batch) == tuple(
        pair[1] for pair in bindings.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["POL:USDC", "TRX:USDC"])
async def test_kraken_rejects_assets_without_usdc_markets(symbol: str) -> None:
    provider = create_builtin_kraken_provider()

    with pytest.raises(UnsupportedInstrument):
        await provider.get_quote(symbol)


@pytest.mark.asyncio
async def test_coingecko_quote_is_usdc_ratio_with_components(fixture_json):
    provider = create_builtin_coingecko_provider("demo")
    provider._request_json = AsyncMock(return_value=fixture_json("coingecko_quote.json"))

    result = await provider.get_quote("WBETH:USDC")

    assert result.price == Decimal("4125.0") / Decimal("0.9998")
    assert result.is_derived is True
    assert [item.role for item in result.components] == ["numerator", "denominator"]


@pytest.mark.asyncio
async def test_coingecko_quote_supports_direct_usd_without_usdc_normalization():
    provider = CoinGeckoProvider(
        "demo",
        coin_ids={"AVAX:USD": "avalanche-2"},
    )
    provider._request_json = AsyncMock(
        return_value={
            "avalanche-2": {"usd": "42.25", "last_updated_at": 1_768_000_000},
            "usd-coin": {"usd": "0.999", "last_updated_at": 1_768_000_000},
        }
    )

    result = await provider.get_quote("AVAX:USD")

    assert result.price == Decimal("42.25")
    assert result.price_basis == "aggregated_spot"
    assert result.is_derived is False
    assert result.components == ()


@pytest.mark.asyncio
async def test_coingecko_allows_subminute_wbeth_component_skew(fixture_json):
    payload = fixture_json("coingecko_quote.json")
    payload["usd-coin"]["last_updated_at"] -= 30
    provider = create_builtin_coingecko_provider("key")
    provider._request_json = AsyncMock(return_value=payload)

    result = await provider.get_quote("WBETH:USDC")

    assert result.components[0].as_of - result.components[1].as_of == timedelta(seconds=32)


@pytest.mark.asyncio
async def test_coingecko_rejects_wbeth_component_skew_over_one_minute(fixture_json):
    payload = fixture_json("coingecko_quote.json")
    payload["usd-coin"]["last_updated_at"] -= 60
    provider = create_builtin_coingecko_provider("key")
    provider._request_json = AsyncMock(return_value=payload)

    with pytest.raises(MalformedResponse, match="configured normalization limit"):
        await provider.get_quote("WBETH:USDC")


@pytest.mark.asyncio
async def test_coingecko_clamps_history_to_demo_365_day_boundary():
    end = datetime(2026, 7, 20, 15, tzinfo=UTC)
    boundary = end - timedelta(days=365)
    provider = create_builtin_coingecko_provider("key")
    provider._request_json = AsyncMock(
        side_effect=[
            {"prices": [[int((boundary + timedelta(hours=9)).timestamp() * 1000), 4000.0]]},
            {"market_data": {"current_price": {"usd": 3900.0}}},
        ]
    )

    result = await provider.get_history(
        "ETH:USD",
        interval="1d",
        start=end - timedelta(days=400),
        end=end,
    )

    params = provider._request_json.await_args_list[0].kwargs["params"]
    assert params["from"] == int(boundary.timestamp())
    assert params["to"] - params["from"] == 365 * 24 * 60 * 60
    assert result[0].timestamp == boundary.replace(hour=0, minute=0, second=0, microsecond=0)
    assert result[0].price == Decimal("3900.0")
    snapshot_params = provider._request_json.await_args_list[1].kwargs["params"]
    assert snapshot_params == {"date": "20-07-2025", "localization": "false"}
    one_year = calculate_changes(Decimal("4100"), end, result)["1y"]
    assert one_year is not None
    assert one_year.reference_as_of == result[0].timestamp


@pytest.mark.asyncio
async def test_coingecko_daily_boundary_uses_short_failure_and_long_success_ttls():
    monotonic = [0.0]
    provider = create_builtin_coingecko_provider("key", clock=lambda: monotonic[0])
    provider._request_json = AsyncMock(
        side_effect=[
            ProviderUnavailable("coingecko", "temporary failure"),
            {"market_data": {"current_price": {"usd": 3900.0}}},
            {"market_data": {"current_price": {"usd": 3950.0}}},
        ]
    )

    with pytest.raises(ProviderUnavailable, match="temporary failure"):
        await provider._daily_snapshot_usd_price("ethereum", "20-07-2025")
    monotonic[0] = provider.daily_snapshot_error_ttl_seconds - 1
    with pytest.raises(ProviderUnavailable, match="temporary failure"):
        await provider._daily_snapshot_usd_price("ethereum", "20-07-2025")
    assert provider._request_json.await_count == 1

    monotonic[0] = provider.daily_snapshot_error_ttl_seconds + 1
    assert await provider._daily_snapshot_usd_price("ethereum", "20-07-2025") == Decimal("3900.0")
    assert provider._request_json.await_count == 2

    monotonic[0] += provider.daily_snapshot_success_ttl_seconds - 1
    assert await provider._daily_snapshot_usd_price("ethereum", "20-07-2025") == Decimal("3900.0")
    assert provider._request_json.await_count == 2

    monotonic[0] += 2
    assert await provider._daily_snapshot_usd_price("ethereum", "20-07-2025") == Decimal("3950.0")
    assert provider._request_json.await_count == 3


@pytest.mark.asyncio
async def test_coingecko_allows_slow_aggregated_staking_component_updates():
    timestamp = 1_768_000_000
    provider = create_builtin_coingecko_provider("key")
    provider._request_json = AsyncMock(
        return_value={
            "wrapped-steth": {"usd": 4800, "last_updated_at": timestamp - 12},
            "usd-coin": {"usd": 1, "last_updated_at": timestamp},
        }
    )

    result = await provider.get_quote("WSTETH:USDC")

    assert result.price == Decimal("4800")
    assert result.as_of == datetime.fromtimestamp(timestamp - 12, tz=UTC)


@pytest.mark.asyncio
async def test_coingecko_batches_all_fallback_symbols_behind_one_refresh():
    timestamp = 1_768_000_000
    payload = {
        "bitcoin": {"usd": 100_000, "last_updated_at": timestamp},
        "ethereum": {"usd": 4_000, "last_updated_at": timestamp},
        "solana": {"usd": 180, "last_updated_at": timestamp},
        "monero": {"usd": 325, "last_updated_at": timestamp},
        "polygon-ecosystem-token": {"usd": 0.25, "last_updated_at": timestamp},
        "binancecoin": {"usd": 800, "last_updated_at": timestamp},
        "tron": {"usd": 0.30, "last_updated_at": timestamp},
        "wrapped-beacon-eth": {"usd": 4_500, "last_updated_at": timestamp},
        "okx-beth": {"usd": 3_995, "last_updated_at": timestamp},
        "staked-ether": {"usd": 3_990, "last_updated_at": timestamp},
        "wrapped-steth": {"usd": 4_800, "last_updated_at": timestamp},
        "usd-coin": {"usd": 1, "last_updated_at": timestamp},
    }
    provider = create_builtin_coingecko_provider("key", clock=lambda: 10.0)

    async def request_json(*_args, **_kwargs):
        await asyncio.sleep(0)
        return payload

    provider._request_json = AsyncMock(side_effect=request_json)

    results = await asyncio.gather(
        provider.get_quote("BTC:USDC"),
        provider.get_quote("ETH:USDC"),
        provider.get_quote("SOL:USDC"),
        provider.get_quote("XMR:USDC"),
        provider.get_quote("POL:USDC"),
        provider.get_quote("BNB:USDC"),
        provider.get_quote("TRX:USDC"),
        provider.get_quote("WBETH:USDC"),
        provider.get_quote("BETH:USDC"),
        provider.get_quote("STETH:USDC"),
        provider.get_quote("WSTETH:USDC"),
    )

    assert len(results) == 11
    assert results[2].price == Decimal("180")
    assert results[3].price == Decimal("325")
    assert results[4].price == Decimal("0.25")
    assert results[5].price == Decimal("800")
    assert results[6].price == Decimal("0.30")
    assert provider._request_json.await_count == 1
    requested_ids = provider._request_json.await_args.kwargs["params"]["ids"].split(",")
    assert set(requested_ids) == {
        "bitcoin",
        "ethereum",
        "solana",
        "monero",
        "polygon-ecosystem-token",
        "binancecoin",
        "tron",
        "wrapped-beacon-eth",
        "okx-beth",
        "staked-ether",
        "wrapped-steth",
        "usd-coin",
    }


@pytest.mark.asyncio
async def test_coingecko_batches_and_merges_two_thousand_dynamic_ids_without_network() -> None:
    coin_ids = {f"C{index:04d}:USDC": f"coin-{index:04d}" for index in range(2_000)}
    expected_ids = {*coin_ids.values(), "usd-coin"}
    provider = CoinGeckoProvider(
        "key",
        coin_ids=coin_ids,
        normalization_quote_asset="USDC",
        normalization_coin_id="usd-coin",
        normalization_component_symbol="USDC:USD",
        clock=lambda: 10.0,
    )

    async def request_json(*_args, **kwargs):
        requested = kwargs["params"]["ids"].split(",")
        return {coin_id: {"usd": 1, "last_updated_at": 1_768_000_000} for coin_id in requested}

    provider._request_json = AsyncMock(side_effect=request_json)

    document = await provider._simple_prices()
    batches = coingecko_simple_price_id_batches(tuple(expected_ids))

    assert set(document) == expected_ids
    assert provider._request_json.await_count == len(batches)
    assert len(batches) == 9
    assert all(len(batch) <= COINGECKO_SIMPLE_PRICE_IDS_PER_REQUEST for batch in batches)
    assert all(
        len(",".join(batch)) <= COINGECKO_SIMPLE_PRICE_ID_CHARACTERS_PER_REQUEST
        for batch in batches
    )
    assert all(
        call.kwargs["allow_quota_reserve"] is True
        for call in provider._request_json.await_args_list
    )


@pytest.mark.asyncio
async def test_coingecko_exposes_negative_cache_retry_and_backs_off_repeated_failures():
    monotonic = [0.0]
    timestamp = 1_768_000_000
    provider = create_builtin_coingecko_provider(
        "key",
        cache_ttl_seconds=300,
        maximum_error_cache_ttl_seconds=3600,
        clock=lambda: monotonic[0],
    )
    provider._request_json = AsyncMock(
        side_effect=[
            ProviderUnavailable("coingecko", "first failure"),
            ProviderUnavailable("coingecko", "second failure"),
            {
                "wrapped-steth": {"usd": 4_800, "last_updated_at": timestamp},
                "usd-coin": {"usd": 1, "last_updated_at": timestamp},
            },
        ]
    )

    with pytest.raises(ProviderUnavailable, match="first failure"):
        await provider.get_quote("WSTETH:USDC")
    assert provider.quote_failure_retry_after_seconds() == 300

    monotonic[0] = 299
    with pytest.raises(ProviderUnavailable, match="first failure"):
        await provider.get_quote("WSTETH:USDC")
    assert provider._request_json.await_count == 1
    assert provider.quote_failure_retry_after_seconds() == 1

    monotonic[0] = 300
    with pytest.raises(ProviderUnavailable, match="second failure"):
        await provider.get_quote("WSTETH:USDC")
    assert provider._request_json.await_count == 2
    assert provider.quote_failure_retry_after_seconds() == 600

    monotonic[0] = 900
    result = await provider.get_quote("WSTETH:USDC")
    assert result.price == Decimal("4800")
    assert provider._request_json.await_count == 3
    assert provider.quote_failure_retry_after_seconds() is None


@pytest.mark.asyncio
async def test_coingecko_does_not_claim_intraday_history_support():
    provider = create_builtin_coingecko_provider("key")

    with pytest.raises(UnsupportedInstrument, match="only for configured"):
        await provider.get_history(
            "BTC:USDC",
            interval="1m",
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 7, 2, tzinfo=UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol",
    ["SOL:USDC", "XMR:USDC", "POL:USDC", "BNB:USDC", "TRX:USDC"],
)
async def test_coingecko_is_quote_only_for_ordinary_spot_fallbacks(symbol: str):
    provider = create_builtin_coingecko_provider("key")

    with pytest.raises(UnsupportedInstrument, match="only for configured"):
        await provider.get_history(
            symbol,
            interval="1d",
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 7, 2, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_coingecko_liquid_staking_history_is_normalized_to_usdc():
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = start + timedelta(days=2)
    timestamp = int((start + timedelta(hours=1)).timestamp())
    provider = create_builtin_coingecko_provider("key", clock=lambda: 10.0)
    provider._request_json = AsyncMock(
        side_effect=[
            {
                "prices": [
                    [timestamp * 1000, 3000],
                    [int(end.timestamp() * 1000), 3030],
                ]
            },
            {
                "staked-ether": {"usd": 3030, "last_updated_at": int(end.timestamp())},
                "wrapped-steth": {"usd": 3600, "last_updated_at": int(end.timestamp())},
                "usd-coin": {"usd": "0.999", "last_updated_at": int(end.timestamp())},
            },
        ]
    )

    points = await provider.get_history(
        "STETH:USDC",
        interval="5m",
        start=start,
        end=end,
    )

    assert [point.price for point in points] == [
        Decimal("3000") / Decimal("0.999"),
        Decimal("3030") / Decimal("0.999"),
    ]
    assert all(point.is_derived for point in points)
    assert all(point.interval == "5m" for point in points)
    market_call = provider._request_json.await_args_list[0]
    assert market_call.kwargs["params"]["vs_currency"] == "usd"
    assert "interval" not in market_call.kwargs["params"]


@pytest.mark.asyncio
async def test_coingecko_internal_usd_history_does_not_consume_usdc_quote_refresh():
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    provider = create_builtin_coingecko_provider("key")
    provider._request_json = AsyncMock(
        return_value={"prices": [[int(start.timestamp() * 1000), "3000"]]}
    )

    points = await provider.get_history(
        "STETH:USD",
        interval="1d",
        start=start,
        end=end,
    )

    assert len(points) == 1
    assert points[0].price == Decimal("3000")
    assert points[0].is_derived is False
    assert provider._request_json.await_count == 1


@pytest.mark.asyncio
async def test_alpaca_iex_quote_bars_and_regular_dividend(fixture_json):
    provider = create_builtin_alpaca_provider("key", "secret")
    provider._request_json = AsyncMock(
        side_effect=[
            fixture_json("alpaca_quote.json"),
            fixture_json("alpaca_clock.json"),
            fixture_json("alpaca_bars.json"),
            fixture_json("alpaca_dividends.json"),
        ]
    )
    start = datetime(2026, 7, 20, 14, tzinfo=UTC)

    latest = await provider.get_quote("QQQM:USD")
    points = await provider.get_history(
        "QQQM:USD", interval="1m", start=start, end=start + timedelta(hours=1)
    )
    event = await provider.get_latest_dividend("QQQM:USD")

    assert latest.price == Decimal("245.18")
    assert latest.feed == "iex"
    assert latest.coverage == "single_venue"
    assert latest.market_status == "open"
    assert provider._request_json.await_args_list[1].args[1] == (
        "https://paper-api.alpaca.markets/v2/clock"
    )
    assert len(points) == 2
    assert event is not None
    assert event.amount == Decimal("0.3215")
    assert event.frequency == "quarterly"
    assert event.event_type == "regular_cash"
    dividend_call = provider._request_json.await_args_list[3]
    assert dividend_call.args[1] == "https://data.alpaca.markets/v1/corporate-actions"
    assert dividend_call.kwargs["params"]["types"] == "cash_dividend"


@pytest.mark.asyncio
async def test_alpaca_stream_trade_does_not_claim_market_clock_status():
    class FakeMessage:
        type = aiohttp.WSMsgType.TEXT

        @staticmethod
        def json():
            return [
                {
                    "T": "t",
                    "S": "QQQM",
                    "p": "245.18",
                    "t": "2026-07-20T20:30:00Z",
                }
            ]

    class FakeWebsocket:
        def __init__(self):
            self._messages = iter((FakeMessage(),))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def send_json(self, payload):
            del payload

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                raise StopAsyncIteration from None

    class FakeSession:
        @staticmethod
        def ws_connect(*args, **kwargs):
            del args, kwargs
            return FakeWebsocket()

    provider = create_builtin_alpaca_provider("key", "secret", session=FakeSession())

    stream = provider.stream_quotes(("QQQM:USD",))
    streamed = await anext(stream)
    await stream.aclose()

    assert streamed.market_status == "unknown"
    assert streamed.market_status_as_of is None


@pytest.mark.asyncio
async def test_finnhub_quote_uses_header_auth_and_normalizes_trade(fixture_json):
    vendor_time = datetime(2026, 7, 20, 15, 30, tzinfo=UTC)
    provider = create_builtin_finnhub_provider("secret-finnhub-key")
    provider._request_json = AsyncMock(return_value=fixture_json("finnhub_quote.json"))

    result = await provider.get_quote("qqqm:usd")

    assert result.symbol == "QQQM:USD"
    assert result.price == Decimal("245.18")
    assert result.as_of == vendor_time
    assert result.provider == "finnhub"
    assert result.feed == "finnhub_rest"
    assert result.price_basis == "last_trade"
    assert result.market_status == "unknown"
    assert result.market_status_as_of is None
    assert result.license_scope == "personal_internal_no_redistribution"
    assert result.coverage == "us_realtime_unspecified"

    call = provider._request_json.await_args
    assert call.args == ("GET", "https://api.finnhub.io/api/v1/quote")
    assert call.kwargs["params"] == {"symbol": "QQQM"}
    assert call.kwargs["headers"] == {"X-Finnhub-Token": "secret-finnhub-key"}
    assert "secret-finnhub-key" not in call.args[1]
    assert "token" not in call.kwargs["params"]


@pytest.mark.asyncio
async def test_finnhub_rejects_a_non_positive_vendor_timestamp(fixture_json):
    payload = fixture_json("finnhub_quote.json")
    payload["t"] = 0
    provider = create_builtin_finnhub_provider("key")
    provider._request_json = AsyncMock(return_value=payload)

    with pytest.raises(ProviderUnavailable, match="no current quote data"):
        await provider.get_quote("QQQM:USD")


@pytest.mark.asyncio
async def test_finnhub_rejects_an_unmapped_instrument_before_request():
    provider = create_builtin_finnhub_provider("key")
    provider._request_json = AsyncMock()

    with pytest.raises(UnsupportedInstrument, match="unsupported symbol"):
        await provider.get_quote("PRIVATE:USD")

    provider._request_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_finnhub_keeps_two_thousand_bindings_with_rest_overflow_after_stream_cap() -> None:
    bindings = {f"F{index:04d}:USD": f"F{index:04d}" for index in range(2_000)}
    provider = FinnhubProvider("key", symbol_bindings=bindings)
    provider._request_json = AsyncMock(return_value={"c": 10, "t": 1_784_561_400})

    assert len(provider.symbols) == 2_000
    assert len(provider.stream_symbols) == 50
    overflow_symbol = tuple(bindings)[-1]
    assert overflow_symbol not in provider.stream_symbols

    result = await provider.get_quote(overflow_symbol)

    assert result.symbol == overflow_symbol
    assert provider._request_json.await_args.kwargs["params"] == {
        "symbol": bindings[overflow_symbol]
    }


@pytest.mark.asyncio
async def test_finnhub_all_zero_quote_fails_closed():
    provider = create_builtin_finnhub_provider("key")
    provider._request_json = AsyncMock(
        return_value={"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0}
    )

    with pytest.raises(ProviderUnavailable, match="quote"):
        await provider.get_quote("QQQM:USD")


@pytest.mark.asyncio
async def test_finnhub_rest_error_does_not_echo_the_vendor_message():
    provider = create_builtin_finnhub_provider("secret-finnhub-key")
    provider._request_json = AsyncMock(return_value={"error": "Invalid API key secret-finnhub-key"})

    with pytest.raises(ProviderUnavailable) as error:
        await provider.get_quote("QQQM:USD")

    rendered = str(error.value)
    assert "secret-finnhub-key" not in rendered
    assert "Invalid API key" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"c": "not-a-price", "t": 1784561400},
        {"c": "245.18"},
        {"t": 1784561400},
    ],
)
async def test_finnhub_malformed_quote_is_rejected(payload):
    provider = create_builtin_finnhub_provider("key")
    provider._request_json = AsyncMock(return_value=payload)

    with pytest.raises(MalformedResponse):
        await provider.get_quote("QQQM:USD")


@pytest.mark.asyncio
async def test_finnhub_stream_subscribes_once_and_normalizes_millisecond_trades():
    class FakeMessage:
        type = aiohttp.WSMsgType.TEXT

        @staticmethod
        def json():
            return {
                "type": "trade",
                "data": [
                    {
                        "p": "245.19",
                        "s": "QQQM",
                        "t": 1784561400123,
                        "v": "10",
                    }
                ],
            }

    class FakeWebsocket:
        def __init__(self):
            self.messages = iter((FakeMessage(),))
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def send_json(self, payload):
            self.sent.append(payload)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration from None

    class FakeSession:
        def __init__(self):
            self.websocket = FakeWebsocket()
            self.connection = None

        def ws_connect(self, *args, **kwargs):
            self.connection = (args, kwargs)
            return self.websocket

    session = FakeSession()
    provider = create_builtin_finnhub_provider(
        "secret-finnhub-key",
        session=session,
        proxy_url="http://10.0.1.7:7890",
    )

    stream = provider.stream_quotes(("QQQM:USD", "AAPL:USD", "QQQM:USD"))
    result = await anext(stream)
    await stream.aclose()

    assert session.connection is not None
    args, kwargs = session.connection
    assert args == ("wss://ws.finnhub.io",)
    assert kwargs["params"] == {"token": "secret-finnhub-key"}
    assert kwargs["proxy"] == "http://10.0.1.7:7890"
    assert session.websocket.sent == [
        {"type": "subscribe", "symbol": "QQQM"},
        {"type": "subscribe", "symbol": "AAPL"},
    ]
    assert result.symbol == "QQQM:USD"
    assert result.price == Decimal("245.19")
    assert result.as_of == datetime(2026, 7, 20, 15, 30, 0, 123000, tzinfo=UTC)
    assert result.feed == "finnhub_websocket"
    assert result.price_basis == "last_trade"
    assert result.market_status == "unknown"
    assert result.market_status_as_of is None
    assert result.license_scope == "personal_internal_no_redistribution"
    assert result.coverage == "us_realtime_unspecified"


@pytest.mark.asyncio
async def test_finnhub_stream_error_fails_without_echoing_vendor_message_or_key():
    class FakeMessage:
        type = aiohttp.WSMsgType.TEXT

        @staticmethod
        def json():
            return {
                "type": "error",
                "msg": "Invalid API key secret-finnhub-key",
            }

    class FakeWebsocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def send_json(self, payload):
            del payload

        def __aiter__(self):
            return self

        async def __anext__(self):
            if hasattr(self, "delivered"):
                raise StopAsyncIteration
            self.delivered = True
            return FakeMessage()

    class FakeSession:
        @staticmethod
        def ws_connect(*args, **kwargs):
            del args, kwargs
            return FakeWebsocket()

    provider = create_builtin_finnhub_provider("secret-finnhub-key", session=FakeSession())
    stream = provider.stream_quotes(("QQQM:USD",))

    with pytest.raises(ProviderUnavailable) as error:
        await anext(stream)
    await stream.aclose()

    rendered = str(error.value)
    assert "secret-finnhub-key" not in rendered
    assert "Invalid API key" not in rendered


@pytest.mark.asyncio
async def test_alpha_date_only_equity_quote_fails_closed_before_regular_close():
    wall_time = datetime(2026, 11, 27, 18, 30, tzinfo=UTC)
    provider = create_builtin_alpha_vantage_provider("key", wall_clock=lambda: wall_time)
    provider._request_json = AsyncMock(
        return_value={
            "Global Quote": {
                "05. price": "250.00",
                # Representative US early-close date. Alpha supplies only the
                # trading date, not the authoritative close timestamp.
                "07. latest trading day": "2026-11-27",
            }
        }
    )

    with pytest.raises(ProviderUnavailable, match="date-only daily close"):
        await provider.get_quote("QQQM:USD")


@pytest.mark.asyncio
async def test_alpha_date_only_equity_quote_never_returns_a_future_timestamp():
    wall_time = datetime(2026, 11, 27, 21, 1, tzinfo=UTC)
    provider = create_builtin_alpha_vantage_provider("key", wall_clock=lambda: wall_time)
    provider._request_json = AsyncMock(
        return_value={
            "Global Quote": {
                "05. price": "250.00",
                "07. latest trading day": "2026-11-27",
            }
        }
    )

    result = await provider.get_quote("QQQM:USD")

    assert result.as_of == datetime(2026, 11, 27, 21, tzinfo=UTC)
    assert result.as_of <= wall_time


@pytest.mark.asyncio
async def test_alpaca_excludes_return_of_capital_from_regular_dividend():
    provider = create_builtin_alpaca_provider("key", "secret")
    provider._request_json = AsyncMock(
        return_value={
            "cash_dividends": [
                {
                    "symbol": "QQQM",
                    "ex_date": "2026-07-10",
                    "rate": "5.0",
                    "special": False,
                    "foreign": False,
                    "sub_type": "return_of_capital",
                },
                {
                    "symbol": "QQQM",
                    "ex_date": "2026-06-20",
                    "rate": "0.32",
                    "special": False,
                    "foreign": False,
                },
            ]
        }
    )

    event = await provider.get_latest_dividend("QQQM:USD")

    assert event is not None
    assert event.amount == Decimal("0.32")


def test_alpaca_bounds_dynamic_stream_symbols_and_paces_rest_overflow():
    bindings = {f"TEST{index}:USD": f"TEST{index}" for index in range(75)}
    provider = AlpacaProvider(
        "key",
        "secret",
        symbol_bindings=bindings,
        stream_symbol_limit=30,
        rest_calls_per_minute=180,
    )

    assert provider.stream_symbols == tuple(bindings)[:30]
    assert len(provider.stream_symbols) == 30
    assert provider.minimum_quote_poll_seconds >= 20

    with pytest.raises(ValueError, match="stream symbol limit"):
        AlpacaProvider(
            "key",
            "secret",
            symbol_bindings=bindings,
            stream_symbols=tuple(bindings)[:31],
            stream_symbol_limit=30,
        )


def test_alpaca_rest_floor_remains_safe_for_two_thousand_symbol_stream_outage():
    bindings = {f"T{index:04d}:USD": f"T{index:04d}" for index in range(2_000)}
    provider = AlpacaProvider(
        "key",
        "secret",
        symbol_bindings=bindings,
        stream_symbol_limit=30,
        rest_calls_per_minute=180,
    )

    assert len(provider.stream_symbols) == 30
    assert provider.minimum_quote_poll_seconds >= 2_000 * 60 / (180 * 0.9)


@pytest.mark.asyncio
async def test_alpaca_applies_one_shared_rate_gate_to_all_rest_requests(monkeypatch):
    class Gate:
        calls = 0

        async def acquire(self):
            self.calls += 1

    gate = Gate()
    upstream = AsyncMock(
        side_effect=[
            {"trade": {"p": "123.45", "t": "2026-07-20T15:30:00Z"}},
            {"is_open": True, "timestamp": "2026-07-20T15:30:00Z"},
        ]
    )
    monkeypatch.setattr(HttpProvider, "_request_json", upstream)
    provider = AlpacaProvider(
        "key",
        "secret",
        symbol_bindings={"TEST:USD": "TEST"},
        rest_gate=gate,
    )

    result = await provider.get_quote("TEST:USD")

    assert result.price == Decimal("123.45")
    assert gate.calls == 2
    assert upstream.await_count == 2


@pytest.mark.asyncio
async def test_alpaca_nested_actions_accept_sgov_interest_distribution(fixture_json):
    provider = create_builtin_alpaca_provider("key", "secret")
    provider._request_json = AsyncMock(return_value=fixture_json("alpaca_sgov_dividends.json"))

    event = await provider.get_latest_dividend("SGOV:USD")

    assert event is not None
    assert event.symbol == "SGOV:USD"
    assert event.amount == Decimal("0.295765")
    assert event.frequency == "monthly"
    assert event.event_type == "regular_cash"


@pytest.mark.asyncio
async def test_alpaca_rejects_malformed_nested_actions_envelope():
    provider = create_builtin_alpaca_provider("key", "secret")
    provider._request_json = AsyncMock(return_value={"corporate_actions": []})

    with pytest.raises(MalformedResponse, match="corporate_actions must be an object"):
        await provider.get_latest_dividend("QQQM:USD")


@pytest.mark.asyncio
async def test_twelve_data_quote_and_history_contract(fixture_json):
    provider = create_builtin_twelve_data_provider("key")
    provider._request_json = AsyncMock(
        side_effect=[fixture_json("twelve_history.json"), fixture_json("twelve_history.json")]
    )
    start = datetime(2026, 7, 20, 14, tzinfo=UTC)

    latest = await provider.get_quote("USD:CNH")
    points = await provider.get_history(
        "USD:CNH", interval="1m", start=start, end=start + timedelta(hours=1)
    )

    assert latest.price == Decimal("7.21530")
    assert latest.market_status == "unknown"
    assert latest.price_basis == "time_series_close"
    assert len(points) == 2
    assert points[0].timestamp < points[1].timestamp
    quote_call = provider._request_json.await_args_list[0]
    assert quote_call.args[1].endswith("/time_series")
    assert quote_call.kwargs["params"]["outputsize"] == 1
    assert quote_call.kwargs["params"]["order"] == "DESC"
    assert quote_call.kwargs["allow_quota_reserve"] is True
    history_call = provider._request_json.await_args_list[1]
    assert history_call.kwargs["params"]["adjust"] == "none"
    assert history_call.kwargs["allow_quota_reserve"] is False


@pytest.mark.asyncio
async def test_twelve_router_allows_locally_paced_ninth_call(monkeypatch):
    class Gate:
        calls = 0

        async def acquire(self):
            self.calls += 1
            if self.calls == 9:
                await asyncio.sleep(0.02)

    gate = Gate()
    provider = create_builtin_twelve_data_provider("key", rate_gate=gate, request_timeout=0.005)
    payload = {"values": [{"datetime": "2026-07-20 14:30:00", "close": "7.21530"}]}
    upstream = AsyncMock(return_value=payload)
    monkeypatch.setattr(HttpProvider, "_request_json", upstream)
    router = ProviderRouter(
        {("USD:CNH", Capability.HISTORY): [provider]},
        timeout_seconds=0.005,
    )
    start = datetime(2026, 7, 20, 14, tzinfo=UTC)
    try:
        results = await asyncio.gather(
            *(
                router.get_history(
                    "USD:CNH",
                    interval="1m",
                    start=start,
                    end=start + timedelta(hours=1),
                    limit=limit,
                )
                for limit in range(1, 10)
            )
        )
    finally:
        await router.close()

    assert gate.calls == 9
    assert upstream.await_count == 9
    assert all(result for result in results)


@pytest.mark.asyncio
async def test_twelve_local_gate_saturation_is_bounded_without_spending_fallback_quota():
    blocked = asyncio.Event()

    class SaturatedGate:
        async def acquire(self):
            await blocked.wait()

    provider = create_builtin_twelve_data_provider(
        "key",
        rate_gate=SaturatedGate(),
        rate_gate_timeout_seconds=0.005,
        request_timeout=0.005,
    )
    start = datetime(2026, 7, 20, 14, tzinfo=UTC)

    class Backup:
        name = "history_backup"
        calls = 0

        async def get_history(self, symbol, **_kwargs):
            self.calls += 1
            return (PricePoint(symbol, start, Decimal("7.2"), self.name, interval="1m"),)

    backup = Backup()
    router = ProviderRouter(
        {("USD:CNH", Capability.HISTORY): [provider, backup]},
        timeout_seconds=0.005,
    )
    try:
        async with asyncio.timeout(0.2):
            with pytest.raises(AllProvidersFailed, match="admission timed out"):
                await router.get_history(
                    "USD:CNH",
                    interval="1m",
                    start=start,
                    end=start + timedelta(hours=1),
                )
    finally:
        await router.close()

    assert backup.calls == 0
    assert router.fallback_counts() == {}


@pytest.mark.asyncio
async def test_kraken_can_reject_an_illiquid_stale_trade_for_router_fallback(fixture_json):
    now = datetime(2026, 7, 20, 15, tzinfo=UTC)
    payload = fixture_json("kraken_quote.json")
    rows = next(value for key, value in payload["result"].items() if key != "last")
    rows[-1][2] = (now - timedelta(minutes=6)).timestamp()
    provider = create_builtin_kraken_provider(
        max_quote_ages={"XMR:USDC": timedelta(minutes=5)},
        wall_clock=lambda: now,
    )
    provider._request_json = AsyncMock(return_value=payload)

    with pytest.raises(ProviderUnavailable, match="stale"):
        await provider.get_quote("XMR:USDC")


@pytest.mark.asyncio
async def test_alpha_vantage_fx_and_dividend_contract(fixture_json):
    provider = create_builtin_alpha_vantage_provider("key")
    provider._request_json = AsyncMock(
        side_effect=[fixture_json("alpha_fx_quote.json"), fixture_json("alpha_dividends.json")]
    )

    latest = await provider.get_quote("USD:CNH")
    event = await provider.get_latest_dividend("SGOV:USD")

    assert latest.price == Decimal("7.2152")
    assert latest.feed == "alpha_vantage_fx"
    assert provider._request_json.await_args_list[0].kwargs["allow_quota_reserve"] is True
    assert "allow_quota_reserve" not in provider._request_json.await_args_list[1].kwargs
    # Alpha Vantage's documented payload has no ordinary/special classifier.
    # The adapter fails safe instead of annualizing an unclassified payment.
    assert event is None


@pytest.mark.asyncio
async def test_alpha_vantage_serializes_emergency_requests(monkeypatch) -> None:
    provider = create_builtin_alpha_vantage_provider(
        "key",
        minimum_request_interval_seconds=0,
    )
    inflight = 0
    maximum_inflight = 0

    async def request_json(_self, _method, _url, **_kwargs):
        nonlocal inflight, maximum_inflight
        inflight += 1
        maximum_inflight = max(maximum_inflight, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return {
            "Realtime Currency Exchange Rate": {
                "5. Exchange Rate": "1.25",
                "6. Last Refreshed": "2026-07-21 14:00:00",
            }
        }

    monkeypatch.setattr(HttpProvider, "_request_json", request_json)

    await asyncio.gather(
        provider.get_quote("USD:EUR"),
        provider.get_quote("USD:GBP"),
    )

    assert maximum_inflight == 1
    assert provider.routing_timeout_seconds >= 60


@pytest.mark.asyncio
async def test_alpha_vantage_paces_request_starts(monkeypatch) -> None:
    clock = [100.0]
    sleeps: list[float] = []

    async def advance(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    provider = create_builtin_alpha_vantage_provider(
        "key",
        request_clock=lambda: clock[0],
        request_sleeper=advance,
        minimum_request_interval_seconds=12.5,
    )
    upstream = AsyncMock(
        return_value={
            "Realtime Currency Exchange Rate": {
                "5. Exchange Rate": "1.25",
                "6. Last Refreshed": "2026-07-21 14:00:00",
            }
        }
    )
    monkeypatch.setattr(HttpProvider, "_request_json", upstream)

    await provider.get_quote("USD:EUR")
    await provider.get_quote("USD:GBP")

    assert sleeps == [12.5]
    assert upstream.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_factory", "payload"),
    [
        (
            lambda: create_builtin_alpha_vantage_provider("key"),
            {
                "data": [
                    {
                        "ex_dividend_date": "2026-07-01",
                        "payment_date": "2026-07-07",
                        "amount": "5.0",
                    }
                ]
            },
        ),
        (
            lambda: create_builtin_alpaca_provider("key", "secret"),
            {
                "cash_dividends": [
                    {
                        "symbol": "QQQM",
                        "ex_date": "2026-06-23",
                        "rate": "5.0",
                        "foreign": False,
                    }
                ]
            },
        ),
    ],
)
async def test_unclassified_distribution_is_not_annualized(provider_factory, payload):
    provider = provider_factory()
    provider._request_json = AsyncMock(return_value=payload)

    symbol = "SGOV:USD" if isinstance(provider, AlphaVantageProvider) else "QQQM:USD"
    assert await provider.get_latest_dividend(symbol) is None


@pytest.mark.asyncio
async def test_fred_boxx_proxy_contract(fixture_json):
    provider = create_builtin_fred_provider("key")
    provider._request_json = AsyncMock(return_value=fixture_json("fred_dgs3mo.json"))

    result = await provider.get_yield("BOXX:USD")

    assert result.value == Decimal("4.0551")
    assert result.method == "treasury_3m_proxy_minus_expense"
    assert result.is_proxy is True
    assert result.components[0].symbol == "DGS3MO"
