"""Safe, serializable metadata for providers installed with QuickPrice.

The admin catalog may refer only to providers declared in this module.  URLs,
headers, Python import paths, and credentials are deliberately absent from
instrument definitions; every network search below selects a fixed endpoint
from this trusted descriptor catalog.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
import time
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import count
from math import ceil
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import aiohttp

from quickprice.instrument_policy import (
    COINGECKO_SUPPORTED_QUOTE_ASSETS,
    SUPPORTED_TREASURY_PROXY_FRED_SERIES,
)
from quickprice.plugin_api import AssetClass
from quickprice.registry import normalize_symbol

from .base import Capability, ProviderUnavailable

MAX_PROVIDER_SEARCH_QUERY_LENGTH = 100
MAX_PROVIDER_SEARCH_RESULTS = 50
PROVIDER_SEARCH_CACHE_TTL_SECONDS = 300.0
PROVIDER_SEARCH_CACHE_MAX_ENTRIES = 256
PROVIDER_FULL_LIST_CACHE_TTL_SECONDS = 900.0
MAX_PROVIDER_SEARCH_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CONCURRENT_PROVIDER_SEARCHES = 4
MAX_PROVIDER_BINDINGS_PER_VERIFICATION = 16_000
MAX_PROVIDER_VERIFICATION_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_VERIFICATION_CATALOG_ROWS = 100_000


class ProviderKind(StrEnum):
    MARKET_DATA = "market_data"
    INCOME_DATA = "income_data"
    DERIVED = "derived"


class VendorSymbolKind(StrEnum):
    EXCHANGE_COMPACT = "exchange_compact"
    EXCHANGE_PAIR = "exchange_pair"
    LISTED_TICKER = "listed_ticker"
    MARKET_OR_TICKER = "market_or_ticker"
    COIN_ID = "coin_id"
    FRED_SERIES = "fred_series"
    CANONICAL = "canonical"
    NONE = "none"


class BindingVerificationMode(StrEnum):
    STATIC_DETERMINISTIC = "static_deterministic"
    UPSTREAM_CATALOG = "upstream_catalog"
    OPAQUE_UPSTREAM_IDENTITY = "opaque_upstream_identity"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Trusted provider capabilities and policy exposed to the admin UI."""

    name: str
    display_name: str
    kind: ProviderKind
    capabilities: frozenset[Capability]
    asset_classes: frozenset[AssetClass]
    credential_fields: tuple[str, ...]
    setting_fields: tuple[str, ...]
    fixed_hosts: tuple[str, ...]
    vendor_symbol_kind: VendorSymbolKind
    binding_verification: BindingVerificationMode
    search_supported: bool = False
    streaming_quotes: bool = False
    stream_symbol_limit: int | None = None
    stream_symbol_limit_setting: str | None = None
    rest_calls_per_minute_setting: str | None = None
    quote_credit_cost: Decimal = Decimal(0)
    history_credit_cost: Decimal = Decimal(0)
    dividend_credit_cost: Decimal = Decimal(0)
    yield_credit_cost: Decimal = Decimal(0)
    notes: str = ""

    def supports(self, asset_class: AssetClass | str, capability: Capability | str) -> bool:
        return (
            AssetClass(asset_class) in self.asset_classes
            and Capability(capability) in self.capabilities
        )

    def credentials_configured(self, settings: Any | None) -> bool:
        if not self.setting_fields:
            return True
        if settings is None:
            return False
        return all(bool(getattr(settings, field, None)) for field in self.setting_fields)

    def credit_cost(self, capability: Capability | str) -> Decimal:
        return {
            Capability.QUOTE: self.quote_credit_cost,
            Capability.HISTORY: self.history_credit_cost,
            Capability.DIVIDEND: self.dividend_credit_cost,
            Capability.YIELD: self.yield_credit_cost,
        }[Capability(capability)]

    def as_dict(self, settings: Any | None = None) -> dict[str, Any]:
        stream_limit = self.stream_symbol_limit
        if settings is not None and self.stream_symbol_limit_setting is not None:
            stream_limit = int(getattr(settings, self.stream_symbol_limit_setting))
        rest_calls_per_minute = None
        if settings is not None and self.rest_calls_per_minute_setting is not None:
            rest_calls_per_minute = int(getattr(settings, self.rest_calls_per_minute_setting))
        return {
            "name": self.name,
            "display_name": self.display_name,
            "kind": self.kind.value,
            "capabilities": sorted(item.value for item in self.capabilities),
            "asset_classes": sorted(item.value for item in self.asset_classes),
            "credential_fields": list(self.credential_fields),
            "credentials_configured": self.credentials_configured(settings),
            "fixed_hosts": list(self.fixed_hosts),
            "vendor_symbol_kind": self.vendor_symbol_kind.value,
            "binding_verification": self.binding_verification.value,
            "search_supported": self.search_supported,
            "streaming_quotes": self.streaming_quotes,
            "operational_limits": {
                "stream_symbols": stream_limit,
                "rest_calls_per_minute": rest_calls_per_minute,
            },
            "credit_costs": {
                capability.value: float(self.credit_cost(capability)) for capability in Capability
            },
            "notes": self.notes,
        }


_CRYPTO = frozenset({AssetClass.CRYPTO})
_LISTED = frozenset({AssetClass.EQUITY, AssetClass.BOND})
_FX = frozenset({AssetClass.FX})
_ALL_ASSETS = frozenset(AssetClass)
_QUOTE_HISTORY = frozenset({Capability.QUOTE, Capability.HISTORY})


def _descriptor(
    name: str,
    display_name: str,
    *,
    kind: ProviderKind = ProviderKind.MARKET_DATA,
    capabilities: frozenset[Capability],
    asset_classes: frozenset[AssetClass],
    credentials: tuple[tuple[str, str], ...] = (),
    hosts: tuple[str, ...] = (),
    symbol_kind: VendorSymbolKind = VendorSymbolKind.NONE,
    binding_verification: BindingVerificationMode = (BindingVerificationMode.STATIC_DETERMINISTIC),
    search: bool = False,
    streaming: bool = False,
    stream_limit: int | None = None,
    stream_limit_setting: str | None = None,
    rest_rate_setting: str | None = None,
    quote_cost: str = "0",
    history_cost: str = "0",
    dividend_cost: str = "0",
    yield_cost: str = "0",
    notes: str = "",
) -> ProviderDescriptor:
    return ProviderDescriptor(
        name=name,
        display_name=display_name,
        kind=kind,
        capabilities=capabilities,
        asset_classes=asset_classes,
        credential_fields=tuple(public for public, _ in credentials),
        setting_fields=tuple(setting for _, setting in credentials),
        fixed_hosts=hosts,
        vendor_symbol_kind=symbol_kind,
        binding_verification=binding_verification,
        search_supported=search,
        streaming_quotes=streaming,
        stream_symbol_limit=stream_limit,
        stream_symbol_limit_setting=stream_limit_setting,
        rest_calls_per_minute_setting=rest_rate_setting,
        quote_credit_cost=Decimal(quote_cost),
        history_credit_cost=Decimal(history_cost),
        dividend_credit_cost=Decimal(dividend_cost),
        yield_credit_cost=Decimal(yield_cost),
        notes=notes,
    )


