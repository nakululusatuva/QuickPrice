"""Pure route compiler for managed instrument catalogs.

This module has no network side effects.  It turns validated, provider-neutral
instrument inputs into an immutable route plan that runtime wiring can warm and
activate atomically.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from math import ceil
from types import MappingProxyType
from typing import Any, Literal

from quickprice.instrument_policy import (
    BUILTIN_ALPACA_ALLOWED_DIVIDEND_SUBTYPES,
    BUILTIN_BINANCE_MIDPOINT_SYMBOLS,
    BUILTIN_BINANCE_SYMBOLS,
    BUILTIN_COINGECKO_COIN_IDS,
    BUILTIN_COINGECKO_COMPONENT_SKEW_SECONDS,
    BUILTIN_COINGECKO_NORMALIZATION_COIN_ID,
    BUILTIN_COINGECKO_NORMALIZATION_COMPONENT_SYMBOL,
    BUILTIN_FRED_POLICIES,
    BUILTIN_FX_HUB_MAX_AGE_SECONDS,
    BUILTIN_KRAKEN_MAX_QUOTE_AGE_SECONDS,
    BUILTIN_OKX_INTERNAL_ALIASES,
    BUILTIN_OKX_MARKETS,
    BUILTIN_PROVIDER_ROUTES,
    BUILTIN_STAKING_RATIO_POLICIES,
    BUILTIN_SYNTHETIC_RECIPES,
    BUILTIN_TWELVE_FX_CACHE_FLOOR_SECONDS,
    BUILTIN_TWELVE_FX_POLL_SETTING,
    COINGECKO_SUPPORTED_QUOTE_ASSETS,
    SUPPORTED_DIVIDEND_STRATEGIES,
    TREASURY_3M_FRED_SERIES,
    builtin_provider_symbols,
)
from quickprice.plugin_api import AssetClass, YieldStrategy
from quickprice.registry import normalize_symbol

from .base import Capability
from .descriptors import (
    ProviderKind,
    canonical_provider_name,
    get_provider_descriptor,
    validate_provider_binding_identity,
    validate_provider_symbol,
)

MAX_PROVIDER_CHAIN_LENGTH = 4
MAX_SYNTHETIC_DEPTH = 4


class RouteCompileError(ValueError):
    """A managed catalog cannot be converted to a safe provider plan."""


def _reward_mode_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value)).strip().lower()


@dataclass(frozen=True, slots=True)
class BuiltinProviderPolicy:
    routes: Mapping[str, tuple[str, ...]]
    provider_symbols: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SyntheticRouteInput:
    operation: Literal["inverse", "multiply", "divide"]
    inputs: tuple[str, ...]
    max_skew_seconds: float = 2.0
    input_max_age_seconds: tuple[float | None, ...] = ()

    def __post_init__(self) -> None:
        normalized_inputs = tuple(normalize_symbol(item) for item in self.inputs)
        expected = 1 if self.operation == "inverse" else 2
        if len(normalized_inputs) != expected:
            raise RouteCompileError(
                f"synthetic {self.operation} requires exactly {expected} input(s)"
            )
        if self.max_skew_seconds < 0:
            raise RouteCompileError("synthetic maximum skew cannot be negative")
        ages = self.input_max_age_seconds or (None,) * expected
        if len(ages) != expected or any(item is not None and item <= 0 for item in ages):
            raise RouteCompileError("synthetic input maximum ages are invalid")
        object.__setattr__(self, "inputs", normalized_inputs)
        object.__setattr__(self, "input_max_age_seconds", tuple(ages))


@dataclass(frozen=True, slots=True)
class InstrumentRouteInput:
    """Minimal provider-facing view of a managed instrument definition."""

    symbol: str
    asset_class: AssetClass | str
    asset_type: str
    quote_poll_seconds: float
    ownership: str = "custom"
    history_enabled: bool = True
    history_poll_seconds: float = 3_600.0
    history_backfill_days: int = 400
    metadata_poll_seconds: float = 21_600.0
    dividend_strategy: str | None = None
    yield_strategy: YieldStrategy | str | None = None
    underlying_asset: str | None = None
    reward_accrual_mode: str | None = None
    provider_symbols: Mapping[str, str] = field(default_factory=dict)
    routes: Mapping[Capability | str, Sequence[str]] = field(default_factory=dict)
    synthetic: SyntheticRouteInput | None = None

    def __post_init__(self) -> None:
        symbol = normalize_symbol(self.symbol)
        if ":" not in symbol:
            raise RouteCompileError(f"invalid instrument symbol: {self.symbol}")
        try:
            asset_class = AssetClass(self.asset_class)
        except ValueError as exc:
            raise RouteCompileError(f"invalid asset class for {symbol}") from exc
        if not self.asset_type.strip():
            raise RouteCompileError(f"instrument {symbol} requires an asset type")
        if (
            self.quote_poll_seconds <= 0
            or self.history_poll_seconds <= 0
            or self.metadata_poll_seconds <= 0
            or self.history_backfill_days <= 0
        ):
            raise RouteCompileError(f"instrument {symbol} has an invalid poll interval")
        bindings: dict[str, str] = {}
        for provider, vendor_symbol in self.provider_symbols.items():
            normalized_provider = provider.strip().lower()
            if normalized_provider in bindings:
                raise RouteCompileError(
                    f"duplicate provider binding for {symbol}: {normalized_provider}"
                )
            try:
                normalized_binding = validate_provider_symbol(normalized_provider, vendor_symbol)
            except ValueError as exc:
                raise RouteCompileError(
                    f"invalid binding for {symbol}/{normalized_provider}: {exc}"
                ) from exc
            try:
                validate_provider_binding_identity(
                    symbol,
                    asset_class,
                    normalized_provider,
                    normalized_binding,
                )
            except ValueError as exc:
                raise RouteCompileError(str(exc)) from exc
            bindings[normalized_provider] = normalized_binding
        routes: dict[Capability, tuple[str, ...]] = {}
        for capability, providers in self.routes.items():
            normalized_capability = Capability(capability)
            chain = tuple(item.strip().lower() for item in providers if item.strip())
            if not chain:
                raise RouteCompileError(
                    f"provider chain cannot be empty: {symbol}/{normalized_capability.value}"
                )
            routes[normalized_capability] = chain
        try:
            normalized_yield = (
                None if self.yield_strategy is None else YieldStrategy(self.yield_strategy)
            )
        except ValueError as exc:
            raise RouteCompileError(f"invalid yield strategy for {symbol}") from exc
        normalized_dividend = self.dividend_strategy
        if normalized_dividend is not None:
            normalized_dividend = normalized_dividend.strip().lower()
            if normalized_dividend not in SUPPORTED_DIVIDEND_STRATEGIES:
                raise RouteCompileError(f"invalid dividend strategy for {symbol}")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "asset_class", asset_class)
        object.__setattr__(self, "asset_type", self.asset_type.strip())
        object.__setattr__(
            self,
            "ownership",
            str(getattr(self.ownership, "value", self.ownership)).strip().lower(),
        )
        object.__setattr__(self, "provider_symbols", MappingProxyType(bindings))
        object.__setattr__(self, "routes", MappingProxyType(routes))
        object.__setattr__(self, "yield_strategy", normalized_yield)
        object.__setattr__(self, "dividend_strategy", normalized_dividend)
        object.__setattr__(
            self,
            "reward_accrual_mode",
            _reward_mode_value(self.reward_accrual_mode),
        )
        if self.underlying_asset is not None:
            object.__setattr__(self, "underlying_asset", self.underlying_asset.strip().upper())


@dataclass(frozen=True, slots=True)
class CompiledRoute:
    symbol: str
    capability: Capability
    providers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompiledInstrumentRoutes:
    symbol: str
    provider_symbols: Mapping[str, str]
    routes: Mapping[Capability, tuple[str, ...]]
    synthetic: SyntheticRouteInput | None

    def providers_for(self, capability: Capability | str) -> tuple[str, ...]:
        return self.routes.get(Capability(capability), ())


@dataclass(frozen=True, slots=True)
class CreditEstimateLine:
    provider: str
    capability: Capability
    symbol: str
    requests_per_day: int
    estimated_credits_per_day: Decimal
    cycles_per_day: int
    steady_state_requests_per_cycle: int
    cold_start_requests: int
    bases: tuple[str, ...]
    committed: bool
    ownerships: tuple[str, ...]
    quota_scopes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "capability": self.capability.value,
            "symbol": self.symbol,
            "requests_per_day": self.requests_per_day,
            "estimated_credits_per_day": float(self.estimated_credits_per_day),
            "cycles_per_day": self.cycles_per_day,
            "steady_state_requests_per_cycle": self.steady_state_requests_per_cycle,
            "cold_start_requests": self.cold_start_requests,
            "bases": list(self.bases),
            "committed": self.committed,
            "ownerships": list(self.ownerships),
            "quota_scopes": list(self.quota_scopes),
        }


@dataclass(frozen=True, slots=True)
class CompiledRoutePlan:
    instruments: Mapping[str, CompiledInstrumentRoutes]
    routes: tuple[CompiledRoute, ...]
    estimated_daily_credits: Mapping[str, Decimal]
    committed_daily_credits: Mapping[str, Decimal]
    committed_daily_credits_by_scope: Mapping[str, Mapping[str, Decimal]]
    worst_case_daily_credits: Mapping[str, Decimal]
    worst_case_daily_credits_by_scope: Mapping[str, Mapping[str, Decimal]]
    hard_capped_daily_credits: Mapping[str, Decimal]
    credit_estimates: tuple[CreditEstimateLine, ...]
    credit_budget_context: Mapping[str, Mapping[str, Any]]

    def instrument(self, symbol: str) -> CompiledInstrumentRoutes | None:
        return self.instruments.get(normalize_symbol(symbol))

    def providers_for(self, symbol: str, capability: Capability | str) -> tuple[str, ...]:
        item = self.instrument(symbol)
        return () if item is None else item.providers_for(capability)

    def as_dict(self) -> dict[str, Any]:
        return {
            "instruments": [
                {
                    "symbol": item.symbol,
                    "provider_symbols": dict(item.provider_symbols),
                    "routes": {
                        capability.value: list(providers)
                        for capability, providers in item.routes.items()
                    },
                    "synthetic": (
                        None
                        if item.synthetic is None
                        else {
                            "operation": item.synthetic.operation,
                            "inputs": list(item.synthetic.inputs),
                            "max_skew_seconds": item.synthetic.max_skew_seconds,
                            "input_max_age_seconds": list(item.synthetic.input_max_age_seconds),
                        }
                    ),
                }
                for item in self.instruments.values()
            ],
            "estimated_daily_credits": {
                provider: float(value) for provider, value in self.estimated_daily_credits.items()
            },
            "credit_plan": {
                "activation_daily_credits": {
                    provider: float(value)
                    for provider, value in self.estimated_daily_credits.items()
                },
                "committed_daily_credits": {
                    provider: float(value)
                    for provider, value in self.committed_daily_credits.items()
                },
                "committed_daily_credits_by_scope": {
                    provider: {scope: float(value) for scope, value in scopes.items()}
                    for provider, scopes in self.committed_daily_credits_by_scope.items()
                },
                "worst_case_daily_credits": {
                    provider: float(value)
                    for provider, value in self.worst_case_daily_credits.items()
                },
                "worst_case_daily_credits_by_scope": {
                    provider: {scope: float(value) for scope, value in scopes.items()}
                    for provider, scopes in self.worst_case_daily_credits_by_scope.items()
                },
                "hard_capped_daily_credits": {
                    provider: float(value)
                    for provider, value in self.hard_capped_daily_credits.items()
                },
                "estimates": [item.as_dict() for item in self.credit_estimates],
                "budgets": {
                    provider: dict(context)
                    for provider, context in self.credit_budget_context.items()
                },
                "assumptions": [
                    "Activation admission uses committed primary demand; fallback demand is reported separately.",
                    "The active generation's committed demand may be used as the activation baseline when it already exceeds a configured cap.",
                    "CoinGecko quote requests share the adapter's ten-minute batch cache.",
                    "History demand includes cold backfill pages and steady 1m, 5m, and 1d refreshes.",
                    "Synthetic and staking-ratio dependency calls are budgeted independently of direct collection.",
                    "Shared FX spokes are deduplicated by provider, capability, and symbol.",
                ],
            },
        }


def configured_provider_names(settings: Any | None) -> frozenset[str]:
    """Return provider names whose required credentials are configured."""

    from .descriptors import list_provider_descriptors

    return frozenset(
        descriptor.name
        for descriptor in list_provider_descriptors()
        if descriptor.credentials_configured(settings)
    )


def builtin_provider_policy(symbol: str) -> BuiltinProviderPolicy:
    """Return the settings-independent routes that preserve the shipped graph."""
    canonical = normalize_symbol(symbol)
    route_policy = BUILTIN_PROVIDER_ROUTES.get(canonical, {})
    routes = {
        str(capability): tuple(str(provider) for provider in providers)
        for capability, providers in route_policy.items()
    }
    return BuiltinProviderPolicy(
        routes=MappingProxyType(routes),
        provider_symbols=builtin_provider_symbols(canonical),
    )


def builtin_provider_policy_snapshot(symbols: Iterable[str]) -> dict[str, Any]:
    """Return JSON-safe built-in policies for v1-to-v2 catalog migration."""

    return {
        normalize_symbol(symbol): {
            "routes": {
                capability: list(providers)
                for capability, providers in builtin_provider_policy(symbol).routes.items()
            },
            "provider_symbols": dict(builtin_provider_policy(symbol).provider_symbols),
        }
        for symbol in symbols
    }


def _requires_vendor_symbol(provider: str) -> bool:
    descriptor = get_provider_descriptor(provider)
    return descriptor.kind is ProviderKind.MARKET_DATA or provider == "fred"


def _is_staking(item: InstrumentRouteInput) -> bool:
    return "staking" in item.asset_type.casefold()


def recommended_provider_chain(
    item: InstrumentRouteInput,
    capability: Capability | str,
    *,
    available_providers: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Recommend only compatible and actually bound market providers."""

    normalized_capability = Capability(capability)
    available = (
        None
        if available_providers is None
        else frozenset(name.strip().lower() for name in available_providers)
    )
    if item.synthetic is not None and normalized_capability in {
        Capability.QUOTE,
        Capability.HISTORY,
    }:
        candidates = ("synthetic",)
    elif item.asset_class is AssetClass.CRYPTO:
        if normalized_capability in {Capability.QUOTE, Capability.HISTORY}:
            candidates = ("binance", "okx", "kraken", "coingecko")
        elif normalized_capability is Capability.YIELD and _is_staking(item):
            candidates = _staking_yield_candidates(item)
        else:
            candidates = ()
    elif item.asset_class in {AssetClass.EQUITY, AssetClass.BOND}:
        if normalized_capability is Capability.QUOTE:
            candidates = ("alpaca", "finnhub", "twelve_data", "alpha_vantage")
        elif normalized_capability is Capability.HISTORY:
            candidates = ("alpaca", "twelve_data", "alpha_vantage")
        elif normalized_capability is Capability.DIVIDEND:
            candidates = ("alpaca",)
        elif normalized_capability is Capability.YIELD:
            candidates = ("fred",)
        else:
            candidates = ()
    elif item.asset_class is AssetClass.FX:
        base, quote = item.symbol.split(":", 1)
        candidates = (
            ("twelve_data", "alpha_vantage") if "USD" in {base, quote} else ("synthetic_fx",)
        )
    else:
        candidates = ()

    selected: list[str] = []
    for provider in candidates:
        descriptor = get_provider_descriptor(provider)
        if available is not None and provider not in available:
            continue
        if not descriptor.supports(item.asset_class, normalized_capability):
            continue
        if _requires_vendor_symbol(provider) and provider not in item.provider_symbols:
            continue
        selected.append(provider)
    return tuple(selected)


