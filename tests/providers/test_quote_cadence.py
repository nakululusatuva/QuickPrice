from __future__ import annotations

import asyncio
import math
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from quickprice.equities import LISTED_SYMBOLS
from quickprice.providers.alpha_vantage import AlphaVantageProvider
from quickprice.providers.base import ProviderUnavailable
from quickprice.providers.finnhub import FinnhubProvider
from quickprice.providers.fx import UsdHubFxQuoteProvider
from quickprice.providers.twelve_data import TwelveDataProvider
from quickprice.registry import INSTRUMENTS


class VirtualClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.origin = datetime(2026, 7, 20, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return self.origin + timedelta(seconds=self.seconds)


@pytest.mark.asyncio
async def test_finnhub_rest_cache_keeps_the_listed_catalog_below_the_minute_limit() -> None:
    clock = VirtualClock()
    calls = 0
    provider = FinnhubProvider(
        "key",
        quote_cache_clock=clock.monotonic,
    )

    async def request_json(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        await asyncio.sleep(0)
        return {"c": "100", "t": int(clock.now().timestamp())}

    provider._request_json = request_json

    for seconds in range(0, 60, 5):
        clock.seconds = float(seconds)
        await asyncio.gather(*(provider.get_quote(symbol) for symbol in LISTED_SYMBOLS))

    assert provider.minimum_quote_poll_seconds == 20
    assert calls == len(LISTED_SYMBOLS) * 3
    assert calls == 39 < 60


def test_coingecko_staking_cadence_fits_the_rolling_month_safe_daily_budget() -> None:
    poll_seconds = min(
        INSTRUMENTS[symbol].quote_poll_seconds for symbol in ("STETH:USDC", "WSTETH:USDC")
    )
    batched_quote_requests = math.ceil(86_400 / poll_seconds)
    hourly_history_requests = 2 * 3 * 24
    initial_paging_overhead = 2 * 2

    assert batched_quote_requests == 131
    assert batched_quote_requests + hourly_history_requests + initial_paging_overhead == 279
    assert 279 <= 9_000 // 31


@pytest.mark.asyncio
async def test_twelve_fx_hub_cache_keeps_the_complete_matrix_under_daily_limit() -> None:
    clock = VirtualClock()
    calls: Counter[str] = Counter()
    provider = TwelveDataProvider("key", quote_cache_clock=clock.monotonic)

    async def request_json(method, url, *, params, **kwargs):
        del method, url, kwargs
        vendor_symbol = str(params["symbol"])
        calls[vendor_symbol] += 1
        await asyncio.sleep(0)
        prices = {
            "USD/EUR": "0.86",
            "USD/GBP": "0.75",
            "USD/HKD": "7.80",
            "USD/SGD": "1.28",
            "USD/CNH": "7.20",
        }
        return {
            "close": prices[vendor_symbol],
            "timestamp": int(clock.now().timestamp()),
            "is_market_open": True,
        }

    provider._request_json = request_json
    synthetic = UsdHubFxQuoteProvider(provider.get_quote, clock=clock.now)

    for seconds in range(0, 86_400, 240):
        clock.seconds = float(seconds)
        results = await asyncio.gather(
            *(provider.get_quote(symbol) for symbol in provider.quote_cache_ttl_seconds),
            synthetic.get_quote("GBP:CNH"),
            synthetic.get_quote("SGD:USD"),
        )
        assert results[-2].symbol == "GBP:CNH"
        assert results[-1].symbol == "SGD:USD"

    # Collection runs every 240 seconds. The four slower spokes refresh on the
    # next collection tick after their 900-second TTL, so this schedule uses
    # 720 quote credits. Even an adversarial exact-expiry schedule is bounded
    # by ceil(86400 / ttl) == 744 credits.
    assert calls == Counter(
        {
            "USD/CNH": 360,
            "USD/EUR": 90,
            "USD/GBP": 90,
            "USD/HKD": 90,
            "USD/SGD": 90,
        }
    )
    assert calls.total() == 720
    theoretical_max = sum(
        math.ceil(86_400 / ttl) for ttl in provider.quote_cache_ttl_seconds.values()
    )
    assert theoretical_max == 744
    initial_hub_history_requests = 5 * (1 + 3 + 1)
    daily_tail_history_requests = 5 * 3
    assert theoretical_max + initial_hub_history_requests == 769 < 790
    assert theoretical_max + daily_tail_history_requests == 759 < 790


@pytest.mark.asyncio
async def test_alpha_fx_cache_limits_five_hub_legs_to_twenty_requests_per_day() -> None:
    clock = VirtualClock()
    calls: Counter[str] = Counter()
    provider = AlphaVantageProvider("key", quote_cache_clock=clock.monotonic)

    async def request_json(method, url, *, params, **kwargs):
        del method, url, kwargs
        counter = str(params["to_currency"])
        calls[counter] += 1
        await asyncio.sleep(0)
        prices = {
            "EUR": "0.86",
            "GBP": "0.75",
            "HKD": "7.80",
            "SGD": "1.28",
            "CNH": "7.20",
        }
        return {
            "Realtime Currency Exchange Rate": {
                "5. Exchange Rate": prices[counter],
                "6. Last Refreshed": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        }

    provider._request_json = request_json
    synthetic = UsdHubFxQuoteProvider(provider.get_quote, clock=clock.now)

    for seconds in range(0, 86_400, 240):
        clock.seconds = float(seconds)
        results = await asyncio.gather(
            *(provider.get_quote(symbol) for symbol in provider.fx_symbols),
            synthetic.get_quote("HKD:CNH"),
            return_exceptions=True,
        )
        assert all(not isinstance(result, BaseException) for result in results[:-1])
        # Between six-hour refreshes the synthetic freshness guard can reject
        # the cached components. That is intentional: callers keep their last
        # valid local snapshot instead of presenting an old cross as current.
        assert results[-1] is not None

    assert calls == Counter({currency: 4 for currency in ("EUR", "GBP", "HKD", "SGD", "CNH")})
    assert calls.total() == 20


@pytest.mark.asyncio
async def test_low_frequency_cache_negative_caches_expected_provider_failures() -> None:
    clock = VirtualClock()
    provider = AlphaVantageProvider("key", quote_cache_clock=clock.monotonic)
    calls = 0

    async def request_json(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        await asyncio.sleep(0)
        raise ProviderUnavailable("alpha_vantage", "fixture outage")

    provider._request_json = request_json

    results = await asyncio.gather(
        *(provider.get_quote("USD:CNH") for _ in range(20)),
        return_exceptions=True,
    )
    assert all(isinstance(result, ProviderUnavailable) for result in results)
    assert calls == 1

    clock.seconds = 21_599
    with pytest.raises(ProviderUnavailable, match="fixture outage"):
        await provider.get_quote("USD:CNH")
    assert calls == 1

    clock.seconds = 21_600
    with pytest.raises(ProviderUnavailable, match="fixture outage"):
        await provider.get_quote("USD:CNH")
    assert calls == 2
