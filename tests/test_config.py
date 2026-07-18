from __future__ import annotations

from pathlib import Path

import pytest

from quickprice.config import Settings


def test_provider_credentials_load_from_explicit_file(monkeypatch, tmp_path: Path) -> None:
    key_file = tmp_path / "provider-keys.env"
    key_file.write_text(
        """\
# Provider credentials only.
QUICKPRICE_ALPACA_API_KEY='alpaca-key'
QUICKPRICE_ALPACA_API_SECRET=alpaca-secret
QUICKPRICE_ETHEREUM_RPC_URLS="https://rpc-one.invalid, https://rpc-two.invalid"
QUICKPRICE_TWELVE_DATA_API_KEY=twelve-key
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("QUICKPRICE_PROVIDER_KEYS_FILE", str(key_file))
    monkeypatch.delenv("QUICKPRICE_ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.alpaca_api_key == "alpaca-key"
    assert settings.alpaca_api_secret == "alpaca-secret"
    assert settings.twelve_data_api_key == "twelve-key"
    assert settings.ethereum_rpc_urls == (
        "https://rpc-one.invalid",
        "https://rpc-two.invalid",
    )


def test_process_environment_overrides_provider_key_file(monkeypatch, tmp_path: Path) -> None:
    key_file = tmp_path / "provider-keys.env"
    key_file.write_text("QUICKPRICE_FRED_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("QUICKPRICE_PROVIDER_KEYS_FILE", str(key_file))
    monkeypatch.setenv("QUICKPRICE_FRED_API_KEY", "environment-key")

    assert Settings.from_env().fred_api_key == "environment-key"


def test_explicit_provider_key_file_must_exist(monkeypatch, tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"
    monkeypatch.setenv("QUICKPRICE_PROVIDER_KEYS_FILE", str(missing))

    with pytest.raises(FileNotFoundError, match="provider key file does not exist"):
        Settings.from_env()


@pytest.mark.parametrize(
    "content",
    [
        "QUICKPRICE_DATABASE_PATH=/tmp/not-allowed\n",
        "QUICKPRICE_FRED_API_KEY=first\nQUICKPRICE_FRED_API_KEY=second\n",
        "QUICKPRICE_FRED_API_KEY='unterminated\n",
        "not-an-assignment\n",
    ],
)
def test_provider_key_file_rejects_unsafe_or_malformed_content(
    monkeypatch, tmp_path: Path, content: str
) -> None:
    key_file = tmp_path / "provider-keys.env"
    key_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("QUICKPRICE_PROVIDER_KEYS_FILE", str(key_file))

    with pytest.raises(ValueError):
        Settings.from_env()


def test_staking_settings_are_parsed_trimmed_and_deduplicated(monkeypatch) -> None:
    monkeypatch.setenv(
        "QUICKPRICE_ETHEREUM_RPC_URLS",
        " https://rpc-one.invalid ,https://rpc-two.invalid,https://rpc-one.invalid ",
    )
    monkeypatch.setenv("QUICKPRICE_BINANCE_API_KEY", " read-only-key ")
    monkeypatch.setenv("QUICKPRICE_BINANCE_API_SECRET", " signing-secret ")
    monkeypatch.setenv("QUICKPRICE_STAKING_YIELD_MARKET_FALLBACK_DAYS", "45")

    settings = Settings.from_env()

    assert settings.ethereum_rpc_urls == (
        "https://rpc-one.invalid",
        "https://rpc-two.invalid",
    )
    assert settings.binance_api_key == "read-only-key"
    assert settings.binance_api_secret == "signing-secret"
    assert settings.staking_yield_market_fallback_days == 45


def test_staking_market_fallback_window_rejects_less_than_seven_days(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_STAKING_YIELD_MARKET_FALLBACK_DAYS", "6")

    with pytest.raises(ValueError, match="must be >= 7"):
        Settings.from_env()


def test_alpaca_trading_clock_defaults_to_paper_and_is_configurable(monkeypatch) -> None:
    monkeypatch.delenv("QUICKPRICE_ALPACA_TRADING_BASE_URL", raising=False)
    assert Settings.from_env().alpaca_trading_base_url == ("https://paper-api.alpaca.markets/v2")

    monkeypatch.setenv(
        "QUICKPRICE_ALPACA_TRADING_BASE_URL",
        " https://clock.example.invalid/v2 ",
    )
    assert Settings.from_env().alpaca_trading_base_url == "https://clock.example.invalid/v2"


@pytest.mark.parametrize(
    ("name", "value", "minimum"),
    [
        ("QUICKPRICE_USD_CNH_POLL_SECONDS", "239", 240),
        ("QUICKPRICE_USD_HKD_POLL_SECONDS", "899", 900),
    ],
)
def test_fx_poll_cadence_cannot_undercut_free_tier_safety(
    monkeypatch,
    name: str,
    value: str,
    minimum: int,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"must be >= {minimum}"):
        Settings.from_env()
