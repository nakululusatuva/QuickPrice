"""Small dependency-free operational metric registry."""

from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True)
class _ProviderEvent:
    recorded_at: float
    capability: str
    outcome: str
    latency_ms: float


@dataclass(slots=True)
class _ProviderLifetime:
    attempts: int = 0
    successful: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    capabilities: Counter[str] = field(default_factory=Counter)
    latency_total_ms: float = 0.0
    latency_max_ms: float | None = None
    first_attempt_at: float | None = None
    last_attempt_at: float | None = None
    last_outcome: str | None = None


class Metrics:
    """Thread-safe, bounded application and upstream telemetry.

    Provider names, capabilities, and outcomes are deliberately constrained to
    internal labels. Events never retain URLs, request parameters, response
    bodies, exception messages, or credentials.
    """

    _SAFE_LABEL: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
    _SURFACES: ClassVar[tuple[str, ...]] = ("operations", "upstream_http")
    _SUCCESS_OUTCOMES: ClassVar[frozenset[str]] = frozenset({"success", "partial"})
    _OUTCOMES: ClassVar[frozenset[str]] = frozenset(
        {
            "success",
            "partial",
            "no_data",
            "timeout",
            "unavailable",
            "rate_limited",
            "busy",
            "unsupported",
            "malformed",
            "rejected",
            "unexpected",
        }
    )
    _CAPABILITIES: ClassVar[frozenset[str]] = frozenset(
        {"quote", "history", "dividend", "yield", "http", "stream", "other"}
    )

    def __init__(self, latency_window: int = 4096, provider_window: int = 2048) -> None:
        if latency_window <= 0 or provider_window <= 0:
            raise ValueError("metric windows must be positive")
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str, int]] = Counter()
        self._latencies_ms: deque[float] = deque(maxlen=latency_window)
        self._fallbacks: Counter[str] = Counter()
        self._ws_reconnects: Counter[str] = Counter()
        self._event_loop_lag_ms = 0.0
        self._started = monotonic()
        self._provider_window = provider_window
        self._provider_names: set[str] = set()
        self._provider_events: dict[tuple[str, str], deque[_ProviderEvent]] = {}
        self._provider_lifetime: dict[tuple[str, str], _ProviderLifetime] = {}

    def observe_request(self, route: str, status: int, latency_ms: float) -> None:
        with self._lock:
            self._requests[(route, status)] += 1
            self._latencies_ms.append(latency_ms)

    def fallback(self, provider: str) -> None:
        with self._lock:
            self._fallbacks[provider] += 1

    def websocket_reconnect(self, provider: str) -> None:
        with self._lock:
            self._ws_reconnects[provider] += 1

    def set_event_loop_lag(self, value_ms: float) -> None:
        with self._lock:
            self._event_loop_lag_ms = max(0.0, value_ms)

    @classmethod
    def _label(cls, value: str) -> str:
        normalized = str(value).strip()
        return normalized if cls._SAFE_LABEL.fullmatch(normalized) else "unknown"

    def register_provider(self, provider: str) -> None:
        """Expose configured providers even before their first attempt."""

        with self._lock:
            self._provider_names.add(self._label(provider))

    def observe_provider_operation(
        self,
        provider: str,
        capability: str,
        outcome: str,
        latency_ms: float,
    ) -> None:
        self._observe_provider("operations", provider, capability, outcome, latency_ms)

    def observe_provider_http(
        self,
        provider: str,
        outcome: str,
        latency_ms: float,
    ) -> None:
        self._observe_provider("upstream_http", provider, "http", outcome, latency_ms)

    def _observe_provider(
        self,
        surface: str,
        provider: str,
        capability: str,
        outcome: str,
        latency_ms: float,
    ) -> None:
        provider_label = self._label(provider)
        capability_label = capability if capability in self._CAPABILITIES else "other"
        outcome_label = outcome if outcome in self._OUTCOMES else "unexpected"
        finite_latency = float(latency_ms)
        if not math.isfinite(finite_latency):
            finite_latency = 0.0
        finite_latency = max(0.0, finite_latency)
        recorded_at = time.time()
        event = _ProviderEvent(
            recorded_at=recorded_at,
            capability=capability_label,
            outcome=outcome_label,
            latency_ms=finite_latency,
        )
        key = (provider_label, surface)
        with self._lock:
            self._provider_names.add(provider_label)
            events = self._provider_events.get(key)
            if events is None:
                events = self._provider_events[key] = deque(maxlen=self._provider_window)
            events.append(event)
            lifetime = self._provider_lifetime.get(key)
            if lifetime is None:
                lifetime = self._provider_lifetime[key] = _ProviderLifetime()
            lifetime.attempts += 1
            if outcome_label in self._SUCCESS_OUTCOMES:
                lifetime.successful += 1
            lifetime.outcomes[outcome_label] += 1
            lifetime.capabilities[capability_label] += 1
            lifetime.latency_total_ms += finite_latency
            lifetime.latency_max_ms = max(lifetime.latency_max_ms or 0.0, finite_latency)
            if lifetime.first_attempt_at is None:
                lifetime.first_attempt_at = recorded_at
            lifetime.last_attempt_at = recorded_at
            lifetime.last_outcome = outcome_label

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1)
        return ordered[index]

    @staticmethod
    def _timestamp(value: float | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def _latency_summary(
        cls,
        values: list[float],
        *,
        total: float | None = None,
        count: int | None = None,
        maximum: float | None = None,
    ) -> dict[str, Any]:
        sample_count = len(values) if count is None else count
        if sample_count == 0:
            return {
                "avg": None,
                "p50": None,
                "p95": None,
                "p99": None,
                "max": None,
                "percentile_scope": "bounded_recent",
                "percentile_sample_size": 0,
            }
        # Percentiles intentionally describe the bounded recent sample, even
        # in the lifetime view. Average and max remain exact lifetime values.
        return {
            "avg": (sum(values) if total is None else total) / sample_count,
            "p50": cls._percentile(values, 0.50) if values else None,
            "p95": cls._percentile(values, 0.95) if values else None,
            "p99": cls._percentile(values, 0.99) if values else None,
            "max": max(values) if maximum is None and values else maximum,
            "percentile_scope": "bounded_recent",
            "percentile_sample_size": len(values),
        }

    @classmethod
    def _event_summary(
        cls,
        events: list[_ProviderEvent],
        *,
        capacity: int,
    ) -> dict[str, Any]:
        outcomes = Counter(event.outcome for event in events)
        capabilities = Counter(event.capability for event in events)
        successful = sum(outcomes[outcome] for outcome in cls._SUCCESS_OUTCOMES)
        attempts = len(events)
        latencies = [event.latency_ms for event in events]
        return {
            "attempts": attempts,
            "successful": successful,
            "success_rate": None if attempts == 0 else successful * 100.0 / attempts,
            "success_rate_unit": "percent",
            "outcomes": dict(sorted(outcomes.items())),
            "capabilities": dict(sorted(capabilities.items())),
            "latency_ms": cls._latency_summary(latencies),
            "first_attempt_at": cls._timestamp(events[0].recorded_at) if events else None,
            "last_attempt_at": cls._timestamp(events[-1].recorded_at) if events else None,
            "last_outcome": events[-1].outcome if events else None,
            "retained": attempts,
            "capacity": capacity,
        }

    @classmethod
    def _lifetime_summary(
        cls,
        lifetime: _ProviderLifetime | None,
        recent_events: list[_ProviderEvent],
    ) -> dict[str, Any]:
        if lifetime is None:
            return {
                "attempts": 0,
                "successful": 0,
                "success_rate": None,
                "success_rate_unit": "percent",
                "outcomes": {},
                "capabilities": {},
                "latency_ms": cls._latency_summary([]),
                "first_attempt_at": None,
                "last_attempt_at": None,
                "last_outcome": None,
            }
        return {
            "attempts": lifetime.attempts,
            "successful": lifetime.successful,
            "success_rate": lifetime.successful * 100.0 / lifetime.attempts,
            "success_rate_unit": "percent",
            "outcomes": dict(sorted(lifetime.outcomes.items())),
            "capabilities": dict(sorted(lifetime.capabilities.items())),
            "latency_ms": cls._latency_summary(
                [event.latency_ms for event in recent_events],
                total=lifetime.latency_total_ms,
                count=lifetime.attempts,
                maximum=lifetime.latency_max_ms,
            ),
            "first_attempt_at": cls._timestamp(lifetime.first_attempt_at),
            "last_attempt_at": cls._timestamp(lifetime.last_attempt_at),
            "last_outcome": lifetime.last_outcome,
        }

    def provider_statistics(self) -> dict[str, Any]:
        with self._lock:
            providers = sorted(self._provider_names)
            events = {key: list(value) for key, value in self._provider_events.items()}
            lifetime = {
                key: _ProviderLifetime(
                    attempts=value.attempts,
                    successful=value.successful,
                    outcomes=Counter(value.outcomes),
                    capabilities=Counter(value.capabilities),
                    latency_total_ms=value.latency_total_ms,
                    latency_max_ms=value.latency_max_ms,
                    first_attempt_at=value.first_attempt_at,
                    last_attempt_at=value.last_attempt_at,
                    last_outcome=value.last_outcome,
                )
                for key, value in self._provider_lifetime.items()
            }
        result: dict[str, Any] = {}
        for provider in providers:
            surfaces: dict[str, Any] = {}
            for surface in self._SURFACES:
                key = (provider, surface)
                recent = events.get(key, [])
                surfaces[surface] = {
                    "lifetime": self._lifetime_summary(lifetime.get(key), recent),
                    "recent": self._event_summary(recent, capacity=self._provider_window),
                }
            result[provider] = surfaces
        return result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latencies = list(self._latencies_ms)
            requests = {
                f"{route}|{status}": count for (route, status), count in self._requests.items()
            }
            fallbacks = dict(self._fallbacks)
            reconnects = dict(self._ws_reconnects)
            lag = self._event_loop_lag_ms
        return {
            "uptime_seconds": monotonic() - self._started,
            "requests_total": requests,
            "request_latency_ms": {
                "count": len(latencies),
                "p50": self._percentile(latencies, 0.50),
                "p95": self._percentile(latencies, 0.95),
                "p99": self._percentile(latencies, 0.99),
                "max": max(latencies, default=0.0),
            },
            "provider_fallbacks_total": fallbacks,
            "provider_statistics": self.provider_statistics(),
            "websocket_reconnects_total": reconnects,
            "event_loop_lag_ms": lag,
        }
