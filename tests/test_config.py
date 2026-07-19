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
QUICKPRICE_FINNHUB_API_KEY=finnhub-key
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
    assert settings.finnhub_api_key == "finnhub-key"
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


def test_dashboard_log_stream_limit_is_configurable_and_positive(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_DASHBOARD_MAX_LOG_STREAMS", "12")
    assert Settings.from_env().dashboard_max_log_streams == 12

    monkeypatch.setenv("QUICKPRICE_DASHBOARD_MAX_LOG_STREAMS", "0")
    with pytest.raises(ValueError, match="must be >= 1"):
        Settings.from_env()


def test_admin_trusted_proxies_require_explicit_ip_addresses(monkeypatch) -> None:
    monkeypatch.setenv(
        "QUICKPRICE_ADMIN_TRUSTED_PROXY_IPS",
        " 127.0.0.1,::1,127.0.0.1 ",
    )
    assert Settings.from_env().admin_trusted_proxy_ips == ("127.0.0.1", "::1")

    monkeypatch.setenv("QUICKPRICE_ADMIN_TRUSTED_PROXY_IPS", "localhost")
    with pytest.raises(ValueError, match="explicit IP addresses"):
        Settings.from_env()


def test_admin_account_bootstrap_and_managed_path_are_configurable(
    monkeypatch, tmp_path: Path
) -> None:
    account_file = tmp_path / "admin-account.json"
    monkeypatch.setenv("QUICKPRICE_ADMIN_USERNAME", " operator ")
    monkeypatch.setenv("QUICKPRICE_ADMIN_PASSWORD_VERIFIER", " verifier ")
    monkeypatch.setenv("QUICKPRICE_ADMIN_PASSWORD_CHANGE_REQUIRED", "false")
    monkeypatch.setenv("QUICKPRICE_MANAGED_ADMIN_ACCOUNT_FILE", str(account_file))
    monkeypatch.setenv("QUICKPRICE_ADMIN_KEY_VERIFIER", "legacy-must-not-load")

    settings = Settings.from_env()

    assert settings.admin_username == "operator"
    assert settings.admin_password_verifier == "verifier"
    assert settings.admin_password_change_required is False
    assert settings.managed_admin_account_path == account_file
    assert not hasattr(settings, "admin_key_verifier")


def test_admin_password_change_bootstrap_defaults_to_required(monkeypatch) -> None:
    monkeypatch.delenv("QUICKPRICE_ADMIN_PASSWORD_CHANGE_REQUIRED", raising=False)

    assert Settings.from_env().admin_password_change_required is True


def test_alpaca_trading_clock_defaults_to_paper_and_is_configurable(monkeypatch) -> None:
    monkeypatch.delenv("QUICKPRICE_ALPACA_TRADING_BASE_URL", raising=False)
    assert Settings.from_env().alpaca_trading_base_url == ("https://paper-api.alpaca.markets/v2")

    monkeypatch.setenv(
        "QUICKPRICE_ALPACA_TRADING_BASE_URL",
        " https://clock.example.invalid/v2 ",
    )
    assert Settings.from_env().alpaca_trading_base_url == "https://clock.example.invalid/v2"


def test_alpaca_capacity_and_catalog_warm_limits_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_ALPACA_STREAM_SYMBOL_LIMIT", "50")
    monkeypatch.setenv("QUICKPRICE_ALPACA_REST_CALLS_PER_MINUTE", "220")
    monkeypatch.setenv("QUICKPRICE_CATALOG_WARM_TIMEOUT_SECONDS", "900")

    settings = Settings.from_env()

    assert settings.alpaca_stream_symbol_limit == 50
    assert settings.alpaca_rest_calls_per_minute == 220
    assert settings.catalog_warm_timeout_seconds == 900


def test_finnhub_key_and_minute_quota_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("FINNHUB_API_KEY", " legacy-finnhub-key ")
    monkeypatch.setenv("QUICKPRICE_FINNHUB_CALLS_PER_MINUTE", "42")

    settings = Settings.from_env()

    assert settings.finnhub_api_key == "legacy-finnhub-key"
    assert settings.finnhub_calls_per_minute == 42


def test_twelve_short_window_rate_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_TWELVE_CALLS_PER_MINUTE", "610")
    monkeypatch.setenv("QUICKPRICE_TWELVE_RATE_GATE_TIMEOUT_SECONDS", "12.5")

    settings = Settings.from_env()
    assert settings.twelve_calls_per_minute == 610
    assert settings.twelve_rate_gate_timeout_seconds == 12.5


def test_twelve_short_window_rate_must_be_positive(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_TWELVE_CALLS_PER_MINUTE", "0")

    with pytest.raises(ValueError, match="must be >= 1"):
        Settings.from_env()


def test_twelve_rate_gate_timeout_must_be_positive(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_TWELVE_RATE_GATE_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError, match=r"must be >= 0\.1"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_float_settings_must_be_finite(monkeypatch, value: str) -> None:
    monkeypatch.setenv("QUICKPRICE_PROVIDER_TIMEOUT_SECONDS", value)

    with pytest.raises(ValueError, match="must be finite"):
        Settings.from_env()


def test_provider_proxy_defaults_to_all_providers(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_PROVIDER_PROXY_URL", "http://10.0.1.7:7890")
    monkeypatch.delenv("QUICKPRICE_PROVIDER_PROXY_NAMES", raising=False)

    settings = Settings.from_env()

    assert settings.provider_proxy_names == ("*",)
    assert settings.proxy_url_for_provider("binance") == "http://10.0.1.7:7890"
    assert settings.proxy_url_for_provider("future_plugin") == "http://10.0.1.7:7890"
    assert (
        Settings(provider_proxy_url="http://proxy.internal:8080").proxy_url_for_provider(
            "future_plugin"
        )
        == "http://proxy.internal:8080"
    )


def test_provider_proxy_can_be_enabled_per_provider(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_PROVIDER_PROXY_URL", "http://10.0.1.7:7890")
    monkeypatch.setenv(
        "QUICKPRICE_PROVIDER_PROXY_NAMES",
        " Binance, lido,binance ",
    )

    settings = Settings.from_env()

    assert settings.provider_proxy_names == ("binance", "lido")
    assert settings.proxy_url_for_provider("BINANCE") == "http://10.0.1.7:7890"
    assert settings.proxy_url_for_provider("kraken") is None


def test_provider_proxy_names_require_a_proxy_url(monkeypatch) -> None:
    monkeypatch.delenv("QUICKPRICE_PROVIDER_PROXY_URL", raising=False)
    monkeypatch.setenv("QUICKPRICE_PROVIDER_PROXY_NAMES", "binance")

    with pytest.raises(ValueError, match="requires QUICKPRICE_PROVIDER_PROXY_URL"):
        Settings.from_env()


@pytest.mark.parametrize(
    "proxy_url",
    (
        "socks5://10.0.1.7:7890",
        "http://10.0.1.7",
        "http://10.0.1.7:7890/path",
    ),
)
def test_provider_proxy_rejects_unsupported_urls(monkeypatch, proxy_url: str) -> None:
    monkeypatch.setenv("QUICKPRICE_PROVIDER_PROXY_URL", proxy_url)
    monkeypatch.delenv("QUICKPRICE_PROVIDER_PROXY_NAMES", raising=False)

    with pytest.raises(ValueError, match=r"HTTP\(S\) proxy URL"):
        Settings.from_env()


def test_finnhub_minute_quota_must_be_positive(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_FINNHUB_CALLS_PER_MINUTE", "0")

    with pytest.raises(ValueError, match="must be >= 1"):
        Settings.from_env()


def test_metadata_retry_interval_is_configurable_and_bounded(monkeypatch) -> None:
    monkeypatch.setenv("QUICKPRICE_METADATA_RETRY_SECONDS", "120")
    assert Settings.from_env().metadata_retry_seconds == 120

    monkeypatch.setenv("QUICKPRICE_METADATA_RETRY_SECONDS", "59")
    with pytest.raises(ValueError, match="must be >= 60"):
        Settings.from_env()


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
