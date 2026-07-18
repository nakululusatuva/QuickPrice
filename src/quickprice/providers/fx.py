"""Acyclic FX synthesis over vendor-backed USD spokes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

from quickprice.domain import PricePoint, ProviderQuote
from quickprice.fx import FX_HUB_SYMBOLS, fx_hub_requirements, split_fx_symbol

from ._models import component, point, quote
from .base import UnsupportedInstrument
from .synthetic import (
    SyntheticComponentError,
    SyntheticRecipe,
    synthesize_division,
    synthesize_history,
)

FX_MAX_SKEW = timedelta(minutes=20)
FX_HUB_MAX_AGES: Mapping[str, timedelta] = MappingProxyType(
    {
        symbol: timedelta(minutes=5) if symbol == "USD:CNH" else timedelta(minutes=20)
        for symbol in FX_HUB_SYMBOLS
    }
)


def _validate_single_component(
    source: ProviderQuote,
    *,
    now: datetime,
    max_age: timedelta,
    provider_name: str,
) -> datetime:
    source_time = source.as_of.astimezone(UTC)
    check_time = now.astimezone(UTC)
    if source_time > check_time + timedelta(seconds=5):
        raise SyntheticComponentError(provider_name, "component timestamp is in the future")
    if check_time - source_time > max_age:
        raise SyntheticComponentError(provider_name, "component is stale")
    return source_time


def synthesize_fx_inverse(
    symbol: str,
    source: ProviderQuote,
    *,
    now: datetime,
    max_age: timedelta,
    provider_name: str = "synthetic_fx",
) -> ProviderQuote:
    """Invert one USD spoke while preserving its complete provenance."""

    as_of = _validate_single_component(
        source,
        now=now,
        max_age=max_age,
        provider_name=provider_name,
    )
    try:
        price = Decimal(1) / source.price
    except (ArithmeticError, ZeroDivisionError) as exc:
        raise SyntheticComponentError(provider_name, "invalid component arithmetic") from exc
    return quote(
        symbol=symbol.strip().upper(),
        price=price,
        as_of=as_of,
        provider=provider_name,
        feed=source.feed,
        price_basis="synthetic_inverse",
        market_status=source.market_status,
        is_derived=True,
        components=(
            component(
                symbol=source.symbol,
                provider=source.provider,
                price=source.price,
                as_of=source.as_of,
                feed=source.feed,
                role="denominator",
            ),
        ),
        fallback_level=source.fallback_level,
        license_scope=source.license_scope,
        coverage="derived_from_components",
        market_status_as_of=source.market_status_as_of,
    )


class UsdHubFxQuoteProvider:
    """Derive non-hub pairs using only direct USD spokes."""

    name = "synthetic_fx"

    def __init__(
        self,
        resolver: Callable[[str], Awaitable[ProviderQuote]],
        *,
        max_ages: Mapping[str, timedelta] = FX_HUB_MAX_AGES,
        max_skew: timedelta = FX_MAX_SKEW,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._resolver = resolver
        self._max_ages = dict(max_ages)
        self._max_skew = max_skew
        self._clock = clock

    async def get_quote(self, symbol: str) -> ProviderQuote:
        normalized = symbol.strip().upper()
        try:
            base, quote_currency = split_fx_symbol(normalized)
            requirements = fx_hub_requirements(normalized)
        except ValueError as exc:
            raise UnsupportedInstrument(self.name, str(exc)) from exc
        if base == "USD":
            raise UnsupportedInstrument(self.name, "USD hub pairs must use direct providers")
        try:
            ages = tuple(self._max_ages[item] for item in requirements)
        except KeyError as exc:
            raise UnsupportedInstrument(
                self.name, f"missing USD hub policy for {exc.args[0]}"
            ) from exc

        now = self._clock()
        if quote_currency == "USD":
            source = await self._resolver(requirements[0])
            return synthesize_fx_inverse(
                normalized,
                source,
                now=now,
                max_age=ages[0],
                provider_name=self.name,
            )

        numerator, denominator = await asyncio.gather(
            self._resolver(requirements[0]),
            self._resolver(requirements[1]),
        )
        return synthesize_division(
            normalized,
            numerator,
            denominator,
            max_skew=self._max_skew,
            now=now,
            numerator_max_age=ages[0],
            denominator_max_age=ages[1],
            provider_name=self.name,
        )


class UsdHubFxHistoryProvider:
    """Derive non-hub histories with causal USD-spoke alignment."""

    name = "synthetic_fx_history"

    def __init__(self, resolver, *, max_skew: timedelta = FX_MAX_SKEW) -> None:
        self._resolver = resolver
        self._max_skew = max_skew

    async def get_history(
        self,
        symbol: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> tuple[PricePoint, ...]:
        normalized = symbol.strip().upper()
        try:
            base, quote_currency = split_fx_symbol(normalized)
            requirements = fx_hub_requirements(normalized)
        except ValueError as exc:
            raise UnsupportedInstrument(self.name, str(exc)) from exc
        if base == "USD":
            raise UnsupportedInstrument(self.name, "USD hub pairs must use direct providers")

        if quote_currency == "USD":
            source = await self._resolver(
                requirements[0],
                interval=interval,
                start=start,
                end=end,
                limit=limit,
            )
            result = tuple(
                point(
                    symbol=normalized,
                    timestamp=item.timestamp,
                    price=Decimal(1) / item.price,
                    provider="synthetic_fx",
                    interval=interval,
                    is_derived=True,
                )
                for item in source
            )
            if limit is not None:
                result = result[-max(0, limit) :]
            return result

        recipe = SyntheticRecipe(
            symbol=normalized,
            left_symbol=requirements[0],
            right_symbol=requirements[1],
            operation="divide",
            max_skew=self._max_skew,
            provider_name="synthetic_fx",
        )
        numerator, denominator = await asyncio.gather(
            self._resolver(
                requirements[0],
                interval=interval,
                start=start,
                end=end,
                limit=limit,
            ),
            self._resolver(
                requirements[1],
                interval=interval,
                start=start - self._max_skew,
                end=end,
                limit=None if limit is None else limit + 1,
            ),
        )
        result = synthesize_history(
            recipe,
            numerator,
            denominator,
            interval=interval,
            limit=limit,
        )
        if numerator and denominator and not result:
            raise SyntheticComponentError(
                self.name,
                "historical component timestamps have no valid overlap",
            )
        return result


__all__ = [
    "FX_HUB_MAX_AGES",
    "FX_MAX_SKEW",
    "UsdHubFxHistoryProvider",
    "UsdHubFxQuoteProvider",
    "synthesize_fx_inverse",
]
