from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from quickprice.managed_config import (
    InstrumentPolicyStore,
    ManagedEnvironmentStore,
    ProviderKeyStore,
    RevisionConflictError,
    UnsupportedSettingError,
    apply_instrument_policy,
)
from quickprice.registry import build_registry


def test_managed_environment_is_allowlisted_revisioned_and_atomic(tmp_path) -> None:
    path = tmp_path / "quickprice.env"
    store = ManagedEnvironmentStore(path)
    initial = store.snapshot()
    updated = store.patch(
        updates={
            "QUICKPRICE_REQUESTS_PER_MINUTE": 240,
            "QUICKPRICE_RATE_LIMIT_ENABLED": True,
            "QUICKPRICE_PROVIDER_PROXY_NAMES": ["binance", "okx"],
        },
        removals=[],
        expected_revision=initial["revision"],
    )
    assert updated["restart_required"] is True
    assert path.stat().st_size > 0
    assert "QUICKPRICE_REQUESTS_PER_MINUTE=240" in path.read_text(encoding="utf-8")
    with pytest.raises(RevisionConflictError):
        store.patch(
            updates={"QUICKPRICE_REQUESTS_PER_MINUTE": 120},
            removals=[],
            expected_revision=initial["revision"],
        )
    with pytest.raises(UnsupportedSettingError):
        store.patch(
            updates={"QUICKPRICE_ADMIN_TOTP_SECRET": "forbidden"},
            removals=[],
            expected_revision=updated["revision"],
        )


def test_managed_environment_revision_check_is_atomic_between_threads(tmp_path) -> None:
    store = ManagedEnvironmentStore(tmp_path / "quickprice.env")
    revision = store.snapshot()["revision"]
    barrier = Barrier(2)

    def update(value: int):
        barrier.wait()
        return store.patch(
            updates={"QUICKPRICE_REQUESTS_PER_MINUTE": value},
            removals=[],
            expected_revision=revision,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(update, value) for value in (240, 360)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(("updated", future.result()))
            except RevisionConflictError:
                outcomes.append(("conflict", None))

    assert [name for name, _ in outcomes].count("updated") == 1
    assert [name for name, _ in outcomes].count("conflict") == 1


def test_provider_keys_are_write_only_and_reject_external_overrides(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-keys.env"
    store = ProviderKeyStore(path)
    initial = store.snapshot()
    secret = "sensitive-provider-secret"
    updated = store.patch(
        updates={"QUICKPRICE_FINNHUB_API_KEY": secret},
        removals=[],
        expected_revision=initial["revision"],
    )
    serialized = repr(updated)
    assert secret not in serialized
    finnhub = next(item for item in updated["keys"] if item["name"] == "QUICKPRICE_FINNHUB_API_KEY")
    assert finnhub["configured"] is True
    assert finnhub["masked_value"] == "Configured"

    monkeypatch.setenv("QUICKPRICE_FRED_API_KEY", "externally-managed")
    current = store.snapshot()
    with pytest.raises(UnsupportedSettingError):
        store.patch(
            updates={"QUICKPRICE_FRED_API_KEY": "replacement"},
            removals=[],
            expected_revision=current["revision"],
        )


def test_provider_key_admin_cannot_change_network_endpoints(tmp_path) -> None:
    path = tmp_path / "provider-keys.env"
    path.write_text(
        "QUICKPRICE_ETHEREUM_RPC_URLS=https://rpc.example.test\n",
        encoding="utf-8",
    )
    store = ProviderKeyStore(path)
    snapshot = store.snapshot()

    assert all(item["name"] != "QUICKPRICE_ETHEREUM_RPC_URLS" for item in snapshot["keys"])
    with pytest.raises(UnsupportedSettingError):
        store.patch(
            updates={"QUICKPRICE_ETHEREUM_RPC_URLS": "http://127.0.0.1:1"},
            removals=[],
            expected_revision=snapshot["revision"],
        )


def test_instrument_policy_only_changes_installed_declarative_catalog(tmp_path) -> None:
    catalog = build_registry()
    path = tmp_path / "instruments.json"
    store = InstrumentPolicyStore(path, catalog)
    initial = store.snapshot()
    updated = store.patch(
        instruments=[
            {
                "symbol": "BTC:USDC",
                "enabled": False,
                "quote_poll_seconds": 2,
                "stale_after_seconds": 20,
            }
        ],
        expected_revision=initial["revision"],
    )
    assert updated["restart_required"] is True
    active = apply_instrument_policy(catalog, path)
    assert "BTC:USDC" not in active
    assert "ETH:USDC" in active

    with pytest.raises(ValueError, match="unknown"):
        store.patch(
            instruments=[{"symbol": "ARBITRARY:CODE", "enabled": True}],
            expected_revision=updated["revision"],
        )
