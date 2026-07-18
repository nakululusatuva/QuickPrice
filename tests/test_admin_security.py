from __future__ import annotations

import pytest

from quickprice.admin_security import (
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminSecurity,
    create_admin_key_verifier,
    generate_admin_key,
    generate_totp_secret,
    resolve_client_ip,
    totp_code,
)


def _security(raw_key: str, secret: str) -> AdminSecurity:
    return AdminSecurity(
        key_verifier=create_admin_key_verifier(raw_key),
        totp_secret=secret,
        expected_origin="https://quickprice.example",
        require_https=True,
        idle_seconds=900,
        absolute_seconds=3600,
        production=True,
    )


def test_admin_login_requires_both_factors_and_rejects_totp_replay() -> None:
    raw_key = generate_admin_key()
    secret = generate_totp_secret()
    security = _security(raw_key, secret)
    epoch = 1_800_000_000.0

    result = security.login(
        admin_key=raw_key,
        otp=totp_code(secret, timestamp=epoch),
        client_ip="203.0.113.7",
        user_agent="test-agent",
        now_epoch=epoch,
        now_monotonic=100.0,
    )
    session = security.authorize(
        session_token=result.session_token,
        user_agent="test-agent",
        csrf_token=result.csrf_token,
        mutation=True,
        now_monotonic=101.0,
    )
    assert session.client_ip == "203.0.113.7"

    with pytest.raises(AdminAuthenticationError):
        security.login(
            admin_key=raw_key,
            otp=totp_code(secret, timestamp=epoch),
            client_ip="203.0.113.7",
            user_agent="test-agent",
            now_epoch=epoch,
            now_monotonic=102.0,
        )


def test_admin_session_is_bound_to_user_agent_csrf_and_idle_lifetime() -> None:
    raw_key = generate_admin_key()
    secret = generate_totp_secret()
    security = _security(raw_key, secret)
    epoch = 1_800_000_000.0
    result = security.login(
        admin_key=raw_key,
        otp=totp_code(secret, timestamp=epoch),
        client_ip="203.0.113.7",
        user_agent="browser-a",
        now_epoch=epoch,
        now_monotonic=100.0,
    )

    with pytest.raises(AdminAuthorizationError):
        security.authorize(
            session_token=result.session_token,
            user_agent="browser-b",
            now_monotonic=101.0,
        )
    with pytest.raises(AdminAuthorizationError):
        security.authorize(
            session_token=result.session_token,
            user_agent="browser-a",
            csrf_token="wrong",
            mutation=True,
            now_monotonic=101.0,
        )
    with pytest.raises(AdminAuthorizationError):
        security.authorize(
            session_token=result.session_token,
            user_agent="browser-a",
            now_monotonic=1001.0,
        )


def test_admin_browser_boundary_requires_exact_origin_https_and_same_origin_fetch() -> None:
    security = _security(generate_admin_key(), generate_totp_secret())
    security.validate_browser_request(
        origin="https://quickprice.example",
        sec_fetch_site="same-origin",
        effective_scheme="https",
        mutation=True,
    )
    for origin, fetch_site, scheme in (
        ("https://evil.quickprice.example", "same-origin", "https"),
        ("https://quickprice.example", "same-site", "https"),
        ("https://quickprice.example", "same-origin", "http"),
        (None, "same-origin", "https"),
    ):
        with pytest.raises(AdminAuthorizationError):
            security.validate_browser_request(
                origin=origin,
                sec_fetch_site=fetch_site,
                effective_scheme=scheme,
                mutation=True,
            )

    security.validate_same_origin_action(
        origin=None,
        sec_fetch_site="same-origin",
        effective_scheme="https",
    )
    security.validate_same_origin_action(
        origin="https://quickprice.example",
        sec_fetch_site=None,
        effective_scheme="https",
    )
    for origin, fetch_site, scheme in (
        (None, None, "https"),
        (None, "same-site", "https"),
        ("https://evil.quickprice.example", "same-origin", "https"),
        ("https://quickprice.example", "same-origin", "http"),
    ):
        with pytest.raises(AdminAuthorizationError):
            security.validate_same_origin_action(
                origin=origin,
                sec_fetch_site=fetch_site,
                effective_scheme=scheme,
            )


def test_forwarded_client_ip_requires_an_explicit_trusted_proxy() -> None:
    assert resolve_client_ip("127.0.0.1", "203.0.113.9") == "127.0.0.1"
    assert (
        resolve_client_ip(
            "127.0.0.1",
            "203.0.113.9",
            trusted_proxy_ips=("127.0.0.1",),
        )
        == "203.0.113.9"
    )
    assert (
        resolve_client_ip(
            "198.51.100.8",
            "203.0.113.9",
            trusted_proxy_ips=("127.0.0.1",),
        )
        == "198.51.100.8"
    )
    assert (
        resolve_client_ip(
            "127.0.0.1",
            "203.0.113.9, 10.0.0.1",
            trusted_proxy_ips=("127.0.0.1",),
        )
        == "127.0.0.1"
    )
