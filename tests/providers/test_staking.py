from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest

from quickprice.domain import PricePoint, RewardAccrualMode, YieldRateType
from quickprice.providers.base import MalformedResponse, ProviderUnavailable
from quickprice.providers.staking import (
    BinanceWbethYieldProvider,
    EthereumExchangeRateYieldProvider,
    StakingMarketRatioSpec,
    StakingMarketRatioYieldProvider,
)


def _uint256(value: Decimal) -> str:
    integer = int(value * Decimal(10**18))
    return f"0x{integer:064x}"


@pytest.mark.asyncio
async def test_ethereum_wbeth_exchange_rate_history_produces_trailing_apy():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = EthereumExchangeRateYieldProvider(
        "https://rpc.example.invalid",
        clock=lambda: now,
    )

    async def rpc(_endpoint, method, params):
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            timestamp = now - timedelta(days=100 - number)
            return {"number": hex(number), "timestamp": hex(int(timestamp.timestamp()))}
        if method == "eth_call":
            assert params[0]["data"] == "0x3ba0b9a9"
            assert params[1] == "0x64"
            return _uint256(Decimal("1.01"))
        if method == "eth_getLogs":
            start = int(params[0]["fromBlock"], 16)
            end = int(params[0]["toBlock"], 16)
            events = [
                {
                    "blockNumber": "0x5c",
                    "data": _uint256(Decimal("1")),
                    "removed": False,
                },
                {
                    "blockNumber": "0x63",
                    "data": _uint256(Decimal("1.01")),
                    "removed": False,
                },
            ]
            return [event for event in events if start <= int(event["blockNumber"], 16) <= end]
        raise AssertionError(method)

    provider._rpc = AsyncMock(side_effect=rpc)

    metric = await provider.get_yield("WBETH:USDC")

    assert metric.rate_type is YieldRateType.APY
    assert metric.accrual_mode is RewardAccrualMode.VALUE_ACCRUING
    assert metric.underlying_asset == "ETH"
    assert metric.is_proxy is False
    assert metric.is_estimate is True
    assert metric.observation_window_days == Decimal("7")
    assert metric.as_of == now - timedelta(days=1)
    assert metric.accrual_index is not None
    assert metric.accrual_index.value == Decimal("1.01")
    assert float(metric.value) == pytest.approx((1.01 ** (365 / 7) - 1) * 100)
    assert metric.quality is not None and metric.quality.confidence == "high"


@pytest.mark.asyncio
async def test_ethereum_rpc_fails_over_after_wrong_chain_id():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = EthereumExchangeRateYieldProvider(
        ("https://wrong.invalid", "https://mainnet.invalid"),
        clock=lambda: now,
    )

    async def rpc(endpoint, method, params):
        if method == "eth_chainId":
            return "0x2" if "wrong" in endpoint else "0x1"
        if method == "eth_blockNumber":
            return "0x1"
        if method == "eth_getBlockByNumber":
            return {"number": params[0], "timestamp": hex(int(now.timestamp()))}
        if method == "eth_getLogs":
            return [
                {
                    "blockNumber": "0x1",
                    "data": _uint256(Decimal("1.1")),
                    "removed": False,
                }
            ]
        if method == "eth_call":
            return _uint256(Decimal("1.1"))
        raise AssertionError(method)

    provider._rpc = AsyncMock(side_effect=rpc)

    index = await provider.get_accrual_index("WBETH:USDC")

    assert index.value == Decimal("1.1")
    endpoints = [
        call.args[0] for call in provider._rpc.await_args_list if call.args[1] == "eth_chainId"
    ]
    assert set(endpoints) == {"https://wrong.invalid", "https://mainnet.invalid"}


