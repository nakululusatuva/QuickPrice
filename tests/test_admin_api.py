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
        assert body["api_key"]["is_permanent"] is False
        assert client.get("/v1/quotes/BTC:USDC", headers={"X-API-Key": raw_key}).status_code == 200
        access = client.get("/v1/access", headers={"X-API-Key": raw_key})
        assert access.status_code == 200
        assert access.json()["data"] == {
            "name": "Excel desktop",
            "expires_at": body["api_key"]["expires_at"],
            "is_permanent": False,
        }

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
        assert expiry_removed.json()["api_key"]["is_permanent"] is True
        permanent_access = client.get("/v1/access", headers={"X-API-Key": raw_key})
        assert permanent_access.json()["data"]["is_permanent"] is True
        assert permanent_access.json()["data"]["expires_at"] is None

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


def test_runtime_configuration_rejects_non_finite_numbers_without_writing(
    settings, tmp_path
) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        csrf = _login(client, admin_key, secret)
        initial = client.get("/admin-api/configuration").json()

        for literal in ("NaN", "Infinity", "-Infinity"):
            response = client.patch(
                "/admin-api/configuration",
                headers=_mutation_headers(csrf),
                content=(
                    '{"revision":"'
                    + initial["revision"]
                    + '","values":{"QUICKPRICE_PROVIDER_TIMEOUT_SECONDS":'
                    + literal
                    + "}}"
                ),
            )

            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "invalid_configuration"

        unchanged = client.get("/admin-api/configuration").json()
        assert unchanged["revision"] == initial["revision"]
        assert not (tmp_path / "managed" / "quickprice.env").exists()


def test_admin_shell_requires_https_and_trusts_only_the_configured_proxy(
    settings, tmp_path
) -> None:
    raw_admin_key = generate_admin_key()
    configured = replace(
        settings,
        admin_key_verifier=create_admin_key_verifier(raw_admin_key),
        admin_totp_secret=generate_totp_secret(),
        admin_origin="https://quickprice.example",
        admin_require_https=True,
        admin_trusted_proxy_ips=("127.0.0.1",),
        managed_config_path=tmp_path / "managed" / "quickprice.env",
        managed_provider_keys_path=tmp_path / "managed" / "provider-keys.env",
        managed_instruments_path=tmp_path / "managed" / "instruments.json",
    )
    service = QuickPriceService(configured)
    seed_complete(service)
    app = create_app(configured, service)

    with TestClient(
        app,
        base_url="http://quickprice.example",
        client=("127.0.0.1", 50_000),
    ) as client:
        plaintext = client.get("/admin")
        assert plaintext.status_code == 404
        assert "Administrator verification" not in plaintext.text
        assert plaintext.headers["cache-control"] == "no-store"

        proxied_https = client.get("/admin", headers={"X-Forwarded-Proto": "https"})
        assert proxied_https.status_code == 200
        assert "Administrator verification" in proxied_https.text


def test_admin_shell_ignores_forwarded_https_from_untrusted_clients(settings, tmp_path) -> None:
    configured = replace(
        settings,
        admin_key_verifier=create_admin_key_verifier(generate_admin_key()),
        admin_totp_secret=generate_totp_secret(),
        admin_origin="https://quickprice.example",
        admin_require_https=True,
        admin_trusted_proxy_ips=("127.0.0.1",),
        managed_config_path=tmp_path / "managed" / "quickprice.env",
        managed_provider_keys_path=tmp_path / "managed" / "provider-keys.env",
        managed_instruments_path=tmp_path / "managed" / "instruments.json",
    )
    service = QuickPriceService(configured)
    seed_complete(service)

    with TestClient(
        create_app(configured, service),
        base_url="http://quickprice.example",
        client=("198.51.100.10", 50_000),
    ) as client:
        response = client.get("/admin", headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == 404


def test_admin_shell_remains_available_over_http_when_https_is_disabled(settings, tmp_path) -> None:
    client, _admin_key, _secret = _admin_client(settings, tmp_path)
    with client:
        response = client.get("/admin")
        assert response.status_code == 200
        assert "Administrator verification" in response.text


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
    client, admin_key, secret = _admin_client(settings, tmp_path)
    headers = {
        "Origin": "http://testserver",
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
    }
    with client:
        malformed_unauthorized = client.post(
            "/admin-api/instrument-catalog/import",
            headers=headers,
            content=b"{",
        )
        assert malformed_unauthorized.status_code == 401
        assert malformed_unauthorized.json()["error"]["code"] == "admin_unauthorized"

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

        csrf = _login(client, admin_key, secret)
        wrong_content_type = client.post(
            "/admin-api/instrument-catalog/validate",
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": csrf,
                "Content-Type": "text/plain",
            },
            content=b'{"revision":"not-parsed"}',
        )
        assert wrong_content_type.status_code == 415
        assert wrong_content_type.json()["error"]["code"] == "json_required"