def _staking_yield_candidates(item: InstrumentRouteInput) -> tuple[str, ...]:
    builtin = BUILTIN_PROVIDER_ROUTES.get(item.symbol, {}).get(Capability.YIELD.value)
    if builtin:
        return tuple(str(provider) for provider in builtin)
    mode = _reward_mode_value(item.reward_accrual_mode)
    if mode == "value_accruing":
        return ("staking_market_ratio_proxy",)
    return ()


def required_capabilities(item: InstrumentRouteInput) -> tuple[Capability, ...]:
    required = [Capability.QUOTE]
    if item.history_enabled:
        required.append(Capability.HISTORY)
    if (
        item.dividend_strategy is not None
        or item.yield_strategy is YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED
    ):
        required.append(Capability.DIVIDEND)
    if item.yield_strategy not in {
        None,
        YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED,
    }:
        required.append(Capability.YIELD)
    return tuple(required)


def _validate_chain(
    item: InstrumentRouteInput,
    capability: Capability,
    providers: Sequence[str],
    *,
    available_providers: frozenset[str] | None,
    max_chain_length: int,
    drop_unconfigured: bool,
    allow_empty: bool,
) -> tuple[str, ...]:
    chain = tuple(provider.strip().lower() for provider in providers)
    if not chain:
        raise RouteCompileError(f"missing provider route: {item.symbol}/{capability.value}")
    if len(chain) > max_chain_length:
        raise RouteCompileError(
            f"provider chain exceeds {max_chain_length}: {item.symbol}/{capability.value}"
        )
    if len(set(chain)) != len(chain):
        raise RouteCompileError(
            f"provider chain contains duplicates: {item.symbol}/{capability.value}"
        )
    selected: list[str] = []
    for provider in chain:
        canonical_provider = canonical_provider_name(provider)
        if canonical_provider != provider:
            shipped = builtin_provider_policy(item.symbol).routes.get(capability.value, ())
            if item.ownership != "builtin" or provider not in shipped:
                raise RouteCompileError(
                    f"provider is not publicly selectable: {item.symbol}/{provider}"
                )
        try:
            descriptor = get_provider_descriptor(provider)
        except ValueError as exc:
            raise RouteCompileError(str(exc)) from exc
        if not descriptor.supports(item.asset_class, capability):
            raise RouteCompileError(
                f"provider {provider} is incompatible with {item.symbol}/{capability.value}"
            )
        if available_providers is not None and not {
            provider,
            canonical_provider_name(provider),
        }.intersection(available_providers):
            if drop_unconfigured:
                continue
            raise RouteCompileError(f"provider is not configured: {provider}")
        if _requires_vendor_symbol(provider) and provider not in item.provider_symbols:
            raise RouteCompileError(f"provider symbol is missing: {item.symbol}/{provider}")
        if (
            provider == "coingecko"
            and capability in {Capability.QUOTE, Capability.HISTORY}
            and item.symbol.split(":", 1)[1] not in COINGECKO_SUPPORTED_QUOTE_ASSETS
        ):
            raise RouteCompileError(
                f"CoinGecko route uses an unsupported quote asset: {item.symbol}"
            )
        if (
            provider == "staking_market_ratio_proxy"
            and _reward_mode_value(item.reward_accrual_mode) != "value_accruing"
        ):
            if item.ownership == "builtin":
                # Catalog v2 files written before this semantic correction may
                # retain STETH's obsolete proxy fallback. Drop it during
                # compilation while the reconciler replaces the built-in
                # income policy from the current immutable baseline.
                continue
            raise RouteCompileError(
                f"market-ratio staking yield requires value_accruing rewards: {item.symbol}"
            )
        selected.append(provider)
    if not selected:
        if allow_empty:
            return ()
        raise RouteCompileError(f"no configured provider remains: {item.symbol}/{capability.value}")
    if "synthetic" in selected or "synthetic_fx" in selected:
        if item.synthetic is None and "synthetic_fx" not in chain:
            raise RouteCompileError(f"synthetic route has no recipe: {item.symbol}")
    if "synthetic_fx" in selected:
        base, _ = item.symbol.split(":", 1)
        if base == "USD":
            raise RouteCompileError(
                f"USD hub pairs must use direct providers: {item.symbol}/{capability.value}"
            )
        if item.synthetic is not None:
            raise RouteCompileError(
                f"explicit synthetic recipes must use the synthetic provider: {item.symbol}"
            )
    return tuple(selected)