@pytest.mark.asyncio
async def test_ethereum_endpoint_race_is_bounded_and_rotates():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    urls = (
        "https://one.invalid",
        "https://two.invalid",
        "https://three.invalid",
    )
    provider = EthereumExchangeRateYieldProvider(
        urls,
        clock=lambda: now,
        endpoint_race_width=2,
    )

    async def rpc(endpoint, method, params):
        if method == "eth_chainId":
            return "0x1" if endpoint == urls[2] else "0x2"
        if method == "eth_blockNumber":
            return "0x1"
        if method == "eth_getLogs":
            return [
                {
                    "blockNumber": "0x1",
                    "data": _uint256(Decimal("1.1")),
                    "removed": False,
                }
            ]
        if method == "eth_getBlockByNumber":
            return {"number": params[0], "timestamp": hex(int(now.timestamp()))}
        if method == "eth_call":
            return _uint256(Decimal("1.1"))
        raise AssertionError(method)

    provider._rpc = AsyncMock(side_effect=rpc)

    with pytest.raises(ProviderUnavailable):
        await provider.get_accrual_index("WBETH:USDC")
    first_chain_calls = [
        call.args[0] for call in provider._rpc.await_args_list if call.args[1] == "eth_chainId"
    ]
    assert set(first_chain_calls) == set(urls[:2])

    provider._rpc.reset_mock()
    index = await provider.get_accrual_index("WBETH:USDC")

    assert index.value == Decimal("1.1")
    second_chain_calls = [
        call.args[0] for call in provider._rpc.await_args_list if call.args[1] == "eth_chainId"
    ]
    assert urls[2] in second_chain_calls


@pytest.mark.asyncio
async def test_ethereum_current_index_rejects_event_and_state_mismatch():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = EthereumExchangeRateYieldProvider(
        "https://rpc.example.invalid",
        clock=lambda: now,
    )

    async def rpc(_endpoint, method, params):
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_blockNumber":
            return "0xa"
        if method == "eth_getLogs":
            return [
                {
                    "blockNumber": "0x9",
                    "data": _uint256(Decimal("1.01")),
                    "removed": False,
                }
            ]
        if method == "eth_getBlockByNumber":
            return {"number": params[0], "timestamp": hex(int(now.timestamp()))}
        if method == "eth_call":
            assert params[1] == "0xa"
            return _uint256(Decimal("1.02"))
        raise AssertionError(method)

    provider._rpc = AsyncMock(side_effect=rpc)

    with pytest.raises(MalformedResponse, match="does not match current contract state"):
        await provider._current_index_on_endpoint(
            "https://rpc.example.invalid",
            provider._spec("WBETH:USDC"),
        )


@pytest.mark.asyncio
async def test_ethereum_history_chunks_log_requests_to_configured_span():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = EthereumExchangeRateYieldProvider(
        "https://rpc.example.invalid",
        clock=lambda: now,
        max_log_block_span=7,
        max_history_block_span=100,
    )
    requested_ranges: list[tuple[int, int]] = []

    async def rpc(_endpoint, method, params):
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_blockNumber":
            return "0x28"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            timestamp = now - timedelta(hours=40 - number)
            return {"number": params[0], "timestamp": hex(int(timestamp.timestamp()))}
        if method == "eth_getLogs":
            block_range = (
                int(params[0]["fromBlock"], 16),
                int(params[0]["toBlock"], 16),
            )
            requested_ranges.append(block_range)
            return []
        raise AssertionError(method)

    provider._rpc = AsyncMock(side_effect=rpc)

    points = await provider.get_accrual_index_history(
        "WBETH:USDC",
        start=now - timedelta(hours=30),
        end=now,
    )

    assert points == ()
    assert len(requested_ranges) > 1
    assert all(end - start + 1 <= 7 for start, end in requested_ranges)
    assert sorted(requested_ranges)[0][0] == 10
    assert sorted(requested_ranges)[-1][1] == 40


