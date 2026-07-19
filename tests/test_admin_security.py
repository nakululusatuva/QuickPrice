from __future__ import annotations

import threading

import pytest

from quickprice.admin_account import AdminAccountStore, create_admin_password_verifier
from quickprice.admin_security import (
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminPasswordChangeRequiredError,
    AdminRateLimitError,
    AdminSecurity,
    generate_totp_secret,
    resolve_client_ip,
    totp_code,
)


def _security(
    tmp_path,
    password: str,
    secret: str,
    *,
    username: str = "admin",
    password_change_required: bool = False,
) -> tuple[AdminSecurity, AdminAccountStore]:
    store = AdminAccountStore(tmp_path / "admin-account.json")
    store.bootstrap(
        username=username,
        password_verifier=create_admin_password_verifier(password),
        password_change_required=password_change_required,
    )
    return (
        AdminSecurity(
            account_store=store,
            totp_secret=secret,
            expected_origin="https://quickprice.example",
            require_https=True,
            idle_seconds=900,
            absolute_seconds=3600,
            production=True,
        ),
        store,
    )


def test_admin_login_requires_username_password_and_totp_and_rejects_replay(tmp_path) -> None:
    password = "new-password"
    secret = generate_totp_secret()
    security, _store = _security(tmp_path, password, secret)
    epoch = 1_800_000_000.0

    result = security.login(
        username="admin",
        password=password,
        otp=totp_code(secret, timestamp=epoch),
        client_ip="203.0.113.7",
        user_agent="test-agent",
        now_epoch=epoch,
        now_monotonic=100.0,
    )
    assert result.username == "admin"
    assert result.password_change_required is False
    session = security.authorize(
        session_token=result.session_token,
        user_agent="test-agent",
        csrf_token=result.csrf_token,
        mutation=True,
        now_monotonic=101.0,
    )
    assert session.client_ip == "203.0.113.7"
    assert session.username == "admin"

    with pytest.raises(AdminAuthenticationError):
        security.login(
            username="admin",
            password=password,
            otp=totp_code(secret, timestamp=epoch),
            client_ip="203.0.113.7",
            user_agent="test-agent",
            now_epoch=epoch,
            now_monotonic=102.0,
        )


@pytest.mark.parametrize(
    ("username", "password"),
    (("wrong", "new-password"), ("admin", "incorrect")),
)
def test_failed_primary_credential_does_not_consume_totp(
    tmp_path, username: str, password: str
) -> None:
    secret = generate_totp_secret()
    security, _store = _security(tmp_path, "new-password", secret)
    epoch = 1_800_000_000.0
    otp = totp_code(secret, timestamp=epoch)

    with pytest.raises(AdminAuthenticationError):
        security.login(
            username=username,
            password=password,
            otp=otp,
            client_ip="203.0.113.7",
            user_agent="test-agent",
            now_epoch=epoch,
            now_monotonic=100.0,
        )
    security.login(
        username="admin",
        password="new-password",
        otp=otp,
        client_ip="203.0.113.7",
        user_agent="test-agent",
        now_epoch=epoch,
        now_monotonic=101.0,
    )


def test_password_change_required_session_is_restricted_by_default(tmp_path) -> None:
    secret = generate_totp_secret()
    security, _store = _security(
        tmp_path,
        "temporary-password",
        secret,
        password_change_required=True,
    )
    result = security.login(
        username="admin",
        password="temporary-password",
        otp=totp_code(secret, timestamp=1_800_000_000),
        client_ip="203.0.113.7",
        user_agent="browser-a",
        now_epoch=1_800_000_000,
        now_monotonic=100,
    )

    assert result.password_change_required is True
    with pytest.raises(AdminPasswordChangeRequiredError):
        security.authorize(
            session_token=result.session_token,
            user_agent="browser-a",
            now_monotonic=101,
        )
    session = security.authorize(
        session_token=result.session_token,
        user_agent="browser-a",
        csrf_token=result.csrf_token,
        mutation=True,
        allow_password_change_required=True,
        now_monotonic=101,
    )
    assert session.password_change_required is True


def test_account_change_reverifies_recent_session_and_rotates_all_sessions(tmp_path) -> None:
    secret = generate_totp_secret()
    security, _store = _security(
        tmp_path,
        "temporary-password",
        secret,
        password_change_required=True,
    )
    first = security.login(
        username="admin",
        password="temporary-password",
        otp=totp_code(secret, timestamp=1_800_000_000),
        client_ip="203.0.113.7",
        user_agent="browser-a",
        now_epoch=1_800_000_000,
        now_monotonic=100,
    )
    session = security.authorize(
        session_token=first.session_token,
        user_agent="browser-a",
        csrf_token=first.csrf_token,
        mutation=True,
        allow_password_change_required=True,
        now_monotonic=101,
    )

    replacement = security.change_account(
        session=session,
        current_password="temporary-password",
        new_username="nova",
        new_password="new-password",
        client_ip="203.0.113.8",
        user_agent="browser-a",
        now_epoch=1_800_000_002,
        now_monotonic=102,
    )

    assert replacement.username == "nova"
    assert replacement.password_change_required is False
    assert replacement.session_token != first.session_token
    assert replacement.csrf_token != first.csrf_token
    assert security.account_snapshot() == {
        "configured": True,
        "username": "nova",
        "password_change_required": False,
        "revision": security.account_snapshot()["revision"],
    }
    with pytest.raises(AdminAuthorizationError):
        security.authorize(
            session_token=first.session_token,
            user_agent="browser-a",
            now_monotonic=103,
        )
    current = security.authorize(
        session_token=replacement.session_token,
        user_agent="browser-a",
        csrf_token=replacement.csrf_token,
        mutation=True,
        now_monotonic=103,
    )
    assert current.username == "nova"
    assert current.client_ip == "203.0.113.8"