PROVIDER_CATALOG: Mapping[str, ProviderDescriptor] = {
    item.name: item
    for item in (
        _descriptor(
            "binance",
            "Binance Spot",
            capabilities=_QUOTE_HISTORY,
            asset_classes=_CRYPTO,
            hosts=("api.binance.com", "stream.binance.com"),
            symbol_kind=VendorSymbolKind.EXCHANGE_COMPACT,
            binding_verification=BindingVerificationMode.UPSTREAM_CATALOG,
            search=True,
            streaming=True,
            notes="Public spot trades, books, and klines.",
        ),
        _descriptor(
            "okx",
            "OKX Spot",
            capabilities=_QUOTE_HISTORY,
            asset_classes=_CRYPTO,
            hosts=("www.okx.com",),
            symbol_kind=VendorSymbolKind.EXCHANGE_PAIR,
            binding_verification=BindingVerificationMode.UPSTREAM_CATALOG,
            search=True,
            notes="Public spot books and candles.",
        ),
        _descriptor(
            "kraken",
            "Kraken Spot",
            capabilities=_QUOTE_HISTORY,
            asset_classes=_CRYPTO,
            hosts=("api.kraken.com", "ws.kraken.com"),
            symbol_kind=VendorSymbolKind.EXCHANGE_COMPACT,
            binding_verification=BindingVerificationMode.UPSTREAM_CATALOG,
            search=True,
            streaming=True,
            notes="Public spot trades and OHLC; history is limited to the latest 720 bars.",
        ),
        _descriptor(
            "coingecko",
            "CoinGecko Demo",
            capabilities=_QUOTE_HISTORY,
            asset_classes=_CRYPTO,
            credentials=(("COINGECKO_API_KEY", "coingecko_api_key"),),
            hosts=("api.coingecko.com",),
            symbol_kind=VendorSymbolKind.COIN_ID,
            binding_verification=BindingVerificationMode.OPAQUE_UPSTREAM_IDENTITY,
            search=True,
            quote_cost="1",
            history_cost="1",
            notes="Aggregated fallback data with a monthly credit allowance.",
        ),
        _descriptor(
            "alpaca",
            "Alpaca IEX",
            capabilities=frozenset({Capability.QUOTE, Capability.HISTORY, Capability.DIVIDEND}),
            asset_classes=_LISTED,
            credentials=(
                ("ALPACA_API_KEY", "alpaca_api_key"),
                ("ALPACA_API_SECRET", "alpaca_api_secret"),
            ),
            hosts=("data.alpaca.markets", "paper-api.alpaca.markets"),
            symbol_kind=VendorSymbolKind.LISTED_TICKER,
            binding_verification=BindingVerificationMode.UPSTREAM_CATALOG,
            search=True,
            streaming=True,
            stream_limit=30,
            stream_limit_setting="alpaca_stream_symbol_limit",
            rest_rate_setting="alpaca_rest_calls_per_minute",
            notes="IEX single-venue market data for personal internal use.",
        ),
        _descriptor(
            "finnhub",
            "Finnhub",
            capabilities=frozenset({Capability.QUOTE}),
            asset_classes=_LISTED,
            credentials=(("FINNHUB_API_KEY", "finnhub_api_key"),),
            hosts=("api.finnhub.io", "ws.finnhub.io"),
            symbol_kind=VendorSymbolKind.LISTED_TICKER,
            binding_verification=BindingVerificationMode.UPSTREAM_CATALOG,
            search=True,
            streaming=True,
            stream_limit=50,
            rest_rate_setting="finnhub_calls_per_minute",
            quote_cost="1",
            notes="Quote-only fallback under the configured minute allowance.",
        ),
        _descriptor(
            "twelve_data",
            "Twelve Data",
            capabilities=_QUOTE_HISTORY,
            asset_classes=frozenset({*_LISTED, *_FX}),
            credentials=(("TWELVE_DATA_API_KEY", "twelve_data_api_key"),),
            hosts=("api.twelvedata.com",),
            symbol_kind=VendorSymbolKind.MARKET_OR_TICKER,
            binding_verification=BindingVerificationMode.UPSTREAM_CATALOG,
            search=True,
            quote_cost="1",
            history_cost="1",
            notes="REST market data governed by the configured daily credit budget.",
        ),
        _descriptor(
            "alpha_vantage",
            "Alpha Vantage",
            capabilities=_QUOTE_HISTORY,
            asset_classes=frozenset({*_LISTED, *_FX}),
            credentials=(("ALPHA_VANTAGE_API_KEY", "alpha_vantage_api_key"),),
            hosts=("www.alphavantage.co",),
            symbol_kind=VendorSymbolKind.MARKET_OR_TICKER,
            binding_verification=BindingVerificationMode.UPSTREAM_CATALOG,
            search=True,
            quote_cost="1",
            history_cost="1",
            notes="Low-frequency emergency fallback; dividend data is excluded from automatic routes.",
        ),
        _descriptor(
            "fred",
            "Federal Reserve Economic Data",
            kind=ProviderKind.INCOME_DATA,
            capabilities=frozenset({Capability.YIELD}),
            asset_classes=frozenset({AssetClass.BOND}),
            credentials=(("FRED_API_KEY", "fred_api_key"),),
            hosts=("api.stlouisfed.org",),
            symbol_kind=VendorSymbolKind.FRED_SERIES,
            search=True,
            yield_cost="1",
            notes="Controlled United States Treasury series for proxy yields.",
        ),
        _descriptor(
            "binance_wbeth_rate",
            "Binance WBETH Rate",
            kind=ProviderKind.INCOME_DATA,
            capabilities=frozenset({Capability.YIELD}),
            asset_classes=_CRYPTO,
            credentials=(
                ("BINANCE_API_KEY", "binance_api_key"),
                ("BINANCE_API_SECRET", "binance_api_secret"),
            ),
            hosts=("api.binance.com",),
            symbol_kind=VendorSymbolKind.CANONICAL,
            yield_cost="1",
        ),
        _descriptor(
            "ethereum_exchange_rate",
            "Ethereum Exchange Rate",
            kind=ProviderKind.INCOME_DATA,
            capabilities=frozenset({Capability.YIELD}),
            asset_classes=_CRYPTO,
            credentials=(("ETHEREUM_RPC_URLS", "ethereum_rpc_urls"),),
            symbol_kind=VendorSymbolKind.CANONICAL,
        ),
        _descriptor(
            "lido",
            "Lido APR",
            kind=ProviderKind.INCOME_DATA,
            capabilities=frozenset({Capability.YIELD}),
            asset_classes=_CRYPTO,
            hosts=("eth-api.lido.fi",),
            symbol_kind=VendorSymbolKind.CANONICAL,
        ),
        _descriptor(
            "okx_beth_yield",
            "OKX BETH APR",
            kind=ProviderKind.INCOME_DATA,
            capabilities=frozenset({Capability.YIELD}),
            asset_classes=_CRYPTO,
            hosts=("www.okx.com",),
            symbol_kind=VendorSymbolKind.CANONICAL,
        ),
        _descriptor(
            "staking_market_ratio_proxy",
            "Staking Market Ratio Proxy",
            kind=ProviderKind.DERIVED,
            capabilities=frozenset({Capability.YIELD}),
            asset_classes=_CRYPTO,
            symbol_kind=VendorSymbolKind.NONE,
            binding_verification=BindingVerificationMode.NOT_APPLICABLE,
            notes="Annualizes the configured token-to-underlying ratio lookback.",
        ),
        _descriptor(
            "synthetic",
            "Restricted Synthetic",
            kind=ProviderKind.DERIVED,
            capabilities=_QUOTE_HISTORY,
            asset_classes=_ALL_ASSETS,
            symbol_kind=VendorSymbolKind.NONE,
            binding_verification=BindingVerificationMode.NOT_APPLICABLE,
        ),
        _descriptor(
            "synthetic_fx",
            "USD Hub FX Synthetic",
            kind=ProviderKind.DERIVED,
            capabilities=_QUOTE_HISTORY,
            asset_classes=_FX,
            symbol_kind=VendorSymbolKind.NONE,
            binding_verification=BindingVerificationMode.NOT_APPLICABLE,
        ),
    )
}


