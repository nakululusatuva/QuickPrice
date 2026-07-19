from __future__ import annotations

import copy
import hashlib
import json
import os

import pytest

import quickprice.catalog as catalog_module
from quickprice.catalog import (
    CapabilityRoute,
    CatalogGeneration,
    CatalogValidationError,
    IncomePolicy,
    InstrumentOwnership,
    ManagedInstrumentDefinition,
    ProviderSymbolBinding,
    SyntheticOperation,
    SyntheticRecipeDefinition,
    definition_from_payload,
)
from quickprice.domain import RewardAccrualMode
from quickprice.managed_config import (
    InstrumentPolicyStore,
    ManagedConfigurationError,
    RevisionConflictError,
    apply_instrument_policy,
)
from quickprice.plugin_api import YieldStrategy
from quickprice.registry import build_registry


def _custom_payload(symbol: str = "ADA:USDC", **updates: object) -> dict[str, object]:
    base, quote = symbol.split(":")
    payload: dict[str, object] = {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "name": f"{base} spot",
        "description": f"{base} spot price quoted in {quote}.",
        "asset_class": "crypto",
        "asset_type": "spot_crypto",
        "price_basis": "last_trade",
        "routes": [],
        "provider_symbols": [{"provider": "binance", "symbol": f"{base}{quote}"}],
    }
    payload.update(updates)
    return payload


def _definition(symbol: str, **updates: object) -> ManagedInstrumentDefinition:
    payload = _custom_payload(symbol, **updates)
    payload.update(
        id=f"custom-{symbol.lower().replace(':', '-')}",
        ownership=InstrumentOwnership.CUSTOM.value,
    )
    return definition_from_payload(payload)


def test_version_one_policy_is_atomically_migrated_without_behavior_change(tmp_path) -> None:
    path = tmp_path / "instruments.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "disabled_symbols": ["btc/usdc"],
                "overrides": {
                    "ETH:USDC": {
                        "quote_poll_seconds": 2,
                        "stale_after_seconds": 30,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = InstrumentPolicyStore(path, build_registry())

    serialized = json.loads(path.read_text(encoding="utf-8"))
    assert serialized["version"] == 2
    assert (
        json.loads((tmp_path / "instruments.json.v1-backup").read_text(encoding="utf-8"))["version"]
        == 1
    )
    assert store.active_generation().by_symbol()["BTC:USDC"].enabled is False
    assert store.active_generation().by_symbol()["ETH:USDC"].quote_poll_seconds == 2
    assert all(
        any(route.capability == "quote" for route in item.routes)
        for item in store.active_generation().instruments
    )
    active = apply_instrument_policy(build_registry(), path)
    assert "BTC:USDC" not in active
    assert active["ETH:USDC"].stale_after_seconds == 30


def test_version_one_migration_can_be_deferred_until_startup_succeeds(tmp_path) -> None:
    path = tmp_path / "instruments.json"
    original = b'{"version":1,"disabled_symbols":["BTC:USDC"],"overrides":{}}\n'
    path.write_bytes(original)

    store = InstrumentPolicyStore(path, build_registry(), defer_migration=True)

    assert path.read_bytes() == original
    assert store.active_generation().by_symbol()["BTC:USDC"].enabled is False
    store.persist_migration()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
    assert (tmp_path / "instruments.json.v1-backup").read_bytes() == original


def test_version_one_migration_preserves_an_intentionally_empty_active_catalog(tmp_path) -> None:
    path = tmp_path / "instruments.json"
    installed = build_registry()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "disabled_symbols": list(installed.symbols),
                "overrides": {},
            }
        ),
        encoding="utf-8",
    )

    store = InstrumentPolicyStore(path, installed, defer_migration=True)

    assert store.active_generation().to_registry().symbols == ()
    assert store.validate()["counts"]["active"] == 0
    store.persist_migration()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
    assert store.active_generation().to_registry().symbols == ()


