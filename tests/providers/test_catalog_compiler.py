from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quickprice.catalog import (
    CapabilityRoute,
    CatalogGeneration,
    IncomePolicy,
    InstrumentOwnership,
    ManagedInstrumentDefinition,
    ProviderSymbolBinding,
    SyntheticOperation,
    SyntheticRecipeDefinition,
)
from quickprice.config import Settings
from quickprice.domain import RewardAccrualMode
from quickprice.managed_config import InstrumentPolicyStore
from quickprice.plugin_api import AssetClass, YieldStrategy
from quickprice.providers.alpaca import AlpacaProvider
from quickprice.providers.alpha_vantage import AlphaVantageProvider
from quickprice.providers.base import Capability, ProviderUnavailable
from quickprice.providers.binance import BinanceProvider
from quickprice.providers.coingecko import (
    CoinGeckoProvider,
    coingecko_simple_price_id_batches,
)
from quickprice.providers.compiler import (
    InstrumentRouteInput,
    RouteCompileError,
    SyntheticRouteInput,
    build_compiled_provider_graph,
    builtin_provider_policy,
    compile_route_plan,
    incremental_credit_budget_errors,
    instrument_route_input_from_definition,
)
from quickprice.providers.descriptors import (
    ProviderBindingVerificationError,
    estimate_daily_credits,
    provider_catalog_snapshot,
    search_provider_symbols,
    validate_provider_symbol,
    verify_provider_bindings,
)
from quickprice.providers.finnhub import FinnhubProvider
from quickprice.providers.fred import FredProvider
from quickprice.providers.kraken import KrakenProvider
from quickprice.providers.okx import OkxMarketProvider
from quickprice.providers.quota import daily_budget
from quickprice.providers.twelve_data import TwelveDataProvider
from quickprice.providers.wiring import build_provider_graph
from quickprice.registry import build_registry
from quickprice.service import QuickPriceService


def test_provider_catalog_is_secret_free_and_uses_fixed_hosts() -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        alpaca_api_key="sensitive-key",
        alpaca_api_secret="sensitive-secret",
    )

    snapshot = provider_catalog_snapshot(settings)
    encoded = repr(snapshot)

    assert snapshot["schema_version"] == 1
    assert "sensitive-key" not in encoded
    assert "sensitive-secret" not in encoded
    alpaca = next(item for item in snapshot["providers"] if item["name"] == "alpaca")
    assert alpaca["credentials_configured"] is True
    assert alpaca["fixed_hosts"] == ["data.alpaca.markets", "paper-api.alpaca.markets"]
    assert alpaca["operational_limits"] == {
        "stream_symbols": 30,
        "rest_calls_per_minute": 180,
    }


@pytest.mark.parametrize(
    ("provider", "raw", "normalized"),
    [
        ("binance", "avaxusdc", "AVAXUSDC"),
        ("okx", "avax-usdc", "AVAX-USDC"),
        ("alpaca", "brk.b", "BRK.B"),
        ("coingecko", "wrapped-bitcoin", "wrapped-bitcoin"),
        ("fred", "DGS1", "DGS1"),
    ],
)
def test_vendor_symbol_validation_is_provider_specific(
    provider: str, raw: str, normalized: str
) -> None:
    assert validate_provider_symbol(provider, raw) == normalized


@pytest.mark.parametrize("value", ["https://evil.invalid/x", "BTCUSDC?key=x", "BTC@USDC"])
def test_vendor_symbol_validation_rejects_network_or_credential_syntax(value: str) -> None:
    with pytest.raises(ValueError):
        validate_provider_symbol("binance", value)


def test_fred_symbol_validation_rejects_uncontrolled_maturities() -> None:
    with pytest.raises(ValueError, match="invalid fred"):
        validate_provider_symbol("fred", "DGS10")


def test_dividend_strategy_is_a_closed_financial_policy() -> None:
    policy = IncomePolicy(dividend_strategy="latest_regular_cash_annualized_x4")
    assert policy.dividend_strategy == "latest_regular_cash_annualized_x4"

    with pytest.raises(ValueError, match="dividend strategy is not supported"):
        IncomePolicy(dividend_strategy="latest_regular_cash_annualized")


@pytest.mark.asyncio
async def test_provider_search_collapses_concurrent_identical_requests() -> None:
    calls = 0

    async def fetcher(
        provider: str,
        url: str,
        params: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> object:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        assert provider == "binance"
        assert url == "https://api.binance.com/api/v3/exchangeInfo"
        assert not params and not headers
        return {
            "symbols": [
                {
                    "status": "TRADING",
                    "symbol": "CACHEUNITUSDC",
                    "baseAsset": "CACHEUNIT",
                    "quoteAsset": "USDC",
                }
            ]
        }

    settings = Settings(require_free_threaded=False, background_enabled=False)
    results = await asyncio.gather(
        *(
            search_provider_symbols(
                settings,
                "binance",
                "CACHEUNIT",
                fetcher=fetcher,
            )
            for _ in range(5)
        )
    )

    assert calls == 1
    assert all(item["results"][0]["canonical_hint"] == "CACHEUNIT:USDC" for item in results)


@pytest.mark.asyncio
async def test_full_list_provider_search_cache_is_query_independent() -> None:
    calls = 0

    async def fetcher(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "symbols": [
                {
                    "status": "TRADING",
                    "symbol": "CACHEALPHAUSDC",
                    "baseAsset": "CACHEALPHA",
                    "quoteAsset": "USDC",
                },
                {
                    "status": "TRADING",
                    "symbol": "CACHEBETAUSDC",
                    "baseAsset": "CACHEBETA",
                    "quoteAsset": "USDC",
                },
            ]
        }

    settings = Settings(require_free_threaded=False, background_enabled=False)
    alpha = await search_provider_symbols(settings, "binance", "CACHEALPHA", fetcher=fetcher)
    beta = await search_provider_symbols(settings, "binance", "CACHEBETA", fetcher=fetcher)

    assert calls == 1
    assert alpha["results"][0]["canonical_hint"] == "CACHEALPHA:USDC"
    assert beta["results"][0]["canonical_hint"] == "CACHEBETA:USDC"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "credentials", "payload"),
    [
        (
            "alpaca",
            {"alpaca_api_key": "key", "alpaca_api_secret": "secret"},
            [{"status": "active", "symbol": "BONDTEST", "name": "Bond Search ETF"}],
        ),
        (
            "finnhub",
            {"finnhub_api_key": "key"},
            {"result": [{"symbol": "BONDTEST", "description": "Bond Search ETF"}]},
        ),
        (
            "twelve_data",
            {"twelve_data_api_key": "key"},
            {
                "data": [
                    {
                        "symbol": "BONDTEST",
                        "instrument_name": "Bond Search ETF",
                        "instrument_type": "ETF",
                    }
                ]
            },
        ),
        (
            "alpha_vantage",
            {"alpha_vantage_api_key": "key"},
            {
                "bestMatches": [
                    {
                        "1. symbol": "BONDTEST",
                        "2. name": "Bond Search ETF",
                        "3. type": "ETF",
                    }
                ]
            },
        ),
    ],
)
async def test_listed_provider_search_can_filter_for_bond_instruments(
    provider: str,
    credentials: dict[str, str],
    payload: object,
) -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        **credentials,
    )

    async def fetcher(*_args, **_kwargs):
        return payload

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        return True

    result = await search_provider_symbols(
        settings,
        provider,
        "BONDTEST",
        asset_class=AssetClass.BOND,
        fetcher=fetcher,
        credit_reserver=reserve_credit,
    )

    assert len(result["results"]) == 1
    assert result["results"][0]["asset_class"] == AssetClass.EQUITY.value
    assert result["results"][0]["asset_classes"] == [
        AssetClass.BOND.value,
        AssetClass.EQUITY.value,
    ]


