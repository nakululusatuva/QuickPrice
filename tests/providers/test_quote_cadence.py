from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from quickprice.providers.alpha_vantage import AlphaVantageProvider
from quickprice.providers.base import ProviderUnavailable
from quickprice.providers.synthetic import SyntheticQuoteProvider, SyntheticRecipe
from quickprice.providers.twelve_data import TwelveDataProvider


class VirtualClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.origin = datetime(2026, 7, 20, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return self.origin + timedelta(seconds=self.seconds)


@pytest.mark.asyncio
async def test_twelve_fx_cache_shares_standalone_and_synthetic_calls_under_daily_limit() -> None:
    clock = VirtualClock()
    calls: Counter[str] = Counter()
    provider = TwelveDataProvider("key", quote_cache_clock=clock.monotonic)

    async def request_json(method, url, *, params, **kwargs):
        del method, url, kwargs
        vendor_symbol = str(params["symbol"])
        calls[vendor_symbol] += 1
        await asyncio.sleep(0)
        return {
            "close": "7.20" if vendor_symbol == "USD/CNH" else "7.80",
            "timestamp": int(clock.now().timestamp()),
            "is_market_open": True,
        }

    provider._request_json = request_json
    synthetic = SyntheticQuoteProvider(
        provider.get_quote,
        (SyntheticRecipe.hkd_cnh(),),
        clock=clock.now,
    )

    for seconds in range(0, 86_400, 130):
        clock.seconds = float(seconds)
        direct, derived = await asyncio.gather(
            provider.get_quote("USD:CNH"),
            synthetic.get_quote("HKD:CNH"),
        )
        assert direct.symbol == "USD:CNH"
        assert derived.symbol == "HKD:CNH"

    # 665 USD/CNH refreshes plus 95 USD/HKD refreshes leave 30 of the
    # 790 daily credits for startup history and occasional equity fallback.
    assert calls == Counter({"USD/CNH": 665, "USD/HKD": 95})
    assert calls.total() == 760


@pytest.mark.asyncio
async def test_alpha_fx_cache_limits_two_synthetic_legs_to_eight_requests_per_day() -> None:
    clock = VirtualClock()
    calls: Counter[str] = Counter()
    provider = AlphaVantageProvider("key", quote_cache_clock=clock.monotonic)

    async def request_json(method, url, *, params, **kwargs):
        del method, url, kwargs
        counter = str(params["to_currency"])
        calls[counter] += 1
        await asyncio.sleep(0)
        return {
            "Realtime Currency Exchange Rate": {
                "5. Exchange Rate": "7.20" if counter == "CNH" else "7.80",
                "6. Last Refreshed": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        }

    provider._request_json = request_json
    synthetic = SyntheticQuoteProvider(
        provider.get_quote,
        (SyntheticRecipe.hkd_cnh(),),
        clock=clock.now,
    )

    for seconds in range(0, 86_400, 130):
        clock.seconds = float(seconds)
        direct, derived = await asyncio.gather(
            provider.get_quote("USD:CNH"),
            synthetic.get_quote("HKD:CNH"),
            return_exceptions=True,
        )
        assert not isinstance(direct, BaseException)
        # Between six-hour refreshes the synthetic freshness guard can reject
        # the cached components. That is intentional: callers keep their last
        # valid local snapshot instead of presenting an old cross as current.
        assert derived is not None

    assert calls == Counter({"CNH": 4, "HKD": 4})
    assert calls.total() == 8


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
