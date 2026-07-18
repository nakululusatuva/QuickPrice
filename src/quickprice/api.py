"""FastAPI route layer; all market-data reads are memory-only."""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import AuthenticationError, Authenticator, RateLimitError
from .config import Settings
from .domain import utc_now
from .registry import InstrumentRegistry, build_registry, normalize_symbol
from .schemas import EnvelopeModel, ErrorModel, instrument_to_wire
from .service import DataUnavailableError, QuickPriceService


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


def _request_id() -> str:
    return str(uuid.uuid7())


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


def create_app(
    settings: Settings | None = None,
    service: QuickPriceService | None = None,
    registry: InstrumentRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if service is not None:
        if registry is not None and registry is not service.registry:
            raise ValueError("the API registry must match the service registry")
        registry = service.registry
    else:
        if registry is None:
            registry = build_registry(settings.enabled_plugins)
        service = QuickPriceService(settings, registry)
    authenticator = Authenticator(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="QuickPrice",
        version="1.1.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.registry = registry
    app.state.authenticator = authenticator
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
        ):
            client_ip = request.client.host if request.client else "unknown"
            try:
                authenticator.authenticate(request.headers.get("X-API-Key"), client_ip)
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
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
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
        content = _envelope(
            request,
            data=None,
            errors=[
                ErrorModel(
                    code="invalid_request",
                    message="; ".join(error["msg"] for error in exc.errors()),
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
    async def unhandled_error_handler(request: Request, _: Exception) -> JSONResponse:
        content = _envelope(
            request,
            data=None,
            errors=[ErrorModel(code="internal_error", message="unexpected server error")],
        )
        return JSONResponse(content, status_code=500)

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

    @app.get("/v1/instruments")
    async def instruments(request: Request) -> dict[str, Any]:
        return _envelope(
            request,
            data=[instrument_to_wire(item).model_dump(mode="json") for item in registry.values()],
        )

    @app.get("/v1/quotes")
    async def quotes(
        request: Request,
        symbols: Annotated[
            str,
            Query(min_length=1, description="Comma-separated instrument symbols, maximum 100"),
        ],
    ) -> JSONResponse:
        requested: list[str] = []
        for raw in symbols.split(","):
            normalized = normalize_symbol(raw)
            instrument = registry.resolve(normalized)
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
            if symbol not in registry:
                errors.append(
                    ErrorModel(code="unknown_symbol", message="unsupported symbol", symbol=symbol)
                )
                continue
            valid_requested += 1
            try:
                data.append(service.get_quote(symbol, now=now).model_dump(mode="json"))
            except DataUnavailableError as exc:
                errors.append(ErrorModel(code=exc.code, message=exc.reason, symbol=symbol))
        if not data and valid_requested:
            content = _envelope(request, data=[], errors=errors, partial=True, generated_at=now)
            return JSONResponse(content, status_code=503)
        if not data and not valid_requested:
            content = _envelope(request, data=[], errors=errors, partial=True, generated_at=now)
            return JSONResponse(content, status_code=400)
        content = _envelope(
            request,
            data=data,
            errors=errors,
            partial=bool(errors),
            generated_at=now,
        )
        return JSONResponse(content, status_code=200)

    @app.get("/v1/quotes/{symbol}")
    async def quote(request: Request, symbol: str) -> JSONResponse:
        normalized = normalize_symbol(symbol)
        instrument = registry.resolve(normalized)
        if instrument is None:
            raise APIError(404, "unknown_symbol", "unsupported symbol", symbol=normalized)
        normalized = instrument.symbol
        now = utc_now()
        try:
            data = service.get_quote(normalized, now=now).model_dump(mode="json")
        except DataUnavailableError as exc:
            raise APIError(503, exc.code, exc.reason, symbol=normalized) from exc
        return JSONResponse(_envelope(request, data=data, generated_at=now))

    return app


app = create_app()
