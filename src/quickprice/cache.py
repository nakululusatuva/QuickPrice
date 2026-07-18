"""Lock-protected, copy-on-write snapshots and bounded history rings."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from threading import RLock
from types import MappingProxyType

from .domain import UTC, PricePoint, QuoteSnapshot


class SnapshotStore:
    """Per-symbol snapshot storage with bounded lock contention.

    Publishing one symbol must remain O(1) as trusted plugins add large
    catalogs. A consolidated immutable mapping is built only for the uncommon
    metrics/debug path that explicitly asks for every snapshot.
    """

    def __init__(self, shard_count: int = 64) -> None:
        if shard_count <= 0:
            raise ValueError("snapshot shard count must be positive")
        self._locks = tuple(RLock() for _ in range(shard_count))
        self._shards: tuple[dict[str, QuoteSnapshot], ...] = tuple({} for _ in range(shard_count))

    def _shard_index(self, symbol: str) -> int:
        return hash(symbol) % len(self._shards)

    def publish(self, snapshot: QuoteSnapshot) -> None:
        symbol = snapshot.quote.symbol
        index = self._shard_index(symbol)
        with self._locks[index]:
            shard = self._shards[index]
            current = shard.get(symbol)
            if current is not None and snapshot.quote.as_of < current.quote.as_of:
                return
            shard[symbol] = snapshot

    def get(self, symbol: str) -> QuoteSnapshot | None:
        index = self._shard_index(symbol)
        with self._locks[index]:
            return self._shards[index].get(symbol)

    def all(self) -> Mapping[str, QuoteSnapshot]:
        combined: dict[str, QuoteSnapshot] = {}
        for lock, shard in zip(self._locks, self._shards, strict=True):
            with lock:
                combined.update(shard)
        return MappingProxyType(combined)

    def clone(self) -> SnapshotStore:
        """Return an isolated store containing the current immutable snapshots."""

        replacement = SnapshotStore(shard_count=len(self._shards))
        for snapshot in self.all().values():
            replacement.publish(snapshot)
        return replacement


class HistoryCache:
    ONE_MINUTE_RETENTION = timedelta(hours=48)
    FIVE_MINUTE_RETENTION = timedelta(days=45)
    DAILY_RETENTION = timedelta(days=400)

    def __init__(self) -> None:
        self._lock = RLock()
        self._one_minute: dict[str, deque[PricePoint]] = defaultdict(deque)
        self._five_minute: dict[str, deque[PricePoint]] = defaultdict(deque)
        self._daily: dict[str, deque[PricePoint]] = defaultdict(deque)

    @staticmethod
    def _bucket(timestamp: datetime, minutes: int) -> datetime:
        timestamp = timestamp.astimezone(UTC)
        minute = timestamp.minute - timestamp.minute % minutes
        return timestamp.replace(minute=minute, second=0, microsecond=0)

    @staticmethod
    def _upsert_bucket(target: deque[PricePoint], point: PricePoint, bucket: datetime) -> None:
        bucketed = PricePoint(
            symbol=point.symbol,
            timestamp=bucket,
            price=point.price,
            provider=point.provider,
            is_derived=point.is_derived,
            interval=point.interval,
        )
        if target and target[-1].timestamp == bucket:
            target[-1] = bucketed
        elif not target or target[-1].timestamp < bucket:
            target.append(bucketed)
        else:
            values = {item.timestamp: item for item in target}
            values[bucket] = bucketed
            target.clear()
            target.extend(values[key] for key in sorted(values))

    def add(self, point: PricePoint) -> None:
        with self._lock:
            if point.interval == "1d":
                self._upsert_bucket(self._daily[point.symbol], point, point.timestamp)
            elif point.interval == "5m":
                five_bucket = self._bucket(point.timestamp, 5)
                self._upsert_bucket(self._five_minute[point.symbol], point, five_bucket)
            elif point.interval == "1m":
                one_bucket = self._bucket(point.timestamp, 1)
                self._upsert_bucket(self._one_minute[point.symbol], point, one_bucket)
                five_bucket = self._bucket(point.timestamp, 5)
                five_point = PricePoint(
                    point.symbol,
                    point.timestamp,
                    point.price,
                    point.provider,
                    point.is_derived,
                    "5m",
                )
                self._upsert_bucket(self._five_minute[point.symbol], five_point, five_bucket)
            else:
                raise ValueError(f"unsupported history interval: {point.interval}")
            self._prune_symbol_locked(point.symbol, point.timestamp)

    def load(self, points: list[PricePoint]) -> None:
        if not points:
            return

        # Prepare each ring once. Calling ``add`` for an older backfill point
        # rebuilds an existing deque, which becomes quadratic when a live tail
        # is already present. A dictionary merge plus one sort per symbol keeps
        # a large bootstrap bounded to O(n log n).
        incoming_one: dict[str, dict[datetime, PricePoint]] = defaultdict(dict)
        incoming_five: dict[str, dict[datetime, PricePoint]] = defaultdict(dict)
        incoming_daily: dict[str, dict[datetime, PricePoint]] = defaultdict(dict)
        for point in sorted(points, key=lambda item: item.timestamp):
            if point.interval == "1d":
                incoming_daily[point.symbol][point.timestamp] = point
                continue
            if point.interval == "1m":
                one_bucket = self._bucket(point.timestamp, 1)
                incoming_one[point.symbol][one_bucket] = PricePoint(
                    point.symbol,
                    one_bucket,
                    point.price,
                    point.provider,
                    point.is_derived,
                    point.interval,
                )
            elif point.interval != "5m":
                raise ValueError(f"unsupported history interval: {point.interval}")
            five_bucket = self._bucket(point.timestamp, 5)
            incoming_five[point.symbol][five_bucket] = PricePoint(
                point.symbol,
                five_bucket,
                point.price,
                point.provider,
                point.is_derived,
                "5m",
            )

        with self._lock:
            symbols = set(incoming_one) | set(incoming_five) | set(incoming_daily)
            for symbol in symbols:
                one = incoming_one[symbol]
                five = incoming_five[symbol]
                daily = incoming_daily[symbol]

                # Existing points are normally newer live observations. They
                # win an exact bucket collision over a historical backfill.
                one.update({item.timestamp: item for item in self._one_minute.get(symbol, ())})
                five.update({item.timestamp: item for item in self._five_minute.get(symbol, ())})
                daily.update({item.timestamp: item for item in self._daily.get(symbol, ())})

                latest = max((*one, *five, *daily))
                one_cutoff = latest - self.ONE_MINUTE_RETENTION
                five_cutoff = latest - self.FIVE_MINUTE_RETENTION
                daily_cutoff = latest - self.DAILY_RETENTION
                self._replace_ring(
                    self._one_minute,
                    symbol,
                    (one[key] for key in sorted(one) if key >= one_cutoff),
                )
                self._replace_ring(
                    self._five_minute,
                    symbol,
                    (five[key] for key in sorted(five) if key >= five_cutoff),
                )
                self._replace_ring(
                    self._daily,
                    symbol,
                    (daily[key] for key in sorted(daily) if key >= daily_cutoff),
                )

    @staticmethod
    def _replace_ring(
        rings: dict[str, deque[PricePoint]],
        symbol: str,
        points: Iterable[PricePoint],
    ) -> None:
        replacement = deque(points)
        if replacement:
            rings[symbol] = replacement
        else:
            rings.pop(symbol, None)

    @staticmethod
    def _prune_ring(
        rings: dict[str, deque[PricePoint]],
        symbol: str,
        cutoff: datetime,
    ) -> int:
        ring = rings.get(symbol)
        if ring is None:
            return 0
        previous_size = len(ring)
        while ring and ring[0].timestamp < cutoff:
            ring.popleft()
        if not ring:
            rings.pop(symbol, None)
        return previous_size - len(ring)

    def _prune_symbol_locked(self, symbol: str, now: datetime) -> int:
        one_cutoff = now - self.ONE_MINUTE_RETENTION
        five_cutoff = now - self.FIVE_MINUTE_RETENTION
        daily_cutoff = now - self.DAILY_RETENTION
        return sum(
            (
                self._prune_ring(self._one_minute, symbol, one_cutoff),
                self._prune_ring(self._five_minute, symbol, five_cutoff),
                self._prune_ring(self._daily, symbol, daily_cutoff),
            )
        )

    def prune(self, now: datetime | None = None) -> int:
        """Expire every history ring, including symbols no longer being collected."""

        effective_now = datetime.now(UTC) if now is None else now.astimezone(UTC)
        with self._lock:
            symbols = set(self._one_minute) | set(self._five_minute) | set(self._daily)
            return sum(self._prune_symbol_locked(symbol, effective_now) for symbol in symbols)

    def points(self, symbol: str) -> tuple[PricePoint, ...]:
        with self._lock:
            one = tuple(self._one_minute.get(symbol, ()))
            five = tuple(self._five_minute.get(symbol, ()))
            daily = tuple(self._daily.get(symbol, ()))
        merged = daily
        if five:
            cutoff = five[0].timestamp
            merged = tuple(point for point in merged if point.timestamp < cutoff) + five
        if one:
            cutoff = one[0].timestamp
            merged = tuple(point for point in merged if point.timestamp < cutoff) + one
        return merged

    def points_for_interval(self, symbol: str, interval: str) -> tuple[PricePoint, ...]:
        """Return one complete ring without hiding overlaps from other intervals."""

        with self._lock:
            if interval == "1m":
                return tuple(self._one_minute.get(symbol, ()))
            if interval == "5m":
                return tuple(self._five_minute.get(symbol, ()))
            if interval == "1d":
                return tuple(self._daily.get(symbol, ()))
        raise ValueError(f"unsupported history interval: {interval}")

    def sizes(self) -> dict[str, dict[str, int]]:
        with self._lock:
            symbols = set(self._one_minute) | set(self._five_minute) | set(self._daily)
            return {
                symbol: {
                    "1m": len(self._one_minute.get(symbol, ())),
                    "5m": len(self._five_minute.get(symbol, ())),
                    "1d": len(self._daily.get(symbol, ())),
                }
                for symbol in symbols
            }
