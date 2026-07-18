from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import aiohttp
import pytest

from quickprice.providers.alpaca import AlpacaProvider
from quickprice.providers.alpha_vantage import AlphaVantageProvider
from quickprice.providers.base import (
    MalformedResponse,
    ProviderUnavailable,
    UnsupportedInstrument,
)
from quickprice.providers.binance import BinanceProvider
from quickprice.providers.coingecko import CoinGeckoProvider
from quickprice.providers.fred import FredProvider
from quickprice.providers.kraken import KrakenProvider
from quickprice.providers.twelve_data import TwelveDataProvider


@pytest.mark.asyncio
async def test_binance_quote_and_unadjusted_kline_contract(fixture_json):
    provider = BinanceProvider()
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


@pytest.mark.asyncio
async def test_binance_sol_quote_and_history_use_the_solusdc_market(fixture_json):
    provider = BinanceProvider()
    provider._request_json = AsyncMock(
        side_effect=[fixture_json("binance_quote.json"), fixture_json("binance_history.json")]
    )

    latest = await provider.get_quote("SOL:USDC")
    points = await provider.get_history(
        "SOL:USDC",
        interval="5m",
        start=datetime(2026, 7, 20, tzinfo=UTC),
        end=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert latest.symbol == "SOL:USDC"
    assert all(point.symbol == "SOL:USDC" for point in points)
    assert provider._request_json.await_args_list[0].kwargs["params"]["symbol"] == "SOLUSDC"
    assert provider._request_json.await_args_list[1].kwargs["params"]["symbol"] == "SOLUSDC"
    with pytest.raises(UnsupportedInstrument):
        await provider.get_quote("XMR:USDC")


@pytest.mark.asyncio
async def test_kraken_trade_contract(fixture_json):
    provider = KrakenProvider()
    provider._request_json = AsyncMock(return_value=fixture_json("kraken_quote.json"))

    result = await provider.get_quote("BTC:USDC")

    assert result.provider == "kraken"
    assert result.price == Decimal("118249.90")
    assert result.as_of.tzinfo is UTC


@pytest.mark.asyncio
async def test_kraken_sol_and_xmr_use_canonical_rest_pairs(fixture_json):
    provider = KrakenProvider()
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


@pytest.mark.asyncio
async def test_coingecko_quote_is_usdc_ratio_with_components(fixture_json):
    provider = CoinGeckoProvider("demo")
    provider._request_json = AsyncMock(return_value=fixture_json("coingecko_quote.json"))

    result = await provider.get_quote("WBETH:USDC")

    assert result.price == Decimal("4125.0") / Decimal("0.9998")
    assert result.is_derived is True
    assert [item.role for item in result.components] == ["numerator", "denominator"]


@pytest.mark.asyncio
async def test_coingecko_rejects_wbeth_components_more_than_two_seconds_apart(fixture_json):
    payload = fixture_json("coingecko_quote.json")
    payload["usd-coin"]["last_updated_at"] -= 1
    provider = CoinGeckoProvider("key")
    provider._request_json = AsyncMock(return_value=payload)

    with pytest.raises(MalformedResponse, match="configured limit"):
        await provider.get_quote("WBETH:USDC")


@pytest.mark.asyncio
async def test_coingecko_allows_slow_aggregated_staking_component_updates():
    timestamp = 1_768_000_000
    provider = CoinGeckoProvider("key")
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
        "wrapped-beacon-eth": {"usd": 4_500, "last_updated_at": timestamp},
        "staked-ether": {"usd": 3_990, "last_updated_at": timestamp},
        "wrapped-steth": {"usd": 4_800, "last_updated_at": timestamp},
        "usd-coin": {"usd": 1, "last_updated_at": timestamp},
    }
    provider = CoinGeckoProvider("key", clock=lambda: 10.0)
    provider._request_json = AsyncMock(return_value=payload)

    results = await asyncio.gather(
        provider.get_quote("BTC:USDC"),
        provider.get_quote("ETH:USDC"),
        provider.get_quote("SOL:USDC"),
        provider.get_quote("XMR:USDC"),
        provider.get_quote("WBETH:USDC"),
        provider.get_quote("STETH:USDC"),
        provider.get_quote("WSTETH:USDC"),
    )

    assert len(results) == 7
    assert results[2].price == Decimal("180")
    assert results[3].price == Decimal("325")
    assert provider._request_json.await_count == 1
    requested_ids = provider._request_json.await_args.kwargs["params"]["ids"].split(",")
    assert set(requested_ids) == {
        "bitcoin",
        "ethereum",
        "solana",
        "monero",
        "wrapped-beacon-eth",
        "staked-ether",
        "wrapped-steth",
        "usd-coin",
    }


