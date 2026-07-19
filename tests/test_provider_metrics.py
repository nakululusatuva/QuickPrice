from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quickprice.collectors import MarketDataCoordinator
from quickprice.config import Settings
from quickprice.domain import ProviderQuote
from quickprice.metrics import Metrics
from quickprice.providers.base import Capability, HttpProvider, ProviderUnavailable
from quickprice.providers.quota import QuotaBudget
from quickprice.providers.router import ProviderRouter


def _quote(provider: str, *, as_of: datetime | None = None) -> ProviderQuote:
    return ProviderQuote(
        symbol="BTC:USDC",
        price=Decimal("10"),
        as_of=as_of or datetime(2026, 7, 21, tzinfo=UTC),
        provider=provider,
        feed="fixture",
    )


def test_provider_statistics_are_bounded_and_keep_exact_lifetime_totals() -> None:
    metrics = Metrics(provider_window=3)
    metrics.register_provider("idle")
    metrics.observe_provider_operation("sample", "quote", "success", 10)
    metrics.observe_provider_operation("sample", "quote", "rate_limited", 20)
    metrics.observe_provider_operation("sample", "history", "partial", 30)
    metrics.observe_provider_operation("sample", "history", "unavailable", 40)

    providers = metrics.provider_statistics()

    assert providers["idle"]["operations"]["lifetime"]["success_rate"] is None
    assert providers["idle"]["upstream_http"]["lifetime"]["success_rate"] is None
    lifetime = providers["sample"]["operations"]["lifetime"]
    assert lifetime["attempts"] == 4
    assert lifetime["successful"] == 2
    assert lifetime["success_rate"] == 50.0
    assert lifetime["outcomes"] == {
        "partial": 1,
        "rate_limited": 1,
        "success": 1,
        "unavailable": 1,
    }
    assert lifetime["latency_ms"]["avg"] == 25.0
    assert lifetime["latency_ms"]["max"] == 40.0
    assert lifetime["last_outcome"] == "unavailable"
    recent = providers["sample"]["operations"]["recent"]
    assert recent["retained"] == 3
    assert recent["capacity"] == 3
    assert recent["outcomes"] == {"partial": 1, "rate_limited": 1, "unavailable": 1}
    assert recent["last_outcome"] == "unavailable"
    assert recent["latency_ms"] == {
        "avg": 30.0,
        "p50": 30.0,
        "p95": 40.0,
        "p99": 40.0,
        "max": 40.0,
        "percentile_scope": "bounded_recent",
        "percentile_sample_size": 3,
    }


def test_provider_metric_labels_cannot_retain_secrets_or_exception_text() -> None:
    metrics = Metrics()
    metrics.observe_provider_operation(
        "api_key=must-not-appear",
        "query?token=must-not-appear",
        "upstream said must-not-appear",
        1,
    )

    snapshot = str(metrics.provider_statistics())

    assert "must-not-appear" not in snapshot
    assert "unknown" in snapshot
    assert "unexpected" in snapshot


@pytest.mark.asyncio
async def test_router_records_each_real_attempt_and_not_singleflight_waiters() -> None:
    class Provider:
        def __init__(self, name: str, value: object) -> None:
            self.name = name
            self.value = value
            self.calls = 0

        async def get_quote(self, _symbol: str) -> ProviderQuote:
            self.calls += 1
            await asyncio.sleep(0.01)
            if isinstance(self.value, BaseException):
                raise self.value
            assert isinstance(self.value, ProviderQuote)
            return self.value

    metrics = Metrics()
    primary = Provider("primary", ProviderUnavailable("primary", "private upstream detail"))
    backup = Provider("backup", _quote("backup"))
    router = ProviderRouter(
        {("BTC:USDC", Capability.QUOTE): [primary, backup]},
        metrics=metrics,
    )

    results = await asyncio.gather(*(router.get_quote("BTC:USDC") for _ in range(10)))

    assert len(results) == 10
    assert primary.calls == backup.calls == 1
    statistics = metrics.provider_statistics()
    assert statistics["primary"]["operations"]["lifetime"]["outcomes"] == {"unavailable": 1}
    assert statistics["backup"]["operations"]["lifetime"]["outcomes"] == {"success": 1}
    assert "private upstream detail" not in str(statistics)


@pytest.mark.asyncio
async def test_http_provider_records_only_sanitized_upstream_http_outcomes() -> None:
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self, *, content_type=None):
            assert content_type is None
            return {"price": "10"}

    class Session:
        def request(self, *_args, **_kwargs):
            return Response()

    metrics = Metrics()
    provider = HttpProvider(session=Session(), metrics=metrics)

    assert await provider._request_json("GET", "https://example.test/private?token=secret") == {
        "price": "10"
    }
    http_stats = metrics.provider_statistics()["http"]["upstream_http"]["lifetime"]
    assert http_stats["outcomes"] == {"success": 1}
    assert "example.test" not in str(metrics.provider_statistics())
    assert "secret" not in str(metrics.provider_statistics())


