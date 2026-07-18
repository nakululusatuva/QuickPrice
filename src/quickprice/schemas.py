"""Stable v1.1 wire models. All Decimal values become JSON numbers here."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .domain import QuoteSnapshot
from .registry import Instrument


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceComponentModel(WireModel):
    symbol: str
    provider: str
    feed: str | None
    role: str | None
    price: float
    as_of: datetime


class SourceModel(WireModel):
    provider: str
    feed: str
    fallback_level: int
    is_derived: bool
    components: list[SourceComponentModel]
    license_scope: str
    coverage: str | None


class QualityModel(WireModel):
    stale: bool
    staleness_ms: int


class ChangeModel(WireModel):
    percent: float
    reference_price: float
    reference_as_of: datetime


class DividendModel(WireModel):
    yield_percent: float
    ex_date: date
    payment_date: date | None
    amount: float
    currency: str
    frequency: str
    method: str
    provider: str


class YieldModel(WireModel):
    percent: float
    as_of: datetime
    method: str
    provider: str
    is_proxy: bool
    fallback_level: int
    rate_type: str | None
    observation_window_days: float | None
    accrual_mode: str | None
    underlying_asset: str | None
    is_estimate: bool
    accrual_index: AccrualIndexModel | None
    quality: YieldQualityModel | None
    components: list[SourceComponentModel]
    inputs: dict[str, Any]


class AccrualIndexModel(WireModel):
    symbol: str
    underlying_asset: str
    value: float
    as_of: datetime
    provider: str
    kind: str


class YieldQualityModel(WireModel):
    stale: bool
    staleness_ms: int
    stale_after_seconds: float
    confidence: str


class QuoteModel(WireModel):
    symbol: str
    base: str
    quote: str
    name: str
    description: str
    asset_class: str
    asset_type: str
    reward_accrual_mode: str | None
    underlying_asset: str | None
    price: float
    price_basis: str
    as_of: datetime
    market_status: str
    changes: dict[str, ChangeModel | None]
    dividend: DividendModel | None
    estimated_annual_yield: YieldModel | None
    source: SourceModel
    quality: QualityModel


class ErrorModel(WireModel):
    code: str
    message: str
    symbol: str | None = None


class EnvelopeModel(WireModel):
    schema_version: str = "1.1"
    request_id: str
    generated_at: datetime
    partial: bool
    data: Any
    errors: list[ErrorModel] = Field(default_factory=list)


class InstrumentModel(WireModel):
    symbol: str
    base: str
    quote: str
    name: str
    description: str
    asset_class: str
    asset_type: str
    reward_accrual_mode: str | None
    underlying_asset: str | None
    price_basis: str
    change_basis: str
    change_windows: dict[str, str]
    dividend_method: str | None
    yield_method: str | None


def instrument_to_wire(instrument: Instrument) -> InstrumentModel:
    return InstrumentModel(
        symbol=instrument.symbol,
        base=instrument.base,
        quote=instrument.quote,
        name=instrument.name,
        description=instrument.description,
        asset_class=instrument.asset_class.value,
        asset_type=instrument.asset_type,
        reward_accrual_mode=(
            instrument.reward_accrual_mode.value if instrument.reward_accrual_mode else None
        ),
        underlying_asset=instrument.underlying_asset,
        price_basis=instrument.price_basis,
        change_basis=instrument.change_basis,
        change_windows={
            "1h": "rolling_1_hour",
            "4h": "rolling_4_hours",
            "1d": "rolling_24_hours",
            "1w": "rolling_7_days",
            "1mo": "rolling_30_days",
            "1y": "rolling_365_days",
        },
        dividend_method=instrument.dividend_strategy,
        yield_method=instrument.yield_strategy.value if instrument.yield_strategy else None,
    )


def snapshot_to_wire(
    snapshot: QuoteSnapshot,
    instrument: Instrument,
    *,
    now: datetime,
    stale_after_seconds: float,
    yield_stale_after_seconds: float | None = None,
) -> QuoteModel:
    quote = snapshot.quote
    staleness_ms = max(0, int((now - quote.as_of).total_seconds() * 1000))
    changes = {
        name: (
            ChangeModel(
                percent=float(value.percent),
                reference_price=float(value.reference_price),
                reference_as_of=value.reference_as_of,
            )
            if value is not None
            else None
        )
        for name, value in snapshot.changes.items()
    }
    dividend = snapshot.dividend
    annual_yield = snapshot.estimated_annual_yield
    annual_yield_quality = None
    if annual_yield is not None:
        yield_threshold = (
            stale_after_seconds if yield_stale_after_seconds is None else yield_stale_after_seconds
        )
        yield_staleness_ms = max(0, int((now - annual_yield.as_of).total_seconds() * 1000))
        supplied_quality = annual_yield.quality
        annual_yield_quality = YieldQualityModel(
            stale=bool(supplied_quality and supplied_quality.stale)
            or yield_staleness_ms > int(yield_threshold * 1000),
            staleness_ms=yield_staleness_ms,
            stale_after_seconds=yield_threshold,
            confidence=(
                supplied_quality.confidence
                if supplied_quality is not None
                else "low"
                if annual_yield.is_proxy
                else "medium"
            ),
        )
    return QuoteModel(
        symbol=instrument.symbol,
        base=instrument.base,
        quote=instrument.quote,
        name=instrument.name,
        description=instrument.description,
        asset_class=instrument.asset_class.value,
        asset_type=instrument.asset_type,
        reward_accrual_mode=(
            instrument.reward_accrual_mode.value if instrument.reward_accrual_mode else None
        ),
        underlying_asset=instrument.underlying_asset,
        price=float(quote.price),
        price_basis=quote.price_basis,
        as_of=quote.as_of,
        market_status=quote.market_status,
        changes=changes,
        dividend=(
            DividendModel(
                yield_percent=float(dividend.yield_percent),
                ex_date=dividend.ex_date,
                payment_date=dividend.payment_date,
                amount=float(dividend.amount),
                currency=dividend.currency,
                frequency=dividend.frequency,
                method=dividend.method,
                provider=dividend.provider,
            )
            if dividend
            else None
        ),
        estimated_annual_yield=(
            YieldModel(
                percent=float(annual_yield.percent),
                as_of=annual_yield.as_of,
                method=annual_yield.method,
                provider=annual_yield.provider,
                is_proxy=annual_yield.is_proxy,
                fallback_level=annual_yield.fallback_level,
                rate_type=(
                    annual_yield.rate_type.value if annual_yield.rate_type is not None else None
                ),
                observation_window_days=(
                    float(annual_yield.observation_window_days)
                    if annual_yield.observation_window_days is not None
                    else None
                ),
                accrual_mode=(
                    annual_yield.accrual_mode.value
                    if annual_yield.accrual_mode is not None
                    else None
                ),
                underlying_asset=annual_yield.underlying_asset,
                is_estimate=annual_yield.is_estimate,
                accrual_index=(
                    AccrualIndexModel(
                        symbol=annual_yield.accrual_index.symbol,
                        underlying_asset=annual_yield.accrual_index.underlying_asset,
                        value=float(annual_yield.accrual_index.value),
                        as_of=annual_yield.accrual_index.as_of,
                        provider=annual_yield.accrual_index.provider,
                        kind=annual_yield.accrual_index.kind,
                    )
                    if annual_yield.accrual_index is not None
                    else None
                ),
                quality=annual_yield_quality,
                components=[
                    SourceComponentModel(
                        symbol=item.symbol,
                        provider=item.provider,
                        feed=item.feed,
                        role=item.role,
                        price=float(item.price),
                        as_of=item.as_of,
                    )
                    for item in annual_yield.components
                ],
                inputs=dict(annual_yield.inputs),
            )
            if annual_yield
            else None
        ),
        source=SourceModel(
            provider=quote.provider,
            feed=quote.feed,
            fallback_level=quote.fallback_level,
            is_derived=quote.is_derived,
            components=[
                SourceComponentModel(
                    symbol=item.symbol,
                    provider=item.provider,
                    feed=item.feed,
                    role=item.role,
                    price=float(item.price),
                    as_of=item.as_of,
                )
                for item in quote.components
            ],
            license_scope=quote.license_scope,
            coverage=quote.coverage,
        ),
        quality=QualityModel(
            stale=staleness_ms > int(stale_after_seconds * 1000),
            staleness_ms=staleness_ms,
        ),
    )
