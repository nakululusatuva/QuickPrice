from __future__ import annotations

import sys
import sysconfig

# Import the complete production dependency graph before inspecting the GIL.
import quickprice.api  # noqa: F401
from quickprice.runtime import inspect_free_threaded_runtime


def test_runtime_probe_matches_cpython_state_after_all_imports():
    status = inspect_free_threaded_runtime()
    expected_build = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    assert status.py_gil_disabled is expected_build
    assert status.gil_enabled is bool(sys._is_gil_enabled())
    assert status.ready is (expected_build and not sys._is_gil_enabled())
