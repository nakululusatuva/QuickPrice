"""Separate, cookie-authenticated administrator control-plane routes."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .admin_security import (
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminNotConfiguredError,
    AdminRateLimitError,
    AdminSecurity,
)
from .api_keys import (
    ApiKeyImport,
    ApiKeyManager,
    ApiKeyManagerError,
    ApiKeyNotFoundError,
    AuditContext,
    DuplicateApiKeyError,
)
from .managed_config import (
    InstrumentPolicyStore,
    ManagedConfigurationError,
    ManagedEnvironmentStore,
    ProviderKeyStore,
    RevisionConflictError,
    UnsupportedSettingError,
)

_LOGGER = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_StrictModel):
    admin_key: str = Field(min_length=1, max_length=256)
    totp: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class CreateApiKeyRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=80)
    expires_at: datetime | None = None


class UpdateApiKeyRequest(_StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    expires_at: datetime | None = None


class ImportApiKeyItem(_StrictModel):
    name: str = Field(min_length=1, max_length=80)
    api_key: str | None = Field(default=None, min_length=20, max_length=256)
    key_hash: str | None = Field(
        default=None,
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9A-Fa-f]{64}$",
    )
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def exactly_one_credential(self) -> ImportApiKeyItem:
        if (self.api_key is None) == (self.key_hash is None):
            raise ValueError("exactly one of api_key or key_hash is required")
        return self


class ImportApiKeysRequest(_StrictModel):
    keys: list[ImportApiKeyItem] = Field(min_length=1, max_length=100)


class RevisionedValuesRequest(_StrictModel):
    revision: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    values: dict[str, Any] = Field(default_factory=dict, max_length=100)


class InstrumentPolicyRequest(_StrictModel):
    revision: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    instruments: list[dict[str, Any]] = Field(max_length=2_000)


class AdminRouteError(RuntimeError):
    def __init__(
        self, status: int, code: str, message: str, *, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def _timestamp_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _client_ip(request: Request, security: AdminSecurity) -> str:
    peer = request.client.host if request.client else None
    return security.resolve_client_ip(peer, request.headers.get("X-Real-IP"))


def _effective_scheme(request: Request, security: AdminSecurity) -> str:
    scheme = request.url.scheme
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("X-Forwarded-Proto")
    if security.trusts_proxy(peer) and forwarded in {"http", "https"}:
        return forwarded
    return scheme


def _audit_context(request: Request, security: AdminSecurity) -> AuditContext:
    return AuditContext(
        request_id=request.state.request_id,
        client_ip=_client_ip(request, security),
    )


def _json_content_required(request: Request) -> None:
    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise AdminRouteError(
            415, "json_required", "administrator mutations require application/json"
        )


def _validate_expiry(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdminRouteError(422, "invalid_expiry", "API key expiry must include a timezone")
    normalized = value.astimezone(UTC)
    if normalized <= datetime.now(UTC):
        raise AdminRouteError(422, "invalid_expiry", "API key expiry must be in the future")
    return normalized


def install_admin_routes(
    app: FastAPI,
    *,
    security: AdminSecurity,
    api_keys: ApiKeyManager,
    configuration: ManagedEnvironmentStore,
    provider_keys: ProviderKeyStore,
    instruments: InstrumentPolicyStore,
    service: Any,
) -> None:
    """Install routes without sharing any quote API authentication state."""

    @app.exception_handler(AdminRouteError)
    async def admin_route_error_handler(_: Request, exc: AdminRouteError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            status_code=exc.status,
            headers=headers,
        )

    def authorize(request: Request, *, mutation: bool = False):
        try:
            if mutation:
                _json_content_required(request)
                security.validate_browser_request(
                    origin=request.headers.get("Origin"),
                    sec_fetch_site=request.headers.get("Sec-Fetch-Site"),
                    effective_scheme=_effective_scheme(request, security),
                    mutation=True,
                )
            return security.authorize(
                session_token=request.cookies.get(security.cookie_name),
                user_agent=request.headers.get("User-Agent", ""),
                csrf_token=request.headers.get("X-CSRF-Token"),
                mutation=mutation,
            )
        except AdminNotConfiguredError as exc:
            raise AdminRouteError(
                503, "admin_not_configured", "administrator access is unavailable"
            ) from exc
        except AdminRateLimitError as exc:
            raise AdminRouteError(
                429,
                "admin_rate_limited",
                "administrator request rate limit exceeded",
                retry_after=exc.retry_after,
            ) from exc
        except AdminAuthorizationError as exc:
            raise AdminRouteError(
                401, "admin_unauthorized", "administrator authorization failed"
            ) from exc

    async def audit_intent(
        request: Request,
        *,
        action: str,
        target_type: str,
        target_id: str | None,
        fields: list[str],
    ) -> None:
        try:
            await api_keys.append_audit(
                audit=_audit_context(request, security),
                action=action,
                target_type=target_type,
                target_id=target_id,
                details={"fields": sorted(set(fields))},
            )
        except Exception as exc:
            raise AdminRouteError(
                503, "audit_unavailable", "durable administrator audit is unavailable"
            ) from exc

    @app.post("/admin-api/session", include_in_schema=False)
    async def admin_login(request: Request, body: LoginRequest) -> JSONResponse:
        _json_content_required(request)
        try:
            security.validate_browser_request(
                origin=request.headers.get("Origin"),
                sec_fetch_site=request.headers.get("Sec-Fetch-Site"),
                effective_scheme=_effective_scheme(request, security),
                mutation=True,
            )
            result = await asyncio.to_thread(
                security.login,
                admin_key=body.admin_key,
                otp=body.totp,
                client_ip=_client_ip(request, security),
                user_agent=request.headers.get("User-Agent", ""),
            )
        except AdminNotConfiguredError as exc:
            raise AdminRouteError(
                503, "admin_not_configured", "administrator access is unavailable"
            ) from exc
        except AdminRateLimitError as exc:
            raise AdminRouteError(
                429,
                "admin_rate_limited",
                "administrator authentication is temporarily unavailable",
                retry_after=exc.retry_after,
            ) from exc
        except (AdminAuthenticationError, AdminAuthorizationError) as exc:
            _LOGGER.warning(
                "Administrator login rejected client_ip=%s request_id=%s reason=%s",
                _client_ip(request, security),
                request.state.request_id,
                type(exc).__name__,
            )
            raise AdminRouteError(
                401, "admin_authentication_failed", "administrator authentication failed"
            ) from exc
        try:
            await api_keys.append_audit(
                audit=_audit_context(request, security),
                action="admin.session.created",
                target_type="admin_session",
                target_id=None,
                details={},
            )
        except Exception as exc:
            security.logout(result.session_token)
            raise AdminRouteError(
                503, "audit_unavailable", "durable administrator audit is unavailable"
            ) from exc
        response = JSONResponse(
            {
                "authenticated": True,
                "csrf_token": result.csrf_token,
                "expires_at": _timestamp_from_epoch(result.expires_at_epoch),
            }
        )
        response.set_cookie(
            key=security.cookie_name,
            value=result.session_token,
            max_age=security.absolute_seconds,
            secure=security.production or security.require_https,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/admin-api/session", include_in_schema=False)
    async def admin_session(request: Request) -> dict[str, Any]:
        session = authorize(request)
        return {
            "authenticated": True,
            "csrf_token": session.csrf_token,
            "expires_at": _timestamp_from_epoch(security.expires_at_epoch(session)),
        }

    @app.delete("/admin-api/session", include_in_schema=False)
    async def admin_logout(request: Request) -> JSONResponse:
        authorize(request, mutation=True)
        token = request.cookies.get(security.cookie_name)
        security.logout(token)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            security.cookie_name,
            path="/",
            secure=security.production or security.require_https,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/admin-api/api-keys", include_in_schema=False)
    async def list_api_keys(request: Request) -> dict[str, Any]:
        authorize(request)
        return {"api_keys": list(api_keys.list_records(include_revoked=True))}

    @app.post("/admin-api/api-keys", include_in_schema=False)
    async def create_api_key(request: Request, body: CreateApiKeyRequest) -> dict[str, Any]:
        authorize(request, mutation=True)
        try:
            record, raw_key = await api_keys.create(
                name=body.name,
                expires_at=_validate_expiry(body.expires_at),
                audit=_audit_context(request, security),
            )
        except DuplicateApiKeyError as exc:
            raise AdminRouteError(409, "duplicate_api_key", "API key already exists") from exc
        except ApiKeyManagerError as exc:
            raise AdminRouteError(
                503, "api_key_store_unavailable", "API key store is unavailable"
            ) from exc
        return {"api_key": record, "raw_key": raw_key, "display_once": True}

    @app.post("/admin-api/api-keys/import", include_in_schema=False)
    async def import_api_keys(request: Request, body: ImportApiKeysRequest) -> dict[str, Any]:
        authorize(request, mutation=True)
        imports = tuple(
            ApiKeyImport(
                name=item.name,
                raw_key=item.api_key,
                key_hash=item.key_hash,
                expires_at=_validate_expiry(item.expires_at),
            )
            for item in body.keys
        )
        try:
            created = await api_keys.import_many(
                imports,
                audit=_audit_context(request, security),
            )
        except DuplicateApiKeyError as exc:
            raise AdminRouteError(
                409, "duplicate_api_key", "one or more API keys already exist"
            ) from exc
        except (ApiKeyManagerError, ValueError) as exc:
            raise AdminRouteError(
                422, "invalid_api_key_import", "API key import is invalid"
            ) from exc
        return {"api_keys": list(created)}

    @app.patch("/admin-api/api-keys/{key_id}", include_in_schema=False)
    async def update_api_key(
        key_id: str, request: Request, body: UpdateApiKeyRequest
    ) -> dict[str, Any]:
        authorize(request, mutation=True)
        current = next((item for item in api_keys.list_records() if item["key_id"] == key_id), None)
        if current is None:
            raise AdminRouteError(404, "api_key_not_found", "API key was not found")
        current_expiry = current["expires_at"]
        expires_at = (
            _validate_expiry(body.expires_at)
            if "expires_at" in body.model_fields_set
            else datetime.fromisoformat(current_expiry.replace("Z", "+00:00"))
            if current_expiry
            else None
        )
        try:
            updated = await api_keys.update(
                key_id,
                name=body.name or current["name"],
                expires_at=expires_at,
                audit=_audit_context(request, security),
            )
        except ApiKeyNotFoundError as exc:
            raise AdminRouteError(404, "api_key_not_found", "API key was not found") from exc
        return {"api_key": updated}

    @app.delete("/admin-api/api-keys/{key_id}", include_in_schema=False)
    async def revoke_api_key(key_id: str, request: Request) -> dict[str, Any]:
        authorize(request, mutation=True)
        try:
            await api_keys.revoke(key_id, audit=_audit_context(request, security))
        except ApiKeyNotFoundError as exc:
            raise AdminRouteError(404, "api_key_not_found", "API key was not found") from exc
        return {"revoked": True, "key_id": key_id}

    @app.get("/admin-api/configuration", include_in_schema=False)
    async def get_configuration(request: Request) -> dict[str, Any]:
        authorize(request)
        return await asyncio.to_thread(configuration.snapshot)

    @app.patch("/admin-api/configuration", include_in_schema=False)
    async def patch_configuration(
        request: Request, body: RevisionedValuesRequest
    ) -> dict[str, Any]:
        authorize(request, mutation=True)
        await audit_intent(
            request,
            action="configuration.change_requested",
            target_type="configuration",
            target_id=None,
            fields=list(body.values),
        )
        updates = {name: value for name, value in body.values.items() if value is not None}
        removals = [name for name, value in body.values.items() if value is None]
        result = await _patch_store(
            configuration, updates=updates, removals=removals, revision=body.revision
        )
        await audit_intent(
            request,
            action="configuration.changed",
            target_type="configuration",
            target_id=result["revision"],
            fields=list(body.values),
        )
        return result

    @app.get("/admin-api/provider-keys", include_in_schema=False)
    async def get_provider_keys(request: Request) -> dict[str, Any]:
        authorize(request)
        return await asyncio.to_thread(provider_keys.snapshot)

    @app.patch("/admin-api/provider-keys", include_in_schema=False)
    async def patch_provider_keys(
        request: Request, body: RevisionedValuesRequest
    ) -> dict[str, Any]:
        authorize(request, mutation=True)
        await audit_intent(
            request,
            action="provider_keys.change_requested",
            target_type="provider_keys",
            target_id=None,
            fields=list(body.values),
        )
        updates = {name: value for name, value in body.values.items() if value is not None}
        removals = [name for name, value in body.values.items() if value is None]
        if any(not isinstance(value, str) for value in updates.values()):
            raise AdminRouteError(
                422, "invalid_provider_key", "provider credentials must be strings"
            )
        result = await _patch_store(
            provider_keys, updates=updates, removals=removals, revision=body.revision
        )
        await audit_intent(
            request,
            action="provider_keys.changed",
            target_type="provider_keys",
            target_id=result["revision"],
            fields=list(body.values),
        )
        return result

    @app.get("/admin-api/instruments", include_in_schema=False)
    async def get_instruments(request: Request) -> dict[str, Any]:
        authorize(request)
        return await asyncio.to_thread(instruments.snapshot)

    @app.patch("/admin-api/instruments", include_in_schema=False)
    async def patch_instruments(request: Request, body: InstrumentPolicyRequest) -> dict[str, Any]:
        authorize(request, mutation=True)
        symbols = [str(item.get("symbol", "")) for item in body.instruments]
        await audit_intent(
            request,
            action="instruments.change_requested",
            target_type="instrument_policy",
            target_id=None,
            fields=symbols,
        )
        try:
            result = await asyncio.to_thread(
                instruments.patch,
                instruments=body.instruments,
                expected_revision=body.revision,
            )
        except RevisionConflictError as exc:
            raise AdminRouteError(
                409, "revision_conflict", "instrument policy changed concurrently"
            ) from exc
        except (ManagedConfigurationError, ValueError) as exc:
            raise AdminRouteError(
                422, "invalid_instrument_policy", "instrument policy is invalid"
            ) from exc
        await audit_intent(
            request,
            action="instruments.changed",
            target_type="instrument_policy",
            target_id=result["revision"],
            fields=symbols,
        )
        return result

    @app.get("/admin-api/provider-statistics", include_in_schema=False)
    async def provider_statistics(request: Request) -> dict[str, Any]:
        authorize(request)
        metrics = service.operational_metrics()
        provider_data = _merge_provider_statistics(metrics)
        return {
            "providers": provider_data,
            "collected_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "quota_updated_at": (
                metrics.get("providers", {}).get("quota_updated_at")
                if isinstance(metrics.get("providers"), dict)
                else None
            ),
            "lifetime_resets_on_restart": True,
            "percentiles_use_bounded_recent_samples": True,
        }

    @app.get("/admin-api/audit-events", include_in_schema=False)
    async def audit_events(request: Request, limit: int = 100) -> dict[str, Any]:
        authorize(request)
        if not 1 <= limit <= 500:
            raise AdminRouteError(
                422, "invalid_limit", "audit event limit must be between 1 and 500"
            )
        return {"events": list(await api_keys.audit_events(limit=limit))}


async def _patch_store(
    store: ManagedEnvironmentStore | ProviderKeyStore,
    *,
    updates: dict[str, Any],
    removals: list[str],
    revision: str,
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            store.patch,
            updates=updates,
            removals=removals,
            expected_revision=revision,
        )
    except RevisionConflictError as exc:
        raise AdminRouteError(
            409, "revision_conflict", "managed configuration changed concurrently"
        ) from exc
    except UnsupportedSettingError as exc:
        raise AdminRouteError(
            422, "unsupported_setting", "one or more settings are not web-manageable"
        ) from exc
    except (ManagedConfigurationError, ValueError) as exc:
        raise AdminRouteError(
            422, "invalid_configuration", "managed configuration is invalid"
        ) from exc


def _merge_provider_statistics(metrics: dict[str, Any]) -> dict[str, Any]:
    telemetry = metrics.get("provider_statistics")
    if not isinstance(telemetry, dict):
        telemetry = {}
    coordinator = metrics.get("providers")
    if not isinstance(coordinator, dict):
        coordinator = {}
    quota = coordinator.get("quota") if isinstance(coordinator.get("quota"), dict) else {}
    fallbacks = (
        coordinator.get("fallback_counts")
        if isinstance(coordinator.get("fallback_counts"), dict)
        else {}
    )
    circuits = coordinator.get("circuits") if isinstance(coordinator.get("circuits"), list) else []
    reconnects = (
        coordinator.get("websocket_reconnects")
        if isinstance(coordinator.get("websocket_reconnects"), dict)
        else {}
    )
    streams = coordinator.get("streams") if isinstance(coordinator.get("streams"), dict) else {}
    names = set(telemetry) | set(quota)
    names.update(str(name) for name in reconnects)
    names.update(str(name) for name in streams)
    names.update(
        str(item.get("provider"))
        for item in circuits
        if isinstance(item, dict) and item.get("provider")
    )
    result: dict[str, Any] = {}
    untracked = {
        "tracked": False,
        "accounting": "untracked",
        "provider_reported": False,
        "limit": None,
        "used": None,
        "remaining": None,
    }
    for name in sorted(names):
        provider_surfaces = telemetry.get(name)
        if not isinstance(provider_surfaces, dict):
            provider_surfaces = {}
        provider_circuits = [
            item for item in circuits if isinstance(item, dict) and item.get("provider") == name
        ]
        states = {str(item.get("state", "unknown")) for item in provider_circuits}
        if "open" in states:
            circuit_state = "open"
        elif "half_open" in states or "probe_ready" in states:
            circuit_state = "half_open"
        elif states:
            circuit_state = "closed"
        else:
            circuit_state = "unobserved"
        fallback_count = sum(
            int(count)
            for key, count in fallbacks.items()
            if isinstance(key, str) and key.rsplit("|", 1)[-1] == name
        )
        common_status = {
            "quota": quota.get(name, untracked),
            "fallbacks": fallback_count,
            "circuit_state": circuit_state,
            "websocket_reconnects": int(reconnects.get(name, 0)),
            "stream": streams.get(name) if isinstance(streams.get(name), dict) else None,
        }
        enriched: dict[str, Any] = {}
        for surface, raw_metric in provider_surfaces.items():
            if not isinstance(raw_metric, dict):
                continue
            enriched[surface] = {
                **raw_metric,
                "quota": quota.get(name, untracked),
                "fallbacks": fallback_count,
                "circuit_state": circuit_state,
            }
        if not enriched:
            empty_latency = {
                "avg": None,
                "p50": None,
                "p95": None,
                "p99": None,
                "max": None,
            }
            empty = {
                "attempts": 0,
                "successful": 0,
                "success_rate": None,
                "outcomes": {},
                "latency_ms": empty_latency,
                "last_attempt_at": None,
            }
            enriched["operations"] = {
                "lifetime": empty,
                "recent": empty,
                "quota": quota.get(name, untracked),
                "fallbacks": fallback_count,
                "circuit_state": circuit_state,
            }
        enriched["status"] = common_status
        result[name] = enriched
    return result


__all__ = ["install_admin_routes"]
