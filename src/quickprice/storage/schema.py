from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

SCHEMA_VERSION = 2


class SchemaTooNewError(RuntimeError):
    pass


MIGRATION_1: tuple[str, ...] = (
    """
    CREATE TABLE schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        checksum TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE minute_prices (
        symbol TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        price TEXT NOT NULL,
        provider TEXT NOT NULL,
        is_derived INTEGER NOT NULL CHECK (is_derived IN (0, 1)),
        source_json TEXT NOT NULL,
        stored_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timestamp, provider)
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX minute_prices_timestamp_idx ON minute_prices(timestamp)",
    """
    CREATE TABLE aggregate_prices (
        symbol TEXT NOT NULL,
        bucket_start TEXT NOT NULL,
        interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
        open TEXT NOT NULL,
        high TEXT NOT NULL,
        low TEXT NOT NULL,
        close TEXT NOT NULL,
        sample_count INTEGER NOT NULL CHECK (sample_count > 0),
        provider TEXT NOT NULL,
        is_derived INTEGER NOT NULL CHECK (is_derived IN (0, 1)),
        source_json TEXT NOT NULL,
        stored_at TEXT NOT NULL,
        PRIMARY KEY (symbol, interval_seconds, bucket_start, provider)
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX aggregate_prices_bucket_idx ON aggregate_prices(interval_seconds, bucket_start)",
    """
    CREATE TABLE latest_snapshots (
        symbol TEXT PRIMARY KEY,
        as_of TEXT NOT NULL,
        price TEXT,
        snapshot_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE dividend_events (
        symbol TEXT NOT NULL,
        ex_date TEXT NOT NULL,
        payment_date TEXT,
        amount TEXT NOT NULL,
        currency TEXT NOT NULL,
        frequency TEXT NOT NULL,
        event_type TEXT NOT NULL,
        is_special INTEGER NOT NULL CHECK (is_special IN (0, 1)),
        provider TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, ex_date, amount, provider)
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX dividend_events_symbol_date_idx ON dividend_events(symbol, ex_date DESC)",
    """
    CREATE TABLE yield_metrics (
        symbol TEXT NOT NULL,
        as_of TEXT NOT NULL,
        annual_percent TEXT NOT NULL,
        method TEXT NOT NULL,
        provider TEXT NOT NULL,
        is_proxy INTEGER NOT NULL CHECK (is_proxy IN (0, 1)),
        source_series TEXT,
        raw_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, as_of, method, provider)
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX yield_metrics_symbol_time_idx ON yield_metrics(symbol, as_of DESC)",
    """
    CREATE TABLE provider_checkpoints (
        provider TEXT NOT NULL,
        feed TEXT NOT NULL,
        checkpoint_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (provider, feed)
    ) STRICT, WITHOUT ROWID
    """,
)


MIGRATION_2: tuple[str, ...] = (
    """
    CREATE TABLE api_keys (
        key_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        key_hash TEXT NOT NULL UNIQUE,
        key_hint TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        revoked_at TEXT,
        origin TEXT NOT NULL CHECK (origin IN ('generated', 'imported', 'legacy'))
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE INDEX api_keys_expiry_idx
    ON api_keys(expires_at)
    WHERE revoked_at IS NULL
    """,
    """
    CREATE TABLE auth_metadata (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE admin_audit_events (
        event_id TEXT PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        request_id TEXT NOT NULL,
        client_ip TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT,
        details_json TEXT NOT NULL
    ) STRICT, WITHOUT ROWID
    """,
    "CREATE INDEX admin_audit_events_time_idx ON admin_audit_events(occurred_at DESC)",
)


MIGRATIONS: dict[int, Sequence[str]] = {1: MIGRATION_1, 2: MIGRATION_2}


def migration_checksum(statements: Sequence[str]) -> str:
    normalized = "\n".join(" ".join(statement.split()) for statement in statements)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def migrate(connection: sqlite3.Connection) -> int:
    """Apply all migrations in one explicit transaction per schema version."""

    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if existing:
        row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
        current = int(row[0])
    else:
        current = 0
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"database schema version {current} is newer than supported version {SCHEMA_VERSION}"
        )

    for version in range(current + 1, SCHEMA_VERSION + 1):
        statements = MIGRATIONS[version]
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version, applied_at, checksum) VALUES (?, ?, ?)",
                (
                    version,
                    datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    migration_checksum(statements),
                ),
            )
            connection.execute(f"PRAGMA user_version = {version}")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    return SCHEMA_VERSION


def verify_schema(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    current = int(row[0])
    if current != SCHEMA_VERSION:
        raise RuntimeError(f"expected schema version {SCHEMA_VERSION}, got {current}")
    for version, statements in MIGRATIONS.items():
        stored = connection.execute(
            "SELECT checksum FROM schema_version WHERE version = ?", (version,)
        ).fetchone()
        if stored is None or stored[0] != migration_checksum(statements):
            raise RuntimeError(f"schema migration {version} checksum mismatch")
    return current