@pytest.mark.asyncio
async def test_provider_search_preserves_an_upstream_rate_limit_status_in_cache() -> None:
    calls = 0
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        finnhub_api_key="key",
    )

    async def fetcher(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise ProviderUnavailable("finnhub", "upstream rate limit", status=429)

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        return True

    for _ in range(2):
        with pytest.raises(ProviderUnavailable) as exc_info:
            await search_provider_symbols(
                settings,
                "finnhub",
                "UPSTREAM429",
                fetcher=fetcher,
                credit_reserver=reserve_credit,
            )
        assert exc_info.value.status == 429
    assert calls == 1


def _crypto_binding_plan(
    *,
    coingecko_id: str = "ethereum",
    providers: tuple[str, ...] = ("binance", "coingecko"),
):
    bindings = {"binance": "ETHUSDC", "coingecko": coingecko_id}
    if "kraken" in providers:
        bindings["kraken"] = "ETHUSDC"
    return compile_route_plan(
        (
            InstrumentRouteInput(
                symbol="ETH:USDC",
                asset_class=AssetClass.CRYPTO,
                asset_type="spot_crypto",
                quote_poll_seconds=60,
                history_enabled=False,
                provider_symbols=bindings,
                routes={Capability.QUOTE: providers},
            ),
        ),
        available_providers=set(providers),
    )


@pytest.mark.asyncio
async def test_binding_verifier_checks_valid_primary_and_fallback_catalogs(fixture_json) -> None:
    plan = _crypto_binding_plan()
    requests: list[tuple[str, str]] = []
    reservations = 0

    async def fetcher(provider, url, _params, _headers):
        requests.append((provider, url))
        return (
            fixture_json("binance_exchange_info_bindings.json")
            if provider == "binance"
            else fixture_json("coingecko_coin_list.json")
        )

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        nonlocal reservations
        reservations += 1
        return True

    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        coingecko_api_key="key",
    )
    result = await verify_provider_bindings(
        settings,
        plan,
        symbols=("ETH:USDC",),
        fetcher=fetcher,
        credit_reserver=reserve_credit,
    )

    assert result["verified"] is True
    assert result["binding_count"] == 2
    assert len(result["binding_set_sha256"]) == 64
    assert {provider for provider, _ in requests} == {"binance", "coingecko"}
    assert reservations == 1

    cached = await verify_provider_bindings(
        settings,
        plan,
        symbols=("ETH:USDC",),
        fetcher=fetcher,
        credit_reserver=reserve_credit,
    )

    assert len(requests) == 2
    assert reservations == 1
    assert cached["providers"]["binance"]["requests"] == 0
    assert cached["providers"]["coingecko"]["requests"] == 0


@pytest.mark.asyncio
async def test_binding_verifier_rejects_opaque_coingecko_identity_mismatch(
    fixture_json,
) -> None:
    plan = _crypto_binding_plan(coingecko_id="bitcoin", providers=("coingecko",))

    async def fetcher(*_args, **_kwargs):
        return fixture_json("coingecko_coin_list.json")

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        return True

    with pytest.raises(ProviderBindingVerificationError) as exc_info:
        await verify_provider_bindings(
            Settings(
                require_free_threaded=False,
                background_enabled=False,
                coingecko_api_key="key",
            ),
            plan,
            symbols=("ETH:USDC",),
            fetcher=fetcher,
            credit_reserver=reserve_credit,
        )

    assert exc_info.value.as_dict()["failures"] == [
        {
            "provider": "coingecko",
            "symbol": "ETH:USDC",
            "code": "identity_mismatch",
            "status": None,
        }
    ]


@pytest.mark.asyncio
async def test_binding_verifier_rejects_an_unsupported_unused_fallback(fixture_json) -> None:
    plan = _crypto_binding_plan(providers=("binance", "kraken"))

    async def fetcher(provider, _url, _params, _headers):
        return fixture_json(
            "binance_exchange_info_bindings.json"
            if provider == "binance"
            else "kraken_asset_pairs_bindings.json"
        )

    with pytest.raises(ProviderBindingVerificationError) as exc_info:
        await verify_provider_bindings(
            Settings(require_free_threaded=False, background_enabled=False),
            plan,
            symbols=("ETH:USDC",),
            fetcher=fetcher,
        )

    assert len(exc_info.value.failures) == 1
    failure = exc_info.value.failures[0]
    assert (failure.provider, failure.symbol, failure.code) == (
        "kraken",
        "ETH:USDC",
        "unsupported_binding",
    )


@pytest.mark.asyncio
async def test_alpha_vantage_binding_verification_batches_listed_instruments() -> None:
    items = tuple(
        InstrumentRouteInput(
            symbol=f"{ticker}:USD",
            asset_class=asset_class,
            asset_type=asset_type,
            quote_poll_seconds=3_600,
            history_enabled=False,
            provider_symbols={"alpha_vantage": ticker},
            routes={Capability.QUOTE: ("alpha_vantage",)},
        )
        for ticker, asset_class, asset_type in (
            ("CATALOGA", AssetClass.EQUITY, "common_stock"),
            ("CATALOGB", AssetClass.BOND, "income_bond_etf"),
        )
    )
    plan = compile_route_plan(items, available_providers={"alpha_vantage"})
    requests: list[tuple[str, str, Mapping[str, object]]] = []
    reservations = 0

    async def fetcher(provider, url, params, headers):
        requests.append((provider, url, params))
        assert headers == {}
        return "\n".join(
            (
                "symbol,name,exchange,assetType,ipoDate,delistingDate,status",
                "CATALOGA,Catalog A,NASDAQ,Stock,2020-01-01,null,Active",
                "CATALOGB,Catalog B,NYSE ARCA,ETF,2020-01-01,null,Active",
            )
        )

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        nonlocal reservations
        reservations += 1
        return True

    result = await verify_provider_bindings(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            alpha_vantage_api_key="key",
        ),
        plan,
        symbols=(item.symbol for item in items),
        fetcher=fetcher,
        credit_reserver=reserve_credit,
    )

    assert result["verified"] is True
    assert result["binding_count"] == 2
    assert result["providers"]["alpha_vantage"]["requests"] == 1
    assert reservations == 1
    assert requests == [
        (
            "alpha_vantage",
            "https://www.alphavantage.co/query",
            {
                "function": "LISTING_STATUS",
                "state": "active",
                "apikey": "key",
            },
        )
    ]


@pytest.mark.asyncio
async def test_alpha_vantage_binding_verification_rejects_an_unlisted_fallback() -> None:
    item = InstrumentRouteInput(
        symbol="CATALOGX:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=3_600,
        history_enabled=False,
        provider_symbols={"finnhub": "CATALOGX", "alpha_vantage": "CATALOGX"},
        routes={Capability.QUOTE: ("finnhub", "alpha_vantage")},
    )
    plan = compile_route_plan(
        (item,),
        available_providers={"finnhub", "alpha_vantage"},
    )

    async def fetcher(provider, _url, _params, _headers):
        if provider == "finnhub":
            return [{"symbol": "CATALOGX"}]
        return "\n".join(
            (
                "symbol,name,exchange,assetType,ipoDate,delistingDate,status",
                "OTHER,Other,NASDAQ,Stock,2020-01-01,null,Active",
            )
        )

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        return True

    with pytest.raises(ProviderBindingVerificationError) as exc_info:
        await verify_provider_bindings(
            Settings(
                require_free_threaded=False,
                background_enabled=False,
                finnhub_api_key="finnhub-key",
                alpha_vantage_api_key="alpha-key",
            ),
            plan,
            symbols=(item.symbol,),
            fetcher=fetcher,
            credit_reserver=reserve_credit,
        )

    assert exc_info.value.as_dict()["failures"] == [
        {
            "provider": "alpha_vantage",
            "symbol": "CATALOGX:USD",
            "code": "unsupported_binding",
            "status": None,
        }
    ]


