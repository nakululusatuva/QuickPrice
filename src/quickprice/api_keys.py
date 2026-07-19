"""Durable API-key lifecycle management with memory-only request authentication."""

from __future__ import annotations

import asyncio
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .auth import hash_api_key
from .storage import AdminAuditEventRecord, ApiKeyRecord


class ApiKeyManagerError(RuntimeError):
    pass


class ApiKeyNotFoundError(ApiKeyManagerError):
    pass


class DuplicateApiKeyError(ApiKeyManagerError):
    pass


class ApiKeyStoreUnavailableError(ApiKeyManagerError):
    pass


@dataclass(frozen=True, slots=True)
class AuthContext:
    key_id: str
    name: str
    digest: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuditContext:
    request_id: str
    client_ip: str


@dataclass(frozen=True, slots=True)
class ApiKeyImport:
    name: str
    expires_at: datetime | None = None
    raw_key: str | None = None
    key_hash: str | None = None

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name or len(name) > 80:
            raise ValueError("API key name must contain 1 to 80 characters")
        object.__setattr__(self, "name", name)
        if (self.raw_key is None) == (self.key_hash is None):
            raise ValueError("exactly one of raw_key or key_hash is required")
        if self.raw_key is not None and not 20 <= len(self.raw_key) <= 256:
            raise ValueError("imported raw API keys must contain 20 to 256 characters")
        if self.key_hash is not None:
            normalized_hash = self.key_hash.strip().lower()
            if re.fullmatch(r"sha256:[0-9a-f]{64}", normalized_hash) is None:
                raise ValueError(
                    "imported API key hashes must use sha256:<64 hexadecimal characters>"
                )
            object.__setattr__(self, "key_hash", normalized_hash)
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                raise ValueError("API key expiry must include a timezone")
            object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))


