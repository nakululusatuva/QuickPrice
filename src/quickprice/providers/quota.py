"""Small in-memory quota gates used before making upstream requests."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

type QuotaPersistence = Callable[[Mapping[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    limit: int
    used: int
    remaining: int
    resets_at: float


class QuotaBudget:
    """Fixed-window quota with a hard reserve and an async-safe counter.

    Providers use UTC-aligned daily/monthly periods where practical.  Tests and
    custom deployments may inject a monotonic clock and a custom alignment.
    """

    def __init__(
        self,
        limit: int,
        period_seconds: float,
        *,
        reserve: int = 0,
        clock: Callable[[], float] = time.time,
        align_windows: bool = True,
        persistence: QuotaPersistence | None = None,
    ) -> None:
        if limit <= 0 or period_seconds <= 0:
            raise ValueError("limit and period_seconds must be positive")
        if not 0 <= reserve < limit:
            raise ValueError("reserve must be between zero and limit")
        self.limit = limit
        self.period_seconds = float(period_seconds)
        self.reserve = reserve
        self._clock = clock
        self._align_windows = align_windows
        self._lock = asyncio.Lock()
        now = clock()
        self._window_start = self._start_for(now)
        self._used = 0
        self._persistence = persistence

    def _start_for(self, now: float) -> float:
        if self._align_windows:
            return now - (now % self.period_seconds)
        return now

    def _roll(self, now: float) -> None:
        if now >= self._window_start + self.period_seconds:
            self._window_start = self._start_for(now)
            self._used = 0

    async def acquire(self, cost: int = 1, *, allow_reserve: bool = False) -> bool:
        if cost <= 0:
            raise ValueError("cost must be positive")
        async with self._lock:
            now = self._clock()
            previous_start = self._window_start
            previous_used = self._used
            self._roll(now)
            ceiling = self.limit if allow_reserve else self.limit - self.reserve
            if self._used + cost > ceiling:
                return False
            self._used += cost
            if self._persistence is not None:
                try:
                    await self._persistence(self._state())
                except BaseException:
                    # The upstream request has not happened yet. Roll back the
                    # reservation so durable and in-memory counters cannot
                    # diverge silently.
                    self._window_start = previous_start
                    self._used = previous_used
                    raise
            return True

    def _state(self) -> dict[str, int | float]:
        return {
            "version": 1,
            "limit": self.limit,
            "period_seconds": self.period_seconds,
            "reserve": self.reserve,
            "window_start": self._window_start,
            "used": self._used,
        }

    def set_persistence(self, callback: QuotaPersistence | None) -> None:
        self._persistence = callback

    async def checkpoint(self) -> dict[str, int | float]:
        async with self._lock:
            self._roll(self._clock())
            return self._state()

    async def restore(self, state: Mapping[str, Any]) -> None:
        """Restore a counter only when it belongs to the current fixed window."""

        async with self._lock:
            if int(state.get("version", 0)) != 1:
                raise ValueError("unsupported quota checkpoint version")
            if int(state.get("limit", -1)) != self.limit:
                raise ValueError("quota checkpoint limit does not match configuration")
            if float(state.get("period_seconds", -1)) != self.period_seconds:
                raise ValueError("quota checkpoint period does not match configuration")
            window_start = float(state["window_start"])
            used = int(state["used"])
            if used < 0:
                raise ValueError("quota checkpoint usage cannot be negative")
            now = self._clock()
            current_start = self._start_for(now)
            if window_start == current_start and now < window_start + self.period_seconds:
                self._window_start = window_start
                # Preserve an over-limit historical count if an operator lowers
                # reserve behavior; requests remain denied until reset.
                self._used = used

    async def snapshot(self) -> QuotaSnapshot:
        async with self._lock:
            now = self._clock()
            self._roll(now)
            return QuotaSnapshot(
                limit=self.limit,
                used=self._used,
                remaining=max(0, self.limit - self._used),
                resets_at=self._window_start + self.period_seconds,
            )


class SlidingWindowRateGate:
    """Async sliding-window burst limiter for short upstream rate limits."""

    def __init__(
        self,
        limit: int,
        period_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if limit <= 0 or period_seconds <= 0:
            raise ValueError("limit and period_seconds must be positive")
        self.limit = limit
        self.period_seconds = float(period_seconds)
        self._clock = clock
        self._sleeper = sleeper
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                cutoff = now - self.period_seconds
                while self._events and self._events[0] <= cutoff:
                    self._events.popleft()
                if len(self._events) < self.limit:
                    self._events.append(now)
                    return
                delay = max(0.0, self._events[0] + self.period_seconds - now)
            await self._sleeper(delay)


def daily_budget(limit: int, *, reserve: int = 0) -> QuotaBudget:
    return QuotaBudget(limit, 86_400, reserve=reserve)


def minute_budget(limit: int) -> QuotaBudget:
    return QuotaBudget(limit, 60)


def rolling_month_safe_daily_budget(limit: int) -> QuotaBudget:
    """Conservatively enforce a monthly ceiling using UTC daily windows.

    Any rolling 30-day interval can touch at most 31 aligned UTC days. Limiting
    each to ``floor(monthly/31)`` prevents a fixed-window boundary from allowing
    twice the advertised monthly allowance in a few seconds.
    """

    if limit < 31:
        raise ValueError("monthly limit must be at least 31")
    return daily_budget(limit // 31)
