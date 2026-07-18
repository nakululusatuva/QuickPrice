"""Environment configuration with isolated provider-credential loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

_PROVIDER_SECRET_NAMES: Final[frozenset[str]] = frozenset(
    {
        "QUICKPRICE_ALPACA_API_KEY",
        "QUICKPRICE_ALPACA_API_SECRET",
        "QUICKPRICE_ALPHA_VANTAGE_API_KEY",
        "QUICKPRICE_BINANCE_API_KEY",
        "QUICKPRICE_BINANCE_API_SECRET",
        "QUICKPRICE_COINGECKO_API_KEY",
        "QUICKPRICE_ETHEREUM_RPC_URLS",
        "QUICKPRICE_FRED_API_KEY",
        "QUICKPRICE_FINNHUB_API_KEY",
        "QUICKPRICE_TWELVE_DATA_API_KEY",
    }
)


def _provider_key_file_values() -> MappingProxyType[str, str]:
    raw_path = os.getenv("QUICKPRICE_PROVIDER_KEYS_FILE", "").strip()
    if not raw_path:
        return MappingProxyType({})

    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"provider key file does not exist: {path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not name:
            raise ValueError(f"{path}:{line_number}: expected NAME=VALUE")
        if name not in _PROVIDER_SECRET_NAMES:
            raise ValueError(f"{path}:{line_number}: unsupported provider credential {name!r}")
        if name in values:
            raise ValueError(f"{path}:{line_number}: duplicate provider credential {name!r}")

        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ValueError(f"{path}:{line_number}: unterminated quoted value")
            value = value[1:-1]
        values[name] = value
    return MappingProxyType(values)


def _provider_value(name: str, file_values: MappingProxyType[str, str]) -> str | None:
    value = os.getenv(name)
    if value is None:
        value = file_values.get(name)
    return value


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _float(name: str, default: float, *, minimum: float = 0) -> float:
    value = float(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _provider_proxy_configuration() -> tuple[str | None, tuple[str, ...]]:
    raw_url = os.getenv("QUICKPRICE_PROVIDER_PROXY_URL", "").strip()
    raw_names = os.getenv("QUICKPRICE_PROVIDER_PROXY_NAMES", "")
    names = tuple(
        dict.fromkeys(item.strip().lower() for item in raw_names.split(",") if item.strip())
    )

    if not raw_url and not names:
        return None, ()
    if not raw_url:
        raise ValueError("QUICKPRICE_PROVIDER_PROXY_NAMES requires QUICKPRICE_PROVIDER_PROXY_URL")
    if not names:
        names = ("*",)

    parsed = urlsplit(raw_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("QUICKPRICE_PROVIDER_PROXY_URL has an invalid port") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "QUICKPRICE_PROVIDER_PROXY_URL must be an HTTP(S) proxy URL with an explicit port"
        )
    if "*" in names and len(names) != 1:
        raise ValueError("QUICKPRICE_PROVIDER_PROXY_NAMES wildcard must be used alone")
    return raw_url, names


def _secret(
    name: str,
    file_values: MappingProxyType[str, str],
    legacy_name: str | None = None,
) -> str | None:
    value = os.getenv(name)
    if value is None and legacy_name:
        value = os.getenv(legacy_name)
    if value is None:
        value = file_values.get(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True, slots=True)
class Settings:
    production: bool = True
    require_free_threaded: bool = True
    background_enabled: bool = True
    database_path: Path = field(default_factory=lambda: Path("data/quickprice.db"))
    api_key_hashes: tuple[str, ...] = ()
    rate_limit_enabled: bool = True
    requests_per_minute: int = 120
    request_burst: int = 20
    invalid_requests_per_minute: int = 30
    invalid_request_burst: int = 10
    dashboard_max_log_streams: int = 8
    provider_timeout_seconds: float = 8.0
    provider_proxy_url: str | None = None
    provider_proxy_names: tuple[str, ...] = ()
    circuit_failure_threshold: int = 3
    circuit_open_seconds: float = 60.0
    crypto_poll_seconds: float = 1.0
    equity_poll_seconds: float = 5.0
    usd_cnh_poll_seconds: float = 240.0
    usd_hkd_poll_seconds: float = 900.0
    metadata_poll_seconds: float = 21600.0
    metadata_retry_seconds: float = 300.0
    history_poll_seconds: float = 3600.0
    twelve_daily_credits: int = 790
    twelve_fx_reserve_credits: int = 769
    twelve_calls_per_minute: int = 8
    twelve_rate_gate_timeout_seconds: float = 5.0
    alpha_vantage_daily_credits: int = 25
    finnhub_calls_per_minute: int = 60
    coingecko_monthly_credits: int = 9000
    high_frequency_publish_ms: int = 250
    sqlite_batch_size: int = 100
    sqlite_batch_ms: int = 250
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"
    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None
    alpaca_trading_base_url: str = "https://paper-api.alpaca.markets/v2"
    twelve_data_api_key: str | None = None
    alpha_vantage_api_key: str | None = None
    finnhub_api_key: str | None = None
    coingecko_api_key: str | None = None
    fred_api_key: str | None = None
    ethereum_rpc_urls: tuple[str, ...] = ()
    binance_api_key: str | None = None
    binance_api_secret: str | None = None
    staking_yield_market_fallback_days: int = 30
    enabled_plugins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        provider_key_values = _provider_key_file_values()
        provider_proxy_url, provider_proxy_names = _provider_proxy_configuration()
        raw_hashes = os.getenv("QUICKPRICE_API_KEY_HASHES", "")
        hashes = tuple(item.strip().lower() for item in raw_hashes.split(",") if item.strip())
        raw_plugins = os.getenv("QUICKPRICE_ENABLED_PLUGINS", "")
        enabled_plugins = tuple(
            dict.fromkeys(item.strip() for item in raw_plugins.split(",") if item.strip())
        )
        ethereum_rpc_urls = tuple(
            dict.fromkeys(
                item.strip()
                for item in (
                    _provider_value("QUICKPRICE_ETHEREUM_RPC_URLS", provider_key_values) or ""
                ).split(",")
                if item.strip()
            )
        )
        return cls(
            production=_bool("QUICKPRICE_PRODUCTION", True),
            require_free_threaded=_bool("QUICKPRICE_REQUIRE_FREE_THREADED", True),
            background_enabled=_bool("QUICKPRICE_BACKGROUND_ENABLED", True),
            database_path=Path(os.getenv("QUICKPRICE_DATABASE_PATH", "data/quickprice.db")),
            api_key_hashes=hashes,
            rate_limit_enabled=_bool("QUICKPRICE_RATE_LIMIT_ENABLED", True),
            requests_per_minute=_int("QUICKPRICE_REQUESTS_PER_MINUTE", 120, minimum=1),
            request_burst=_int("QUICKPRICE_REQUEST_BURST", 20, minimum=1),
            invalid_requests_per_minute=_int(
                "QUICKPRICE_INVALID_REQUESTS_PER_MINUTE", 30, minimum=1
            ),
            invalid_request_burst=_int("QUICKPRICE_INVALID_REQUEST_BURST", 10, minimum=1),
            dashboard_max_log_streams=_int("QUICKPRICE_DASHBOARD_MAX_LOG_STREAMS", 8, minimum=1),
            provider_timeout_seconds=_float(
                "QUICKPRICE_PROVIDER_TIMEOUT_SECONDS", 8.0, minimum=0.1
            ),
            provider_proxy_url=provider_proxy_url,
            provider_proxy_names=provider_proxy_names,
            circuit_failure_threshold=_int("QUICKPRICE_CIRCUIT_FAILURE_THRESHOLD", 3, minimum=1),
            circuit_open_seconds=_float("QUICKPRICE_CIRCUIT_OPEN_SECONDS", 60.0, minimum=1.0),
            crypto_poll_seconds=_float("QUICKPRICE_CRYPTO_POLL_SECONDS", 1.0, minimum=0.25),
            equity_poll_seconds=_float("QUICKPRICE_EQUITY_POLL_SECONDS", 5.0, minimum=1),
            usd_cnh_poll_seconds=_float("QUICKPRICE_USD_CNH_POLL_SECONDS", 240, minimum=240),
            usd_hkd_poll_seconds=_float("QUICKPRICE_USD_HKD_POLL_SECONDS", 900, minimum=900),
            metadata_poll_seconds=_float("QUICKPRICE_METADATA_POLL_SECONDS", 21600, minimum=300),
            metadata_retry_seconds=_float("QUICKPRICE_METADATA_RETRY_SECONDS", 300, minimum=60),
            history_poll_seconds=_float("QUICKPRICE_HISTORY_POLL_SECONDS", 3600, minimum=60),
            twelve_daily_credits=_int("QUICKPRICE_TWELVE_DAILY_CREDITS", 790, minimum=1),
            twelve_fx_reserve_credits=_int("QUICKPRICE_TWELVE_FX_RESERVE_CREDITS", 769, minimum=0),
            twelve_calls_per_minute=_int("QUICKPRICE_TWELVE_CALLS_PER_MINUTE", 8, minimum=1),
            twelve_rate_gate_timeout_seconds=_float(
                "QUICKPRICE_TWELVE_RATE_GATE_TIMEOUT_SECONDS", 5.0, minimum=0.1
            ),
            alpha_vantage_daily_credits=_int(
                "QUICKPRICE_ALPHA_VANTAGE_DAILY_CREDITS", 25, minimum=1
            ),
            finnhub_calls_per_minute=_int("QUICKPRICE_FINNHUB_CALLS_PER_MINUTE", 60, minimum=1),
            coingecko_monthly_credits=_int(
                "QUICKPRICE_COINGECKO_MONTHLY_CREDITS", 9000, minimum=31
            ),
            high_frequency_publish_ms=_int("QUICKPRICE_HIGH_FREQUENCY_PUBLISH_MS", 250, minimum=50),
            sqlite_batch_size=_int("QUICKPRICE_SQLITE_BATCH_SIZE", 100, minimum=1),
            sqlite_batch_ms=_int("QUICKPRICE_SQLITE_BATCH_MS", 250, minimum=10),
            host=os.getenv("QUICKPRICE_HOST", "0.0.0.0"),
            port=_int("QUICKPRICE_PORT", 8080, minimum=1),
            log_level=os.getenv("QUICKPRICE_LOG_LEVEL", "info").lower(),
            alpaca_api_key=_secret(
                "QUICKPRICE_ALPACA_API_KEY", provider_key_values, "ALPACA_API_KEY"
            ),
            alpaca_api_secret=_secret(
                "QUICKPRICE_ALPACA_API_SECRET", provider_key_values, "ALPACA_API_SECRET"
            ),
            alpaca_trading_base_url=(
                os.getenv(
                    "QUICKPRICE_ALPACA_TRADING_BASE_URL",
                    "https://paper-api.alpaca.markets/v2",
                ).strip()
                or "https://paper-api.alpaca.markets/v2"
            ),
            twelve_data_api_key=_secret(
                "QUICKPRICE_TWELVE_DATA_API_KEY", provider_key_values, "TWELVE_DATA_API_KEY"
            ),
            alpha_vantage_api_key=_secret(
                "QUICKPRICE_ALPHA_VANTAGE_API_KEY",
                provider_key_values,
                "ALPHA_VANTAGE_API_KEY",
            ),
            finnhub_api_key=_secret(
                "QUICKPRICE_FINNHUB_API_KEY", provider_key_values, "FINNHUB_API_KEY"
            ),
            coingecko_api_key=_secret(
                "QUICKPRICE_COINGECKO_API_KEY", provider_key_values, "COINGECKO_API_KEY"
            ),
            fred_api_key=_secret("QUICKPRICE_FRED_API_KEY", provider_key_values, "FRED_API_KEY"),
            ethereum_rpc_urls=ethereum_rpc_urls,
            binance_api_key=_secret("QUICKPRICE_BINANCE_API_KEY", provider_key_values),
            binance_api_secret=_secret("QUICKPRICE_BINANCE_API_SECRET", provider_key_values),
            staking_yield_market_fallback_days=_int(
                "QUICKPRICE_STAKING_YIELD_MARKET_FALLBACK_DAYS", 30, minimum=7
            ),
            enabled_plugins=enabled_plugins,
        )

    def proxy_url_for_provider(self, provider_name: str) -> str | None:
        normalized = provider_name.strip().lower()
        if self.provider_proxy_url and (
            not self.provider_proxy_names
            or "*" in self.provider_proxy_names
            or normalized in self.provider_proxy_names
        ):
            return self.provider_proxy_url
        return None

    @property
    def docs_enabled(self) -> bool:
        return not self.production