def _validate_synthetic_dependencies(
    items: Mapping[str, InstrumentRouteInput],
    *,
    maximum_depth: int,
) -> None:
    if maximum_depth <= 0:
        raise ValueError("maximum synthetic depth must be positive")
    visiting: set[str] = set()
    depths: dict[str, int] = {}

    def visit(symbol: str) -> int:
        if symbol in visiting:
            raise RouteCompileError(f"synthetic dependency cycle includes {symbol}")
        if symbol in depths:
            return depths[symbol]
        item = items.get(symbol)
        if item is None:
            return 0
        if item.synthetic is None:
            depths[symbol] = 0
            return 0
        visiting.add(symbol)
        dependency_depths: list[int] = []
        for dependency in item.synthetic.inputs:
            if dependency not in items:
                raise RouteCompileError(f"synthetic input is not defined: {symbol} -> {dependency}")
            dependency_depths.append(visit(dependency))
        visiting.remove(symbol)
        depth = 1 + max(dependency_depths, default=0)
        if depth > maximum_depth:
            raise RouteCompileError(f"synthetic dependency depth exceeds {maximum_depth}: {symbol}")
        depths[symbol] = depth
        return depth

    for symbol in items:
        visit(symbol)


def _validate_staking_ratio_dependencies(
    items: Mapping[str, InstrumentRouteInput],
    compiled: Mapping[str, CompiledInstrumentRoutes],
) -> None:
    """Prove every generic market-ratio input can provide historical prices."""

    for symbol, route in compiled.items():
        if "staking_market_ratio_proxy" not in route.providers_for(Capability.YIELD):
            continue
        builtin_policy = next(
            (policy for policy in BUILTIN_STAKING_RATIO_POLICIES if policy.symbol == symbol),
            None,
        )
        if builtin_policy is not None and any(
            dependency not in items
            for dependency in (builtin_policy.staking_pair, builtin_policy.underlying_pair)
        ):
            if all(
                dependency in BUILTIN_COINGECKO_COIN_IDS
                for dependency in (builtin_policy.staking_pair, builtin_policy.underlying_pair)
            ):
                continue
            raise RouteCompileError(f"built-in market-ratio dependency is unavailable: {symbol}")
        if builtin_policy is not None:
            staking_pair = builtin_policy.staking_pair
            underlying_pair = builtin_policy.underlying_pair
        else:
            item = items[symbol]
            if item.underlying_asset is None:
                raise RouteCompileError(
                    f"market-ratio staking yield requires an underlying asset: {symbol}"
                )
            quote_asset = symbol.split(":", 1)[1]
            staking_pair = symbol
            underlying_pair = f"{item.underlying_asset}:{quote_asset}"
        staking = compiled.get(staking_pair)
        underlying = compiled.get(underlying_pair)
        if staking is None or underlying is None:
            raise RouteCompileError(f"market-ratio staking dependency is not active: {symbol}")
        if not staking.providers_for(Capability.HISTORY):
            raise RouteCompileError(
                f"market-ratio staking token requires a usable history route: {symbol}"
            )
        if not underlying.providers_for(Capability.HISTORY):
            raise RouteCompileError(
                "market-ratio staking underlying pair has no usable history route: "
                f"{symbol} -> {underlying_pair}"
            )


def _poll_seconds(item: InstrumentRouteInput, capability: Capability) -> float:
    return {
        Capability.QUOTE: item.quote_poll_seconds,
        Capability.HISTORY: item.history_poll_seconds,
        Capability.DIVIDEND: item.metadata_poll_seconds,
        Capability.YIELD: item.metadata_poll_seconds,
    }[capability]


def _credit_budget_context(settings: Any | None) -> Mapping[str, Mapping[str, Any]]:
    if settings is None:
        return MappingProxyType({})
    twelve_limit = int(settings.twelve_daily_credits)
    twelve_reserve = min(
        int(settings.twelve_fx_reserve_credits),
        max(0, twelve_limit - 1),
    )
    from .alpha_vantage import ALPHA_VANTAGE_DEFAULT_FX_QUOTE_RESERVE_CREDITS
    from .coingecko import COINGECKO_DAILY_QUOTE_RESERVE_CREDITS

    alpha_limit = int(settings.alpha_vantage_daily_credits)
    alpha_reserve = min(
        ALPHA_VANTAGE_DEFAULT_FX_QUOTE_RESERVE_CREDITS,
        max(0, alpha_limit - 1),
    )
    coingecko_limit = int(settings.coingecko_monthly_credits) // 31
    coingecko_reserve = min(
        COINGECKO_DAILY_QUOTE_RESERVE_CREDITS,
        max(0, coingecko_limit - 1),
    )
    raw: dict[str, Mapping[str, Any]] = {
        "twelve_data": MappingProxyType(
            {
                "period": "utc_day",
                "limit": twelve_limit,
                "daily_limit": twelve_limit,
                "reserved_for_fx": twelve_reserve,
                "available_outside_reserve": max(0, twelve_limit - twelve_reserve),
            }
        ),
        "alpha_vantage": MappingProxyType(
            {
                "period": "utc_day",
                "limit": alpha_limit,
                "daily_limit": alpha_limit,
                "reserved_for_fx": alpha_reserve,
                "available_outside_reserve": max(0, alpha_limit - alpha_reserve),
            }
        ),
        "coingecko": MappingProxyType(
            {
                "period": "rolling_month_safe_utc_day",
                "monthly_limit": int(settings.coingecko_monthly_credits),
                "daily_limit": coingecko_limit,
                "reserved_for_quotes": coingecko_reserve,
                "available_outside_reserve": max(0, coingecko_limit - coingecko_reserve),
            }
        ),
        "finnhub": MappingProxyType(
            {
                "period": "minute",
                "limit": int(settings.finnhub_calls_per_minute),
                "daily_limit": int(settings.finnhub_calls_per_minute) * 1_440,
            }
        ),
    }
    return MappingProxyType(raw)


