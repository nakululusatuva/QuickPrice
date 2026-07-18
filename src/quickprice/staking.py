"""Provider-neutral liquid-staking yield calculations."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, localcontext

from .domain import (
    AccrualIndexPoint,
    RewardAccrualMode,
    YieldEstimate,
    YieldMetric,
    YieldRateType,
    decimal,
)

_SECONDS_PER_YEAR = Decimal(365 * 24 * 60 * 60)

# ExchangeRateUpdated is a daily protocol index. One expected publication plus
# twelve hours of scheduling and chain/provider grace keeps a normal daily event
# current while detecting a missed update within two six-hour metadata cycles.
ONCHAIN_EXCHANGE_RATE_FRESHNESS_SECONDS = 36 * 60 * 60
ONCHAIN_EXCHANGE_RATE_TRAILING_APY_METHOD = "onchain_exchange_rate_trailing_apy"


def annualize_index_growth(
    reference: AccrualIndexPoint,
    current: AccrualIndexPoint,
    *,
    rate_type: YieldRateType = YieldRateType.APY,
) -> tuple[Decimal, Decimal]:
    """Annualize index growth over the exact elapsed time.

    The returned percentage uses QuickPrice's wire convention: ``3.25`` means
    3.25 percent. APY compounds the observed growth; APR linearly annualizes it.
    """

    if reference.symbol != current.symbol:
        raise ValueError("accrual index symbols must match")
    if reference.underlying_asset != current.underlying_asset:
        raise ValueError("accrual index underlying assets must match")
    elapsed_seconds = Decimal(str((current.as_of - reference.as_of).total_seconds()))
    if elapsed_seconds <= 0:
        raise ValueError("current accrual index must be newer than the reference")
    growth = current.value / reference.value
    annualization_factor = _SECONDS_PER_YEAR / elapsed_seconds
    with localcontext() as context:
        context.prec = 40
        if rate_type is YieldRateType.APY:
            annual_growth = (growth.ln() * annualization_factor).exp() - Decimal(1)
        else:
            annual_growth = (growth - Decimal(1)) * annualization_factor
        percent = decimal(annual_growth * Decimal(100))
        window_days = decimal(elapsed_seconds / Decimal(timedelta(days=1).total_seconds()))
    return percent, window_days


def estimate_from_staking_metric(metric: YieldMetric) -> YieldEstimate:
    """Convert a provider metric without losing its staking-yield semantics."""

    if metric.accrual_mode is None:
        raise ValueError("staking yield metrics require an accrual mode")
    if metric.rate_type is None:
        raise ValueError("staking yield metrics require an APR or APY rate type")
    inputs: dict[str, object] = {}
    if metric.accrual_index is not None:
        inputs["accrual_index"] = float(metric.accrual_index.value)
        inputs["accrual_index_as_of"] = metric.accrual_index.as_of.isoformat()
        inputs["accrual_index_kind"] = metric.accrual_index.kind
    return YieldEstimate(
        percent=metric.value,
        as_of=metric.as_of,
        method=metric.method,
        provider=metric.provider,
        is_proxy=metric.is_proxy,
        inputs=inputs,
        rate_type=metric.rate_type,
        observation_window_days=metric.observation_window_days,
        accrual_mode=metric.accrual_mode,
        underlying_asset=metric.underlying_asset,
        is_estimate=metric.is_estimate,
        accrual_index=metric.accrual_index,
        quality=metric.quality,
        components=metric.components,
        fallback_level=metric.fallback_level,
    )


def requires_staking_yield(accrual_mode: RewardAccrualMode | None) -> bool:
    """Return whether an instrument must expose staking-income metadata."""

    return accrual_mode is not None
