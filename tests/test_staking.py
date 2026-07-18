from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quickprice.domain import (
    AccrualIndexPoint,
    RewardAccrualMode,
    YieldMetric,
    YieldQuality,
    YieldRateType,
)
from quickprice.staking import annualize_index_growth, estimate_from_staking_metric


def test_all_staking_reward_accrual_modes_are_stable_wire_values():
    assert {item.value for item in RewardAccrualMode} == {
        "value_accruing",
        "rebasing_balance",
        "distributed_units",
        "claimable_rewards",
    }


def test_index_growth_uses_actual_elapsed_time_for_apy_and_apr():
    reference = AccrualIndexPoint(
        "LST:ETH",
        "ETH",
        Decimal("1"),
        datetime(2026, 6, 20, tzinfo=UTC),
        "fixture",
    )
    current = AccrualIndexPoint(
        "LST:ETH",
        "ETH",
        Decimal("1.01"),
        datetime(2026, 7, 20, 12, tzinfo=UTC),
        "fixture",
    )

    apy, apy_window = annualize_index_growth(reference, current)
    apr, apr_window = annualize_index_growth(reference, current, rate_type=YieldRateType.APR)

    elapsed_days = 30.5
    assert float(apy) == pytest.approx((1.01 ** (365 / elapsed_days) - 1) * 100)
    assert float(apr) == pytest.approx(1 * 365 / elapsed_days)
    assert float(apy_window) == pytest.approx(elapsed_days)
    assert apr_window == apy_window


def test_staking_estimate_keeps_rate_accrual_index_and_quality_metadata():
    as_of = datetime(2026, 7, 20, tzinfo=UTC)
    index = AccrualIndexPoint("WBETH:ETH", "ETH", Decimal("1.1"), as_of, "fixture")
    quality = YieldQuality(stale=False, staleness_ms=1000, confidence="high")
    metric = YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal("3.2"),
        as_of=as_of,
        method="fixture_apy",
        provider="fixture",
        rate_type=YieldRateType.APY,
        observation_window_days=Decimal("7"),
        accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
        underlying_asset="ETH",
        is_estimate=True,
        accrual_index=index,
        quality=quality,
    )

    estimate = estimate_from_staking_metric(metric)

    assert estimate.percent == metric.value
    assert estimate.rate_type is YieldRateType.APY
    assert estimate.accrual_mode is RewardAccrualMode.VALUE_ACCRUING
    assert estimate.accrual_index == index
    assert estimate.quality == quality
    assert estimate.inputs["accrual_index"] == 1.1


def test_invalid_accrual_index_and_quality_are_rejected():
    with pytest.raises(ValueError, match="positive"):
        AccrualIndexPoint(
            "LST:ETH",
            "ETH",
            Decimal("0"),
            datetime(2026, 7, 20, tzinfo=UTC),
            "fixture",
        )
    with pytest.raises(ValueError, match="confidence"):
        YieldQuality(confidence="unknown")
