"""Deterministic synthetic quotes with explicit component freshness checks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from quickprice.domain import ProviderQuote

from ._models import component, quote
from .base import ProviderUnavailable, UnsupportedInstrument


class SyntheticComponentError(ProviderUnavailable):
    """A component was too old, too far apart, or invalid for synthesis."""


def _validate_components(
    provider_name: str,
    left: ProviderQuote,
    right: ProviderQuote,
    *,
    max_skew: timedelta,
    now: datetime | None,
    left_max_age: timedelta | None,
    right_max_age: timedelta | None,
) -> datetime:
    if max_skew.total_seconds() < 0:
        raise ValueError("max_skew cannot be negative")
    left_time = left.as_of.astimezone(UTC)
    right_time = right.as_of.astimezone(UTC)
    if abs(left_time - right_time) > max_skew:
        raise SyntheticComponentError(provider_name, "component timestamps exceed maximum skew")
    check_time = (now or datetime.now(UTC)).astimezone(UTC)
    if left_time > check_time + timedelta(seconds=5) or right_time > check_time + timedelta(
        seconds=5
    ):
        raise SyntheticComponentError(provider_name, "component timestamp is in the future")
    if left_max_age is not None and check_time - left_time > left_max_age:
        raise SyntheticComponentError(provider_name, "left component is stale")
    if right_max_age is not None and check_time - right_time > right_max_age:
        raise SyntheticComponentError(provider_name, "right component is stale")
    return min(left_time, right_time)


def _market_status(left: ProviderQuote, right: ProviderQuote) -> str:
    if left.market_status == right.market_status:
        return left.market_status
    if "unknown" in (left.market_status, right.market_status):
        return "unknown"
    # A cross is not live if either required market is closed.
    return "closed"


def _synthetic_quote(
    symbol: str,
    left: ProviderQuote,
    right: ProviderQuote,
    *,
    operation: Literal["multiply", "divide"],
    max_skew: timedelta,
    now: datetime | None = None,
    left_max_age: timedelta | None = None,
    right_max_age: timedelta | None = None,
    provider_name: str = "synthetic",
):
    as_of = _validate_components(
        provider_name,
        left,
        right,
        max_skew=max_skew,
        now=now,
        left_max_age=left_max_age,
        right_max_age=right_max_age,
    )
    try:
        price = left.price * right.price if operation == "multiply" else left.price / right.price
    except (ArithmeticError, ZeroDivisionError) as exc:
        raise SyntheticComponentError(provider_name, "invalid component arithmetic") from exc
    if price <= Decimal(0):
        raise SyntheticComponentError(provider_name, "synthetic price is not positive")
    roles = (
        ("multiplicand", "multiplier") if operation == "multiply" else ("numerator", "denominator")
    )
    components_list = []
    for item, role in zip((left, right), roles, strict=True):
        components_list.append(
            component(
                symbol=item.symbol,
                provider=item.provider,
                price=item.price,
                as_of=item.as_of,
                feed=item.feed,
                role=role,
            )
        )
        components_list.extend(
            component(
                symbol=child.symbol,
                provider=child.provider,
                price=child.price,
                as_of=child.as_of,
                feed=child.feed,
                role=f"{role}_{child.role or 'component'}",
            )
            for child in item.components
        )
    components = tuple(components_list)
    feed = "+".join(dict.fromkeys((left.feed, right.feed)))
    license_scope = (
        left.license_scope
        if left.license_scope == right.license_scope
        else "most_restrictive_component_terms"
    )
    return quote(
        symbol=symbol.strip().upper(),
        price=price,
        as_of=as_of,
        provider=provider_name,
        feed=feed,
        price_basis=f"synthetic_{operation}",
        market_status=_market_status(left, right),
        is_derived=True,
        components=components,
        fallback_level=max(left.fallback_level, right.fallback_level),
        license_scope=license_scope,
        coverage="derived_from_components",
    )


def synthesize_multiplication(
    symbol: str,
    left: ProviderQuote,
    right: ProviderQuote,
    *,
    max_skew: timedelta = timedelta(seconds=2),
    now: datetime | None = None,
    left_max_age: timedelta | None = None,
    right_max_age: timedelta | None = None,
    provider_name: str = "synthetic",
):
    return _synthetic_quote(
        symbol,
        left,
        right,
        operation="multiply",
        max_skew=max_skew,
        now=now,
        left_max_age=left_max_age,
        right_max_age=right_max_age,
        provider_name=provider_name,
    )


def synthesize_division(
    symbol: str,
    numerator: ProviderQuote,
    denominator: ProviderQuote,
    *,
    max_skew: timedelta = timedelta(seconds=2),
    now: datetime | None = None,
    numerator_max_age: timedelta | None = None,
    denominator_max_age: timedelta | None = None,
    provider_name: str = "synthetic",
):
    return _synthetic_quote(
        symbol,
        numerator,
        denominator,
        operation="divide",
        max_skew=max_skew,
        now=now,
        left_max_age=numerator_max_age,
        right_max_age=denominator_max_age,
        provider_name=provider_name,
    )


def synthesize_inverse(
    symbol: str,
    source: ProviderQuote,
    *,
    now: datetime | None = None,
    maximum_age: timedelta | None = None,
    provider_name: str = "synthetic",
):
    """Return the reciprocal of one positive component observation."""

    source_time = source.as_of.astimezone(UTC)
    check_time = (now or datetime.now(UTC)).astimezone(UTC)
    if source_time > check_time + timedelta(seconds=5):
        raise SyntheticComponentError(provider_name, "component timestamp is in the future")
    if maximum_age is not None and check_time - source_time > maximum_age:
        raise SyntheticComponentError(provider_name, "component is stale")
    try:
        price = Decimal(1) / source.price
    except (ArithmeticError, ZeroDivisionError) as exc:
        raise SyntheticComponentError(provider_name, "invalid component arithmetic") from exc
    if price <= 0:
        raise SyntheticComponentError(provider_name, "synthetic price is not positive")
    components = (
        component(
            symbol=source.symbol,
            provider=source.provider,
            price=source.price,
            as_of=source.as_of,
            feed=source.feed,
            role="denominator",
        ),
        *(
            component(
                symbol=child.symbol,
                provider=child.provider,
                price=child.price,
                as_of=child.as_of,
                feed=child.feed,
                role=f"denominator_{child.role or 'component'}",
            )
            for child in source.components
        ),
    )
    return quote(
        symbol=symbol.strip().upper(),
        price=price,
        as_of=source_time,
        provider=provider_name,
        feed=source.feed,
        price_basis="synthetic_inverse",
        market_status=source.market_status,
        is_derived=True,
        components=components,
        fallback_level=source.fallback_level,
        license_scope=source.license_scope,
        coverage="derived_from_components",
    )


def synthesize_wbeth(
    wbeth_eth: ProviderQuote,
    eth_usdc: ProviderQuote,
    *,
    now: datetime | None = None,
    provider_name: str = "synthetic_binance",
):
    """Primary WBETH:USDC formula: WBETH/ETH multiplied by ETH/USDC."""

    return synthesize_multiplication(
        "WBETH:USDC",
        wbeth_eth,
        eth_usdc,
        max_skew=timedelta(seconds=2),
        now=now,
        left_max_age=timedelta(seconds=15),
        right_max_age=timedelta(seconds=15),
        provider_name=provider_name,
    )


def synthesize_hkd_cnh(
    usd_cnh: ProviderQuote,
    usd_hkd: ProviderQuote,
    *,
    now: datetime | None = None,
    provider_name: str = "synthetic_fx",
):
    """HKD/CNH formula: USD/CNH divided by USD/HKD.

    The slower USD/HKD leg may be up to 20 minutes old. USD/CNH retains a
    tighter five-minute freshness requirement.
    """

    return synthesize_division(
        "HKD:CNH",
        usd_cnh,
        usd_hkd,
        max_skew=timedelta(minutes=20),
        now=now,
        numerator_max_age=timedelta(minutes=5),
        denominator_max_age=timedelta(minutes=20),
        provider_name=provider_name,
    )


@dataclass(frozen=True, slots=True)
class SyntheticRecipe:
    symbol: str
    left_symbol: str
    right_symbol: str
    operation: Literal["inverse", "multiply", "divide"]
    max_skew: timedelta
    left_max_age: timedelta | None = None
    right_max_age: timedelta | None = None
    provider_name: str = "synthetic"

    @classmethod
    def wbeth_primary(cls) -> SyntheticRecipe:
        return cls(
            symbol="WBETH:USDC",
            left_symbol="WBETH:ETH",
            right_symbol="ETH:USDC",
            operation="multiply",
            max_skew=timedelta(seconds=2),
            left_max_age=timedelta(seconds=15),
            right_max_age=timedelta(seconds=15),
            provider_name="synthetic_binance",
        )

    @classmethod
    def wbeth_usdt_fallback(cls) -> SyntheticRecipe:
        return cls(
            symbol="WBETH:USDC",
            left_symbol="WBETH:USDT",
            right_symbol="USDC:USDT",
            operation="divide",
            max_skew=timedelta(seconds=2),
            left_max_age=timedelta(seconds=15),
            right_max_age=timedelta(seconds=15),
            provider_name="synthetic_binance",
        )

    @classmethod
    def beth_okx_primary(cls) -> SyntheticRecipe:
        """Primary BETH formula using only synchronized OKX spot books."""

        return cls(
            symbol="BETH:USDC",
            left_symbol="OKX_BETH:ETH",
            right_symbol="OKX_ETH:USDC",
            operation="multiply",
            max_skew=timedelta(seconds=2),
            left_max_age=timedelta(seconds=15),
            right_max_age=timedelta(seconds=15),
            provider_name="synthetic_okx",
        )

    @classmethod
    def beth_okx_usdt_fallback(cls) -> SyntheticRecipe:
        """Alternate BETH formula using only synchronized OKX spot books."""

        return cls(
            symbol="BETH:USDC",
            left_symbol="OKX_BETH:USDT",
            right_symbol="OKX_USDC:USDT",
            operation="divide",
            max_skew=timedelta(seconds=2),
            left_max_age=timedelta(seconds=15),
            right_max_age=timedelta(seconds=15),
            provider_name="synthetic_okx",
        )

    @classmethod
    def hkd_cnh(cls) -> SyntheticRecipe:
        return cls(
            symbol="HKD:CNH",
            left_symbol="USD:CNH",
            right_symbol="USD:HKD",
            operation="divide",
            max_skew=timedelta(minutes=20),
            left_max_age=timedelta(minutes=5),
            right_max_age=timedelta(minutes=20),
            provider_name="synthetic_fx",
        )


class SyntheticQuoteProvider:
    """Quote capability backed by two independently routed component calls."""

    name = "synthetic"

    def __init__(
        self,
        resolver: Callable[[str], Awaitable[ProviderQuote]],
        recipes: Mapping[str, SyntheticRecipe] | tuple[SyntheticRecipe, ...],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._resolver = resolver
        if isinstance(recipes, Mapping):
            values = recipes.values()
        else:
            values = recipes
        self._recipes = {recipe.symbol.strip().upper(): recipe for recipe in values}
        self._clock = clock

    async def get_quote(self, symbol: str):
        normalized = symbol.strip().upper()
        try:
            recipe = self._recipes[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc
        if recipe.operation == "inverse":
            source = await self._resolver(recipe.left_symbol)
            return synthesize_inverse(
                normalized,
                source,
                now=self._clock(),
                maximum_age=recipe.left_max_age,
                provider_name=recipe.provider_name,
            )
        left, right = await asyncio.gather(
            self._resolver(recipe.left_symbol),
            self._resolver(recipe.right_symbol),
        )
        if recipe.operation == "multiply":
            return synthesize_multiplication(
                normalized,
                left,
                right,
                max_skew=recipe.max_skew,
                now=self._clock(),
                left_max_age=recipe.left_max_age,
                right_max_age=recipe.right_max_age,
                provider_name=recipe.provider_name,
            )
        return synthesize_division(
            normalized,
            left,
            right,
            max_skew=recipe.max_skew,
            now=self._clock(),
            numerator_max_age=recipe.left_max_age,
            denominator_max_age=recipe.right_max_age,
            provider_name=recipe.provider_name,
        )


def synthesize_history(
    recipe: SyntheticRecipe,
    left_points,
    right_points,
    *,
    interval: str,
    limit: int | None = None,
):
    """Combine ordered history using the last right value at/before each left value."""

    from ._models import point

    left_ordered = sorted(left_points, key=lambda item: item.timestamp)
    if recipe.operation == "inverse":
        result = []
        for source in left_ordered:
            try:
                price_value = Decimal(1) / source.price
            except ArithmeticError, ZeroDivisionError:
                continue
            if price_value <= 0:
                continue
            result.append(
                point(
                    symbol=recipe.symbol,
                    timestamp=source.timestamp,
                    price=price_value,
                    provider=recipe.provider_name,
                    interval=interval,
                    is_derived=True,
                )
            )
        if limit is not None:
            result = result[-max(0, limit) :]
        return tuple(result)
    right_ordered = sorted(right_points, key=lambda item: item.timestamp)
    result = []
    right_index = 0
    last_right = None
    for left in left_ordered:
        while (
            right_index < len(right_ordered)
            and right_ordered[right_index].timestamp <= left.timestamp
        ):
            last_right = right_ordered[right_index]
            right_index += 1
        if last_right is None or left.timestamp - last_right.timestamp > recipe.max_skew:
            continue
        try:
            price_value = (
                left.price * last_right.price
                if recipe.operation == "multiply"
                else left.price / last_right.price
            )
        except ArithmeticError, ZeroDivisionError:
            continue
        if price_value <= 0:
            continue
        result.append(
            point(
                symbol=recipe.symbol,
                timestamp=left.timestamp,
                price=price_value,
                provider=recipe.provider_name,
                interval=interval,
                is_derived=True,
            )
        )
    if limit is not None:
        result = result[-max(0, limit) :]
    return tuple(result)


class SyntheticHistoryProvider:
    """History capability using the same formulas and skew rules as live quotes."""

    name = "synthetic_history"

    def __init__(
        self, resolver, recipes: Mapping[str, SyntheticRecipe] | tuple[SyntheticRecipe, ...]
    ):
        self._resolver = resolver
        values = recipes.values() if isinstance(recipes, Mapping) else recipes
        self._recipes = {recipe.symbol.strip().upper(): recipe for recipe in values}

    async def get_history(
        self,
        symbol: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ):
        normalized = symbol.strip().upper()
        try:
            recipe = self._recipes[normalized]
        except KeyError as exc:
            raise UnsupportedInstrument(self.name, f"unsupported symbol {normalized}") from exc
        if recipe.operation == "inverse":
            left_points = await self._resolver(
                recipe.left_symbol,
                interval=interval,
                start=start,
                end=end,
                limit=limit,
            )
            right_points = ()
        else:
            left_points, right_points = await asyncio.gather(
                self._resolver(
                    recipe.left_symbol,
                    interval=interval,
                    start=start,
                    end=end,
                    limit=limit,
                ),
                self._resolver(
                    recipe.right_symbol,
                    interval=interval,
                    start=start - recipe.max_skew,
                    end=end,
                    limit=None if limit is None else limit + 1,
                ),
            )
        result = synthesize_history(
            recipe,
            left_points,
            right_points,
            interval=interval,
            limit=limit,
        )
        if left_points and (right_points or recipe.operation == "inverse") and not result:
            raise SyntheticComponentError(
                recipe.provider_name,
                "historical component timestamps have no valid overlap",
            )
        return result
