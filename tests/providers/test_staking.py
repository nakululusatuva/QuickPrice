from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest

from quickprice.domain import (
    AccrualIndexPoint,
    PricePoint,
    RewardAccrualMode,
    YieldMetric,
    YieldRateType,
)
from quickprice.provider_factory import (
    create_builtin_binance_yield_provider,
    create_builtin_ethereum_yield_provider,
    create_builtin_lido_provider,
)
from quickprice.providers.base import (
    Capability,
    MalformedResponse,
    ProviderUnavailable,
    UnsupportedInstrument,
)
from quickprice.providers.router import ProviderRouter
from quickprice.providers.staking import (
    StakingMarketRatioSpec,
    StakingMarketRatioYieldProvider,
)
from quickprice.staking import ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS


def _uint256(value: Decimal) -> str:
    integer = int(value * Decimal(10**18))
    return f"0x{integer:064x}"


@pytest.mark.asyncio
async def test_ethereum_yield_uses_bounded_multi_rpc_route_budget_not_global_timeout():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = create_builtin_ethereum_yield_provider(
        "https://rpc.example.invalid",
        request_timeout=8,
        clock=lambda: now,
    )
    expected = YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal("2.4"),
        as_of=now,
        method="onchain_exchange_rate_trailing_apy",
        provider=provider.name,
        rate_type=YieldRateType.APY,
        accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
        underlying_asset="ETH",
        is_estimate=True,
    )

    async def slower_than_global_timeout(_symbol: str) -> YieldMetric:
        await asyncio.sleep(0.02)
        return expected

    provider.get_yield = AsyncMock(side_effect=slower_than_global_timeout)
    router = ProviderRouter(
        {("WBETH:USDC", Capability.YIELD): [provider]},
        timeout_seconds=0.005,
    )
    try:
        result = await router.get_yield("WBETH:USDC")
    finally:
        await router.close()

    assert provider.request_timeout == 8
    assert provider.routing_timeout_seconds == 64
    assert result == expected


@pytest.mark.asyncio
async def test_ethereum_rpc_keeps_individual_http_request_timeout():
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self, **_kwargs):
            return {"jsonrpc": "2.0", "id": 1, "result": "0x1"}

    class Session:
        def __init__(self):
            self.timeout = None

        def request(self, *_args, **kwargs):
            self.timeout = kwargs["timeout"]
            return Response()

    session = Session()
    provider = create_builtin_ethereum_yield_provider(
        "https://rpc.example.invalid",
        session=session,
        request_timeout=0.125,
    )

    result = await provider._rpc("https://rpc.example.invalid", "eth_chainId", ())

    assert result == "0x1"
    assert session.timeout == 0.125
    assert provider.routing_timeout_seconds == 45


@pytest.mark.asyncio
async def test_ethereum_wbeth_exchange_rate_history_produces_trailing_apy():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = create_builtin_ethereum_yield_provider(
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

    metric = await provider.get_yield("WBETH:USD")

    assert metric.symbol == "WBETH:USD"
    assert metric.method == "onchain_exchange_rate_trailing_apy"
    assert metric.rate_type is YieldRateType.APY
    assert metric.accrual_mode is RewardAccrualMode.VALUE_ACCRUING
    assert metric.underlying_asset == "ETH"
    assert metric.is_proxy is False
    assert metric.is_estimate is True
    assert metric.fallback_level == 0
    assert metric.observation_window_days == Decimal("7")
    assert metric.as_of == now - timedelta(days=1)
    assert metric.accrual_index is not None
    assert metric.accrual_index.value == Decimal("1.01")
    assert float(metric.value) == pytest.approx((1.01 ** (365 / 7) - 1) * 100)
    assert metric.quality is not None
    assert metric.quality.confidence == "high"
    assert metric.quality.stale is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_age", "expected_stale"),
    [
        (timedelta(hours=16), False),
        (timedelta(seconds=ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS + 1), True),
    ],
)
async def test_ethereum_daily_exchange_rate_uses_daily_freshness_tolerance(
    event_age: timedelta,
    expected_stale: bool,
) -> None:
    now = datetime(2026, 7, 20, 16, tzinfo=UTC)
    provider = create_builtin_ethereum_yield_provider(
        "https://rpc.example.invalid",
        clock=lambda: now,
    )
    current = AccrualIndexPoint(
        symbol="WBETH:ETH",
        underlying_asset="ETH",
        value=Decimal("1.01"),
        as_of=now - event_age,
        provider=provider.name,
        kind="protocol_exchange_rate",
    )
    reference = AccrualIndexPoint(
        symbol="WBETH:ETH",
        underlying_asset="ETH",
        value=Decimal("1"),
        as_of=current.as_of - timedelta(days=7),
        provider=provider.name,
        kind="protocol_exchange_rate",
    )
    provider._current_index_on_endpoint = AsyncMock(return_value=current)
    provider._index_history_on_endpoint = AsyncMock(return_value=(reference,))

    metric = await provider._yield_on_endpoint(
        "https://rpc.example.invalid",
        provider._spec("WBETH:USDC"),
    )

    assert metric.quality is not None
    assert metric.quality.stale is expected_stale
    assert metric.quality.staleness_ms == int(event_age.total_seconds() * 1000)


