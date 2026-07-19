"""FastAPI route layer; all market-data reads are memory-only."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .admin_api import install_admin_routes
from .admin_security import (
    AdminAuthorizationError,
    AdminNotConfiguredError,
    AdminRateLimitError,
    AdminSecurity,
)
from .api_keys import ApiKeyManager, AuthContext
from .auth import AuthenticationError, Authenticator, RateLimitError
from .config import Settings
from .dashboard_logs import DashboardLogBroker, DashboardLogCapacityError
from .domain import utc_now
from .managed_config import (
    InstrumentPolicyStore,
    ManagedEnvironmentStore,
    ProviderKeyStore,
)
from .registry import InstrumentRegistry, build_registry, normalize_symbol
from .schemas import EnvelopeModel, ErrorModel, instrument_to_wire
from .service import DataUnavailableError, QuickPriceService

_LOGGER = logging.getLogger(__name__)
_DASHBOARD_ROOT = Path(__file__).with_name("dashboard")
_ADMIN_ROOT = Path(__file__).with_name("admin")
_LOG_STREAM_PATH = "/internal/logs/stream"
_CATALOG_REVISION_HEADER = "X-QuickPrice-Catalog-Revision"
_ADMIN_BODY_LIMIT = 64 * 1024
_ADMIN_CATALOG_IMPORT_BODY_LIMIT = 8 * 1024 * 1024
_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "connect-src 'self'",
        "font-src 'self'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "img-src 'self' data:",
        "object-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
    )
)


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        symbol: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.symbol = symbol
        self.headers = headers or {}


class _AdminRequestBodyTooLarge(RuntimeError):
    pass


class _AdminRequestGuardMiddleware:
    """Authenticate, rate-limit, and cap admin requests before body parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        maximum_bytes: int,
        catalog_import_maximum_bytes: int,
        security: AdminSecurity,
    ) -> None:
        self.app = app
        self.maximum_bytes = maximum_bytes
        self.catalog_import_maximum_bytes = catalog_import_maximum_bytes
        self.security = security
        self._catalog_import_slots = asyncio.Semaphore(1)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        if scope["type"] != "http" or not path.startswith("/admin-api/"):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        maximum_bytes = (
            self.catalog_import_maximum_bytes
            if path == "/admin-api/instrument-catalog/import"
            else self.maximum_bytes
        )
        peer = scope.get("client")
        peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else None
        forwarded_ip = headers.get(b"x-real-ip")
        try:
            self.security.throttle_browser_request(
                self.security.resolve_client_ip(
                    peer_ip,
                    forwarded_ip.decode("ascii", errors="ignore") if forwarded_ip else None,
                )
            )
        except AdminRateLimitError as exc:
            await self._error(
                scope,
                receive,
                send,
                status_code=429,
                code="admin_rate_limited",
                message="administrator request rate limit exceeded",
                retry_after=exc.retry_after,
            )
            return

        is_login = method == "POST" and path == "/admin-api/session"
        provider_search = (
            method in {"GET", "HEAD"}
            and path.startswith("/admin-api/provider-catalog/")
            and path.endswith("/search")
        )
        if not is_login:
            mutation = method not in {"GET", "HEAD", "OPTIONS"}
            authorization_level = (
                "same_origin_action" if provider_search else ("mutation" if mutation else "read")
            )
            try:
                request = Request(scope)
                scheme = str(scope.get("scheme", "http"))
                forwarded_proto = headers.get(b"x-forwarded-proto")
                if self.security.trusts_proxy(peer_ip) and forwarded_proto is not None:
                    candidate = forwarded_proto.decode("ascii", errors="ignore").lower()
                    if candidate in {"http", "https"}:
                        scheme = candidate
                origin = request.headers.get("Origin")
                sec_fetch_site = request.headers.get("Sec-Fetch-Site")
                if mutation:
                    self.security.validate_browser_request(
                        origin=origin,
                        sec_fetch_site=sec_fetch_site,
                        effective_scheme=scheme,
                        mutation=True,
                    )
                elif provider_search:
                    self.security.validate_same_origin_action(
                        origin=origin,
                        sec_fetch_site=sec_fetch_site,
                        effective_scheme=scheme,
                    )
                session = self.security.authorize(
                    session_token=request.cookies.get(self.security.cookie_name),
                    user_agent=request.headers.get("User-Agent", ""),
                    csrf_token=request.headers.get("X-CSRF-Token"),
                    mutation=mutation or provider_search,
                )
            except AdminNotConfiguredError:
                await self._error(
                    scope,
                    receive,
                    send,
                    status_code=503,
                    code="admin_not_configured",
                    message="administrator access is unavailable",
                )
                return
            except AdminRateLimitError as exc:
                await self._error(
                    scope,
                    receive,
                    send,
                    status_code=429,
                    code="admin_rate_limited",
                    message="administrator request rate limit exceeded",
                    retry_after=exc.retry_after,
                )
                return
            except AdminAuthorizationError:
                await self._error(
                    scope,
                    receive,
                    send,
                    status_code=401,
                    code="admin_unauthorized",
                    message="administrator authorization failed",
                )
                return
            state = scope.setdefault("state", {})
            state["quickprice_admin_session"] = session
            state["quickprice_admin_authorization_level"] = authorization_level

        if method in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return

        content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if content_type != b"application/json":
            await self._error(
                scope,
                receive,
                send,
                status_code=415,
                code="json_required",
                message="administrator mutations require application/json",
            )
            return

        raw_length = headers.get(b"content-length")
        if b"transfer-encoding" in headers:
            await self._body_too_large(scope, receive, send)
            return
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await self._body_too_large(scope, receive, send)
                return
            if content_length < 0 or content_length > maximum_bytes:
                await self._body_too_large(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum_bytes:
                    raise _AdminRequestBodyTooLarge
            return message

        async def dispatch() -> None:
            try:
                await self.app(scope, limited_receive, send)
            except _AdminRequestBodyTooLarge:
                await self._body_too_large(scope, receive, send)

        if path == "/admin-api/instrument-catalog/import":
            async with self._catalog_import_slots:
                await dispatch()
            return
        await dispatch()

    @staticmethod
    async def _body_too_large(scope: Scope, receive: Receive, send: Send) -> None:
        await _AdminRequestGuardMiddleware._error(
            scope,
            receive,
            send,
            status_code=413,
            code="request_too_large",
            message="administrator request body exceeds the safe limit",
        )

    @staticmethod
    async def _error(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
        retry_after: int | None = None,
    ) -> None:
        response = JSONResponse(
            {"error": {"code": code, "message": message}},
            status_code=status_code,
            headers={"Retry-After": str(retry_after)} if retry_after is not None else None,
        )
        await response(scope, receive, send)


def _request_id() -> str:
    return str(uuid.uuid7())


def _dashboard_redacted_values(settings: Settings) -> tuple[str | None, ...]:
    return (
        *settings.api_key_hashes,
        settings.admin_key_verifier,
        settings.admin_totp_secret,
        settings.alpaca_api_key,
        settings.alpaca_api_secret,
        settings.twelve_data_api_key,
        settings.alpha_vantage_api_key,
        settings.finnhub_api_key,
        settings.coingecko_api_key,
        settings.fred_api_key,
        settings.binance_api_key,
        settings.binance_api_secret,
        settings.okx_api_key,
        settings.okx_api_secret,
        settings.okx_api_passphrase,
        settings.provider_proxy_url,
        *settings.ethereum_rpc_urls,
    )


def _envelope(
    request: Request,
    *,
    data: Any,
    errors: list[ErrorModel] | None = None,
    partial: bool = False,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    model = EnvelopeModel(
        request_id=request.state.request_id,
        generated_at=generated_at or utc_now(),
        partial=partial,
        data=data,
        errors=errors or [],
    )
    return model.model_dump(mode="json")


def _if_none_match_matches(value: str | None, etag: str) -> bool:
    """Match the active catalog validator without accepting malformed partial tags."""

    if value is None:
        return False
    return any(
        candidate in {"*", etag, f"W/{etag}"} for candidate in map(str.strip, value.split(","))
    )


def create_app(
    settings: Settings | None = None,
    service: QuickPriceService | None = None,
    registry: InstrumentRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    admin_catalog: InstrumentRegistry
    active_catalog: Any
    if service is not None:
        if registry is not None and registry is not service.registry:
            raise ValueError("the API registry must match the service registry")
        registry = service.registry
        admin_catalog = build_registry(settings.enabled_plugins)
        managed_instruments = InstrumentPolicyStore(
            settings.managed_instruments_path,
            admin_catalog,
            defer_migration=True,
        )
        active_catalog = managed_instruments.active_generation()
        active_registry = active_catalog.to_registry()
        if active_registry.symbols == service.registry.symbols:
            service._activate_runtime_generation(
                active_registry,
                revision=active_catalog.revision,
                catalog=active_catalog,
            )
    else:
        admin_catalog = registry or build_registry(settings.enabled_plugins)
        managed_instruments = InstrumentPolicyStore(
            settings.managed_instruments_path,
            admin_catalog,
            defer_migration=True,
        )
        active_catalog = managed_instruments.active_generation()
        registry = active_catalog.to_registry()
        service = QuickPriceService(
            settings,
            registry,
            runtime_revision=active_catalog.revision,
            runtime_catalog=active_catalog,
        )
    api_key_manager = ApiKeyManager(settings.api_key_hashes)
    authenticator = Authenticator(settings, api_key_manager)
    service.bind_api_key_state(lambda: authenticator.configured)
    admin_security = AdminSecurity(
        key_verifier=settings.admin_key_verifier,
        totp_secret=settings.admin_totp_secret,
        expected_origin=settings.admin_origin,
        require_https=settings.admin_require_https,
        idle_seconds=settings.admin_session_idle_seconds,
        absolute_seconds=settings.admin_session_absolute_seconds,
        production=settings.production,
        trusted_proxy_ips=settings.admin_trusted_proxy_ips,
    )
    managed_configuration = ManagedEnvironmentStore(settings.managed_config_path, settings)
    managed_provider_keys = ProviderKeyStore(settings.managed_provider_keys_path)

    async def catalog_audit_sink(
        action: str,
        target_id: str,
        details: Any,
        audit: Any,
    ) -> None:
        if audit is None:
            return
        await api_key_manager.append_audit(
            audit=audit,
            action=action,
            target_type="instrument_catalog_job",
            target_id=target_id,
            details=dict(details),
        )

    service.bind_instrument_catalog(
        managed_instruments,
        audit_sink=catalog_audit_sink,
    )
    dashboard_logs = DashboardLogBroker(
        max_subscribers=settings.dashboard_max_log_streams,
        redacted_values=_dashboard_redacted_values(settings),
    )
    dashboard_logger = logging.getLogger("quickprice")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        previous_level = dashboard_logger.level
        level_changed = dashboard_logger.getEffectiveLevel() > logging.INFO
        if level_changed:
            dashboard_logger.setLevel(logging.INFO)
        dashboard_logger.addHandler(dashboard_logs)
        try:
            _LOGGER.info("QuickPrice startup initiated")
            try:
                await service.start()
                await api_key_manager.start(service.storage)
                await asyncio.to_thread(managed_instruments.persist_migration)
            except Exception as exc:
                _LOGGER.error("QuickPrice startup failed error_type=%s", type(exc).__name__)
                raise
            _LOGGER.info("QuickPrice startup complete")
            try:
                yield
            finally:
                _LOGGER.info("QuickPrice shutdown initiated")
                try:
                    await service.stop()
                except Exception as exc:
                    _LOGGER.error("QuickPrice shutdown failed error_type=%s", type(exc).__name__)
                    raise
                finally:
                    _LOGGER.info("QuickPrice shutdown complete")
        finally:
            dashboard_logger.removeHandler(dashboard_logs)
            if level_changed:
                dashboard_logger.setLevel(previous_level)
            dashboard_logs.close()

    app = FastAPI(
        title="QuickPrice",
        version=__version__,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.add_middleware(
        _AdminRequestGuardMiddleware,
        maximum_bytes=_ADMIN_BODY_LIMIT,
        catalog_import_maximum_bytes=_ADMIN_CATALOG_IMPORT_BODY_LIMIT,
        security=admin_security,
    )
    if settings.admin_origin:
        admin_hostname = urlsplit(settings.admin_origin).hostname
        if not admin_hostname:
            raise ValueError("QUICKPRICE_ADMIN_ORIGIN must contain a valid hostname")
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[admin_hostname],
            www_redirect=False,
        )
    app.state.settings = settings
    app.state.service = service
    app.state.registry = service.registry_view
    app.state.authenticator = authenticator
    app.state.api_key_manager = api_key_manager
    app.state.admin_security = admin_security
    app.state.managed_configuration = managed_configuration
    app.state.managed_provider_keys = managed_provider_keys
    app.state.managed_instruments = managed_instruments
    app.state.dashboard_logs = dashboard_logs
    readiness_cache: tuple[float, bool, dict[str, Any]] | None = None

    def cached_readiness_details() -> tuple[bool, dict[str, Any]]:
        nonlocal readiness_cache
        now = time.monotonic()
        if readiness_cache is None or readiness_cache[0] <= now:
            is_ready, details = service.readiness()
            readiness_cache = (now + 1.0, is_ready, details)
        return readiness_cache[1], readiness_cache[2]

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = _request_id()
        started = time.perf_counter()
        response: JSONResponse | Any | None = None
        path = request.url.path
        if (
            path == "/v1"
            or path.startswith("/v1/")
            or path == "/internal"
            or path.startswith("/internal/")
        ) and response is None:
            client_ip = admin_security.resolve_client_ip(
                request.client.host if request.client else None,
                request.headers.get("X-Real-IP"),
            )
            try:
                request.state.api_key_context = authenticator.authenticate(
                    request.headers.get("X-API-Key"), client_ip
                )
            except RateLimitError as exc:
                response = JSONResponse(
                    _envelope(
                        request,
                        data=None,
                        errors=[ErrorModel(code="rate_limited", message="rate limit exceeded")],
                    ),
                    status_code=429,
                    headers={"Retry-After": str(exc.retry_after)},
                )
            except AuthenticationError:
                response = JSONResponse(
                    _envelope(
                        request,
                        data=None,
                        errors=[
                            ErrorModel(code="unauthorized", message="invalid or missing API key")
                        ],
                    ),
                    status_code=401,
                )
        if response is None:
            response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        route = request.scope.get("route")
        route_template = getattr(route, "path", None)
        if not isinstance(route_template, str):
            route_template = (
                "/v1/{unmatched}" if path == "/v1" or path.startswith("/v1/") else "/{unmatched}"
            )
        service.metrics.observe_request(route_template, response.status_code, elapsed_ms)
        if path != _LOG_STREAM_PATH:
            _LOGGER.info(
                "HTTP %s %s returned %d in %.2f ms request_id=%s",
                request.method,
                route_template,
                response.status_code,
                elapsed_ms,
                request.state.request_id,
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if path == "/admin" or path.startswith("/admin/") or path.startswith("/admin-api/"):
            response.headers["Vary"] = "Origin, Sec-Fetch-Site"
        if settings.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        content = _envelope(
            request,
            data=None,
            errors=[ErrorModel(code=exc.code, message=exc.message, symbol=exc.symbol)],
            partial=False,
        )
        return JSONResponse(content, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        message = "; ".join(error["msg"] for error in exc.errors())
        if request.url.path == "/admin-api" or request.url.path.startswith("/admin-api/"):
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": message}},
                status_code=422,
            )
        content = _envelope(
            request,
            data=None,
            errors=[
                ErrorModel(
                    code="invalid_request",
                    message=message,
                )
            ],
        )
        return JSONResponse(content, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        content = _envelope(
            request,
            data=None,
            errors=[ErrorModel(code=code, message=str(exc.detail))],
        )
        headers = dict(exc.headers or {})
        return JSONResponse(content, status_code=exc.status_code, headers=headers)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        route_template = getattr(request.scope.get("route"), "path", "/{unmatched}")
        _LOGGER.error(
            "Unhandled server error route=%s error_type=%s",
            route_template,
            type(exc).__name__,
        )
        content = _envelope(
            request,
            data=None,
            errors=[ErrorModel(code="internal_error", message="unexpected server error")],
        )
        return JSONResponse(content, status_code=500)

    dashboard_html = (_DASHBOARD_ROOT / "index.html").read_text(encoding="utf-8")
    admin_html = (_ADMIN_ROOT / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=RedirectResponse, include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard", status_code=307)

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/dashboard/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(dashboard_html)

    @app.get("/dashboard/assets/dashboard.css", include_in_schema=False)
    async def dashboard_stylesheet() -> FileResponse:
        return FileResponse(
            _DASHBOARD_ROOT / "assets" / "dashboard.css",
            media_type="text/css",
        )

    @app.get("/dashboard/assets/dashboard.js", include_in_schema=False)
    async def dashboard_script() -> FileResponse:
        return FileResponse(
            _DASHBOARD_ROOT / "assets" / "dashboard.js",
            media_type="text/javascript",
        )

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
    async def admin_dashboard(request: Request) -> Response:
        effective_scheme = request.url.scheme
        peer = request.client.host if request.client else None
        forwarded = request.headers.get("X-Forwarded-Proto")
        if admin_security.trusts_proxy(peer) and forwarded in {"http", "https"}:
            effective_scheme = forwarded
        if admin_security.require_https and effective_scheme.lower() != "https":
            # Do not present a credential form over a transport that would
            # expose the administrator key and one-time code before the login
            # endpoint has an opportunity to reject the request.
            return Response(status_code=404)
        return HTMLResponse(admin_html)

    @app.get("/admin/assets/admin.css", include_in_schema=False)
    async def admin_stylesheet() -> FileResponse:
        return FileResponse(_ADMIN_ROOT / "assets" / "admin.css", media_type="text/css")

    @app.get("/admin/assets/admin.js", include_in_schema=False)
    async def admin_script() -> FileResponse:
        return FileResponse(
            _ADMIN_ROOT / "assets" / "admin.js",
            media_type="text/javascript",
        )

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        is_ready = service.is_ready()
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready"},
            status_code=200 if is_ready else 503,
        )

    @app.get("/internal/readiness", include_in_schema=False)
    async def readiness_details(request: Request) -> dict[str, Any]:
        _, details = cached_readiness_details()
        return _envelope(request, data=details)

    @app.get("/internal/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> dict[str, Any]:
        return _envelope(request, data=service.operational_metrics())

    @app.get("/v1/access", include_in_schema=False)
    async def current_api_key_access(request: Request) -> dict[str, Any]:
        """Return non-secret validity metadata for the authenticated client key."""

        context = request.state.api_key_context
        if not isinstance(context, AuthContext):
            raise APIError(503, "api_key_metadata_unavailable", "API key metadata is unavailable")
        expires_at = context.expires_at
        return _envelope(
            request,
            data={
                "name": context.name,
                "expires_at": (
                    None if expires_at is None else expires_at.isoformat().replace("+00:00", "Z")
                ),
                "is_permanent": expires_at is None,
            },
        )

    @app.get("/internal/dashboard/quotes", include_in_schema=False)
    async def dashboard_quotes(
        request: Request,
        symbols: Annotated[
            str | None,
            Query(min_length=1, description="Comma-separated instrument symbols, maximum 100"),
        ] = None,
    ) -> JSONResponse:
        """Return the best in-memory quote projection for the operations dashboard.

        The public quote API intentionally rejects snapshots missing mandatory
        dividend or yield metadata. The private dashboard still needs to expose
        an existing market price while retaining that exact completeness error,
        so operators can distinguish an absent price from incomplete metadata.
        """

        generation = service.capture_generation()
        active_registry = generation.registry
        if symbols is None:
            requested = list(active_registry.symbols)
        else:
            requested = []
            for raw in symbols.split(","):
                normalized = normalize_symbol(raw)
                instrument = active_registry.resolve(normalized)
                symbol = instrument.symbol if instrument is not None else normalized
                if symbol and symbol not in requested:
                    requested.append(symbol)
            if not requested:
                raise APIError(422, "invalid_request", "symbols cannot be empty")
            if len(requested) > 100:
                raise APIError(
                    422,
                    "invalid_request",
                    "symbols cannot contain more than 100 items",
                )

        data: list[dict[str, Any]] = []
        errors: list[ErrorModel] = []
        now = utc_now()
        for symbol in requested:
            instrument = active_registry.resolve(symbol)
            if instrument is None:
                errors.append(
                    ErrorModel(code="unknown_symbol", message="unsupported symbol", symbol=symbol)
                )
                continue
            try:
                quote = service.get_quote(instrument.symbol, now=now, generation=generation)
            except DataUnavailableError as exc:
                errors.append(
                    ErrorModel(code=exc.code, message=exc.reason, symbol=instrument.symbol)
                )
                try:
                    quote = service.get_quote(
                        instrument.symbol,
                        now=now,
                        require_complete_metadata=False,
                        generation=generation,
                    )
                except DataUnavailableError:
                    continue
            data.append(quote.model_dump(mode="json"))

        return JSONResponse(
            _envelope(
                request,
                data=data,
                errors=errors,
                partial=bool(errors),
                generated_at=now,
            ),
            headers={_CATALOG_REVISION_HEADER: generation.revision},
        )

    @app.get(_LOG_STREAM_PATH, include_in_schema=False)
    async def dashboard_log_stream(request: Request) -> StreamingResponse:
        raw_last_event_id = request.headers.get("Last-Event-ID")
        after_id: int | None = None
        if raw_last_event_id:
            try:
                after_id = int(raw_last_event_id)
            except ValueError as exc:
                raise APIError(
                    400,
                    "invalid_last_event_id",
                    "Last-Event-ID must be a non-negative integer",
                ) from exc
            if after_id < 0:
                raise APIError(
                    400,
                    "invalid_last_event_id",
                    "Last-Event-ID must be a non-negative integer",
                )
        try:
            log_events = dashboard_logs.stream(after_id=after_id)
        except DashboardLogCapacityError as exc:
            raise APIError(
                429,
                "log_stream_limit_reached",
                "dashboard log stream capacity is currently exhausted",
                headers={"Retry-After": "5"},
            ) from exc
        return StreamingResponse(
            log_events,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v1/instruments")
    async def instruments(request: Request) -> Response:
        generation = service.capture_generation()
        etag = f'"{generation.revision}"'
        headers = {
            "Cache-Control": "private, no-cache, max-age=0",
            "ETag": etag,
            _CATALOG_REVISION_HEADER: generation.revision,
        }
        if _if_none_match_matches(request.headers.get("If-None-Match"), etag):
            return Response(status_code=304, headers=headers)
        return JSONResponse(
            _envelope(
                request,
                data=[
                    instrument_to_wire(item).model_dump(mode="json")
                    for item in generation.registry.values()
                ],
            ),
            headers=headers,
        )

    install_admin_routes(
        app,
        security=admin_security,
        api_keys=api_key_manager,
        configuration=managed_configuration,
        provider_keys=managed_provider_keys,
        instruments=managed_instruments,
        service=service,
    )

    @app.get("/v1/quotes")
    async def quotes(
        request: Request,
        symbols: Annotated[
            str | None,
            Query(min_length=1, description="Comma-separated instrument symbols, maximum 100"),
        ] = None,
    ) -> JSONResponse:
        generation = service.capture_generation()
        active_registry = generation.registry
        if symbols is None:
            requested = list(active_registry.symbols)
        else:
            requested = []
            for raw in symbols.split(","):
                normalized = normalize_symbol(raw)
                instrument = active_registry.resolve(normalized)
                symbol = instrument.symbol if instrument is not None else normalized
                if symbol and symbol not in requested:
                    requested.append(symbol)
            if not requested:
                raise APIError(422, "invalid_request", "symbols cannot be empty")
            if len(requested) > 100:
                raise APIError(422, "invalid_request", "symbols cannot contain more than 100 items")
        data: list[dict[str, Any]] = []
        errors: list[ErrorModel] = []
        valid_requested = 0
        now = utc_now()
        for symbol in requested:
            if symbol not in active_registry:
                errors.append(
                    ErrorModel(code="unknown_symbol", message="unsupported symbol", symbol=symbol)
                )
                continue
            valid_requested += 1
            try:
                data.append(
                    service.get_quote(symbol, now=now, generation=generation).model_dump(
                        mode="json"
                    )
                )
            except DataUnavailableError as exc:
                errors.append(ErrorModel(code=exc.code, message=exc.reason, symbol=symbol))
        if not data and valid_requested:
            content = _envelope(request, data=[], errors=errors, partial=True, generated_at=now)
            return JSONResponse(
                content,
                status_code=503,
                headers={_CATALOG_REVISION_HEADER: generation.revision},
            )
        if not data and not valid_requested:
            if symbols is None:
                content = _envelope(
                    request,
                    data=[],
                    errors=[],
                    partial=False,
                    generated_at=now,
                )
                return JSONResponse(
                    content,
                    status_code=200,
                    headers={_CATALOG_REVISION_HEADER: generation.revision},
                )
            content = _envelope(request, data=[], errors=errors, partial=True, generated_at=now)
            return JSONResponse(
                content,
                status_code=400,
                headers={_CATALOG_REVISION_HEADER: generation.revision},
            )
        content = _envelope(
            request,
            data=data,
            errors=errors,
            partial=bool(errors),
            generated_at=now,
        )
        return JSONResponse(
            content,
            status_code=200,
            headers={_CATALOG_REVISION_HEADER: generation.revision},
        )

    @app.get("/v1/quotes/{symbol}")
    async def quote(request: Request, symbol: str) -> JSONResponse:
        generation = service.capture_generation()
        active_registry = generation.registry
        normalized = normalize_symbol(symbol)
        instrument = active_registry.resolve(normalized)
        if instrument is None:
            raise APIError(404, "unknown_symbol", "unsupported symbol", symbol=normalized)
        normalized = instrument.symbol
        now = utc_now()
        try:
            data = service.get_quote(normalized, now=now, generation=generation).model_dump(
                mode="json"
            )
        except DataUnavailableError as exc:
            raise APIError(503, exc.code, exc.reason, symbol=normalized) from exc
        return JSONResponse(
            _envelope(request, data=data, generated_at=now),
            headers={_CATALOG_REVISION_HEADER: generation.revision},
        )

    return app


app = create_app()