def test_legacy_v2_rebasing_ratio_fallback_is_normalized_before_validation(tmp_path) -> None:
    path = tmp_path / "instruments.json"
    InstrumentPolicyStore(path, build_registry())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged"] = copy.deepcopy(payload["active"])
    payload["last_known_good"] = copy.deepcopy(payload["active"])

    for generation_name in ("active", "staged", "last_known_good"):
        generation = payload[generation_name]
        steth = next(item for item in generation["instruments"] if item["symbol"] == "STETH:USDC")
        steth["income"]["fallback_ratio_days"] = 30
        yield_route = next(route for route in steth["routes"] if route["capability"] == "yield")
        yield_route["providers"] = ["lido", "staking_market_ratio_proxy"]
        revision_payload = {"instruments": generation["instruments"]}
        generation["revision"] = hashlib.sha256(
            json.dumps(
                revision_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = InstrumentPolicyStore(path, build_registry(), defer_migration=True)

    for generation in (
        store.active_generation(),
        store.staged_generation(),
        store.last_known_good_generation(),
    ):
        assert generation is not None
        assert generation.by_symbol()["STETH:USDC"].income.fallback_ratio_days is None
    store.persist_migration()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    for generation_name in ("active", "staged", "last_known_good"):
        steth = next(
            item
            for item in persisted[generation_name]["instruments"]
            if item["symbol"] == "STETH:USDC"
        )
        assert steth["income"]["fallback_ratio_days"] is None

    with pytest.raises(ValueError, match="only valid for value-accruing"):
        IncomePolicy(
            yield_strategy=YieldStrategy.STAKING_PROVIDER_METRIC,
            reward_accrual_mode=RewardAccrualMode.REBASING_BALANCE,
            underlying_asset="ETH",
            fallback_ratio_days=30,
        )


def test_builtin_history_defaults_migrate_only_null_catalog_values(tmp_path) -> None:
    path = tmp_path / "instruments.json"
    InstrumentPolicyStore(path, build_registry())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged"] = copy.deepcopy(payload["active"])
    payload["last_known_good"] = copy.deepcopy(payload["active"])

    for generation_name in ("active", "staged", "last_known_good"):
        generation = payload[generation_name]
        by_symbol = {item["symbol"]: item for item in generation["instruments"]}
        by_symbol["BETH:USDC"]["history"]["poll_seconds"] = None
        by_symbol["STETH:USDC"]["history"]["poll_seconds"] = None
        by_symbol["WSTETH:USDC"]["history"]["poll_seconds"] = 7_200.0
        by_symbol["BTC:USDC"]["history"]["poll_seconds"] = None
        generation["revision"] = hashlib.sha256(
            json.dumps(
                {"instruments": generation["instruments"]},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = InstrumentPolicyStore(path, build_registry(), defer_migration=True)

    for generation in (
        store.active_generation(),
        store.staged_generation(),
        store.last_known_good_generation(),
    ):
        assert generation is not None
        by_symbol = generation.by_symbol()
        assert by_symbol["BETH:USDC"].history.poll_seconds == 21_600.0
        assert by_symbol["STETH:USDC"].history.poll_seconds == 21_600.0
        assert by_symbol["WSTETH:USDC"].history.poll_seconds == 7_200.0
        assert by_symbol["BTC:USDC"].history.poll_seconds is None

    store.persist_migration()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    for generation_name in ("active", "staged", "last_known_good"):
        by_symbol = {item["symbol"]: item for item in persisted[generation_name]["instruments"]}
        assert by_symbol["BETH:USDC"]["history"]["poll_seconds"] == 21_600.0
        assert by_symbol["WSTETH:USDC"]["history"]["poll_seconds"] == 7_200.0


def test_managed_definition_round_trip_preserves_history_poll_policy() -> None:
    item = build_registry()["BETH:USDC"]
    definition = ManagedInstrumentDefinition.from_instrument_spec(
        item,
        instrument_id="builtin-beth-usdc",
        ownership=InstrumentOwnership.BUILTIN,
    )

    assert definition.history.poll_seconds == 21_600.0
    assert definition.to_instrument_spec().history_poll_seconds == 21_600.0


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
def test_catalog_rejects_a_file_writable_by_another_account(tmp_path) -> None:
    path = tmp_path / "instruments.json"
    path.write_text('{"version":1,"disabled_symbols":[],"overrides":{}}', encoding="utf-8")
    path.chmod(0o666)

    with pytest.raises(ManagedConfigurationError, match="writable by another account"):
        InstrumentPolicyStore(path, build_registry())


def test_staged_activation_last_known_good_and_rollback(tmp_path) -> None:
    store = InstrumentPolicyStore(tmp_path / "instruments.json", build_registry())
    initial = store.catalog_snapshot()

    staged = store.create_instrument(_custom_payload(), initial["revision"])
    custom = next(item for item in staged["staged"]["instruments"] if item["symbol"] == "ADA:USDC")
    assert custom["id"].startswith("custom-")
    assert "ADA:USDC" not in store.active_generation().by_symbol()

    activated = store.activate_staged(
        staged["revision"],
        expected_staged_revision=staged["staged_revision"],
    )
    assert "ADA:USDC" in store.active_generation().by_symbol()
    assert activated["last_known_good_revision"] == initial["active_revision"]

    rolled_back = store.rollback(activated["revision"])
    assert "ADA:USDC" not in store.active_generation().by_symbol()
    assert rolled_back["last_known_good_revision"] == activated["active_revision"]


def test_custom_archive_is_staged_and_registry_omits_archived_definition(tmp_path) -> None:
    store = InstrumentPolicyStore(tmp_path / "instruments.json", build_registry())
    created = store.create_instrument(_custom_payload(), store.snapshot()["revision"])
    instrument_id = next(
        item["id"] for item in created["staged"]["instruments"] if item["symbol"] == "ADA:USDC"
    )
    activated = store.activate_staged(created["revision"])
    archived = store.archive_instrument(instrument_id, activated["revision"])
    staged_item = next(
        item for item in archived["staged"]["instruments"] if item["id"] == instrument_id
    )
    assert staged_item["archived"] is True
    assert staged_item["enabled"] is False
    final = store.activate_staged(archived["revision"])
    assert "ADA:USDC" not in store.active_generation().to_registry()
    assert any(item["id"] == instrument_id for item in final["active"]["instruments"])


def test_built_in_core_is_read_only_but_policy_and_route_order_are_editable(tmp_path) -> None:
    store = InstrumentPolicyStore(tmp_path / "instruments.json", build_registry())
    current = store.snapshot()
    bitcoin = store.active_generation().by_symbol()["BTC:USDC"]

    with pytest.raises(CatalogValidationError, match="read-only"):
        store.update_instrument(
            bitcoin.id,
            {"name": "Renamed Bitcoin"},
            current["revision"],
        )

    updated = store.update_instrument(
        bitcoin.id,
        {
            "quote_poll_seconds": 2,
            "stale_after_seconds": 20,
            "routes": [
                {
                    "capability": "quote",
                    "providers": ["kraken", "binance"],
                }
            ],
        },
        current["revision"],
    )
    staged = next(item for item in updated["staged"]["instruments"] if item["id"] == bitcoin.id)
    assert staged["quote_poll_seconds"] == 2
    assert staged["routes"][0]["providers"] == ["kraken", "binance"]

    with pytest.raises(CatalogValidationError, match="not archived"):
        store.archive_instrument(bitcoin.id, updated["revision"])


def test_catalog_mutations_use_file_revision_optimistic_concurrency(tmp_path) -> None:
    store = InstrumentPolicyStore(tmp_path / "instruments.json", build_registry())
    initial_revision = store.snapshot()["revision"]
    store.create_instrument(_custom_payload(), initial_revision)

    with pytest.raises(RevisionConflictError):
        store.create_instrument(_custom_payload("DOGE:USDC"), initial_revision)


def test_strict_schema_rejects_unknown_fields_urls_xss_and_long_provider_chains() -> None:
    payload = _custom_payload()
    payload.update(id="custom-ada", ownership="custom", arbitrary_url="https://example.test")
    with pytest.raises(CatalogValidationError, match="Extra inputs"):
        definition_from_payload(payload)

    unsafe = _custom_payload(description="<script>alert(1)</script>")
    unsafe.update(id="custom-ada", ownership="custom")
    with pytest.raises(CatalogValidationError, match="unsafe"):
        definition_from_payload(unsafe)

    url_binding = _custom_payload(
        provider_symbols=[{"provider": "binance", "symbol": "https://example.test"}]
    )
    url_binding.update(id="custom-ada", ownership="custom")
    with pytest.raises(CatalogValidationError, match=r"unsupported characters|URL"):
        definition_from_payload(url_binding)

    long_chain = _custom_payload(
        routes=[
            {
                "capability": "quote",
                "providers": ["one", "two", "three", "four", "five"],
            }
        ]
    )
    long_chain.update(id="custom-ada", ownership="custom")
    with pytest.raises(CatalogValidationError, match="1 to 4"):
        definition_from_payload(long_chain)

    string_interval = _custom_payload(quote_poll_seconds="5")
    string_interval.update(id="custom-ada", ownership="custom")
    with pytest.raises(CatalogValidationError, match="number"):
        definition_from_payload(string_interval)

    boolean_interval = _custom_payload(quote_poll_seconds=True)
    boolean_interval.update(id="custom-ada", ownership="custom")
    with pytest.raises(CatalogValidationError, match="number"):
        definition_from_payload(boolean_interval)


def test_synthetic_arity_cycles_and_depth_are_bounded() -> None:
    with pytest.raises(ValueError, match="exactly 1"):
        SyntheticRecipeDefinition(
            operation=SyntheticOperation.INVERSE,
            inputs=("BTC:USDC", "ETH:USDC"),
        )

    base = _definition("BASE:USD")
    left = _definition(
        "LEFT:USD",
        synthetic={"operation": "inverse", "inputs": ["RIGHT:USD"]},
    )
    right = _definition(
        "RIGHT:USD",
        synthetic={"operation": "inverse", "inputs": ["LEFT:USD"]},
    )
    with pytest.raises(ValueError, match="cycle"):
        CatalogGeneration.build((base, left, right))

    definitions = [base]
    dependency = "BASE:USD"
    for index in range(1, 6):
        symbol = f"SYN{index}:USD"
        definitions.append(
            _definition(
                symbol,
                synthetic={"operation": "inverse", "inputs": [dependency]},
            )
        )
        dependency = symbol
    with pytest.raises(ValueError, match="depth"):
        CatalogGeneration.build(definitions)


def test_import_round_trip_merge_replace_custom_and_revision_validation(tmp_path) -> None:
    store = InstrumentPolicyStore(tmp_path / "instruments.json", build_registry())
    created = store.create_instrument(_custom_payload(), store.snapshot()["revision"])
    active = store.activate_staged(created["revision"])
    exported = store.export_catalog()

    imported = store.import_catalog(exported, "merge", active["revision"])
    assert imported["staged_revision"] == exported["revision"]

    only_builtins = {
        "version": 2,
        "instruments": [
            item
            for item in exported["instruments"]
            if item["ownership"] == InstrumentOwnership.BUILTIN.value
        ],
    }
    replaced = store.import_catalog(only_builtins, "replace_custom", imported["revision"])
    assert all(
        item["ownership"] == InstrumentOwnership.BUILTIN.value
        for item in replaced["staged"]["instruments"]
    )

    tampered = dict(exported)
    tampered["instruments"] = [*exported["instruments"]]
    tampered["instruments"][0] = {
        **tampered["instruments"][0],
        "enabled": not tampered["instruments"][0]["enabled"],
    }
    with pytest.raises(CatalogValidationError, match="revision"):
        store.import_catalog(tampered, "merge", replaced["revision"])


def test_partial_merge_is_allowed_but_whole_generation_cannot_remove_builtins(tmp_path) -> None:
    store = InstrumentPolicyStore(tmp_path / "instruments.json", build_registry())
    current = store.snapshot()
    partial = {
        "version": 2,
        "instruments": [
            _definition("ADA:USDC").model_dump(mode="json"),
        ],
    }
    merged = store.import_catalog(partial, "merge", current["revision"])
    assert any(item["symbol"] == "ADA:USDC" for item in merged["staged"]["instruments"])

    only_custom = CatalogGeneration.build((_definition("DOGE:USDC"),))
    with pytest.raises(CatalogValidationError, match="retain every built-in"):
        store.replace_staged(only_custom, merged["revision"])


def test_custom_limit_and_catalog_file_size_are_enforced(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(catalog_module, "MAX_CUSTOM_INSTRUMENTS", 1)
    with pytest.raises(ValueError, match="custom instrument limit"):
        CatalogGeneration.build((_definition("ONE:USD"), _definition("TWO:USD")))

    path = tmp_path / "instruments.json"
    path.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(ManagedConfigurationError, match="too large"):
        InstrumentPolicyStore(path, build_registry())


def test_two_thousand_typical_custom_definitions_fit_the_bounded_import() -> None:
    definitions = tuple(_definition(f"ASSET{index}:USD") for index in range(2_000))
    generation = CatalogGeneration.build(definitions)
    payload = {
        "version": 2,
        "revision": generation.revision,
        "instruments": [item.model_dump(mode="json") for item in definitions],
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert len(encoded) < catalog_module.MAX_CATALOG_IMPORT_BYTES


def test_safe_provider_bindings_can_rely_on_compiler_selected_default_routes() -> None:
    definition = _definition("ADA:USDC")
    assert definition.routes == ()
    assert definition.provider_symbols == (
        ProviderSymbolBinding(provider="binance", symbol="ADAUSDC"),
    )
    assert CapabilityRoute(capability="quote", providers=("binance",)).providers == ("binance",)
