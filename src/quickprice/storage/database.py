from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from .records import (
    AdminAuditEventRecord,
    AggregatePriceRecord,
    ApiKeyRecord,
    BootstrapApiKeysCommand,
    CheckpointResult,
    CleanupCommand,
    CleanupResult,
    DividendEventRecord,
    ImportApiKeysCommand,
    LatestSnapshotRecord,
    MinutePriceRecord,
    ProviderCheckpointRecord,
    RestoredState,
    RevokeApiKeyCommand,
    StorageMetrics,
    UpdateApiKeyCommand,
    WriteCommand,
    WriteResult,
    YieldMetricRecord,
    decode_json,
    decode_timestamp,
    encode_json,
    encode_timestamp,
    utc_datetime,
)
from .schema import migrate, verify_schema


class StorageError(RuntimeError):
    pass


class StorageNotRunning(StorageError):
    pass


class StorageQueueFull(StorageError):
    pass


class StorageWriterFailed(StorageError):
    pass


class StorageBatchError(StorageError):
    pass


class StorageCorruptionError(StorageError):
    pass


type FaultInjector = Callable[[str, Sequence[WriteCommand]], None]
type WriteOutcome = WriteResult | CleanupResult

_WRITE_COMMAND_TYPES = (
    MinutePriceRecord,
    AggregatePriceRecord,
    LatestSnapshotRecord,
    DividendEventRecord,
    YieldMetricRecord,
    ProviderCheckpointRecord,
    ApiKeyRecord,
    AdminAuditEventRecord,
    BootstrapApiKeysCommand,
    ImportApiKeysCommand,
    UpdateApiKeyCommand,
    RevokeApiKeyCommand,
    CleanupCommand,
)


@dataclass(frozen=True, slots=True)
class _Barrier:
    pass


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    mode: str


type _ControlCommand = _Barrier | _Checkpoint
type _QueuedCommand = WriteCommand | _ControlCommand


@dataclass(slots=True)
class _Envelope:
    command: _QueuedCommand
    future: concurrent.futures.Future[Any] | None


_STOP = object()


