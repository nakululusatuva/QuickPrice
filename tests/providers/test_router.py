from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quickprice.domain import DividendEvent, PricePoint, ProviderQuote, YieldMetric
from quickprice.providers.base import AllProvidersFailed, Capability, ProviderUnavailable
from quickprice.providers.router import ProviderRouter


def fixed_quote(provider: str, price: str = "10") -> ProviderQuote:
    return ProviderQuote(
        symbol="BTC:USDC",
        price=Decimal(price),
        as_of=datetime(2026, 7, 20, tzinfo=UTC),
        provider=provider,
        feed="fixture",
    )


class ScriptedProvider:
    def __init__(self, name: str, outcomes, *, delay: float = 0.0):
        self.name = name
        self.outcomes = list(outcomes)
        self.delay = delay
        self.calls = 0

    async def get_quote(self, symbol: str):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class CloseTrackedProvider(ScriptedProvider):
    def __init__(self, name: str):
        super().__init__(name, [fixed_quote(name)])
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class OutOfRangeProvider:
    name = "out_of_range"

    async def get_quote(self, symbol: str):
        return ProviderQuote(
            symbol=symbol,
            price=Decimal("1e10000"),
            as_of=datetime(2026, 7, 20, tzinfo=UTC),
            provider=self.name,
            feed="malformed",
        )


class DividendProviderFixture:
    def __init__(self, name, value):
        self.name = name
        self.value = value
        self.calls = 0

    async def get_latest_dividend(self, symbol):
        self.calls += 1
        return self.value


class HistoryProviderFixture:
    def __init__(self, name, points, *, history_prefix_limited=False):
        self.name = name
        self.points = points
        self.calls = 0
        self.history_prefix_limited = history_prefix_limited

    async def get_history(self, symbol, **_kwargs):
        self.calls += 1
        return self.points


class YieldProviderFixture:
    def __init__(self, name, value):
        self.name = name
        self.value = value
        self.calls = 0

    async def get_yield(self, symbol):
        self.calls += 1
        return self.value


@pytest.mark.asyncio
async def test_fallback_is_labeled_and_counted():
    first = ScriptedProvider("primary", [ProviderUnavailable("primary", "down")])
    second = ScriptedProvider("backup", [fixed_quote("backup")])
    router = ProviderRouter({("BTC:USDC", Capability.QUOTE): [first, second]})

    result = await router.get_quote("btc:usdc")

    assert result.provider == "backup"
    assert result.fallback_level == 1
    assert router.fallback_counts() == {"BTC:USDC|quote|backup": 1}


@pytest.mark.asyncio
async def test_wrong_symbol_quote_results_are_failures_at_primary_and_fallback_levels():
    primary = ScriptedProvider(
        "primary",
        [
            ProviderQuote(
                symbol="ETH:USDC",
                price=Decimal("20"),
                as_of=datetime(2026, 7, 20, tzinfo=UTC),
                provider="primary",
                feed="fixture",
            )
        ],
    )
    fallback = ScriptedProvider(
        "fallback",
        [
            ProviderQuote(
                symbol="btc:usdc",
                price=Decimal("15"),
                as_of=datetime(2026, 7, 20, tzinfo=UTC),
                provider="fallback",
                feed="fixture",
            )
        ],
    )
    final = ScriptedProvider("final", [fixed_quote("final")])
    router = ProviderRouter({("BTC:USDC", Capability.QUOTE): [primary, fallback, final]})

    result = await router.get_quote("BTC:USDC")

    assert result.provider == "final"
    assert result.fallback_level == 2
    assert primary.calls == fallback.calls == final.calls == 1


@pytest.mark.asyncio
async def test_wrong_symbol_history_point_is_rejected_before_merging_fallbacks():
    observed_at = datetime(2026, 7, 20, tzinfo=UTC)
    primary = HistoryProviderFixture(
        "primary",
        (PricePoint("ETH:USDC", observed_at, Decimal("20"), "primary"),),
    )
    fallback = HistoryProviderFixture(
        "fallback",
        (PricePoint("BTC:USDC", observed_at, Decimal("10"), "fallback"),),
    )
    router = ProviderRouter({("BTC:USDC", Capability.HISTORY): [primary, fallback]})

    result = await router.get_history(
        "BTC:USDC",
        interval="1m",
        start=observed_at - timedelta(minutes=1),
        end=observed_at,
    )

    assert result == fallback.points
    assert router.fallback_counts() == {"BTC:USDC|history|fallback": 1}
    assert primary.calls == fallback.calls == 1


