"""Lock-protected, copy-on-write snapshots and bounded history rings."""

from __future__ import annotations

import dataclasses
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from threading import RLock
from types import MappingProxyType

from .analytics import WINDOWS
from .domain import UTC, PricePoint, QuoteSnapshot


class _ReferenceCacheEntry:
    __slots__ = ("as_of", "minute", "references")

    def __init__(
        self,
        as_of: datetime,
        references: Mapping[str, PricePoint | None],
    ) -> None:
        self.as_of = as_of
        self.minute = as_of.replace(second=0, microsecond=0)
        self.references = MappingProxyType(dict(references))


@dataclasses.dataclass(frozen=True, slots=True)
class _SyntheticHistoryRecipe:
    inputs: tuple[str, ...]
    operation: str
    max_skew: timedelta
    provider: str


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

    def retain_symbols(self, symbols: Iterable[str]) -> int:
        """Remove snapshots that are no longer addressable by the active catalog."""

        retained = set(symbols)
        removed = 0
        for lock, shard in zip(self._locks, self._shards, strict=True):
            with lock:
                discarded = tuple(symbol for symbol in shard if symbol not in retained)
                for symbol in discarded:
                    del shard[symbol]
                removed += len(discarded)
        return removed


class HistoryCache:
    ONE_MINUTE_RETENTION = timedelta(hours=48)
    FIVE_MINUTE_RETENTION = timedelta(days=45)
    DAILY_RETENTION = timedelta(days=400)

    def __init__(self) -> None:
        self._lock = RLock()
        self._one_minute: dict[str, deque[PricePoint]] = defaultdict(deque)
        self._five_minute: dict[str, deque[PricePoint]] = defaultdict(deque)
        self._daily: dict[str, deque[PricePoint]] = defaultdict(deque)
        self._reference_cache: dict[str, _ReferenceCacheEntry] = {}
        self._synthetic_recipes: dict[str, _SyntheticHistoryRecipe] = {}
        self._synthetic_dependents: dict[str, set[str]] = defaultdict(set)

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
            self._invalidate_references_locked(point.symbol, point.timestamp)

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
                self._invalidate_references_locked(symbol)
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
        removed = sum(
            (
                self._prune_ring(self._one_minute, symbol, one_cutoff),
                self._prune_ring(self._five_minute, symbol, five_cutoff),
                self._prune_ring(self._daily, symbol, daily_cutoff),
            )
        )
        if removed:
            self._invalidate_references_locked(symbol)
        return removed

    def prune(self, now: datetime | None = None) -> int:
        """Expire every history ring, including symbols no longer being collected."""

        effective_now = datetime.now(UTC) if now is None else now.astimezone(UTC)
        with self._lock:
            symbols = set(self._one_minute) | set(self._five_minute) | set(self._daily)
            return sum(self._prune_symbol_locked(symbol, effective_now) for symbol in symbols)

    def retain_symbols(self, symbols: Iterable[str]) -> int:
        """Drop inactive in-memory histories while leaving durable rows untouched."""

        retained = set(symbols)
        with self._lock:
            # Active virtual outputs require their component rings even when a
            # future managed catalog keeps those components private.
            pending = list(retained)
            while pending:
                recipe = self._synthetic_recipes.get(pending.pop())
                if recipe is None:
                    continue
                for dependency in recipe.inputs:
                    if dependency not in retained:
                        retained.add(dependency)
                        pending.append(dependency)

            present = set(self._one_minute) | set(self._five_minute) | set(self._daily)
            removed_points = 0
            for symbol in present - retained:
                removed_points += len(self._one_minute.pop(symbol, ()))
                removed_points += len(self._five_minute.pop(symbol, ()))
                removed_points += len(self._daily.pop(symbol, ()))
                self._reference_cache.pop(symbol, None)

            for symbol in tuple(self._synthetic_recipes):
                if symbol in retained:
                    continue
                recipe = self._synthetic_recipes.pop(symbol)
                self._reference_cache.pop(symbol, None)
                for dependency in recipe.inputs:
                    dependents = self._synthetic_dependents.get(dependency)
                    if dependents is None:
                        continue
                    dependents.discard(symbol)
                    if not dependents:
                        self._synthetic_dependents.pop(dependency, None)
            return removed_points

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

    def register_synthetic_history(
        self,
        symbol: str,
        inputs: tuple[str, ...],
        *,
        operation: str,
        max_skew: timedelta,
        provider: str,
    ) -> None:
        """Keep a synthetic history virtual instead of duplicating component rings."""

        expected = 1 if operation == "inverse" else 2
        if operation not in {"inverse", "multiply", "divide"} or len(inputs) != expected:
            raise ValueError("invalid synthetic history recipe")
        if max_skew < timedelta(0):
            raise ValueError("synthetic history maximum skew cannot be negative")
        recipe = _SyntheticHistoryRecipe(inputs, operation, max_skew, provider)
        with self._lock:

            def reaches_target(candidate: str, visited: set[str]) -> bool:
                if candidate == symbol:
                    return True
                if candidate in visited:
                    return False
                visited.add(candidate)
                dependency_recipe = self._synthetic_recipes.get(candidate)
                return dependency_recipe is not None and any(
                    reaches_target(dependency, visited) for dependency in dependency_recipe.inputs
                )

            if any(reaches_target(dependency, set()) for dependency in inputs):
                raise ValueError("synthetic history recipes cannot contain a dependency cycle")
            current = self._synthetic_recipes.get(symbol)
            if current == recipe:
                return
            if current is not None:
                for dependency in current.inputs:
                    self._synthetic_dependents[dependency].discard(symbol)
            self._synthetic_recipes[symbol] = recipe
            for dependency in inputs:
                self._synthetic_dependents[dependency].add(symbol)
            # Previously materialized synthetic rings are redundant once the
            # recipe can derive the six rolling anchors from component rings.
            self._one_minute.pop(symbol, None)
            self._five_minute.pop(symbol, None)
            self._daily.pop(symbol, None)
            self._invalidate_references_locked(symbol)

    def _invalidate_references_locked(
        self,
        symbol: str,
        timestamp: datetime | None = None,
        visited: set[str] | None = None,
    ) -> None:
        visited = set() if visited is None else visited
        if symbol in visited:
            return
        visited.add(symbol)
        cached = self._reference_cache.get(symbol)
        if timestamp is None or (cached is not None and timestamp <= cached.as_of - WINDOWS["1h"]):
            self._reference_cache.pop(symbol, None)
        for dependent in self._synthetic_dependents.get(symbol, ()):
            self._invalidate_references_locked(dependent, timestamp, visited)

    @staticmethod
    def _latest_at_or_before(
        ring: deque[PricePoint] | None,
        cutoff: datetime,
    ) -> PricePoint | None:
        if not ring:
            return None
        return next((point for point in reversed(ring) if point.timestamp <= cutoff), None)

    def _reference_at_locked(self, symbol: str, cutoff: datetime) -> PricePoint | None:
        """Match the interval precedence used by :meth:`points` without copying."""

        recipe = self._synthetic_recipes.get(symbol)
        if recipe is not None:
            return self._synthetic_reference_at_locked(symbol, recipe, cutoff)

        one = self._one_minute.get(symbol)
        if one and one[0].timestamp <= cutoff:
            return self._latest_at_or_before(one, cutoff)
        five = self._five_minute.get(symbol)
        if five and five[0].timestamp <= cutoff:
            return self._latest_at_or_before(five, cutoff)
        return self._latest_at_or_before(self._daily.get(symbol), cutoff)

    def _synthetic_reference_at_locked(
        self,
        symbol: str,
        recipe: _SyntheticHistoryRecipe,
        cutoff: datetime,
    ) -> PricePoint | None:
        for interval, rings in (
            ("1m", self._one_minute),
            ("5m", self._five_minute),
            ("1d", self._daily),
        ):
            left_ring = rings.get(recipe.inputs[0])
            if not left_ring:
                continue
            eligible_left = (left for left in reversed(left_ring) if left.timestamp <= cutoff)
            if recipe.operation == "inverse":
                for left in eligible_left:
                    try:
                        price = Decimal(1) / left.price
                    except ArithmeticError, ZeroDivisionError:
                        continue
                    if price > 0:
                        return PricePoint(
                            symbol=symbol,
                            timestamp=left.timestamp,
                            price=price,
                            provider=recipe.provider,
                            is_derived=True,
                            interval=interval,
                        )
                continue

            right_ring = rings.get(recipe.inputs[1])
            if not right_ring:
                continue
            right_iterator = iter(reversed(right_ring))
            right = next(right_iterator, None)
            for left in eligible_left:
                while right is not None and right.timestamp > left.timestamp:
                    right = next(right_iterator, None)
                if right is None:
                    break
                if left.timestamp - right.timestamp > recipe.max_skew:
                    continue
                try:
                    price = (
                        left.price * right.price
                        if recipe.operation == "multiply"
                        else left.price / right.price
                    )
                except ArithmeticError, ZeroDivisionError:
                    continue
                if price <= 0:
                    continue
                return PricePoint(
                    symbol=symbol,
                    timestamp=left.timestamp,
                    price=price,
                    provider=recipe.provider,
                    is_derived=True,
                    interval=interval,
                )
        return None

    def change_references(
        self,
        symbol: str,
        current_as_of: datetime,
    ) -> Mapping[str, PricePoint | None]:
        """Return the six rolling cutoff predecessors with a minute-local cache.

        Intraday rings are minute bucketed, so their selected predecessors are
        stable throughout a quote minute. A recent live point therefore updates
        the current price without invalidating historical reference selection.
        """

        current_as_of = current_as_of.astimezone(UTC)
        minute = current_as_of.replace(second=0, microsecond=0)
        with self._lock:
            cached = self._reference_cache.get(symbol)
            if cached is not None and cached.minute == minute:
                return cached.references
            references = {
                name: self._reference_at_locked(symbol, current_as_of - duration)
                for name, duration in WINDOWS.items()
            }
            entry = _ReferenceCacheEntry(current_as_of, references)
            self._reference_cache[symbol] = entry
            return entry.references

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