@pytest.mark.asyncio
async def test_ethereum_rpc_fails_over_after_wrong_chain_id():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = create_builtin_ethereum_yield_provider(
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
    provider = create_builtin_ethereum_yield_provider(
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
    provider = create_builtin_ethereum_yield_provider(
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
    provider = create_builtin_ethereum_yield_provider(
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
async def test_binance_wbeth_rate_preserves_annual_fraction_and_signs_request():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = create_builtin_binance_yield_provider(
        "read-only-key",
        "secret",
        clock=lambda: now,
    )
    provider._request_json = AsyncMock(
        return_value={
            "rows": [
                {
                    "annualPercentageRate": "0.023",
                    "exchangeRate": "1.1",
                    "time": int((now - timedelta(hours=1)).timestamp() * 1000),
                }
            ],
            "total": "1",
        }
    )

    metric = await provider.get_yield("WBETH:USD")

    assert metric.symbol == "WBETH:USD"
    assert metric.value == Decimal("2.300")
    assert metric.rate_type is YieldRateType.APR
    assert metric.method == "binance_wbeth_rate_history_apr"
    assert metric.is_proxy is False
    assert metric.is_estimate is False
    assert metric.observation_window_days is None
    assert metric.fallback_level == 0
    assert metric.quality is not None
    assert metric.quality.confidence == "high"
    assert metric.quality.stale is False
    assert metric.accrual_index is not None
    assert metric.accrual_index.kind == "vendor_exchange_rate"
    assert metric.components[0].feed == "binance_eth_staking"
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


@pytest.mark.asyncio
async def test_binance_wbeth_rate_rejects_negative_vendor_apr():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = create_builtin_binance_yield_provider(
        "read-only-key",
        "secret",
        clock=lambda: now,
    )
    provider._request_json = AsyncMock(
        return_value={
            "rows": [
                {
                    "annualPercentageRate": "-0.001",
                    "exchangeRate": "1.1",
                    "time": int((now - timedelta(hours=1)).timestamp() * 1000),
                }
            ],
            "total": "1",
        }
    )

    with pytest.raises(MalformedResponse, match="APR cannot be negative"):
        await provider.get_yield("WBETH:USDC")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "expected_mode"),
    (
        ("STETH:USDC", RewardAccrualMode.REBASING_BALANCE),
        ("WSTETH:USDC", RewardAccrualMode.VALUE_ACCRUING),
        ("STETH:USD", RewardAccrualMode.REBASING_BALANCE),
        ("WSTETH:USD", RewardAccrualMode.VALUE_ACCRUING),
    ),
)
async def test_lido_official_sma_apr_preserves_token_accrual_mode(
    symbol: str,
    expected_mode: RewardAccrualMode,
):
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = create_builtin_lido_provider(clock=lambda: now)
    provider._request_json = AsyncMock(
        return_value={
            "data": {
                "aprs": [
                    {"timeUnix": int((now - timedelta(days=1)).timestamp()), "apr": "2.1"},
                    {"timeUnix": int((now - timedelta(hours=1)).timestamp()), "apr": "2.2"},
                ],
                "smaApr": "2.15",
            },
            "meta": {
                "symbol": "stETH",
                "address": "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84",
                "chainId": 1,
            },
        }
    )

    metric = await provider.get_yield(symbol)

    assert metric.value == Decimal("2.15")
    assert metric.method == "lido_steth_apr_7d_sma"
    assert metric.provider == "lido"
    assert metric.rate_type is YieldRateType.APR
    assert metric.observation_window_days == Decimal("7")
    assert metric.accrual_mode is expected_mode
    assert metric.underlying_asset == "ETH"
    assert metric.is_proxy is False
    assert metric.is_estimate is True
    assert metric.quality is not None
    assert metric.quality.confidence == "high"
    assert metric.quality.stale is False


@pytest.mark.asyncio
async def test_lido_apr_rejects_unexpected_contract_metadata():
    now = datetime(2026, 7, 20, tzinfo=UTC)
    provider = create_builtin_lido_provider(clock=lambda: now)
    provider._request_json = AsyncMock(
        return_value={
            "data": {
                "aprs": [{"timeUnix": int(now.timestamp()), "apr": "2.1"}],
                "smaApr": "2.1",
            },
            "meta": {"symbol": "stETH", "address": "0x0", "chainId": 1},
        }
    )

    with pytest.raises(MalformedResponse, match="unexpected Lido APR metadata"):
        await provider.get_yield("STETH:USDC")


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
                accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
            ),
        ),
        clock=lambda: now,
    )

    metric = await provider.get_yield("LST:USD")

    assert metric.method == "staking_market_ratio_30d_annualized"
    assert metric.is_proxy is True
    assert metric.is_estimate is True
    assert metric.fallback_level == 0
    assert metric.rate_type is YieldRateType.APY
    assert metric.accrual_mode is RewardAccrualMode.VALUE_ACCRUING
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
                accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
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
    assert metric.accrual_mode is RewardAccrualMode.VALUE_ACCRUING
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


def test_market_ratio_rejects_non_value_accruing_tokens() -> None:
    with pytest.raises(ValueError, match="only valid for value-accruing"):
        StakingMarketRatioSpec(
            symbol="LST:USD",
            staking_pair="LST:USD",
            underlying_pair="BASE:USD",
            underlying_asset="BASE",
            accrual_mode=RewardAccrualMode.REBASING_BALANCE,
        )


def test_ethereum_exchange_rate_spec_rebinds_only_the_quote_currency() -> None:
    provider = create_builtin_ethereum_yield_provider("https://ethereum.invalid")

    spec = provider._spec("WBETH:USD")

    assert spec.symbol == "WBETH:USD"
    assert spec.index_symbol == "WBETH:ETH"
    with pytest.raises(UnsupportedInstrument):
        provider._spec("WSTETH:USD")
