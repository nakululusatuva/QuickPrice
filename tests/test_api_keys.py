from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quickprice.api_keys import ApiKeyImport, ApiKeyManager, AuditContext
from quickprice.auth import hash_api_key
from quickprice.storage import SQLiteStorage


async def test_api_key_expiry_revocation_and_restart_persistence(tmp_path) -> None:
    path = tmp_path / "quickprice.sqlite3"
    legacy_raw = "legacy-key-with-sufficient-entropy"
    storage = SQLiteStorage(path)
    await storage.start()
    manager = ApiKeyManager((hash_api_key(legacy_raw),))
    await manager.start(storage)
    assert manager.authenticate_digest(hash_api_key(legacy_raw)) is not None

    expires_at = datetime.now(UTC) + timedelta(days=7)
    created, raw_key = await manager.create(
        name="Excel desktop",
        expires_at=expires_at,
        audit=AuditContext(request_id="request-create", client_ip="127.0.0.1"),
    )
    assert created["is_permanent"] is False
    assert manager.authenticate_digest(hash_api_key(raw_key), now=expires_at - timedelta(seconds=1))
    assert manager.authenticate_digest(hash_api_key(raw_key), now=expires_at) is None
    await storage.stop()

    restarted_storage = SQLiteStorage(path)
    await restarted_storage.start()
    restarted = ApiKeyManager((hash_api_key("a-new-env-key-must-not-rebootstrap"),))
    await restarted.start(restarted_storage)
    assert restarted.authenticate_digest(hash_api_key(raw_key), now=expires_at - timedelta(days=1))
    assert restarted.authenticate_digest(hash_api_key("a-new-env-key-must-not-rebootstrap")) is None

    await restarted.revoke(
        created["key_id"],
        audit=AuditContext(request_id="request-revoke", client_ip="127.0.0.1"),
    )
    assert (
        restarted.authenticate_digest(hash_api_key(raw_key), now=expires_at - timedelta(days=1))
        is None
    )
    events = await restarted.audit_events()
    assert [event["action"] for event in events[:2]] == [
        "api_key.revoked",
        "api_key.created",
    ]
    await restarted_storage.stop()


def test_imported_api_key_hash_must_be_sha256_hex() -> None:
    with pytest.raises(ValueError, match="sha256"):
        ApiKeyImport(name="Broken import", key_hash="not-a-digest")


async def test_revocation_updates_auth_snapshot_without_a_database_reload(
    tmp_path, monkeypatch
) -> None:
    storage = SQLiteStorage(tmp_path / "quickprice.sqlite3")
    await storage.start()
    manager = ApiKeyManager()
    await manager.start(storage)
    created, raw_key = await manager.create(
        name="Revocation target",
        expires_at=None,
        audit=AuditContext(request_id="request-create", client_ip="127.0.0.1"),
    )
    assert created["is_permanent"] is True
    context = manager.authenticate_digest(hash_api_key(raw_key))
    assert context is not None
    assert context.expires_at is None

    def fail_reload():
        raise RuntimeError("simulated read failure")

    monkeypatch.setattr(storage, "load_api_keys", fail_reload)
    await manager.revoke(
        created["key_id"],
        audit=AuditContext(request_id="request-revoke", client_ip="127.0.0.1"),
    )

    assert manager.authenticate_digest(hash_api_key(raw_key)) is None
    await storage.stop()
