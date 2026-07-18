#!/usr/bin/env python3
"""Create a consistent online backup of the QuickPrice SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("backups"))
    args = parser.parse_args()

    source_path = args.database.expanduser().resolve(strict=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination_path = (args.output_dir / f"quickprice-{timestamp}.sqlite3").resolve()
    if destination_path.exists():
        parser.error(f"destination already exists: {destination_path}")

    source_uri = f"file:{source_path.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as source:
        with sqlite3.connect(destination_path, timeout=30) as destination:
            source.backup(destination, pages=1000, sleep=0.05)
            result = destination.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                destination_path.unlink(missing_ok=True)
                raise RuntimeError(f"backup integrity_check failed: {result!r}")

    destination_path.chmod(0o600)
    print(destination_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
