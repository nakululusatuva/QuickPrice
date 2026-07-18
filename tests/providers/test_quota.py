from __future__ import annotations

import pytest

from quickprice.providers.quota import (
    QuotaBudget,
    SlidingWindowRateGate,
    rolling_month_safe_daily_budget,
)


@pytest.mark.asyncio
async def test_quota_hard_limit_and_reserve():
    clock = [0.0]
    budget = QuotaBudget(
        5,
        60,
        reserve=1,
        clock=lambda: clock[0],
        align_windows=False,
    )

    assert await budget.acquire(4)
    assert not await budget.acquire()
    assert await budget.acquire(allow_reserve=True)
    assert not await budget.acquire(allow_reserve=True)

    snapshot = await budget.snapshot()
    assert snapshot.used == 5
    assert snapshot.remaining == 0

    clock[0] = 61
    assert await budget.acquire()
    assert (await budget.snapshot()).used == 1


@pytest.mark.asyncio
async def test_quota_concurrent_acquisition_never_overshoots():
    import asyncio

    budget = QuotaBudget(10, 60)
    results = await asyncio.gather(*(budget.acquire() for _ in range(100)))
    assert sum(results) == 10


@pytest.mark.asyncio
async def test_sliding_window_gate_paces_concurrent_bursts() -> None:
    import asyncio

    clock = [0.0]
    sleeps: list[float] = []

    async def advance(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay
        await asyncio.sleep(0)

    gate = SlidingWindowRateGate(
        3,
        1.0,
        clock=lambda: clock[0],
        sleeper=advance,
    )

    await asyncio.gather(*(gate.acquire() for _ in range(7)))

    assert clock[0] == 2.0
    assert sleeps == [1.0, 1.0]


@pytest.mark.asyncio
async def test_quota_checkpoint_survives_restart_and_is_persisted_before_use():
    clock = [120.0]
    durable = {}

    async def persist(state):
        durable.clear()
        durable.update(state)

    first = QuotaBudget(
        5,
        60,
        clock=lambda: clock[0],
        persistence=persist,
    )
    assert await first.acquire(3)
    assert durable["used"] == 3

    restored = QuotaBudget(5, 60, clock=lambda: clock[0])
    await restored.restore(durable)
    assert (await restored.snapshot()).used == 3
    assert await restored.acquire(2)
    assert not await restored.acquire()


@pytest.mark.asyncio
async def test_quota_denies_upstream_reservation_when_persistence_fails():
    async def fail(_state):
        raise OSError("disk full")

    budget = QuotaBudget(5, 60, persistence=fail)
    with pytest.raises(OSError, match="disk full"):
        await budget.acquire()
    assert (await budget.snapshot()).used == 0


@pytest.mark.asyncio
async def test_monthly_fallback_budget_is_safe_across_daily_boundaries():
    budget = rolling_month_safe_daily_budget(9_000)
    snapshot = await budget.snapshot()
    assert snapshot.limit == 290
    assert snapshot.limit * 31 <= 9_000