class SQLiteStorage:
    """Single-writer SQLite persistence for QuickPrice.

    All mutation is serialized through one dedicated thread. API/collector tasks only
    enqueue immutable records; the writer commits at most ``batch_size`` commands or
    after ``batch_interval`` seconds, whichever happens first.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        batch_size: int = 100,
        batch_interval: float = 0.250,
        batch_interval_ms: int | None = None,
        queue_capacity: int = 20_000,
        busy_timeout_ms: int = 5_000,
        enqueue_timeout: float = 2.0,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        if str(path) == ":memory:":
            raise ValueError("SQLiteStorage requires a file-backed database")
        if batch_interval_ms is not None:
            if batch_interval_ms <= 0:
                raise ValueError("batch_interval_ms must be positive")
            if batch_interval != 0.250:
                raise ValueError("specify only one of batch_interval and batch_interval_ms")
            batch_interval = batch_interval_ms / 1000
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if batch_interval <= 0:
            raise ValueError("batch_interval must be positive")
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")
        if enqueue_timeout <= 0:
            raise ValueError("enqueue_timeout must be positive")

        self.batch_size = batch_size
        self.batch_interval = batch_interval
        self.queue_capacity = queue_capacity
        self.busy_timeout_ms = busy_timeout_ms
        self.enqueue_timeout = enqueue_timeout
        self.fault_injector = fault_injector

        self._queue: queue.Queue[_Envelope | object] = queue.Queue(maxsize=queue_capacity)
        self._state_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._initialize_lock = threading.Lock()
        self._writer_thread: threading.Thread | None = None
        self._startup_event: threading.Event | None = None
        self._startup_error: BaseException | None = None
        self._fatal_error: BaseException | None = None
        self._initialized = False
        self._stopping = False

        self._max_queue_depth = 0
        self._batches_committed = 0
        self._records_committed = 0
        self._commit_failures = 0
        self._last_commit_ms: float | None = None
        self._last_error: str | None = None
        self._last_checkpoint: CheckpointResult | None = None
        self._unreported_failure: BaseException | None = None

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            thread = self._writer_thread
            return bool(thread is not None and thread.is_alive() and not self._stopping)

    @property
    def schema_version(self) -> int:
        self.initialize()
        with self._reader_connection() as connection:
            row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0])

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                connection.close()
                raise StorageError(f"could not enable WAL mode; SQLite returned {mode!r}")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA wal_autocheckpoint = 1000")
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        if foreign_keys != 1:
            connection.close()
            raise StorageError("SQLite foreign key enforcement is disabled")
        return connection

    @contextmanager
    def _reader_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(read_only=True)
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create/migrate the database; safe to call repeatedly before ``start``."""

        with self._initialize_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                migrate(connection)
                verify_schema(connection)
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    raise StorageCorruptionError(f"SQLite quick_check failed: {integrity}")
            finally:
                connection.close()
            self._initialized = True

    async def start(self) -> None:
        """Initialize storage and start its writer thread."""

        await asyncio.to_thread(self.initialize)
        with self._state_lock:
            if self._writer_thread is not None and self._writer_thread.is_alive():
                if self._stopping:
                    raise StorageNotRunning("storage is stopping")
                return
            self._startup_event = threading.Event()
            self._startup_error = None
            self._fatal_error = None
            self._stopping = False
            thread = threading.Thread(
                target=self._writer_main,
                name="quickprice-sqlite-writer",
                daemon=False,
            )
            self._writer_thread = thread
            thread.start()
            startup_event = self._startup_event

        started = await asyncio.to_thread(startup_event.wait, 10.0)
        if not started:
            raise StorageWriterFailed("SQLite writer did not start within 10 seconds")
        if self._startup_error is not None:
            error = self._startup_error
            with self._state_lock:
                self._writer_thread = None
            raise StorageWriterFailed(f"SQLite writer failed to start: {error}") from error

    async def stop(self, *, shutdown_timeout: float = 15.0) -> None:
        """Flush queued writes in FIFO order and stop the writer."""

        with self._state_lock:
            thread = self._writer_thread
            if thread is None:
                return
            self._stopping = True
        try:
            await asyncio.to_thread(self._queue.put, _STOP, True, self.enqueue_timeout)
        except queue.Full as exc:
            with self._state_lock:
                self._stopping = False
            raise StorageQueueFull("could not enqueue writer stop marker") from exc
        await asyncio.to_thread(thread.join, shutdown_timeout)
        if thread.is_alive():
            raise StorageWriterFailed(
                f"SQLite writer did not stop within {shutdown_timeout:g} seconds"
            )
        with self._state_lock:
            self._writer_thread = None
            self._stopping = False
        if self._fatal_error is not None:
            raise StorageWriterFailed(f"SQLite writer terminated unexpectedly: {self._fatal_error}")
        with self._metrics_lock:
            unreported_failure = self._unreported_failure
            self._unreported_failure = None
        if unreported_failure is not None:
            raise StorageBatchError(
                f"an asynchronous SQLite write failed before shutdown: {unreported_failure}"
            ) from unreported_failure

    async def _put(self, envelope: _Envelope) -> None:
        with self._state_lock:
            thread = self._writer_thread
            if self._stopping or thread is None or not thread.is_alive():
                if self._fatal_error is not None:
                    raise StorageWriterFailed(str(self._fatal_error)) from self._fatal_error
                raise StorageNotRunning("SQLite writer is not running")
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            try:
                await asyncio.to_thread(
                    self._queue.put,
                    envelope,
                    True,
                    self.enqueue_timeout,
                )
            except queue.Full as exc:
                raise StorageQueueFull(
                    f"SQLite writer queue remained full for {self.enqueue_timeout:g} seconds"
                ) from exc
        depth = self._queue.qsize()
        with self._metrics_lock:
            self._max_queue_depth = max(self._max_queue_depth, depth)

    async def enqueue(self, command: WriteCommand, *, wait: bool = False) -> WriteOutcome | None:
        if not isinstance(command, _WRITE_COMMAND_TYPES):
            raise TypeError(f"unsupported storage command: {type(command).__name__}")
        future: concurrent.futures.Future[Any] | None = (
            concurrent.futures.Future() if wait else None
        )
        await self._put(_Envelope(command, future))
        if future is None:
            return None
        return cast(WriteOutcome, await asyncio.shield(asyncio.wrap_future(future)))

    async def enqueue_many(
        self, commands: Iterable[WriteCommand], *, wait: bool = False
    ) -> tuple[WriteOutcome, ...] | None:
        futures: list[concurrent.futures.Future[Any]] = []
        for command in commands:
            if not isinstance(command, _WRITE_COMMAND_TYPES):
                raise TypeError(f"unsupported storage command: {type(command).__name__}")
            future: concurrent.futures.Future[Any] | None = (
                concurrent.futures.Future() if wait else None
            )
            await self._put(_Envelope(command, future))
            if future is not None:
                futures.append(future)
        if not wait:
            return None
        results = await asyncio.gather(
            *(asyncio.shield(asyncio.wrap_future(item)) for item in futures)
        )
        return tuple(cast(WriteOutcome, item) for item in results)

    async def enqueue_minute_price(
        self, record: MinutePriceRecord | Any, *, wait: bool = False
    ) -> WriteOutcome | None:
        if not isinstance(record, MinutePriceRecord):
            record = MinutePriceRecord.from_domain(record)
        return await self.enqueue(record, wait=wait)

    async def enqueue_price(
        self, record: MinutePriceRecord | Any, *, wait: bool = False
    ) -> WriteOutcome | None:
        """Domain-facing alias used by the application service."""

        return await self.enqueue_minute_price(record, wait=wait)

    async def enqueue_aggregate_price(
        self, record: AggregatePriceRecord | Any, *, wait: bool = False
    ) -> WriteOutcome | None:
        if not isinstance(record, AggregatePriceRecord):
            record = AggregatePriceRecord.from_domain(record)
        return await self.enqueue(record, wait=wait)

    async def enqueue_latest_snapshot(
        self, record: LatestSnapshotRecord | Any, *, wait: bool = False
    ) -> WriteOutcome | None:
        if not isinstance(record, LatestSnapshotRecord):
            record = LatestSnapshotRecord.from_domain(record)
        return await self.enqueue(record, wait=wait)

    async def enqueue_snapshot(
        self, record: LatestSnapshotRecord | Any, *, wait: bool = False
    ) -> WriteOutcome | None:
        return await self.enqueue_latest_snapshot(record, wait=wait)

    async def enqueue_dividend(
        self, record: DividendEventRecord | Any, *, wait: bool = False
    ) -> WriteOutcome | None:
        if not isinstance(record, DividendEventRecord):
            record = DividendEventRecord.from_domain(record)
        return await self.enqueue(record, wait=wait)

    async def enqueue_yield_metric(
        self, record: YieldMetricRecord | Any, *, wait: bool = False
    ) -> WriteOutcome | None:
        if not isinstance(record, YieldMetricRecord):
            record = YieldMetricRecord.from_domain(record)
        return await self.enqueue(record, wait=wait)

    async def enqueue_yield(
        self, record: YieldMetricRecord | Any, *, wait: bool = False
    ) -> WriteOutcome | None:
        return await self.enqueue_yield_metric(record, wait=wait)

    async def enqueue_checkpoint_record(
        self, record: ProviderCheckpointRecord, *, wait: bool = False
    ) -> WriteOutcome | None:
        return await self.enqueue(record, wait=wait)

    def load_api_keys(self) -> tuple[ApiKeyRecord, ...]:
        """Load credential metadata without exposing it through market-data reads."""

        self.initialize()
        with self._reader_connection() as connection:
            rows = connection.execute(
                """
                SELECT key_id, name, key_hash, key_hint, created_at, updated_at,
                       expires_at, revoked_at, origin
                FROM api_keys
                ORDER BY created_at, key_id
                """
            ).fetchall()
        return tuple(
            ApiKeyRecord(
                key_id=str(row["key_id"]),
                name=str(row["name"]),
                key_hash=str(row["key_hash"]),
                key_hint=None if row["key_hint"] is None else str(row["key_hint"]),
                created_at=decode_timestamp(str(row["created_at"])),
                updated_at=decode_timestamp(str(row["updated_at"])),
                expires_at=(
                    None if row["expires_at"] is None else decode_timestamp(str(row["expires_at"]))
                ),
                revoked_at=(
                    None if row["revoked_at"] is None else decode_timestamp(str(row["revoked_at"]))
                ),
                origin=str(row["origin"]),
            )
            for row in rows
        )

    def api_key_bootstrap_complete(self) -> bool:
        self.initialize()
        with self._reader_connection() as connection:
            row = connection.execute(
                "SELECT value FROM auth_metadata WHERE name = 'legacy_api_key_bootstrap'"
            ).fetchone()
        return row is not None and str(row["value"]) == "complete"

    def load_admin_audit_events(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        """Return a bounded, already-redacted administrator audit projection."""

        if not 1 <= limit <= 500:
            raise ValueError("audit event limit must be between 1 and 500")
        self.initialize()
        with self._reader_connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, occurred_at, request_id, client_ip, action,
                       target_type, target_id, details_json
                FROM admin_audit_events
                ORDER BY occurred_at DESC, event_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(
            {
                "event_id": str(row["event_id"]),
                "occurred_at": str(row["occurred_at"]),
                "request_id": str(row["request_id"]),
                "client_ip": str(row["client_ip"]),
                "action": str(row["action"]),
                "target_type": str(row["target_type"]),
                "target_id": None if row["target_id"] is None else str(row["target_id"]),
                "details": decode_json(str(row["details_json"])),
            }
            for row in rows
        )

    async def bootstrap_api_keys(
        self,
        records: Iterable[ApiKeyRecord],
        *,
        completed_at: datetime,
    ) -> WriteOutcome | None:
        return await self.enqueue(
            BootstrapApiKeysCommand(tuple(records), completed_at),
            wait=True,
        )

    async def create_api_key(self, record: ApiKeyRecord) -> WriteOutcome | None:
        return await self.enqueue(record, wait=True)

    async def import_api_keys(
        self,
        records: Iterable[ApiKeyRecord],
        *,
        audit: AdminAuditEventRecord,
    ) -> WriteOutcome | None:
        return await self.enqueue(ImportApiKeysCommand(tuple(records), audit), wait=True)

    async def update_api_key(
        self,
        *,
        key_id: str,
        name: str,
        expires_at: datetime | None,
        updated_at: datetime,
        audit: AdminAuditEventRecord,
    ) -> WriteOutcome | None:
        return await self.enqueue(
            UpdateApiKeyCommand(key_id, name, expires_at, updated_at, audit),
            wait=True,
        )

    async def revoke_api_key(
        self,
        *,
        key_id: str,
        revoked_at: datetime,
        audit: AdminAuditEventRecord,
    ) -> WriteOutcome | None:
        return await self.enqueue(RevokeApiKeyCommand(key_id, revoked_at, audit), wait=True)

    async def append_admin_audit(self, record: AdminAuditEventRecord) -> WriteOutcome | None:
        return await self.enqueue(record, wait=True)

    async def flush(self) -> None:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        await self._put(_Envelope(_Barrier(), future))
        await asyncio.shield(asyncio.wrap_future(future))

    async def checkpoint(
        self, mode: Literal["PASSIVE", "FULL", "RESTART", "TRUNCATE"] = "PASSIVE"
    ) -> CheckpointResult:
        normalized = mode.upper()
        if normalized not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError(f"unsupported WAL checkpoint mode: {mode!r}")
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        await self._put(_Envelope(_Checkpoint(normalized), future))
        return cast(
            CheckpointResult,
            await asyncio.shield(asyncio.wrap_future(future)),
        )

    async def cleanup(
        self,
        *,
        now: datetime | None = None,
        minute_retention: timedelta = timedelta(hours=48),
        aggregate_retention: timedelta = timedelta(days=45),
        daily_retention: timedelta = timedelta(days=400),
    ) -> CleanupResult:
        current = utc_datetime(now or datetime.now(UTC))
        if (
            minute_retention <= timedelta(0)
            or aggregate_retention <= timedelta(0)
            or daily_retention <= timedelta(0)
        ):
            raise ValueError("retention durations must be positive")
        outcome = await self.enqueue(
            CleanupCommand(
                minute_before=current - minute_retention,
                aggregate_before=current - aggregate_retention,
                daily_before=current - daily_retention,
            ),
            wait=True,
        )
        return cast(CleanupResult, outcome)

    def _writer_main(self) -> None:
        connection: sqlite3.Connection | None = None
        stopping = False
        try:
            connection = self._connect()
            if self._startup_event is not None:
                self._startup_event.set()
            while not stopping:
                item = self._queue.get()
                if item is _STOP:
                    self._queue.task_done()
                    break

                envelopes = [cast(_Envelope, item)]
                deadline = time.monotonic() + self.batch_interval
                while len(envelopes) < self.batch_size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        item = self._queue.get(timeout=remaining)
                    except queue.Empty:
                        break
                    if item is _STOP:
                        self._queue.task_done()
                        stopping = True
                        break
                    envelope = cast(_Envelope, item)
                    envelopes.append(envelope)
                    # Controls should run promptly and delimit write transactions.
                    if isinstance(envelope.command, (_Barrier, _Checkpoint)):
                        break

                self._process_envelopes(connection, envelopes)
                for _ in envelopes:
                    self._queue.task_done()
        except BaseException as exc:
            self._fatal_error = exc
            if self._startup_event is not None and not self._startup_event.is_set():
                self._startup_error = exc
                self._startup_event.set()
            self._fail_pending(exc)
        finally:
            if connection is not None:
                connection.close()

    def _process_envelopes(
        self, connection: sqlite3.Connection, envelopes: Sequence[_Envelope]
    ) -> None:
        segment: list[_Envelope] = []
        for envelope in envelopes:
            if isinstance(envelope.command, (_Barrier, _Checkpoint)):
                if segment:
                    self._commit_segment(connection, segment)
                    segment = []
                self._process_control(connection, envelope)
            else:
                segment.append(envelope)
        if segment:
            self._commit_segment(connection, segment)

    def _commit_segment(
        self, connection: sqlite3.Connection, envelopes: Sequence[_Envelope]
    ) -> None:
        commands = tuple(cast(WriteCommand, item.command) for item in envelopes)
        started = time.perf_counter()
        try:
            if self.fault_injector is not None:
                self.fault_injector("before_begin", commands)
            connection.execute("BEGIN IMMEDIATE")
            outcomes = [self._execute_write(connection, command) for command in commands]
            if self.fault_injector is not None:
                self.fault_injector("before_commit", commands)
            connection.execute("COMMIT")
        except BaseException as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            elapsed_ms = (time.perf_counter() - started) * 1000
            with self._metrics_lock:
                self._commit_failures += 1
                self._last_commit_ms = elapsed_ms
                self._last_error = f"{type(exc).__name__}: {exc}"
                if any(item.future is None for item in envelopes):
                    self._unreported_failure = exc
            wrapped = StorageBatchError(f"SQLite batch rolled back: {exc}")
            for envelope in envelopes:
                if envelope.future is not None and not envelope.future.done():
                    envelope.future.set_exception(wrapped)
            return

        elapsed_ms = (time.perf_counter() - started) * 1000
        with self._metrics_lock:
            self._batches_committed += 1
            self._records_committed += len(commands)
            self._last_commit_ms = elapsed_ms
        for envelope, outcome in zip(envelopes, outcomes, strict=True):
            if envelope.future is not None and not envelope.future.done():
                envelope.future.set_result(outcome)
        if self.fault_injector is not None:
            # Useful for simulating a process loss after a durable commit. This phase
            # intentionally cannot turn a successful commit into a rollback.
            try:
                self.fault_injector("after_commit", commands)
            except BaseException as exc:
                with self._metrics_lock:
                    self._last_error = f"post-commit fault: {type(exc).__name__}: {exc}"

    def _process_control(self, connection: sqlite3.Connection, envelope: _Envelope) -> None:
        if isinstance(envelope.command, _Barrier):
            with self._metrics_lock:
                failure = self._unreported_failure
                self._unreported_failure = None
            if envelope.future is not None:
                if failure is None:
                    envelope.future.set_result(None)
                else:
                    envelope.future.set_exception(
                        StorageBatchError(f"an asynchronous SQLite write failed: {failure}")
                    )
            return

        command = cast(_Checkpoint, envelope.command)
        try:
            row = connection.execute(f"PRAGMA wal_checkpoint({command.mode})").fetchone()
            result = CheckpointResult(
                busy=int(row[0]),
                wal_frames=int(row[1]),
                checkpointed_frames=int(row[2]),
                mode=command.mode,
            )
            with self._metrics_lock:
                self._last_checkpoint = result
            if envelope.future is not None:
                envelope.future.set_result(result)
        except BaseException as exc:
            with self._metrics_lock:
                self._last_error = f"checkpoint: {type(exc).__name__}: {exc}"
            if envelope.future is not None:
                envelope.future.set_exception(StorageError(f"WAL checkpoint failed: {exc}"))

    def _execute_write(self, connection: sqlite3.Connection, command: WriteCommand) -> WriteOutcome:
        stored_at = encode_timestamp(datetime.now(UTC))
        if isinstance(command, MinutePriceRecord):
            cursor = connection.execute(
                """
                INSERT INTO minute_prices(
                    symbol, timestamp, price, provider, is_derived, source_json, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timestamp, provider) DO UPDATE SET
                    price = excluded.price,
                    is_derived = excluded.is_derived,
                    source_json = excluded.source_json,
                    stored_at = excluded.stored_at
                """,
                (
                    command.symbol,
                    encode_timestamp(command.timestamp),
                    str(command.price),
                    command.provider,
                    int(command.is_derived),
                    encode_json(command.source),
                    stored_at,
                ),
            )
            return WriteResult(cursor.rowcount)

        if isinstance(command, AggregatePriceRecord):
            cursor = connection.execute(
                """
                INSERT INTO aggregate_prices(
                    symbol, bucket_start, interval_seconds, open, high, low, close,
                    sample_count, provider, is_derived, source_json, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval_seconds, bucket_start, provider) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    sample_count = excluded.sample_count,
                    is_derived = excluded.is_derived,
                    source_json = excluded.source_json,
                    stored_at = excluded.stored_at
                """,
                (
                    command.symbol,
                    encode_timestamp(command.bucket_start),
                    command.interval_seconds,
                    str(command.open),
                    str(command.high),
                    str(command.low),
                    str(command.close),
                    command.sample_count,
                    command.provider,
                    int(command.is_derived),
                    encode_json(command.source),
                    stored_at,
                ),
            )
            return WriteResult(cursor.rowcount)

        if isinstance(command, LatestSnapshotRecord):
            cursor = connection.execute(
                """
                INSERT INTO latest_snapshots(symbol, as_of, price, snapshot_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    as_of = excluded.as_of,
                    price = excluded.price,
                    snapshot_json = excluded.snapshot_json,
                    updated_at = excluded.updated_at
                WHERE excluded.as_of >= latest_snapshots.as_of
                """,
                (
                    command.symbol,
                    encode_timestamp(command.as_of),
                    None if command.price is None else str(command.price),
                    encode_json(command.payload),
                    stored_at,
                ),
            )
            return WriteResult(cursor.rowcount)

        if isinstance(command, DividendEventRecord):
            cursor = connection.execute(
                """
                INSERT INTO dividend_events(
                    symbol, ex_date, payment_date, amount, currency, frequency,
                    event_type, is_special, provider, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, ex_date, amount, provider) DO UPDATE SET
                    payment_date = excluded.payment_date,
                    currency = excluded.currency,
                    frequency = excluded.frequency,
                    event_type = excluded.event_type,
                    is_special = excluded.is_special,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    command.symbol,
                    command.ex_date.isoformat(),
                    None if command.payment_date is None else command.payment_date.isoformat(),
                    str(command.amount),
                    command.currency,
                    command.frequency,
                    command.event_type,
                    int(command.is_special),
                    command.provider,
                    encode_json(command.raw),
                    stored_at,
                ),
            )
            return WriteResult(cursor.rowcount)

        if isinstance(command, YieldMetricRecord):
            cursor = connection.execute(
                """
                INSERT INTO yield_metrics(
                    symbol, as_of, annual_percent, method, provider, is_proxy,
                    source_series, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, as_of, method, provider) DO UPDATE SET
                    annual_percent = excluded.annual_percent,
                    is_proxy = excluded.is_proxy,
                    source_series = excluded.source_series,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    command.symbol,
                    encode_timestamp(command.as_of),
                    str(command.annual_percent),
                    command.method,
                    command.provider,
                    int(command.is_proxy),
                    command.source_series,
                    encode_json(command.raw),
                    stored_at,
                ),
            )
            return WriteResult(cursor.rowcount)

        if isinstance(command, ProviderCheckpointRecord):
            cursor = connection.execute(
                """
                INSERT INTO provider_checkpoints(provider, feed, checkpoint_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, feed) DO UPDATE SET
                    checkpoint_json = excluded.checkpoint_json,
                    updated_at = excluded.updated_at
                WHERE excluded.updated_at >= provider_checkpoints.updated_at
                """,
                (
                    command.provider,
                    command.feed,
                    encode_json(command.checkpoint),
                    encode_timestamp(command.updated_at),
                ),
            )
            return WriteResult(cursor.rowcount)

        if isinstance(command, ApiKeyRecord):
            cursor = self._insert_api_key(connection, command)
            return WriteResult(cursor.rowcount)

        if isinstance(command, AdminAuditEventRecord):
            cursor = self._insert_admin_audit(connection, command)
            return WriteResult(cursor.rowcount)

        if isinstance(command, BootstrapApiKeysCommand):
            affected = 0
            for record in command.records:
                cursor = connection.execute(
                    """
                    INSERT INTO api_keys(
                        key_id, name, key_hash, key_hint, created_at, updated_at,
                        expires_at, revoked_at, origin
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key_hash) DO NOTHING
                    """,
                    self._api_key_values(record),
                )
                affected += cursor.rowcount
            metadata = connection.execute(
                """
                INSERT INTO auth_metadata(name, value, updated_at)
                VALUES ('legacy_api_key_bootstrap', 'complete', ?)
                ON CONFLICT(name) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (encode_timestamp(command.completed_at),),
            )
            return WriteResult(affected + metadata.rowcount)

        if isinstance(command, ImportApiKeysCommand):
            affected = 0
            for record in command.records:
                affected += self._insert_api_key(connection, record).rowcount
            affected += self._insert_admin_audit(connection, command.audit).rowcount
            return WriteResult(affected)

        if isinstance(command, UpdateApiKeyCommand):
            cursor = connection.execute(
                """
                UPDATE api_keys
                SET name = ?, expires_at = ?, updated_at = ?
                WHERE key_id = ? AND revoked_at IS NULL
                """,
                (
                    command.name,
                    None if command.expires_at is None else encode_timestamp(command.expires_at),
                    encode_timestamp(command.updated_at),
                    command.key_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"active API key not found: {command.key_id}")
            self._insert_admin_audit(connection, command.audit)
            return WriteResult(cursor.rowcount)

        if isinstance(command, RevokeApiKeyCommand):
            timestamp = encode_timestamp(command.revoked_at)
            cursor = connection.execute(
                """
                UPDATE api_keys
                SET revoked_at = ?, updated_at = ?
                WHERE key_id = ? AND revoked_at IS NULL
                """,
                (timestamp, timestamp, command.key_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"active API key not found: {command.key_id}")
            self._insert_admin_audit(connection, command.audit)
            return WriteResult(cursor.rowcount)

        if isinstance(command, CleanupCommand):
            minute_cursor = connection.execute(
                "DELETE FROM minute_prices WHERE timestamp < ?",
                (encode_timestamp(command.minute_before),),
            )
            aggregate_cursor = connection.execute(
                """
                DELETE FROM aggregate_prices
                WHERE interval_seconds != 86400 AND bucket_start < ?
                """,
                (encode_timestamp(command.aggregate_before),),
            )
            daily_cursor = connection.execute(
                """
                DELETE FROM aggregate_prices
                WHERE interval_seconds = 86400 AND bucket_start < ?
                """,
                (encode_timestamp(command.daily_before),),
            )
            dividend_cursor = connection.execute(
                """
                DELETE FROM dividend_events
                WHERE (symbol, ex_date, amount, provider) NOT IN (
                    SELECT symbol, ex_date, amount, provider
                    FROM (
                        SELECT symbol, ex_date, amount, provider,
                               ROW_NUMBER() OVER (
                                   PARTITION BY symbol
                                   ORDER BY
                                       CASE
                                           WHEN is_special = 0
                                            AND event_type IN ('regular_cash', 'cash_dividend')
                                           THEN 0 ELSE 1
                                       END,
                                       ex_date DESC,
                                       updated_at DESC,
                                       provider DESC
                               ) AS row_number
                        FROM dividend_events
                    )
                    WHERE row_number = 1
                )
                """
            )
            yield_cursor = connection.execute(
                """
                DELETE FROM yield_metrics
                WHERE (symbol, as_of, method, provider) NOT IN (
                    SELECT symbol, as_of, method, provider
                    FROM (
                        SELECT symbol, as_of, method, provider,
                               ROW_NUMBER() OVER (
                                   PARTITION BY symbol
                                   ORDER BY updated_at DESC, as_of DESC,
                                            method DESC, provider DESC
                               ) AS row_number
                        FROM yield_metrics
                    )
                    WHERE row_number = 1
                )
                """
            )
            return CleanupResult(
                minute_prices_deleted=minute_cursor.rowcount,
                aggregate_prices_deleted=aggregate_cursor.rowcount + daily_cursor.rowcount,
                dividend_events_deleted=dividend_cursor.rowcount,
                yield_metrics_deleted=yield_cursor.rowcount,
            )
        raise TypeError(f"unsupported storage command: {type(command).__name__}")

    @staticmethod
    def _api_key_values(record: ApiKeyRecord) -> tuple[Any, ...]:
        return (
            record.key_id,
            record.name,
            record.key_hash,
            record.key_hint,
            encode_timestamp(record.created_at),
            encode_timestamp(record.updated_at),
            None if record.expires_at is None else encode_timestamp(record.expires_at),
            None if record.revoked_at is None else encode_timestamp(record.revoked_at),
            record.origin,
        )

    @classmethod
    def _insert_api_key(
        cls,
        connection: sqlite3.Connection,
        record: ApiKeyRecord,
    ) -> sqlite3.Cursor:
        return connection.execute(
            """
            INSERT INTO api_keys(
                key_id, name, key_hash, key_hint, created_at, updated_at,
                expires_at, revoked_at, origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            cls._api_key_values(record),
        )

    @staticmethod
    def _insert_admin_audit(
        connection: sqlite3.Connection,
        record: AdminAuditEventRecord,
    ) -> sqlite3.Cursor:
        return connection.execute(
            """
            INSERT INTO admin_audit_events(
                event_id, occurred_at, request_id, client_ip, action,
                target_type, target_id, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.event_id,
                encode_timestamp(record.occurred_at),
                record.request_id,
                record.client_ip,
                record.action,
                record.target_type,
                record.target_id,
                encode_json(record.details),
            ),
        )

    def _fail_pending(self, error: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if isinstance(item, _Envelope) and item.future is not None:
                    item.future.set_exception(StorageWriterFailed(str(error)))
            finally:
                self._queue.task_done()

    async def restore(
        self,
        *,
        now: datetime | None = None,
        minute_retention: timedelta = timedelta(hours=48),
        aggregate_retention: timedelta = timedelta(days=45),
        daily_retention: timedelta = timedelta(days=400),
        aggregate_interval_seconds: int = 300,
    ) -> RestoredState:
        await asyncio.to_thread(self.initialize)
        return await asyncio.to_thread(
            self.restore_sync,
            now=now,
            minute_retention=minute_retention,
            aggregate_retention=aggregate_retention,
            daily_retention=daily_retention,
            aggregate_interval_seconds=aggregate_interval_seconds,
        )

    def restore_sync(
        self,
        *,
        now: datetime | None = None,
        minute_retention: timedelta = timedelta(hours=48),
        aggregate_retention: timedelta = timedelta(days=45),
        daily_retention: timedelta = timedelta(days=400),
        aggregate_interval_seconds: int = 300,
    ) -> RestoredState:
        self.initialize()
        current = utc_datetime(now or datetime.now(UTC))
        if (
            minute_retention <= timedelta(0)
            or aggregate_retention <= timedelta(0)
            or daily_retention <= timedelta(0)
        ):
            raise ValueError("retention durations must be positive")
        if aggregate_interval_seconds <= 0:
            raise ValueError("aggregate_interval_seconds must be positive")
        minute_cutoff = encode_timestamp(current - minute_retention)
        aggregate_cutoff = encode_timestamp(current - aggregate_retention)
        daily_cutoff = encode_timestamp(current - daily_retention)
        try:
            with self._reader_connection() as connection:
                minute_rows = connection.execute(
                    """
                    SELECT symbol, timestamp, price, provider, is_derived, source_json
                    FROM minute_prices
                    WHERE timestamp >= ?
                    ORDER BY timestamp, symbol, provider
                    """,
                    (minute_cutoff,),
                ).fetchall()
                aggregate_rows = connection.execute(
                    """
                    SELECT symbol, bucket_start, interval_seconds, open, high, low, close,
                           sample_count, provider, is_derived, source_json
                    FROM aggregate_prices
                    WHERE (interval_seconds = ? AND bucket_start >= ?)
                       OR (interval_seconds = 86400 AND bucket_start >= ?)
                    ORDER BY bucket_start, symbol, provider
                    """,
                    (aggregate_interval_seconds, aggregate_cutoff, daily_cutoff),
                ).fetchall()
                snapshot_rows = connection.execute(
                    """
                    SELECT symbol, as_of, price, snapshot_json
                    FROM latest_snapshots
                    ORDER BY symbol
                    """
                ).fetchall()
                dividend_rows = connection.execute(
                    """
                    SELECT symbol, ex_date, payment_date, amount, currency, frequency,
                           event_type, is_special, provider, raw_json
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY symbol
                            ORDER BY
                                CASE
                                    WHEN is_special = 0
                                     AND event_type IN ('regular_cash', 'cash_dividend')
                                    THEN 0 ELSE 1
                                END,
                                ex_date DESC,
                                updated_at DESC,
                                provider DESC
                        ) AS row_number
                        FROM dividend_events
                    )
                    WHERE row_number = 1
                    ORDER BY symbol
                    """
                ).fetchall()
                yield_rows = connection.execute(
                    """
                    SELECT symbol, as_of, annual_percent, method, provider, is_proxy,
                           source_series, raw_json
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY symbol
                            ORDER BY updated_at DESC, as_of DESC,
                                     method DESC, provider DESC
                        ) AS row_number
                        FROM yield_metrics
                    )
                    WHERE row_number = 1
                    ORDER BY symbol
                    """
                ).fetchall()
                checkpoint_rows = connection.execute(
                    """
                    SELECT provider, feed, checkpoint_json, updated_at
                    FROM provider_checkpoints
                    ORDER BY provider, feed
                    """
                ).fetchall()
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageCorruptionError(
                f"could not restore persisted QuickPrice state: {exc}"
            ) from exc

        try:
            return RestoredState(
                minute_prices=tuple(
                    MinutePriceRecord(
                        symbol=row["symbol"],
                        timestamp=decode_timestamp(row["timestamp"]),
                        price=Decimal(row["price"]),
                        provider=row["provider"],
                        is_derived=bool(row["is_derived"]),
                        source=decode_json(row["source_json"]),
                    )
                    for row in minute_rows
                ),
                aggregate_prices=tuple(
                    AggregatePriceRecord(
                        symbol=row["symbol"],
                        bucket_start=decode_timestamp(row["bucket_start"]),
                        interval_seconds=int(row["interval_seconds"]),
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        sample_count=int(row["sample_count"]),
                        provider=row["provider"],
                        is_derived=bool(row["is_derived"]),
                        source=decode_json(row["source_json"]),
                    )
                    for row in aggregate_rows
                ),
                latest_snapshots=tuple(
                    LatestSnapshotRecord(
                        symbol=row["symbol"],
                        as_of=decode_timestamp(row["as_of"]),
                        price=None if row["price"] is None else Decimal(row["price"]),
                        payload=decode_json(row["snapshot_json"]),
                    )
                    for row in snapshot_rows
                ),
                dividend_events=tuple(
                    DividendEventRecord(
                        symbol=row["symbol"],
                        ex_date=datetime.fromisoformat(row["ex_date"]).date(),
                        payment_date=(
                            None
                            if row["payment_date"] is None
                            else datetime.fromisoformat(row["payment_date"]).date()
                        ),
                        amount=Decimal(row["amount"]),
                        currency=row["currency"],
                        frequency=row["frequency"],
                        event_type=row["event_type"],
                        is_special=bool(row["is_special"]),
                        provider=row["provider"],
                        raw=decode_json(row["raw_json"]),
                    )
                    for row in dividend_rows
                ),
                yield_metric_records=tuple(
                    YieldMetricRecord(
                        symbol=row["symbol"],
                        as_of=decode_timestamp(row["as_of"]),
                        annual_percent=Decimal(row["annual_percent"]),
                        method=row["method"],
                        provider=row["provider"],
                        is_proxy=bool(row["is_proxy"]),
                        source_series=row["source_series"],
                        raw=decode_json(row["raw_json"]),
                    )
                    for row in yield_rows
                ),
                provider_checkpoints=tuple(
                    ProviderCheckpointRecord(
                        provider=row["provider"],
                        feed=row["feed"],
                        checkpoint=decode_json(row["checkpoint_json"]),
                        updated_at=decode_timestamp(row["updated_at"]),
                    )
                    for row in checkpoint_rows
                ),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise StorageCorruptionError(f"invalid persisted QuickPrice record: {exc}") from exc

    async def integrity_check(self) -> str:
        await asyncio.to_thread(self.initialize)

        def check() -> str:
            with self._reader_connection() as connection:
                return str(connection.execute("PRAGMA integrity_check").fetchone()[0])

        result = await asyncio.to_thread(check)
        if result != "ok":
            raise StorageCorruptionError(f"SQLite integrity_check failed: {result}")
        return result

    def metrics(self) -> StorageMetrics:
        def size(path: Path) -> int:
            try:
                return path.stat().st_size
            except FileNotFoundError:
                return 0

        with self._metrics_lock:
            return StorageMetrics(
                queue_depth=self._queue.qsize(),
                queue_capacity=self.queue_capacity,
                max_queue_depth=self._max_queue_depth,
                batches_committed=self._batches_committed,
                records_committed=self._records_committed,
                commit_failures=self._commit_failures,
                last_commit_ms=self._last_commit_ms,
                last_error=self._last_error,
                database_bytes=size(self.path),
                wal_bytes=size(Path(f"{self.path}-wal")),
                shm_bytes=size(Path(f"{self.path}-shm")),
                last_checkpoint=self._last_checkpoint,
            )