@pytest.mark.asyncio
async def test_quota_metrics_distinguish_local_accounting_from_untracked() -> None:
    metrics = Metrics()
    coordinator = MarketDataCoordinator(
        SimpleNamespace(metrics=metrics),
        Settings(background_enabled=False),
    )
    quota = QuotaBudget(10, 60, reserve=2, align_windows=False)
    await quota.acquire(3)
    coordinator.graph.providers["tracked_fixture"] = SimpleNamespace(quota=quota)
    coordinator.graph.providers["untracked_fixture"] = SimpleNamespace(quota=None)

    await coordinator._update_quota_metrics()

    quota_metrics = coordinator.metrics()["quota"]
    assert quota_metrics["tracked_fixture"] == {
        "limit": 10,
        "used": 3,
        "remaining": 7,
        "resets_at": quota_metrics["tracked_fixture"]["resets_at"],
        "tracked": True,
        "accounting": "local_request_reservations",
        "provider_reported": False,
        "unit": "credits",
        "reserve": 2,
        "usable_limit": 8,
        "usable_remaining": 5,
        "period_seconds": 60.0,
    }
    assert quota_metrics["untracked_fixture"]["tracked"] is False
    assert quota_metrics["untracked_fixture"]["accounting"] == "untracked"
    assert quota_metrics["untracked_fixture"]["used"] is None
    assert coordinator.metrics()["quota_updated_at"].endswith("Z")


@pytest.mark.asyncio
async def test_stream_health_records_connection_result_and_reconnects() -> None:
    class StreamProvider:
        async def stream_quotes(self, _symbols):
            yield _quote("stream_fixture", as_of=datetime.now(UTC))
            raise RuntimeError("fixture disconnect")

    metrics = Metrics()
    coordinator = MarketDataCoordinator(
        SimpleNamespace(metrics=metrics),
        Settings(background_enabled=False),
    )
    provider = StreamProvider()
    task = asyncio.create_task(
        coordinator._provider_stream_loop(
            "stream_fixture",
            provider,
            ("BTC:USDC",),
        )
    )
    try:
        for _ in range(50):
            if coordinator.metrics()["websocket_reconnects"].get("stream_fixture") == 1:
                break
            await asyncio.sleep(0.01)
        stream = coordinator.metrics()["streams"]["stream_fixture"]
        assert stream["state"] == "disconnected"
        assert stream["messages"] == 1
        assert stream["successful_connections"] == 1
        assert coordinator.metrics()["websocket_reconnects"]["stream_fixture"] == 1
        operation = metrics.provider_statistics()["stream_fixture"]["operations"]["lifetime"]
        assert operation["capabilities"] == {"stream": 1}
        assert operation["last_outcome"] == "success"
        assert (id(provider), "BTC:USDC") not in coordinator._stream_observed_at
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await coordinator.graph.close()


@pytest.mark.asyncio
async def test_stream_observation_preserves_source_age_and_ignores_unsolicited_symbol() -> None:
    source_age = 3.0
    yielded = asyncio.Event()
    release = asyncio.Event()

    class StreamProvider:
        async def stream_quotes(self, _symbols):
            yield ProviderQuote(
                symbol="BTC:USDC",
                price=Decimal("10"),
                as_of=datetime.now(UTC) - timedelta(seconds=source_age),
                provider="stream_fixture",
                feed="fixture",
            )
            yield ProviderQuote(
                symbol="ETH:USDC",
                price=Decimal("20"),
                as_of=datetime.now(UTC),
                provider="stream_fixture",
                feed="fixture",
            )
            yielded.set()
            await release.wait()

    metrics = Metrics()
    coordinator = MarketDataCoordinator(
        SimpleNamespace(metrics=metrics),
        Settings(background_enabled=False),
    )
    provider = StreamProvider()
    observed_before = asyncio.get_running_loop().time()
    task = asyncio.create_task(
        coordinator._provider_stream_loop(
            "stream_fixture",
            provider,
            ("BTC:USDC",),
        )
    )
    try:
        await asyncio.wait_for(yielded.wait(), timeout=1)
        observed_after = asyncio.get_running_loop().time()
        observation = coordinator._stream_observed_at[(id(provider), "BTC:USDC")]

        assert observed_before - source_age - 0.5 <= observation
        assert observation <= observed_after - source_age + 0.5
        assert (id(provider), "ETH:USDC") not in coordinator._stream_observed_at
        assert all(quote.symbol != "ETH:USDC" for quote in coordinator._pending.values())
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await coordinator.graph.close()