@pytest.mark.asyncio
async def test_alpha_vantage_binding_verification_obeys_the_shared_credit_ledger() -> None:
    item = InstrumentRouteInput(
        symbol="CATALOGQ:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=3_600,
        history_enabled=False,
        provider_symbols={"alpha_vantage": "CATALOGQ"},
        routes={Capability.QUOTE: ("alpha_vantage",)},
    )
    plan = compile_route_plan((item,), available_providers={"alpha_vantage"})
    fetch_calls = 0

    async def fetcher(*_args, **_kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        return ""

    async def deny_credit(_provider: str, _cost: int) -> bool:
        return False

    with pytest.raises(ProviderBindingVerificationError) as exc_info:
        await verify_provider_bindings(
            Settings(
                require_free_threaded=False,
                background_enabled=False,
                alpha_vantage_api_key="key",
            ),
            plan,
            symbols=(item.symbol,),
            fetcher=fetcher,
            credit_reserver=deny_credit,
        )

    assert fetch_calls == 0
    assert exc_info.value.as_dict()["failures"] == [
        {
            "provider": "alpha_vantage",
            "symbol": "CATALOGQ:USD",
            "code": "verification_rate_limited",
            "status": 429,
        }
    ]


@pytest.mark.asyncio
async def test_alpha_vantage_fx_binding_verification_uses_a_fixed_pair_request() -> None:
    item = InstrumentRouteInput(
        symbol="USD:JPY",
        asset_class=AssetClass.FX,
        asset_type="forex_pair",
        quote_poll_seconds=3_600,
        history_enabled=True,
        provider_symbols={"alpha_vantage": "USD/JPY"},
        routes={
            Capability.QUOTE: ("alpha_vantage",),
            Capability.HISTORY: ("alpha_vantage",),
        },
    )
    plan = compile_route_plan((item,), available_providers={"alpha_vantage"})

    async def fetcher(provider, url, params, headers):
        assert provider == "alpha_vantage"
        assert url == "https://www.alphavantage.co/query"
        assert params == {
            "function": "CURRENCY_EXCHANGE_RATE",
            "from_currency": "USD",
            "to_currency": "JPY",
            "apikey": "key",
        }
        assert headers == {}
        return {
            "Realtime Currency Exchange Rate": {
                "1. From_Currency Code": "USD",
                "3. To_Currency Code": "JPY",
            }
        }

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        return True

    result = await verify_provider_bindings(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            alpha_vantage_api_key="key",
        ),
        plan,
        symbols=(item.symbol,),
        fetcher=fetcher,
        credit_reserver=reserve_credit,
    )

    assert result["verified"] is True
    assert result["providers"]["alpha_vantage"]["requests"] == 1


def _dynamic_fx_cross_binding_plan():
    return compile_route_plan(
        (
            InstrumentRouteInput(
                symbol="CAD:JPY",
                asset_class=AssetClass.FX,
                asset_type="forex_pair",
                quote_poll_seconds=240,
                history_enabled=True,
                routes={
                    Capability.QUOTE: ("synthetic_fx",),
                    Capability.HISTORY: ("synthetic_fx",),
                },
            ),
        ),
        available_providers={"synthetic_fx", "twelve_data", "alpha_vantage"},
    )


@pytest.mark.asyncio
async def test_binding_verifier_includes_every_hidden_fx_spoke_fallback() -> None:
    plan = _dynamic_fx_cross_binding_plan()
    requests: list[tuple[str, str, Mapping[str, object]]] = []
    reservations: list[str] = []

    async def fetcher(provider, url, params, _headers):
        requests.append((provider, url, params))
        if provider == "twelve_data":
            return {
                "data": [
                    {"symbol": "USD/CAD"},
                    {"symbol": "USD/JPY"},
                ]
            }
        return {
            "Realtime Currency Exchange Rate": {
                "1. From_Currency Code": params["from_currency"],
                "3. To_Currency Code": params["to_currency"],
            }
        }

    async def reserve_credit(provider: str, _cost: int) -> bool:
        reservations.append(provider)
        return True

    result = await verify_provider_bindings(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            twelve_data_api_key="twelve-key",
            alpha_vantage_api_key="alpha-key",
        ),
        plan,
        symbols=("CAD:JPY",),
        fetcher=fetcher,
        credit_reserver=reserve_credit,
    )

    assert result["verified"] is True
    assert result["binding_count"] == 4
    assert result["providers"]["twelve_data"]["bindings"] == 2
    assert result["providers"]["twelve_data"]["requests"] == 1
    assert result["providers"]["alpha_vantage"]["bindings"] == 2
    assert result["providers"]["alpha_vantage"]["requests"] == 2
    assert reservations.count("twelve_data") == 1
    assert reservations.count("alpha_vantage") == 2
    assert {(provider, url, params.get("function")) for provider, url, params in requests} == {
        ("twelve_data", "https://api.twelvedata.com/forex_pairs", None),
        (
            "alpha_vantage",
            "https://www.alphavantage.co/query",
            "CURRENCY_EXCHANGE_RATE",
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported_provider", ["twelve_data", "alpha_vantage"])
async def test_binding_verifier_rejects_an_unsupported_hidden_fx_spoke(
    unsupported_provider: str,
) -> None:
    plan = _dynamic_fx_cross_binding_plan()

    async def fetcher(provider, _url, params, _headers):
        if provider == "twelve_data":
            pairs = ["USD/JPY"] if unsupported_provider == provider else ["USD/CAD", "USD/JPY"]
            return {"data": [{"symbol": pair} for pair in pairs]}
        quote = str(params["to_currency"])
        if unsupported_provider == provider and quote == "CAD":
            quote = "EUR"
        return {
            "Realtime Currency Exchange Rate": {
                "1. From_Currency Code": "USD",
                "3. To_Currency Code": quote,
            }
        }

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        return True

    with pytest.raises(ProviderBindingVerificationError) as exc_info:
        await verify_provider_bindings(
            Settings(
                require_free_threaded=False,
                background_enabled=False,
                twelve_data_api_key="twelve-key",
                alpha_vantage_api_key="alpha-key",
            ),
            plan,
            symbols=("CAD:JPY",),
            fetcher=fetcher,
            credit_reserver=reserve_credit,
        )

    assert exc_info.value.as_dict()["failures"] == [
        {
            "provider": unsupported_provider,
            "symbol": "USD:CAD",
            "code": "unsupported_binding",
            "status": None,
        }
    ]


@pytest.mark.asyncio
async def test_binding_verifier_collapses_two_thousand_opaque_ids_to_one_catalog_call() -> None:
    items = tuple(
        InstrumentRouteInput(
            symbol=f"C{index:04d}:USDC",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            quote_poll_seconds=300,
            history_enabled=False,
            provider_symbols={"coingecko": f"c{index:04d}"},
            routes={Capability.QUOTE: ("coingecko",)},
        )
        for index in range(2_000)
    )
    plan = compile_route_plan(items, available_providers={"coingecko"})
    calls = 0
    reservations = 0

    async def fetcher(_provider, _url, _params, _headers):
        nonlocal calls
        calls += 1
        return [
            {"id": f"c{index:04d}", "symbol": f"c{index:04d}", "name": "Fixture"}
            for index in range(2_000)
        ]

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        nonlocal reservations
        reservations += 1
        return True

    result = await verify_provider_bindings(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            coingecko_api_key="key",
        ),
        plan,
        symbols=(item.symbol for item in items),
        fetcher=fetcher,
        credit_reserver=reserve_credit,
    )

    assert result["binding_count"] == 2_000
    assert calls == reservations == 1


@pytest.mark.asyncio
async def test_binding_verifier_sanitizes_upstream_failure_details() -> None:
    plan = _crypto_binding_plan(providers=("coingecko",))
    secret = "secret-provider-key"

    async def fetcher(*_args, **_kwargs):
        raise ProviderUnavailable(
            "coingecko",
            f"https://api.coingecko.invalid?key={secret}",
            status=500,
        )

    async def reserve_credit(_provider: str, _cost: int) -> bool:
        return True

    with pytest.raises(ProviderBindingVerificationError) as exc_info:
        await verify_provider_bindings(
            Settings(
                require_free_threaded=False,
                background_enabled=False,
                coingecko_api_key=secret,
            ),
            plan,
            symbols=("ETH:USDC",),
            fetcher=fetcher,
            credit_reserver=reserve_credit,
        )

    rendered = f"{exc_info.value!s} {exc_info.value.as_dict()!r}"
    assert secret not in rendered
    assert "https://" not in rendered
    assert exc_info.value.failures[0].status == 500


@pytest.mark.asyncio
async def test_metered_search_and_collectors_share_one_quota_ledger() -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        twelve_data_api_key="twelve-key",
    )
    service = QuickPriceService(settings)
    quota = daily_budget(2)
    shared, _ = service.share_provider_quota("twelve_data", quota)

    async def fetcher(*_args, **_kwargs):
        return {
            "data": [
                {
                    "symbol": "SHARED",
                    "instrument_name": "Shared Ledger Test",
                    "instrument_type": "Common Stock",
                }
            ]
        }

    result = await search_provider_symbols(
        settings,
        "twelve_data",
        "SHARED",
        fetcher=fetcher,
        credit_reserver=service.reserve_provider_search_credit,
    )
    assert result["results"][0]["canonical_hint"] == "SHARED:USD"
    assert (await shared.snapshot()).used == 1

    assert await shared.acquire() is True
    with pytest.raises(ProviderUnavailable) as exc_info:
        await search_provider_symbols(
            settings,
            "twelve_data",
            "EXHAUSTED",
            fetcher=fetcher,
            credit_reserver=service.reserve_provider_search_credit,
        )
    assert exc_info.value.status == 429
    assert (await shared.snapshot()).used == 2


