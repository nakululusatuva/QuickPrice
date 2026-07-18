import pytest

from quickprice.auth import (
    AuthenticationError,
    Authenticator,
    RateLimitError,
    TokenBucketLimiter,
    hash_api_key,
)
from quickprice.config import Settings


def test_hash_and_constant_time_authentication_interface():
    settings = Settings(
        api_key_hashes=(hash_api_key("secret"),),
        rate_limit_enabled=False,
    )
    authenticator = Authenticator(settings)
    assert authenticator.authenticate("secret", "127.0.0.1") == hash_api_key("secret")
    with pytest.raises(AuthenticationError):
        authenticator.authenticate("wrong", "127.0.0.1")


def test_valid_key_burst_is_limited():
    settings = Settings(
        api_key_hashes=(hash_api_key("secret"),),
        rate_limit_enabled=True,
        requests_per_minute=1,
        request_burst=2,
    )
    authenticator = Authenticator(settings)
    authenticator.authenticate("secret", "127.0.0.1")
    authenticator.authenticate("secret", "127.0.0.1")
    with pytest.raises(RateLimitError):
        authenticator.authenticate("secret", "127.0.0.1")


def test_rotating_identities_cannot_grow_limiter_without_bound():
    limiter = TokenBucketLimiter(60, 1, max_identities=3, idle_ttl_seconds=10)
    for index in range(20):
        limiter.consume(f"2001:db8::{index}", now=float(index))
    assert len(limiter._buckets) <= 3

    limiter.consume("fresh", now=100.0)
    assert list(limiter._buckets) == ["fresh"]