def _daily_credit_caps(settings: Any | None) -> Mapping[str, Decimal]:
    if settings is None:
        return MappingProxyType({})
    return MappingProxyType(
        {
            "twelve_data": Decimal(int(settings.twelve_daily_credits)),
            "alpha_vantage": Decimal(int(settings.alpha_vantage_daily_credits)),
            "coingecko": Decimal(int(settings.coingecko_monthly_credits) // 31),
            "finnhub": Decimal(int(settings.finnhub_calls_per_minute) * 1_440),
        }
    )


@dataclass(slots=True)
class _CreditDemand:
    poll_seconds: float
    history_backfill_days: int
    request_model: Literal["single", "collector_history"]
    bases: set[str]
    ownerships: set[str]
    quota_scopes: set[str]
    committed: bool


@dataclass(frozen=True, slots=True)
class _CreditRequestProfile:
    cycles_per_day: int
    steady_state_requests_per_cycle: int
    cold_start_requests: int

    @property
    def requests_per_day(self) -> int:
        return self.cold_start_requests + max(0, self.cycles_per_day - 1) * (
            self.steady_state_requests_per_cycle
        )


def _quota_scope(provider: str, asset_class: AssetClass) -> str:
    if canonical_provider_name(provider) == "twelve_data" and asset_class is AssetClass.FX:
        return "fx_reserved"
    return "general"


def _collector_history_profile(
    provider: str,
    *,
    poll_seconds: float,
    backfill_days: int,
) -> _CreditRequestProfile:
    """Estimate the collector's paged 1m, 5m, and 1d provider operations."""

    cycles = ceil(86_400 / poll_seconds)
    if canonical_provider_name(provider) == "alpha_vantage":
        # The free Alpha Vantage adapter rejects intraday intervals before it
        # reaches the network, leaving one daily request per collector cycle.
        return _CreditRequestProfile(cycles, 1, 1)

    page_bar_limit = 5_000
    intraday = (
        (60, min(2, backfill_days)),
        (300, min(45, backfill_days)),
    )
    cold_requests = 1  # The daily adapter is deliberately called once per cycle.
    steady_requests = 1
    for interval_seconds, retained_days in intraday:
        cold_bars = max(1, ceil(retained_days * 86_400 / interval_seconds))
        # The collector overlaps the newest bar, and scheduling delay can add
        # one more bucket. Including both makes the steady-state estimate
        # conservative without pretending every refresh repeats a full backfill.
        steady_bars = max(1, ceil(poll_seconds / interval_seconds) + 2)
        cold_requests += ceil(cold_bars / page_bar_limit)
        steady_requests += ceil(steady_bars / page_bar_limit)
    return _CreditRequestProfile(cycles, steady_requests, cold_requests)


def _credit_request_profile(provider: str, demand: _CreditDemand) -> _CreditRequestProfile:
    if demand.request_model == "collector_history":
        return _collector_history_profile(
            provider,
            poll_seconds=demand.poll_seconds,
            backfill_days=demand.history_backfill_days,
        )
    cycles = ceil(86_400 / demand.poll_seconds)
    return _CreditRequestProfile(cycles, 1, 1)


def _build_credit_estimates(
    items: Mapping[str, InstrumentRouteInput],
    compiled: Mapping[str, CompiledInstrumentRoutes],
    *,
    available_providers: frozenset[str] | None,
    settings: Any | None,
) -> tuple[
    tuple[CreditEstimateLine, ...],
    Mapping[str, Decimal],
    Mapping[str, Decimal],
    Mapping[str, Decimal],
    Mapping[str, Decimal],
    Mapping[str, Mapping[str, Decimal]],
    Mapping[str, Mapping[str, Decimal]],
]:
    """Build cache-aware candidate totals including derived-route dependencies."""

    from .fx import dynamic_fx_requirements

    demands: dict[tuple[str, Capability, str, str], _CreditDemand] = {}

    def add(
        provider: str,
        capability: Capability,
        symbol: str,
        poll_seconds: float,
        basis: str,
        *,
        asset_class: AssetClass,
        ownership: str,
        committed: bool,
        history_backfill_days: int = 400,
        request_model: Literal["single", "collector_history"] = "single",
        request_group: str | None = None,
    ) -> None:
        descriptor = get_provider_descriptor(provider)
        if descriptor.credit_cost(capability) == 0:
            return
        canonical_provider = descriptor.name
        key = (
            canonical_provider,
            capability,
            normalize_symbol(symbol),
            basis if request_group is None else request_group,
        )
        previous = demands.get(key)
        quota_scope = _quota_scope(canonical_provider, asset_class)
        if previous is None:
            demands[key] = _CreditDemand(
                poll_seconds=poll_seconds,
                history_backfill_days=history_backfill_days,
                request_model=request_model,
                bases={basis},
                ownerships={ownership},
                quota_scopes={quota_scope},
                committed=committed,
            )
        else:
            previous.poll_seconds = min(previous.poll_seconds, poll_seconds)
            previous.history_backfill_days = max(
                previous.history_backfill_days,
                history_backfill_days,
            )
            if request_model == "collector_history":
                previous.request_model = request_model
            previous.bases.add(basis)
            previous.ownerships.add(ownership)
            previous.quota_scopes.add(quota_scope)
            previous.committed = previous.committed or committed

    for symbol, route in compiled.items():
        item = items[symbol]
        for capability, providers in route.routes.items():
            poll_seconds = _poll_seconds(item, capability)
            for position, provider in enumerate(providers):
                add(
                    provider,
                    capability,
                    symbol,
                    poll_seconds,
                    "direct_route",
                    asset_class=item.asset_class,
                    ownership=item.ownership,
                    committed=position == 0,
                    history_backfill_days=item.history_backfill_days,
                    request_model=(
                        "collector_history" if capability is Capability.HISTORY else "single"
                    ),
                )

    for symbol, route in compiled.items():
        item = items[symbol]
        if item.synthetic is not None:
            for capability in (Capability.QUOTE, Capability.HISTORY):
                if "synthetic" not in route.providers_for(capability):
                    continue
                poll_seconds = _poll_seconds(item, capability)
                for dependency in item.synthetic.inputs:
                    dependency_route = compiled.get(dependency)
                    if dependency_route is None:
                        continue
                    for position, provider in enumerate(dependency_route.providers_for(capability)):
                        add(
                            provider,
                            capability,
                            dependency,
                            poll_seconds,
                            f"synthetic_dependency:{symbol}",
                            asset_class=items[dependency].asset_class,
                            ownership=item.ownership,
                            committed=position == 0,
                            history_backfill_days=item.history_backfill_days,
                            request_model=(
                                "collector_history"
                                if capability is Capability.HISTORY
                                else "single"
                            ),
                        )

        if item.asset_class is AssetClass.FX:
            for capability, synthetic_providers in (
                (Capability.QUOTE, frozenset({"synthetic_fx"})),
                (
                    Capability.HISTORY,
                    frozenset({"synthetic_fx", "synthetic_fx_history"}),
                ),
            ):
                if not synthetic_providers.intersection(route.providers_for(capability)):
                    continue
                poll_seconds = _poll_seconds(item, capability)
                for dependency in dynamic_fx_requirements(symbol):
                    installed = tuple(
                        provider
                        for provider in ("twelve_data", "alpha_vantage")
                        if available_providers is None or provider in available_providers
                    )
                    for position, provider in enumerate(installed):
                        add(
                            provider,
                            capability,
                            dependency,
                            poll_seconds,
                            f"fx_spoke_dependency:{symbol}",
                            asset_class=AssetClass.FX,
                            request_group="fx_spoke_dependency",
                            ownership=item.ownership,
                            committed=position == 0,
                            history_backfill_days=item.history_backfill_days,
                            request_model=(
                                "collector_history"
                                if capability is Capability.HISTORY
                                else "single"
                            ),
                        )

        if "staking_market_ratio_proxy" in route.providers_for(Capability.YIELD):
            builtin_policy = next(
                (policy for policy in BUILTIN_STAKING_RATIO_POLICIES if policy.symbol == symbol),
                None,
            )
            if builtin_policy is not None:
                dependencies = (
                    builtin_policy.staking_pair,
                    builtin_policy.underlying_pair,
                )
            elif item.underlying_asset is not None:
                quote_asset = symbol.split(":", 1)[1]
                dependencies = (symbol, f"{item.underlying_asset}:{quote_asset}")
            else:
                dependencies = ()
            internal_dependencies = tuple(
                dependency for dependency in dependencies if dependency not in compiled
            )
            if internal_dependencies:
                for dependency in internal_dependencies:
                    if available_providers is None or "coingecko" in available_providers:
                        add(
                            "coingecko",
                            Capability.HISTORY,
                            dependency,
                            item.metadata_poll_seconds,
                            f"staking_ratio_dependency:{symbol}",
                            asset_class=AssetClass.CRYPTO,
                            ownership=item.ownership,
                            committed=True,
                        )
            else:
                for dependency in dependencies:
                    dependency_route = compiled.get(dependency)
                    if dependency_route is None:
                        continue
                    for position, provider in enumerate(
                        dependency_route.providers_for(Capability.HISTORY)
                    ):
                        add(
                            provider,
                            Capability.HISTORY,
                            dependency,
                            item.metadata_poll_seconds,
                            f"staking_ratio_dependency:{symbol}",
                            asset_class=items[dependency].asset_class,
                            ownership=item.ownership,
                            committed=position == 0,
                        )

    lines: list[CreditEstimateLine] = []
    coingecko_quote = [
        (key, value)
        for key, value in demands.items()
        if key[0] == "coingecko" and key[1] is Capability.QUOTE
    ]
    if coingecko_quote:
        from .coingecko import (
            COINGECKO_SHARED_QUOTE_CACHE_SECONDS,
            coingecko_simple_price_id_batches,
        )

        access_interval = min(value.poll_seconds for _, value in coingecko_quote)
        coin_ids = {BUILTIN_COINGECKO_NORMALIZATION_COIN_ID}
        for symbol, instrument_routes in compiled.items():
            if not any("coingecko" in providers for providers in instrument_routes.routes.values()):
                continue
            coin_id = items[symbol].provider_symbols.get("coingecko")
            if coin_id is not None:
                coin_ids.add(coin_id)
        for policy in BUILTIN_STAKING_RATIO_POLICIES:
            routes = compiled.get(policy.symbol)
            if routes is None or "staking_market_ratio_proxy" not in routes.providers_for(
                Capability.YIELD
            ):
                continue
            for dependency in (policy.staking_pair, policy.underlying_pair):
                coin_id = BUILTIN_COINGECKO_COIN_IDS.get(dependency)
                if coin_id is not None:
                    coin_ids.add(coin_id)
        batch_count = len(coingecko_simple_price_id_batches(tuple(coin_ids)))
        refreshes = ceil(86_400 / max(COINGECKO_SHARED_QUOTE_CACHE_SECONDS, access_interval))
        requests = refreshes * batch_count
        cost = get_provider_descriptor("coingecko").quote_credit_cost
        bases = {basis for _, demand in coingecko_quote for basis in demand.bases}
        ownerships = {ownership for _, demand in coingecko_quote for ownership in demand.ownerships}
        quota_scopes = {scope for _, demand in coingecko_quote for scope in demand.quota_scopes}
        bases.add("shared_batch_cache")
        bases.add(f"shared_batch_count:{batch_count}")
        lines.append(
            CreditEstimateLine(
                provider="coingecko",
                capability=Capability.QUOTE,
                symbol="*",
                requests_per_day=requests,
                estimated_credits_per_day=Decimal(requests) * cost,
                cycles_per_day=refreshes,
                steady_state_requests_per_cycle=batch_count,
                cold_start_requests=batch_count,
                bases=tuple(sorted(bases)),
                committed=any(demand.committed for _, demand in coingecko_quote),
                ownerships=tuple(sorted(ownerships)),
                quota_scopes=tuple(sorted(quota_scopes)),
            )
        )
        for key, _ in coingecko_quote:
            demands.pop(key)

    for (provider, capability, symbol, _request_group), demand in sorted(
        demands.items(),
        key=lambda item: (item[0][0], item[0][1].value, item[0][2], item[0][3]),
    ):
        descriptor = get_provider_descriptor(provider)
        profile = _credit_request_profile(provider, demand)
        requests = profile.requests_per_day
        lines.append(
            CreditEstimateLine(
                provider=provider,
                capability=capability,
                symbol=symbol,
                requests_per_day=requests,
                estimated_credits_per_day=(Decimal(requests) * descriptor.credit_cost(capability)),
                cycles_per_day=profile.cycles_per_day,
                steady_state_requests_per_cycle=profile.steady_state_requests_per_cycle,
                cold_start_requests=profile.cold_start_requests,
                bases=tuple(sorted(demand.bases)),
                committed=demand.committed,
                ownerships=tuple(sorted(demand.ownerships)),
                quota_scopes=tuple(sorted(demand.quota_scopes)),
            )
        )
    lines.sort(key=lambda item: (item.provider, item.capability.value, item.symbol))
    worst_case: defaultdict[str, Decimal] = defaultdict(Decimal)
    committed_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    worst_case_by_scope: defaultdict[str, defaultdict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    committed_by_scope: defaultdict[str, defaultdict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for line in lines:
        worst_case[line.provider] += line.estimated_credits_per_day
        for scope in line.quota_scopes:
            worst_case_by_scope[line.provider][scope] += line.estimated_credits_per_day
        if not line.committed:
            continue
        committed_totals[line.provider] += line.estimated_credits_per_day
        for scope in line.quota_scopes:
            committed_by_scope[line.provider][scope] += line.estimated_credits_per_day

    caps = _daily_credit_caps(settings)
    hard_capped: dict[str, Decimal] = {}
    for provider in worst_case:
        cap = caps.get(provider)
        hard_capped[provider] = (
            worst_case[provider] if cap is None else min(worst_case[provider], cap)
        )
    committed = dict(committed_totals)
    frozen_committed_by_scope = MappingProxyType(
        {
            provider: MappingProxyType(dict(scopes))
            for provider, scopes in committed_by_scope.items()
        }
    )
    frozen_worst_case_by_scope = MappingProxyType(
        {
            provider: MappingProxyType(dict(scopes))
            for provider, scopes in worst_case_by_scope.items()
        }
    )
    return (
        tuple(lines),
        MappingProxyType(committed),
        MappingProxyType(committed),
        MappingProxyType(dict(worst_case)),
        MappingProxyType(hard_capped),
        frozen_committed_by_scope,
        frozen_worst_case_by_scope,
    )


def incremental_credit_budget_errors(
    active: CompiledRoutePlan | None,
    candidate: CompiledRoutePlan,
) -> tuple[str, ...]:
    """Return activation errors while grandfathering an existing over-cap plan.

    A candidate may retain, but never replace or increase, exact committed
    demand that the active generation already runs above a newly lowered
    budget. Twelve Data's non-FX routes are additionally constrained to the
    portion outside the FX reserve, while every Twelve route still
    participates in the total cap.
    """

    def line_identity(line: CreditEstimateLine) -> tuple[Any, ...]:
        return (
            line.provider,
            line.capability.value,
            line.symbol,
            line.bases,
            line.quota_scopes,
        )

    def committed_lines(
        plan: CompiledRoutePlan | None,
        provider: str,
        *,
        quota_scope: str | None = None,
    ) -> Mapping[tuple[Any, ...], Decimal]:
        if plan is None:
            return {}
        totals: defaultdict[tuple[Any, ...], Decimal] = defaultdict(Decimal)
        for line in plan.credit_estimates:
            if not line.committed or line.provider != provider:
                continue
            if quota_scope is not None and quota_scope not in line.quota_scopes:
                continue
            totals[line_identity(line)] += line.estimated_credits_per_day
        return totals

    def retained_credits(provider: str, *, quota_scope: str | None = None) -> Decimal:
        active_lines = committed_lines(active, provider, quota_scope=quota_scope)
        candidate_lines = committed_lines(candidate, provider, quota_scope=quota_scope)
        return sum(
            (
                min(active_value, candidate_lines.get(identity, Decimal(0)))
                for identity, active_value in active_lines.items()
            ),
            Decimal(0),
        )

    errors: list[str] = []
    for provider, context in candidate.credit_budget_context.items():
        raw_limit = context.get("daily_limit")
        if raw_limit is None:
            continue
        configured_limit = Decimal(str(raw_limit))
        allowed = max(configured_limit, retained_credits(provider))
        candidate_value = candidate.committed_daily_credits.get(provider, Decimal(0))
        if candidate_value > allowed:
            errors.append(
                f"{provider} committed daily credits exceed the total budget: "
                f"{candidate_value} > {allowed}"
            )

    twelve_context = candidate.credit_budget_context.get("twelve_data")
    if twelve_context is not None:
        raw_general_limit = twelve_context.get("available_outside_reserve")
        if raw_general_limit is not None:
            configured_general_limit = Decimal(str(raw_general_limit))
            retained_general = retained_credits(
                "twelve_data",
                quota_scope="general",
            )
            allowed_general = max(configured_general_limit, retained_general)
            candidate_general = candidate.committed_daily_credits_by_scope.get(
                "twelve_data", {}
            ).get("general", Decimal(0))
            if candidate_general > allowed_general:
                errors.append(
                    "twelve_data non-FX committed daily credits exceed the "
                    f"unreserved budget: {candidate_general} > {allowed_general}"
                )
    return tuple(errors)


def compile_route_plan(
    definitions: Iterable[InstrumentRouteInput],
    *,
    settings: Any | None = None,
    available_providers: Iterable[str] | None = None,
    max_chain_length: int = MAX_PROVIDER_CHAIN_LENGTH,
    maximum_synthetic_depth: int = MAX_SYNTHETIC_DEPTH,
    daily_credit_limits: Mapping[str, int | float | Decimal] | None = None,
    drop_unconfigured: bool = False,
    strict: bool = True,
) -> CompiledRoutePlan:
    """Compile definitions into an immutable and fully validated route plan."""

    if max_chain_length <= 0:
        raise ValueError("maximum provider chain length must be positive")
    items: dict[str, InstrumentRouteInput] = {}
    for item in definitions:
        if not isinstance(item, InstrumentRouteInput):
            raise TypeError("route definitions must be InstrumentRouteInput instances")
        if item.symbol in items:
            raise RouteCompileError(f"duplicate instrument route definition: {item.symbol}")
        items[item.symbol] = item
    _validate_synthetic_dependencies(items, maximum_depth=maximum_synthetic_depth)
    available = (
        configured_provider_names(settings)
        if available_providers is None and settings is not None
        else (
            None
            if available_providers is None
            else frozenset(name.strip().lower() for name in available_providers)
        )
    )
    compiled: dict[str, CompiledInstrumentRoutes] = {}
    flattened: list[CompiledRoute] = []
    for item in items.values():
        routes: dict[Capability, tuple[str, ...]] = {}
        capabilities = required_capabilities(item)
        for capability in capabilities:
            explicit = item.routes.get(capability)
            candidates = (
                tuple(explicit)
                if explicit is not None
                else recommended_provider_chain(
                    item,
                    capability,
                    available_providers=available,
                )
            )
            if not candidates and not strict:
                continue
            chain = _validate_chain(
                item,
                capability,
                candidates,
                available_providers=available,
                max_chain_length=max_chain_length,
                drop_unconfigured=(
                    drop_unconfigured and (explicit is None or item.ownership == "builtin")
                ),
                allow_empty=not strict or item.ownership == "builtin",
            )
            if not chain:
                continue
            routes[capability] = chain
            flattened.append(CompiledRoute(item.symbol, capability, chain))
        compiled[item.symbol] = CompiledInstrumentRoutes(
            symbol=item.symbol,
            provider_symbols=MappingProxyType(dict(item.provider_symbols)),
            routes=MappingProxyType(routes),
            synthetic=item.synthetic,
        )
    _validate_staking_ratio_dependencies(items, compiled)
    (
        credit_estimates,
        activation_credits,
        committed_credits,
        worst_case_credits,
        hard_capped_credits,
        committed_credits_by_scope,
        worst_case_credits_by_scope,
    ) = _build_credit_estimates(
        items,
        compiled,
        available_providers=available,
        settings=settings,
    )
    if daily_credit_limits is not None:
        for raw_provider, raw_limit in daily_credit_limits.items():
            provider = raw_provider.strip().lower()
            limit = Decimal(str(raw_limit))
            if limit < 0:
                raise ValueError(f"credit limit cannot be negative: {provider}")
            used = activation_credits.get(provider, Decimal(0))
            if used > limit:
                raise RouteCompileError(
                    f"estimated daily credits exceed budget for {provider}: {used} > {limit}"
                )
    return CompiledRoutePlan(
        instruments=MappingProxyType(compiled),
        routes=tuple(flattened),
        estimated_daily_credits=activation_credits,
        committed_daily_credits=committed_credits,
        committed_daily_credits_by_scope=committed_credits_by_scope,
        worst_case_daily_credits=worst_case_credits,
        worst_case_daily_credits_by_scope=worst_case_credits_by_scope,
        hard_capped_daily_credits=hard_capped_credits,
        credit_estimates=credit_estimates,
        credit_budget_context=_credit_budget_context(settings),
    )


def instrument_route_input_from_definition(definition: Any) -> InstrumentRouteInput:
    """Adapt a managed-catalog model without coupling the compiler to Pydantic."""

    instrument = getattr(definition, "instrument", definition)
    income = getattr(definition, "income", None)
    history = getattr(definition, "history", None)
    provider_bindings = getattr(definition, "provider_symbols", ())
    if isinstance(provider_bindings, Mapping):
        symbols = dict(provider_bindings)
    else:
        symbols = {str(binding.provider): str(binding.symbol) for binding in provider_bindings}
    managed_yield_strategy = None if income is None else income.yield_strategy
    if managed_yield_strategy in {
        YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE,
        YieldStrategy.TREASURY_PROXY_MINUS_EXPENSE,
    }:
        managed_series = getattr(income, "fred_series", None)
        if managed_series is None:
            raise RouteCompileError("Treasury proxy yield requires IncomePolicy.fred_series")
        if (
            managed_yield_strategy is YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE
            and managed_series != TREASURY_3M_FRED_SERIES
        ):
            raise RouteCompileError(
                f"the three-month Treasury strategy requires {TREASURY_3M_FRED_SERIES}"
            )
        explicit_series = symbols.get("fred")
        if explicit_series is not None and explicit_series.strip().upper() != managed_series:
            raise RouteCompileError("FRED provider binding must match IncomePolicy.fred_series")
        symbols["fred"] = managed_series
    elif "fred" in symbols and income is not None:
        raise RouteCompileError("FRED provider binding requires the Treasury proxy yield strategy")
    if AssetClass(instrument.asset_class) is AssetClass.FX:
        vendor_pair = str(instrument.symbol).replace(":", "/")
        symbols.setdefault("twelve_data", vendor_pair)
        symbols.setdefault("alpha_vantage", vendor_pair)
    raw_routes = getattr(definition, "routes", ())
    if isinstance(raw_routes, Mapping):
        routes = dict(raw_routes)
    else:
        routes = {Capability(str(route.capability)): tuple(route.providers) for route in raw_routes}
    raw_synthetic = getattr(definition, "synthetic", None)
    synthetic = None
    if raw_synthetic is not None:
        max_ages = getattr(raw_synthetic, "input_max_age_seconds", ())
        if not max_ages:
            max_ages = tuple(
                value
                for value in (
                    getattr(raw_synthetic, "left_max_age_seconds", None),
                    getattr(raw_synthetic, "right_max_age_seconds", None),
                )
                if value is not None
            )
        synthetic = SyntheticRouteInput(
            operation=str(raw_synthetic.operation),
            inputs=tuple(raw_synthetic.inputs),
            max_skew_seconds=float(getattr(raw_synthetic, "max_skew_seconds", 2.0)),
            input_max_age_seconds=tuple(max_ages),
        )
    raw_ownership = getattr(definition, "ownership", "custom")
    return InstrumentRouteInput(
        symbol=str(instrument.symbol),
        asset_class=instrument.asset_class,
        asset_type=str(instrument.asset_type),
        quote_poll_seconds=float(instrument.quote_poll_seconds),
        ownership=str(getattr(raw_ownership, "value", raw_ownership)),
        history_enabled=bool(
            getattr(
                history,
                "enabled",
                getattr(instrument, "history_enabled", True),
            )
        ),
        history_poll_seconds=float(
            getattr(history, "poll_seconds", None)
            or getattr(definition, "history_poll_seconds", 3_600)
        ),
        history_backfill_days=int(
            getattr(history, "backfill_days", None)
            or getattr(definition, "history_backfill_days", 400)
        ),
        metadata_poll_seconds=float(getattr(definition, "metadata_poll_seconds", 21_600)),
        dividend_strategy=(
            getattr(instrument, "dividend_strategy", None)
            if income is None
            else income.dividend_strategy
        ),
        yield_strategy=(
            getattr(instrument, "yield_strategy", None) if income is None else income.yield_strategy
        ),
        underlying_asset=(
            getattr(instrument, "underlying_asset", None)
            if income is None
            else income.underlying_asset
        ),
        reward_accrual_mode=(
            getattr(instrument, "reward_accrual_mode", None)
            if income is None
            else (None if income.reward_accrual_mode is None else income.reward_accrual_mode.value)
        ),
        provider_symbols=symbols,
        routes=routes,
        synthetic=synthetic,
    )


def compile_catalog_route_plan(
    definitions: Iterable[Any],
    **kwargs: Any,
) -> CompiledRoutePlan:
    return compile_route_plan(
        (instrument_route_input_from_definition(item) for item in definitions),
        **kwargs,
    )


def _active_definitions(definitions: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(
        item
        for item in definitions
        if bool(getattr(item, "enabled", True)) and not bool(getattr(item, "archived", False))
    )


def _bindings_by_provider(
    items: Sequence[InstrumentRouteInput],
    plan: CompiledRoutePlan,
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = defaultdict(dict)
    for item in items:
        routed_providers = {
            provider for chain in plan.instrument(item.symbol).routes.values() for provider in chain
        }
        for provider, vendor_symbol in item.provider_symbols.items():
            if provider in routed_providers:
                result[provider][item.symbol] = vendor_symbol
    return dict(result)


def _replace_graph_provider(
    graph: Any,
    name: str,
    replacement: Any,
    *,
    metrics: Any | None,
) -> None:
    previous = graph.providers.get(name)
    if previous is None:
        return
    graph.router.replace_provider_instance(previous, replacement)
    graph.providers[name] = replacement
    set_metrics = getattr(replacement, "set_metrics", None)
    if callable(set_metrics):
        set_metrics(metrics)


def _proxy_options(settings: Any, provider: str) -> dict[str, str]:
    proxy = settings.proxy_url_for_provider(provider)
    return {"proxy_url": proxy} if proxy else {}


def _install_instance_bindings(
    graph: Any,
    settings: Any,
    definitions: Sequence[Any],
    inputs: Sequence[InstrumentRouteInput],
    plan: CompiledRoutePlan,
    *,
    metrics: Any | None,
) -> None:
    """Replace adapters before publication using generation-local bindings."""

    from quickprice.domain import RewardAccrualMode

    from .alpaca import AlpacaProvider
    from .alpha_vantage import AlphaVantageProvider, alpha_vantage_quota_budget
    from .binance import BinanceProvider
    from .coingecko import CoinGeckoProvider, coingecko_quota_budget
    from .finnhub import FinnhubProvider
    from .fred import FredProvider
    from .fx import UsdHubFxHistoryProvider, UsdHubFxQuoteProvider, dynamic_fx_requirements
    from .kraken import KrakenProvider
    from .okx import OkxMarketProvider
    from .quota import daily_budget, minute_budget
    from .staking import StakingMarketRatioSpec, StakingMarketRatioYieldProvider
    from .twelve_data import TwelveDataProvider

    bindings = _bindings_by_provider(inputs, plan)
    active_symbols = frozenset(plan.instruments)
    managed_fx_requirements: dict[str, tuple[str, ...]] = {}
    for item in inputs:
        if item.asset_class is not AssetClass.FX:
            continue
        base, _ = item.symbol.split(":", 1)
        if base == "USD":
            continue
        requirements = dynamic_fx_requirements(item.symbol)
        managed_fx_requirements[item.symbol] = requirements
        for dependency in requirements:
            vendor_symbol = dependency.replace(":", "/")
            bindings.setdefault("twelve_data", {}).setdefault(dependency, vendor_symbol)
            bindings.setdefault("alpha_vantage", {}).setdefault(dependency, vendor_symbol)

    if "binance" in graph.providers:
        symbols = dict(bindings.get("binance", {}))
        for recipe in BUILTIN_SYNTHETIC_RECIPES.values():
            if recipe.symbol not in active_symbols or not recipe.provider_name.endswith("binance"):
                continue
            for dependency in recipe.inputs:
                vendor_symbol = BUILTIN_BINANCE_SYMBOLS.get(dependency)
                if vendor_symbol is not None:
                    symbols[dependency] = vendor_symbol
        replacement = BinanceProvider(
            symbol_bindings=symbols,
            midpoint_symbols=BUILTIN_BINANCE_MIDPOINT_SYMBOLS,
            **_proxy_options(settings, "binance"),
        )
        _replace_graph_provider(graph, "binance", replacement, metrics=metrics)

    if "kraken" in graph.providers:
        symbols: dict[str, str | tuple[str, str]] = dict(bindings.get("kraken", {}))
        replacement = KrakenProvider(
            symbol_bindings=symbols,
            max_quote_ages={
                symbol: timedelta(seconds=seconds)
                for symbol, seconds in BUILTIN_KRAKEN_MAX_QUOTE_AGE_SECONDS.items()
                if symbol in symbols
            },
            **_proxy_options(settings, "kraken"),
        )
        _replace_graph_provider(graph, "kraken", replacement, metrics=metrics)

    if "okx" in graph.providers:
        markets = dict(bindings.get("okx", {}))
        internal_aliases: dict[str, str] = {}
        if any(
            recipe.symbol in active_symbols and recipe.provider_name.endswith("okx")
            for recipe in BUILTIN_SYNTHETIC_RECIPES.values()
        ):
            markets.update(BUILTIN_OKX_MARKETS)
            internal_aliases.update(BUILTIN_OKX_INTERNAL_ALIASES)
        replacement = OkxMarketProvider(
            market_bindings=markets,
            internal_aliases=internal_aliases,
            request_timeout=settings.provider_timeout_seconds,
            **_proxy_options(settings, "okx"),
        )
        _replace_graph_provider(graph, "okx", replacement, metrics=metrics)

    if "coingecko" in graph.providers:
        coin_ids = dict(bindings.get("coingecko", {}))
        internal_history_symbols: set[str] = set()
        for policy in BUILTIN_STAKING_RATIO_POLICIES:
            if "staking_market_ratio_proxy" not in plan.providers_for(
                policy.symbol,
                Capability.YIELD,
            ):
                continue
            for dependency in (policy.staking_pair, policy.underlying_pair):
                vendor_symbol = BUILTIN_COINGECKO_COIN_IDS.get(dependency)
                if dependency not in active_symbols and vendor_symbol is not None:
                    coin_ids[dependency] = vendor_symbol
                    internal_history_symbols.add(dependency)
        history_symbols = {
            *internal_history_symbols,
            *(
                item.symbol
                for item in inputs
                if "coingecko" in plan.providers_for(item.symbol, Capability.HISTORY)
            ),
        }
        replacement = CoinGeckoProvider(
            settings.coingecko_api_key,
            coin_ids=coin_ids,
            history_symbols=history_symbols,
            component_skew_limits={
                symbol: timedelta(seconds=seconds)
                for symbol, seconds in BUILTIN_COINGECKO_COMPONENT_SKEW_SECONDS.items()
                if symbol in coin_ids
            },
            normalization_quote_asset=(
                BUILTIN_COINGECKO_NORMALIZATION_COMPONENT_SYMBOL.partition(":")[0]
            ),
            normalization_coin_id=BUILTIN_COINGECKO_NORMALIZATION_COIN_ID,
            normalization_component_symbol=BUILTIN_COINGECKO_NORMALIZATION_COMPONENT_SYMBOL,
            quota=coingecko_quota_budget(settings.coingecko_monthly_credits),
            **_proxy_options(settings, "coingecko"),
        )
        _replace_graph_provider(graph, "coingecko", replacement, metrics=metrics)

    dividend_frequencies: dict[str, str] = {}
    for definition in definitions:
        income = getattr(definition, "income", None)
        if income is None:
            continue
        strategy = getattr(income, "dividend_strategy", None)
        yield_strategy = getattr(income, "yield_strategy", None)
        if strategy is not None:
            dividend_frequencies[definition.symbol] = (
                "monthly" if "monthly" in strategy else "quarterly"
            )
        elif yield_strategy is YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED:
            dividend_frequencies[definition.symbol] = "monthly"

    if "alpaca" in graph.providers:
        symbols = dict(bindings.get("alpaca", {}))
        replacement = AlpacaProvider(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            trading_base_url=settings.alpaca_trading_base_url,
            symbol_bindings=symbols,
            dividend_frequencies=dividend_frequencies,
            regular_dividend_subtypes={
                symbol: BUILTIN_ALPACA_ALLOWED_DIVIDEND_SUBTYPES.get(symbol, ("",))
                for symbol in dividend_frequencies
            },
            stream_symbol_limit=settings.alpaca_stream_symbol_limit,
            rest_calls_per_minute=settings.alpaca_rest_calls_per_minute,
            **_proxy_options(settings, "alpaca"),
        )
        _replace_graph_provider(graph, "alpaca", replacement, metrics=metrics)

    if "finnhub" in graph.providers:
        symbols = dict(bindings.get("finnhub", {}))
        replacement = FinnhubProvider(
            settings.finnhub_api_key,
            symbol_bindings=symbols,
            quota=minute_budget(settings.finnhub_calls_per_minute),
            **_proxy_options(settings, "finnhub"),
        )
        _replace_graph_provider(graph, "finnhub", replacement, metrics=metrics)

    if "twelve_data" in graph.providers:
        symbols = dict(bindings.get("twelve_data", {}))
        fx_symbols = {
            symbol
            for symbol in bindings.get("twelve_data", {})
            if ":" in symbol and "/" in bindings["twelve_data"][symbol]
        }
        input_by_symbol = {item.symbol: item for item in inputs}
        fx_floors = {
            symbol: BUILTIN_TWELVE_FX_CACHE_FLOOR_SECONDS.get(symbol, 900.0)
            for symbol in fx_symbols
        }
        fx_ttls = {
            symbol: (
                float(getattr(settings, BUILTIN_TWELVE_FX_POLL_SETTING[symbol]))
                if symbol in BUILTIN_TWELVE_FX_POLL_SETTING
                else max(fx_floors[symbol], input_by_symbol.get(symbol).quote_poll_seconds)
                if symbol in input_by_symbol
                else fx_floors[symbol]
            )
            for symbol in fx_symbols
        }
        replacement = TwelveDataProvider(
            settings.twelve_data_api_key,
            symbol_bindings=symbols,
            fx_symbols=fx_symbols,
            fx_quote_ttl_floors_seconds=fx_floors,
            fx_quote_ttl_seconds=fx_ttls,
            calls_per_minute=settings.twelve_calls_per_minute,
            rate_gate_timeout_seconds=settings.twelve_rate_gate_timeout_seconds,
            request_timeout=settings.provider_timeout_seconds,
            quota=daily_budget(
                settings.twelve_daily_credits,
                reserve=min(
                    settings.twelve_fx_reserve_credits,
                    settings.twelve_daily_credits - 1,
                ),
            ),
            **_proxy_options(settings, "twelve_data"),
        )
        _replace_graph_provider(graph, "twelve_data", replacement, metrics=metrics)

    if "alpha_vantage" in graph.providers:
        equity_symbols: dict[str, str] = {}
        fx_symbols: dict[str, str | tuple[str, str]] = {}
        for item in inputs:
            vendor_symbol = bindings.get("alpha_vantage", {}).get(item.symbol)
            if vendor_symbol is None:
                continue
            if item.asset_class is AssetClass.FX:
                fx_symbols[item.symbol] = vendor_symbol
            else:
                equity_symbols[item.symbol] = vendor_symbol
        for symbol, vendor_symbol in bindings.get("alpha_vantage", {}).items():
            if symbol not in {item.symbol for item in inputs} and symbol.startswith("USD:"):
                fx_symbols[symbol] = vendor_symbol
        replacement = AlphaVantageProvider(
            settings.alpha_vantage_api_key,
            equity_symbol_bindings=equity_symbols,
            fx_symbol_bindings=fx_symbols,
            dividend_frequencies=dividend_frequencies,
            quota=alpha_vantage_quota_budget(
                settings.alpha_vantage_daily_credits,
                len(fx_symbols),
            ),
            **_proxy_options(settings, "alpha_vantage"),
        )
        _replace_graph_provider(graph, "alpha_vantage", replacement, metrics=metrics)

    if "fred" in graph.providers:
        series_bindings: dict[str, str] = {}
        expense_ratios: dict[str, Decimal | float] = {}
        method_bindings: dict[str, str] = {}
        component_role_bindings: dict[str, str] = {}
        for definition in definitions:
            vendor_symbol = bindings.get("fred", {}).get(definition.symbol)
            income = getattr(definition, "income", None)
            if vendor_symbol is None or income is None:
                continue
            series_bindings[definition.symbol] = vendor_symbol
            expense_ratios[definition.symbol] = income.expense_ratio_percent
            method_bindings[definition.symbol] = (
                "treasury_3m_proxy_minus_expense"
                if income.yield_strategy is YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE
                else "treasury_series_proxy_minus_expense"
            )
            builtin_policy = BUILTIN_FRED_POLICIES.get(definition.symbol)
            component_role_bindings[definition.symbol] = (
                str(builtin_policy["component_role"])
                if builtin_policy is not None
                else "treasury_yield_percent"
            )
        replacement = FredProvider(
            settings.fred_api_key,
            series_bindings=series_bindings,
            expense_ratios=expense_ratios,
            method_bindings=method_bindings,
            component_role_bindings=component_role_bindings,
            **_proxy_options(settings, "fred"),
        )
        _replace_graph_provider(graph, "fred", replacement, metrics=metrics)

    ratio_provider = graph.providers.get("staking_market_ratio_proxy")
    if ratio_provider is not None:
        specs = dict(ratio_provider.specs)
        for definition, item in zip(definitions, inputs, strict=True):
            if not _is_staking(item) or item.underlying_asset is None:
                continue
            if item.symbol in specs:
                continue
            if "staking_market_ratio_proxy" not in plan.providers_for(
                item.symbol, Capability.YIELD
            ):
                continue
            quote_asset = item.symbol.split(":", 1)[1]
            income = getattr(definition, "income", None)
            raw_mode = item.reward_accrual_mode
            if raw_mode is None:
                continue
            specs[item.symbol] = StakingMarketRatioSpec(
                symbol=item.symbol,
                staking_pair=item.symbol,
                underlying_pair=f"{item.underlying_asset}:{quote_asset}",
                underlying_asset=item.underlying_asset,
                accrual_mode=RewardAccrualMode(raw_mode),
                lookback_days=(None if income is None else income.fallback_ratio_days),
            )
        replacement = StakingMarketRatioYieldProvider(
            graph.router,
            specs=tuple(specs.values()),
            lookback_days=settings.staking_yield_market_fallback_days,
        )
        _replace_graph_provider(
            graph,
            "staking_market_ratio_proxy",
            replacement,
            metrics=metrics,
        )

    if managed_fx_requirements and "synthetic_fx" in graph.providers:
        quote_provider = graph.providers["synthetic_fx"]
        requirements = {
            **getattr(quote_provider, "_requirements", {}),
            **managed_fx_requirements,
        }
        maximum_ages = {
            symbol: timedelta(seconds=seconds)
            for symbol, seconds in BUILTIN_FX_HUB_MAX_AGE_SECONDS.items()
        }
        for dependencies in managed_fx_requirements.values():
            for dependency in dependencies:
                maximum_ages.setdefault(dependency, timedelta(minutes=20))
        _replace_graph_provider(
            graph,
            "synthetic_fx",
            UsdHubFxQuoteProvider(
                graph.router.get_quote,
                requirements=requirements,
                max_ages=maximum_ages,
            ),
            metrics=metrics,
        )
        history_provider = graph.providers.get("synthetic_fx_history")
        if history_provider is not None:
            history_requirements = {
                **getattr(history_provider, "_requirements", {}),
                **managed_fx_requirements,
            }
            _replace_graph_provider(
                graph,
                "synthetic_fx_history",
                UsdHubFxHistoryProvider(
                    graph.router.get_history,
                    requirements=history_requirements,
                ),
                metrics=metrics,
            )


def _install_compiled_routes(
    graph: Any,
    plan: CompiledRoutePlan,
    inputs: Sequence[InstrumentRouteInput],
) -> None:
    from .fx import dynamic_fx_requirements
    from .synthetic import SyntheticHistoryProvider, SyntheticQuoteProvider, SyntheticRecipe

    fx_sources = tuple(
        graph.providers[name]
        for name in ("twelve_data", "alpha_vantage")
        if name in graph.providers
    )
    for item in inputs:
        if item.asset_class is not AssetClass.FX:
            continue
        base, _ = item.symbol.split(":", 1)
        if base == "USD":
            continue
        for dependency in dynamic_fx_requirements(item.symbol):
            if fx_sources:
                graph.router.replace(dependency, Capability.QUOTE, fx_sources)
                graph.router.replace(dependency, Capability.HISTORY, fx_sources)

    for item in plan.instruments.values():
        for capability, provider_names in item.routes.items():
            chain: list[Any] = []
            for provider_name in provider_names:
                if provider_name == "synthetic":
                    if item.synthetic is None:
                        raise RouteCompileError(f"synthetic route has no recipe: {item.symbol}")
                    recipe = SyntheticRecipe(
                        symbol=item.symbol,
                        left_symbol=item.synthetic.inputs[0],
                        right_symbol=(
                            item.synthetic.inputs[0]
                            if len(item.synthetic.inputs) == 1
                            else item.synthetic.inputs[1]
                        ),
                        operation=item.synthetic.operation,
                        max_skew=timedelta(seconds=item.synthetic.max_skew_seconds),
                        left_max_age=(
                            None
                            if item.synthetic.input_max_age_seconds[0] is None
                            else timedelta(seconds=item.synthetic.input_max_age_seconds[0])
                        ),
                        right_max_age=(
                            None
                            if len(item.synthetic.input_max_age_seconds) == 1
                            or item.synthetic.input_max_age_seconds[1] is None
                            else timedelta(seconds=item.synthetic.input_max_age_seconds[1])
                        ),
                        provider_name="synthetic_managed",
                    )
                    if capability is Capability.QUOTE:
                        provider = SyntheticQuoteProvider(graph.router.get_quote, (recipe,))
                    elif capability is Capability.HISTORY:
                        provider = SyntheticHistoryProvider(graph.router.get_history, (recipe,))
                    else:
                        raise RouteCompileError(
                            f"synthetic provider cannot serve {capability.value}"
                        )
                    key = (
                        f"synthetic_managed_{capability.value}_"
                        f"{item.symbol.lower().replace(':', '_')}"
                    )
                    graph.providers[key] = provider
                    chain.append(provider)
                    continue
                installed_name = (
                    "synthetic_fx_history"
                    if provider_name == "synthetic_fx" and capability is Capability.HISTORY
                    else provider_name
                )
                provider = graph.providers.get(installed_name)
                if provider is None:
                    raise RouteCompileError(f"compiled provider was not installed: {provider_name}")
                chain.append(provider)
            graph.router.replace(item.symbol, capability, chain)


def build_compiled_provider_graph(
    settings: Any,
    registry: Any,
    definitions: Iterable[Any],
    *,
    metrics: Any | None = None,
    strict: bool = False,
    daily_credit_limits: Mapping[str, int | float | Decimal] | None = None,
) -> tuple[Any, CompiledRoutePlan]:
    """Build a fresh generation graph while retaining proven built-in wiring."""

    from .wiring import build_provider_graph

    active = _active_definitions(definitions)
    inputs = tuple(instrument_route_input_from_definition(item) for item in active)
    graph = build_provider_graph(settings, metrics=metrics)
    available = set(graph.providers)
    available.add("synthetic")
    if "synthetic_fx" not in graph.providers:
        available.discard("synthetic_fx")
    try:
        plan = compile_route_plan(
            inputs,
            settings=settings,
            available_providers=available,
            daily_credit_limits=daily_credit_limits,
            drop_unconfigured=True,
            strict=strict,
        )
        _install_instance_bindings(
            graph,
            settings,
            active,
            inputs,
            plan,
            metrics=metrics,
        )
        _install_compiled_routes(graph, plan, inputs)
    except BaseException:
        # No provider session is normally opened during construction. The
        # caller still owns closing the graph if it performs asynchronous
        # validation after this function returns.
        raise
    return graph, plan


__all__ = [
    "MAX_PROVIDER_CHAIN_LENGTH",
    "MAX_SYNTHETIC_DEPTH",
    "BuiltinProviderPolicy",
    "CompiledInstrumentRoutes",
    "CompiledRoute",
    "CompiledRoutePlan",
    "InstrumentRouteInput",
    "RouteCompileError",
    "SyntheticRouteInput",
    "build_compiled_provider_graph",
    "builtin_provider_policy",
    "builtin_provider_policy_snapshot",
    "compile_catalog_route_plan",
    "compile_route_plan",
    "configured_provider_names",
    "incremental_credit_budget_errors",
    "instrument_route_input_from_definition",
    "recommended_provider_chain",
    "required_capabilities",
]