def test_default_crypto_route_uses_only_bound_and_available_providers() -> None:
    item = InstrumentRouteInput(
        symbol="AVAX:USDC",
        asset_class=AssetClass.CRYPTO,
        asset_type="spot_crypto",
        quote_poll_seconds=1,
        provider_symbols={
            "binance": "AVAXUSDC",
            "okx": "AVAX-USDC",
            "coingecko": "avalanche-2",
        },
    )

    plan = compile_route_plan(
        (item,),
        available_providers={"binance", "okx", "kraken", "coingecko"},
    )

    assert plan.providers_for("AVAX:USDC", Capability.QUOTE) == (
        "binance",
        "okx",
        "coingecko",
    )
    assert plan.providers_for("AVAX:USDC", Capability.HISTORY) == (
        "binance",
        "okx",
        "coingecko",
    )


def test_custom_explicit_route_never_drops_an_unconfigured_fallback() -> None:
    explicit = InstrumentRouteInput(
        symbol="AVAX:USDC",
        asset_class=AssetClass.CRYPTO,
        asset_type="spot_crypto",
        quote_poll_seconds=60,
        history_enabled=False,
        provider_symbols={
            "binance": "AVAXUSDC",
            "coingecko": "avalanche-2",
        },
        routes={Capability.QUOTE: ("binance", "coingecko")},
    )

    with pytest.raises(RouteCompileError, match="provider is not configured: coingecko"):
        compile_route_plan(
            (explicit,),
            available_providers={"binance"},
            drop_unconfigured=True,
        )

    automatic = InstrumentRouteInput(
        symbol="AVAX:USDC",
        asset_class=AssetClass.CRYPTO,
        asset_type="spot_crypto",
        quote_poll_seconds=60,
        history_enabled=False,
        provider_symbols=explicit.provider_symbols,
    )
    plan = compile_route_plan(
        (automatic,),
        available_providers={"binance"},
        drop_unconfigured=True,
    )
    assert plan.providers_for("AVAX:USDC", Capability.QUOTE) == ("binance",)


def test_advanced_route_rejects_an_incompatible_provider() -> None:
    with pytest.raises(RouteCompileError, match="incompatible"):
        InstrumentRouteInput(
            symbol="AVAX:USDC",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            quote_poll_seconds=1,
            history_enabled=False,
            provider_symbols={"finnhub": "AVAX"},
            routes={Capability.QUOTE: ("finnhub",)},
        )


@pytest.mark.parametrize(
    ("item", "available"),
    [
        (
            InstrumentRouteInput(
                symbol="LST:USDC",
                asset_class=AssetClass.CRYPTO,
                asset_type="spot_crypto",
                quote_poll_seconds=60,
                history_enabled=False,
                routes={Capability.QUOTE: ("synthetic_wbeth_primary",)},
            ),
            {"synthetic_wbeth_primary"},
        ),
        (
            InstrumentRouteInput(
                symbol="LST:USDC",
                asset_class=AssetClass.CRYPTO,
                asset_type="spot_crypto",
                quote_poll_seconds=60,
                provider_symbols={"binance": "LSTUSDC"},
                routes={
                    Capability.QUOTE: ("binance",),
                    Capability.HISTORY: ("synthetic_wbeth_history_primary",),
                },
            ),
            {"binance", "synthetic_wbeth_history_primary"},
        ),
        (
            InstrumentRouteInput(
                symbol="CAD:JPY",
                asset_class=AssetClass.FX,
                asset_type="forex_pair",
                quote_poll_seconds=240,
                routes={
                    Capability.QUOTE: ("synthetic_fx",),
                    Capability.HISTORY: ("synthetic_fx_history",),
                },
            ),
            {"synthetic_fx", "synthetic_fx_history"},
        ),
    ],
)
def test_custom_routes_reject_private_provider_graph_keys(
    item: InstrumentRouteInput,
    available: set[str],
) -> None:
    with pytest.raises(RouteCompileError, match="not publicly selectable"):
        compile_route_plan((item,), available_providers=available)


def test_builtin_route_accepts_private_graph_keys_only_for_its_shipped_policy() -> None:
    item = InstrumentRouteInput(
        symbol="BTC:USDC",
        asset_class=AssetClass.CRYPTO,
        asset_type="spot_crypto",
        quote_poll_seconds=60,
        ownership="builtin",
        history_enabled=False,
        routes={Capability.QUOTE: ("synthetic_wbeth_primary",)},
    )

    with pytest.raises(RouteCompileError, match="not publicly selectable"):
        compile_route_plan(
            (item,),
            available_providers={"synthetic_wbeth_primary"},
        )


