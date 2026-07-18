from __future__ import annotations

from dataclasses import replace

import pytest

from quickprice.builtin_plugin import BUILTIN_PLUGIN
from quickprice.plugin_api import (
    AssetClass,
    InstrumentPlugin,
    InstrumentSpec,
    RewardAccrualMode,
    SyntheticRecipe,
    YieldStrategy,
)
from quickprice.registry import INSTRUMENTS, SYMBOLS, InstrumentRegistry, build_registry


def test_builtin_plugin_preserves_the_initial_catalog_and_asset_classes() -> None:
    assert SYMBOLS == (
        "BTC:USDC",
        "ETH:USDC",
        "WBETH:USDC",
        "QQQM:USD",
        "BOXX:USD",
        "SGOV:USD",
        "USD:CNH",
        "HKD:CNH",
    )
    assert INSTRUMENTS["BTC:USDC"].asset_class is AssetClass.CRYPTO
    assert INSTRUMENTS["QQQM:USD"].asset_class is AssetClass.EQUITY
    assert INSTRUMENTS["BOXX:USD"].asset_class is AssetClass.BOND
    assert INSTRUMENTS["SGOV:USD"].asset_type == "income_bond_etf"
    assert INSTRUMENTS["WBETH:USDC"].asset_type == "liquid_staking_token"
    assert all(item.name and item.description for item in INSTRUMENTS.values())


def test_every_bond_has_a_yield_strategy() -> None:
    bonds = [item for item in INSTRUMENTS.values() if item.asset_class is AssetClass.BOND]
    assert bonds
    assert all(item.yield_strategy is not None for item in bonds)


def test_every_staking_token_declares_income_semantics() -> None:
    staking_tokens = [item for item in INSTRUMENTS.values() if "staking" in item.asset_type]
    assert staking_tokens
    assert all(item.yield_strategy is not None for item in staking_tokens)
    assert all(item.reward_accrual_mode is not None for item in staking_tokens)
    assert all(item.underlying_asset for item in staking_tokens)


def test_builtin_wbeth_declares_required_staking_income_semantics() -> None:
    wbeth = INSTRUMENTS["WBETH:USDC"]

    assert wbeth.yield_strategy is YieldStrategy.STAKING_PROVIDER_METRIC
    assert wbeth.reward_accrual_mode is RewardAccrualMode.VALUE_ACCRUING
    assert wbeth.underlying_asset == "ETH"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"yield_strategy": None}, "requires a yield strategy"),
        ({"reward_accrual_mode": None}, "requires a reward accrual mode"),
        ({"underlying_asset": None}, "requires an underlying asset"),
    ],
)
def test_registry_rejects_wbeth_without_income_semantics(changes, message: str) -> None:
    wbeth = replace(INSTRUMENTS["WBETH:USDC"], **changes)
    plugin = replace(BUILTIN_PLUGIN, instruments=(wbeth,))

    with pytest.raises(ValueError, match=message):
        InstrumentRegistry((plugin,))


class FakeEntryPoint:
    def __init__(self, name: str, value, calls: list[str]) -> None:
        self.name = name
        self._value = value
        self._calls = calls

    def load(self):
        self._calls.append(self.name)
        return self._value


def _external_plugin(*, alias: str = "XBT:USD") -> InstrumentPlugin:
    return InstrumentPlugin(
        plugin_id="example",
        version="1.0.0",
        provider_installer=lambda _: None,
        instruments=(
            InstrumentSpec(
                symbol="TEST:USD",
                base="TEST",
                quote="USD",
                name="Test Asset",
                description="An instrument supplied by a test plugin.",
                asset_class=AssetClass.CRYPTO,
                asset_type="spot_crypto",
                price_basis="last_trade",
                aliases=(alias,),
            ),
        ),
    )


