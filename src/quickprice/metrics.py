"""Small dependency-free operational metric registry."""

from __future__ import annotations

import math
import threading
from collections import Counter, deque
from time import monotonic
from typing import Any


class Metrics:
    def __init__(self, latency_window: int = 4096) -> None:
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str, int]] = Counter()
        self._latencies_ms: deque[float] = deque(maxlen=latency_window)
        self._fallbacks: Counter[str] = Counter()
        self._ws_reconnects: Counter[str] = Counter()
        self._event_loop_lag_ms = 0.0
        self._started = monotonic()

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

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1)
        return ordered[index]

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
            "websocket_reconnects_total": reconnects,
            "event_loop_lag_ms": lag,
        }