_COMPACT_PATTERN = re.compile(r"^[A-Z0-9]{4,30}$")
_PAIR_PATTERN = re.compile(r"^[A-Z0-9]{2,15}-[A-Z0-9]{2,15}$")
_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,19}$")
_MARKET_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,19}(?:/[A-Z0-9][A-Z0-9._-]{0,19})?$")
_COIN_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CANONICAL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*:[A-Z0-9][A-Z0-9._-]*$")


def canonical_provider_name(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in PROVIDER_CATALOG:
        return normalized
    if normalized == "synthetic_fx_history":
        return "synthetic_fx"
    if normalized.startswith("synthetic_"):
        return "synthetic"
    return normalized


def get_provider_descriptor(name: str) -> ProviderDescriptor:
    normalized = canonical_provider_name(name)
    try:
        return PROVIDER_CATALOG[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown provider: {name}") from exc


def list_provider_descriptors() -> tuple[ProviderDescriptor, ...]:
    return tuple(PROVIDER_CATALOG.values())


def provider_catalog_snapshot(settings: Any | None = None) -> dict[str, Any]:
    """Return a secret-free document suitable for an admin API response."""

    return {
        "schema_version": 1,
        "providers": [item.as_dict(settings) for item in list_provider_descriptors()],
    }


def validate_provider_symbol(provider: str, value: str) -> str:
    descriptor = get_provider_descriptor(provider)
    if not isinstance(value, str):
        raise ValueError("vendor symbol must be a string")
    candidate = value.strip()
    if not candidate or len(candidate) > 80 or any(ord(character) < 32 for character in candidate):
        raise ValueError("vendor symbol is empty or too long")
    if "://" in candidate or "@" in candidate or "?" in candidate or "#" in candidate:
        raise ValueError("vendor symbol contains a forbidden URL or credential delimiter")
    kind = descriptor.vendor_symbol_kind
    if kind is VendorSymbolKind.NONE:
        raise ValueError(f"provider {descriptor.name} does not accept a vendor symbol")
    if kind is VendorSymbolKind.COIN_ID:
        normalized = candidate.lower()
        valid = bool(_COIN_ID_PATTERN.fullmatch(normalized))
    else:
        normalized = candidate.upper()
        valid = {
            VendorSymbolKind.EXCHANGE_COMPACT: bool(_COMPACT_PATTERN.fullmatch(normalized)),
            VendorSymbolKind.EXCHANGE_PAIR: bool(_PAIR_PATTERN.fullmatch(normalized)),
            VendorSymbolKind.LISTED_TICKER: bool(_TICKER_PATTERN.fullmatch(normalized)),
            VendorSymbolKind.MARKET_OR_TICKER: bool(_MARKET_PATTERN.fullmatch(normalized)),
            VendorSymbolKind.FRED_SERIES: normalized in SUPPORTED_TREASURY_PROXY_FRED_SERIES,
            VendorSymbolKind.CANONICAL: bool(_CANONICAL_PATTERN.fullmatch(normalized)),
        }.get(kind, False)
    if not valid:
        raise ValueError(f"invalid {descriptor.name} vendor symbol")
    return normalized


def validate_provider_binding_identity(
    instrument_symbol: str,
    asset_class: AssetClass | str,
    provider: str,
    vendor_symbol: str,
) -> str:
    """Validate syntax and deterministic canonical-to-vendor identity."""

    canonical = normalize_symbol(instrument_symbol)
    try:
        base, quote = canonical.split(":", 1)
        normalized_asset_class = AssetClass(asset_class)
    except ValueError as exc:
        raise ValueError("instrument binding identity is invalid") from exc
    descriptor = get_provider_descriptor(provider)
    normalized_provider = descriptor.name
    normalized_vendor = validate_provider_symbol(normalized_provider, vendor_symbol)
    if normalized_asset_class not in descriptor.asset_classes:
        raise ValueError(
            f"provider binding is incompatible with {canonical}: {normalized_provider}"
        )
    upper_vendor = normalized_vendor.upper()
    if normalized_provider == "binance":
        valid = upper_vendor == f"{base}{quote}"
    elif normalized_provider == "okx":
        valid = upper_vendor == f"{base}-{quote}"
    elif normalized_provider == "kraken":
        aliases = {"BTC": "XBT", "DOGE": "XDG"}
        valid = upper_vendor == f"{aliases.get(base, base)}{aliases.get(quote, quote)}"
    elif normalized_provider in {"alpaca", "finnhub", "twelve_data", "alpha_vantage"}:
        if normalized_asset_class is AssetClass.FX:
            valid = upper_vendor == f"{base}/{quote}"
        else:
            valid = quote == "USD" and upper_vendor == base
    elif descriptor.vendor_symbol_kind is VendorSymbolKind.COIN_ID:
        valid = quote in COINGECKO_SUPPORTED_QUOTE_ASSETS
    elif descriptor.vendor_symbol_kind is VendorSymbolKind.CANONICAL:
        valid = upper_vendor == canonical
    else:
        # FRED identity is additionally tied to IncomePolicy.fred_series by
        # the managed-catalog adapter; its controlled syntax is sufficient here.
        valid = True
    if not valid:
        raise ValueError(f"provider binding does not match {canonical}: {normalized_provider}")
    return normalized_vendor


def estimate_daily_credits(
    provider: str,
    capability: Capability | str,
    *,
    poll_seconds: float,
    instrument_count: int = 1,
) -> dict[str, Any]:
    """Return a deterministic worst-case polling estimate, not a quota reservation."""

    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if instrument_count <= 0:
        raise ValueError("instrument_count must be positive")
    descriptor = get_provider_descriptor(provider)
    normalized_capability = Capability(capability)
    cost = descriptor.credit_cost(normalized_capability)
    requests = ceil(86_400 / poll_seconds) * instrument_count
    credits = Decimal(requests) * cost
    return {
        "provider": descriptor.name,
        "capability": normalized_capability.value,
        "requests_per_day": requests,
        "credits_per_request": float(cost),
        "estimated_credits_per_day": float(credits),
        "basis": "worst_case_polling",
    }


def _clean_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("search query must be a string")
    normalized = query.strip()
    if not normalized or len(normalized) > MAX_PROVIDER_SEARCH_QUERY_LENGTH:
        raise ValueError("search query is empty or too long")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("search query contains control characters")
    return normalized


type SearchFetcher = Callable[[str, str, Mapping[str, Any], Mapping[str, str]], Awaitable[Any]]
type SearchCreditReserver = Callable[[str, int], Awaitable[bool]]
type BindingVerificationFetcher = SearchFetcher


@dataclass(frozen=True, slots=True)
class ProviderBindingFailure:
    provider: str
    symbol: str
    code: str
    status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "symbol": self.symbol,
            "code": self.code,
            "status": self.status,
        }


class ProviderBindingVerificationError(RuntimeError):
    """One or more routed bindings could not be safely established."""

    def __init__(self, failures: Iterable[ProviderBindingFailure]) -> None:
        self.failures = tuple(failures)
        super().__init__(
            f"provider binding verification failed for {len(self.failures)} binding(s)"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": "provider_binding_verification_failed",
            "failures": [failure.as_dict() for failure in self.failures],
        }


@dataclass(frozen=True, slots=True)
class _BindingCandidate:
    provider: str
    symbol: str
    vendor_symbol: str


class _BoundedSearchCache:
    """Small per-process TTL cache with per-key singleflight collapse."""

    def __init__(self, *, ttl_seconds: float, maximum_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.maximum_entries = maximum_entries
        self._entries: OrderedDict[
            tuple[str, str, str, int, str],
            tuple[float, dict[str, Any] | tuple[str, int | None]],
        ] = OrderedDict()
        self._locks: dict[tuple[str, str, str, int, str], asyncio.Lock] = {}

    @staticmethod
    def _copy(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": value["provider"],
            "query": value["query"],
            "results": [dict(item) for item in value["results"]],
        }

    @classmethod
    def _read(cls, value: dict[str, Any] | tuple[str, int | None]) -> dict[str, Any]:
        if isinstance(value, tuple):
            raise ProviderUnavailable(
                value[0],
                "symbol search is temporarily unavailable",
                status=value[1],
            )
        return cls._copy(value)

    async def get_or_load(
        self,
        key: tuple[str, str, str, int, str],
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and now < cached[0]:
            self._entries.move_to_end(key)
            return self._read(cached[1])
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._entries.get(key)
            if cached is not None and now < cached[0]:
                self._entries.move_to_end(key)
                return self._read(cached[1])
            try:
                value = await loader()
            except ProviderUnavailable as exc:
                self._entries[key] = (
                    now + min(30.0, self.ttl_seconds),
                    (exc.provider, exc.status),
                )
                self._entries.move_to_end(key)
                self._trim()
                raise ProviderUnavailable(
                    exc.provider,
                    "symbol search is temporarily unavailable",
                    status=exc.status,
                ) from None
            self._entries[key] = (now + self.ttl_seconds, self._copy(value))
            self._entries.move_to_end(key)
            self._trim()
            return self._copy(value)

    def _trim(self) -> None:
        while len(self._entries) > self.maximum_entries:
            evicted_key, _ = self._entries.popitem(last=False)
            self._locks.pop(evicted_key, None)


_SEARCH_CACHE = _BoundedSearchCache(
    ttl_seconds=PROVIDER_SEARCH_CACHE_TTL_SECONDS,
    maximum_entries=PROVIDER_SEARCH_CACHE_MAX_ENTRIES,
)


class _FullListCache:
    """Cache query-independent exchange/asset lists before local filtering."""

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str], tuple[float, Any]] = OrderedDict()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def get_or_load(
        self,
        key: tuple[str, str],
        loader: Callable[[], Awaitable[Any]],
    ) -> Any:
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and now < cached[0]:
            self._entries.move_to_end(key)
            return cached[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._entries.get(key)
            if cached is not None and now < cached[0]:
                self._entries.move_to_end(key)
                return cached[1]
            payload = await loader()
            self._entries[key] = (now + PROVIDER_FULL_LIST_CACHE_TTL_SECONDS, payload)
            self._entries.move_to_end(key)
            while len(self._entries) > 32:
                evicted, _ = self._entries.popitem(last=False)
                self._locks.pop(evicted, None)
            return payload


_FULL_LIST_CACHE = _FullListCache()
_FULL_LIST_PROVIDERS = frozenset({"binance", "okx", "kraken", "alpaca"})


_SEARCH_GATES: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    tuple[asyncio.Semaphore, dict[str, asyncio.Semaphore]],
] = WeakKeyDictionary()
_FETCHER_SCOPES: WeakKeyDictionary[Any, str] = WeakKeyDictionary()
_FETCHER_SCOPE_SEQUENCE = count(1)


@asynccontextmanager
async def _search_network_slot(provider: str) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    global_gate, provider_gates = _SEARCH_GATES.setdefault(
        loop,
        (asyncio.Semaphore(MAX_CONCURRENT_PROVIDER_SEARCHES), {}),
    )
    provider_gate = provider_gates.setdefault(provider, asyncio.Semaphore(1))
    async with global_gate, provider_gate:
        yield


def _search_scope(
    settings: Any, descriptor: ProviderDescriptor, fetcher: SearchFetcher | None
) -> str:
    credential_material = "\0".join(
        str(getattr(settings, field, "")) for field in descriptor.setting_fields
    )
    digest = hashlib.sha256(credential_material.encode("utf-8")).hexdigest()[:16]
    if fetcher is None:
        fetcher_scope = "default"
    else:
        try:
            fetcher_scope = _FETCHER_SCOPES.get(fetcher)
            if fetcher_scope is None:
                fetcher_scope = f"injected-{next(_FETCHER_SCOPE_SEQUENCE)}"
                _FETCHER_SCOPES[fetcher] = fetcher_scope
        except TypeError:
            # Unhashable or non-weak-referenceable fixture callables are not
            # assigned a reusable cache identity.
            fetcher_scope = f"ephemeral-{next(_FETCHER_SCOPE_SEQUENCE)}"
    return f"{digest}:{fetcher_scope}"


async def _reserve_search_credit(
    descriptor: ProviderDescriptor,
    reserver: SearchCreditReserver | None,
) -> None:
    cost = descriptor.quote_credit_cost
    if cost == 0:
        return
    if cost != cost.to_integral_value() or cost > Decimal(2**31 - 1):
        raise ProviderUnavailable(descriptor.name, "invalid symbol search credit policy")
    if reserver is None:
        raise ProviderUnavailable(
            descriptor.name,
            "shared provider quota ledger is unavailable",
            status=503,
        )
    if not await reserver(descriptor.name, int(cost)):
        raise ProviderUnavailable(
            descriptor.name,
            "symbol search quota is exhausted",
            status=429,
        )


async def _bounded_response_json(
    response: aiohttp.ClientResponse,
    provider: str,
    *,
    maximum_bytes: int = MAX_PROVIDER_SEARCH_RESPONSE_BYTES,
) -> Any:
    content_length = response.content_length
    if content_length is not None and content_length > maximum_bytes:
        raise ProviderUnavailable(provider, "symbol search response is too large")
    payload = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        payload.extend(chunk)
        if len(payload) > maximum_bytes:
            raise ProviderUnavailable(provider, "symbol search response is too large")
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        raise ProviderUnavailable(provider, "symbol search returned malformed JSON") from None


async def _bounded_response_text(
    response: aiohttp.ClientResponse,
    provider: str,
    *,
    maximum_bytes: int,
) -> str:
    content_length = response.content_length
    if content_length is not None and content_length > maximum_bytes:
        raise ProviderUnavailable(provider, "binding catalog response is too large")
    payload = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        payload.extend(chunk)
        if len(payload) > maximum_bytes:
            raise ProviderUnavailable(provider, "binding catalog response is too large")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ProviderUnavailable(provider, "binding catalog returned malformed text") from None


async def _default_search_fetcher(
    provider: str,
    url: str,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    settings: Any,
) -> Any:
    descriptor = get_provider_descriptor(provider)
    hostname = (urlsplit(url).hostname or "").lower()
    if hostname not in descriptor.fixed_hosts:
        raise ValueError("provider search attempted an untrusted host")
    timeout = aiohttp.ClientTimeout(total=float(getattr(settings, "provider_timeout_seconds", 8)))
    request_options: dict[str, Any] = {
        "params": params,
        "headers": headers,
        "allow_redirects": False,
    }
    proxy_resolver = getattr(settings, "proxy_url_for_provider", None)
    if callable(proxy_resolver):
        proxy = proxy_resolver(descriptor.name)
        if proxy:
            request_options["proxy"] = proxy
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, **request_options) as response:
                if response.status >= 400:
                    raise ProviderUnavailable(
                        descriptor.name, "symbol search was rejected", status=response.status
                    )
                return await _bounded_response_json(response, descriptor.name)
    except ProviderUnavailable:
        raise
    except TimeoutError, aiohttp.ClientError, ValueError:
        raise ProviderUnavailable(descriptor.name, "symbol search is unavailable") from None


