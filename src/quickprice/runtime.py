"""Runtime invariants checked only after the dependency graph is imported."""

from __future__ import annotations

import sys
import sysconfig
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from threading import RLock
from typing import Any

from .plugin_api import InstrumentSpec
from .registry import InstrumentRegistry


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


def _registry_revision(registry: InstrumentRegistry) -> str:
    """Return a stable identity for an immutable instrument registry."""

    rows = (
        "\x1f".join(
            (
                item.symbol,
                item.name,
                item.description,
                item.asset_class.value,
                item.asset_type,
                item.price_basis,
                item.yield_strategy.value if item.yield_strategy is not None else "",
                item.dividend_strategy or "",
                item.reward_accrual_mode.value if item.reward_accrual_mode is not None else "",
                item.underlying_asset or "",
                item.market_calendar.value,
                format(item.stale_after_seconds, ".12g"),
                format(item.quote_poll_seconds, ".12g"),
                "1" if item.history_enabled else "0",
                ",".join(item.aliases),
            )
        )
        for item in registry.values()
    )
    return sha256("\n".join(rows).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeGeneration:
    """One immutable data-plane view captured by an HTTP request or collector."""

    generation_id: str
    revision: str
    registry: InstrumentRegistry
    activated_at: datetime
    catalog: Any = None
    route_plan: Any = None
    data_state: Any = None


class RuntimeGenerationManager:
    """Publish complete runtime generations with one atomic pointer swap."""

    def __init__(
        self,
        registry: InstrumentRegistry,
        *,
        revision: str | None = None,
        catalog: Any = None,
        route_plan: Any = None,
        data_state: Any = None,
    ) -> None:
        self._lock = RLock()
        self._current = RuntimeGeneration(
            generation_id=str(uuid.uuid7()),
            revision=revision or _registry_revision(registry),
            registry=registry,
            activated_at=datetime.now(UTC),
            catalog=catalog,
            route_plan=route_plan,
            data_state=data_state,
        )

    def capture(self) -> RuntimeGeneration:
        with self._lock:
            return self._current

    def is_current(self, generation_id: str | None) -> bool:
        if generation_id is None:
            return True
        with self._lock:
            return self._current.generation_id == generation_id

    def activate(
        self,
        registry: InstrumentRegistry,
        *,
        revision: str,
        catalog: Any = None,
        route_plan: Any = None,
        data_state: Any = None,
        generation_id: str | None = None,
    ) -> tuple[RuntimeGeneration, RuntimeGeneration]:
        replacement = RuntimeGeneration(
            generation_id=generation_id or str(uuid.uuid7()),
            revision=revision,
            registry=registry,
            activated_at=datetime.now(UTC),
            catalog=catalog,
            route_plan=route_plan,
            data_state=data_state,
        )
        with self._lock:
            previous = self._current
            self._current = replacement
        return previous, replacement


class RuntimeRegistryView(Mapping[str, InstrumentSpec]):
    """Compatibility mapping that always delegates to the active generation."""

    def __init__(self, generations: RuntimeGenerationManager) -> None:
        self._generations = generations

    def _registry(self) -> InstrumentRegistry:
        return self._generations.capture().registry

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._registry().symbols

    @property
    def plugins(self) -> tuple[Any, ...]:
        return self._registry().plugins

    def resolve(self, value: str) -> InstrumentSpec | None:
        return self._registry().resolve(value)

    def canonical_symbol(self, value: str) -> str | None:
        return self._registry().canonical_symbol(value)

    def __getitem__(self, symbol: str) -> InstrumentSpec:
        return self._registry()[symbol]

    def __iter__(self) -> Iterator[str]:
        return iter(self._registry())

    def __len__(self) -> int:
        return len(self._registry())
