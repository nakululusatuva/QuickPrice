from __future__ import annotations

from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from quickprice.api import create_app
from quickprice.domain import (
    AccrualIndexPoint,
    ProviderQuote,
    RewardAccrualMode,
    SourceComponent,
    YieldMetric,
    YieldQuality,
    YieldRateType,
)
from quickprice.plugin_api import AssetClass, InstrumentPlugin, InstrumentSpec
from quickprice.registry import InstrumentRegistry
from quickprice.service import QuickPriceService
from tests.helpers import NOW, seed_complete

UTC = UTC


def test_authentication_is_required(client):
    response = client.get("/v1/quotes")
    assert response.status_code == 401
    body = response.json()
    assert body["errors"][0]["code"] == "unauthorized"
    assert "request_id" in body


def test_unknown_single_symbol_is_404(client, auth_headers):
    response = client.get("/v1/quotes/NOPE:USD", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "unknown_symbol"


def test_framework_404_and_405_also_use_the_standard_envelope(client, auth_headers):
    missing = client.get("/v1/not-a-route", headers=auth_headers)
    assert missing.status_code == 404
    assert missing.json()["errors"][0]["code"] == "not_found"
    method = client.post("/v1/quotes", headers=auth_headers)
    assert method.status_code == 405
    assert method.json()["errors"][0]["code"] == "method_not_allowed"


def test_unmatched_v1_routes_and_methods_are_authenticated_first(client):
    missing = client.get("/v1/not-a-route")
    assert missing.status_code == 401
    assert missing.json()["errors"][0]["code"] == "unauthorized"
    method = client.post("/v1/quotes")
    assert method.status_code == 401
    assert method.json()["errors"][0]["code"] == "unauthorized"

    for index in range(20):
        assert client.get(f"/v1/random-{index}").status_code == 401
    metric_keys = client.app.state.service.metrics.snapshot()["requests_total"]
    assert not any("random-" in key for key in metric_keys)


def test_public_readiness_is_minimal_and_internal_diagnostics_are_protected(client, auth_headers):
    public = client.get("/health/ready")
    assert public.status_code == 200
    assert public.json() == {"status": "ready"}

    unauthorized = client.get("/internal/readiness")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["errors"][0]["code"] == "unauthorized"

    detailed = client.get("/internal/readiness", headers=auth_headers)
    assert detailed.status_code == 200
    assert detailed.json()["data"]["ready"] is True

    unknown = client.get("/internal/not-a-route")
    assert unknown.status_code == 401


def test_public_readiness_does_not_build_detailed_symbol_diagnostics(client, monkeypatch) -> None:
    def fail_if_called():
        raise AssertionError("detailed readiness should not run on the public route")

    monkeypatch.setattr(client.app.state.service, "readiness", fail_if_called)
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_all_quotes_have_stable_schema_and_numeric_values(client, auth_headers):
    symbols = ",".join(client.app.state.registry.symbols)
    response = client.get(f"/v1/quotes?symbols={symbols}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1.1"
    assert body["partial"] is False
    assert len(body["data"]) == len(client.app.state.registry.symbols)
    by_symbol = {item["symbol"]: item for item in body["data"]}
    assert isinstance(by_symbol["BTC:USDC"]["price"], float | int)
    assert isinstance(by_symbol["QQQM:USD"]["dividend"]["yield_percent"], float | int)
    assert by_symbol["SGOV:USD"]["estimated_annual_yield"]["method"] == (
        "latest_distribution_annualized"
    )
    assert by_symbol["BOXX:USD"]["estimated_annual_yield"]["is_proxy"] is True
    wbeth = by_symbol["WBETH:USDC"]
    assert wbeth["reward_accrual_mode"] == "value_accruing"
    assert wbeth["underlying_asset"] == "ETH"
    assert wbeth["estimated_annual_yield"]["rate_type"] == "apy"
    assert wbeth["estimated_annual_yield"]["quality"]["confidence"] == "high"
    assert by_symbol["STETH:USDC"]["reward_accrual_mode"] == "rebasing_balance"
    assert by_symbol["WSTETH:USDC"]["reward_accrual_mode"] == "value_accruing"
    assert by_symbol["STETH:USDC"]["estimated_annual_yield"] is not None
    assert by_symbol["WSTETH:USDC"]["estimated_annual_yield"] is not None
    assert by_symbol["QQQM:USD"]["asset_class"] == "equity"
    assert by_symbol["BOXX:USD"]["asset_type"] == "growth_bond_etf"
    assert by_symbol["QQQM:USD"]["name"] == "Invesco NASDAQ 100 ETF"
    assert by_symbol["QQQM:USD"]["description"]
    assert by_symbol["SOL:USDC"]["asset_type"] == "spot_crypto"
    assert by_symbol["XMR:USDC"]["asset_type"] == "spot_crypto"
    assert by_symbol["POL:USDC"]["asset_type"] == "spot_crypto"
    assert by_symbol["BNB:USDC"]["asset_type"] == "spot_crypto"
    assert by_symbol["TRX:USDC"]["asset_type"] == "spot_crypto"
    assert by_symbol["POL:USDC"]["price"] == 0.25
    assert by_symbol["BNB:USDC"]["price"] == 800
    assert by_symbol["TRX:USDC"]["price"] == 0.3
    assert by_symbol["POL:USDC"]["name"] == "Polygon Ecosystem Token"
    assert by_symbol["BNB:USDC"]["name"] == "BNB"
    assert by_symbol["TRX:USDC"]["name"] == "TRON"
    for symbol in ("POL:USDC", "BNB:USDC", "TRX:USDC"):
        assert by_symbol[symbol]["asset_class"] == "crypto"
        assert by_symbol[symbol]["estimated_annual_yield"] is None
    assert isinstance(by_symbol["AAPL:USD"]["dividend"]["yield_percent"], float | int)
    assert by_symbol["AMZN:USD"]["dividend"] is None
    assert by_symbol["SPCX:USD"]["name"] == "Space Exploration Technologies Corp."
    assert set(by_symbol["BTC:USDC"]["changes"]) == {
        "1h",
        "4h",
        "1d",
        "1w",
        "1mo",
        "1y",
    }
    assert by_symbol["BTC:USDC"]["changes"]["1y"] is not None


def test_staking_yield_metadata_survives_service_and_api_serialization(client, auth_headers):
    component = SourceComponent(
        symbol="WBETH:ETH",
        provider="staking_market_ratio_proxy",
        price="1.10",
        as_of=NOW,
        feed="daily_history",
        role="current_market_ratio",
    )
    client.app.state.service.publish_yield_metric(
        YieldMetric(
            symbol="WBETH:USDC",
            value="3.125",
            as_of=NOW,
            method="staking_market_ratio_30d_annualized",
            provider="staking_market_ratio_proxy",
            is_proxy=True,
            components=(component,),
            rate_type=YieldRateType.APY,
            observation_window_days="30",
            accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
            underlying_asset="ETH",
            is_estimate=True,
            accrual_index=AccrualIndexPoint(
                symbol="WBETH:ETH",
                underlying_asset="ETH",
                value="1.10",
                as_of=NOW,
                provider="staking_market_ratio_proxy",
                kind="market_price_ratio",
            ),
            quality=YieldQuality(stale=False, staleness_ms=1_000, confidence="low"),
            fallback_level=2,
        ),
        persist=False,
    )

    service_yield = client.app.state.service.get_quote("WBETH:USDC", now=NOW).estimated_annual_yield
    assert service_yield is not None
    assert service_yield.fallback_level == 2
    assert service_yield.components[0].role == "current_market_ratio"

    response = client.get("/v1/quotes/WBETH:USDC", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()["data"]["estimated_annual_yield"]
    quality = body.pop("quality")
    assert quality["staleness_ms"] >= 0
    assert quality["stale_after_seconds"] == 43_200.0
    assert quality["stale"] is (quality["staleness_ms"] > quality["stale_after_seconds"] * 1000)
    assert quality["confidence"] == "low"
    assert body == {
        "percent": 3.125,
        "as_of": "2026-07-20T04:00:00Z",
        "method": "staking_market_ratio_30d_annualized",
        "provider": "staking_market_ratio_proxy",
        "is_proxy": True,
        "fallback_level": 2,
        "rate_type": "apy",
        "observation_window_days": 30.0,
        "accrual_mode": "value_accruing",
        "underlying_asset": "ETH",
        "is_estimate": True,
        "accrual_index": {
            "symbol": "WBETH:ETH",
            "underlying_asset": "ETH",
            "value": 1.1,
            "as_of": "2026-07-20T04:00:00Z",
            "provider": "staking_market_ratio_proxy",
            "kind": "market_price_ratio",
        },
        "components": [
            {
                "symbol": "WBETH:ETH",
                "provider": "staking_market_ratio_proxy",
                "feed": "daily_history",
                "role": "current_market_ratio",
                "price": 1.1,
                "as_of": "2026-07-20T04:00:00Z",
            }
        ],
        "inputs": {
            "accrual_index": 1.1,
            "accrual_index_as_of": "2026-07-20T04:00:00+00:00",
            "accrual_index_kind": "market_price_ratio",
        },
    }


def test_batch_partial_success(settings, auth_headers):
    service = QuickPriceService(settings)
    seed_complete(service, missing={"ETH:USDC"})
    with TestClient(create_app(settings, service)) as client:
        response = client.get(
            "/v1/quotes?symbols=BTC:USDC,ETH:USDC,UNKNOWN:USD", headers=auth_headers
        )
    assert response.status_code == 200
    body = response.json()
    assert body["partial"] is True
    assert [item["symbol"] for item in body["data"]] == ["BTC:USDC"]
    assert {error["code"] for error in body["errors"]} == {
        "data_unavailable",
        "unknown_symbol",
    }


def test_no_valid_data_is_503(settings, auth_headers):
    service = QuickPriceService(settings)
    with TestClient(create_app(settings, service)) as client:
        response = client.get("/v1/quotes/BTC:USDC", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["errors"][0]["code"] == "data_unavailable"


def test_instruments_documents_classification_and_methods(client, auth_headers):
    response = client.get("/v1/instruments", headers=auth_headers)
    assert response.status_code == 200
    items = {item["symbol"]: item for item in response.json()["data"]}
    assert len(items) == len(client.app.state.registry.symbols)
    assert items["SGOV:USD"]["yield_method"] == "latest_distribution_annualized"
    assert items["WBETH:USDC"]["yield_method"] == "staking_provider_metric"
    assert items["WBETH:USDC"]["reward_accrual_mode"] == "value_accruing"
    assert items["STETH:USDC"]["reward_accrual_mode"] == "rebasing_balance"
    assert items["WSTETH:USDC"]["reward_accrual_mode"] == "value_accruing"
    assert items["EUR:GBP"]["asset_class"] == "fx"
    assert items["QQQM:USD"]["dividend_method"] == "latest_regular_cash_annualized_x4"
    assert items["QQQM:USD"]["name"] == "Invesco NASDAQ 100 ETF"
    assert items["QQQM:USD"]["description"]
    assert items["AAPL:USD"]["asset_type"] == "common_stock"
    assert items["AAPL:USD"]["dividend_method"] == "latest_regular_cash_annualized_x4"
    assert items["AMZN:USD"]["dividend_method"] is None
    assert items["SPCX:USD"]["name"] == "Space Exploration Technologies Corp."
    assert items["QQQM:USD"]["change_windows"]["1y"] == "rolling_365_days"


def test_batch_quotes_defaults_to_all_and_limits_explicit_unique_items(client, auth_headers):
    all_quotes = client.get("/v1/quotes", headers=auth_headers)
    assert all_quotes.status_code == 200
    assert len(all_quotes.json()["data"]) == len(client.app.state.registry.symbols)

    empty = client.get("/v1/quotes?symbols=", headers=auth_headers)
    assert empty.status_code == 422

    too_many = ",".join(f"ASSET{index}:USD" for index in range(101))
    response = client.get(f"/v1/quotes?symbols={too_many}", headers=auth_headers)
    assert response.status_code == 422
    assert "more than 100" in response.json()["errors"][0]["message"]


def test_batch_accepts_one_hundred_plugin_instruments(settings, auth_headers) -> None:
    instruments = tuple(
        InstrumentSpec(
            symbol=f"ASSET{index}:USD",
            base=f"ASSET{index}",
            quote="USD",
            name=f"Test Asset {index}",
            description="An instrument used to verify the batch boundary.",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            price_basis="last_trade",
        )
        for index in range(100)
    )
    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="batch-test",
                version="1",
                instruments=instruments,
                provider_installer=lambda _: None,
            ),
        )
    )
    service = QuickPriceService(settings, registry)
    for instrument in instruments:
        service.publish_quote(
            ProviderQuote(
                instrument.symbol,
                "100",
                NOW,
                "fixture",
                "fixture",
            ),
            persist=False,
        )
    symbols = ",".join(instrument.symbol for instrument in instruments)
    with TestClient(create_app(settings, service)) as client:
        response = client.get(f"/v1/quotes?symbols={symbols}", headers=auth_headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) == 100


def test_create_app_rejects_a_registry_that_differs_from_the_service(settings) -> None:
    service = QuickPriceService(settings)
    registry = InstrumentRegistry(
        (InstrumentPlugin(plugin_id="empty", version="1", instruments=()),)
    )
    with pytest.raises(ValueError, match="must match"):
        create_app(settings, service, registry)


def test_production_docs_are_disabled(settings, service):
    production_settings = type(settings)(
        production=True,
        require_free_threaded=False,
        background_enabled=False,
        database_path=settings.database_path,
        api_key_hashes=settings.api_key_hashes,
        rate_limit_enabled=False,
    )
    with TestClient(create_app(production_settings, service)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_request_id_is_uuid7(client, auth_headers):
    response = client.get("/v1/instruments", headers=auth_headers)
    request_id = response.json()["request_id"]
    assert request_id == response.headers["X-Request-ID"]
    import uuid

    assert uuid.UUID(request_id).version == 7
