from __future__ import annotations

import asyncio
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from quickprice.equities import LISTED_SYMBOLS
from quickprice.provider_factory import (
    builtin_fx_max_ages,
    builtin_fx_requirements,
    create_builtin_alpha_vantage_provider,
    create_builtin_finnhub_provider,
    create_builtin_twelve_data_provider,
)
from quickprice.providers.base import HttpProvider, ProviderRateLimited, ProviderUnavailable
from quickprice.providers.coingecko import (
    COINGECKO_DAILY_QUOTE_RESERVE_CREDITS,
    COINGECKO_SHARED_QUOTE_CACHE_SECONDS,
)
from quickprice.providers.fx import UsdHubFxQuoteProvider
from quickprice.providers.quota import QuotaBudget
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
    provider = create_builtin_finnhub_provider(
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
    symbols = ("BETH:USDC", "STETH:USDC", "WSTETH:USDC")
    assert {INSTRUMENTS[symbol].history_poll_seconds for symbol in symbols} == {21_600.0}

    batched_quote_requests = math.ceil(86_400 / COINGECKO_SHARED_QUOTE_CACHE_SECONDS)
    history_requests = len(symbols) * (3 * 4 + 2)
    ratio_history_requests = 2 * 4
    planned_requests = batched_quote_requests + history_requests + ratio_history_requests

    assert batched_quote_requests == 144
    assert COINGECKO_DAILY_QUOTE_RESERVE_CREDITS == 145
    assert planned_requests == 194
    assert planned_requests <= 9_000 // 31


@pytest.mark.asyncio
async def test_twelve_fx_hub_cache_keeps_the_complete_matrix_under_daily_limit() -> None:
    clock = VirtualClock()
    calls: Counter[str] = Counter()
    provider = create_builtin_twelve_data_provider("key", quote_cache_clock=clock.monotonic)

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
            "values": [
                {
                    "close": prices[vendor_symbol],
                    "datetime": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            ]
        }

    provider._request_json = request_json
    synthetic = UsdHubFxQuoteProvider(
        provider.get_quote,
        requirements=builtin_fx_requirements(),
        max_ages=builtin_fx_max_ages(),
        clock=clock.now,
    )

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
async def test_twelve_local_gate_timeout_is_not_negative_cached(monkeypatch) -> None:
    class Gate:
        calls = 0

        async def acquire(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(1)

    gate = Gate()
    provider = create_builtin_twelve_data_provider(
        "key",
        rate_gate=gate,
        rate_gate_timeout_seconds=0.005,
    )
    upstream = AsyncMock(
        return_value={"values": [{"datetime": "2026-07-20 15:30:00", "close": "7.20"}]}
    )
    monkeypatch.setattr(HttpProvider, "_request_json", upstream)

    with pytest.raises(ProviderRateLimited, match="admission timed out"):
        await provider.get_quote("USD:CNH")
    result = await provider.get_quote("USD:CNH")

    assert result.price == Decimal("7.20")
    assert gate.calls == 2
    assert upstream.await_count == 1


@pytest.mark.asyncio
async def test_twelve_exhausted_history_is_rejected_before_the_short_window_gate() -> None:
    class Gate:
        calls = 0

        async def acquire(self):
            self.calls += 1

    gate = Gate()
    quota = QuotaBudget(2, 86_400, reserve=1, align_windows=False)
    assert await quota.acquire()
    provider = create_builtin_twelve_data_provider("key", quota=quota, rate_gate=gate)

    with pytest.raises(ProviderRateLimited, match="local quota exhausted"):
        await provider.get_history(
            "USD:CNH",
            interval="1m",
            start=datetime(2026, 7, 20, tzinfo=UTC),
            end=datetime(2026, 7, 20, 1, tzinfo=UTC),
        )

    assert gate.calls == 0


@pytest.mark.asyncio
async def test_twelve_upstream_failure_remains_negative_cached(monkeypatch) -> None:
    class Gate:
        calls = 0

        async def acquire(self):
            self.calls += 1

    gate = Gate()
    provider = create_builtin_twelve_data_provider("key", rate_gate=gate)
    upstream = AsyncMock(side_effect=ProviderUnavailable("twelve_data", "fixture outage"))
    monkeypatch.setattr(HttpProvider, "_request_json", upstream)

    for _ in range(2):
        with pytest.raises(ProviderUnavailable, match="fixture outage"):
            await provider.get_quote("USD:CNH")

    assert gate.calls == 1
    assert upstream.await_count == 1


@pytest.mark.asyncio
async def test_alpha_fx_cache_limits_five_hub_legs_to_twenty_requests_per_day() -> None:
    clock = VirtualClock()
    calls: Counter[str] = Counter()
    provider = create_builtin_alpha_vantage_provider("key", quote_cache_clock=clock.monotonic)

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
    synthetic = UsdHubFxQuoteProvider(
        provider.get_quote,
        requirements=builtin_fx_requirements(),
        max_ages=builtin_fx_max_ages(),
        clock=clock.now,
    )

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
@pytest.mark.parametrize("provider_name", ["twelve_data", "alpha_vantage"])
async def test_equity_fallback_cache_expires_at_the_next_session_open(
    provider_name: str,
) -> None:
    clock = VirtualClock()
    clock.origin = datetime(2026, 7, 20, 13, 20, tzinfo=UTC)  # Monday 09:20 New York
    calls = 0
    provider = (
        create_builtin_twelve_data_provider(
            "key",
            quote_cache_clock=clock.monotonic,
            wall_clock=clock.now,
        )
        if provider_name == "twelve_data"
        else create_builtin_alpha_vantage_provider(
            "key",
            quote_cache_clock=clock.monotonic,
            wall_clock=clock.now,
        )
    )

    async def request_json(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if provider_name == "twelve_data":
            return {
                "close": "200.00",
                "datetime": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
                "is_market_open": clock.seconds >= 600,
            }
        return {
            "Global Quote": {
                "05. price": "200.00",
                "07. latest trading day": "2026-07-17",
            }
        }

    provider._request_json = request_json

    await provider.get_quote("QQQM:USD")
    clock.seconds = 599
    await provider.get_quote("QQQM:USD")
    assert calls == 1

    clock.seconds = 600
    await provider.get_quote("QQQM:USD")
    clock.seconds = 601
    await provider.get_quote("QQQM:USD")

    assert calls == 2


@pytest.mark.asyncio
async def test_low_frequency_cache_negative_caches_expected_provider_failures() -> None:
    clock = VirtualClock()
    provider = create_builtin_alpha_vantage_provider("key", quote_cache_clock=clock.monotonic)
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
