"""Hardened, browser-only administrator authentication primitives.

The administrator credential is deliberately independent from quote API keys.
Successful authentication creates an opaque, process-local session; only the
session cookie is sent to the browser and only its SHA-256 digest is retained.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from .admin_account import (
    AdminAccount,
    AdminAccountStore,
    create_admin_password_verifier,
    validate_admin_username,
    verify_admin_password,
)
from .auth import RateLimitError, TokenBucketLimiter

_SESSION_LIMIT: Final[int] = 64
_SCRYPT_WORK_LIMIT: Final[int] = 2
_ACCOUNT_CHANGE_MAX_SESSION_AGE: Final[float] = 300.0
_COOKIE_PRODUCTION: Final[str] = "__Host-quickprice_admin"
_COOKIE_DEVELOPMENT: Final[str] = "quickprice_admin"


class AdminSecurityError(RuntimeError):
    pass


class AdminNotConfiguredError(AdminSecurityError):
    pass


class AdminAuthenticationError(AdminSecurityError):
    pass


class AdminAuthorizationError(AdminSecurityError):
    pass


class AdminPasswordChangeRequiredError(AdminAuthorizationError):
    pass


class AdminRateLimitError(AdminSecurityError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("administrator authentication rate limit exceeded")
        self.retry_after = max(1, retry_after)


@dataclass(slots=True)
class AdminSession:
    session_digest: str
    csrf_token: str
    user_agent_digest: str
    created_monotonic: float
    last_seen_monotonic: float
    absolute_expires_monotonic: float
    created_at_epoch: float
    client_ip: str
    username: str
    account_revision: str
    password_change_required: bool


@dataclass(frozen=True, slots=True)
class AdminLoginResult:
    session_token: str
    csrf_token: str
    expires_at_epoch: float
    username: str
    password_change_required: bool


def generate_totp_secret() -> str:
    """Return a 160-bit RFC 6238 Base32 seed suitable for local enrollment."""

    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_totp_secret(value: str) -> bytes:
    normalized = "".join(value.upper().split())
    if not 16 <= len(normalized) <= 128:
        raise ValueError("invalid administrator TOTP secret")
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    try:
        decoded = base64.b32decode(normalized + padding, casefold=False)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid administrator TOTP secret") from exc
    if len(decoded) < 16:
        raise ValueError("administrator TOTP secret must contain at least 128 bits")
    return decoded


def totp_code(
    secret: str | bytes, *, timestamp: float | None = None, counter: int | None = None
) -> str:
    """Generate a six-digit RFC 6238 code; exposed for local enrollment tests."""

    key = _decode_totp_secret(secret) if isinstance(secret, str) else secret
    step = (
        int((time.time() if timestamp is None else timestamp) // 30) if counter is None else counter
    )
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


def resolve_client_ip(
    peer_ip: str | None,
    forwarded_ip: str | None,
    *,
    trusted_proxy_ips: tuple[str, ...] = (),
) -> str:
    """Trust one normalized X-Real-IP value only from an explicit proxy peer."""

    peer = peer_ip or "unknown"
    try:
        normalized_peer = str(ipaddress.ip_address(peer))
    except ValueError:
        normalized_peer = peer
    if normalized_peer in trusted_proxy_ips and forwarded_ip and "," not in forwarded_ip:
        candidate = forwarded_ip.strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return peer


class AdminSecurity:
    """Authenticate the administrator and own bounded, in-memory sessions."""

    def __init__(
        self,
        *,
        account_store: AdminAccountStore | None,
        totp_secret: str | None,
        expected_origin: str | None,
        require_https: bool,
        idle_seconds: int,
        absolute_seconds: int,
        production: bool,
        trusted_proxy_ips: tuple[str, ...] = (),
    ) -> None:
        if idle_seconds < 300 or absolute_seconds < 900 or idle_seconds > absolute_seconds:
            raise ValueError("invalid administrator session lifetime")
        account_configured = account_store is not None and account_store.configured
        configured_values = (account_configured, bool(totp_secret), bool(expected_origin))
        if any(configured_values) and not all(configured_values):
            raise ValueError(
                "administrator account, TOTP secret, and origin must be configured together"
            )
        self._configured = bool(all(configured_values))
        self._account_store = account_store
        self._totp_secret = _decode_totp_secret(totp_secret) if totp_secret is not None else None
        self._expected_origin = expected_origin.rstrip("/") if expected_origin else None
        parsed_origin = urlsplit(self._expected_origin) if self._expected_origin else None
        if parsed_origin is not None:
            try:
                _ = parsed_origin.port
            except ValueError as exc:
                raise ValueError("administrator origin has an invalid port") from exc
            if (
                parsed_origin.scheme not in {"http", "https"}
                or not parsed_origin.hostname
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.path
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise ValueError("administrator origin must be an HTTP(S) origin without a path")
        if (
            production
            and self._expected_origin
            and not self._expected_origin.startswith("https://")
        ):
            raise ValueError("production administrator origin must use HTTPS")
        self.require_https = require_https
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds
        self.production = production
        try:
            self.trusted_proxy_ips = tuple(
                dict.fromkeys(str(ipaddress.ip_address(item)) for item in trusted_proxy_ips)
            )
        except ValueError as exc:
            raise ValueError("administrator trusted proxies must be explicit IP addresses") from exc
        self.cookie_name = _COOKIE_PRODUCTION if production else _COOKIE_DEVELOPMENT
        self._sessions: dict[str, AdminSession] = {}
        self._last_totp_counter = -1
        self._lock = threading.Lock()
        self._scrypt_slots = threading.BoundedSemaphore(_SCRYPT_WORK_LIMIT)
        self._ip_login_limiter = TokenBucketLimiter(
            5, 3, max_identities=4096, idle_ttl_seconds=3600
        )
        self._global_login_limiter = TokenBucketLimiter(
            30, 10, max_identities=1, idle_ttl_seconds=3600
        )
        self._account_change_limiter = TokenBucketLimiter(
            5, 3, max_identities=_SESSION_LIMIT, idle_ttl_seconds=3600
        )
        self._global_account_change_limiter = TokenBucketLimiter(
            30, 10, max_identities=1, idle_ttl_seconds=3600
        )
        self._mutation_limiter = TokenBucketLimiter(
            120, 20, max_identities=_SESSION_LIMIT, idle_ttl_seconds=3600
        )
        self._global_mutation_limiter = TokenBucketLimiter(
            600, 100, max_identities=1, idle_ttl_seconds=3600
        )
        self._request_limiter = TokenBucketLimiter(
            120, 30, max_identities=4096, idle_ttl_seconds=3600
        )
        self._global_request_limiter = TokenBucketLimiter(
            600, 100, max_identities=1, idle_ttl_seconds=3600
        )

    @property
    def configured(self) -> bool:
        return self._configured

    def validate_browser_request(
        self,
        *,
        origin: str | None,
        sec_fetch_site: str | None,
        effective_scheme: str,
        mutation: bool,
    ) -> None:
        self._require_configured()
        if self.require_https and effective_scheme.lower() != "https":
            raise AdminAuthorizationError("administrator HTTPS is required")
        if origin != self._expected_origin:
            raise AdminAuthorizationError("administrator origin is invalid")
        if mutation and sec_fetch_site is not None and sec_fetch_site.lower() != "same-origin":
            raise AdminAuthorizationError("cross-site administrator request rejected")

    def validate_same_origin_action(
        self,
        *,
        origin: str | None,
        sec_fetch_site: str | None,
        effective_scheme: str,
    ) -> None:
        """Validate a browser action whose HTTP method is otherwise read-only.

        Provider symbol discovery uses GET for a stable administrative API
        contract, but it can consume upstream quota. Require both the session
        CSRF token (in :meth:`authorize`) and an unforgeable browser same-origin
        signal before allowing that request to reach a provider.
        """

        self._require_configured()
        if self.require_https and effective_scheme.lower() != "https":
            raise AdminAuthorizationError("administrator HTTPS is required")
        if origin is not None and origin != self._expected_origin:
            raise AdminAuthorizationError("administrator origin is invalid")
        if sec_fetch_site is not None and sec_fetch_site.lower() != "same-origin":
            raise AdminAuthorizationError("cross-site administrator request rejected")
        if origin != self._expected_origin and (
            sec_fetch_site is None or sec_fetch_site.lower() != "same-origin"
        ):
            raise AdminAuthorizationError("administrator same-origin signal is missing")

    def throttle_browser_request(self, client_ip: str) -> None:
        """Bound every admin API request, including requests rejected during parsing."""

        try:
            self._request_limiter.consume(client_ip)
            self._global_request_limiter.consume("global")
        except RateLimitError as exc:
            raise AdminRateLimitError(exc.retry_after) from exc

    def trusts_proxy(self, peer_ip: str | None) -> bool:
        try:
            normalized = str(ipaddress.ip_address(peer_ip or ""))
        except ValueError:
            return False
        return normalized in self.trusted_proxy_ips

    def resolve_client_ip(self, peer_ip: str | None, forwarded_ip: str | None) -> str:
        return resolve_client_ip(
            peer_ip,
            forwarded_ip,
            trusted_proxy_ips=self.trusted_proxy_ips,
        )

    def login(
        self,
        *,
        username: str,
        password: str,
        otp: str,
        client_ip: str,
        user_agent: str,
        now_epoch: float | None = None,
        now_monotonic: float | None = None,
    ) -> AdminLoginResult:
        self._require_configured()
        try:
            self._ip_login_limiter.consume(client_ip)
            self._global_login_limiter.consume("global")
        except RateLimitError as exc:
            raise AdminRateLimitError(exc.retry_after) from exc

        account = self._require_account()
        bounded_username = username if isinstance(username, str) and len(username) <= 64 else ""
        bounded_otp = (
            otp
            if isinstance(otp, str) and len(otp) == 6 and otp.isascii() and otp.isdigit()
            else ""
        )
        password_valid = self._verify_password(password, account)
        username_valid = hmac.compare_digest(
            bounded_username.encode("utf-8"), account.username.encode("utf-8")
        )
        epoch = time.time() if now_epoch is None else now_epoch
        current_counter = int(epoch // 30)
        matching_counter = -1
        for counter in (current_counter - 1, current_counter, current_counter + 1):
            equal = hmac.compare_digest(
                totp_code(self._totp_secret or b"", counter=counter), bounded_otp
            )
            if equal:
                matching_counter = counter

        monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        with self._lock:
            self._prune_locked(monotonic)
            replayed = matching_counter <= self._last_totp_counter
            if not username_valid or not password_valid or matching_counter < 0 or replayed:
                raise AdminAuthenticationError("administrator authentication failed")
            self._last_totp_counter = matching_counter
            return self._create_session_locked(
                account=account,
                client_ip=client_ip,
                user_agent=user_agent,
                epoch=epoch,
                monotonic=monotonic,
            )

    def authorize(
        self,
        *,
        session_token: str | None,
        user_agent: str,
        csrf_token: str | None = None,
        mutation: bool = False,
        allow_password_change_required: bool = False,
        now_monotonic: float | None = None,
    ) -> AdminSession:
        self._require_configured()
        account = self._require_account()
        raw = session_token if session_token and len(session_token) <= 256 else ""
        digest = self._session_digest(raw)
        monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        with self._lock:
            self._prune_locked(monotonic)
            session = self._sessions.get(digest)
            if (
                session is None
                or not hmac.compare_digest(
                    session.user_agent_digest, self._user_agent_digest(user_agent)
                )
                or not hmac.compare_digest(session.account_revision, account.revision)
            ):
                self._sessions.pop(digest, None)
                raise AdminAuthorizationError("administrator session is invalid")
            if session.password_change_required and not allow_password_change_required:
                raise AdminPasswordChangeRequiredError(
                    "administrator password must be changed before continuing"
                )
            if mutation and not (
                csrf_token
                and len(csrf_token) <= 256
                and hmac.compare_digest(session.csrf_token, csrf_token)
            ):
                raise AdminAuthorizationError("administrator CSRF token is invalid")
            if mutation:
                try:
                    self._mutation_limiter.consume(digest)
                    self._global_mutation_limiter.consume("global")
                except RateLimitError as exc:
                    raise AdminRateLimitError(exc.retry_after) from exc
            session.last_seen_monotonic = monotonic
            return session

    def account_snapshot(self) -> dict[str, object]:
        """Return secret-free metadata for the single administrator account."""

        self._require_configured()
        store = self._account_store
        if store is None:
            raise AdminNotConfiguredError("administrator authentication is not configured")
        return store.snapshot()

    def change_account(
        self,
        *,
        session: AdminSession,
        current_password: str,
        new_username: str,
        new_password: str,
        client_ip: str,
        user_agent: str,
        now_epoch: float | None = None,
        now_monotonic: float | None = None,
    ) -> AdminLoginResult:
        """Replace the account after recent reauthentication and rotate the session.

        The caller receives a new session and CSRF token. Every prior session,
        including the session used for this request, is revoked atomically from
        the perspective of subsequent authorization checks.
        """

        epoch = time.time() if now_epoch is None else now_epoch
        monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        age = monotonic - session.created_monotonic
        if age < 0 or age > _ACCOUNT_CHANGE_MAX_SESSION_AGE:
            raise AdminAuthenticationError(
                "administrator account change requires recent authentication"
            )
        account = self._require_account()
        with self._lock:
            if (
                self._sessions.get(session.session_digest) is not session
                or not hmac.compare_digest(
                    session.user_agent_digest, self._user_agent_digest(user_agent)
                )
                or not hmac.compare_digest(session.account_revision, account.revision)
            ):
                raise AdminAuthorizationError("administrator session is invalid")
        try:
            self._account_change_limiter.consume(session.session_digest)
            self._global_account_change_limiter.consume("global")
        except RateLimitError as exc:
            raise AdminRateLimitError(exc.retry_after) from exc
        if not self._verify_password(current_password, account):
            raise AdminAuthenticationError("administrator authentication failed")
        validate_admin_username(new_username)
        if new_password == current_password:
            raise ValueError("new administrator password must differ from the current password")
        new_verifier = self._create_password_verifier(new_password)
        store = self._account_store
        if store is None:
            raise AdminNotConfiguredError("administrator authentication is not configured")
        with self._lock:
            current_session = self._sessions.get(session.session_digest)
            current_account = store.current()
            if (
                current_session is not session
                or current_account is None
                or not hmac.compare_digest(
                    session.user_agent_digest, self._user_agent_digest(user_agent)
                )
                or not hmac.compare_digest(session.account_revision, current_account.revision)
            ):
                raise AdminAuthorizationError("administrator session is invalid")
            replacement = store.replace(
                username=new_username,
                password_verifier=new_verifier,
                password_change_required=False,
                expected_revision=session.account_revision,
            )
            self._sessions.clear()
            return self._create_session_locked(
                account=replacement,
                client_ip=client_ip,
                user_agent=user_agent,
                epoch=epoch,
                monotonic=monotonic,
            )

    def invalidate_sessions(self) -> None:
        with self._lock:
            self._sessions.clear()

    def logout(self, session_token: str | None) -> None:
        digest = self._session_digest(session_token or "")
        with self._lock:
            self._sessions.pop(digest, None)

    def expires_at_epoch(self, session: AdminSession, *, now_epoch: float | None = None) -> float:
        epoch = time.time() if now_epoch is None else now_epoch
        remaining = max(0.0, session.absolute_expires_monotonic - time.monotonic())
        return epoch + remaining

    def _prune_locked(self, now: float) -> None:
        expired = [
            digest
            for digest, session in self._sessions.items()
            if now >= session.absolute_expires_monotonic
            or now - session.last_seen_monotonic >= self.idle_seconds
        ]
        for digest in expired:
            self._sessions.pop(digest, None)

    def _require_configured(self) -> None:
        if not self._configured:
            raise AdminNotConfiguredError("administrator authentication is not configured")

    def _require_account(self) -> AdminAccount:
        self._require_configured()
        store = self._account_store
        account = store.current() if store is not None else None
        if account is None:
            raise AdminNotConfiguredError("administrator authentication is not configured")
        return account

    def _verify_password(self, password: str, account: AdminAccount) -> bool:
        if not self._scrypt_slots.acquire(blocking=False):
            raise AdminRateLimitError(1)
        try:
            return verify_admin_password(password, account.password_verifier)
        finally:
            self._scrypt_slots.release()

    def _create_password_verifier(self, password: str) -> str:
        if not self._scrypt_slots.acquire(blocking=False):
            raise AdminRateLimitError(1)
        try:
            return create_admin_password_verifier(password)
        finally:
            self._scrypt_slots.release()

    def _create_session_locked(
        self,
        *,
        account: AdminAccount,
        client_ip: str,
        user_agent: str,
        epoch: float,
        monotonic: float,
    ) -> AdminLoginResult:
        if len(self._sessions) >= _SESSION_LIMIT:
            oldest = min(self._sessions, key=lambda item: self._sessions[item].last_seen_monotonic)
            self._sessions.pop(oldest, None)
        raw_session = secrets.token_urlsafe(32)
        session_digest = self._session_digest(raw_session)
        csrf_token = secrets.token_urlsafe(32)
        self._sessions[session_digest] = AdminSession(
            session_digest=session_digest,
            csrf_token=csrf_token,
            user_agent_digest=self._user_agent_digest(user_agent),
            created_monotonic=monotonic,
            last_seen_monotonic=monotonic,
            absolute_expires_monotonic=monotonic + self.absolute_seconds,
            created_at_epoch=epoch,
            client_ip=client_ip,
            username=account.username,
            account_revision=account.revision,
            password_change_required=account.password_change_required,
        )
        return AdminLoginResult(
            session_token=raw_session,
            csrf_token=csrf_token,
            expires_at_epoch=epoch + self.absolute_seconds,
            username=account.username,
            password_change_required=account.password_change_required,
        )

    @staticmethod
    def _session_digest(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _user_agent_digest(value: str) -> str:
        return hashlib.sha256(value[:1024].encode("utf-8")).hexdigest()


__all__ = [
    "AdminAuthenticationError",
    "AdminAuthorizationError",
    "AdminLoginResult",
    "AdminNotConfiguredError",
    "AdminPasswordChangeRequiredError",
    "AdminRateLimitError",
    "AdminSecurity",
    "create_admin_password_verifier",
    "generate_totp_secret",
    "resolve_client_ip",
    "totp_code",
]
