from __future__ import annotations

import pytest

from quickprice.config import Settings


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
        ("QUICKPRICE_USD_CNH_POLL_SECONDS", "129", 130),
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
