#!/usr/bin/env python3
"""Small authenticated QuickPrice load/soak driver using aiohttp."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import math
import os
import random
import sys
import time
from collections import Counter
from urllib.parse import urlencode

import aiohttp

DEFAULT_SYMBOLS = "BTC:USDC,ETH:USDC,WBETH:USDC,QQQM:USD,BOXX:USD,SGOV:USD,USD:CNH,HKD:CNH"


class LatencyRecorder:
    """Streaming totals plus a fixed-size reservoir for long soak tests."""

    def __init__(self, sample_limit: int) -> None:
        self.sample_limit = sample_limit
        self.samples: list[float] = []
        self.count = 0
        self.total = 0.0
        self.minimum = math.inf
        self.maximum = -math.inf
        self._random = random.Random(0x51505249)

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        if len(self.samples) < self.sample_limit:
            self.samples.append(value)
            return
        index = self._random.randrange(self.count)
        if index < self.sample_limit:
            self.samples[index] = value

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else math.nan


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    index = max(0, math.ceil((percentile_value / 100) * len(ordered)) - 1)
    return ordered[index]


async def run(args: argparse.Namespace, api_key: str) -> int:
    semaphore = asyncio.Semaphore(args.concurrency)
    latencies_ms = LatencyRecorder(args.latency_sample_size)
    statuses: Counter[str] = Counter()
    tasks: set[asyncio.Task[None]] = set()
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=args.concurrency, ttl_dns_cache=300)
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    url = args.base_url.rstrip("/") + args.path

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers=headers,
        raise_for_status=False,
    ) as session:
        if not args.skip_connection_prewarm:
            prewarm_statuses: Counter[str] = Counter()

            async def prewarm() -> None:
                try:
                    async with session.get(url) as response:
                        await response.read()
                        prewarm_statuses[str(response.status)] += 1
                except Exception as exc:
                    prewarm_statuses[f"exception:{type(exc).__name__}"] += 1

            # Launch together so the connector establishes up to the requested
            # number of persistent connections before the measured RPS phase.
            await asyncio.gather(*(prewarm() for _ in range(args.concurrency)))
            if prewarm_statuses != Counter({"200": args.concurrency}):
                print(
                    f"FAIL: connection prewarm statuses={dict(prewarm_statuses)}",
                    file=sys.stderr,
                )
                return 1

        async def issue_request() -> None:
            started = time.perf_counter()
            async with semaphore:
                try:
                    async with session.get(url) as response:
                        await response.read()
                        statuses[str(response.status)] += 1
                except Exception as exc:  # report by class without leaking request data
                    statuses[f"exception:{type(exc).__name__}"] += 1
                finally:
                    latencies_ms.observe((time.perf_counter() - started) * 1000)

        requested = max(1, round(args.duration * args.rps))
        started = time.perf_counter()
        for sequence in range(requested):
            due = started + (sequence / args.rps)
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            task = asyncio.create_task(issue_request())
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        if tasks:
            await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started

    total = sum(statuses.values())
    successes = statuses.get("200", 0)
    unexpected = total - successes
    achieved_rps = total / elapsed if elapsed else 0
    p50 = percentile(latencies_ms.samples, 50)
    p95 = percentile(latencies_ms.samples, 95)
    p99 = percentile(latencies_ms.samples, 99)
    error_rate = (unexpected / total * 100) if total else 100.0

    print(f"requests={total} elapsed={elapsed:.2f}s achieved_rps={achieved_rps:.1f}")
    print(
        f"latency_ms min={latencies_ms.minimum if latencies_ms.count else math.nan:.1f} "
        f"mean={latencies_ms.mean:.1f} "
        f"p50={p50:.1f} p95={p95:.1f} p99={p99:.1f} "
        f"max={latencies_ms.maximum if latencies_ms.count else math.nan:.1f} "
        f"percentile_sample={len(latencies_ms.samples)}/{latencies_ms.count}"
    )
    print(f"statuses={dict(statuses)} unexpected_error_rate={error_rate:.4f}%")

    failed = False
    if p95 > args.max_p95_ms:
        print(f"FAIL: p95 {p95:.1f}ms exceeds {args.max_p95_ms:.1f}ms", file=sys.stderr)
        failed = True
    if error_rate > args.max_error_percent:
        print(
            f"FAIL: error rate {error_rate:.4f}% exceeds {args.max_error_percent:.4f}%",
            file=sys.stderr,
        )
        failed = True
    if achieved_rps < args.rps * 0.95:
        print(
            f"FAIL: achieved {achieved_rps:.1f} RPS is below 95% of target {args.rps:.1f}",
            file=sys.stderr,
        )
        failed = True
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="for example https://price.example.com")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--path", help="explicit authenticated path, including any query")
    target.add_argument(
        "--symbols",
        default=os.environ.get("QUICKPRICE_SYMBOLS", DEFAULT_SYMBOLS),
        help="comma-separated quote symbols; defaults to QUICKPRICE_SYMBOLS or the built-ins",
    )
    parser.add_argument("--duration", type=float, default=60.0, help="seconds")
    parser.add_argument("--rps", type=float, default=300.0)
    parser.add_argument("--concurrency", type=int, default=500)
    parser.add_argument("--timeout", type=float, default=10.0, help="per request seconds")
    parser.add_argument(
        "--latency-sample-size",
        type=int,
        default=200_000,
        help="bounded reservoir used for percentiles during long soak tests",
    )
    parser.add_argument("--max-p95-ms", type=float, default=100.0)
    parser.add_argument("--max-error-percent", type=float, default=0.1)
    parser.add_argument(
        "--skip-connection-prewarm",
        action="store_true",
        help="do not pre-establish the requested number of keep-alive connections",
    )
    args = parser.parse_args()

    if (
        args.duration <= 0
        or args.rps <= 0
        or args.concurrency <= 0
        or args.latency_sample_size <= 0
    ):
        parser.error("duration, rps, concurrency and latency sample size must all be positive")
    if args.path is None:
        symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        if not 1 <= len(symbols) <= 100:
            parser.error("--symbols must contain between 1 and 100 symbols")
        args.path = f"/v1/quotes?{urlencode({'symbols': ','.join(symbols)})}"
    if not args.path.startswith("/"):
        parser.error("--path must start with /")

    api_key = os.environ.get("QUICKPRICE_API_KEY") or getpass.getpass(
        "QuickPrice API key (input is hidden): "
    )
    if not api_key:
        parser.error("QUICKPRICE_API_KEY is empty")
    return asyncio.run(run(args, api_key))


if __name__ == "__main__":
    raise SystemExit(main())
