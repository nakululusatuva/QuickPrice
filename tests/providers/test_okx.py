from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from quickprice.domain import RewardAccrualMode, YieldRateType
from quickprice.providers.base import (
    MalformedResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    UnsupportedInstrument,
)
from quickprice.providers.okx import OkxBethYieldProvider, OkxMarketProvider


@pytest.mark.asyncio
async def test_okx_ticker_uses_a_current_safe_book_midpoint(fixture_json) -> None:
    observed_at = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    provider = OkxMarketProvider(
        wall_clock=lambda: observed_at,
        minimum_request_interval_seconds=0,
    )
    provider._request_json = AsyncMock(return_value=fixture_json("okx_ticker.json"))

    result = await provider.get_quote("OKX_BETH:ETH")

    assert result.symbol == "BETH:ETH"
    assert result.price == Decimal("0.9998")
    assert result.as_of == observed_at
    assert result.provider == "okx"
    assert result.feed == "okx_spot_ticker"
    assert result.price_basis == "midpoint"
    assert result.is_derived is True
    assert [item.role for item in result.components] == ["best_bid", "best_ask"]
    assert [item.price for item in result.components] == [
        Decimal("0.9997"),
        Decimal("0.9999"),
    ]
    request = provider._request_json.await_args
    assert request.args[1].endswith("/api/v5/market/ticker")
    assert request.kwargs["params"] == {"instId": "BETH-ETH"}


@pytest.mark.asyncio
async def test_okx_rejects_a_wide_or_crossed_book() -> None:
    provider = OkxMarketProvider(minimum_request_interval_seconds=0)
    provider._request_json = AsyncMock(
        return_value={
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instId": "BETH-ETH",
                    "bidPx": "0.99",
                    "askPx": "1.01",
                    "ts": "1784604228062",
                }
            ],
        }
    )

    with pytest.raises(ProviderUnavailable, match="spread exceeds"):
        await provider.get_quote("BETH:ETH")


@pytest.mark.asyncio
async def test_okx_history_normalizes_descending_candles(fixture_json) -> None:
    provider = OkxMarketProvider(minimum_request_interval_seconds=0)
    provider._request_json = AsyncMock(return_value=fixture_json("okx_candles.json"))
    start = datetime.fromtimestamp(1784604000, tz=UTC)
    end = datetime.fromtimestamp(1784604240, tz=UTC)

    points = await provider.get_history(
        "OKX_BETH:ETH",
        interval="1m",
        start=start,
        end=end,
        limit=10,
    )

    assert [item.timestamp for item in points] == sorted(item.timestamp for item in points)
    assert [item.price for item in points] == [
        Decimal("0.9997"),
        Decimal("0.9998"),
        Decimal("0.9997"),
    ]
    assert all(item.symbol == "BETH:ETH" for item in points)
    request = provider._request_json.await_args
    assert request.args[1].endswith("/api/v5/market/history-candles")
    assert request.kwargs["params"]["instId"] == "BETH-ETH"
    assert request.kwargs["params"]["bar"] == "1m"


@pytest.mark.asyncio
async def test_okx_history_rejects_malformed_rows() -> None:
    provider = OkxMarketProvider(minimum_request_interval_seconds=0)
    provider._request_json = AsyncMock(return_value={"code": "0", "msg": "", "data": [["bad"]]})

    with pytest.raises(MalformedResponse, match="invalid candle"):
        await provider.get_history(
            "BETH:ETH",
            interval="1m",
            start=datetime(2026, 7, 20, tzinfo=UTC),
            end=datetime(2026, 7, 21, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_okx_rate_limit_code_is_normalized() -> None:
    provider = OkxMarketProvider(minimum_request_interval_seconds=0)
    provider._request_json = AsyncMock(
        return_value={"code": "50011", "msg": "rate limit reached", "data": []}
    )

    with pytest.raises(ProviderRateLimited, match="quota exceeded"):
        await provider.get_quote("BETH:ETH")


@pytest.mark.asyncio
async def test_okx_beth_yield_is_provider_reported_apr_in_percent(fixture_json) -> None:
    latest = datetime.fromtimestamp(1784598300, tz=UTC)
    provider = OkxBethYieldProvider(clock=lambda: latest + timedelta(hours=1))
    provider._request_json = AsyncMock(return_value=fixture_json("okx_beth_apy.json"))

    metric = await provider.get_yield("BETH:USDC")

    assert metric.value == Decimal("2.11669700")
    assert metric.as_of == latest
    assert metric.method == "okx_beth_provider_reported_apr"
    assert metric.provider == "okx_beth_yield"
    assert metric.rate_type is YieldRateType.APR
    assert metric.accrual_mode is RewardAccrualMode.DISTRIBUTED_UNITS
    assert metric.underlying_asset == "ETH"
    assert metric.is_proxy is False
    assert metric.is_estimate is False
    assert metric.accrual_index is None
    assert metric.quality is not None and metric.quality.stale is False
    assert metric.components[0].role == "provider_reported_apr_fraction"
    assert metric.components[0].price == Decimal("0.02116697")
    request = provider._request_json.await_args
    assert request.args[1].endswith("/api/v5/finance/staking-defi/eth/apy-history")
    assert request.kwargs["params"] == {"days": "30"}


@pytest.mark.asyncio
async def test_okx_beth_yield_has_no_synthetic_symbol_fallback() -> None:
    provider = OkxBethYieldProvider()

    with pytest.raises(UnsupportedInstrument):
        await provider.get_yield("WBETH:USDC")
