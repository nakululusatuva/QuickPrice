from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from quickprice.admin_security import (
    create_admin_key_verifier,
    generate_admin_key,
    generate_totp_secret,
    totp_code,
)
from quickprice.api import create_app
from quickprice.config import Settings
from quickprice.service import QuickPriceService
from tests.helpers import API_KEY, seed_complete


def _admin_client(settings: Settings, tmp_path):
    raw_admin_key = generate_admin_key()
    totp_secret = generate_totp_secret()
    configured = replace(
        settings,
        admin_key_verifier=create_admin_key_verifier(raw_admin_key),
        admin_totp_secret=totp_secret,
        admin_origin="http://testserver",
        admin_require_https=False,
        managed_config_path=tmp_path / "managed" / "quickprice.env",
        managed_provider_keys_path=tmp_path / "managed" / "provider-keys.env",
        managed_instruments_path=tmp_path / "managed" / "instruments.json",
    )
    service = QuickPriceService(configured)
    seed_complete(service)
    return TestClient(create_app(configured, service)), raw_admin_key, totp_secret


def _login(client: TestClient, admin_key: str, secret: str) -> str:
    response = client.post(
        "/admin-api/session",
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        json={"admin_key": admin_key, "totp": totp_code(secret)},
    )
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert admin_key not in response.text
    return response.json()["csrf_token"]


def _mutation_headers(csrf: str, *, origin: str = "http://testserver") -> dict[str, str]:
    return {
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": csrf,
        "Content-Type": "application/json",
    }


def test_client_api_key_cannot_authorize_admin_and_admin_session_is_csrf_protected(
    settings, tmp_path
) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        ordinary = client.get("/admin-api/api-keys", headers={"X-API-Key": API_KEY})
        assert ordinary.status_code == 401

        csrf = _login(client, admin_key, secret)
        assert client.get("/admin-api/api-keys").status_code == 200
        missing_csrf = client.post(
            "/admin-api/api-keys",
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
            json={"name": "Excel"},
        )
        assert missing_csrf.status_code == 401
        sibling = client.post(
            "/admin-api/api-keys",
            headers=_mutation_headers(csrf, origin="http://evil.testserver"),
            json={"name": "Excel"},
        )
        assert sibling.status_code == 401


def test_admin_can_create_expire_and_revoke_client_api_keys(settings, tmp_path) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        csrf = _login(client, admin_key, secret)
        expires_at = datetime.now(UTC) + timedelta(days=30)
        created = client.post(
            "/admin-api/api-keys",
            headers=_mutation_headers(csrf),
            json={"name": "Excel desktop", "expires_at": expires_at.isoformat()},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        raw_key = body["raw_key"]
        key_id = body["api_key"]["key_id"]
        assert body["display_once"] is True
        assert client.get("/v1/quotes/BTC:USDC", headers={"X-API-Key": raw_key}).status_code == 200

        listing = client.get("/admin-api/api-keys").json()
        assert raw_key not in repr(listing)
        assert any(item["key_id"] == key_id for item in listing["api_keys"])

        renamed = client.patch(
            f"/admin-api/api-keys/{key_id}",
            headers=_mutation_headers(csrf),
            json={"name": "Excel desktop renamed"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["api_key"]["expires_at"] == body["api_key"]["expires_at"]

        expiry_removed = client.patch(
            f"/admin-api/api-keys/{key_id}",
            headers=_mutation_headers(csrf),
            json={"expires_at": None},
        )
        assert expiry_removed.status_code == 200, expiry_removed.text
        assert expiry_removed.json()["api_key"]["expires_at"] is None

        revoked = client.request(
            "DELETE",
            f"/admin-api/api-keys/{key_id}",
            headers=_mutation_headers(csrf),
            content=b"{}",
        )
        assert revoked.status_code == 200
        assert client.get("/v1/quotes/BTC:USDC", headers={"X-API-Key": raw_key}).status_code == 401


def test_provider_key_and_runtime_configuration_responses_never_echo_secrets(
    settings, tmp_path
) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        csrf = _login(client, admin_key, secret)
        provider_snapshot = client.get("/admin-api/provider-keys").json()
        provider_secret = "provider-secret-that-must-never-return"
        provider_update = client.patch(
            "/admin-api/provider-keys",
            headers=_mutation_headers(csrf),
            json={
                "revision": provider_snapshot["revision"],
                "values": {"QUICKPRICE_FINNHUB_API_KEY": provider_secret},
            },
        )
        assert provider_update.status_code == 200, provider_update.text
        assert provider_secret not in provider_update.text
        assert provider_secret not in client.get("/admin-api/provider-keys").text

        configuration = client.get("/admin-api/configuration").json()
        changed = client.patch(
            "/admin-api/configuration",
            headers=_mutation_headers(csrf),
            json={
                "revision": configuration["revision"],
                "values": {"QUICKPRICE_REQUESTS_PER_MINUTE": 240},
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["restart_required"] is True
        audit = client.get("/admin-api/audit-events").json()["events"]
        assert any(item["action"] == "provider_keys.change_requested" for item in audit)
        assert any(item["action"] == "provider_keys.changed" for item in audit)
        assert any(item["action"] == "configuration.changed" for item in audit)
        assert provider_secret not in repr(audit)


def test_provider_statistics_are_admin_only_bounded_operational_data(settings, tmp_path) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        service = client.app.state.service
        service.metrics.observe_provider_operation("finnhub", "quote", "success", 12.5)
        service.metrics.observe_provider_http("finnhub", "success", 10.0)
        unauthorized = client.get(
            "/admin-api/provider-statistics",
            headers={"X-API-Key": API_KEY},
        )
        assert unauthorized.status_code == 401
        _login(client, admin_key, secret)
        response = client.get("/admin-api/provider-statistics")
        assert response.status_code == 200
        finnhub = response.json()["providers"]["finnhub"]
        assert finnhub["operations"]["lifetime"]["success_rate"] == 100.0
        assert finnhub["upstream_http"]["lifetime"]["latency_ms"]["p95"] == 10.0
        assert finnhub["operations"]["quota"]["tracked"] is False
        assert "provider-secret" not in response.text.lower()


def test_admin_request_guard_rejects_large_and_chunked_bodies(settings, tmp_path) -> None:
    client, _, _ = _admin_client(settings, tmp_path)
    headers = {
        "Origin": "http://testserver",
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
    }
    with client:
        oversized = client.post(
            "/admin-api/session",
            headers=headers,
            content=b"x" * 65_537,
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "request_too_large"

        def chunks():
            yield b"{" + b"x" * 100
            yield b"}"

        chunked = client.post(
            "/admin-api/session",
            headers=headers,
            content=chunks(),
        )
        assert chunked.status_code == 413