def _custom_instrument(symbol: str = "DOGE:USDC") -> dict[str, object]:
    base, quote = symbol.split(":", 1)
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "name": f"{base} / {quote}",
        "description": f"Managed {base} spot market against {quote}",
        "asset_class": "crypto",
        "asset_type": "spot_crypto",
        "price_basis": "market_price",
        "change_basis": "unadjusted_market_price",
        "enabled": True,
        "archived": False,
        "aliases": [],
        "market_calendar": "always_open",
        "quote_poll_seconds": 5,
        "stale_after_seconds": 15,
        "history": {"enabled": True, "poll_seconds": 60, "backfill_days": 30},
        "routes": [
            {"capability": "quote", "providers": ["binance", "okx"]},
            {"capability": "history", "providers": ["binance", "okx"]},
        ],
        "provider_symbols": [
            {"provider": "binance", "symbol": f"{base}{quote}"},
            {"provider": "okx", "symbol": f"{base}-{quote}"},
        ],
        "income": None,
        "synthetic": None,
    }


def test_admin_can_stage_validate_export_update_and_archive_custom_instrument(
    settings, tmp_path
) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        unauthorized = client.get(
            "/admin-api/instrument-catalog",
            headers={"X-API-Key": API_KEY},
        )
        assert unauthorized.status_code == 401
        csrf = _login(client, admin_key, secret)

        initial = client.get("/admin-api/instrument-catalog").json()
        active_symbols = {item["symbol"] for item in initial["active"]["instruments"]}
        assert "DOGE:USDC" not in active_symbols
        created = client.post(
            "/admin-api/instrument-catalog/instruments",
            headers=_mutation_headers(csrf),
            json={"revision": initial["revision"], "instrument": _custom_instrument()},
        )
        assert created.status_code == 201, created.text
        staged = created.json()
        custom = next(
            item for item in staged["staged"]["instruments"] if item["symbol"] == "DOGE:USDC"
        )
        assert custom["ownership"] == "custom"
        assert custom["id"].startswith("custom-")

        validated = client.post(
            "/admin-api/instrument-catalog/validate",
            headers=_mutation_headers(csrf),
            json={"revision": staged["revision"]},
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["valid"] is True
        assert validated.json()["diff"]["added"] == ["DOGE:USDC"]
        assert validated.json()["diff"]["counts"]["total"] == 1
        exported = client.get("/admin-api/instrument-catalog/export?state=staged")
        assert exported.status_code == 200
        assert any(item["symbol"] == "DOGE:USDC" for item in exported.json()["instruments"])

        changed = client.patch(
            f"/admin-api/instrument-catalog/instruments/{custom['id']}",
            headers=_mutation_headers(csrf),
            json={
                "revision": staged["revision"],
                "changes": {"quote_poll_seconds": 10, "stale_after_seconds": 20},
            },
        )
        assert changed.status_code == 200, changed.text
        changed_item = next(
            item for item in changed.json()["staged"]["instruments"] if item["id"] == custom["id"]
        )
        assert changed_item["quote_poll_seconds"] == 10

        archived = client.request(
            "DELETE",
            f"/admin-api/instrument-catalog/instruments/{custom['id']}",
            headers=_mutation_headers(csrf),
            json={"revision": changed.json()["revision"]},
        )
        assert archived.status_code == 200, archived.text
        archived_item = next(
            item for item in archived.json()["staged"]["instruments"] if item["id"] == custom["id"]
        )
        assert archived_item["archived"] is True
        assert archived_item["enabled"] is False

        restored = client.patch(
            f"/admin-api/instrument-catalog/instruments/{custom['id']}",
            headers=_mutation_headers(csrf),
            json={
                "revision": archived.json()["revision"],
                "changes": {"archived": False, "enabled": False},
            },
        )
        assert restored.status_code == 200, restored.text
        restored_item = next(
            item for item in restored.json()["staged"]["instruments"] if item["id"] == custom["id"]
        )
        assert restored_item["archived"] is False
        assert restored_item["enabled"] is False

        public_symbols = {
            item["symbol"]
            for item in client.get("/v1/instruments", headers={"X-API-Key": API_KEY}).json()["data"]
        }
        assert "DOGE:USDC" not in public_symbols
        events = client.get("/admin-api/audit-events").json()["events"]
        assert any(item["action"] == "instrument_catalog.created" for item in events)
        assert any(item["action"] == "instrument_catalog.archived" for item in events)


def test_catalog_rejects_unsafe_manifests_revision_conflicts_and_builtin_core_edits(
    settings, tmp_path
) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        csrf = _login(client, admin_key, secret)
        initial = client.get("/admin-api/instrument-catalog").json()
        definition = _custom_instrument("ADA:USDC")
        definition["endpoint"] = "https://example.invalid/quotes"
        unsafe = client.post(
            "/admin-api/instrument-catalog/instruments",
            headers=_mutation_headers(csrf),
            json={"revision": initial["revision"], "instrument": definition},
        )
        assert unsafe.status_code == 422
        assert unsafe.json()["error"]["code"] == "unsafe_instrument_catalog"

        created = client.post(
            "/admin-api/instrument-catalog/instruments",
            headers=_mutation_headers(csrf),
            json={"revision": initial["revision"], "instrument": _custom_instrument("ADA:USDC")},
        )
        assert created.status_code == 201
        conflict = client.post(
            "/admin-api/instrument-catalog/instruments",
            headers=_mutation_headers(csrf),
            json={"revision": initial["revision"], "instrument": _custom_instrument("DOT:USDC")},
        )
        assert conflict.status_code == 409

        builtin = created.json()["staged"]["instruments"][0]
        immutable = client.patch(
            f"/admin-api/instrument-catalog/instruments/{builtin['id']}",
            headers=_mutation_headers(csrf),
            json={
                "revision": created.json()["revision"],
                "changes": {"name": "Unauthorized built-in rename"},
            },
        )
        assert immutable.status_code == 422
        assert "Unauthorized built-in rename" not in immutable.text


def test_catalog_import_endpoint_accepts_two_thousand_custom_definitions(
    settings, tmp_path
) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    definitions: list[dict[str, object]] = []
    for index in range(2_000):
        definition = _custom_instrument(f"ASSET{index}:USDC")
        definition.update(id=f"custom-import-{index}", ownership="custom")
        definitions.append(definition)

    with client:
        csrf = _login(client, admin_key, secret)
        revision = client.get("/admin-api/instrument-catalog").json()["revision"]
        response = client.post(
            "/admin-api/instrument-catalog/import",
            headers=_mutation_headers(csrf),
            json={
                "revision": revision,
                "mode": "merge",
                "catalog": {"version": 2, "instruments": definitions},
            },
        )

        assert response.status_code == 200, response.text
        staged = response.json()["staged"]["instruments"]
        assert sum(item["ownership"] == "custom" for item in staged) == 2_000


def test_catalog_import_api_supports_merge_and_replace_custom_modes(settings, tmp_path) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    first = _custom_instrument("ADA:USDC")
    first.update(id="custom-import-ada", ownership="custom")
    replacement = _custom_instrument("DOGE:USDC")
    replacement.update(id="custom-import-doge", ownership="custom")

    with client:
        csrf = _login(client, admin_key, secret)
        revision = client.get("/admin-api/instrument-catalog").json()["revision"]
        merged = client.post(
            "/admin-api/instrument-catalog/import",
            headers=_mutation_headers(csrf),
            json={
                "revision": revision,
                "mode": "merge",
                "catalog": {"version": 2, "instruments": [first]},
            },
        )
        assert merged.status_code == 200, merged.text

        replaced = client.post(
            "/admin-api/instrument-catalog/import",
            headers=_mutation_headers(csrf),
            json={
                "revision": merged.json()["revision"],
                "mode": "replace-custom",
                "catalog": {"version": 2, "instruments": [replacement]},
            },
        )
        assert replaced.status_code == 200, replaced.text
        custom_symbols = {
            item["symbol"]
            for item in replaced.json()["staged"]["instruments"]
            if item["ownership"] == "custom"
        }
        assert custom_symbols == {"DOGE:USDC"}


def test_provider_catalog_is_secret_free_and_search_uses_service_boundary(
    settings, tmp_path
) -> None:
    provider_secret = "provider-secret-value-that-must-not-be-returned"
    client, admin_key, secret = _admin_client(
        replace(settings, alpaca_api_secret=provider_secret), tmp_path
    )

    async def search_provider_symbols(**values):
        assert values == {
            "provider": "binance",
            "query": "DOGE",
            "asset_class": "crypto",
            "limit": 5,
        }
        return {
            "provider": "binance",
            "query": "DOGE",
            "results": [
                {
                    "vendor_symbol": "DOGEUSDC",
                    "name": "Dogecoin / USD Coin",
                    "asset_class": "crypto",
                    "capabilities": ["quote", "history"],
                    "verified": True,
                }
            ],
        }

    client.app.state.service.search_provider_symbols = search_provider_symbols
    with client:
        csrf = _login(client, admin_key, secret)
        catalog = client.get("/admin-api/provider-catalog")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["fixed_endpoints_only"] is True
        assert catalog.json()["custom_providers_allowed"] is False
        assert provider_secret not in catalog.text
        missing_csrf = client.get(
            "/admin-api/provider-catalog/binance/search",
            params={"q": "DOGE", "asset_class": "crypto", "limit": 5},
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        assert missing_csrf.status_code == 401
        cross_origin = client.get(
            "/admin-api/provider-catalog/binance/search",
            params={"q": "DOGE", "asset_class": "crypto", "limit": 5},
            headers=_mutation_headers(csrf, origin="http://attacker.invalid"),
        )
        assert cross_origin.status_code == 401
        result = client.get(
            "/admin-api/provider-catalog/binance/search",
            params={"q": "DOGE", "asset_class": "crypto", "limit": 5},
            headers=_mutation_headers(csrf),
        )
        assert result.status_code == 200, result.text
        assert result.json()["results"][0]["vendor_symbol"] == "DOGEUSDC"
        unknown = client.get(
            "/admin-api/provider-catalog/not_installed/search",
            params={"q": "DOGE"},
            headers=_mutation_headers(csrf),
        )
        assert unknown.status_code == 404


def test_provider_search_preserves_rate_limit_status_and_retry_hint(settings, tmp_path) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)

    class SearchQuotaExhausted(RuntimeError):
        status = 429
        retry_after = 17

    async def search_provider_symbols(**_values):
        raise SearchQuotaExhausted("provider quota exhausted")

    client.app.state.service.search_provider_symbols = search_provider_symbols
    with client:
        csrf = _login(client, admin_key, secret)
        response = client.get(
            "/admin-api/provider-catalog/binance/search",
            params={"q": "DOGE", "asset_class": "crypto"},
            headers=_mutation_headers(csrf),
        )

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "17"
        assert response.json() == {
            "error": {
                "code": "provider_rate_limited",
                "message": "provider search rate limit exceeded",
            }
        }


def test_admin_validation_errors_use_the_admin_error_contract(settings, tmp_path) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        csrf = _login(client, admin_key, secret)
        malformed = client.post(
            "/admin-api/instrument-catalog/validate",
            headers=_mutation_headers(csrf),
            json={"revision": "not-a-revision", "unexpected": True},
        )

        assert malformed.status_code == 422
        assert set(malformed.json()) == {"error"}
        assert malformed.json()["error"]["code"] == "invalid_request"
        assert "request_id" not in malformed.text


def test_catalog_activation_job_endpoints_are_admin_only_and_audited(settings, tmp_path) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    jobs: dict[str, dict[str, object]] = {}

    async def activate_instrument_catalog(expected_revision: str):
        jobs["job-1"] = {
            "job_id": "job-1",
            "status": "warming",
            "revision": expected_revision,
            "progress_percent": 25,
        }
        return jobs["job-1"]

    async def rollback_instrument_catalog(expected_revision: str):
        jobs["job-2"] = {
            "job_id": "job-2",
            "status": "queued",
            "revision": expected_revision,
        }
        return jobs["job-2"]

    async def instrument_catalog_job(job_id: str):
        return jobs.get(job_id)

    service = client.app.state.service
    service.activate_instrument_catalog = activate_instrument_catalog
    service.rollback_instrument_catalog = rollback_instrument_catalog
    service.instrument_catalog_job = instrument_catalog_job
    with client:
        csrf = _login(client, admin_key, secret)
        revision = client.get("/admin-api/instrument-catalog").json()["revision"]
        unauthorized = client.post(
            "/admin-api/instrument-catalog/activate",
            headers={"Content-Type": "application/json"},
            json={"revision": revision},
        )
        assert unauthorized.status_code == 401
        activated = client.post(
            "/admin-api/instrument-catalog/activate",
            headers=_mutation_headers(csrf),
            json={"revision": revision},
        )
        assert activated.status_code == 202
        assert activated.json()["job_id"] == "job-1"
        assert client.get("/admin-api/instrument-catalog/jobs/job-1").status_code == 200
        missing = client.get("/admin-api/instrument-catalog/jobs/missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "activation_job_not_found"
        rolled_back = client.post(
            "/admin-api/instrument-catalog/rollback",
            headers=_mutation_headers(csrf),
            json={"revision": revision},
        )
        assert rolled_back.status_code == 202
        events = client.get("/admin-api/audit-events").json()["events"]
        assert any(item["action"] == "instrument_catalog.activation_started" for item in events)
        assert any(item["action"] == "instrument_catalog.rollback_started" for item in events)


def test_catalog_completion_audit_failure_does_not_reverse_success(settings, tmp_path) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        csrf = _login(client, admin_key, secret)
        initial = client.get("/admin-api/instrument-catalog").json()
        manager = client.app.state.api_key_manager
        append_audit = manager.append_audit
        calls = 0

        async def fail_second_audit(**values):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated audit outage")
            return await append_audit(**values)

        manager.append_audit = fail_second_audit
        created = client.post(
            "/admin-api/instrument-catalog/instruments",
            headers=_mutation_headers(csrf),
            json={"revision": initial["revision"], "instrument": _custom_instrument()},
        )
        assert created.status_code == 201, created.text
        assert any(
            item["symbol"] == "DOGE:USDC" for item in created.json()["staged"]["instruments"]
        )
        assert calls == 2


def test_legacy_instrument_patch_stages_without_changing_active_runtime(settings, tmp_path) -> None:
    client, admin_key, secret = _admin_client(settings, tmp_path)
    with client:
        csrf = _login(client, admin_key, secret)
        initial = client.get("/admin-api/instrument-catalog").json()
        btc = next(
            item for item in initial["active"]["instruments"] if item["symbol"] == "BTC:USDC"
        )

        staged = client.patch(
            "/admin-api/instruments",
            headers=_mutation_headers(csrf),
            json={
                "revision": initial["revision"],
                "instruments": [
                    {
                        "symbol": "BTC:USDC",
                        "enabled": False,
                        "quote_poll_seconds": btc["quote_poll_seconds"],
                        "stale_after_seconds": btc["stale_after_seconds"],
                    }
                ],
            },
        )
        assert staged.status_code == 200, staged.text
        body = staged.json()
        assert body["state"] == "staged"
        assert body["activation_required"] is True
        assert body["restart_required"] is False
        active_btc = next(
            item for item in body["active"]["instruments"] if item["symbol"] == "BTC:USDC"
        )
        staged_btc = next(
            item for item in body["staged"]["instruments"] if item["symbol"] == "BTC:USDC"
        )
        assert active_btc["enabled"] is True
        assert staged_btc["enabled"] is False
        public_symbols = {
            item["symbol"]
            for item in client.get("/v1/instruments", headers={"X-API-Key": API_KEY}).json()["data"]
        }
        assert "BTC:USDC" in public_symbols
