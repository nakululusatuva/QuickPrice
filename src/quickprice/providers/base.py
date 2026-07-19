"""Capability protocols, common errors, and safe HTTP primitives."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

import aiohttp

from quickprice.domain import (
    AccrualIndexPoint,
    DividendEvent,
    PricePoint,
    ProviderQuote,
    YieldMetric,
)
from quickprice.metrics import Metrics

from .quota import QuotaBudget


class Capability(StrEnum):
    QUOTE = "quote"
    HISTORY = "history"
    DIVIDEND = "dividend"
    YIELD = "yield"


class ProviderError(RuntimeError):
    """Base class for an expected upstream failure."""

    retryable = True

    def __init__(self, provider: str, message: str, *, status: int | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.status = status


class ProviderUnavailable(ProviderError):
    """Network, timeout, or server-side failure."""


class NetworkUnavailable(ProviderUnavailable):
    """A connection, DNS, TLS, proxy, or timeout failure safe for fast retry."""


class ProviderRateLimited(ProviderError):
    """The upstream or QuickPrice's local quota gate rejected the request."""


class ProviderBusy(ProviderRateLimited):
    """Local provider admission is saturated; another backend must not consume quota."""


class MalformedResponse(ProviderError):
    """The upstream answered, but its payload did not satisfy the contract."""


class UnsupportedInstrument(ProviderError):
    """The provider cannot serve this instrument/capability pair."""

    retryable = False


class AllProvidersFailed(ProviderError):
    """Every configured provider was unavailable or rejected the request."""

    def __init__(
        self,
        symbol: str,
        capability: Capability,
        attempts: Sequence[tuple[str, str]],
    ) -> None:
        self.symbol = symbol
        self.capability = capability
        self.attempts = tuple(attempts)
        summary = "; ".join(f"{name}={reason}" for name, reason in attempts)
        super().__init__("router", f"{capability.value} unavailable for {symbol}: {summary}")


@runtime_checkable
class QuoteProvider(Protocol):
    name: str

    async def get_quote(self, symbol: str) -> ProviderQuote: ...


@runtime_checkable
class StreamingQuoteProvider(QuoteProvider, Protocol):
    def stream_quotes(self, symbols: Sequence[str]) -> AsyncIterator[ProviderQuote]: ...


@runtime_checkable
class HistoryProvider(Protocol):
    name: str

    async def get_history(
        self,
        symbol: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> Sequence[PricePoint]: ...


@runtime_checkable
class DividendProvider(Protocol):
    name: str

    async def get_latest_dividend(self, symbol: str) -> DividendEvent | None: ...


@runtime_checkable
class YieldProvider(Protocol):
    name: str

    async def get_yield(self, symbol: str) -> YieldMetric: ...


@runtime_checkable
class AccrualIndexProvider(Protocol):
    name: str

    async def get_accrual_index(self, symbol: str) -> AccrualIndexPoint: ...

    async def get_accrual_index_history(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
    ) -> Sequence[AccrualIndexPoint]: ...


class HttpProvider:
    """Shared JSON transport with redacted errors and an optional quota gate."""

    name = "http"

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | Any | None = None,
        quota: QuotaBudget | None = None,
        request_timeout: float = 10.0,
        user_agent: str = "QuickPrice/1.1",
        proxy_url: str | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self.quota = quota
        self.request_timeout = request_timeout
        self.user_agent = user_agent
        self.proxy_url = proxy_url
        self._metrics = metrics
        if metrics is not None:
            metrics.register_provider(self.name)

    def set_metrics(self, metrics: Metrics | None) -> None:
        """Bind the shared registry after a plugin has built its providers."""

        self._metrics = metrics
        if metrics is not None:
            metrics.register_provider(self.name)

    async def __aenter__(self) -> Self:
        await self._ensure_session()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def _ensure_session(self) -> Any:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            options: dict[str, Any] = {
                "timeout": timeout,
                "headers": {"User-Agent": self.user_agent},
            }
            if self.proxy_url:
                options["proxy"] = self.proxy_url
            self._session = aiohttp.ClientSession(**options)
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _proxy_request_options(self) -> dict[str, str]:
        return {"proxy": self.proxy_url} if self.proxy_url else {}

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        quota_cost: int = 1,
        allow_quota_reserve: bool = False,
    ) -> Any:
        if self.quota is not None and not await self.quota.acquire(
            quota_cost, allow_reserve=allow_quota_reserve
        ):
            raise ProviderRateLimited(self.name, "local quota exhausted")

        session = await self._ensure_session()
        started = time.perf_counter()
        try:
            async with session.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_body,
                timeout=self.request_timeout,
                **self._proxy_request_options(),
            ) as response:
                status = response.status
                if status == 429:
                    raise ProviderRateLimited(self.name, "upstream quota exceeded", status=status)
                if status >= 500:
                    raise ProviderUnavailable(self.name, "upstream server error", status=status)
                if status >= 400:
                    # URLs/response bodies can contain credentials.  Never include either.
                    raise ProviderError(self.name, "upstream request rejected", status=status)
                try:
                    payload = await response.json(content_type=None)
                except (
                    aiohttp.ContentTypeError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    ValueError,
                ):
                    raise MalformedResponse(self.name, "invalid JSON response") from None
        except ProviderError as exc:
            self._observe_http(provider_error_outcome(exc), started)
            raise
        except TimeoutError as exc:
            self._observe_http("timeout", started)
            raise NetworkUnavailable(self.name, type(exc).__name__) from None
        except aiohttp.ClientError as exc:
            # aiohttp exceptions may embed the request URL, including vendor
            # keys passed as query parameters. Suppress that cause so even an
            # accidental traceback log cannot disclose credentials.
            self._observe_http("unavailable", started)
            raise NetworkUnavailable(self.name, type(exc).__name__) from None
        self._observe_http("success", started)
        return payload

    def _observe_http(self, outcome: str, started: float) -> None:
        if self._metrics is not None:
            self._metrics.observe_provider_http(
                self.name,
                outcome,
                (time.perf_counter() - started) * 1000,
            )


def provider_error_outcome(error: ProviderError) -> str:
    """Map provider exceptions to a fixed, non-sensitive telemetry label."""

    if isinstance(error, ProviderBusy):
        return "busy"
    if isinstance(error, ProviderRateLimited):
        return "rate_limited"
    if isinstance(error, MalformedResponse):
        return "malformed"
    if isinstance(error, UnsupportedInstrument):
        return "unsupported"
    if isinstance(error, ProviderUnavailable):
        return "unavailable"
    return "rejected"


def require_mapping(payload: Any, provider: str, context: str = "response") -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise MalformedResponse(provider, f"{context} must be an object")
    return payload


def require_sequence(payload: Any, provider: str, context: str = "response") -> Sequence[Any]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
        raise MalformedResponse(provider, f"{context} must be an array")
    return payload
