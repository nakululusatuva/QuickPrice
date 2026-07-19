"""Stable declarations exposed to trusted QuickPrice instrument plugins."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from .domain import RewardAccrualMode


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    EQUITY = "equity"
    BOND = "bond"
    FX = "fx"


class YieldStrategy(StrEnum):
    LATEST_DISTRIBUTION_ANNUALIZED = "latest_distribution_annualized"
    TREASURY_3M_PROXY_MINUS_EXPENSE = "treasury_3m_proxy_minus_expense"
    TREASURY_PROXY_MINUS_EXPENSE = "treasury_proxy_minus_expense"
    STAKING_PROVIDER_METRIC = "staking_provider_metric"


class MarketCalendar(StrEnum):
    ALWAYS_OPEN = "always_open"
    US_EQUITY = "us_equity"
    FX_24X5 = "fx_24x5"


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentSpec:
    """Provider-neutral metadata and collection policy for one public symbol."""

    symbol: str
    base: str
    quote: str
    name: str
    description: str
    asset_class: AssetClass
    asset_type: str
    price_basis: str
    change_basis: str = "unadjusted_market_price"
    yield_strategy: YieldStrategy | None = None
    dividend_strategy: str | None = None
    reward_accrual_mode: RewardAccrualMode | None = None
    underlying_asset: str | None = None
    aliases: tuple[str, ...] = ()
    market_calendar: MarketCalendar = MarketCalendar.ALWAYS_OPEN
    stale_after_seconds: float = 10.0
    quote_poll_seconds: float = 5.0
    history_enabled: bool = True
    history_poll_seconds: float | None = None


# Backward-compatible public spelling retained for adapter and application imports.
Instrument = InstrumentSpec


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderBinding:
    """A declarative capability route supplied by a trusted plugin."""

    symbol: str
    capability: Literal["quote", "history", "dividend", "yield"]
    providers: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticRecipe:
    """A provider-neutral two-leg synthetic price declaration."""

    symbol: str
    left_symbol: str
    right_symbol: str
    operation: Literal["multiply", "divide"]
    max_skew_seconds: float
    left_max_age_seconds: float | None = None
    right_max_age_seconds: float | None = None
    provider_name: str = "synthetic"
    quote_enabled: bool = True
    history_enabled: bool = True


type ProviderInstaller = Callable[[Any], None]


@dataclass(slots=True)
class ProviderInstallContext:
    """Runtime objects available to a trusted plugin's provider installer."""

    settings: Any
    registry: Any
    router: Any
    providers: dict[str, Any] = field(default_factory=dict)

    def add_provider(self, name: str, provider: Any) -> Any:
        normalized = name.strip()
        if not normalized:
            raise ValueError("provider name cannot be empty")
        existing = self.providers.get(normalized)
        if existing is not None and existing is not provider:
            raise ValueError(f"duplicate provider: {normalized}")
        self.providers[normalized] = provider
        return provider

    def register(
        self, symbol: str, capability: str, providers: tuple[Any, ...] | list[Any]
    ) -> None:
        self.router.register(symbol, capability, providers)


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentPlugin:
    """Trusted entry-point payload for instruments and provider installation."""

    plugin_id: str
    version: str
    instruments: tuple[InstrumentSpec, ...]
    provider_installer: str | ProviderInstaller | None = None
    provider_bindings: tuple[ProviderBinding, ...] = ()
    synthetic_recipes: tuple[SyntheticRecipe, ...] = ()


ENTRY_POINT_GROUP = "quickprice.instrument_plugins"


__all__ = [
    "ENTRY_POINT_GROUP",
    "AssetClass",
    "Instrument",
    "InstrumentPlugin",
    "InstrumentSpec",
    "MarketCalendar",
    "ProviderBinding",
    "ProviderInstallContext",
    "ProviderInstaller",
    "RewardAccrualMode",
    "SyntheticRecipe",
    "YieldStrategy",
]
