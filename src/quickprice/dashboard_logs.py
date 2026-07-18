"""Bounded, credential-redacting log delivery for the dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

DEFAULT_MAX_SUBSCRIBERS = 8

_URL_QUERY = re.compile(r"((?:https?://|/)[^\s?#]*)\?[^\s#]*", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(x-api-key|api[-_]?key|authorization|access[-_]?token|token|password|secret)\b"
    r"(\s*(?::|=)\s*|\s+)(?:bearer\s+)?[^\s,;]+",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DashboardLogEvent:
    """A stable log event suitable for JSON and Server-Sent Events."""

    id: int
    timestamp: str
    level: str
    logger: str
    message: str

    def as_dict(self) -> dict[str, int | str]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
        }

    def as_sse(self) -> str:
        payload = json.dumps(self.as_dict(), ensure_ascii=True, separators=(",", ":"))
        return f"id: {self.id}\nevent: log\ndata: {payload}\n\n"


class DashboardLogCapacityError(RuntimeError):
    """Raised before streaming when the bounded subscriber pool is full."""


@dataclass(frozen=True, slots=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[DashboardLogEvent]


class _DashboardLogStream(AsyncIterator[str]):
    """An acquired stream that releases its subscriber on close or cancellation."""

    def __init__(
        self,
        *,
        broker: DashboardLogBroker,
        subscriber_id: int,
        queue: asyncio.Queue[DashboardLogEvent],
        heartbeat_seconds: float,
    ) -> None:
        self._broker = broker
        self._subscriber_id = subscriber_id
        self._queue = queue
        self._heartbeat_seconds = heartbeat_seconds
        self._closed = False

    def __aiter__(self) -> _DashboardLogStream:
        return self

    async def __anext__(self) -> str:
        if self._closed:
            raise StopAsyncIteration
        try:
            event = await asyncio.wait_for(
                self._queue.get(),
                timeout=self._heartbeat_seconds,
            )
        except TimeoutError:
            timestamp = datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            payload = json.dumps({"timestamp": timestamp}, separators=(",", ":"))
            return f"event: heartbeat\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            self._release()
            raise
        except Exception:
            self._release()
            raise
        return event.as_sse()

    async def aclose(self) -> None:
        self._release()

    def _release(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._broker.unsubscribe(self._subscriber_id)


class DashboardLogBroker(logging.Handler):
    """Capture records into a bounded backlog and fan them out without blocking emitters."""

    def __init__(
        self,
        *,
        capacity: int = 500,
        client_queue_size: int = 100,
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
        redacted_values: Iterable[str | None] = (),
    ) -> None:
        if capacity <= 0 or client_queue_size <= 0 or max_subscribers <= 0:
            raise ValueError("dashboard log bounds must be positive")
        super().__init__(level=logging.DEBUG)
        self._events: deque[DashboardLogEvent] = deque(maxlen=capacity)
        self._client_queue_size = client_queue_size
        self._max_subscribers = max_subscribers
        self._redacted_values = tuple(
            sorted(
                {value for item in redacted_values if (value := (item or "").strip())},
                key=len,
                reverse=True,
            )
        )
        self._subscribers: dict[int, _Subscriber] = {}
        self._next_subscriber_id = 1
        self._next_event_id = 1
        self._state_lock = threading.Lock()

    def redact(self, value: str) -> str:
        """Remove configured values, credential assignments, and URL query strings."""

        result = value
        for secret in self._redacted_values:
            if len(secret) >= 4:
                result = result.replace(secret, "[REDACTED]")
        result = _URL_QUERY.sub(r"\1?[REDACTED]", result)
        return _SECRET_ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
            result,
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            timestamp = (
                datetime.fromtimestamp(record.created, tz=UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            event = DashboardLogEvent(
                id=0,
                timestamp=timestamp,
                level=record.levelname.upper(),
                logger=self.redact(record.name),
                message=self.redact(record.getMessage()),
            )
            with self._state_lock:
                event = DashboardLogEvent(
                    id=self._next_event_id,
                    timestamp=event.timestamp,
                    level=event.level,
                    logger=event.logger,
                    message=event.message,
                )
                self._next_event_id += 1
                self._events.append(event)
                subscribers = tuple(self._subscribers.values())
            for subscriber in subscribers:
                try:
                    subscriber.loop.call_soon_threadsafe(
                        self._offer,
                        subscriber.queue,
                        event,
                    )
                except RuntimeError:
                    continue
        except Exception:
            self.handleError(record)

    @staticmethod
    def _offer(
        queue: asyncio.Queue[DashboardLogEvent],
        event: DashboardLogEvent,
    ) -> None:
        try:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        except asyncio.QueueEmpty, asyncio.QueueFull:
            return

    def snapshot(self) -> tuple[DashboardLogEvent, ...]:
        with self._state_lock:
            return tuple(self._events)

    def subscribe(
        self,
        *,
        after_id: int | None = None,
    ) -> tuple[int, asyncio.Queue[DashboardLogEvent]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[DashboardLogEvent] = asyncio.Queue(maxsize=self._client_queue_size)
        with self._state_lock:
            if len(self._subscribers) >= self._max_subscribers:
                raise DashboardLogCapacityError("dashboard log subscriber limit reached")
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            initial = tuple(
                event for event in self._events if after_id is None or event.id > after_id
            )[-self._client_queue_size :]
            self._subscribers[subscriber_id] = _Subscriber(loop=loop, queue=queue)
        for event in initial:
            queue.put_nowait(event)
        return subscriber_id, queue

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._state_lock:
            self._subscribers.pop(subscriber_id, None)

    @property
    def subscriber_count(self) -> int:
        with self._state_lock:
            return len(self._subscribers)

    def stream(
        self,
        *,
        after_id: int | None = None,
        heartbeat_seconds: float = 15.0,
    ) -> AsyncIterator[str]:
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        subscriber_id, queue = self.subscribe(after_id=after_id)
        return _DashboardLogStream(
            broker=self,
            subscriber_id=subscriber_id,
            queue=queue,
            heartbeat_seconds=heartbeat_seconds,
        )

    def close(self) -> None:
        with self._state_lock:
            self._subscribers.clear()
        super().close()