@pytest.mark.asyncio
async def test_wrong_symbol_dividend_is_rejected_before_metadata_fallback():
    wrong = DividendEvent(
        symbol="SGOV:USD",
        ex_date=date(2026, 7, 1),
        payment_date=date(2026, 7, 7),
        amount=Decimal("0.30"),
        currency="USD",
        frequency="quarterly",
        provider="primary",
    )
    expected = DividendEvent(
        symbol="QQQM:USD",
        ex_date=date(2026, 7, 1),
        payment_date=date(2026, 7, 7),
        amount=Decimal("0.30"),
        currency="USD",
        frequency="quarterly",
        provider="fallback",
    )
    primary = DividendProviderFixture("primary", wrong)
    fallback = DividendProviderFixture("fallback", expected)
    router = ProviderRouter({("QQQM:USD", Capability.DIVIDEND): [primary, fallback]})

    result = await router.get_latest_dividend("QQQM:USD")

    assert result is not None
    assert result.symbol == "QQQM:USD"
    assert primary.calls == fallback.calls == 1


@pytest.mark.asyncio
async def test_wrong_symbol_yield_is_rejected_before_metadata_fallback():
    observed_at = datetime(2026, 7, 20, tzinfo=UTC)
    wrong = YieldMetric(
        symbol="STETH:USDC",
        value=Decimal("2.5"),
        as_of=observed_at,
        method="fixture",
        provider="primary",
    )
    expected = YieldMetric(
        symbol="WBETH:USDC",
        value=Decimal("2.3"),
        as_of=observed_at,
        method="fixture",
        provider="fallback",
    )
    primary = YieldProviderFixture("primary", wrong)
    fallback = YieldProviderFixture("fallback", expected)
    router = ProviderRouter({("WBETH:USDC", Capability.YIELD): [primary, fallback]})

    result = await router.get_yield("WBETH:USDC")

    assert result.symbol == "WBETH:USDC"
    assert result.fallback_level == 1
    assert primary.calls == fallback.calls == 1


@pytest.mark.asyncio
async def test_fallback_logging_is_transition_based_and_sanitized(caplog):
    secret = "api_key=must-not-appear"
    primary = ScriptedProvider(
        "primary",
        [
            ProviderUnavailable("primary", secret),
            ProviderUnavailable("primary", secret),
            fixed_quote("primary", "11"),
        ],
    )
    backup = ScriptedProvider("backup", [fixed_quote("backup")])
    router = ProviderRouter({("BTC:USDC", Capability.QUOTE): [primary, backup]})

    with caplog.at_level("INFO", logger="quickprice.providers.router"):
        assert (await router.get_quote("BTC:USDC")).provider == "backup"
        assert (await router.get_quote("BTC:USDC")).provider == "backup"
        assert (await router.get_quote("BTC:USDC")).provider == "primary"

    messages = [record.getMessage() for record in caplog.records]
    assert (
        messages.count(
            "Provider fallback selected provider=backup symbol=BTC:USDC "
            "capability=quote fallback_level=1"
        )
        == 1
    )
    assert (
        messages.count(
            "Provider primary recovered provider=primary symbol=BTC:USDC capability=quote"
        )
        == 1
    )
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_non_json_representable_primary_price_triggers_fallback():
    primary = OutOfRangeProvider()
    backup = ScriptedProvider("backup", [fixed_quote("backup")])
    router = ProviderRouter({("BTC:USDC", Capability.QUOTE): [primary, backup]})

    result = await router.get_quote("BTC:USDC")

    assert result.provider == "backup"
    assert result.fallback_level == 1