def test_synthetic_fx_rejects_direct_usd_spokes_and_explicit_recipes() -> None:
    direct = InstrumentRouteInput(
        symbol="USD:CAD",
        asset_class=AssetClass.FX,
        asset_type="forex_pair",
        quote_poll_seconds=240,
        history_enabled=False,
        routes={Capability.QUOTE: ("synthetic_fx",)},
    )
    with pytest.raises(RouteCompileError, match="must use direct providers"):
        compile_route_plan((direct,), available_providers={"synthetic_fx"})

    explicit = InstrumentRouteInput(
        symbol="CAD:JPY",
        asset_class=AssetClass.FX,
        asset_type="forex_pair",
        quote_poll_seconds=240,
        history_enabled=False,
        routes={Capability.QUOTE: ("synthetic_fx",)},
        synthetic=SyntheticRouteInput("inverse", ("USD:CAD",)),
    )
    source = InstrumentRouteInput(
        symbol="USD:CAD",
        asset_class=AssetClass.FX,
        asset_type="forex_pair",
        quote_poll_seconds=240,
        history_enabled=False,
        provider_symbols={"twelve_data": "USD/CAD"},
        routes={Capability.QUOTE: ("twelve_data",)},
    )
    with pytest.raises(RouteCompileError, match="must use the synthetic provider"):
        compile_route_plan(
            (source, explicit),
            available_providers={"synthetic_fx", "twelve_data"},
        )


@pytest.mark.parametrize(
    ("symbol", "asset_class", "provider", "vendor_symbol"),
    [
        ("AAPL:USD", AssetClass.EQUITY, "alpaca", "MSFT"),
        ("BTC:USDC", AssetClass.CRYPTO, "binance", "ETHUSDC"),
        ("BTC:USDC", AssetClass.CRYPTO, "okx", "BTC-USDT"),
        ("BTC:USDC", AssetClass.CRYPTO, "kraken", "XBTUSDT"),
        ("EUR:JPY", AssetClass.FX, "twelve_data", "AAPL"),
        ("AVAX:EUR", AssetClass.CRYPTO, "coingecko", "avalanche-2"),
    ],
)
def test_vendor_binding_must_match_the_canonical_instrument(
    symbol: str,
    asset_class: AssetClass,
    provider: str,
    vendor_symbol: str,
) -> None:
    with pytest.raises(RouteCompileError, match="does not match"):
        InstrumentRouteInput(
            symbol=symbol,
            asset_class=asset_class,
            asset_type="test_asset",
            quote_poll_seconds=60,
            history_enabled=False,
            provider_symbols={provider: vendor_symbol},
        )


def test_coingecko_quote_route_accepts_direct_usd_output() -> None:
    item = InstrumentRouteInput(
        symbol="AVAX:USD",
        asset_class=AssetClass.CRYPTO,
        asset_type="spot_crypto",
        quote_poll_seconds=60,
        history_enabled=False,
        provider_symbols={"coingecko": "avalanche-2"},
        routes={Capability.QUOTE: ("coingecko",)},
    )

    plan = compile_route_plan((item,), available_providers={"coingecko"})

    assert plan.providers_for("AVAX:USD", Capability.QUOTE) == ("coingecko",)


@pytest.mark.asyncio
async def test_coingecko_explicit_usd_quote_and_history_routes_compile_into_the_graph() -> None:
    definition = ManagedInstrumentDefinition(
        id="custom-avax-usd",
        symbol="AVAX:USD",
        base="AVAX",
        quote="USD",
        name="Avalanche",
        description="Avalanche spot price quoted in United States dollars.",
        asset_class=AssetClass.CRYPTO,
        asset_type="spot_crypto",
        price_basis="aggregated_spot",
        ownership=InstrumentOwnership.CUSTOM,
        quote_poll_seconds=300,
        stale_after_seconds=900,
        routes=(
            CapabilityRoute(capability="quote", providers=("coingecko",)),
            CapabilityRoute(capability="history", providers=("coingecko",)),
        ),
        provider_symbols=(ProviderSymbolBinding(provider="coingecko", symbol="avalanche-2"),),
    )
    generation = CatalogGeneration.build((definition,))
    graph, plan = build_compiled_provider_graph(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            coingecko_api_key="key",
        ),
        generation.to_registry(),
        generation.definitions,
        strict=True,
    )
    try:
        assert plan.providers_for("AVAX:USD", Capability.QUOTE) == ("coingecko",)
        assert plan.providers_for("AVAX:USD", Capability.HISTORY) == ("coingecko",)
        assert "AVAX:USD" in graph.providers["coingecko"].history_symbols
    finally:
        await graph.close()


@pytest.mark.parametrize("capability", [Capability.QUOTE, Capability.HISTORY])
def test_coingecko_market_route_rejects_unsupported_quote_asset(
    capability: Capability,
) -> None:
    with pytest.raises(RouteCompileError, match="does not match"):
        InstrumentRouteInput(
            symbol="AVAX:EUR",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            quote_poll_seconds=60,
            history_enabled=capability is Capability.HISTORY,
            provider_symbols={"coingecko": "avalanche-2"},
            routes={capability: ("coingecko",)},
        )


def test_market_ratio_yield_requires_value_accrual() -> None:
    with pytest.raises(RouteCompileError, match="requires value_accruing"):
        compile_route_plan(
            (
                InstrumentRouteInput(
                    symbol="LST:USDC",
                    asset_class=AssetClass.CRYPTO,
                    asset_type="staking_token",
                    quote_poll_seconds=60,
                    history_enabled=False,
                    yield_strategy=YieldStrategy.STAKING_PROVIDER_METRIC,
                    underlying_asset="ETH",
                    reward_accrual_mode=RewardAccrualMode.REBASING_BALANCE,
                    provider_symbols={"binance": "LSTUSDC"},
                    routes={
                        Capability.QUOTE: ("binance",),
                        Capability.YIELD: ("staking_market_ratio_proxy",),
                    },
                ),
            ),
            available_providers={"binance", "staking_market_ratio_proxy"},
        )


@pytest.mark.parametrize("include_underlying", [False, True])
def test_market_ratio_yield_requires_an_active_underlying_history_route(
    include_underlying: bool,
) -> None:
    staking = InstrumentRouteInput(
        symbol="LST:USDC",
        asset_class=AssetClass.CRYPTO,
        asset_type="staking_token",
        quote_poll_seconds=60,
        yield_strategy=YieldStrategy.STAKING_PROVIDER_METRIC,
        underlying_asset="ETH",
        reward_accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
        provider_symbols={"binance": "LSTUSDC"},
        routes={
            Capability.QUOTE: ("binance",),
            Capability.HISTORY: ("binance",),
            Capability.YIELD: ("staking_market_ratio_proxy",),
        },
    )
    definitions = [staking]
    if include_underlying:
        definitions.append(
            InstrumentRouteInput(
                symbol="ETH:USDC",
                asset_class=AssetClass.CRYPTO,
                asset_type="spot_crypto",
                quote_poll_seconds=60,
                history_enabled=False,
                provider_symbols={"binance": "ETHUSDC"},
                routes={Capability.QUOTE: ("binance",)},
            )
        )

    message = "no usable history route" if include_underlying else "is not active"
    with pytest.raises(RouteCompileError, match=message):
        compile_route_plan(
            definitions,
            available_providers={"binance", "staking_market_ratio_proxy"},
        )


def test_synthetic_cycle_and_credit_budget_are_rejected() -> None:
    left = InstrumentRouteInput(
        symbol="LEFT:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=5,
        history_enabled=False,
        synthetic=SyntheticRouteInput("inverse", ("RIGHT:USD",)),
    )
    right = InstrumentRouteInput(
        symbol="RIGHT:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=5,
        history_enabled=False,
        synthetic=SyntheticRouteInput("inverse", ("LEFT:USD",)),
    )
    with pytest.raises(RouteCompileError, match="cycle"):
        compile_route_plan((left, right), available_providers={"synthetic"})

    paid = InstrumentRouteInput(
        symbol="TEST:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=60,
        history_enabled=False,
        provider_symbols={"finnhub": "TEST"},
        routes={Capability.QUOTE: ("finnhub",)},
    )
    with pytest.raises(RouteCompileError, match="exceed budget"):
        compile_route_plan(
            (paid,),
            available_providers={"finnhub"},
            daily_credit_limits={"finnhub": Decimal("10")},
        )


