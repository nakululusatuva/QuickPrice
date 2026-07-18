"""Strict, revisioned stores for administrator-managed declarative settings."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal

from .catalog import (
    CATALOG_SCHEMA_VERSION,
    MAX_CATALOG_IMPORT_BYTES,
    CapabilityRoute,
    CatalogGeneration,
    CatalogValidationError,
    InstrumentOwnership,
    ManagedCatalogDocument,
    ManagedInstrumentDefinition,
    ProviderSymbolBinding,
    builtin_instrument_id,
    definition_from_payload,
    document_from_payload,
    generation_from_payload,
)
from .config import _PROVIDER_SECRET_NAMES, Settings
from .plugin_api import InstrumentPlugin
from .registry import InstrumentRegistry, build_registry, normalize_symbol

_ENV_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*$")
_MAX_FILE_BYTES: Final[int] = 64 * 1024
_MAX_CATALOG_FILE_BYTES: Final[int] = 8 * 1024 * 1024
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
class _InstrumentCatalogTransitionToken:
    """Opaque in-process image used only to reverse one runtime transition."""

    content: bytes
    revision: str
    lease_id: str


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
            if not math.isfinite(numeric):
                raise ValueError(f"{self.name} must be finite")
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
        "QUICKPRICE_CATALOG_WARM_TIMEOUT_SECONDS",
        "Catalog warm-up timeout (seconds)",
        "number",
        1,
        7_200,
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
        "QUICKPRICE_ALPACA_STREAM_SYMBOL_LIMIT",
        "Alpaca WebSocket symbol limit",
        "integer",
        0,
        10_000,
    ),
    SettingSpec(
        "QUICKPRICE_ALPACA_REST_CALLS_PER_MINUTE",
        "Alpaca REST calls per minute",
        "integer",
        1,
        10_000,
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
    "QUICKPRICE_CATALOG_WARM_TIMEOUT_SECONDS": "catalog_warm_timeout_seconds",
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
    "QUICKPRICE_ALPACA_STREAM_SYMBOL_LIMIT": "alpaca_stream_symbol_limit",
    "QUICKPRICE_ALPACA_REST_CALLS_PER_MINUTE": "alpaca_rest_calls_per_minute",
    "QUICKPRICE_FINNHUB_CALLS_PER_MINUTE": "finnhub_calls_per_minute",
    "QUICKPRICE_COINGECKO_MONTHLY_CREDITS": "coingecko_monthly_credits",
    "QUICKPRICE_HIGH_FREQUENCY_PUBLISH_MS": "high_frequency_publish_ms",
    "QUICKPRICE_SQLITE_BATCH_SIZE": "sqlite_batch_size",
    "QUICKPRICE_SQLITE_BATCH_MS": "sqlite_batch_ms",
    "QUICKPRICE_LOG_LEVEL": "log_level",
}


def _revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_bytes(path: Path, *, maximum_bytes: int = _MAX_FILE_BYTES) -> bytes:
    if path.is_symlink():
        raise ManagedConfigurationError("managed configuration path cannot be a symbolic link")
    if not path.exists():
        return b""
    if not path.is_file():
        raise ManagedConfigurationError("managed configuration path is not a regular file")
    size = path.stat().st_size
    if size > maximum_bytes:
        raise ManagedConfigurationError("managed configuration file is too large")
    return path.read_bytes()


def _validate_integrity_permissions(path: Path) -> None:
    """Reject catalog paths writable by another local account on POSIX."""

    if os.name != "posix" or not path.exists():
        return
    file_status = path.stat(follow_symlinks=False)
    if file_status.st_uid not in {0, os.geteuid()}:
        raise ManagedConfigurationError("managed instrument catalog has an unsafe owner")
    if stat.S_IMODE(file_status.st_mode) & 0o022:
        raise ManagedConfigurationError("managed instrument catalog is writable by another account")
    parent_status = path.parent.stat(follow_symlinks=False)
    if stat.S_IMODE(parent_status.st_mode) & 0o022:
        raise ManagedConfigurationError(
            "managed instrument catalog directory is writable by another account"
        )


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


_BUILTIN_MUTABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "enabled",
        "quote_poll_seconds",
        "stale_after_seconds",
        "history",
        "routes",
    }
)


def _serialize_catalog_document(document: ManagedCatalogDocument) -> bytes:
    content = (
        json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(content) > _MAX_CATALOG_FILE_BYTES:
        raise CatalogValidationError("managed instrument catalog is too large")
    return content


def _catalog_generation_from_registry(
    catalog: InstrumentRegistry,
    *,
    disabled_symbols: set[str] | None = None,
    overrides: dict[str, dict[str, float]] | None = None,
) -> CatalogGeneration:
    from .providers.compiler import builtin_provider_policy

    disabled = disabled_symbols or set()
    policy_overrides = overrides or {}
    routes_by_symbol: dict[str, dict[str, tuple[str, ...]]] = {}
    for plugin in catalog.plugins:
        for binding in plugin.provider_bindings:
            symbol = normalize_symbol(binding.symbol)
            capability_routes = routes_by_symbol.setdefault(symbol, {})
            existing = capability_routes.get(binding.capability, ())
            capability_routes[binding.capability] = tuple(
                dict.fromkeys((*existing, *(name.strip().lower() for name in binding.providers)))
            )

    definitions: list[ManagedInstrumentDefinition] = []
    for item in catalog.values():
        installed_route_map = routes_by_symbol.get(item.symbol, {})
        builtin_policy = builtin_provider_policy(item.symbol)
        route_map = {
            **builtin_policy.routes,
            **installed_route_map,
        }
        routes = tuple(
            CapabilityRoute(capability=capability, providers=route_map[capability])
            for capability in ("quote", "history", "dividend", "yield")
            if capability in route_map
        )
        definition = ManagedInstrumentDefinition.from_instrument_spec(
            replace(item, **policy_overrides.get(item.symbol, {})),
            instrument_id=builtin_instrument_id(item.symbol),
            ownership=InstrumentOwnership.BUILTIN,
            enabled=item.symbol not in disabled,
            routes=routes,
            provider_symbols=tuple(
                ProviderSymbolBinding(provider=provider, symbol=vendor_symbol)
                for provider, vendor_symbol in builtin_policy.provider_symbols.items()
            ),
        )
        definitions.append(definition)
    return CatalogGeneration.build(definitions)


def _parse_v1_instrument_policy(content: bytes, catalog: InstrumentRegistry) -> dict[str, Any]:
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
    normalized_disabled: list[str] = []
    for item in disabled:
        canonical = catalog.canonical_symbol(normalize_symbol(str(item)))
        if canonical is None:
            raise ManagedConfigurationError(
                "instrument policy contains an unknown or duplicate symbol"
            )
        normalized_disabled.append(canonical)
    if len(set(normalized_disabled)) != len(normalized_disabled):
        raise ManagedConfigurationError("instrument policy contains an unknown or duplicate symbol")
    normalized_overrides: dict[str, dict[str, float]] = {}
    for raw_symbol, raw_override in overrides.items():
        symbol = catalog.canonical_symbol(normalize_symbol(str(raw_symbol)))
        if (
            symbol is None
            or symbol in normalized_overrides
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


def _read_catalog_json(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedConfigurationError("instrument catalog is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ManagedConfigurationError("instrument catalog must be a JSON object")
    return value


def _normalize_legacy_v2_staking_fallbacks(value: dict[str, Any]) -> bool:
    """Remove one obsolete built-in field before strict schema validation."""

    changed = False
    for generation_name in ("active", "staged", "last_known_good"):
        generation = value.get(generation_name)
        if not isinstance(generation, dict):
            continue
        instruments = generation.get("instruments")
        if not isinstance(instruments, list):
            continue
        generation_changed = False
        for instrument in instruments:
            if (
                not isinstance(instrument, dict)
                or instrument.get("ownership") != "builtin"
                or "staking" not in str(instrument.get("asset_type", ""))
            ):
                continue
            income = instrument.get("income")
            if (
                isinstance(income, dict)
                and income.get("fallback_ratio_days") is not None
                and income.get("reward_accrual_mode") != "value_accruing"
            ):
                income["fallback_ratio_days"] = None
                generation_changed = True
        if generation_changed:
            revision_payload = {"instruments": instruments}
            generation["revision"] = hashlib.sha256(
                json.dumps(
                    revision_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            changed = True
    return changed


def _reconcile_generation(
    generation: CatalogGeneration,
    baseline: CatalogGeneration,
) -> CatalogGeneration:
    baseline_by_id = baseline.by_id()
    seen_builtin_ids: set[str] = set()
    reconciled: list[ManagedInstrumentDefinition] = []
    for item in generation.instruments:
        if item.ownership is InstrumentOwnership.CUSTOM:
            reconciled.append(item)
            continue
        expected = baseline_by_id.get(item.id)
        if expected is None or expected.symbol != item.symbol:
            raise ManagedConfigurationError(
                f"managed catalog references an unknown built-in instrument: {item.id}"
            )
        seen_builtin_ids.add(item.id)
        baseline_payload = expected.model_dump(mode="json")
        persisted_payload = item.model_dump(mode="json")
        for field_name in _BUILTIN_MUTABLE_FIELDS - {"routes"}:
            baseline_payload[field_name] = persisted_payload[field_name]
        # An empty v2 route list predates the declarative built-in route compiler.
        # A valid advanced override always retains at least one required route.
        if persisted_payload["routes"]:
            baseline_payload["routes"] = persisted_payload["routes"]
        baseline_payload["archived"] = False
        reconciled.append(definition_from_payload(baseline_payload))
    reconciled.extend(item for item in baseline.instruments if item.id not in seen_builtin_ids)
    try:
        return CatalogGeneration.build(reconciled)
    except ValueError as exc:
        raise ManagedConfigurationError(str(exc)) from exc


def _reconcile_document(
    document: ManagedCatalogDocument,
    baseline: CatalogGeneration,
) -> ManagedCatalogDocument:
    try:
        return ManagedCatalogDocument(
            active=_reconcile_generation(document.active, baseline),
            staged=(
                None
                if document.staged is None
                else _reconcile_generation(document.staged, baseline)
            ),
            last_known_good=(
                None
                if document.last_known_good is None
                else _reconcile_generation(document.last_known_good, baseline)
            ),
        )
    except ValueError as exc:
        raise ManagedConfigurationError(str(exc)) from exc


def _load_catalog_document(
    content: bytes,
    catalog: InstrumentRegistry,
) -> tuple[ManagedCatalogDocument, bool]:
    baseline = _catalog_generation_from_registry(catalog)
    if not content:
        return ManagedCatalogDocument(active=baseline), True
    value = _read_catalog_json(content)
    version = value.get("version")
    if version == 1:
        policy = _parse_v1_instrument_policy(content, catalog)
        active = _catalog_generation_from_registry(
            catalog,
            disabled_symbols=set(policy["disabled_symbols"]),
            overrides=policy["overrides"],
        )
        return ManagedCatalogDocument(active=active), True
    if version != CATALOG_SCHEMA_VERSION:
        raise ManagedConfigurationError("unsupported instrument catalog version")
    normalized_legacy_fallbacks = _normalize_legacy_v2_staking_fallbacks(value)
    try:
        parsed = document_from_payload(value)
    except CatalogValidationError as exc:
        raise ManagedConfigurationError(str(exc)) from exc
    reconciled = _reconcile_document(parsed, baseline)
    return reconciled, normalized_legacy_fallbacks or reconciled != parsed


def _assert_revision(content: bytes, expected_revision: str) -> None:
    if not hmac_compare_revision(_revision(content), expected_revision):
        raise RevisionConflictError("instrument catalog changed concurrently")


def _definition_payload(item: ManagedInstrumentDefinition) -> dict[str, Any]:
    return item.model_dump(mode="json")


def _assert_builtin_update_allowed(
    current: ManagedInstrumentDefinition,
    candidate: ManagedInstrumentDefinition,
) -> None:
    if current.ownership is not InstrumentOwnership.BUILTIN:
        return
    current_payload = _definition_payload(current)
    candidate_payload = _definition_payload(candidate)
    changed = {name for name in current_payload if current_payload[name] != candidate_payload[name]}
    forbidden = changed - _BUILTIN_MUTABLE_FIELDS
    if forbidden:
        raise CatalogValidationError(
            "built-in instrument core fields are read-only: " + ", ".join(sorted(forbidden))
        )


class InstrumentPolicyStore:
    """Persist active, staged, and last-known-good instrument generations.

    The legacy ``snapshot`` and ``patch`` methods remain available while new
    administrator workflows use the staged catalog methods.
    """

    def __init__(
        self,
        path: Path,
        catalog: InstrumentRegistry,
        *,
        defer_migration: bool = False,
    ) -> None:
        self.path = path.expanduser()
        self.catalog = catalog
        self._lock = threading.RLock()
        self._transition_id: str | None = None
        with self._lock:
            _validate_integrity_permissions(self.path)
            content = _read_bytes(self.path, maximum_bytes=_MAX_CATALOG_FILE_BYTES)
            document, needs_write = _load_catalog_document(content, self.catalog)
            self._migration_pending = needs_write
            self._startup_revision = _revision(content)
            self._runtime_active_revision = document.active.revision
        if needs_write and not defer_migration:
            self.persist_migration()

    def persist_migration(self) -> None:
        """Persist a synthesized v2 document after application startup succeeds."""

        with self._lock:
            if self._transition_id is not None:
                raise RevisionConflictError("instrument catalog activation is in progress")
            _validate_integrity_permissions(self.path)
            content = _read_bytes(self.path, maximum_bytes=_MAX_CATALOG_FILE_BYTES)
            document, needs_write = _load_catalog_document(content, self.catalog)
            if not needs_write:
                self._migration_pending = False
                return
            if content:
                try:
                    is_version_one = json.loads(content).get("version") == 1
                except AttributeError, json.JSONDecodeError, UnicodeDecodeError:
                    is_version_one = False
                backup = self.path.with_name(f"{self.path.name}.v1-backup")
                if is_version_one and not backup.exists():
                    _atomic_write(backup, content)
            migrated = _serialize_catalog_document(document)
            _atomic_write(self.path, migrated)
            self._startup_revision = _revision(migrated)
            self._runtime_active_revision = document.active.revision
            self._migration_pending = False

    def _read_locked(self) -> tuple[bytes, ManagedCatalogDocument]:
        _validate_integrity_permissions(self.path)
        content = _read_bytes(self.path, maximum_bytes=_MAX_CATALOG_FILE_BYTES)
        document, _ = _load_catalog_document(content, self.catalog)
        return content, document

    def _write_locked(
        self,
        document: ManagedCatalogDocument,
        transition_token: object | None = None,
    ) -> None:
        if self._transition_id is not None:
            if (
                not isinstance(transition_token, _InstrumentCatalogTransitionToken)
                or transition_token.lease_id != self._transition_id
            ):
                raise RevisionConflictError("instrument catalog activation is in progress")
        _atomic_write(self.path, _serialize_catalog_document(document))

    def capture_transition(self, expected_revision: str) -> object:
        """Capture exact validated bytes for one internal activation transaction."""

        with self._lock:
            if self._transition_id is not None:
                raise RevisionConflictError("instrument catalog activation is already in progress")
            content, _ = self._read_locked()
            _assert_revision(content, expected_revision)
            token = _InstrumentCatalogTransitionToken(
                content=content,
                revision=_revision(content),
                lease_id=str(uuid.uuid7()),
            )
            self._transition_id = token.lease_id
            return token

    def _assert_transition_locked(self, token: object) -> _InstrumentCatalogTransitionToken:
        if (
            not isinstance(token, _InstrumentCatalogTransitionToken)
            or self._transition_id is None
            or token.lease_id != self._transition_id
        ):
            raise RevisionConflictError("instrument catalog activation lease is not current")
        return token

    def commit_transition(self, token: object, expected_revision: str) -> None:
        """Release a successful activation lease after verifying its final file."""

        with self._lock:
            self._assert_transition_locked(token)
            current = _read_bytes(self.path, maximum_bytes=_MAX_CATALOG_FILE_BYTES)
            _assert_revision(current, expected_revision)
            self._transition_id = None

    def abort_transition(self, token: object) -> None:
        """Release a lease that made no file change or could not be restored."""

        with self._lock:
            self._assert_transition_locked(token)
            self._transition_id = None

    def restore_transition(
        self,
        token: object,
        expected_revision: str,
    ) -> dict[str, Any]:
        """CAS-restore active, staged, and last-known-good state exactly."""

        if not isinstance(token, _InstrumentCatalogTransitionToken):
            raise TypeError("invalid instrument catalog transition token")
        if _revision(token.content) != token.revision:
            raise CatalogValidationError("instrument catalog transition token is corrupted")
        # Validate the opaque image before entering the filesystem transition.
        _load_catalog_document(token.content, self.catalog)
        with self._lock:
            self._assert_transition_locked(token)
            current = _read_bytes(self.path, maximum_bytes=_MAX_CATALOG_FILE_BYTES)
            _assert_revision(current, expected_revision)
            _atomic_write(self.path, token.content)
            self._transition_id = None
            return self.catalog_snapshot()

    def active_generation(self) -> CatalogGeneration:
        with self._lock:
            return self._read_locked()[1].active

    def assert_revision(self, expected_revision: str) -> None:
        """Fail before runtime validation if an admin request is already stale."""

        with self._lock:
            content = _read_bytes(self.path, maximum_bytes=_MAX_CATALOG_FILE_BYTES)
            _assert_revision(content, expected_revision)

    def assert_runtime_target(
        self,
        expected_revision: str,
        operation: Literal["activate", "rollback"],
        expected_target_revision: str,
    ) -> None:
        """Atomically recheck both the file and queued generation identity."""

        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            if operation == "activate":
                target = document.staged
            elif operation == "rollback":
                target = document.last_known_good
            else:
                raise ValueError("unsupported catalog runtime operation")
            if target is None or not hmac_compare_revision(
                target.revision,
                expected_target_revision,
            ):
                raise RevisionConflictError("catalog activation target changed")

    def staged_generation(self) -> CatalogGeneration | None:
        with self._lock:
            return self._read_locked()[1].staged

    def last_known_good_generation(self) -> CatalogGeneration | None:
        with self._lock:
            return self._read_locked()[1].last_known_good

    def catalog_snapshot(self) -> dict[str, Any]:
        with self._lock:
            content, document = self._read_locked()
            current_revision = _revision(content)
            active_payload = document.active.model_dump(mode="json")
            staged_payload = (
                None if document.staged is None else document.staged.model_dump(mode="json")
            )
            last_payload = (
                None
                if document.last_known_good is None
                else document.last_known_good.model_dump(mode="json")
            )
            active_instruments = [
                {
                    **_definition_payload(item),
                    "history_enabled": item.history.enabled,
                }
                for item in document.active.instruments
            ]
            return {
                "version": CATALOG_SCHEMA_VERSION,
                "revision": current_revision,
                "startup_revision": self._startup_revision,
                "restart_required": document.active.revision != self._runtime_active_revision,
                "scope": "managed_catalog",
                "active_revision": document.active.revision,
                "staged_revision": (None if document.staged is None else document.staged.revision),
                "last_known_good_revision": (
                    None if document.last_known_good is None else document.last_known_good.revision
                ),
                "active": active_payload,
                "staged": staged_payload,
                "last_known_good": last_payload,
                "instruments": active_instruments,
                "limits": {
                    "custom_instruments": 2_000,
                    "provider_chain": 4,
                    "synthetic_depth": 4,
                },
            }

    def snapshot(self) -> dict[str, Any]:
        return self.catalog_snapshot()

    def mark_runtime_applied(self, revision: str | None = None) -> None:
        """Mark a successfully activated file revision as live in this process."""

        with self._lock:
            content = _read_bytes(self.path, maximum_bytes=_MAX_CATALOG_FILE_BYTES)
            current = _revision(content)
            if revision is not None and not hmac_compare_revision(current, revision):
                raise RevisionConflictError("instrument catalog changed before runtime activation")
            document, _ = _load_catalog_document(content, self.catalog)
            self._startup_revision = current
            self._runtime_active_revision = document.active.revision

    def patch(self, *, instruments: list[dict[str, Any]], expected_revision: str) -> dict[str, Any]:
        """Apply the legacy installed-instrument policy directly to active state."""

        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            active = self._legacy_policy_generation(document.active, instruments)
            updated = ManagedCatalogDocument(
                active=active,
                staged=document.staged,
                last_known_good=document.active,
            )
            self._write_locked(updated)
            return self.catalog_snapshot()

    def stage_patch(
        self,
        *,
        instruments: list[dict[str, Any]],
        expected_revision: str,
    ) -> dict[str, Any]:
        """Stage the bounded legacy policy without changing the active generation."""

        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            staged = self._legacy_policy_generation(self._staged_base(document), instruments)
            updated = ManagedCatalogDocument(
                active=document.active,
                staged=staged,
                last_known_good=document.last_known_good,
            )
            self._write_locked(updated)
            result = self.catalog_snapshot()
            result.update(
                activation_required=True,
                restart_required=False,
                state="staged",
            )
            return result

    def _legacy_policy_generation(
        self,
        base: CatalogGeneration,
        instruments: list[dict[str, Any]],
    ) -> CatalogGeneration:
        if not isinstance(instruments, list) or len(instruments) > len(self.catalog):
            raise ValueError("invalid instrument policy update")
        definitions = list(base.instruments)
        try:
            indexes = {item.symbol: index for index, item in enumerate(definitions)}
            seen: set[str] = set()
            for update in instruments:
                if not isinstance(update, dict) or set(update) - {
                    "symbol",
                    "enabled",
                    "quote_poll_seconds",
                    "stale_after_seconds",
                }:
                    raise ValueError("instrument updates contain unsupported fields")
                requested = normalize_symbol(str(update.get("symbol", "")))
                canonical = self.catalog.canonical_symbol(requested)
                if canonical is None or canonical in seen:
                    raise ValueError(f"unknown or duplicate installed instrument: {requested}")
                seen.add(canonical)
                enabled = update.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError(f"enabled must be a boolean for {canonical}")
                index = indexes[canonical]
                item = definitions[index]
                poll = update.get("quote_poll_seconds", item.quote_poll_seconds)
                stale = update.get("stale_after_seconds", item.stale_after_seconds)
                payload = _definition_payload(item)
                payload.update(
                    enabled=enabled,
                    quote_poll_seconds=poll,
                    stale_after_seconds=stale,
                )
                try:
                    definitions[index] = definition_from_payload(payload)
                except CatalogValidationError as exc:
                    raise ValueError(f"invalid collection policy for {canonical}: {exc}") from exc
            return CatalogGeneration.build(definitions)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def _staged_base(self, document: ManagedCatalogDocument) -> CatalogGeneration:
        return document.active if document.staged is None else document.staged

    def _stage_definitions_locked(
        self,
        document: ManagedCatalogDocument,
        definitions: list[ManagedInstrumentDefinition],
    ) -> None:
        try:
            staged = CatalogGeneration.build(definitions)
            updated = ManagedCatalogDocument(
                active=document.active,
                staged=staged,
                last_known_good=document.last_known_good,
            )
        except ValueError as exc:
            raise CatalogValidationError(str(exc)) from exc
        self._write_locked(updated)

    def _assert_complete_builtin_set(self, generation: CatalogGeneration) -> None:
        expected = {
            item.id
            for item in _catalog_generation_from_registry(self.catalog).instruments
            if item.ownership is InstrumentOwnership.BUILTIN
        }
        received = {
            item.id
            for item in generation.instruments
            if item.ownership is InstrumentOwnership.BUILTIN
        }
        if received != expected:
            raise CatalogValidationError("staged catalog must retain every built-in instrument")

    def create_instrument(
        self,
        instrument: dict[str, Any],
        expected_revision: str,
    ) -> dict[str, Any]:
        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            if not isinstance(instrument, dict):
                raise CatalogValidationError("instrument must be a JSON object")
            if "id" in instrument or "ownership" in instrument:
                raise CatalogValidationError(
                    "custom instrument id and ownership are server-managed"
                )
            payload = dict(instrument)
            payload["id"] = f"custom-{uuid.uuid7()}"
            payload["ownership"] = InstrumentOwnership.CUSTOM.value
            candidate = definition_from_payload(payload)
            base = self._staged_base(document)
            self._stage_definitions_locked(document, [*base.instruments, candidate])
            return self.catalog_snapshot()

    def update_instrument(
        self,
        instrument_id: str,
        updates: dict[str, Any],
        expected_revision: str,
    ) -> dict[str, Any]:
        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            if not isinstance(updates, dict):
                raise CatalogValidationError("instrument updates must be a JSON object")
            if "id" in updates or "ownership" in updates:
                raise CatalogValidationError("instrument id and ownership are immutable")
            base = self._staged_base(document)
            definitions = list(base.instruments)
            index = next(
                (position for position, item in enumerate(definitions) if item.id == instrument_id),
                None,
            )
            if index is None:
                raise KeyError(instrument_id)
            current = definitions[index]
            payload = _definition_payload(current)
            payload.update(updates)
            candidate = definition_from_payload(payload)
            _assert_builtin_update_allowed(current, candidate)
            definitions[index] = candidate
            self._stage_definitions_locked(document, definitions)
            return self.catalog_snapshot()

    def archive_instrument(
        self,
        instrument_id: str,
        expected_revision: str,
    ) -> dict[str, Any]:
        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            base = self._staged_base(document)
            definitions = list(base.instruments)
            index = next(
                (position for position, item in enumerate(definitions) if item.id == instrument_id),
                None,
            )
            if index is None:
                raise KeyError(instrument_id)
            current = definitions[index]
            if current.ownership is InstrumentOwnership.BUILTIN:
                raise CatalogValidationError(
                    "built-in instruments can be disabled but not archived"
                )
            payload = _definition_payload(current)
            payload.update(enabled=False, archived=True)
            definitions[index] = definition_from_payload(payload)
            self._stage_definitions_locked(document, definitions)
            return self.catalog_snapshot()

    def replace_staged(
        self,
        generation: CatalogGeneration | dict[str, Any],
        expected_revision: str,
    ) -> dict[str, Any]:
        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            candidate = (
                generation_from_payload(generation) if isinstance(generation, dict) else generation
            )
            self._assert_complete_builtin_set(candidate)
            current_by_id = self._staged_base(document).by_id()
            for item in candidate.instruments:
                current = current_by_id.get(item.id)
                if current is not None:
                    _assert_builtin_update_allowed(current, item)
            updated = ManagedCatalogDocument(
                active=document.active,
                staged=candidate,
                last_known_good=document.last_known_good,
            )
            self._write_locked(updated)
            return self.catalog_snapshot()

    def import_catalog(
        self,
        payload: dict[str, Any],
        mode: Literal["merge", "replace_custom", "replace-custom"],
        expected_revision: str,
    ) -> dict[str, Any]:
        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            if not isinstance(payload, dict) or set(payload) - {
                "version",
                "revision",
                "instruments",
            }:
                raise CatalogValidationError("catalog import contains unsupported fields")
            try:
                import_size = len(
                    json.dumps(
                        payload,
                        allow_nan=False,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                )
            except (TypeError, ValueError) as exc:
                raise CatalogValidationError(
                    "catalog import must contain only JSON values"
                ) from exc
            if import_size > MAX_CATALOG_IMPORT_BYTES:
                raise CatalogValidationError("catalog import is too large")
            if payload.get("version") != CATALOG_SCHEMA_VERSION:
                raise CatalogValidationError("catalog import has an unsupported version")
            raw_instruments = payload.get("instruments")
            if not isinstance(raw_instruments, list):
                raise CatalogValidationError("catalog import instruments must be an array")
            imported = tuple(definition_from_payload(item) for item in raw_instruments)
            if "revision" in payload:
                incoming_items = generation_from_payload(
                    {"revision": payload["revision"], "instruments": raw_instruments}
                ).instruments
            else:
                incoming_items = imported
            base = self._staged_base(document)
            base_by_id = dict(base.by_id())
            if mode == "merge":
                merged = list(base.instruments)
            elif mode in {"replace_custom", "replace-custom"}:
                merged = [
                    item
                    for item in base.instruments
                    if item.ownership is InstrumentOwnership.BUILTIN
                ]
            else:
                raise CatalogValidationError("catalog import mode must be merge or replace_custom")
            indexes = {item.id: index for index, item in enumerate(merged)}
            for item in incoming_items:
                current = base_by_id.get(item.id)
                if item.ownership is InstrumentOwnership.BUILTIN:
                    if current is None or current.ownership is not InstrumentOwnership.BUILTIN:
                        raise CatalogValidationError(
                            "catalog import contains an unknown built-in id"
                        )
                    _assert_builtin_update_allowed(current, item)
                index = indexes.get(item.id)
                if index is None:
                    indexes[item.id] = len(merged)
                    merged.append(item)
                else:
                    merged[index] = item
            self._stage_definitions_locked(document, merged)
            return self.catalog_snapshot()

    def export_catalog(self, state: Literal["active", "staged"] = "active") -> dict[str, Any]:
        with self._lock:
            document = self._read_locked()[1]
            generation = document.active if state == "active" else document.staged
            if generation is None:
                raise CatalogValidationError("there is no staged catalog to export")
            return {
                "version": CATALOG_SCHEMA_VERSION,
                "revision": generation.revision,
                "instruments": [item.model_dump(mode="json") for item in generation.instruments],
            }

    def validate(self, expected_revision: str | None = None) -> dict[str, Any]:
        with self._lock:
            content, document = self._read_locked()
            if expected_revision is not None:
                _assert_revision(content, expected_revision)
            generation = document.staged or document.active
            active = sum(1 for item in generation.instruments if item.enabled and not item.archived)
            custom = sum(
                1 for item in generation.instruments if item.ownership is InstrumentOwnership.CUSTOM
            )
            return {
                "valid": True,
                "revision": _revision(content),
                "generation_revision": generation.revision,
                "state": "staged" if document.staged is not None else "active",
                "errors": [],
                "warnings": [],
                "counts": {
                    "total": len(generation.instruments),
                    "active": active,
                    "custom": custom,
                },
            }

    def activate_staged(
        self,
        expected_revision: str,
        expected_staged_revision: str | None = None,
        *,
        transition_token: object | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            if document.staged is None:
                raise CatalogValidationError("there is no staged catalog to activate")
            if expected_staged_revision is not None and not hmac_compare_revision(
                document.staged.revision,
                expected_staged_revision,
            ):
                raise RevisionConflictError("staged catalog changed during activation")
            updated = ManagedCatalogDocument(
                active=document.staged,
                staged=None,
                last_known_good=document.active,
            )
            self._write_locked(updated, transition_token)
            return self.catalog_snapshot()

    def rollback(
        self,
        expected_revision: str,
        *,
        transition_token: object | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            content, document = self._read_locked()
            _assert_revision(content, expected_revision)
            if document.last_known_good is None:
                raise CatalogValidationError("there is no last-known-good catalog")
            updated = ManagedCatalogDocument(
                active=document.last_known_good,
                staged=None,
                last_known_good=document.active,
            )
            self._write_locked(updated, transition_token)
            return self.catalog_snapshot()


def _parse_instrument_policy(content: bytes, catalog: InstrumentRegistry) -> dict[str, Any]:
    document, _ = _load_catalog_document(content, catalog)
    active_by_symbol = document.active.by_symbol()
    disabled = [
        item.symbol
        for item in document.active.instruments
        if item.ownership is InstrumentOwnership.BUILTIN and not item.enabled
    ]
    overrides: dict[str, dict[str, float]] = {}
    for installed in catalog.values():
        managed = active_by_symbol.get(installed.symbol)
        if managed is None:
            disabled.append(installed.symbol)
            continue
        if (
            managed.quote_poll_seconds != installed.quote_poll_seconds
            or managed.stale_after_seconds != installed.stale_after_seconds
        ):
            overrides[installed.symbol] = {
                "quote_poll_seconds": managed.quote_poll_seconds,
                "stale_after_seconds": managed.stale_after_seconds,
            }
    return {
        "version": CATALOG_SCHEMA_VERSION,
        "disabled_symbols": sorted(disabled),
        "overrides": overrides,
    }


def apply_instrument_policy(registry: InstrumentRegistry, path: Path) -> InstrumentRegistry:
    content = _read_bytes(path.expanduser(), maximum_bytes=_MAX_CATALOG_FILE_BYTES)
    document, _ = _load_catalog_document(content, registry)
    active = {
        item.symbol: item
        for item in document.active.instruments
        if item.enabled and not item.archived
    }
    installed_symbols = set(registry.symbols)
    plugins: list[InstrumentPlugin] = []
    for plugin in registry.plugins:
        instruments = tuple(
            active[item.symbol].to_instrument_spec()
            for item in plugin.instruments
            if item.symbol in active
        )
        plugins.append(replace(plugin, instruments=instruments))
    custom = tuple(
        item.to_instrument_spec()
        for item in document.active.instruments
        if item.symbol not in installed_symbols and item.enabled and not item.archived
    )
    if custom:
        plugins.append(
            InstrumentPlugin(
                plugin_id="managed-custom",
                version=str(CATALOG_SCHEMA_VERSION),
                instruments=custom,
                provider_installer=_managed_noop_installer,
            )
        )
    return InstrumentRegistry(plugins)


def _managed_noop_installer(_context: Any) -> None:
    """Keep custom metadata valid until the dynamic route compiler installs routes."""


def build_managed_registry(settings: Settings) -> InstrumentRegistry:
    catalog = build_registry(settings.enabled_plugins)
    return apply_instrument_policy(catalog, settings.managed_instruments_path)


def hmac_compare_revision(current: str, expected: str) -> bool:
    import hmac

    return (
        isinstance(expected, str) and len(expected) == 64 and hmac.compare_digest(current, expected)
    )


__all__ = [
    "CatalogValidationError",
    "InstrumentPolicyStore",
    "ManagedConfigurationError",
    "ManagedEnvironmentStore",
    "ProviderKeyStore",
    "RevisionConflictError",
    "UnsupportedSettingError",
    "apply_instrument_policy",
    "build_managed_registry",
]