class ApiKeyManager:
    """Own durable client keys and publish immutable authentication snapshots."""

    maximum_import_size = 100

    def __init__(self, legacy_hashes: tuple[str, ...] = ()) -> None:
        self._legacy_hashes = tuple(item.strip().lower() for item in legacy_hashes if item.strip())
        self._records: tuple[ApiKeyRecord, ...] = ()
        self._storage: Any = None
        self._write_lock = asyncio.Lock()
        self._started = False

    async def start(self, storage: Any) -> None:
        if storage is None or not getattr(storage, "is_running", False):
            raise ApiKeyStoreUnavailableError("API-key storage is unavailable")
        async with self._write_lock:
            self._storage = storage
            complete = await asyncio.to_thread(storage.api_key_bootstrap_complete)
            if not complete:
                now = datetime.now(UTC)
                legacy = tuple(
                    ApiKeyRecord(
                        key_id=str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"quickprice:legacy-api-key:{digest}",
                            )
                        ),
                        name=f"Legacy API key {index}",
                        key_hash=digest,
                        key_hint=f"legacy-{index}",
                        created_at=now,
                        updated_at=now,
                        origin="legacy",
                    )
                    for index, digest in enumerate(self._legacy_hashes, 1)
                )
                await storage.bootstrap_api_keys(legacy, completed_at=now)
            await self._reload()
            self._started = True

    @property
    def available(self) -> bool:
        return self._started and self._storage is not None

    def configured(self, *, now: datetime | None = None) -> bool:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        return any(self._is_active(record, current) for record in self._records)

    @staticmethod
    def _is_active(record: ApiKeyRecord, now: datetime) -> bool:
        return record.revoked_at is None and (record.expires_at is None or now < record.expires_at)

    def authenticate_digest(
        self,
        candidate: str,
        *,
        now: datetime | None = None,
    ) -> AuthContext | None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        matched: ApiKeyRecord | None = None
        for record in self._records:
            equal = hmac.compare_digest(candidate, record.key_hash)
            if equal and self._is_active(record, current):
                matched = record
        if matched is None:
            return None
        return AuthContext(matched.key_id, matched.name, candidate, matched.expires_at)

    def list_records(self, *, include_revoked: bool = False) -> tuple[dict[str, Any], ...]:
        now = datetime.now(UTC)
        return tuple(
            self._to_public(record, now)
            for record in self._records
            if include_revoked or record.revoked_at is None
        )

    async def create(
        self,
        *,
        name: str,
        expires_at: datetime | None,
        audit: AuditContext,
    ) -> tuple[dict[str, Any], str]:
        raw_key = "qp_" + secrets.token_urlsafe(32)
        record = self._new_record(
            name=name,
            key_hash=hash_api_key(raw_key),
            key_hint=self._key_hint(raw_key),
            expires_at=expires_at,
            origin="generated",
        )
        async with self._write_lock:
            self._require_available()
            self._ensure_unique((record,))
            await self._storage.import_api_keys(
                (record,),
                audit=self._audit(
                    audit,
                    action="api_key.created",
                    target_id=record.key_id,
                    details={"name": record.name, "expires_at": self._timestamp(record.expires_at)},
                ),
            )
            self._records = tuple(
                sorted((*self._records, record), key=lambda item: (item.created_at, item.key_id))
            )
        return self._to_public(record, datetime.now(UTC)), raw_key

    async def import_many(
        self,
        items: tuple[ApiKeyImport, ...],
        *,
        audit: AuditContext,
    ) -> tuple[dict[str, Any], ...]:
        if not items or len(items) > self.maximum_import_size:
            raise ValueError(f"API key import must contain 1 to {self.maximum_import_size} items")
        records = tuple(self._record_from_import(item) for item in items)
        hashes = [item.key_hash for item in records]
        if len(set(hashes)) != len(hashes):
            raise DuplicateApiKeyError("import contains duplicate API keys")
        async with self._write_lock:
            self._require_available()
            self._ensure_unique(records)
            await self._storage.import_api_keys(
                records,
                audit=self._audit(
                    audit,
                    action="api_key.imported",
                    target_id=None,
                    details={"count": len(records), "key_ids": [item.key_id for item in records]},
                ),
            )
            self._records = tuple(
                sorted(
                    (*self._records, *records),
                    key=lambda item: (item.created_at, item.key_id),
                )
            )
        now = datetime.now(UTC)
        return tuple(self._to_public(record, now) for record in records)

    async def update(
        self,
        key_id: str,
        *,
        name: str,
        expires_at: datetime | None,
        audit: AuditContext,
    ) -> dict[str, Any]:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 80:
            raise ValueError("API key name must contain 1 to 80 characters")
        normalized_expiry = self._normalize_expiry(expires_at)
        async with self._write_lock:
            self._require_available()
            existing = self._active_record(key_id)
            now = datetime.now(UTC)
            await self._storage.update_api_key(
                key_id=existing.key_id,
                name=normalized_name,
                expires_at=normalized_expiry,
                updated_at=now,
                audit=self._audit(
                    audit,
                    action="api_key.updated",
                    target_id=existing.key_id,
                    details={
                        "name": normalized_name,
                        "expires_at": self._timestamp(normalized_expiry),
                    },
                ),
            )
            updated = replace(
                existing,
                name=normalized_name,
                expires_at=normalized_expiry,
                updated_at=now,
            )
            self._replace_record(updated)
        return self._to_public(updated, datetime.now(UTC))

    async def revoke(self, key_id: str, *, audit: AuditContext) -> None:
        async with self._write_lock:
            self._require_available()
            existing = self._active_record(key_id)
            now = datetime.now(UTC)
            await self._storage.revoke_api_key(
                key_id=existing.key_id,
                revoked_at=now,
                audit=self._audit(
                    audit,
                    action="api_key.revoked",
                    target_id=existing.key_id,
                    details={"name": existing.name},
                ),
            )
            self._replace_record(replace(existing, revoked_at=now, updated_at=now))

    async def append_audit(
        self,
        *,
        audit: AuditContext,
        action: str,
        target_type: str,
        target_id: str | None,
        details: dict[str, Any],
    ) -> None:
        self._require_available()
        await self._storage.append_admin_audit(
            self._audit(
                audit,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        )

    async def audit_events(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        self._require_available()
        return await asyncio.to_thread(self._storage.load_admin_audit_events, limit=limit)

    async def _reload(self) -> None:
        try:
            records = await asyncio.to_thread(self._storage.load_api_keys)
        except Exception as exc:
            raise ApiKeyStoreUnavailableError("failed to load API keys") from exc
        self._records = tuple(records)

    def _ensure_unique(self, records: tuple[ApiKeyRecord, ...]) -> None:
        existing = {record.key_hash for record in self._records}
        if any(record.key_hash in existing for record in records):
            raise DuplicateApiKeyError("API key already exists")

    def _active_record(self, key_id: str) -> ApiKeyRecord:
        for record in self._records:
            if record.key_id == key_id and record.revoked_at is None:
                return record
        raise ApiKeyNotFoundError("API key not found")

    def _replace_record(self, replacement: ApiKeyRecord) -> None:
        self._records = tuple(
            replacement if record.key_id == replacement.key_id else record
            for record in self._records
        )

    def _require_available(self) -> None:
        if not self.available:
            raise ApiKeyStoreUnavailableError("API-key storage is unavailable")

    @classmethod
    def _record_from_import(cls, item: ApiKeyImport) -> ApiKeyRecord:
        if item.raw_key is not None:
            digest = hash_api_key(item.raw_key)
            hint = cls._key_hint(item.raw_key)
        else:
            digest = str(item.key_hash).strip().lower()
            hint = "Imported hash"
        return cls._new_record(
            name=item.name,
            key_hash=digest,
            key_hint=hint,
            expires_at=item.expires_at,
            origin="imported",
        )

    @classmethod
    def _new_record(
        cls,
        *,
        name: str,
        key_hash: str,
        key_hint: str,
        expires_at: datetime | None,
        origin: str,
    ) -> ApiKeyRecord:
        now = datetime.now(UTC)
        return ApiKeyRecord(
            key_id=str(uuid.uuid7()),
            name=name,
            key_hash=key_hash,
            key_hint=key_hint,
            created_at=now,
            updated_at=now,
            expires_at=cls._normalize_expiry(expires_at),
            origin=origin,
        )

    @staticmethod
    def _normalize_expiry(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("API key expiry must include a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _key_hint(raw_key: str) -> str:
        return f"{raw_key[:10]}...{raw_key[-4:]}"

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat().replace("+00:00", "Z")

    @classmethod
    def _to_public(cls, record: ApiKeyRecord, now: datetime) -> dict[str, Any]:
        if record.revoked_at is not None:
            status = "revoked"
        elif record.expires_at is not None and now >= record.expires_at:
            status = "expired"
        else:
            status = "active"
        return {
            "key_id": record.key_id,
            "name": record.name,
            "key_hint": record.key_hint,
            "created_at": cls._timestamp(record.created_at),
            "updated_at": cls._timestamp(record.updated_at),
            "expires_at": cls._timestamp(record.expires_at),
            "is_permanent": record.expires_at is None,
            "revoked_at": cls._timestamp(record.revoked_at),
            "origin": record.origin,
            "status": status,
        }

    @staticmethod
    def _audit(
        context: AuditContext,
        *,
        action: str,
        target_id: str | None,
        details: dict[str, Any],
        target_type: str = "api_key",
    ) -> AdminAuditEventRecord:
        return AdminAuditEventRecord(
            event_id=str(uuid.uuid7()),
            occurred_at=datetime.now(UTC),
            request_id=context.request_id,
            client_ip=context.client_ip,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )


def expiry_from_ttl(ttl_days: int | None) -> datetime | None:
    if ttl_days is None:
        return None
    if not 1 <= ttl_days <= 3650:
        raise ValueError("ttl_days must be between 1 and 3650")
    return datetime.now(UTC) + timedelta(days=ttl_days)