def test_discovery_loads_only_explicitly_enabled_entry_points() -> None:
    calls: list[str] = []
    available = (
        FakeEntryPoint("enabled", _external_plugin(), calls),
        FakeEntryPoint("disabled", _external_plugin(alias="OTHER:USD"), calls),
    )
    registry = build_registry(("builtin", "enabled"), available_entry_points=available)

    assert calls == ["enabled"]
    assert registry["TEST:USD"].name == "Test Asset"
    assert registry["XBT/USD"].symbol == "TEST:USD"
    assert len(registry.plugins) == 2


def test_missing_enabled_entry_point_fails_without_loading_other_plugins() -> None:
    calls: list[str] = []
    available = (FakeEntryPoint("disabled", _external_plugin(), calls),)
    with pytest.raises(RuntimeError, match="were not found"):
        build_registry(("missing",), available_entry_points=available)
    assert calls == []


def test_duplicate_enabled_entry_point_names_are_rejected_as_ambiguous() -> None:
    calls: list[str] = []
    available = (
        FakeEntryPoint("duplicate", _external_plugin(), calls),
        FakeEntryPoint("duplicate", _external_plugin(), calls),
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        build_registry(("duplicate",), available_entry_points=available)
    assert calls == []


@pytest.mark.parametrize(
    ("plugin", "message"),
    [
        (
            replace(
                _external_plugin(),
                instruments=(replace(_external_plugin().instruments[0], name=""),),
            ),
            "requires a name",
        ),
        (
            replace(
                _external_plugin(),
                instruments=(replace(_external_plugin().instruments[0], description=""),),
            ),
            "requires a description",
        ),
        (
            replace(
                _external_plugin(),
                instruments=(replace(_external_plugin().instruments[0], symbol="test:usd"),),
            ),
            "invalid canonical",
        ),
    ],
)
def test_registry_rejects_invalid_plugin_metadata(plugin, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        InstrumentRegistry((BUILTIN_PLUGIN, plugin))


def test_registry_rejects_duplicate_symbols_and_aliases() -> None:
    duplicate_symbol = replace(
        _external_plugin(),
        instruments=(
            replace(
                _external_plugin().instruments[0],
                symbol="BTC:USDC",
                base="BTC",
                quote="USDC",
            ),
        ),
    )
    with pytest.raises(ValueError, match="duplicate instrument identity"):
        InstrumentRegistry((BUILTIN_PLUGIN, duplicate_symbol))

    duplicate_alias = replace(
        _external_plugin(),
        instruments=(replace(_external_plugin().instruments[0], aliases=("ETH:USDC",)),),
    )
    with pytest.raises(ValueError, match="duplicate instrument alias"):
        InstrumentRegistry((BUILTIN_PLUGIN, duplicate_alias))


def test_registry_rejects_synthetic_dependency_cycles() -> None:
    plugin = replace(
        _external_plugin(),
        synthetic_recipes=(
            SyntheticRecipe(
                symbol="TEST:USD",
                left_symbol="LEG:USD",
                right_symbol="USD:USD",
                operation="multiply",
                max_skew_seconds=2,
            ),
            SyntheticRecipe(
                symbol="LEG:USD",
                left_symbol="TEST:USD",
                right_symbol="USD:USD",
                operation="multiply",
                max_skew_seconds=2,
            ),
        ),
    )
    with pytest.raises(ValueError, match="dependency cycle"):
        InstrumentRegistry((plugin,))


def test_registry_rejects_synthetic_cycles_across_plugins() -> None:
    first = InstrumentPlugin(
        plugin_id="first",
        version="1",
        instruments=(),
        synthetic_recipes=(
            SyntheticRecipe(
                symbol="FIRST:USD",
                left_symbol="SECOND:USD",
                right_symbol="USD:USD",
                operation="multiply",
                max_skew_seconds=2,
            ),
        ),
    )
    second = InstrumentPlugin(
        plugin_id="second",
        version="1",
        instruments=(),
        synthetic_recipes=(
            SyntheticRecipe(
                symbol="SECOND:USD",
                left_symbol="FIRST:USD",
                right_symbol="USD:USD",
                operation="multiply",
                max_skew_seconds=2,
            ),
        ),
    )

    with pytest.raises(ValueError, match="dependency cycle"):
        InstrumentRegistry((first, second))