def test_account_change_requires_current_password_and_recent_login(tmp_path) -> None:
    secret = generate_totp_secret()
    security, _store = _security(tmp_path, "current-password", secret)
    result = security.login(
        username="admin",
        password="current-password",
        otp=totp_code(secret, timestamp=1_800_000_000),
        client_ip="203.0.113.7",
        user_agent="browser-a",
        now_epoch=1_800_000_000,
        now_monotonic=100,
    )
    session = security.authorize(
        session_token=result.session_token,
        user_agent="browser-a",
        csrf_token=result.csrf_token,
        mutation=True,
        now_monotonic=101,
    )
    with pytest.raises(AdminAuthenticationError):
        security.change_account(
            session=session,
            current_password="wrong-password",
            new_username="nova",
            new_password="new-password",
            client_ip="203.0.113.7",
            user_agent="browser-a",
            now_monotonic=102,
        )
    with pytest.raises(AdminAuthenticationError, match="recent authentication"):
        security.change_account(
            session=session,
            current_password="current-password",
            new_username="nova",
            new_password="new-password",
            client_ip="203.0.113.7",
            user_agent="browser-a",
            now_monotonic=401,
        )


def test_forced_account_change_rejects_reusing_the_bootstrap_password(tmp_path) -> None:
    secret = generate_totp_secret()
    security, store = _security(
        tmp_path,
        "temporary-password",
        secret,
        password_change_required=True,
    )
    result = security.login(
        username="admin",
        password="temporary-password",
        otp=totp_code(secret, timestamp=1_800_000_000),
        client_ip="203.0.113.7",
        user_agent="browser-a",
        now_epoch=1_800_000_000,
        now_monotonic=100,
    )
    session = security.authorize(
        session_token=result.session_token,
        user_agent="browser-a",
        csrf_token=result.csrf_token,
        mutation=True,
        allow_password_change_required=True,
        now_monotonic=101,
    )

    with pytest.raises(ValueError, match="must differ"):
        security.change_account(
            session=session,
            current_password="temporary-password",
            new_username="nova",
            new_password="temporary-password",
            client_ip="203.0.113.7",
            user_agent="browser-a",
            now_monotonic=102,
        )

    assert store.require_current().username == "admin"
    assert store.require_current().password_change_required is True


def test_account_change_current_password_attempts_have_a_strict_budget(tmp_path) -> None:
    secret = generate_totp_secret()
    security, _store = _security(tmp_path, "current-password", secret)
    result = security.login(
        username="admin",
        password="current-password",
        otp=totp_code(secret, timestamp=1_800_000_000),
        client_ip="203.0.113.7",
        user_agent="browser-a",
        now_epoch=1_800_000_000,
        now_monotonic=100,
    )
    session = security.authorize(
        session_token=result.session_token,
        user_agent="browser-a",
        csrf_token=result.csrf_token,
        mutation=True,
        now_monotonic=101,
    )

    for attempt in range(3):
        with pytest.raises(AdminAuthenticationError):
            security.change_account(
                session=session,
                current_password=f"wrong-password-{attempt}",
                new_username="nova",
                new_password="new-password",
                client_ip="203.0.113.7",
                user_agent="browser-a",
                now_monotonic=102 + attempt,
            )
    with pytest.raises(AdminRateLimitError):
        security.change_account(
            session=session,
            current_password="wrong-password-last",
            new_username="nova",
            new_password="new-password",
            client_ip="203.0.113.7",
            user_agent="browser-a",
            now_monotonic=106,
        )


def test_account_revision_change_invalidates_existing_session(tmp_path) -> None:
    secret = generate_totp_secret()
    security, store = _security(tmp_path, "current-password", secret)
    result = security.login(
        username="admin",
        password="current-password",
        otp=totp_code(secret, timestamp=1_800_000_000),
        client_ip="203.0.113.7",
        user_agent="browser-a",
        now_epoch=1_800_000_000,
        now_monotonic=100,
    )
    account = store.require_current()
    store.replace(
        username="nova",
        password_verifier=create_admin_password_verifier("new-password"),
        password_change_required=False,
        expected_revision=account.revision,
    )

    with pytest.raises(AdminAuthorizationError):
        security.authorize(
            session_token=result.session_token,
            user_agent="browser-a",
            now_monotonic=101,
        )


def test_admin_session_is_bound_to_user_agent_csrf_and_idle_lifetime(tmp_path) -> None:
    secret = generate_totp_secret()
    security, _store = _security(tmp_path, "current-password", secret)
    epoch = 1_800_000_000.0
    result = security.login(
        username="admin",
        password="current-password",
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


def test_scrypt_work_is_bounded_before_expensive_login(tmp_path) -> None:
    secret = generate_totp_secret()
    security, _store = _security(tmp_path, "current-password", secret)
    security._scrypt_slots = threading.BoundedSemaphore(1)
    assert security._scrypt_slots.acquire(blocking=False)
    try:
        with pytest.raises(AdminRateLimitError):
            security.login(
                username="admin",
                password="current-password",
                otp=totp_code(secret),
                client_ip="203.0.113.7",
                user_agent="browser-a",
            )
    finally:
        security._scrypt_slots.release()


def test_admin_browser_boundary_requires_exact_origin_https_and_same_origin_fetch(tmp_path) -> None:
    security, _store = _security(tmp_path, "current-password", generate_totp_secret())
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
