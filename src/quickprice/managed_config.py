"""Strict, revisioned stores for administrator-managed declarative settings."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal

from .config import _PROVIDER_SECRET_NAMES, Settings
from .registry import InstrumentRegistry, build_registry, normalize_symbol

_ENV_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*$")
_MAX_FILE_BYTES: Final[int] = 64 * 1024
_WEB_MANAGED_PROVIDER_SECRET_NAMES: Final[frozenset[str]] = frozenset(
    _PROVIDER_SECRET_NAMES - {"QUICKPRICE_ETHEREUM_RPC_URLS"}
)
_PROVIDER_DISPLAY_NAMES: Final[dict[str, str]] = {
    "QUICKPRICE_ALPACA_API_KEY": "Alpaca API key",
    "QUICKPRICE_ALPACA_API_SECRET": "Alpaca API secret",
    "QUICKPRICE_ALPHA_VANTAGE_API_KEY": "Alpha Vantage API key",
    "QUICKPRICE_BINANCE_API_KEY": "Binance API key",
    "QUICKPRICE_BINANCE_API_SECRET": "Binance API secret",
    "QUICKPRICE_COINGECKO_API_KEY": "CoinGecko Demo API key",
    "QUICKPRICE_ETHEREUM_RPC_URLS": "Ethereum RPC endpoints",
    "QUICKPRICE_FINNHUB_API_KEY": "Finnhub API key",
    "QUICKPRICE_FRED_API_KEY": "FRED API key",
    "QUICKPRICE_OKX_API_KEY": "OKX API key",
    "QUICKPRICE_OKX_API_PASSPHRASE": "OKX API passphrase",
    "QUICKPRICE_OKX_API_SECRET": "OKX API secret",
    "QUICKPRICE_TWELVE_DATA_API_KEY": "Twelve Data API key",
}


class ManagedConfigurationError(RuntimeError):
    pass


class RevisionConflictError(ManagedConfigurationError):
    pass


class UnsupportedSettingError(ManagedConfigurationError):
    pass


@dataclass(frozen=True, slots=True)
class SettingSpec:
    name: str
    label: str
    kind: Literal["boolean", "integer", "number", "choice", "provider_list"]
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    def validate(self, value: Any) -> str:
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{self.name} must be a boolean")
            return "true" if value else "false"
        if self.kind in {"integer", "number"}:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{self.name} must be numeric")
            if self.kind == "integer" and not isinstance(value, int):
                raise ValueError(f"{self.name} must be an integer")
            numeric = float(value)
            if self.minimum is not None and numeric < self.minimum:
                raise ValueError(f"{self.name} must be at least {self.minimum:g}")
            if self.maximum is not None and numeric > self.maximum:
                raise ValueError(f"{self.name} must be at most {self.maximum:g}")
            return str(value)
        if self.kind == "choice":
            if not isinstance(value, str) or value not in self.choices:
                raise ValueError(f"{self.name} has an unsupported value")
            return value
        if self.kind == "provider_list":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"{self.name} must be a list of provider names")
            normalized = tuple(
                dict.fromkeys(item.strip().lower() for item in value if item.strip())
            )
            if len(normalized) > 32 or any(
                not re.fullmatch(r"[a-z0-9_*-]{1,64}", item) for item in normalized
            ):
                raise ValueError(f"{self.name} contains an invalid provider name")
            if "*" in normalized and len(normalized) != 1:
                raise ValueError(f"{self.name} wildcard must be used alone")
            return ",".join(normalized)
        raise AssertionError("unreachable setting type")


_MANAGED_SETTINGS: Final[tuple[SettingSpec, ...]] = (
    SettingSpec("QUICKPRICE_RATE_LIMIT_ENABLED", "API rate limiting", "boolean"),
    SettingSpec("QUICKPRICE_REQUESTS_PER_MINUTE", "Requests per minute", "integer", 1, 100_000),
    SettingSpec("QUICKPRICE_REQUEST_BURST", "Request burst", "integer", 1, 10_000),
    SettingSpec(
        "QUICKPRICE_INVALID_REQUESTS_PER_MINUTE",
        "Invalid requests per minute",
        "integer",
        1,
        10_000,
    ),
    SettingSpec("QUICKPRICE_INVALID_REQUEST_BURST", "Invalid request burst", "integer", 1, 1_000),
    SettingSpec(
        "QUICKPRICE_PROVIDER_TIMEOUT_SECONDS", "Provider timeout (seconds)", "number", 0.1, 60
    ),
    SettingSpec(
        "QUICKPRICE_PROVIDER_PROXY_NAMES", "Providers using the configured proxy", "provider_list"
    ),
    SettingSpec(
        "QUICKPRICE_CIRCUIT_FAILURE_THRESHOLD", "Circuit failure threshold", "integer", 1, 20
    ),
    SettingSpec(
        "QUICKPRICE_CIRCUIT_OPEN_SECONDS", "Circuit open interval (seconds)", "number", 1, 3_600
    ),
    SettingSpec(
        "QUICKPRICE_CRYPTO_POLL_SECONDS", "Crypto polling interval (seconds)", "number", 0.25, 3_600
    ),
    SettingSpec(
        "QUICKPRICE_EQUITY_POLL_SECONDS", "Equity polling interval (seconds)", "number", 1, 3_600
    ),
    SettingSpec(
        "QUICKPRICE_USD_CNH_POLL_SECONDS",
        "USD/CNH polling interval (seconds)",
        "number",
        240,
        86_400,
    ),
    SettingSpec(
        "QUICKPRICE_USD_HKD_POLL_SECONDS",
        "FX hub polling interval (seconds)",
        "number",
        900,
        86_400,
    ),
    SettingSpec(
        "QUICKPRICE_METADATA_POLL_SECONDS",
        "Metadata polling interval (seconds)",
        "number",
        300,
        604_800,
    ),
    SettingSpec(
        "QUICKPRICE_METADATA_RETRY_SECONDS",
        "Metadata retry interval (seconds)",
        "number",
        60,
        86_400,
    ),
    SettingSpec(
        "QUICKPRICE_HISTORY_POLL_SECONDS",
        "History polling interval (seconds)",
        "number",
        60,
        86_400,
    ),
    SettingSpec(
        "QUICKPRICE_TWELVE_DAILY_CREDITS",
        "Twelve Data daily credit budget",
        "integer",
        1,
        1_000_000,
    ),
    SettingSpec(
        "QUICKPRICE_TWELVE_FX_RESERVE_CREDITS",
        "Twelve Data FX credit reserve",
        "integer",
        0,
        1_000_000,
    ),
    SettingSpec(
        "QUICKPRICE_ALPHA_VANTAGE_DAILY_CREDITS",
        "Alpha Vantage daily credit budget",
        "integer",
        1,
        1_000_000,
    ),
    SettingSpec(
        "QUICKPRICE_FINNHUB_CALLS_PER_MINUTE", "Finnhub calls per minute", "integer", 1, 10_000
    ),
    SettingSpec(
        "QUICKPRICE_COINGECKO_MONTHLY_CREDITS",
        "CoinGecko monthly credit budget",
        "integer",
        31,
        10_000_000,
    ),
    SettingSpec(
        "QUICKPRICE_HIGH_FREQUENCY_PUBLISH_MS",
        "Snapshot coalescing interval (ms)",
        "integer",
        50,
        10_000,
    ),
    SettingSpec("QUICKPRICE_SQLITE_BATCH_SIZE", "SQLite batch size", "integer", 1, 10_000),
    SettingSpec("QUICKPRICE_SQLITE_BATCH_MS", "SQLite batch interval (ms)", "integer", 10, 10_000),
    SettingSpec(
        "QUICKPRICE_LOG_LEVEL", "Log level", "choice", choices=("debug", "info", "warning", "error")
    ),
)
_MANAGED_BY_NAME: Final[dict[str, SettingSpec]] = {item.name: item for item in _MANAGED_SETTINGS}
_SETTING_ATTRIBUTES: Final[dict[str, str]] = {
    "QUICKPRICE_RATE_LIMIT_ENABLED": "rate_limit_enabled",
    "QUICKPRICE_REQUESTS_PER_MINUTE": "requests_per_minute",
    "QUICKPRICE_REQUEST_BURST": "request_burst",
    "QUICKPRICE_INVALID_REQUESTS_PER_MINUTE": "invalid_requests_per_minute",
    "QUICKPRICE_INVALID_REQUEST_BURST": "invalid_request_burst",
    "QUICKPRICE_PROVIDER_TIMEOUT_SECONDS": "provider_timeout_seconds",
    "QUICKPRICE_PROVIDER_PROXY_NAMES": "provider_proxy_names",
    "QUICKPRICE_CIRCUIT_FAILURE_THRESHOLD": "circuit_failure_threshold",
    "QUICKPRICE_CIRCUIT_OPEN_SECONDS": "circuit_open_seconds",
    "QUICKPRICE_CRYPTO_POLL_SECONDS": "crypto_poll_seconds",
    "QUICKPRICE_EQUITY_POLL_SECONDS": "equity_poll_seconds",
    "QUICKPRICE_USD_CNH_POLL_SECONDS": "usd_cnh_poll_seconds",
    "QUICKPRICE_USD_HKD_POLL_SECONDS": "usd_hkd_poll_seconds",
    "QUICKPRICE_METADATA_POLL_SECONDS": "metadata_poll_seconds",
    "QUICKPRICE_METADATA_RETRY_SECONDS": "metadata_retry_seconds",
    "QUICKPRICE_HISTORY_POLL_SECONDS": "history_poll_seconds",
    "QUICKPRICE_TWELVE_DAILY_CREDITS": "twelve_daily_credits",
    "QUICKPRICE_TWELVE_FX_RESERVE_CREDITS": "twelve_fx_reserve_credits",
    "QUICKPRICE_ALPHA_VANTAGE_DAILY_CREDITS": "alpha_vantage_daily_credits",
    "QUICKPRICE_FINNHUB_CALLS_PER_MINUTE": "finnhub_calls_per_minute",
    "QUICKPRICE_COINGECKO_MONTHLY_CREDITS": "coingecko_monthly_credits",
    "QUICKPRICE_HIGH_FREQUENCY_PUBLISH_MS": "high_frequency_publish_ms",
    "QUICKPRICE_SQLITE_BATCH_SIZE": "sqlite_batch_size",
    "QUICKPRICE_SQLITE_BATCH_MS": "sqlite_batch_ms",
    "QUICKPRICE_LOG_LEVEL": "log_level",
}


def _revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise ManagedConfigurationError("managed configuration path cannot be a symbolic link")
    if not path.exists():
        return b""
    if not path.is_file():
        raise ManagedConfigurationError("managed configuration path is not a regular file")
    size = path.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise ManagedConfigurationError("managed configuration file is too large")
    return path.read_bytes()


def _parse_env(content: bytes, allowed_names: frozenset[str]) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManagedConfigurationError("managed configuration must use UTF-8") from exc
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not _ENV_NAME_PATTERN.fullmatch(name):
            raise ManagedConfigurationError(f"line {line_number} must use NAME=VALUE")
        if name not in allowed_names:
            raise UnsupportedSettingError(f"unsupported managed setting: {name}")
        if name in values:
            raise ManagedConfigurationError(f"duplicate managed setting: {name}")
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ManagedConfigurationError(f"invalid value for {name}")
        values[name] = value.strip()
    return values


def _serialize_env(values: dict[str, str], *, heading: str) -> bytes:
    lines = [f"# {heading}", "# Managed by QuickPrice. Manual edits require a service restart.", ""]
    lines.extend(f"{name}={values[name]}" for name in sorted(values))
    lines.append("")
    content = "\n".join(lines).encode("utf-8")
    if len(content) > _MAX_FILE_BYTES:
        raise ManagedConfigurationError("managed configuration file is too large")
    return content


def _atomic_write(path: Path, content: bytes) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ManagedConfigurationError("managed configuration path cannot be a symbolic link")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = -1
        if directory_descriptor >= 0:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


class ManagedEnvironmentStore:
    def __init__(self, path: Path, settings: Settings | None = None) -> None:
        self.path = path.expanduser()
        self.settings = settings
        self._lock = threading.RLock()
        self._startup_revision = _revision(_read_bytes(self.path))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            content = _read_bytes(self.path)
            values = _parse_env(content, frozenset(_MANAGED_BY_NAME))
            return {
                "revision": _revision(content),
                "startup_revision": self._startup_revision,
                "restart_required": _revision(content) != self._startup_revision,
                "settings": [
                    {
                        "name": spec.name,
                        "label": spec.label,
                        "kind": spec.kind,
                        "minimum": spec.minimum,
                        "maximum": spec.maximum,
                        "choices": list(spec.choices),
                        "value": self._display_value(spec, values.get(spec.name)),
                        "running_value": self._runtime_value(spec),
                        "source": "managed_file" if spec.name in values else "running_process",
                        "is_set": spec.name in values,
                        "group": self._group(spec.name),
                    }
                    for spec in _MANAGED_SETTINGS
                ],
            }

    def patch(
        self,
        *,
        updates: dict[str, Any],
        removals: list[str],
        expected_revision: str,
    ) -> dict[str, Any]:
        with self._lock:
            content = _read_bytes(self.path)
            if not hmac_compare_revision(_revision(content), expected_revision):
                raise RevisionConflictError("managed configuration changed concurrently")
            values = _parse_env(content, frozenset(_MANAGED_BY_NAME))
            for name, value in updates.items():
                spec = _MANAGED_BY_NAME.get(name)
                if spec is None:
                    raise UnsupportedSettingError(f"unsupported managed setting: {name}")
                values[name] = spec.validate(value)
            for name in removals:
                if name not in _MANAGED_BY_NAME:
                    raise UnsupportedSettingError(f"unsupported managed setting: {name}")
                values.pop(name, None)
            _atomic_write(
                self.path,
                _serialize_env(values, heading="QuickPrice non-secret runtime settings"),
            )
            return self.snapshot()

    @staticmethod
    def _public_value(spec: SettingSpec, value: str | None) -> Any:
        if value is None:
            return None
        if spec.kind == "boolean":
            return value.lower() == "true"
        if spec.kind == "integer":
            return int(value)
        if spec.kind == "number":
            return float(value)
        if spec.kind == "provider_list":
            return [item for item in value.split(",") if item]
        return value

    def _display_value(self, spec: SettingSpec, value: str | None) -> Any:
        if value is not None:
            return self._public_value(spec, value)
        return self._runtime_value(spec)

    def _runtime_value(self, spec: SettingSpec) -> Any:
        attribute = _SETTING_ATTRIBUTES[spec.name]
        value = getattr(self.settings, attribute) if self.settings is not None else None
        if spec.kind == "provider_list" and value is not None:
            return list(value)
        return value

    @staticmethod
    def _group(name: str) -> str:
        if "REQUEST" in name or "RATE_LIMIT" in name:
            return "Client access"
        if "SQLITE" in name:
            return "Persistence"
        if name.endswith("LOG_LEVEL"):
            return "Operations"
        return "Provider collection"


class ProviderKeyStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._lock = threading.RLock()
        self._startup_revision = _revision(_read_bytes(self.path))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            content = _read_bytes(self.path)
            values = _parse_env(content, _PROVIDER_SECRET_NAMES)
            items = []
            for name in sorted(_WEB_MANAGED_PROVIDER_SECRET_NAMES):
                external = os.getenv(name)
                configured = bool(
                    (external if external is not None else values.get(name, "")).strip()
                )
                items.append(
                    {
                        "name": name,
                        "label": _PROVIDER_DISPLAY_NAMES.get(name, name),
                        "configured": configured,
                        "source": "external_environment"
                        if external is not None
                        else "managed_file"
                        if name in values
                        else "unset",
                        "editable": external is None,
                        "masked_value": "Configured" if configured else "Not configured",
                    }
                )
            current = _revision(content)
            return {
                "revision": current,
                "startup_revision": self._startup_revision,
                "restart_required": current != self._startup_revision,
                "keys": items,
            }

    def patch(
        self,
        *,
        updates: dict[str, str],
        removals: list[str],
        expected_revision: str,
    ) -> dict[str, Any]:
        with self._lock:
            content = _read_bytes(self.path)
            if not hmac_compare_revision(_revision(content), expected_revision):
                raise RevisionConflictError("provider key file changed concurrently")
            values = _parse_env(content, _PROVIDER_SECRET_NAMES)
            for name, value in updates.items():
                self._validate_editable_name(name)
                if not isinstance(value, str) or not 1 <= len(value) <= 4096:
                    raise ValueError(f"{name} must contain 1 to 4096 characters")
                if any(character in value for character in ("\x00", "\r", "\n")):
                    raise ValueError(f"{name} contains an invalid character")
                values[name] = value
            for name in removals:
                self._validate_editable_name(name)
                values.pop(name, None)
            _atomic_write(
                self.path,
                _serialize_env(values, heading="QuickPrice provider credentials (write-only)"),
            )
            return self.snapshot()

    @staticmethod
    def _validate_editable_name(name: str) -> None:
        if name not in _WEB_MANAGED_PROVIDER_SECRET_NAMES:
            raise UnsupportedSettingError(f"unsupported provider credential: {name}")
        if os.getenv(name) is not None:
            raise UnsupportedSettingError(f"{name} is externally managed and cannot be changed")


class InstrumentPolicyStore:
    """Manage only activation and bounded collection policy for installed instruments."""

    def __init__(self, path: Path, catalog: InstrumentRegistry) -> None:
        self.path = path.expanduser()
        self.catalog = catalog
        self._lock = threading.RLock()
        self._startup_revision = _revision(_read_bytes(self.path))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            content = _read_bytes(self.path)
            policy = _parse_instrument_policy(content, self.catalog)
            disabled = set(policy["disabled_symbols"])
            overrides = policy["overrides"]
            current = _revision(content)
            return {
                "revision": current,
                "startup_revision": self._startup_revision,
                "restart_required": current != self._startup_revision,
                "scope": "installed_catalog",
                "instruments": [
                    {
                        "symbol": item.symbol,
                        "name": item.name,
                        "description": item.description,
                        "asset_class": item.asset_class.value,
                        "asset_type": item.asset_type,
                        "enabled": item.symbol not in disabled,
                        "quote_poll_seconds": overrides.get(item.symbol, {}).get(
                            "quote_poll_seconds", item.quote_poll_seconds
                        ),
                        "stale_after_seconds": overrides.get(item.symbol, {}).get(
                            "stale_after_seconds", item.stale_after_seconds
                        ),
                    }
                    for item in self.catalog.values()
                ],
            }

    def patch(self, *, instruments: list[dict[str, Any]], expected_revision: str) -> dict[str, Any]:
        with self._lock:
            content = _read_bytes(self.path)
            if not hmac_compare_revision(_revision(content), expected_revision):
                raise RevisionConflictError("instrument policy changed concurrently")
            if not isinstance(instruments, list) or len(instruments) > len(self.catalog):
                raise ValueError("invalid instrument policy update")
            current = _parse_instrument_policy(content, self.catalog)
            disabled = set(current["disabled_symbols"])
            overrides = dict(current["overrides"])
            seen: set[str] = set()
            for update in instruments:
                if not isinstance(update, dict):
                    raise ValueError("instrument updates must be objects")
                symbol = normalize_symbol(str(update.get("symbol", "")))
                if symbol in seen or self.catalog.resolve(symbol) is None:
                    raise ValueError(f"unknown or duplicate installed instrument: {symbol}")
                seen.add(symbol)
                enabled = update.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError(f"enabled must be a boolean for {symbol}")
                if enabled:
                    disabled.discard(symbol)
                else:
                    disabled.add(symbol)
                item = self.catalog[symbol]
                poll = update.get("quote_poll_seconds", item.quote_poll_seconds)
                stale = update.get("stale_after_seconds", item.stale_after_seconds)
                if (
                    isinstance(poll, bool)
                    or not isinstance(poll, int | float)
                    or not 0.25 <= float(poll) <= 86_400
                ):
                    raise ValueError(f"invalid quote polling interval for {symbol}")
                if (
                    isinstance(stale, bool)
                    or not isinstance(stale, int | float)
                    or not 1 <= float(stale) <= 604_800
                ):
                    raise ValueError(f"invalid stale threshold for {symbol}")
                if float(stale) < float(poll):
                    raise ValueError(
                        f"stale threshold must not be shorter than polling for {symbol}"
                    )
                overrides[symbol] = {
                    "quote_poll_seconds": float(poll),
                    "stale_after_seconds": float(stale),
                }
            if len(disabled) >= len(self.catalog):
                raise ValueError("at least one installed instrument must remain enabled")
            payload = {
                "version": 1,
                "disabled_symbols": sorted(disabled),
                "overrides": {symbol: overrides[symbol] for symbol in sorted(overrides)},
            }
            encoded = (
                json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            _atomic_write(self.path, encoded)
            return self.snapshot()


def _parse_instrument_policy(content: bytes, catalog: InstrumentRegistry) -> dict[str, Any]:
    if not content:
        return {"version": 1, "disabled_symbols": [], "overrides": {}}
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedConfigurationError("instrument policy is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) - {"version", "disabled_symbols", "overrides"}:
        raise ManagedConfigurationError("instrument policy has unsupported fields")
    if value.get("version") != 1:
        raise ManagedConfigurationError("unsupported instrument policy version")
    disabled = value.get("disabled_symbols", [])
    overrides = value.get("overrides", {})
    if not isinstance(disabled, list) or not isinstance(overrides, dict):
        raise ManagedConfigurationError("invalid instrument policy structure")
    known = set(catalog.symbols)
    normalized_disabled = [normalize_symbol(str(item)) for item in disabled]
    if len(set(normalized_disabled)) != len(normalized_disabled) or any(
        item not in known for item in normalized_disabled
    ):
        raise ManagedConfigurationError("instrument policy contains an unknown or duplicate symbol")
    normalized_overrides: dict[str, dict[str, float]] = {}
    for raw_symbol, raw_override in overrides.items():
        symbol = normalize_symbol(str(raw_symbol))
        if (
            symbol not in known
            or not isinstance(raw_override, dict)
            or set(raw_override) - {"quote_poll_seconds", "stale_after_seconds"}
        ):
            raise ManagedConfigurationError("instrument policy contains an invalid override")
        item = catalog[symbol]
        poll = raw_override.get("quote_poll_seconds", item.quote_poll_seconds)
        stale = raw_override.get("stale_after_seconds", item.stale_after_seconds)
        if isinstance(poll, bool) or isinstance(stale, bool):
            raise ManagedConfigurationError("instrument policy intervals must be numeric")
        poll_value, stale_value = float(poll), float(stale)
        if (
            not 0.25 <= poll_value <= 86_400
            or not 1 <= stale_value <= 604_800
            or stale_value < poll_value
        ):
            raise ManagedConfigurationError("instrument policy interval is outside safe bounds")
        normalized_overrides[symbol] = {
            "quote_poll_seconds": poll_value,
            "stale_after_seconds": stale_value,
        }
    return {
        "version": 1,
        "disabled_symbols": normalized_disabled,
        "overrides": normalized_overrides,
    }


def apply_instrument_policy(registry: InstrumentRegistry, path: Path) -> InstrumentRegistry:
    content = _read_bytes(path.expanduser())
    policy = _parse_instrument_policy(content, registry)
    disabled = set(policy["disabled_symbols"])
    overrides = policy["overrides"]
    plugins = []
    for plugin in registry.plugins:
        instruments = tuple(
            replace(item, **overrides.get(item.symbol, {}))
            for item in plugin.instruments
            if item.symbol not in disabled
        )
        plugins.append(replace(plugin, instruments=instruments))
    return InstrumentRegistry(plugins)


def build_managed_registry(settings: Settings) -> InstrumentRegistry:
    catalog = build_registry(settings.enabled_plugins)
    return apply_instrument_policy(catalog, settings.managed_instruments_path)


def hmac_compare_revision(current: str, expected: str) -> bool:
    import hmac

    return (
        isinstance(expected, str) and len(expected) == 64 and hmac.compare_digest(current, expected)
    )


__all__ = [
    "InstrumentPolicyStore",
    "ManagedConfigurationError",
    "ManagedEnvironmentStore",
    "ProviderKeyStore",
    "RevisionConflictError",
    "UnsupportedSettingError",
    "apply_instrument_policy",
    "build_managed_registry",
]