def _binding_candidates(plan: Any, symbols: Iterable[str]) -> tuple[_BindingCandidate, ...]:
    selected_symbols = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
    candidates: dict[tuple[str, str, str], _BindingCandidate] = {}

    def add(provider: str, symbol: str, vendor_symbol: str) -> None:
        candidate = _BindingCandidate(
            provider=provider,
            symbol=symbol,
            vendor_symbol=validate_provider_symbol(provider, vendor_symbol),
        )
        candidates[(candidate.provider, candidate.symbol, candidate.vendor_symbol)] = candidate

    for symbol in selected_symbols:
        instrument = plan.instrument(symbol)
        if instrument is None:
            raise ProviderBindingVerificationError(
                (ProviderBindingFailure("catalog", symbol, "instrument_not_compiled"),)
            )
        routed = {
            canonical_provider_name(provider)
            for providers in instrument.routes.values()
            for provider in providers
        }
        for raw_provider, raw_vendor_symbol in instrument.provider_symbols.items():
            provider = canonical_provider_name(raw_provider)
            if provider not in routed:
                continue
            add(provider, symbol, raw_vendor_symbol)

    # Managed non-USD FX crosses install private USD-spoke routes after route
    # compilation. Their exact provider, capability, dependency, and parent
    # relationship are already represented by the credit plan. Include each
    # installed Twelve Data and Alpha Vantage binding once, even though quote
    # and history produce separate credit lines.
    parent_bases = {f"fx_spoke_dependency:{symbol}" for symbol in selected_symbols}
    for estimate in getattr(plan, "credit_estimates", ()):
        provider = canonical_provider_name(str(estimate.provider))
        if provider not in {"twelve_data", "alpha_vantage"}:
            continue
        if not parent_bases.intersection(estimate.bases):
            continue
        dependency = normalize_symbol(str(estimate.symbol))
        vendor_symbol = dependency.replace(":", "/")
        try:
            validated = validate_provider_binding_identity(
                dependency,
                AssetClass.FX,
                provider,
                vendor_symbol,
            )
        except ValueError as exc:
            raise ProviderBindingVerificationError(
                (ProviderBindingFailure(provider, dependency, "invalid_hidden_binding"),)
            ) from exc
        add(provider, dependency, validated)

    ordered = tuple(
        sorted(
            candidates.values(), key=lambda item: (item.provider, item.symbol, item.vendor_symbol)
        )
    )
    if len(ordered) > MAX_PROVIDER_BINDINGS_PER_VERIFICATION:
        raise ProviderBindingVerificationError(
            (ProviderBindingFailure("catalog", "*", "binding_limit_exceeded"),)
        )
    return ordered