@pytest.mark.asyncio
async def test_outer_primary_route_preserves_component_fallback_level():
    derived = fixed_quote("synthetic")
    derived = ProviderQuote(
        symbol=derived.symbol,
        price=derived.price,
        as_of=derived.as_of,
        provider=derived.provider,
        feed=derived.feed,
        is_derived=True,
        fallback_level=2,
    )
    provider = ScriptedProvider("synthetic", [derived])
    router = ProviderRouter({("BTC:USDC", Capability.QUOTE): [provider]})

    result = await router.get_quote("BTC:USDC")

    assert result.fallback_level == 2


@pytest.mark.asyncio
async def test_no_dividend_data_continues_to_fallback_provider():
    from datetime import date

    from quickprice.domain import DividendEvent

    primary = DividendProviderFixture("primary", None)
    expected = DividendEvent(
        symbol="QQQM:USD",
        ex_date=date(2026, 6, 23),
        payment_date=date(2026, 6, 27),
        amount=Decimal("0.32"),
        currency="USD",
        frequency="quarterly",
        provider="backup",
    )
    backup = DividendProviderFixture("backup", expected)
    router = ProviderRouter({("QQQM:USD", Capability.DIVIDEND): [primary, backup]})

    result = await router.get_latest_dividend("QQQM:USD")

    assert result is expected
    assert primary.calls == backup.calls == 1


@pytest.mark.asyncio
async def test_singleflight_merges_concurrent_identical_calls():
    provider = ScriptedProvider("one", [fixed_quote("one")], delay=0.02)
    router = ProviderRouter({("BTC:USDC", Capability.QUOTE): [provider]})

    results = await asyncio.gather(*(router.get_quote("BTC:USDC") for _ in range(20)))

    assert provider.calls == 1
    assert len(results) == 20