def _synthetic_chain(depth: int) -> tuple[InstrumentRouteInput, ...]:
    items = [
        InstrumentRouteInput(
            symbol="AAPL:USD",
            asset_class=AssetClass.EQUITY,
            asset_type="common_stock",
            quote_poll_seconds=60,
            history_enabled=False,
            provider_symbols={"finnhub": "AAPL"},
            routes={Capability.QUOTE: ("finnhub",)},
        )
    ]
    dependency = "AAPL:USD"
    for level in range(1, depth + 1):
        symbol = f"SYN{level}:USD"
        items.append(
            InstrumentRouteInput(
                symbol=symbol,
                asset_class=AssetClass.EQUITY,
                asset_type="synthetic_equity",
                quote_poll_seconds=60,
                history_enabled=False,
                synthetic=SyntheticRouteInput("inverse", (dependency,)),
            )
        )
        dependency = symbol
    return tuple(items)


@pytest.mark.parametrize("reverse", [False, True])
def test_synthetic_depth_four_is_order_independent_and_allowed(reverse: bool) -> None:
    items = _synthetic_chain(4)
    plan = compile_route_plan(
        tuple(reversed(items)) if reverse else items,
        available_providers={"finnhub", "synthetic"},
    )

    assert plan.providers_for("SYN4:USD", Capability.QUOTE) == ("synthetic",)


@pytest.mark.parametrize("reverse", [False, True])
def test_synthetic_depth_five_is_order_independent_and_rejected(reverse: bool) -> None:
    items = _synthetic_chain(5)

    with pytest.raises(RouteCompileError, match="synthetic dependency depth exceeds 4"):
        compile_route_plan(
            tuple(reversed(items)) if reverse else items,
            available_providers={"finnhub", "synthetic"},
        )


def test_builtin_policy_preserves_special_and_fx_routes() -> None:
    assert builtin_provider_policy("BTC:USDC").routes["quote"] == (
        "binance",
        "kraken",
        "coingecko",
    )
    assert builtin_provider_policy("WBETH:USDC").routes["quote"][:2] == (
        "synthetic_wbeth_primary",
        "synthetic_wbeth_alternate",
    )
    assert builtin_provider_policy("EUR:GBP").routes == {
        "quote": ("synthetic_fx",),
        "history": ("synthetic_fx_history",),
    }


def test_adapters_accept_instance_level_bindings_without_mutating_defaults() -> None:
    binance = BinanceProvider(symbol_bindings={"AVAX:USDC": "AVAXUSDC"})
    kraken = KrakenProvider(symbol_bindings={"AVAX:USDC": "AVAXUSDC"})
    alpaca = AlpacaProvider("key", "secret", symbol_bindings={"V:USD": "V"})
    finnhub = FinnhubProvider("key", symbol_bindings={"V:USD": "V"})
    twelve = TwelveDataProvider("key", symbol_bindings={"V:USD": "V"})
    alpha = AlphaVantageProvider("key", equity_symbol_bindings={"V:USD": "V"})
    coingecko = CoinGeckoProvider("key", coin_ids={"AVAX:USDC": "avalanche-2"})
    okx = OkxMarketProvider(market_bindings={"AVAX:USDC": "AVAX-USDC"}, internal_aliases={})
    fred = FredProvider(
        "key",
        series_bindings={"BOND:USD": "DGS10"},
        expense_ratios={"BOND:USD": "0.10"},
        method_bindings={"BOND:USD": "treasury_series_proxy_minus_expense"},
        component_role_bindings={"BOND:USD": "treasury_yield_percent"},
    )

    assert binance._exchange_symbol("AVAX:USDC") == "AVAXUSDC"
    assert kraken._pair("AVAX:USDC") == ("AVAXUSDC", "AVAX/USDC")
    assert alpaca._ticker("V:USD") == finnhub._ticker("V:USD") == "V"
    assert twelve._vendor_symbol("V:USD") == "V"
    assert alpha.equity_symbols == {"V:USD": "V"}
    assert coingecko._coin("AVAX:USDC") == "avalanche-2"
    assert okx._market("AVAX:USDC") == ("AVAX:USDC", "AVAX-USDC")
    assert fred.series_bindings == {"BOND:USD": "DGS10"}
    assert "AVAX:USDC" not in BinanceProvider.symbols


@pytest.mark.asyncio
async def test_compiled_graph_installs_a_custom_bound_symbol() -> None:
    definition = ManagedInstrumentDefinition(
        id="custom-avax-usdc",
        symbol="AVAX:USDC",
        base="AVAX",
        quote="USDC",
        name="Avalanche",
        description="Avalanche spot price quoted in USD Coin.",
        asset_class=AssetClass.CRYPTO,
        asset_type="spot_crypto",
        price_basis="last_trade",
        ownership=InstrumentOwnership.CUSTOM,
        quote_poll_seconds=1,
        stale_after_seconds=10,
        routes=(
            CapabilityRoute(capability="quote", providers=("binance",)),
            CapabilityRoute(capability="history", providers=("binance",)),
        ),
        provider_symbols=(
            ProviderSymbolBinding(provider="binance", symbol="AVAXUSDC"),
            ProviderSymbolBinding(provider="coingecko", symbol="avalanche-2"),
        ),
    )
    generation = CatalogGeneration.build((definition,))
    registry = generation.to_registry()

    graph, plan = build_compiled_provider_graph(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            coingecko_api_key="coingecko-key",
        ),
        registry,
        generation.definitions,
        strict=True,
    )
    try:
        provider = graph.router.providers_for("AVAX:USDC", Capability.QUOTE)[0]
        assert provider is graph.providers["binance"]
        assert provider._exchange_symbol("AVAX:USDC") == "AVAXUSDC"
        assert "AVAX:USDC" not in graph.providers["coingecko"].coin_ids
        assert plan.providers_for("AVAX:USDC", Capability.HISTORY) == ("binance",)
        assert graph.router.providers_for("BTC:USDC", Capability.QUOTE)
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_compiled_graph_synthesizes_a_dynamic_fx_cross_through_usd_spokes() -> None:
    definition = ManagedInstrumentDefinition(
        id="custom-cad-jpy",
        symbol="CAD:JPY",
        base="CAD",
        quote="JPY",
        name="Canadian Dollar / Japanese Yen",
        description="The value of one Canadian dollar expressed in Japanese yen.",
        asset_class=AssetClass.FX,
        asset_type="forex_pair",
        price_basis="synthetic_cross",
        ownership=InstrumentOwnership.CUSTOM,
        quote_poll_seconds=240,
        stale_after_seconds=1_200,
        routes=(
            CapabilityRoute(capability="quote", providers=("synthetic_fx",)),
            CapabilityRoute(capability="history", providers=("synthetic_fx",)),
        ),
    )
    generation = CatalogGeneration.build((definition,))
    registry = generation.to_registry()
    graph, _ = build_compiled_provider_graph(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            twelve_data_api_key="key",
        ),
        registry,
        generation.definitions,
        strict=True,
    )
    try:
        twelve = graph.providers["twelve_data"]
        observed_at = datetime.now(UTC).isoformat()

        async def request_json(*_args, **kwargs):
            vendor_symbol = kwargs["params"]["symbol"]
            prices = {"USD/JPY": "150", "USD/CAD": "1.35"}
            return {
                "status": "ok",
                "values": [{"datetime": observed_at, "close": prices[vendor_symbol]}],
            }

        twelve._request_json = request_json
        quote = await graph.router.get_quote("CAD:JPY")

        assert quote.price == Decimal("150") / Decimal("1.35")
        assert tuple(component.symbol for component in quote.components) == (
            "USD:JPY",
            "USD:CAD",
        )
        assert graph.router.providers_for("USD:CAD", Capability.QUOTE) == (twelve,)
        assert graph.router.providers_for("USD:JPY", Capability.HISTORY) == (twelve,)
    finally:
        await graph.close()


