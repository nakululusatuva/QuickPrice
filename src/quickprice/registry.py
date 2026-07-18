"""Immutable instrument registry with an explicit trusted-plugin allowlist."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from importlib.metadata import EntryPoint, entry_points
from types import MappingProxyType
from typing import Any

from .builtin_plugin import BUILTIN_PLUGIN
from .plugin_api import (
    ENTRY_POINT_GROUP,
    AssetClass,
    Instrument,
    InstrumentPlugin,
    InstrumentSpec,
    MarketCalendar,
    YieldStrategy,
)

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*:[A-Z0-9][A-Z0-9._-]*$")


def normalize_symbol(value: str) -> str:
    return value.strip().upper().replace("/", ":")


def _coerce_plugin(value: Any, entry_point_name: str) -> InstrumentPlugin:
    candidate = value
    if not isinstance(candidate, InstrumentPlugin) and callable(candidate):
        candidate = candidate()
    if not isinstance(candidate, InstrumentPlugin):
        raise TypeError(
            f"plugin entry point {entry_point_name!r} must expose an InstrumentPlugin "
            "or a zero-argument factory"
        )
    return candidate


class InstrumentRegistry(Mapping[str, InstrumentSpec]):
    """Validated, immutable catalog assembled from trusted plugins."""

    def __init__(self, plugins: Iterable[InstrumentPlugin]) -> None:
        plugin_items = tuple(plugins)
        if not plugin_items:
            raise ValueError("at least one instrument plugin is required")
        instruments: dict[str, InstrumentSpec] = {}
        aliases: dict[str, str] = {}
        plugin_ids: set[str] = set()
        for plugin in plugin_items:
            plugin_id = plugin.plugin_id.strip()
            if not plugin_id:
                raise ValueError("plugin_id cannot be empty")
            if plugin_id in plugin_ids:
                raise ValueError(f"duplicate plugin_id: {plugin_id}")
            plugin_ids.add(plugin_id)
            if not plugin.version.strip():
                raise ValueError(f"plugin {plugin_id} must declare a version")
            if (
                plugin.instruments
                and plugin.provider_installer is None
                and not plugin.provider_bindings
                and not plugin.synthetic_recipes
            ):
                raise ValueError(f"plugin {plugin_id} does not declare provider installation")
            self._validate_plugin_routes(plugin)
            for raw_item in plugin.instruments:
                item = self._validate_instrument(raw_item)
                if item.symbol in instruments or item.symbol in aliases:
                    raise ValueError(f"duplicate instrument identity: {item.symbol}")
                instruments[item.symbol] = item
                for raw_alias in item.aliases:
                    alias = normalize_symbol(raw_alias)
                    if not _SYMBOL_PATTERN.fullmatch(alias):
                        raise ValueError(f"invalid alias for {item.symbol}: {raw_alias!r}")
                    if alias in instruments or alias in aliases:
                        raise ValueError(f"duplicate instrument alias: {alias}")
                    aliases[alias] = item.symbol
        self._validate_cross_plugin_synthetic_routes(plugin_items)
        self._plugins = plugin_items
        self._instruments: Mapping[str, InstrumentSpec] = MappingProxyType(instruments)
        self._aliases: Mapping[str, str] = MappingProxyType(aliases)

    @staticmethod
    def _validate_plugin_routes(plugin: InstrumentPlugin) -> None:
        for binding in plugin.provider_bindings:
            if not binding.symbol.strip() or not binding.providers:
                raise ValueError(f"plugin {plugin.plugin_id} has an empty provider binding")
            if any(not provider.strip() for provider in binding.providers):
                raise ValueError(f"plugin {plugin.plugin_id} has an invalid provider name")
        recipes = plugin.synthetic_recipes
        outputs: dict[str, list[Any]] = {}
        for recipe in recipes:
            outputs.setdefault(normalize_symbol(recipe.symbol), []).append(recipe)
            if recipe.max_skew_seconds < 0:
                raise ValueError(f"synthetic recipe {recipe.symbol} has negative maximum skew")
            if not recipe.quote_enabled and not recipe.history_enabled:
                raise ValueError(f"synthetic recipe {recipe.symbol} enables no capabilities")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(symbol: str) -> None:
            if symbol in visited or symbol not in outputs:
                return
            if symbol in visiting:
                raise ValueError(f"plugin {plugin.plugin_id} has a synthetic dependency cycle")
            visiting.add(symbol)
            for recipe in outputs[symbol]:
                visit(normalize_symbol(recipe.left_symbol))
                visit(normalize_symbol(recipe.right_symbol))
            visiting.remove(symbol)
            visited.add(symbol)

        for symbol in outputs:
            visit(symbol)

    @staticmethod
    def _validate_cross_plugin_synthetic_routes(
        plugins: tuple[InstrumentPlugin, ...],
    ) -> None:
        outputs: dict[str, list[Any]] = {}
        for plugin in plugins:
            for recipe in plugin.synthetic_recipes:
                outputs.setdefault(normalize_symbol(recipe.symbol), []).append(recipe)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(symbol: str) -> None:
            if symbol in visited or symbol not in outputs:
                return
            if symbol in visiting:
                raise ValueError("instrument plugins have a synthetic dependency cycle")
            visiting.add(symbol)
            for recipe in outputs[symbol]:
                visit(normalize_symbol(recipe.left_symbol))
                visit(normalize_symbol(recipe.right_symbol))
            visiting.remove(symbol)
            visited.add(symbol)

        for symbol in outputs:
            visit(symbol)

    @staticmethod
    def _validate_instrument(item: InstrumentSpec) -> InstrumentSpec:
        symbol = normalize_symbol(item.symbol)
        if symbol != item.symbol or not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"invalid canonical instrument symbol: {item.symbol!r}")
        if item.base != item.base.strip().upper() or item.quote != item.quote.strip().upper():
            raise ValueError(f"base and quote must be uppercase for {symbol}")
        if symbol != f"{item.base}:{item.quote}":
            raise ValueError(f"symbol does not match base and quote for {symbol}")
        if not item.name.strip():
            raise ValueError(f"instrument {symbol} requires a name")
        if not item.description.strip():
            raise ValueError(f"instrument {symbol} requires a description")
        if not item.asset_type.strip() or not item.price_basis.strip():
            raise ValueError(f"instrument {symbol} has incomplete classification")
        if item.stale_after_seconds <= 0 or item.quote_poll_seconds <= 0:
            raise ValueError(f"instrument {symbol} has an invalid collection interval")
        if item.asset_class is AssetClass.BOND and item.yield_strategy is None:
            raise ValueError(f"bond instrument {symbol} requires a yield strategy")
        is_staking_asset = "staking" in item.asset_type.lower()
        if is_staking_asset and item.reward_accrual_mode is None:
            raise ValueError(f"staking instrument {symbol} requires a reward accrual mode")
        if is_staking_asset and item.yield_strategy is None:
            raise ValueError(f"staking instrument {symbol} requires a yield strategy")
        if is_staking_asset and not (item.underlying_asset or "").strip():
            raise ValueError(f"staking instrument {symbol} requires an underlying asset")
        if item.reward_accrual_mode is not None and item.yield_strategy is None:
            raise ValueError(f"income-bearing instrument {symbol} requires a yield strategy")
        if item.underlying_asset is not None and (
            not item.underlying_asset.strip()
            or item.underlying_asset != item.underlying_asset.strip().upper()
        ):
            raise ValueError(f"instrument {symbol} has an invalid underlying asset")
        return item

    @property
    def plugins(self) -> tuple[InstrumentPlugin, ...]:
        return self._plugins

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._instruments)

    def resolve(self, value: str) -> InstrumentSpec | None:
        symbol = normalize_symbol(value)
        canonical = self._aliases.get(symbol, symbol)
        return self._instruments.get(canonical)

    def canonical_symbol(self, value: str) -> str | None:
        item = self.resolve(value)
        return item.symbol if item is not None else None

    def __getitem__(self, symbol: str) -> InstrumentSpec:
        item = self.resolve(symbol)
        if item is None:
            raise KeyError(normalize_symbol(symbol))
        return item

    def __iter__(self) -> Iterator[str]:
        return iter(self._instruments)

    def __len__(self) -> int:
        return len(self._instruments)

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and self.resolve(value) is not None


def discover_plugins(
    enabled_plugins: Iterable[str] = (),
    *,
    available_entry_points: Iterable[EntryPoint] | None = None,
) -> tuple[InstrumentPlugin, ...]:
    """Load only explicitly enabled third-party entry points plus the built-in plugin."""

    enabled = tuple(
        name
        for name in dict.fromkeys(item.strip() for item in enabled_plugins if item.strip())
        if name != BUILTIN_PLUGIN.plugin_id
    )
    if not enabled:
        return (BUILTIN_PLUGIN,)
    available = tuple(
        available_entry_points
        if available_entry_points is not None
        else entry_points(group=ENTRY_POINT_GROUP)
    )
    by_name: dict[str, list[EntryPoint]] = {}
    for item in available:
        by_name.setdefault(item.name, []).append(item)
    missing = [name for name in enabled if name not in by_name]
    if missing:
        raise RuntimeError(f"enabled instrument plugins were not found: {', '.join(missing)}")
    ambiguous = [name for name in enabled if len(by_name[name]) != 1]
    if ambiguous:
        raise RuntimeError(
            "enabled instrument plugin entry points are ambiguous: " + ", ".join(ambiguous)
        )
    loaded = tuple(_coerce_plugin(by_name[name][0].load(), name) for name in enabled)
    return (BUILTIN_PLUGIN, *loaded)


def build_registry(
    enabled_plugins: Iterable[str] = (),
    *,
    available_entry_points: Iterable[EntryPoint] | None = None,
) -> InstrumentRegistry:
    return InstrumentRegistry(
        discover_plugins(enabled_plugins, available_entry_points=available_entry_points)
    )


DEFAULT_REGISTRY = build_registry()
INSTRUMENTS: Mapping[str, InstrumentSpec] = DEFAULT_REGISTRY
SYMBOLS: tuple[str, ...] = DEFAULT_REGISTRY.symbols


def get_instrument(
    symbol: str, registry: InstrumentRegistry = DEFAULT_REGISTRY
) -> InstrumentSpec | None:
    return registry.resolve(symbol)


__all__ = [
    "DEFAULT_REGISTRY",
    "INSTRUMENTS",
    "SYMBOLS",
    "AssetClass",
    "Instrument",
    "InstrumentRegistry",
    "InstrumentSpec",
    "MarketCalendar",
    "YieldStrategy",
    "build_registry",
    "discover_plugins",
    "get_instrument",
    "normalize_symbol",
]
