"""Immutable provider-neutral domain objects.

Provider adapters may never leak their wire formats beyond this module's types.
Decimal is retained internally; the HTTP schema deliberately converts it to a
JSON number at the final serialization boundary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def decimal(value: Decimal | str | int | float) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid decimal value") from exc
    if not result.is_finite():
        raise ValueError("numeric value must be finite")
    try:
        wire_value = float(result)
    except (OverflowError, ValueError) as exc:
        raise ValueError("numeric value is not representable as a JSON number") from exc
    if not math.isfinite(wire_value) or (result != 0 and wire_value == 0):
        raise ValueError("numeric value is not representable as a finite JSON number")
    return result


@dataclass(frozen=True, slots=True)
class SourceComponent:
    symbol: str
    provider: str
    price: Decimal
    as_of: datetime
    feed: str | None = None
    role: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", decimal(self.price))
        object.__setattr__(self, "as_of", ensure_utc(self.as_of))
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("component price must be finite and positive")


@dataclass(frozen=True, slots=True)
class ProviderQuote:
    symbol: str
    price: Decimal
    as_of: datetime
    provider: str
    feed: str
    price_basis: str = "last_trade"
    market_status: str = "open"
    is_derived: bool = False
    components: tuple[SourceComponent, ...] = ()
    fallback_level: int = 0
    license_scope: str = "personal_internal"
    coverage: str | None = None
    market_status_as_of: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", decimal(self.price))
        object.__setattr__(self, "as_of", ensure_utc(self.as_of))
        if self.market_status_as_of is not None:
            object.__setattr__(
                self,
                "market_status_as_of",
                ensure_utc(self.market_status_as_of),
            )
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("price must be finite and positive")
        if self.fallback_level < 0:
            raise ValueError("fallback level cannot be negative")
        if self.market_status not in {"open", "closed", "unknown"}:
            raise ValueError(f"invalid market_status: {self.market_status}")


@dataclass(frozen=True, slots=True)
class PricePoint:
    symbol: str
    timestamp: datetime
    price: Decimal
    provider: str
    is_derived: bool = False
    interval: str = "1m"

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_utc(self.timestamp))
        object.__setattr__(self, "price", decimal(self.price))
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("price must be finite and positive")

    @property
    def as_of(self) -> datetime:
        return self.timestamp


@dataclass(frozen=True, slots=True)
class AggregatePrice:
    symbol: str
    bucket_start: datetime
    interval_seconds: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    sample_count: int
    provider: str
    is_derived: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "bucket_start", ensure_utc(self.bucket_start))
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, decimal(getattr(self, name)))
        if self.interval_seconds <= 0 or self.sample_count <= 0:
            raise ValueError("aggregate interval and sample count must be positive")
        if not all(getattr(self, name).is_finite() for name in ("open", "high", "low", "close")):
            raise ValueError("aggregate values must be finite")
        if (
            self.low <= 0
            or self.high < max(self.open, self.close)
            or self.low > min(self.open, self.close)
        ):
            raise ValueError("invalid aggregate OHLC values")


@dataclass(frozen=True, slots=True)
class DividendEvent:
    symbol: str
    ex_date: date
    payment_date: date | None
    amount: Decimal
    currency: str
    frequency: str
    provider: str
    event_type: str = "regular_cash"
    declared_date: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", decimal(self.amount))
        if not self.amount.is_finite() or self.amount < 0:
            raise ValueError("dividend amount cannot be negative")


class RewardAccrualMode(StrEnum):
    """How a staking asset makes rewards economically available to its holder."""

    VALUE_ACCRUING = "value_accruing"
    REBASING_BALANCE = "rebasing_balance"
    DISTRIBUTED_UNITS = "distributed_units"
    CLAIMABLE_REWARDS = "claimable_rewards"


class YieldRateType(StrEnum):
    """Compounding convention used by an annualized yield observation."""

    APR = "apr"
    APY = "apy"


@dataclass(frozen=True, slots=True)
class AccrualIndexPoint:
    """Provider-neutral value of one staking token in its underlying asset."""

    symbol: str
    underlying_asset: str
    value: Decimal
    as_of: datetime
    provider: str
    kind: str = "redemption_rate"

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", decimal(self.value))
        object.__setattr__(self, "as_of", ensure_utc(self.as_of))
        if not self.symbol.strip() or not self.underlying_asset.strip():
            raise ValueError("accrual index symbols must not be empty")
        if not self.provider.strip() or not self.kind.strip():
            raise ValueError("accrual index provider and kind must not be empty")
        if self.value <= 0:
            raise ValueError("accrual index value must be positive")


@dataclass(frozen=True, slots=True)
class YieldQuality:
    """Freshness and confidence metadata independent from quote quality."""

    stale: bool = False
    staleness_ms: int = 0
    confidence: str = "high"

    def __post_init__(self) -> None:
        if self.staleness_ms < 0:
            raise ValueError("yield staleness cannot be negative")
        if self.confidence not in {"high", "medium", "low"}:
            raise ValueError("yield confidence must be high, medium, or low")


@dataclass(frozen=True, slots=True)
class YieldMetric:
    symbol: str
    value: Decimal
    as_of: datetime
    method: str
    provider: str
    is_proxy: bool = False
    components: tuple[SourceComponent, ...] = ()
    rate_type: YieldRateType | None = None
    observation_window_days: Decimal | None = None
    accrual_mode: RewardAccrualMode | None = None
    underlying_asset: str | None = None
    is_estimate: bool = False
    accrual_index: AccrualIndexPoint | None = None
    quality: YieldQuality | None = None
    fallback_level: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", decimal(self.value))
        object.__setattr__(self, "as_of", ensure_utc(self.as_of))
        if self.rate_type is not None and not isinstance(self.rate_type, YieldRateType):
            object.__setattr__(self, "rate_type", YieldRateType(self.rate_type))
        if self.accrual_mode is not None and not isinstance(self.accrual_mode, RewardAccrualMode):
            object.__setattr__(self, "accrual_mode", RewardAccrualMode(self.accrual_mode))
        if self.observation_window_days is not None:
            object.__setattr__(
                self,
                "observation_window_days",
                decimal(self.observation_window_days),
            )
            if self.observation_window_days <= 0:
                raise ValueError("yield observation window must be positive")
        if self.underlying_asset is not None and not self.underlying_asset.strip():
            raise ValueError("underlying asset must not be empty")
        if self.fallback_level < 0:
            raise ValueError("yield fallback level cannot be negative")
        if not self.value.is_finite():
            raise ValueError("yield value must be finite")


@dataclass(frozen=True, slots=True)
class ChangeValue:
    percent: Decimal
    reference_price: Decimal
    reference_as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "percent", decimal(self.percent))
        object.__setattr__(self, "reference_price", decimal(self.reference_price))
        object.__setattr__(self, "reference_as_of", ensure_utc(self.reference_as_of))
        if (
            not self.percent.is_finite()
            or not self.reference_price.is_finite()
            or self.reference_price <= 0
        ):
            raise ValueError("change values must be finite with a positive reference")


@dataclass(frozen=True, slots=True)
class DividendMetric:
    yield_percent: Decimal
    ex_date: date
    payment_date: date | None
    amount: Decimal
    currency: str
    frequency: str
    method: str
    provider: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "yield_percent", decimal(self.yield_percent))
        object.__setattr__(self, "amount", decimal(self.amount))
        if not self.yield_percent.is_finite() or not self.amount.is_finite():
            raise ValueError("dividend values must be finite")


@dataclass(frozen=True, slots=True)
class YieldEstimate:
    percent: Decimal
    as_of: datetime
    method: str
    provider: str
    is_proxy: bool
    inputs: Mapping[str, Any] = field(default_factory=dict)
    rate_type: YieldRateType | None = None
    observation_window_days: Decimal | None = None
    accrual_mode: RewardAccrualMode | None = None
    underlying_asset: str | None = None
    is_estimate: bool = False
    accrual_index: AccrualIndexPoint | None = None
    quality: YieldQuality | None = None
    components: tuple[SourceComponent, ...] = ()
    fallback_level: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "percent", decimal(self.percent))
        object.__setattr__(self, "as_of", ensure_utc(self.as_of))
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        if self.rate_type is not None and not isinstance(self.rate_type, YieldRateType):
            object.__setattr__(self, "rate_type", YieldRateType(self.rate_type))
        if self.accrual_mode is not None and not isinstance(self.accrual_mode, RewardAccrualMode):
            object.__setattr__(self, "accrual_mode", RewardAccrualMode(self.accrual_mode))
        if self.observation_window_days is not None:
            object.__setattr__(
                self,
                "observation_window_days",
                decimal(self.observation_window_days),
            )
            if self.observation_window_days <= 0:
                raise ValueError("yield observation window must be positive")
        if self.underlying_asset is not None and not self.underlying_asset.strip():
            raise ValueError("underlying asset must not be empty")
        if self.fallback_level < 0:
            raise ValueError("yield fallback level cannot be negative")
        if not self.percent.is_finite():
            raise ValueError("yield estimate must be finite")


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    quote: ProviderQuote
    changes: Mapping[str, ChangeValue | None]
    dividend: DividendMetric | None = None
    estimated_annual_yield: YieldEstimate | None = None
    published_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "changes", MappingProxyType(dict(self.changes)))
        object.__setattr__(self, "published_at", ensure_utc(self.published_at))
