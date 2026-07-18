"""Separate, cookie-authenticated administrator control-plane routes."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
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
from .catalog import MAX_CUSTOM_INSTRUMENTS
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


class CatalogRevisionRequest(_StrictModel):
    revision: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class CreateCatalogInstrumentRequest(CatalogRevisionRequest):
    instrument: dict[str, Any]


class UpdateCatalogInstrumentRequest(CatalogRevisionRequest):
    changes: dict[str, Any]


class ImportInstrumentCatalogRequest(CatalogRevisionRequest):
    mode: Literal["merge", "replace_custom", "replace-custom"] = "merge"
    catalog: dict[str, Any] | list[dict[str, Any]]


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

    def authorize(
        request: Request,
        *,
        mutation: bool = False,
        same_origin_action: bool = False,
    ):
        preauthorized = getattr(request.state, "quickprice_admin_session", None)
        authorization_level = getattr(
            request.state,
            "quickprice_admin_authorization_level",
            None,
        )
        accepted_levels = (
            {"mutation"}
            if mutation
            else {"mutation", "same_origin_action"}
            if same_origin_action
            else {"read", "mutation", "same_origin_action"}
        )
        if preauthorized is not None and authorization_level in accepted_levels:
            if mutation:
                _json_content_required(request)
            return preauthorized
        try:
            if mutation:
                _json_content_required(request)
                security.validate_browser_request(
                    origin=request.headers.get("Origin"),
                    sec_fetch_site=request.headers.get("Sec-Fetch-Site"),
                    effective_scheme=_effective_scheme(request, security),
                    mutation=True,
                )
            elif same_origin_action:
                security.validate_same_origin_action(
                    origin=request.headers.get("Origin"),
                    sec_fetch_site=request.headers.get("Sec-Fetch-Site"),
                    effective_scheme=_effective_scheme(request, security),
                )
            return security.authorize(
                session_token=request.cookies.get(security.cookie_name),
                user_agent=request.headers.get("User-Agent", ""),
                csrf_token=request.headers.get("X-CSRF-Token"),
                mutation=mutation or same_origin_action,
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

    async def audit_completion(
        request: Request,
        *,
        action: str,
        target_type: str,
        target_id: str | None,
        fields: list[str],
    ) -> None:
        """Record an outcome without changing an already committed response."""

        try:
            await audit_intent(
                request,
                action=action,
                target_type=target_type,
                target_id=target_id,
                fields=fields,
            )
        except AdminRouteError as exc:
            cause = exc.__cause__ or exc
            _LOGGER.error(
                "Administrator completion audit unavailable action=%s error_type=%s",
                action,
                type(cause).__name__,
            )

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
        await audit_completion(
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
        await audit_completion(
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
                instruments.stage_patch,
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
        await audit_completion(
            request,
            action="instruments.changed",
            target_type="instrument_policy",
            target_id=result["revision"],
            fields=symbols,
        )
        return result

    @app.get("/admin-api/instrument-catalog", include_in_schema=False)
    async def get_instrument_catalog(request: Request) -> dict[str, Any]:
        authorize(request)
        result = await _invoke_catalog_hook(instruments, ("catalog_snapshot", "snapshot"))
        return _response_mapping(result, code="instrument_catalog_unavailable")

    @app.post(
        "/admin-api/instrument-catalog/instruments",
        include_in_schema=False,
        status_code=201,
    )
    async def create_catalog_instrument(
        request: Request, body: CreateCatalogInstrumentRequest
    ) -> dict[str, Any]:
        authorize(request, mutation=True)
        instrument_id = _manifest_identifier(body.instrument)
        await audit_intent(
            request,
            action="instrument_catalog.create_requested",
            target_type="instrument",
            target_id=instrument_id,
            fields=sorted(body.instrument),
        )
        try:
            _validate_managed_manifest(body.instrument)
            result = await _invoke_catalog_hook(
                instruments,
                ("create_instrument", "create"),
                instrument=body.instrument,
                expected_revision=body.revision,
            )
        except Exception as exc:
            await _audit_catalog_failure(
                audit_intent,
                request,
                action="instrument_catalog.create_failed",
                target_type="instrument",
                target_id=instrument_id,
            )
            raise _catalog_route_error(exc) from exc
        response = _response_mapping(result, code="instrument_catalog_unavailable")
        await audit_completion(
            request,
            action="instrument_catalog.created",
            target_type="instrument",
            target_id=instrument_id,
            fields=sorted(body.instrument),
        )
        return response

    @app.patch(
        "/admin-api/instrument-catalog/instruments/{instrument_id}",
        include_in_schema=False,
    )
    async def update_catalog_instrument(
        instrument_id: str,
        request: Request,
        body: UpdateCatalogInstrumentRequest,
    ) -> dict[str, Any]:
        authorize(request, mutation=True)
        _validate_catalog_identifier(instrument_id)
        await audit_intent(
            request,
            action="instrument_catalog.update_requested",
            target_type="instrument",
            target_id=instrument_id,
            fields=sorted(body.changes),
        )
        try:
            _validate_managed_manifest(body.changes)
            result = await _invoke_catalog_hook(
                instruments,
                ("update_instrument", "patch_instrument", "update"),
                instrument_id=instrument_id,
                changes=body.changes,
                expected_revision=body.revision,
            )
        except Exception as exc:
            await _audit_catalog_failure(
                audit_intent,
                request,
                action="instrument_catalog.update_failed",
                target_type="instrument",
                target_id=instrument_id,
            )
            raise _catalog_route_error(exc) from exc
        response = _response_mapping(result, code="instrument_catalog_unavailable")
        await audit_completion(
            request,
            action="instrument_catalog.updated",
            target_type="instrument",
            target_id=instrument_id,
            fields=sorted(body.changes),
        )
        return response

    @app.delete(
        "/admin-api/instrument-catalog/instruments/{instrument_id}",
        include_in_schema=False,
    )
    async def archive_catalog_instrument(
        instrument_id: str,
        request: Request,
        body: CatalogRevisionRequest,
    ) -> dict[str, Any]:
        authorize(request, mutation=True)
        _validate_catalog_identifier(instrument_id)
        await audit_intent(
            request,
            action="instrument_catalog.archive_requested",
            target_type="instrument",
            target_id=instrument_id,
            fields=["archived"],
        )
        try:
            result = await _invoke_catalog_hook(
                instruments,
                ("archive_instrument", "delete_instrument", "archive"),
                instrument_id=instrument_id,
                expected_revision=body.revision,
            )
        except Exception as exc:
            await _audit_catalog_failure(
                audit_intent,
                request,
                action="instrument_catalog.archive_failed",
                target_type="instrument",
                target_id=instrument_id,
            )
            raise _catalog_route_error(exc) from exc
        response = _response_mapping(result, code="instrument_catalog_unavailable")
        await audit_completion(
            request,
            action="instrument_catalog.archived",
            target_type="instrument",
            target_id=instrument_id,
            fields=["archived"],
        )
        return response

    @app.post("/admin-api/instrument-catalog/import", include_in_schema=False)
    async def import_instrument_catalog(
        request: Request, body: ImportInstrumentCatalogRequest
    ) -> dict[str, Any]:
        authorize(request, mutation=True)
        await audit_intent(
            request,
            action="instrument_catalog.import_requested",
            target_type="instrument_catalog",
            target_id=None,
            fields=["catalog", "mode"],
        )
        try:
            _validate_managed_manifest(body.catalog)
            import_payload = (
                {"version": 2, "instruments": body.catalog}
                if isinstance(body.catalog, list)
                else body.catalog
            )
            result = await _invoke_catalog_hook(
                instruments,
                ("import_catalog", "import_manifest"),
                catalog=import_payload,
                mode=body.mode,
                expected_revision=body.revision,
            )
        except Exception as exc:
            await _audit_catalog_failure(
                audit_intent,
                request,
                action="instrument_catalog.import_failed",
                target_type="instrument_catalog",
                target_id=None,
            )
            raise _catalog_route_error(exc) from exc
        response = _response_mapping(result, code="instrument_catalog_unavailable")
        await audit_completion(
            request,
            action="instrument_catalog.imported",
            target_type="instrument_catalog",
            target_id=str(response.get("staged_revision") or response.get("revision") or ""),
            fields=["catalog", "mode"],
        )
        return response

    @app.get("/admin-api/instrument-catalog/export", include_in_schema=False)
    async def export_instrument_catalog(
        request: Request,
        state: Literal["active", "staged"] = Query(default="active"),
    ) -> dict[str, Any]:
        authorize(request)
        try:
            result = await _invoke_catalog_hook(
                instruments,
                ("export_catalog", "export_manifest", "snapshot"),
                state=state,
            )
        except Exception as exc:
            raise _catalog_route_error(exc) from exc
        return _response_mapping(result, code="instrument_catalog_unavailable")

    @app.post("/admin-api/instrument-catalog/validate", include_in_schema=False)
    async def validate_instrument_catalog(
        request: Request, body: CatalogRevisionRequest
    ) -> dict[str, Any]:
        authorize(request, mutation=True)
        await audit_intent(
            request,
            action="instrument_catalog.validation_requested",
            target_type="instrument_catalog",
            target_id=body.revision,
            fields=["revision"],
        )
        try:
            catalog_before = await _invoke_catalog_hook(
                instruments,
                ("catalog_snapshot", "snapshot"),
            )
            validation_target = (
                service
                if callable(getattr(service, "validate_instrument_catalog", None))
                else instruments
            )
            result = await _invoke_catalog_hook(
                validation_target,
                (
                    "validate_instrument_catalog",
                    "validate",
                    "validate_catalog",
                    "validate_staged",
                ),
                expected_revision=body.revision,
            )
        except Exception as exc:
            await _audit_catalog_failure(
                audit_intent,
                request,
                action="instrument_catalog.validation_failed",
                target_type="instrument_catalog",
                target_id=body.revision,
            )
            raise _catalog_route_error(exc) from exc
        response = _response_mapping(result, code="instrument_catalog_unavailable")
        response.setdefault("diff", _catalog_staged_diff(catalog_before))
        await audit_completion(
            request,
            action="instrument_catalog.validated",
            target_type="instrument_catalog",
            target_id=body.revision,
            fields=["revision"],
        )
        return response

    @app.post("/admin-api/instrument-catalog/activate", include_in_schema=False)
    async def activate_instrument_catalog(
        request: Request, body: CatalogRevisionRequest
    ) -> JSONResponse:
        authorize(request, mutation=True)
        await audit_intent(
            request,
            action="instrument_catalog.activation_requested",
            target_type="instrument_catalog",
            target_id=body.revision,
            fields=["revision"],
        )
        try:
            result = await _invoke_catalog_hook(
                service,
                ("activate_instrument_catalog",),
                expected_revision=body.revision,
                audit=_audit_context(request, security),
            )
        except Exception as exc:
            await _audit_catalog_failure(
                audit_intent,
                request,
                action="instrument_catalog.activation_failed",
                target_type="instrument_catalog",
                target_id=body.revision,
            )
            raise _catalog_route_error(exc) from exc
        response = _response_mapping(result, code="instrument_activation_unavailable")
        await audit_completion(
            request,
            action="instrument_catalog.activation_started",
            target_type="instrument_catalog_job",
            target_id=str(response.get("job_id") or body.revision),
            fields=["revision"],
        )
        return JSONResponse(response, status_code=202)

    @app.post("/admin-api/instrument-catalog/rollback", include_in_schema=False)
    async def rollback_instrument_catalog(
        request: Request, body: CatalogRevisionRequest
    ) -> JSONResponse:
        authorize(request, mutation=True)
        await audit_intent(
            request,
            action="instrument_catalog.rollback_requested",
            target_type="instrument_catalog",
            target_id=body.revision,
            fields=["revision"],
        )
        try:
            result = await _invoke_catalog_hook(
                service,
                ("rollback_instrument_catalog",),
                expected_revision=body.revision,
                audit=_audit_context(request, security),
            )
        except Exception as exc:
            await _audit_catalog_failure(
                audit_intent,
                request,
                action="instrument_catalog.rollback_failed",
                target_type="instrument_catalog",
                target_id=body.revision,
            )
            raise _catalog_route_error(exc) from exc
        response = _response_mapping(result, code="instrument_activation_unavailable")
        await audit_completion(
            request,
            action="instrument_catalog.rollback_started",
            target_type="instrument_catalog_job",
            target_id=str(response.get("job_id") or body.revision),
            fields=["revision"],
        )
        return JSONResponse(response, status_code=202)

    @app.get("/admin-api/instrument-catalog/jobs/{job_id}", include_in_schema=False)
    async def instrument_catalog_job(job_id: str, request: Request) -> dict[str, Any]:
        authorize(request)
        _validate_catalog_identifier(job_id)
        try:
            result = await _invoke_catalog_hook(
                service,
                ("instrument_catalog_job", "get_instrument_catalog_job"),
                job_id=job_id,
            )
        except Exception as exc:
            raise _catalog_route_error(exc) from exc
        if result is None:
            raise AdminRouteError(404, "activation_job_not_found", "activation job was not found")
        return _response_mapping(result, code="instrument_activation_unavailable")

    @app.get("/admin-api/provider-catalog", include_in_schema=False)
    async def get_provider_catalog(request: Request) -> dict[str, Any]:
        authorize(request)
        try:
            result = await _provider_catalog_snapshot(service)
        except Exception as exc:
            raise _catalog_route_error(exc, provider=True) from exc
        return _provider_catalog_response(result)

    @app.get("/admin-api/provider-catalog/{provider}/search", include_in_schema=False)
    async def search_provider_catalog(
        provider: str,
        request: Request,
        q: str = Query(min_length=1, max_length=128),
        asset_class: str | None = Query(default=None, min_length=1, max_length=32),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> dict[str, Any]:
        authorize(request, same_origin_action=True)
        _validate_provider_name(provider)
        query = q.strip()
        if not query or any(character in query for character in ("\x00", "\r", "\n")):
            raise AdminRouteError(422, "invalid_provider_query", "provider search query is invalid")
        try:
            result = await _search_provider_catalog(
                service,
                provider=provider,
                query=query,
                asset_class=asset_class,
                limit=limit,
            )
        except Exception as exc:
            raise _catalog_route_error(exc, provider=True) from exc
        return _response_mapping(result, code="provider_search_unavailable")

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


_CATALOG_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BLOCKED_MANIFEST_KEYS = {
    "api_key",
    "api_keys",
    "authorization",
    "base_url",
    "code",
    "command",
    "credential",
    "credentials",
    "endpoint",
    "headers",
    "import",
    "import_path",
    "module",
    "module_path",
    "password",
    "python",
    "request_headers",
    "script",
    "secret",
    "secrets",
    "token",
    "url",
    "urls",
}
_BLOCKED_RESPONSE_KEYS = {
    "api_key",
    "authorization",
    "auth_url",
    "authentication_url",
    "credentials",
    "headers",
    "password",
    "raw_response",
    "response_body",
    "secret",
    "token",
}
_MAX_MANIFEST_DEPTH = 12
# A complete generation can contain the 2,000 custom definitions promised by
# the catalog contract plus the installed built-ins. 256 nodes per definition
# comfortably covers the bounded routes, aliases, bindings, income policy, and
# synthetic recipe while the independent 8 MiB request cap remains authoritative.
_MAX_MANIFEST_LIST_ITEMS = MAX_CUSTOM_INSTRUMENTS + 256
_MAX_MANIFEST_NODES = _MAX_MANIFEST_LIST_ITEMS * 256
_HOOK_PARAMETER_ALIASES = {
    "catalog": ("payload", "manifest"),
    "changes": ("updates", "patch"),
    "expected_revision": ("revision",),
    "instrument": ("definition", "item"),
    "instrument_id": ("id",),
    "query": ("q",),
}


async def _invoke_catalog_hook(
    target: Any,
    names: tuple[str, ...],
    **values: Any,
) -> Any:
    hook = next(
        (getattr(target, name, None) for name in names if callable(getattr(target, name, None))),
        None,
    )
    if hook is None:
        raise AdminRouteError(
            503,
            "catalog_runtime_unavailable",
            "instrument catalog runtime is unavailable",
        )
    signature = inspect.signature(hook)
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    arguments: dict[str, Any] = {}
    for name, value in values.items():
        if accepts_kwargs or name in signature.parameters:
            arguments[name] = value
            continue
        alias = next(
            (
                candidate
                for candidate in _HOOK_PARAMETER_ALIASES.get(name, ())
                if candidate in signature.parameters
            ),
            None,
        )
        if alias is not None:
            arguments[alias] = value
    if inspect.iscoroutinefunction(hook):
        return await hook(**arguments)
    result = await asyncio.to_thread(hook, **arguments)
    if inspect.isawaitable(result):
        return await result
    return result


def _response_mapping(value: Any, *, code: str) -> dict[str, Any]:
    converted = _safe_admin_value(value)
    if not isinstance(converted, dict):
        raise AdminRouteError(503, code, "administrator data source returned an invalid response")
    return converted


def _safe_admin_value(value: Any) -> Any:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        value = value.model_dump(mode="json")
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Enum):
        return _safe_admin_value(value.value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {
            str(key): _safe_admin_value(item)
            for key, item in value.items()
            if str(key).lower() not in _BLOCKED_RESPONSE_KEYS
        }
    if isinstance(value, tuple | list | set | frozenset):
        return [_safe_admin_value(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _validate_managed_manifest(value: Any) -> None:
    nodes = 0
    pending: list[tuple[Any, int]] = [(value, 0)]

    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_MANIFEST_NODES or depth > _MAX_MANIFEST_DEPTH:
            raise AdminRouteError(
                422,
                "invalid_instrument_catalog",
                "instrument catalog exceeds structural limits",
            )
        if isinstance(item, dict):
            if len(item) > 256:
                raise AdminRouteError(
                    422,
                    "invalid_instrument_catalog",
                    "instrument catalog object is too large",
                )
            for key, nested in item.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise AdminRouteError(
                        422,
                        "invalid_instrument_catalog",
                        "instrument catalog contains an invalid field name",
                    )
                if key.lower() in _BLOCKED_MANIFEST_KEYS:
                    raise AdminRouteError(
                        422,
                        "unsafe_instrument_catalog",
                        "instrument catalog contains a prohibited field",
                    )
                pending.append((nested, depth + 1))
            continue
        if isinstance(item, list):
            if len(item) > _MAX_MANIFEST_LIST_ITEMS:
                raise AdminRouteError(
                    422,
                    "invalid_instrument_catalog",
                    "instrument catalog list is too large",
                )
            for nested in item:
                pending.append((nested, depth + 1))
            continue
        if isinstance(item, str):
            lowered = item.lower()
            if (
                len(item) > 4_096
                or any(character in item for character in ("\x00", "\r", "\n", "<", ">"))
                or any(
                    scheme in lowered
                    for scheme in ("http://", "https://", "file://", "javascript:")
                )
            ):
                raise AdminRouteError(
                    422,
                    "unsafe_instrument_catalog",
                    "instrument catalog contains a prohibited value",
                )
            continue
        if item is None or isinstance(item, int | float | bool):
            continue
        raise AdminRouteError(
            422,
            "invalid_instrument_catalog",
            "instrument catalog contains an unsupported value",
        )


def _validate_catalog_identifier(value: str) -> None:
    if not _CATALOG_IDENTIFIER.fullmatch(value):
        raise AdminRouteError(422, "invalid_catalog_identifier", "catalog identifier is invalid")


def _validate_provider_name(value: str) -> None:
    if not _PROVIDER_NAME.fullmatch(value):
        raise AdminRouteError(404, "provider_not_found", "provider was not found")


def _manifest_identifier(value: dict[str, Any]) -> str | None:
    candidate = value.get("id") or value.get("symbol")
    if not isinstance(candidate, str) or not _CATALOG_IDENTIFIER.fullmatch(candidate):
        return None
    return candidate


def _catalog_staged_diff(snapshot: Any) -> dict[str, Any]:
    """Return a bounded, secret-free active-to-staged summary for validation UX."""

    if not isinstance(snapshot, Mapping):
        return {"available": False}
    active = snapshot.get("active")
    staged = snapshot.get("staged")
    if not isinstance(active, Mapping) or not isinstance(staged, Mapping):
        return {"available": False}
    active_items = active.get("instruments")
    staged_items = staged.get("instruments")
    if not isinstance(active_items, list) or not isinstance(staged_items, list):
        return {"available": False}

    def by_id(items: list[Any]) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            identifier = item.get("id")
            if isinstance(identifier, str) and identifier:
                result[identifier] = item
        return result

    active_by_id = by_id(active_items)
    staged_by_id = by_id(staged_items)
    added_ids = staged_by_id.keys() - active_by_id.keys()
    removed_ids = active_by_id.keys() - staged_by_id.keys()
    common_ids = active_by_id.keys() & staged_by_id.keys()
    categories: dict[str, list[str]] = {
        "added": [],
        "removed": [],
        "archived": [],
        "restored": [],
        "enabled": [],
        "disabled": [],
        "modified": [],
    }
    changed_ids = set(added_ids) | set(removed_ids)
    categories["added"] = [str(staged_by_id[item].get("symbol", item)) for item in added_ids]
    categories["removed"] = [str(active_by_id[item].get("symbol", item)) for item in removed_ids]
    state_fields = {"enabled", "archived"}
    for identifier in common_ids:
        before = active_by_id[identifier]
        after = staged_by_id[identifier]
        symbol = str(after.get("symbol") or before.get("symbol") or identifier)
        before_archived = before.get("archived") is True
        after_archived = after.get("archived") is True
        before_enabled = before.get("enabled") is not False and not before_archived
        after_enabled = after.get("enabled") is not False and not after_archived
        if not before_archived and after_archived:
            categories["archived"].append(symbol)
        elif before_archived and not after_archived:
            categories["restored"].append(symbol)
        if not before_enabled and after_enabled:
            categories["enabled"].append(symbol)
        elif before_enabled and not after_enabled and not after_archived:
            categories["disabled"].append(symbol)
        before_core = {key: value for key, value in before.items() if key not in state_fields}
        after_core = {key: value for key, value in after.items() if key not in state_fields}
        if before_core != after_core:
            categories["modified"].append(symbol)
        if before != after:
            changed_ids.add(identifier)
    for values in categories.values():
        values.sort()
    return {
        "available": True,
        "changed_count": len(changed_ids),
        "unchanged_count": max(0, len(common_ids) - len(changed_ids & set(common_ids))),
        **categories,
    }


def _catalog_route_error(exc: Exception, *, provider: bool = False) -> AdminRouteError:
    if isinstance(exc, AdminRouteError):
        return exc
    name = type(exc).__name__.lower()
    if "catalogjobnotfound" in name or "activationjobnotfound" in name:
        return AdminRouteError(
            404,
            "activation_job_not_found",
            "activation job was not found",
        )
    if isinstance(exc, RevisionConflictError) or "revisionconflict" in name:
        return AdminRouteError(409, "revision_conflict", "instrument catalog changed concurrently")
    if provider and (getattr(exc, "status", None) == 429 or "ratelimit" in name):
        raw_retry_after = getattr(exc, "retry_after", None)
        retry_after = (
            raw_retry_after if isinstance(raw_retry_after, int) and raw_retry_after > 0 else None
        )
        return AdminRouteError(
            429,
            "provider_rate_limited",
            "provider search rate limit exceeded",
            retry_after=retry_after,
        )
    if isinstance(exc, LookupError) or "notfound" in name or "unknowninstrument" in name:
        code = "provider_not_found" if provider else "instrument_not_found"
        message = "provider was not found" if provider else "instrument was not found"
        return AdminRouteError(404, code, message)
    if "busy" in name or "inprogress" in name or "alreadyrunning" in name:
        return AdminRouteError(
            409, "activation_in_progress", "an activation job is already running"
        )
    if "catalogruntime" in name:
        return AdminRouteError(
            409,
            "invalid_catalog_state",
            "instrument catalog is not in a valid state for this operation",
        )
    if (
        isinstance(exc, ManagedConfigurationError | ValueError)
        or "validation" in name
        or "routecompile" in name
    ):
        code = "invalid_provider_request" if provider else "invalid_instrument_catalog"
        message = "provider request is invalid" if provider else "instrument catalog is invalid"
        return AdminRouteError(422, code, message)
    _LOGGER.error("Administrator catalog operation failed error_type=%s", type(exc).__name__)
    code = "provider_catalog_unavailable" if provider else "instrument_catalog_unavailable"
    message = "provider catalog is unavailable" if provider else "instrument catalog is unavailable"
    return AdminRouteError(503, code, message)


async def _audit_catalog_failure(
    audit: Any,
    request: Request,
    *,
    action: str,
    target_type: str,
    target_id: str | None,
) -> None:
    await audit(
        request,
        action=action,
        target_type=target_type,
        target_id=target_id,
        fields=["error_type"],
    )


async def _provider_catalog_snapshot(service: Any) -> Any:
    if callable(getattr(service, "provider_catalog_snapshot", None)):
        return await _invoke_catalog_hook(service, ("provider_catalog_snapshot",))
    try:
        from .providers.descriptors import provider_catalog_snapshot
    except ImportError as exc:
        raise AdminRouteError(
            503, "provider_catalog_unavailable", "provider catalog is unavailable"
        ) from exc
    return provider_catalog_snapshot(getattr(service, "settings", None))


def _provider_catalog_response(value: Any) -> dict[str, Any]:
    converted = _safe_admin_value(value)
    if isinstance(converted, list):
        return {
            "providers": converted,
            "fixed_endpoints_only": True,
            "custom_providers_allowed": False,
        }
    if isinstance(converted, dict):
        converted.setdefault("fixed_endpoints_only", True)
        converted.setdefault("custom_providers_allowed", False)
        return converted
    raise AdminRouteError(
        503,
        "provider_catalog_unavailable",
        "provider catalog returned an invalid response",
    )


async def _search_provider_catalog(
    service: Any,
    *,
    provider: str,
    query: str,
    asset_class: str | None,
    limit: int,
) -> Any:
    try:
        from .providers.descriptors import get_provider_descriptor

        get_provider_descriptor(provider)
    except ImportError as exc:
        raise AdminRouteError(
            503, "provider_catalog_unavailable", "provider catalog is unavailable"
        ) from exc
    except ValueError as exc:
        raise AdminRouteError(404, "provider_not_found", "provider was not found") from exc
    if callable(getattr(service, "search_provider_symbols", None)):
        return await _invoke_catalog_hook(
            service,
            ("search_provider_symbols",),
            provider=provider,
            query=query,
            asset_class=asset_class,
            limit=limit,
        )
    try:
        from .providers.descriptors import (
            search_provider_symbols,
        )
    except ImportError as exc:
        raise AdminRouteError(
            503, "provider_catalog_unavailable", "provider catalog is unavailable"
        ) from exc
    return await search_provider_symbols(
        getattr(service, "settings", None),
        provider,
        query,
        asset_class=asset_class,
        limit=limit,
    )


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