def _custom_growth_bond(
    *,
    strategy: YieldStrategy = YieldStrategy.TREASURY_PROXY_MINUS_EXPENSE,
    series: str = "DGS3MO",
    explicit_series: str | None = None,
) -> ManagedInstrumentDefinition:
    bindings = [ProviderSymbolBinding(provider="alpaca", symbol="TBND")]
    if explicit_series is not None:
        bindings.append(ProviderSymbolBinding(provider="fred", symbol=explicit_series))
    return ManagedInstrumentDefinition(
        id="custom-tbnd-usd",
        symbol="TBND:USD",
        base="TBND",
        quote="USD",
        name="Test Treasury Bond Fund",
        description="A test growth bond fund backed by a controlled Treasury proxy.",
        asset_class=AssetClass.BOND,
        asset_type="growth_bond_etf",
        price_basis="last_trade",
        ownership=InstrumentOwnership.CUSTOM,
        quote_poll_seconds=60,
        stale_after_seconds=300,
        routes=(
            CapabilityRoute(capability="quote", providers=("alpaca",)),
            CapabilityRoute(capability="history", providers=("alpaca",)),
            CapabilityRoute(capability="yield", providers=("fred",)),
        ),
        provider_symbols=tuple(bindings),
        income=IncomePolicy(
            yield_strategy=strategy,
            fred_series=series,
            expense_ratio_percent=0.15,
        ),
    )


def test_fred_binding_is_derived_only_from_the_income_policy() -> None:
    definition = _custom_growth_bond(series="DGS3MO", explicit_series="DGS1")

    with pytest.raises(RouteCompileError, match=r"must match IncomePolicy\.fred_series"):
        instrument_route_input_from_definition(definition)


def test_legacy_three_month_strategy_cannot_change_maturity() -> None:
    with pytest.raises(ValueError, match="requires DGS3MO"):
        IncomePolicy(
            yield_strategy=YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE,
            fred_series="DGS1",
            expense_ratio_percent=0.15,
        )


@pytest.mark.asyncio
async def test_general_treasury_strategy_keeps_its_method_when_using_dgs3mo() -> None:
    definition = _custom_growth_bond()
    generation = CatalogGeneration.build((definition,))
    graph, plan = build_compiled_provider_graph(
        Settings(
            require_free_threaded=False,
            background_enabled=False,
            alpaca_api_key="key",
            alpaca_api_secret="secret",
            fred_api_key="fred-key",
        ),
        generation.to_registry(),
        generation.definitions,
        strict=True,
    )
    try:
        fred = graph.providers["fred"]

        async def request_json(*_args, **_kwargs):
            return {"observations": [{"date": "2026-07-20", "value": "4.25"}]}

        fred._request_json = request_json
        metric = await graph.router.get_yield("TBND:USD")

        assert plan.instrument("TBND:USD").provider_symbols["fred"] == "DGS3MO"
        assert metric.method == "treasury_series_proxy_minus_expense"
        assert metric.components[0].symbol == "DGS3MO"
        assert metric.value == Decimal("4.10")
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_seeded_builtin_catalog_compiles_strictly_without_optional_keys(tmp_path) -> None:
    store = InstrumentPolicyStore(tmp_path / "instruments.json", build_registry())
    generation = store.active_generation()
    registry = generation.to_registry()

    graph, plan = build_compiled_provider_graph(
        Settings(require_free_threaded=False, background_enabled=False),
        registry,
        generation.definitions,
        strict=True,
    )
    try:
        assert len(generation.definitions) == len(plan.instruments) == 54
        assert plan.providers_for("BTC:USDC", Capability.QUOTE) == (
            "binance",
            "kraken",
        )
        assert plan.providers_for("EUR:GBP", Capability.QUOTE) == ()
        assert graph.router.providers_for("BTC:USDC", Capability.QUOTE)
    finally:
        await graph.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_settings",
    [
        {},
        {"coingecko_api_key": "coingecko-key"},
        {
            "alpaca_api_key": "alpaca-key",
            "alpaca_api_secret": "alpaca-secret",
            "finnhub_api_key": "finnhub-key",
            "twelve_data_api_key": "twelve-key",
            "alpha_vantage_api_key": "alpha-key",
            "coingecko_api_key": "coingecko-key",
            "fred_api_key": "fred-key",
            "binance_api_key": "binance-key",
            "binance_api_secret": "binance-secret",
            "ethereum_rpc_urls": ("https://ethereum-mainnet.invalid",),
        },
    ],
    ids=("public-only", "coingecko", "all-providers"),
)
async def test_all_54_builtin_routes_match_the_legacy_graph(
    tmp_path,
    provider_settings: dict[str, object],
) -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        **provider_settings,
    )
    generation = InstrumentPolicyStore(
        tmp_path / "instruments.json",
        build_registry(),
    ).active_generation()
    legacy = build_provider_graph(settings)
    compiled, _ = build_compiled_provider_graph(
        settings,
        generation.to_registry(),
        generation.definitions,
        strict=False,
    )
    try:
        assert len(generation.definitions) == 54
        for definition in generation.definitions:
            for capability in Capability:
                legacy_names = tuple(
                    provider.name
                    for provider in legacy.router.providers_for(definition.symbol, capability)
                )
                compiled_names = tuple(
                    provider.name
                    for provider in compiled.router.providers_for(definition.symbol, capability)
                )
                assert compiled_names == legacy_names, (
                    definition.symbol,
                    capability.value,
                    legacy_names,
                    compiled_names,
                )
    finally:
        await legacy.close()
        await compiled.close()


def test_credit_estimate_is_numeric_and_json_safe() -> None:
    estimate = estimate_daily_credits(
        "finnhub",
        Capability.QUOTE,
        poll_seconds=60,
    )
    assert estimate["requests_per_day"] == 1_440
    assert estimate["estimated_credits_per_day"] == 1_440.0


def test_coingecko_quote_credits_are_shared_across_the_batch_cache() -> None:
    items = tuple(
        InstrumentRouteInput(
            symbol=f"{base}:USDC",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            quote_poll_seconds=1,
            history_enabled=False,
            provider_symbols={"coingecko": coin_id},
            routes={Capability.QUOTE: ("coingecko",)},
        )
        for base, coin_id in (("AVAX", "avalanche-2"), ("LINK", "chainlink"))
    )

    plan = compile_route_plan(items, available_providers={"coingecko"})

    assert plan.estimated_daily_credits["coingecko"] == Decimal(288)
    assert len(plan.credit_estimates) == 1
    assert plan.credit_estimates[0].symbol == "*"
    assert "shared_batch_cache" in plan.credit_estimates[0].bases


def test_coingecko_credit_plan_counts_two_thousand_dynamic_id_batches() -> None:
    items = tuple(
        InstrumentRouteInput(
            symbol=f"C{index:04d}:USDC",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            quote_poll_seconds=300,
            history_enabled=False,
            provider_symbols={"coingecko": f"coin-{index:04d}"},
            routes={Capability.QUOTE: ("coingecko",)},
        )
        for index in range(2_000)
    )

    plan = compile_route_plan(items, available_providers={"coingecko"})
    batch_count = len(
        coingecko_simple_price_id_batches(
            (*(item.provider_symbols["coingecko"] for item in items), "usd-coin")
        )
    )

    assert batch_count == 9
    assert plan.estimated_daily_credits["coingecko"] == Decimal(288 * batch_count)
    assert plan.credit_estimates[0].requests_per_day == 288 * batch_count
    assert f"shared_batch_count:{batch_count}" in plan.credit_estimates[0].bases


