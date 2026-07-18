"""Construction helpers isolating adapters from harmless domain-model growth."""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from quickprice.domain import DividendEvent, PricePoint, ProviderQuote, SourceComponent, YieldMetric


def utc_datetime(value: Any, *, milliseconds: bool = False) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float, Decimal)):
        seconds = float(value) / (1000 if milliseconds else 1)
        dt = datetime.fromtimestamp(seconds, tz=UTC)
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        if text.isdigit():
            return utc_datetime(int(text), milliseconds=milliseconds)
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"unsupported timestamp type: {type(value).__name__}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def decimal_value(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid decimal value") from exc
    if not result.is_finite():
        raise ValueError("non-finite decimal value")
    return result


def date_value(value: Any) -> date | None:
    if value in (None, "", "0000-00-00"):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _construct[T](cls: type[T], **values: Any) -> T:
    """Pass fields supported by the shared model while preserving required ones.

    Core models are intentionally owned outside this package.  Filtering only
    unknown optional metadata lets those models add/remove presentation fields
    without forcing every provider adapter to change at once.
    """

    try:
        signature = inspect.signature(cls)
    except TypeError, ValueError:
        return cls(**values)
    accepted = {
        key: value
        for key, value in values.items()
        if key in signature.parameters
        or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    }
    return cls(**accepted)


def component(**values: Any) -> SourceComponent:
    return _construct(SourceComponent, **values)


def quote(**values: Any) -> ProviderQuote:
    return _construct(ProviderQuote, **values)


def point(**values: Any) -> PricePoint:
    return _construct(PricePoint, **values)


def dividend(**values: Any) -> DividendEvent:
    return _construct(DividendEvent, **values)


def yield_metric(**values: Any) -> YieldMetric:
    return _construct(YieldMetric, **values)


def replace_metadata[T](value: T, **changes: Any) -> T:
    if dataclasses.is_dataclass(value):
        allowed = {field.name for field in dataclasses.fields(value)}
        updates = {key: item for key, item in changes.items() if key in allowed}
        return dataclasses.replace(value, **updates) if updates else value
    model_copy = getattr(value, "model_copy", None)
    if callable(model_copy):
        fields = getattr(value.__class__, "model_fields", {})
        updates = {key: item for key, item in changes.items() if key in fields}
        return model_copy(update=updates) if updates else value
    for name, item in changes.items():
        if hasattr(value, name):
            try:
                setattr(value, name, item)
            except AttributeError, TypeError:
                pass
    return value
