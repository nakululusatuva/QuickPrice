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

from .auth import RateLimitError, TokenBucketLimiter

_SCRYPT_NAME: Final[str] = "scrypt"
_SCRYPT_N: Final[int] = 2**15
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1
_SCRYPT_MAXMEM: Final[int] = 128 * 1024 * 1024
_SESSION_LIMIT: Final[int] = 64
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


@dataclass(frozen=True, slots=True)
class AdminLoginResult:
    session_token: str
    csrf_token: str
    expires_at_epoch: float


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def generate_admin_key() -> str:
    """Return a 256-bit administrator credential suitable for one-time display."""

    return "qpa_" + secrets.token_urlsafe(32)


def generate_totp_secret() -> str:
    """Return a 160-bit RFC 6238 Base32 seed suitable for local enrollment."""

    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def create_admin_key_verifier(raw_key: str) -> str:
    if not 20 <= len(raw_key) <= 256:
        raise ValueError("administrator key must contain 20 to 256 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        raw_key.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            _SCRYPT_NAME,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def _parse_admin_key_verifier(value: str) -> tuple[int, int, int, bytes, bytes]:
    try:
        name, raw_n, raw_r, raw_p, raw_salt, raw_digest = value.split("$")
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt, digest = _b64decode(raw_salt), _b64decode(raw_digest)
    except (TypeError, ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid administrator key verifier") from exc
    if name != _SCRYPT_NAME or (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
        raise ValueError("unsupported administrator key verifier parameters")
    if len(salt) != 16 or len(digest) != 32:
        raise ValueError("invalid administrator key verifier length")
    return n, r, p, salt, digest


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
        key_verifier: str | None,
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
        configured_values = (key_verifier, totp_secret, expected_origin)
        if any(configured_values) and not all(configured_values):
            raise ValueError(
                "administrator key verifier, TOTP secret, and origin must be configured together"
            )
        self._configured = bool(all(configured_values))
        self._verifier = (
            _parse_admin_key_verifier(key_verifier) if key_verifier is not None else None
        )
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
        self._ip_login_limiter = TokenBucketLimiter(
            5, 3, max_identities=4096, idle_ttl_seconds=3600
        )
        self._global_login_limiter = TokenBucketLimiter(
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
        admin_key: str,
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

        bounded_key = admin_key if isinstance(admin_key, str) and len(admin_key) <= 256 else ""
        bounded_otp = otp if isinstance(otp, str) and len(otp) == 6 and otp.isascii() else ""
        n, r, p, salt, expected = self._verifier or (0, 0, 0, b"", b"")
        candidate = hashlib.scrypt(
            bounded_key.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=32,
            maxmem=_SCRYPT_MAXMEM,
        )
        key_valid = 20 <= len(bounded_key) <= 256 and hmac.compare_digest(candidate, expected)
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
            if not key_valid or matching_counter < 0 or replayed:
                raise AdminAuthenticationError("administrator authentication failed")
            self._last_totp_counter = matching_counter
            if len(self._sessions) >= _SESSION_LIMIT:
                oldest = min(
                    self._sessions, key=lambda item: self._sessions[item].last_seen_monotonic
                )
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
            )
        return AdminLoginResult(
            session_token=raw_session,
            csrf_token=csrf_token,
            expires_at_epoch=epoch + self.absolute_seconds,
        )

    def authorize(
        self,
        *,
        session_token: str | None,
        user_agent: str,
        csrf_token: str | None = None,
        mutation: bool = False,
        now_monotonic: float | None = None,
    ) -> AdminSession:
        self._require_configured()
        raw = session_token if session_token and len(session_token) <= 256 else ""
        digest = self._session_digest(raw)
        monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        with self._lock:
            self._prune_locked(monotonic)
            session = self._sessions.get(digest)
            if session is None or not hmac.compare_digest(
                session.user_agent_digest, self._user_agent_digest(user_agent)
            ):
                raise AdminAuthorizationError("administrator session is invalid")
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
    "AdminRateLimitError",
    "AdminSecurity",
    "create_admin_key_verifier",
    "generate_admin_key",
    "generate_totp_secret",
    "resolve_client_ip",
    "totp_code",
]
