"""Concurrency-safe expiring cache for low-frequency provider operations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .base import ProviderError


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    expires_at: float
    value: V | None = None
    error: ProviderError | None = None


class AsyncTtlCache[K, V]:
    """Cache values and expected provider failures with independently bounded TTLs.

    Per-key locks collapse concurrent misses. Expected failures are cached too,
    because repeatedly probing a broken emergency feed would consume the same
    scarce vendor allowance as successful requests. Callers may select a
    shorter error TTL when recovery must be observed before a long-lived
    successful value would normally expire.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[K, _Entry[V]] = {}
        self._locks: dict[K, asyncio.Lock] = {}

    def discard(self, key: K) -> None:
        """Remove one cached value or error without disturbing its singleflight lock."""

        self._entries.pop(key, None)

    @staticmethod
    def _clone_error(error: ProviderError) -> ProviderError:
        # ``copy.copy`` reconstructs exceptions from ``args``. ProviderError
        # subclasses deliberately expose a structured constructor whose
        # arguments differ from RuntimeError.args, so clone the exception state
        # without invoking that constructor again.
        cloned = type(error).__new__(type(error))
        cloned.args = error.args
        cloned.__dict__.update(error.__dict__)
        cloned.__cause__ = None
        cloned.__context__ = None
        return cloned

    @staticmethod
    def _read(entry: _Entry[V]) -> V:
        if entry.error is not None:
            # Re-raising one exception object repeatedly grows its traceback.
            # A clean copy keeps a long-lived negative cache bounded.
            error = AsyncTtlCache._clone_error(entry.error).with_traceback(None)
            raise error from None
        if entry.value is None:
            raise RuntimeError("TTL cache entry has neither a value nor an error")
        return entry.value

    async def get_or_load(
        self,
        key: K,
        ttl_seconds: float,
        loader: Callable[[], Awaitable[V]],
        *,
        error_ttl_seconds: float | None = None,
    ) -> V:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if error_ttl_seconds is None:
            error_ttl_seconds = ttl_seconds
        if error_ttl_seconds <= 0:
            raise ValueError("error_ttl_seconds must be positive")
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now < entry.expires_at:
            return self._read(entry)

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            started_at = self._clock()
            entry = self._entries.get(key)
            if entry is not None and started_at < entry.expires_at:
                return self._read(entry)
            try:
                value = await loader()
            except ProviderError as exc:
                cached_error = self._clone_error(exc).with_traceback(None)
                self._entries[key] = _Entry(
                    expires_at=started_at + error_ttl_seconds,
                    error=cached_error,
                )
                raise
            self._entries[key] = _Entry(
                expires_at=started_at + ttl_seconds,
                value=value,
            )
            return value