@pytest.mark.asyncio
async def test_binance_wbeth_rate_keeps_vendor_apr_and_signs_request():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = BinanceWbethYieldProvider(
        "read-only-key",
        "secret",
        clock=lambda: now,
    )
    provider._request_json = AsyncMock(
        return_value={
            "rows": [
                {
                    "annualPercentageRate": "0.032",
                    "exchangeRate": "1.1",
                    "time": int((now - timedelta(hours=1)).timestamp() * 1000),
                }
            ],
            "total": "1",
        }
    )

    metric = await provider.get_yield("WBETH:USDC")

    assert metric.value == Decimal("3.200")
    assert metric.rate_type is YieldRateType.APR
    assert metric.method == "binance_wbeth_rate_history_apr"
    assert metric.is_proxy is False
    assert metric.is_estimate is False
    assert metric.accrual_index is not None
    call = provider._request_json.await_args
    params = dict(call.kwargs["params"])
    signature = params.pop("signature")
    expected = hmac.new(
        b"secret",
        urlencode(params).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected
    assert call.kwargs["headers"] == {"X-MBX-APIKEY": "read-only-key"}
    assert "secret" not in urlencode(call.kwargs["params"])


class _HistoryFixture:
    name = "history_fixture"

    def __init__(self, points):
        self.points = points

    async def get_history(self, symbol, **_kwargs):
        return self.points[symbol]


@pytest.mark.asyncio
async def test_generic_market_ratio_is_a_low_confidence_30_day_proxy():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    reference = now - timedelta(days=30, hours=12)
    history = _HistoryFixture(
        {
            "LST:USD": (
                PricePoint("LST:USD", reference, Decimal("100"), "fixture", interval="1d"),
                PricePoint("LST:USD", now, Decimal("103"), "fixture", interval="1d"),
            ),
            "BASE:USD": (
                PricePoint("BASE:USD", reference, Decimal("100"), "fixture", interval="1d"),
                PricePoint("BASE:USD", now, Decimal("100"), "fixture", interval="1d"),
            ),
        }
    )
    provider = StakingMarketRatioYieldProvider(
        history,
        specs=(
            StakingMarketRatioSpec(
                symbol="LST:USD",
                staking_pair="LST:USD",
                underlying_pair="BASE:USD",
                underlying_asset="BASE",
                accrual_mode=RewardAccrualMode.REBASING_BALANCE,
            ),
        ),
        clock=lambda: now,
    )

    metric = await provider.get_yield("LST:USD")

    assert metric.method == "staking_market_ratio_30d_annualized"
    assert metric.is_proxy is True
    assert metric.is_estimate is True
    assert metric.rate_type is YieldRateType.APY
    assert metric.accrual_mode is RewardAccrualMode.REBASING_BALANCE
    assert metric.observation_window_days == Decimal("30.5")
    assert metric.accrual_index is not None
    assert metric.accrual_index.kind == "market_price_ratio"
    assert metric.quality is not None and metric.quality.confidence == "low"
    assert len(metric.components) == 4
    assert float(metric.value) == pytest.approx((1.03 ** (365 / 30.5) - 1) * 100)


@pytest.mark.asyncio
async def test_market_ratio_alignment_never_uses_a_future_underlying_price():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    reference = now - timedelta(days=30)
    history = _HistoryFixture(
        {
            "LST:USD": (
                PricePoint("LST:USD", reference, Decimal("100"), "fixture", interval="1d"),
                PricePoint("LST:USD", now, Decimal("103"), "fixture", interval="1d"),
            ),
            "BASE:USD": (
                PricePoint(
                    "BASE:USD",
                    reference - timedelta(hours=2),
                    Decimal("100"),
                    "fixture",
                    interval="1d",
                ),
                PricePoint(
                    "BASE:USD",
                    reference + timedelta(hours=1),
                    Decimal("200"),
                    "fixture",
                    interval="1d",
                ),
                PricePoint(
                    "BASE:USD",
                    now - timedelta(hours=2),
                    Decimal("100"),
                    "fixture",
                    interval="1d",
                ),
                PricePoint(
                    "BASE:USD",
                    now + timedelta(hours=1),
                    Decimal("50"),
                    "fixture",
                    interval="1d",
                ),
            ),
        }
    )
    provider = StakingMarketRatioYieldProvider(
        history,
        specs=(
            StakingMarketRatioSpec(
                symbol="LST:USD",
                staking_pair="LST:USD",
                underlying_pair="BASE:USD",
                underlying_asset="BASE",
                accrual_mode=RewardAccrualMode.CLAIMABLE_REWARDS,
            ),
        ),
        clock=lambda: now,
    )

    metric = await provider.get_yield("LST:USD")

    underlying_components = [
        component
        for component in metric.components
        if component.role and "underlying" in component.role
    ]
    assert [component.as_of for component in underlying_components] == [
        reference - timedelta(hours=2),
        now - timedelta(hours=2),
    ]
    assert metric.accrual_mode is RewardAccrualMode.CLAIMABLE_REWARDS
    assert metric.is_proxy is True
    assert metric.quality is not None and metric.quality.confidence == "low"


def test_market_ratio_requires_the_same_quote_asset():
    with pytest.raises(ValueError, match="share a quote asset"):
        StakingMarketRatioSpec(
            symbol="LST:USD",
            staking_pair="LST:USD",
            underlying_pair="BASE:EUR",
            underlying_asset="BASE",
            accrual_mode=RewardAccrualMode.CLAIMABLE_REWARDS,
        )
