from __future__ import annotations

import sys
import sysconfig

# Import the complete production dependency graph before inspecting the GIL.
import quickprice.api  # noqa: F401
from quickprice.plugin_api import AssetClass, InstrumentPlugin, InstrumentSpec
from quickprice.registry import InstrumentRegistry
from quickprice.runtime import (
    RuntimeGenerationManager,
    RuntimeRegistryView,
    inspect_free_threaded_runtime,
)


def test_runtime_probe_matches_cpython_state_after_all_imports():
    status = inspect_free_threaded_runtime()
    expected_build = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    assert status.py_gil_disabled is expected_build
    assert status.gil_enabled is bool(sys._is_gil_enabled())
    assert status.ready is (expected_build and not sys._is_gil_enabled())


def _registry(symbol: str) -> InstrumentRegistry:
    base, quote = symbol.split(":", 1)
    instrument = InstrumentSpec(
        symbol=symbol,
        base=base,
        quote=quote,
        name=base,
        description="A runtime generation fixture.",
        asset_class=AssetClass.CRYPTO,
        asset_type="spot_crypto",
        price_basis="last_trade",
    )
    return InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id=f"fixture-{base.lower()}",
                version="1",
                instruments=(instrument,),
                provider_installer=lambda _: None,
            ),
        )
    )


def test_runtime_generation_switch_is_atomic_and_view_tracks_active_registry():
    first = _registry("AAA:USD")
    second = _registry("BBB:USD")
    manager = RuntimeGenerationManager(first)
    view = RuntimeRegistryView(manager)

    captured = manager.capture()
    previous, active = manager.activate(second, revision="b" * 64)

    assert previous is captured
    assert captured.registry.symbols == ("AAA:USD",)
    assert active.registry.symbols == ("BBB:USD",)
    assert view.symbols == ("BBB:USD",)
    assert not manager.is_current(captured.generation_id)
    assert manager.is_current(active.generation_id)