@pytest.mark.asyncio
async def test_coingecko_does_not_claim_intraday_history_support():
    provider = CoinGeckoProvider("key")

    with pytest.raises(UnsupportedInstrument, match="only for configured"):
        await provider.get_history(
            "BTC:USDC",
            interval="1m",
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 7, 2, tzinfo=UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["SOL:USDC", "XMR:USDC"])
async def test_coingecko_is_quote_only_for_ordinary_spot_fallbacks(symbol: str):
    provider = CoinGeckoProvider("key")

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
    provider = CoinGeckoProvider("key", clock=lambda: 10.0)
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
    provider = CoinGeckoProvider("key")
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
    provider = AlpacaProvider("key", "secret")
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

    provider = AlpacaProvider("key", "secret", session=FakeSession())

    stream = provider.stream_quotes(("QQQM:USD",))
    streamed = await anext(stream)
    await stream.aclose()

    assert streamed.market_status == "unknown"
    assert streamed.market_status_as_of is None


@pytest.mark.asyncio
async def test_alpha_date_only_equity_quote_fails_closed_before_regular_close():
    wall_time = datetime(2026, 11, 27, 18, 30, tzinfo=UTC)
    provider = AlphaVantageProvider("key", wall_clock=lambda: wall_time)
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
    provider = AlphaVantageProvider("key", wall_clock=lambda: wall_time)
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
    provider = AlpacaProvider("key", "secret")
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


@pytest.mark.asyncio
async def test_twelve_data_quote_and_history_contract(fixture_json):
    provider = TwelveDataProvider("key")
    provider._request_json = AsyncMock(
        side_effect=[fixture_json("twelve_quote.json"), fixture_json("twelve_history.json")]
    )
    start = datetime(2026, 7, 20, 14, tzinfo=UTC)

    latest = await provider.get_quote("USD:CNH")
    points = await provider.get_history(
        "USD:CNH", interval="1m", start=start, end=start + timedelta(hours=1)
    )

    assert latest.price == Decimal("7.21530")
    assert latest.market_status == "open"
    assert len(points) == 2
    assert points[0].timestamp < points[1].timestamp
    assert provider._request_json.await_args_list[1].kwargs["params"]["adjust"] == "none"


@pytest.mark.asyncio
async def test_alpha_vantage_fx_and_dividend_contract(fixture_json):
    provider = AlphaVantageProvider("key")
    provider._request_json = AsyncMock(
        side_effect=[fixture_json("alpha_fx_quote.json"), fixture_json("alpha_dividends.json")]
    )

    latest = await provider.get_quote("USD:CNH")
    event = await provider.get_latest_dividend("SGOV:USD")

    assert latest.price == Decimal("7.2152")
    assert latest.feed == "alpha_vantage_fx"
    # Alpha Vantage's documented payload has no ordinary/special classifier.
    # The adapter fails safe instead of annualizing an unclassified payment.
    assert event is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_factory", "payload"),
    [
        (
            lambda: AlphaVantageProvider("key"),
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
            lambda: AlpacaProvider("key", "secret"),
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
    provider = FredProvider("key")
    provider._request_json = AsyncMock(return_value=fixture_json("fred_dgs3mo.json"))

    result = await provider.get_yield("BOXX:USD")

    assert result.value == Decimal("4.0551")
    assert result.method == "treasury_3m_proxy_minus_expense"
    assert result.is_proxy is True
    assert result.components[0].symbol == "DGS3MO"