@pytest.mark.asyncio
async def test_cancelled_only_waiter_does_not_leave_orphaned_flight():
    provider = ScriptedProvider("one", [fixed_quote("one")], delay=0.02)
    router = ProviderRouter({("BTC:USDC", Capability.QUOTE): [provider]})
    cleanup_finished = asyncio.Event()
    original_cleanup = router._cleanup_flight

    async def observed_cleanup(key, task):
        await original_cleanup(key, task)
        cleanup_finished.set()

    router._cleanup_flight = observed_cleanup
    waiter = asyncio.create_task(router.get_quote("BTC:USDC"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    async with asyncio.timeout(1):
        await cleanup_finished.wait()

    assert router._flights == {}


@pytest.mark.asyncio
async def test_timeout_falls_back_without_leaking_cancelled_task():
    slow = ScriptedProvider("slow", [fixed_quote("slow")], delay=0.1)
    backup = ScriptedProvider("backup", [fixed_quote("backup")])
    router = ProviderRouter({("BTC:USDC", Capability.QUOTE): [slow, backup]}, timeout_seconds=0.01)

    result = await router.get_quote("BTC:USDC")

    assert result.provider == "backup"
    assert result.fallback_level == 1


@pytest.mark.asyncio
async def test_three_failures_open_breaker_then_half_open_success_resets_it(caplog):
    clock = [100.0]
    secret = "access_token=must-not-appear"
    primary = ScriptedProvider(
        "primary",
        [
            ProviderUnavailable("primary", secret),
            ProviderUnavailable("primary", secret),
            ProviderUnavailable("primary", secret),
            fixed_quote("primary", "11"),
        ],
    )
    backup = ScriptedProvider("backup", [fixed_quote("backup")])
    router = ProviderRouter(
        {("BTC:USDC", Capability.QUOTE): [primary, backup]},
        clock=lambda: clock[0],
    )

    with caplog.at_level("INFO", logger="quickprice.providers.router"):
        for _ in range(3):
            assert (await router.get_quote("BTC:USDC")).provider == "backup"
        assert primary.calls == 3
        assert router.circuit_snapshots()[0].state == "open"

        await router.get_quote("BTC:USDC")
        assert primary.calls == 3

        clock[0] += 60
        result = await router.get_quote("BTC:USDC")
    assert result.provider == "primary"
    assert result.fallback_level == 0
    assert router.circuit_snapshots()[0].state == "closed"
    messages = [record.getMessage() for record in caplog.records]
    assert (
        messages.count(
            "Provider circuit opened provider=primary symbol=BTC:USDC capability=quote "
            "error_type=ProviderUnavailable retry_in_seconds=60"
        )
        == 1
    )
    assert (
        messages.count(
            "Provider circuit recovered provider=primary symbol=BTC:USDC capability=quote"
        )
        == 1
    )
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_failed_half_open_probe_doubles_backoff():
    clock = [10.0]
    failure = ProviderUnavailable("primary", "down")
    primary = ScriptedProvider("primary", [failure])
    router = ProviderRouter(
        {("BTC:USDC", Capability.QUOTE): [primary]},
        failure_threshold=1,
        half_open_after_seconds=60,
        clock=lambda: clock[0],
    )

    with pytest.raises(AllProvidersFailed):
        await router.get_quote("BTC:USDC")
    assert router.circuit_snapshots()[0].retry_in_seconds == 60

    clock[0] += 60
    with pytest.raises(AllProvidersFailed):
        await router.get_quote("BTC:USDC")
    assert router.circuit_snapshots()[0].retry_in_seconds == 120


@pytest.mark.asyncio
async def test_unconfigured_capability_has_structured_attempts():
    router = ProviderRouter()
    with pytest.raises(AllProvidersFailed) as caught:
        await router.get_quote("BTC:USDC")
    assert caught.value.symbol == "BTC:USDC"
    assert caught.value.attempts == (("router", "not configured"),)


def test_duplicate_route_registration_is_rejected() -> None:
    provider = ScriptedProvider("primary", [fixed_quote("primary")])
    router = ProviderRouter()
    router.register("BTC:USDC", Capability.QUOTE, [provider])

    with pytest.raises(ValueError, match="duplicate provider route"):
        router.register("BTC:USDC", Capability.QUOTE, [provider])


@pytest.mark.asyncio
async def test_close_can_transfer_provider_ownership_without_leaking_flights() -> None:
    retained = CloseTrackedProvider("retained")
    retired = CloseTrackedProvider("retired")
    router = ProviderRouter(
        {
            ("BTC:USDC", Capability.QUOTE): [retained],
            ("ETH:USDC", Capability.QUOTE): [retired],
        }
    )

    await router.close(exclude_providers=(retained,))

    assert retained.close_calls == 0
    assert retired.close_calls == 1
    assert router._flights == {}


@pytest.mark.asyncio
async def test_incomplete_recent_history_is_completed_by_next_fallback():
    end = datetime(2026, 7, 20, tzinfo=UTC)
    start = end - timedelta(days=30)
    recent = HistoryProviderFixture(
        "kraken",
        (
            PricePoint("BTC:USDC", end - timedelta(hours=12), Decimal("10"), "kraken"),
            PricePoint("BTC:USDC", end, Decimal("11"), "kraken"),
        ),
        history_prefix_limited=True,
    )
    older = HistoryProviderFixture(
        "coingecko",
        (
            PricePoint("BTC:USDC", start, Decimal("8"), "coingecko"),
            PricePoint("BTC:USDC", end - timedelta(hours=12), Decimal("9"), "coingecko"),
        ),
    )
    router = ProviderRouter({("BTC:USDC", Capability.HISTORY): [recent, older]})

    result = await router.get_history("BTC:USDC", interval="5m", start=start, end=end, limit=10_000)

    assert recent.calls == older.calls == 1
    assert result[0].provider == "coingecko"
    assert result[-1].provider == "kraken"
    assert router.fallback_counts() == {"BTC:USDC|history|coingecko": 1}


@pytest.mark.asyncio
async def test_normal_market_closure_does_not_trigger_history_fallback():
    end = datetime(2026, 7, 20, tzinfo=UTC)
    start = end - timedelta(days=2)
    primary = HistoryProviderFixture(
        "alpaca",
        (PricePoint("QQQM:USD", end, Decimal("10"), "alpaca"),),
    )
    backup = HistoryProviderFixture("twelve_data", ())
    router = ProviderRouter({("QQQM:USD", Capability.HISTORY): [primary, backup]})

    result = await router.get_history("QQQM:USD", interval="1m", start=start, end=end, limit=10_000)

    assert result == primary.points
    assert primary.calls == 1
    assert backup.calls == 0