def _verification_requests(
    settings: Any,
    descriptor: ProviderDescriptor,
    candidates: Sequence[_BindingCandidate],
) -> tuple[tuple[str, str, dict[str, Any], dict[str, str]], ...]:
    provider = descriptor.name
    if provider == "binance":
        return (("spot", "https://api.binance.com/api/v3/exchangeInfo", {}, {}),)
    if provider == "okx":
        return (
            (
                "spot",
                "https://www.okx.com/api/v5/public/instruments",
                {"instType": "SPOT"},
                {},
            ),
        )
    if provider == "kraken":
        return (("spot", "https://api.kraken.com/0/public/AssetPairs", {}, {}),)
    if provider == "coingecko":
        return (
            (
                "coins",
                "https://api.coingecko.com/api/v3/coins/list",
                {"include_platform": "false", "status": "active"},
                {"x-cg-demo-api-key": settings.coingecko_api_key},
            ),
        )
    if provider == "alpaca":
        return (
            (
                "listed",
                "https://paper-api.alpaca.markets/v2/assets",
                {"status": "active", "asset_class": "us_equity"},
                {
                    "APCA-API-KEY-ID": settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
                },
            ),
        )
    if provider == "finnhub":
        return (
            (
                "listed",
                "https://api.finnhub.io/api/v1/stock/symbol",
                {"exchange": "US"},
                {"X-Finnhub-Token": settings.finnhub_api_key},
            ),
        )
    if provider == "twelve_data":
        headers = {"Authorization": f"apikey {settings.twelve_data_api_key}"}
        requests: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
        if any("/" not in candidate.vendor_symbol for candidate in candidates):
            requests.append(
                (
                    "listed",
                    "https://api.twelvedata.com/stocks",
                    {"country": "United States", "outputsize": 5_000, "format": "JSON"},
                    headers,
                )
            )
        if any("/" in candidate.vendor_symbol for candidate in candidates):
            requests.append(
                (
                    "fx",
                    "https://api.twelvedata.com/forex_pairs",
                    {"outputsize": 5_000, "format": "JSON"},
                    headers,
                )
            )
        return tuple(requests)
    if provider == "alpha_vantage":
        api_key = settings.alpha_vantage_api_key
        requests = []
        if any("/" not in candidate.vendor_symbol for candidate in candidates):
            requests.append(
                (
                    "listed",
                    "https://www.alphavantage.co/query",
                    {
                        "function": "LISTING_STATUS",
                        "state": "active",
                        "apikey": api_key,
                    },
                    {},
                )
            )
        for vendor_symbol in sorted(
            {candidate.vendor_symbol for candidate in candidates if "/" in candidate.vendor_symbol}
        ):
            base, quote = vendor_symbol.split("/", 1)
            requests.append(
                (
                    f"fx:{vendor_symbol}",
                    "https://www.alphavantage.co/query",
                    {
                        "function": "CURRENCY_EXCHANGE_RATE",
                        "from_currency": base,
                        "to_currency": quote,
                        "apikey": api_key,
                    },
                    {},
                )
            )
        return tuple(requests)
    return ()


def _catalog_rows(payload: Any, provider: str) -> Sequence[Any]:
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        rows = payload
    elif isinstance(payload, Mapping):
        raw_rows = payload.get("data")
        if isinstance(raw_rows, Sequence) and not isinstance(raw_rows, (str, bytes, bytearray)):
            rows = raw_rows
        else:
            raise ProviderUnavailable(provider, "binding catalog is malformed")
    else:
        raise ProviderUnavailable(provider, "binding catalog is malformed")
    if len(rows) > MAX_PROVIDER_VERIFICATION_CATALOG_ROWS:
        raise ProviderUnavailable(provider, "binding catalog row limit exceeded")
    return rows


