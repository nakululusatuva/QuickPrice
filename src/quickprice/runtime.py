"""Runtime invariants checked only after the dependency graph is imported."""

from __future__ import annotations

import sys
import sysconfig
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FreeThreadedStatus:
    py_gil_disabled: bool
    gil_enabled: bool | None

    @property
    def ready(self) -> bool:
        return self.py_gil_disabled and self.gil_enabled is False

    def as_dict(self) -> dict[str, bool | None]:
        return {
            "py_gil_disabled": self.py_gil_disabled,
            "gil_enabled": self.gil_enabled,
            "ready": self.ready,
        }


def inspect_free_threaded_runtime() -> FreeThreadedStatus:
    compiled_without_gil = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    checker = getattr(sys, "_is_gil_enabled", None)
    gil_enabled = bool(checker()) if checker is not None else None
    return FreeThreadedStatus(compiled_without_gil, gil_enabled)
