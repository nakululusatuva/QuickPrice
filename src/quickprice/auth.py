"""Constant-time API-key verification and bounded in-memory token buckets."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from .config import Settings

_SHA256_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")


def hash_api_key(raw_key: str) -> str:
    if not raw_key:
        raise ValueError("API key cannot be empty")
    return "sha256:" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class AuthenticationError(Exception):
    pass


class RateLimitError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limit exceeded")
        self.retry_after = max(1, retry_after)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(
        self,
        rate_per_minute: int,
        capacity: int,
        *,
        max_identities: int = 10_000,
        idle_ttl_seconds: float = 600.0,
    ) -> None:
        if max_identities <= 0 or idle_ttl_seconds <= 0:
            raise ValueError("limiter bounds must be positive")
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = float(capacity)
        self._max_identities = max_identities
        self._idle_ttl_seconds = idle_ttl_seconds
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, identity: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            stale_before = now - self._idle_ttl_seconds
            while self._buckets:
                oldest_identity, oldest = next(iter(self._buckets.items()))
                if oldest.updated_at >= stale_before:
                    break
                self._buckets.pop(oldest_identity)
            bucket = self._buckets.get(identity)
            if bucket is None:
                if len(self._buckets) >= self._max_identities:
                    self._buckets.popitem(last=False)
                bucket = _Bucket(self._capacity, now)
                self._buckets[identity] = bucket
            else:
                self._buckets.move_to_end(identity)
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._rate_per_second)
            bucket.updated_at = now
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return
            wait = (1 - bucket.tokens) / self._rate_per_second
        raise RateLimitError(int(wait) + 1)


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.rate_limit_enabled
        self._hashes = tuple(self._validate_hash(item) for item in settings.api_key_hashes)
        self._valid_limiter = TokenBucketLimiter(
            settings.requests_per_minute, settings.request_burst
        )
        self._invalid_limiter = TokenBucketLimiter(
            settings.invalid_requests_per_minute, settings.invalid_request_burst
        )

    @staticmethod
    def _validate_hash(value: str) -> str:
        normalized = value.lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("QUICKPRICE_API_KEY_HASHES accepts only sha256:<64 hex chars>")
        return normalized

    @property
    def configured(self) -> bool:
        return bool(self._hashes)

    def authenticate(self, raw_key: str | None, client_ip: str) -> str:
        candidate = hash_api_key(raw_key) if raw_key else "sha256:" + "0" * 64
        matched = False
        # Compare every configured digest so timing does not disclose its position.
        for configured in self._hashes:
            matched = hmac.compare_digest(candidate, configured) or matched
        if not matched:
            if self._enabled:
                self._invalid_limiter.consume(client_ip)
            raise AuthenticationError("invalid API key")
        if self._enabled:
            self._valid_limiter.consume(candidate)
        return candidate