def _verification_identities(
    provider: str,
    scope: str,
    payload: Any,
) -> dict[str, str | None]:
    identities: dict[str, str | None] = {}
    if provider == "binance":
        if not isinstance(payload, Mapping):
            raise ProviderUnavailable(provider, "binding catalog is malformed")
        rows = _catalog_rows(payload.get("symbols"), provider)
        for row in rows:
            if not isinstance(row, Mapping) or str(row.get("status", "")) != "TRADING":
                continue
            vendor = str(row.get("symbol", "")).upper()
            base = str(row.get("baseAsset", "")).upper()
            quote = str(row.get("quoteAsset", "")).upper()
            if vendor and base and quote:
                identities[vendor] = f"{base}:{quote}"
    elif provider == "okx":
        for row in _catalog_rows(payload, provider):
            if not isinstance(row, Mapping) or str(row.get("state", "")) != "live":
                continue
            vendor = str(row.get("instId", "")).upper()
            base = str(row.get("baseCcy", "")).upper()
            quote = str(row.get("quoteCcy", "")).upper()
            if vendor and base and quote:
                identities[vendor] = f"{base}:{quote}"
    elif provider == "kraken":
        if not isinstance(payload, Mapping) or not isinstance(payload.get("result"), Mapping):
            raise ProviderUnavailable(provider, "binding catalog is malformed")
        rows = payload["result"]
        if len(rows) > MAX_PROVIDER_VERIFICATION_CATALOG_ROWS:
            raise ProviderUnavailable(provider, "binding catalog row limit exceeded")
        aliases = {"XBT": "BTC", "XDG": "DOGE"}
        for key, row in rows.items():
            if not isinstance(row, Mapping):
                continue
            vendor = str(row.get("altname", key)).upper()
            ws_name = str(row.get("wsname", "")).upper()
            if not vendor or "/" not in ws_name:
                continue
            base, quote = ws_name.split("/", 1)
            identities[vendor] = f"{aliases.get(base, base)}:{aliases.get(quote, quote)}"
    elif provider == "coingecko":
        for row in _catalog_rows(payload, provider):
            if not isinstance(row, Mapping):
                continue
            vendor = str(row.get("id", "")).lower()
            reported_base = str(row.get("symbol", "")).upper()
            if vendor and reported_base:
                identities[vendor] = reported_base
    elif provider == "alpaca":
        for row in _catalog_rows(payload, provider):
            if not isinstance(row, Mapping) or str(row.get("status", "")) != "active":
                continue
            vendor = str(row.get("symbol", "")).upper()
            if vendor:
                identities[vendor] = None
    elif provider == "finnhub":
        for row in _catalog_rows(payload, provider):
            if not isinstance(row, Mapping):
                continue
            vendor = str(row.get("symbol") or row.get("displaySymbol") or "").upper()
            if vendor:
                identities[vendor] = None
    elif provider == "twelve_data":
        for row in _catalog_rows(payload, provider):
            if not isinstance(row, Mapping):
                continue
            vendor = str(row.get("symbol", "")).upper()
            if not vendor:
                continue
            if scope == "fx" and "/" in vendor:
                identities[vendor] = vendor.replace("/", ":")
            elif scope == "listed" and "/" not in vendor:
                identities[vendor] = None
    elif provider == "alpha_vantage":
        if scope == "listed":
            if not isinstance(payload, str):
                raise ProviderUnavailable(provider, "binding catalog is malformed")
            reader = csv.DictReader(payload.splitlines())
            if reader.fieldnames is None or "symbol" not in reader.fieldnames:
                raise ProviderUnavailable(provider, "binding catalog is malformed")
            for index, row in enumerate(reader):
                if index >= MAX_PROVIDER_VERIFICATION_CATALOG_ROWS:
                    raise ProviderUnavailable(provider, "binding catalog row limit exceeded")
                if str(row.get("status", "")).strip().casefold() != "active":
                    continue
                vendor = str(row.get("symbol", "")).strip().upper()
                if vendor:
                    identities[vendor] = None
        elif scope.startswith("fx:"):
            if not isinstance(payload, Mapping):
                raise ProviderUnavailable(provider, "binding catalog is malformed")
            row = payload.get("Realtime Currency Exchange Rate")
            if not isinstance(row, Mapping):
                raise ProviderUnavailable(provider, "binding catalog is malformed")
            base = str(row.get("1. From_Currency Code", "")).strip().upper()
            quote = str(row.get("3. To_Currency Code", "")).strip().upper()
            if base and quote:
                identities[f"{base}/{quote}"] = f"{base}:{quote}"
    return identities


async def _default_binding_verification_fetcher(
    provider: str,
    url: str,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    settings: Any,
) -> Any:
    descriptor = get_provider_descriptor(provider)
    hostname = (urlsplit(url).hostname or "").lower()
    if hostname not in descriptor.fixed_hosts:
        raise ValueError("provider verification attempted an untrusted host")
    timeout = aiohttp.ClientTimeout(total=float(getattr(settings, "provider_timeout_seconds", 8)))
    request_options: dict[str, Any] = {
        "params": params,
        "headers": headers,
        "allow_redirects": False,
    }
    proxy_resolver = getattr(settings, "proxy_url_for_provider", None)
    if callable(proxy_resolver):
        proxy = proxy_resolver(descriptor.name)
        if proxy:
            request_options["proxy"] = proxy
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, **request_options) as response:
                if response.status >= 400:
                    raise ProviderUnavailable(
                        descriptor.name,
                        "binding catalog request was rejected",
                        status=response.status,
                    )
                if (
                    descriptor.name == "alpha_vantage"
                    and params.get("function") == "LISTING_STATUS"
                ):
                    return await _bounded_response_text(
                        response,
                        descriptor.name,
                        maximum_bytes=MAX_PROVIDER_VERIFICATION_RESPONSE_BYTES,
                    )
                return await _bounded_response_json(
                    response,
                    descriptor.name,
                    maximum_bytes=MAX_PROVIDER_VERIFICATION_RESPONSE_BYTES,
                )
    except ProviderUnavailable:
        raise
    except TimeoutError, aiohttp.ClientError, ValueError:
        raise ProviderUnavailable(
            descriptor.name,
            "binding catalog is unavailable",
        ) from None


