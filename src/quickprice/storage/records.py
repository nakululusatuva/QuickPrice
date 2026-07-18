from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

_API_KEY_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_API_KEY_ORIGINS = frozenset({"generated", "imported", "legacy"})


def utc_datetime(value: datetime) -> datetime:
    """Return an aware UTC datetime and reject ambiguous naive timestamps."""

    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("storage timestamps must be timezone-aware")
    return value.astimezone(UTC)


def encode_timestamp(value: datetime) -> str:
    return utc_datetime(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def decode_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    return utc_datetime(datetime.fromisoformat(value))


def decimal_value(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("boolean is not a numeric storage value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("non-finite numeric values cannot be persisted")
    return result


def _required_text(value: Any, field_name: str) -> str:
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def _lookup(value: Any, *names: str, default: Any = dataclasses.MISSING) -> Any:
    for name in names:
        current = value
        found = True
        for part in name.split("."):
            if isinstance(current, Mapping):
                if part not in current:
                    found = False
                    break
                current = current[part]
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                found = False
                break
        if found:
            return current
    if default is not dataclasses.MISSING:
        return default
    raise AttributeError(f"none of {names!r} is present on {type(value).__name__}")


def to_jsonable(value: Any) -> Any:
    """Convert domain/Pydantic values to deterministic standard-library JSON values."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be encoded as JSON")
        return value
    if isinstance(value, Decimal):
        # A string preserves exact provider precision across a SQLite restart.
        return str(decimal_value(value))
    if isinstance(value, datetime):
        return encode_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        # dataclasses.asdict() deep-copies values and fails on immutable
        # MappingProxyType fields used by the domain layer.
        return {
            item.name: to_jsonable(getattr(value, item.name)) for item in dataclasses.fields(value)
        }
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return to_jsonable(model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [to_jsonable(item) for item in value]
    raise TypeError(f"cannot encode {type(value).__name__} as persisted JSON")


def encode_json(value: Any) -> str:
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_json(value: str) -> Any:
    return json.loads(value)


@dataclass(frozen=True, slots=True)
class MinutePriceRecord:
    symbol: str
    timestamp: datetime
    price: Decimal
    provider: str
    is_derived: bool = False
    source: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp))
        object.__setattr__(self, "price", decimal_value(self.price))
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        if self.price <= 0:
            raise ValueError("price must be greater than zero")

    @classmethod
    def from_domain(cls, point: Any) -> MinutePriceRecord:
        return cls(
            symbol=_lookup(point, "symbol"),
            timestamp=_lookup(point, "timestamp", "as_of"),
            price=_lookup(point, "price", "close"),
            provider=_lookup(point, "provider", "source.provider", default="unknown"),
            is_derived=bool(_lookup(point, "is_derived", "source.is_derived", default=False)),
            source=_lookup(point, "source", default={}),
        )


@dataclass(frozen=True, slots=True)
class AggregatePriceRecord:
    symbol: str
    bucket_start: datetime
    interval_seconds: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    sample_count: int
    provider: str
    is_derived: bool = False
    source: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        object.__setattr__(self, "bucket_start", utc_datetime(self.bucket_start))
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, decimal_value(getattr(self, name)))
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.sample_count <= 0:
            raise ValueError("sample_count must be positive")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be greater than zero")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low is above another OHLC value")

    @classmethod
    def from_domain(cls, bar: Any, *, interval_seconds: int = 300) -> AggregatePriceRecord:
        return cls(
            symbol=_lookup(bar, "symbol"),
            bucket_start=_lookup(bar, "bucket_start", "timestamp", "as_of"),
            interval_seconds=int(_lookup(bar, "interval_seconds", default=interval_seconds)),
            open=_lookup(bar, "open"),
            high=_lookup(bar, "high"),
            low=_lookup(bar, "low"),
            close=_lookup(bar, "close", "price"),
            sample_count=int(_lookup(bar, "sample_count", "volume", default=1)),
            provider=_lookup(bar, "provider", "source.provider", default="unknown"),
            is_derived=bool(_lookup(bar, "is_derived", "source.is_derived", default=False)),
            source=_lookup(bar, "source", default={}),
        )


@dataclass(frozen=True, slots=True)
class LatestSnapshotRecord:
    symbol: str
    as_of: datetime
    payload: Mapping[str, Any]
    price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        object.__setattr__(self, "as_of", utc_datetime(self.as_of))
        if self.price is not None:
            object.__setattr__(self, "price", decimal_value(self.price))
            if self.price <= 0:
                raise ValueError("snapshot price must be greater than zero")
        if not isinstance(self.payload, Mapping):
            raise TypeError("snapshot payload must be a mapping")
        # Validate eagerly so writer failures are not caused by unsupported objects.
        encode_json(self.payload)

    @classmethod
    def from_domain(cls, snapshot: Any) -> LatestSnapshotRecord:
        if isinstance(snapshot, Mapping):
            payload = dict(snapshot)
        else:
            dumped = getattr(snapshot, "model_dump", None)
            if callable(dumped):
                payload = dumped(mode="python")
            elif dataclasses.is_dataclass(snapshot) and not isinstance(snapshot, type):
                payload = {
                    item.name: getattr(snapshot, item.name) for item in dataclasses.fields(snapshot)
                }
            else:
                raise TypeError("snapshot must be a mapping, dataclass, or Pydantic model")
        return cls(
            symbol=_lookup(snapshot, "symbol", "quote.symbol"),
            as_of=_lookup(snapshot, "as_of", "timestamp", "quote.as_of"),
            price=_lookup(snapshot, "price", "quote.price", default=None),
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class DividendEventRecord:
    symbol: str
    ex_date: date
    amount: Decimal
    currency: str
    frequency: str
    provider: str
    payment_date: date | None = None
    event_type: str = "cash_dividend"
    is_special: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        object.__setattr__(self, "amount", decimal_value(self.amount))
        object.__setattr__(self, "currency", _required_text(self.currency, "currency"))
        object.__setattr__(self, "frequency", _required_text(self.frequency, "frequency"))
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(self, "event_type", _required_text(self.event_type, "event_type"))
        if not isinstance(self.ex_date, date) or isinstance(self.ex_date, datetime):
            raise TypeError("ex_date must be a date")
        if self.payment_date is not None and (
            not isinstance(self.payment_date, date) or isinstance(self.payment_date, datetime)
        ):
            raise TypeError("payment_date must be a date or None")
        if self.amount < 0:
            raise ValueError("dividend amount cannot be negative")
        encode_json(self.raw)

    @classmethod
    def from_domain(cls, event: Any) -> DividendEventRecord:
        raw = _lookup(event, "raw", default=None)
        if raw is None:
            raw = {}
            declared_date = _lookup(event, "declared_date", default=None)
            if declared_date is not None:
                raw["declared_date"] = declared_date
        return cls(
            symbol=_lookup(event, "symbol"),
            ex_date=_lookup(event, "ex_date"),
            payment_date=_lookup(event, "payment_date", "pay_date", default=None),
            amount=_lookup(event, "amount"),
            currency=_lookup(event, "currency", default="USD"),
            frequency=_lookup(event, "frequency", default="unknown"),
            provider=_lookup(event, "provider", "source.provider", default="unknown"),
            event_type=_lookup(event, "event_type", default="cash_dividend"),
            is_special=bool(_lookup(event, "is_special", default=False)),
            raw=raw,
        )


@dataclass(frozen=True, slots=True)
class YieldMetricRecord:
    symbol: str
    as_of: datetime
    annual_percent: Decimal
    method: str
    provider: str
    is_proxy: bool = False
    source_series: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        object.__setattr__(self, "as_of", utc_datetime(self.as_of))
        object.__setattr__(self, "annual_percent", decimal_value(self.annual_percent))
        object.__setattr__(self, "method", _required_text(self.method, "method"))
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        if self.source_series is not None:
            object.__setattr__(
                self, "source_series", _required_text(self.source_series, "source_series")
            )
        encode_json(self.raw)

    @classmethod
    def from_domain(cls, metric: Any) -> YieldMetricRecord:
        value = _lookup(metric, "annual_percent", "value", "estimated_annual_yield")
        if isinstance(value, Mapping):
            value = _lookup(value, "value", "percent")
        raw = dict(_lookup(metric, "raw", default={}))
        for name in (
            "rate_type",
            "observation_window_days",
            "accrual_mode",
            "underlying_asset",
            "is_estimate",
            "accrual_index",
            "quality",
            "components",
            "fallback_level",
        ):
            item = _lookup(metric, name, default=None)
            if item is not None:
                raw[name] = item
        return cls(
            symbol=_lookup(metric, "symbol"),
            as_of=_lookup(metric, "as_of", "timestamp"),
            annual_percent=value,
            method=_lookup(metric, "method"),
            provider=_lookup(metric, "provider", "source.provider", default="derived"),
            is_proxy=bool(_lookup(metric, "is_proxy", default=False)),
            source_series=_lookup(metric, "source_series", default=None),
            raw=raw,
        )


@dataclass(frozen=True, slots=True)
class ProviderCheckpointRecord:
    provider: str
    feed: str
    updated_at: datetime
    checkpoint: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(self, "feed", _required_text(self.feed, "feed"))
        object.__setattr__(self, "updated_at", utc_datetime(self.updated_at))
        if not isinstance(self.checkpoint, Mapping):
            raise TypeError("checkpoint must be a mapping")
        encode_json(self.checkpoint)


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    key_id: str
    name: str
    key_hash: str
    key_hint: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    origin: str = "generated"

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_id", _required_text(self.key_id, "key_id"))
        name = _required_text(self.name, "name")
        if len(name) > 80:
            raise ValueError("API key name cannot exceed 80 characters")
        object.__setattr__(self, "name", name)
        key_hash = self.key_hash.strip().lower()
        if not _API_KEY_HASH_PATTERN.fullmatch(key_hash):
            raise ValueError("key_hash must use sha256:<64 lowercase hex chars>")
        object.__setattr__(self, "key_hash", key_hash)
        if self.key_hint is not None:
            hint = _required_text(self.key_hint, "key_hint")
            if len(hint) > 32:
                raise ValueError("API key hint cannot exceed 32 characters")
            object.__setattr__(self, "key_hint", hint)
        object.__setattr__(self, "created_at", utc_datetime(self.created_at))
        object.__setattr__(self, "updated_at", utc_datetime(self.updated_at))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", utc_datetime(self.expires_at))
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", utc_datetime(self.revoked_at))
        origin = self.origin.strip().lower()
        if origin not in _API_KEY_ORIGINS:
            raise ValueError(f"unsupported API key origin: {origin!r}")
        object.__setattr__(self, "origin", origin)


@dataclass(frozen=True, slots=True)
class AdminAuditEventRecord:
    event_id: str
    occurred_at: datetime
    request_id: str
    client_ip: str
    action: str
    target_type: str
    target_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("event_id", "request_id", "client_ip", "action", "target_type"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(self, "occurred_at", utc_datetime(self.occurred_at))
        if self.target_id is not None:
            object.__setattr__(self, "target_id", _required_text(self.target_id, "target_id"))
        if not isinstance(self.details, Mapping):
            raise TypeError("audit details must be a mapping")
        encode_json(self.details)


@dataclass(frozen=True, slots=True)
class BootstrapApiKeysCommand:
    records: tuple[ApiKeyRecord, ...]
    completed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        if any(not isinstance(item, ApiKeyRecord) for item in self.records):
            raise TypeError("bootstrap records must contain ApiKeyRecord values")
        object.__setattr__(self, "completed_at", utc_datetime(self.completed_at))


@dataclass(frozen=True, slots=True)
class ImportApiKeysCommand:
    records: tuple[ApiKeyRecord, ...]
    audit: AdminAuditEventRecord

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        if not self.records:
            raise ValueError("API key import cannot be empty")
        if any(not isinstance(item, ApiKeyRecord) for item in self.records):
            raise TypeError("import records must contain ApiKeyRecord values")


@dataclass(frozen=True, slots=True)
class UpdateApiKeyCommand:
    key_id: str
    name: str
    expires_at: datetime | None
    updated_at: datetime
    audit: AdminAuditEventRecord

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_id", _required_text(self.key_id, "key_id"))
        name = _required_text(self.name, "name")
        if len(name) > 80:
            raise ValueError("API key name cannot exceed 80 characters")
        object.__setattr__(self, "name", name)
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", utc_datetime(self.expires_at))
        object.__setattr__(self, "updated_at", utc_datetime(self.updated_at))


@dataclass(frozen=True, slots=True)
class RevokeApiKeyCommand:
    key_id: str
    revoked_at: datetime
    audit: AdminAuditEventRecord

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_id", _required_text(self.key_id, "key_id"))
        object.__setattr__(self, "revoked_at", utc_datetime(self.revoked_at))


@dataclass(frozen=True, slots=True)
class CleanupCommand:
    minute_before: datetime
    aggregate_before: datetime
    daily_before: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "minute_before", utc_datetime(self.minute_before))
        object.__setattr__(self, "aggregate_before", utc_datetime(self.aggregate_before))
        object.__setattr__(self, "daily_before", utc_datetime(self.daily_before))


WriteCommand = (
    MinutePriceRecord
    | AggregatePriceRecord
    | LatestSnapshotRecord
    | DividendEventRecord
    | YieldMetricRecord
    | ProviderCheckpointRecord
    | ApiKeyRecord
    | AdminAuditEventRecord
    | BootstrapApiKeysCommand
    | ImportApiKeysCommand
    | UpdateApiKeyCommand
    | RevokeApiKeyCommand
    | CleanupCommand
)


@dataclass(frozen=True, slots=True)
class WriteResult:
    rows_affected: int = 0


@dataclass(frozen=True, slots=True)
class CleanupResult:
    minute_prices_deleted: int
    aggregate_prices_deleted: int
    dividend_events_deleted: int = 0
    yield_metrics_deleted: int = 0


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    busy: int
    wal_frames: int
    checkpointed_frames: int
    mode: str


@dataclass(frozen=True, slots=True)
class RestoredState:
    minute_prices: tuple[MinutePriceRecord, ...]
    aggregate_prices: tuple[AggregatePriceRecord, ...]
    latest_snapshots: tuple[LatestSnapshotRecord, ...]
    dividend_events: tuple[DividendEventRecord, ...]
    yield_metric_records: tuple[YieldMetricRecord, ...]
    provider_checkpoints: tuple[ProviderCheckpointRecord, ...]

    @property
    def price_points(self) -> tuple[Any, ...]:
        """Provider-neutral domain points ready for HistoryCache restoration."""

        from quickprice.domain import PricePoint

        points = [
            PricePoint(
                symbol=item.symbol,
                timestamp=item.timestamp,
                price=item.price,
                provider=item.provider,
                is_derived=item.is_derived,
                interval="1m",
            )
            for item in self.minute_prices
        ]
        points.extend(
            PricePoint(
                symbol=item.symbol,
                timestamp=item.bucket_start,
                price=item.close,
                provider=item.provider,
                is_derived=item.is_derived,
                interval="1d" if item.interval_seconds == 86_400 else "5m",
            )
            for item in self.aggregate_prices
        )
        return tuple(sorted(points, key=lambda item: (item.timestamp, item.interval, item.symbol)))

    @property
    def dividends(self) -> tuple[Any, ...]:
        from quickprice.domain import DividendEvent

        return tuple(
            DividendEvent(
                symbol=item.symbol,
                ex_date=item.ex_date,
                payment_date=item.payment_date,
                amount=item.amount,
                currency=item.currency,
                frequency=item.frequency,
                provider=item.provider,
                event_type=item.event_type,
                declared_date=(
                    date.fromisoformat(str(item.raw["declared_date"]))
                    if item.raw.get("declared_date")
                    else None
                ),
            )
            for item in self.dividend_events
        )

    @property
    def yield_metrics(self) -> tuple[Any, ...]:
        from quickprice.domain import (
            AccrualIndexPoint,
            SourceComponent,
            YieldMetric,
            YieldQuality,
        )

        def component(value: Mapping[str, Any]) -> SourceComponent:
            return SourceComponent(
                symbol=str(value["symbol"]),
                provider=str(value["provider"]),
                price=Decimal(str(value["price"])),
                as_of=decode_timestamp(str(value["as_of"])),
                feed=None if value.get("feed") is None else str(value["feed"]),
                role=None if value.get("role") is None else str(value["role"]),
            )

        def accrual_index(value: Any) -> AccrualIndexPoint | None:
            if not isinstance(value, Mapping):
                return None
            return AccrualIndexPoint(
                symbol=str(value["symbol"]),
                underlying_asset=str(value["underlying_asset"]),
                value=Decimal(str(value["value"])),
                as_of=decode_timestamp(str(value["as_of"])),
                provider=str(value["provider"]),
                kind=str(value.get("kind", "redemption_rate")),
            )

        def quality(value: Any) -> YieldQuality | None:
            if not isinstance(value, Mapping):
                return None
            return YieldQuality(
                stale=bool(value.get("stale", False)),
                staleness_ms=int(value.get("staleness_ms", 0)),
                confidence=str(value.get("confidence", "high")),
            )

        return tuple(
            YieldMetric(
                symbol=item.symbol,
                value=item.annual_percent,
                as_of=item.as_of,
                method=item.method,
                provider=item.provider,
                is_proxy=item.is_proxy,
                components=tuple(
                    component(value)
                    for value in item.raw.get("components", ())
                    if isinstance(value, Mapping)
                ),
                rate_type=item.raw.get("rate_type"),
                observation_window_days=(
                    None
                    if item.raw.get("observation_window_days") is None
                    else Decimal(str(item.raw["observation_window_days"]))
                ),
                accrual_mode=item.raw.get("accrual_mode"),
                underlying_asset=item.raw.get("underlying_asset"),
                is_estimate=bool(item.raw.get("is_estimate", False)),
                accrual_index=accrual_index(item.raw.get("accrual_index")),
                quality=quality(item.raw.get("quality")),
                fallback_level=int(item.raw.get("fallback_level", 0)),
            )
            for item in self.yield_metric_records
        )

    @property
    def quotes(self) -> tuple[Any, ...]:
        """Best-effort reconstruction of ProviderQuote objects from snapshot JSON."""

        from quickprice.domain import ProviderQuote, SourceComponent

        result: list[Any] = []
        for snapshot in self.latest_snapshots:
            try:
                payload = snapshot.payload
                quote = payload.get("quote", payload)
                components = tuple(
                    SourceComponent(
                        symbol=item["symbol"],
                        provider=item["provider"],
                        price=item["price"],
                        as_of=decode_timestamp(item["as_of"]),
                        feed=item.get("feed"),
                        role=item.get("role"),
                    )
                    for item in quote.get("components", ())
                )
                result.append(
                    ProviderQuote(
                        symbol=quote.get("symbol", snapshot.symbol),
                        price=quote.get("price", snapshot.price),
                        as_of=decode_timestamp(
                            quote.get("as_of", encode_timestamp(snapshot.as_of))
                        ),
                        provider=quote["provider"],
                        feed=quote["feed"],
                        price_basis=quote.get("price_basis", "last_trade"),
                        market_status=quote.get("market_status", "unknown"),
                        is_derived=bool(quote.get("is_derived", False)),
                        components=components,
                        fallback_level=int(quote.get("fallback_level", 0)),
                        license_scope=quote.get("license_scope", "personal_internal"),
                        coverage=quote.get("coverage"),
                        market_status_as_of=(
                            decode_timestamp(quote["market_status_as_of"])
                            if quote.get("market_status_as_of")
                            else None
                        ),
                    )
                )
            except KeyError, TypeError, ValueError:
                continue
        return tuple(result)

    @property
    def snapshots_by_symbol(self) -> dict[str, LatestSnapshotRecord]:
        return {item.symbol: item for item in self.latest_snapshots}

    @property
    def checkpoints_by_key(self) -> dict[tuple[str, str], ProviderCheckpointRecord]:
        return {(item.provider, item.feed): item for item in self.provider_checkpoints}


@dataclass(frozen=True, slots=True)
class StorageMetrics:
    queue_depth: int
    queue_capacity: int
    max_queue_depth: int
    batches_committed: int
    records_committed: int
    commit_failures: int
    last_commit_ms: float | None
    last_error: str | None
    database_bytes: int
    wal_bytes: int
    shm_bytes: int
    last_checkpoint: CheckpointResult | None