def test_credit_plan_includes_hidden_fx_spokes() -> None:
    item = InstrumentRouteInput(
        symbol="CAD:JPY",
        asset_class=AssetClass.FX,
        asset_type="forex_pair",
        quote_poll_seconds=240,
        history_poll_seconds=3_600,
        routes={
            Capability.QUOTE: ("synthetic_fx",),
            Capability.HISTORY: ("synthetic_fx",),
        },
    )

    plan = compile_route_plan(
        (item,),
        available_providers={"synthetic_fx", "twelve_data"},
    )

    assert plan.estimated_daily_credits["twelve_data"] == Decimal(868)
    assert plan.committed_daily_credits_by_scope == {"twelve_data": {"fx_reserved": Decimal(868)}}
    assert {line.symbol for line in plan.credit_estimates} == {"USD:CAD", "USD:JPY"}
    assert all(
        any(basis.startswith("fx_spoke_dependency") for basis in line.bases)
        for line in plan.credit_estimates
    )


def test_history_credit_plan_counts_intervals_and_cold_backfill_pages() -> None:
    item = InstrumentRouteInput(
        symbol="AAPL:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=60,
        history_poll_seconds=3_600,
        history_backfill_days=400,
        provider_symbols={"alpaca": "AAPL", "twelve_data": "AAPL"},
        routes={
            Capability.QUOTE: ("alpaca",),
            Capability.HISTORY: ("twelve_data",),
        },
    )

    plan = compile_route_plan(
        (item,),
        available_providers={"alpaca", "twelve_data"},
    )

    [estimate] = plan.credit_estimates
    assert estimate.provider == "twelve_data"
    assert estimate.capability is Capability.HISTORY
    assert estimate.cycles_per_day == 24
    assert estimate.cold_start_requests == 5
    assert estimate.steady_state_requests_per_cycle == 3
    assert estimate.requests_per_day == 74
    assert estimate.estimated_credits_per_day == Decimal(74)
    assert estimate.quota_scopes == ("general",)
    assert plan.as_dict()["credit_plan"]["estimates"][0]["cold_start_requests"] == 5


def test_twelve_credit_admission_preserves_fx_reserve_and_active_baseline() -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        twelve_data_api_key="key",
        twelve_daily_credits=790,
        twelve_fx_reserve_credits=769,
    )
    equity = InstrumentRouteInput(
        symbol="AAPL:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=3_600,
        history_enabled=False,
        provider_symbols={"twelve_data": "AAPL"},
        routes={Capability.QUOTE: ("twelve_data",)},
    )
    fx = InstrumentRouteInput(
        symbol="USD:CAD",
        asset_class=AssetClass.FX,
        asset_type="forex_pair",
        quote_poll_seconds=3_600,
        history_enabled=False,
        provider_symbols={"twelve_data": "USD/CAD"},
        routes={Capability.QUOTE: ("twelve_data",)},
    )
    equity_plan = compile_route_plan(
        (equity,),
        settings=settings,
        available_providers={"twelve_data"},
    )
    replacement_equity = InstrumentRouteInput(
        symbol="MSFT:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=3_600,
        history_enabled=False,
        provider_symbols={"twelve_data": "MSFT"},
        routes={Capability.QUOTE: ("twelve_data",)},
    )
    replacement_plan = compile_route_plan(
        (replacement_equity,),
        settings=settings,
        available_providers={"twelve_data"},
    )
    fx_plan = compile_route_plan(
        (fx,),
        settings=settings,
        available_providers={"twelve_data"},
    )

    assert equity_plan.committed_daily_credits["twelve_data"] == Decimal(24)
    assert equity_plan.committed_daily_credits_by_scope["twelve_data"] == {"general": Decimal(24)}
    assert any("non-FX" in error for error in incremental_credit_budget_errors(None, equity_plan))
    assert incremental_credit_budget_errors(equity_plan, equity_plan) == ()
    assert any(
        "non-FX" in error
        for error in incremental_credit_budget_errors(equity_plan, replacement_plan)
    )
    assert fx_plan.committed_daily_credits_by_scope["twelve_data"] == {"fx_reserved": Decimal(24)}
    assert incremental_credit_budget_errors(None, fx_plan) == ()


def test_twelve_credit_admission_applies_total_cap_to_reserved_fx_demand() -> None:
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        twelve_data_api_key="key",
        twelve_daily_credits=790,
        twelve_fx_reserve_credits=769,
    )
    high_frequency_fx = InstrumentRouteInput(
        symbol="USD:JPY",
        asset_class=AssetClass.FX,
        asset_type="forex_pair",
        quote_poll_seconds=60,
        history_enabled=False,
        provider_symbols={"twelve_data": "USD/JPY"},
        routes={Capability.QUOTE: ("twelve_data",)},
    )
    plan = compile_route_plan(
        (high_frequency_fx,),
        settings=settings,
        available_providers={"twelve_data"},
    )

    assert any("total budget" in error for error in incremental_credit_budget_errors(None, plan))


def test_credit_plan_counts_synthetic_component_requests() -> None:
    source = InstrumentRouteInput(
        symbol="AAPL:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=60,
        history_enabled=False,
        provider_symbols={"finnhub": "AAPL"},
        routes={Capability.QUOTE: ("finnhub",)},
    )
    synthetic = InstrumentRouteInput(
        symbol="AAPLI:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="synthetic_equity",
        quote_poll_seconds=60,
        history_enabled=False,
        synthetic=SyntheticRouteInput("inverse", ("AAPL:USD",)),
    )

    plan = compile_route_plan(
        (source, synthetic),
        available_providers={"finnhub", "synthetic"},
    )

    assert plan.estimated_daily_credits["finnhub"] == Decimal(2_880)
    assert {basis for line in plan.credit_estimates for basis in line.bases} == {
        "direct_route",
        "synthetic_dependency:AAPLI:USD",
    }


def test_credit_plan_gates_primary_demand_and_reports_fallback_worst_case() -> None:
    item = InstrumentRouteInput(
        symbol="AAPL:USD",
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        quote_poll_seconds=60,
        history_enabled=False,
        provider_symbols={"finnhub": "AAPL", "twelve_data": "AAPL"},
        routes={Capability.QUOTE: ("finnhub", "twelve_data")},
    )
    settings = Settings(
        require_free_threaded=False,
        background_enabled=False,
        finnhub_api_key="key",
        twelve_data_api_key="key",
        twelve_daily_credits=790,
    )

    plan = compile_route_plan(
        (item,),
        settings=settings,
        available_providers={"finnhub", "twelve_data"},
    )

    assert plan.estimated_daily_credits == plan.committed_daily_credits
    assert plan.committed_daily_credits == {"finnhub": Decimal(1_440)}
    assert plan.worst_case_daily_credits == {
        "finnhub": Decimal(1_440),
        "twelve_data": Decimal(1_440),
    }
    assert plan.hard_capped_daily_credits == {
        "finnhub": Decimal(1_440),
        "twelve_data": Decimal(790),
    }


def test_catalog_synthetic_model_accepts_only_restricted_operations() -> None:
    recipe = SyntheticRecipeDefinition(
        operation=SyntheticOperation.DIVIDE,
        inputs=("BTC:USDC", "ETH:USDC"),
    )
    assert recipe.operation is SyntheticOperation.DIVIDE