async def verify_provider_bindings(
    settings: Any,
    plan: Any,
    *,
    symbols: Iterable[str],
    credit_reserver: SearchCreditReserver | None = None,
    fetcher: BindingVerificationFetcher | None = None,
) -> dict[str, Any]:
    """Verify every routed binding for changed instruments using fixed catalogs."""

    candidates = _binding_candidates(plan, symbols)
    grouped: defaultdict[str, list[_BindingCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.provider].append(candidate)
    failures: list[ProviderBindingFailure] = []
    provider_results: dict[str, dict[str, Any]] = {}

    async def verify_upstream(
        descriptor: ProviderDescriptor,
        provider_candidates: tuple[_BindingCandidate, ...],
    ) -> tuple[list[ProviderBindingFailure], int]:
        requests = _verification_requests(settings, descriptor, provider_candidates)
        identities: dict[str, str | None] = {}
        credential_scope = _search_scope(settings, descriptor, fetcher)
        network_requests = 0
        try:
            for scope, url, params, headers in requests:

                async def load_catalog(
                    request_url: str = url,
                    request_params: Mapping[str, Any] = params,
                    request_headers: Mapping[str, str] = headers,
                ) -> Any:
                    nonlocal network_requests
                    await _reserve_search_credit(descriptor, credit_reserver)
                    async with _search_network_slot(descriptor.name):
                        network_requests += 1
                        return (
                            await fetcher(
                                descriptor.name,
                                request_url,
                                request_params,
                                request_headers,
                            )
                            if fetcher is not None
                            else await _default_binding_verification_fetcher(
                                descriptor.name,
                                request_url,
                                request_params,
                                request_headers,
                                settings=settings,
                            )
                        )

                payload = await _FULL_LIST_CACHE.get_or_load(
                    (
                        descriptor.name,
                        f"binding-verification:{scope}:{credential_scope}",
                    ),
                    load_catalog,
                )
                identities.update(_verification_identities(descriptor.name, scope, payload))
        except ProviderUnavailable as exc:
            code = "verification_rate_limited" if exc.status == 429 else "verification_unavailable"
            return (
                [
                    ProviderBindingFailure(
                        descriptor.name,
                        candidate.symbol,
                        code,
                        status=exc.status,
                    )
                    for candidate in provider_candidates
                ],
                network_requests,
            )
        except Exception:
            return (
                [
                    ProviderBindingFailure(
                        descriptor.name,
                        candidate.symbol,
                        "verification_unavailable",
                    )
                    for candidate in provider_candidates
                ],
                network_requests,
            )

        local_failures: list[ProviderBindingFailure] = []
        for candidate in provider_candidates:
            observed = identities.get(candidate.vendor_symbol)
            if candidate.vendor_symbol not in identities:
                local_failures.append(
                    ProviderBindingFailure(
                        descriptor.name,
                        candidate.symbol,
                        "unsupported_binding",
                    )
                )
                continue
            if descriptor.name == "coingecko":
                expected = candidate.symbol.split(":", 1)[0]
                if observed != expected:
                    local_failures.append(
                        ProviderBindingFailure(
                            descriptor.name,
                            candidate.symbol,
                            "identity_mismatch",
                        )
                    )
            elif observed is not None and observed != candidate.symbol:
                local_failures.append(
                    ProviderBindingFailure(
                        descriptor.name,
                        candidate.symbol,
                        "identity_mismatch",
                    )
                )
        return local_failures, network_requests

    upstream_jobs: list[
        tuple[
            str,
            tuple[_BindingCandidate, ...],
            asyncio.Task[tuple[list[ProviderBindingFailure], int]],
        ]
    ] = []
    warnings: list[dict[str, str]] = []
    for provider, values in sorted(grouped.items()):
        descriptor = get_provider_descriptor(provider)
        provider_candidates = tuple(values)
        if descriptor.binding_verification in {
            BindingVerificationMode.UPSTREAM_CATALOG,
            BindingVerificationMode.OPAQUE_UPSTREAM_IDENTITY,
        }:
            task = asyncio.create_task(
                verify_upstream(descriptor, provider_candidates),
                name=f"verify-bindings:{provider}",
            )
            upstream_jobs.append((provider, provider_candidates, task))
        else:
            provider_results[provider] = {
                "mode": descriptor.binding_verification.value,
                "bindings": len(provider_candidates),
                "requests": 0,
            }
            if descriptor.binding_verification is BindingVerificationMode.STATIC_DETERMINISTIC:
                warnings.append({"provider": provider, "code": "deterministic_identity_only"})
    job_results = await asyncio.gather(*(task for _, _, task in upstream_jobs))
    for (provider, provider_candidates, _), (local_failures, request_count) in zip(
        upstream_jobs,
        job_results,
        strict=True,
    ):
        failures.extend(local_failures)
        provider_results[provider] = {
            "mode": get_provider_descriptor(provider).binding_verification.value,
            "bindings": len(provider_candidates),
            "requests": request_count,
        }
    if failures:
        raise ProviderBindingVerificationError(failures)
    digest_payload = [
        (candidate.provider, candidate.symbol, candidate.vendor_symbol) for candidate in candidates
    ]
    digest = hashlib.sha256(
        json.dumps(digest_payload, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "verified": True,
        "binding_count": len(candidates),
        "binding_set_sha256": digest,
        "providers": provider_results,
        "warnings": warnings,
    }


def _search_result(
    descriptor: ProviderDescriptor,
    vendor_symbol: str,
    *,
    canonical_hint: str | None,
    name: str,
    asset_class: AssetClass,
    asset_classes: frozenset[AssetClass] | None = None,
) -> dict[str, Any]:
    compatible = asset_classes or frozenset({asset_class})
    if asset_class not in compatible or not compatible.issubset(descriptor.asset_classes):
        raise ValueError("provider search result declares incompatible asset classes")
    return {
        "provider": descriptor.name,
        "vendor_symbol": validate_provider_symbol(descriptor.name, vendor_symbol),
        "canonical_hint": canonical_hint,
        "name": name.strip()[:200],
        # Keep the singular field for backward-compatible display heuristics.
        # Filtering and new clients use the complete compatibility set.
        "asset_class": asset_class.value,
        "asset_classes": sorted(item.value for item in compatible),
        "capabilities": sorted(item.value for item in descriptor.capabilities),
        "verified": True,
    }


def _contains_query(query: str, *values: Any) -> bool:
    needle = query.casefold()
    return any(needle in str(value).casefold() for value in values)


def _parse_search_payload(
    descriptor: ProviderDescriptor,
    query: str,
    payload: Any,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if descriptor.name == "binance" and isinstance(payload, Mapping):
        for row in payload.get("symbols", ()):
            if not isinstance(row, Mapping) or str(row.get("status", "")) != "TRADING":
                continue
            vendor = str(row.get("symbol", ""))
            base = str(row.get("baseAsset", "")).upper()
            quote = str(row.get("quoteAsset", "")).upper()
            if base and quote and _contains_query(query, vendor, base, quote):
                results.append(
                    _search_result(
                        descriptor,
                        vendor,
                        canonical_hint=f"{base}:{quote}",
                        name=f"{base} / {quote}",
                        asset_class=AssetClass.CRYPTO,
                    )
                )
    elif descriptor.name == "okx" and isinstance(payload, Mapping):
        for row in payload.get("data", ()):
            if not isinstance(row, Mapping) or str(row.get("state", "")) != "live":
                continue
            vendor = str(row.get("instId", ""))
            base = str(row.get("baseCcy", "")).upper()
            quote = str(row.get("quoteCcy", "")).upper()
            if base and quote and _contains_query(query, vendor, base, quote):
                results.append(
                    _search_result(
                        descriptor,
                        vendor,
                        canonical_hint=f"{base}:{quote}",
                        name=f"{base} / {quote}",
                        asset_class=AssetClass.CRYPTO,
                    )
                )
    elif descriptor.name == "kraken" and isinstance(payload, Mapping):
        document = payload.get("result", {})
        if isinstance(document, Mapping):
            for key, row in document.items():
                if not isinstance(row, Mapping):
                    continue
                vendor = str(row.get("altname", key)).upper()
                ws_name = str(row.get("wsname", ""))
                if _contains_query(query, vendor, ws_name):
                    canonical = ws_name.replace("/", ":").replace("XBT", "BTC") if ws_name else None
                    results.append(
                        _search_result(
                            descriptor,
                            vendor,
                            canonical_hint=canonical,
                            name=ws_name or vendor,
                            asset_class=AssetClass.CRYPTO,
                        )
                    )
    elif descriptor.name == "coingecko" and isinstance(payload, Mapping):
        for row in payload.get("coins", ()):
            if not isinstance(row, Mapping):
                continue
            vendor = str(row.get("id", ""))
            token = str(row.get("symbol", "")).upper()
            name = str(row.get("name", vendor))
            if vendor and _contains_query(query, vendor, token, name):
                results.append(
                    _search_result(
                        descriptor,
                        vendor,
                        canonical_hint=f"{token}:USDC" if token else None,
                        name=name,
                        asset_class=AssetClass.CRYPTO,
                    )
                )
    elif (
        descriptor.name == "alpaca"
        and isinstance(payload, Sequence)
        and not isinstance(payload, (str, bytes))
    ):
        for row in payload:
            if not isinstance(row, Mapping) or str(row.get("status", "")) != "active":
                continue
            vendor = str(row.get("symbol", "")).upper()
            name = str(row.get("name", vendor))
            if vendor and _contains_query(query, vendor, name):
                results.append(
                    _search_result(
                        descriptor,
                        vendor,
                        canonical_hint=f"{vendor}:USD",
                        name=name,
                        asset_class=AssetClass.EQUITY,
                        asset_classes=_LISTED,
                    )
                )
    elif descriptor.name == "finnhub" and isinstance(payload, Mapping):
        for row in payload.get("result", ()):
            if not isinstance(row, Mapping):
                continue
            vendor = str(row.get("symbol", "")).upper()
            name = str(row.get("description", vendor))
            if vendor and _contains_query(query, vendor, name):
                results.append(
                    _search_result(
                        descriptor,
                        vendor,
                        canonical_hint=f"{vendor}:USD",
                        name=name,
                        asset_class=AssetClass.EQUITY,
                        asset_classes=_LISTED,
                    )
                )
    elif descriptor.name == "twelve_data" and isinstance(payload, Mapping):
        for row in payload.get("data", ()):
            if not isinstance(row, Mapping):
                continue
            vendor = str(row.get("symbol", "")).upper()
            name = str(row.get("instrument_name", vendor))
            instrument_type = str(row.get("instrument_type", "")).lower()
            is_fx = "/" in vendor or "forex" in instrument_type
            canonical = vendor.replace("/", ":") if is_fx else f"{vendor}:USD"
            if vendor and _contains_query(query, vendor, name):
                compatible = _FX if is_fx else _LISTED
                results.append(
                    _search_result(
                        descriptor,
                        vendor,
                        canonical_hint=canonical,
                        name=name,
                        asset_class=AssetClass.FX if is_fx else AssetClass.EQUITY,
                        asset_classes=compatible,
                    )
                )
    elif descriptor.name == "alpha_vantage" and isinstance(payload, Mapping):
        for row in payload.get("bestMatches", ()):
            if not isinstance(row, Mapping):
                continue
            vendor = str(row.get("1. symbol", "")).upper()
            name = str(row.get("2. name", vendor))
            if vendor and _contains_query(query, vendor, name):
                results.append(
                    _search_result(
                        descriptor,
                        vendor,
                        canonical_hint=f"{vendor}:USD",
                        name=name,
                        asset_class=AssetClass.EQUITY,
                        asset_classes=_LISTED,
                    )
                )
    return results


async def search_provider_symbols(
    settings: Any,
    provider: str,
    query: str,
    *,
    asset_class: AssetClass | str | None = None,
    limit: int = 20,
    fetcher: SearchFetcher | None = None,
    credit_reserver: SearchCreditReserver | None = None,
) -> dict[str, Any]:
    """Search a fixed provider endpoint without accepting any network configuration."""

    descriptor = get_provider_descriptor(provider)
    normalized_query = _clean_query(query)
    requested_asset_class = AssetClass(asset_class) if asset_class is not None else None
    bounded_limit = max(1, min(int(limit), MAX_PROVIDER_SEARCH_RESULTS))
    if not descriptor.search_supported:
        return {"provider": descriptor.name, "query": normalized_query, "results": []}
    if requested_asset_class is not None and requested_asset_class not in descriptor.asset_classes:
        return {"provider": descriptor.name, "query": normalized_query, "results": []}
    if not descriptor.credentials_configured(settings):
        raise ValueError(f"provider credentials are not configured: {descriptor.name}")
    scope = _search_scope(settings, descriptor, fetcher)

    cache_key = (
        descriptor.name,
        normalized_query.casefold(),
        "" if requested_asset_class is None else requested_asset_class.value,
        bounded_limit,
        scope,
    )

    async def load() -> dict[str, Any]:
        if descriptor.name == "fred":
            rows = [
                _search_result(
                    descriptor,
                    series,
                    canonical_hint=None,
                    name=f"United States Treasury {series} series",
                    asset_class=AssetClass.BOND,
                )
                for series in sorted(SUPPORTED_TREASURY_PROXY_FRED_SERIES)
                if _contains_query(normalized_query, series)
            ]
        else:
            endpoint, params, headers = _search_request(settings, descriptor, normalized_query)
            selected_fetcher = fetcher
            if selected_fetcher is None:

                async def selected_fetcher(
                    provider_name: str,
                    url: str,
                    query_params: Mapping[str, Any],
                    request_headers: Mapping[str, str],
                ) -> Any:
                    return await _default_search_fetcher(
                        provider_name,
                        url,
                        query_params,
                        request_headers,
                        settings=settings,
                    )

            async def fetch_payload() -> Any:
                await _reserve_search_credit(descriptor, credit_reserver)
                async with _search_network_slot(descriptor.name):
                    return await selected_fetcher(
                        descriptor.name,
                        endpoint,
                        params,
                        headers,
                    )

            if descriptor.name in _FULL_LIST_PROVIDERS:
                payload = await _FULL_LIST_CACHE.get_or_load(
                    (descriptor.name, scope),
                    fetch_payload,
                )
            else:
                payload = await fetch_payload()
            rows = _parse_search_payload(descriptor, normalized_query, payload)
        if requested_asset_class is not None:
            rows = [row for row in rows if requested_asset_class.value in row["asset_classes"]]
        return {
            "provider": descriptor.name,
            "query": normalized_query,
            "results": rows[:bounded_limit],
        }

    return await _SEARCH_CACHE.get_or_load(cache_key, load)


def _search_request(
    settings: Any,
    descriptor: ProviderDescriptor,
    query: str,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    if descriptor.name == "binance":
        return "https://api.binance.com/api/v3/exchangeInfo", {}, {}
    if descriptor.name == "okx":
        return "https://www.okx.com/api/v5/public/instruments", {"instType": "SPOT"}, {}
    if descriptor.name == "kraken":
        return "https://api.kraken.com/0/public/AssetPairs", {}, {}
    if descriptor.name == "coingecko":
        return (
            "https://api.coingecko.com/api/v3/search",
            {"query": query},
            {"x-cg-demo-api-key": settings.coingecko_api_key},
        )
    if descriptor.name == "alpaca":
        return (
            "https://paper-api.alpaca.markets/v2/assets",
            {"status": "active", "asset_class": "us_equity"},
            {
                "APCA-API-KEY-ID": settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
            },
        )
    if descriptor.name == "finnhub":
        return (
            "https://api.finnhub.io/api/v1/search",
            {"q": query},
            {"X-Finnhub-Token": settings.finnhub_api_key},
        )
    if descriptor.name == "twelve_data":
        return (
            "https://api.twelvedata.com/symbol_search",
            {"symbol": query, "apikey": settings.twelve_data_api_key},
            {},
        )
    if descriptor.name == "alpha_vantage":
        return (
            "https://www.alphavantage.co/query",
            {
                "function": "SYMBOL_SEARCH",
                "keywords": query,
                "apikey": settings.alpha_vantage_api_key,
            },
            {},
        )
    raise ValueError(f"provider does not support symbol search: {descriptor.name}")


__all__ = [
    "MAX_CONCURRENT_PROVIDER_SEARCHES",
    "MAX_PROVIDER_BINDINGS_PER_VERIFICATION",
    "MAX_PROVIDER_SEARCH_QUERY_LENGTH",
    "MAX_PROVIDER_SEARCH_RESPONSE_BYTES",
    "MAX_PROVIDER_SEARCH_RESULTS",
    "MAX_PROVIDER_VERIFICATION_CATALOG_ROWS",
    "MAX_PROVIDER_VERIFICATION_RESPONSE_BYTES",
    "PROVIDER_CATALOG",
    "PROVIDER_FULL_LIST_CACHE_TTL_SECONDS",
    "PROVIDER_SEARCH_CACHE_MAX_ENTRIES",
    "PROVIDER_SEARCH_CACHE_TTL_SECONDS",
    "BindingVerificationFetcher",
    "BindingVerificationMode",
    "ProviderBindingFailure",
    "ProviderBindingVerificationError",
    "ProviderDescriptor",
    "ProviderKind",
    "SearchCreditReserver",
    "VendorSymbolKind",
    "canonical_provider_name",
    "estimate_daily_credits",
    "get_provider_descriptor",
    "list_provider_descriptors",
    "provider_catalog_snapshot",
    "search_provider_symbols",
    "validate_provider_binding_identity",
    "validate_provider_symbol",
    "verify_provider_bindings",
]
