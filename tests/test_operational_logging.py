from __future__ import annotations

import asyncio
import logging

import pytest

from quickprice.config import Settings
from quickprice.service import QuickPriceService


@pytest.mark.asyncio
async def test_storage_startup_failure_is_logged_without_exception_text(
    tmp_path, monkeypatch, caplog
) -> None:
    secret = "storage-api-key=must-not-appear"

    class BrokenStorage:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def start(self) -> None:
            raise RuntimeError(secret)

    monkeypatch.setattr("quickprice.storage.SQLiteStorage", BrokenStorage)
    service = QuickPriceService(
        Settings(
            background_enabled=False,
            require_free_threaded=False,
            database_path=tmp_path / "broken.db",
        )
    )

    with caplog.at_level(logging.ERROR, logger="quickprice.service"):
        await service._start_storage()

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["Storage startup failed stage=start error_type=RuntimeError"]
    assert secret not in caplog.text
    assert service._storage_ready is False


@pytest.mark.asyncio
async def test_collector_startup_failure_is_logged_without_exception_text(
    monkeypatch, caplog
) -> None:
    secret = "provider-token=must-not-appear"

    class BrokenCoordinator:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def start(self) -> None:
            raise RuntimeError(secret)

    monkeypatch.setattr("quickprice.collectors.MarketDataCoordinator", BrokenCoordinator)
    service = QuickPriceService(
        Settings(
            background_enabled=True,
            require_free_threaded=False,
        )
    )

    with caplog.at_level(logging.ERROR, logger="quickprice.service"):
        await service._start_collectors()

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["Collector startup failed error_type=RuntimeError"]
    assert secret not in caplog.text
    assert service._coordinator is None
    assert isinstance(service._collector_start_error, RuntimeError)


@pytest.mark.asyncio
async def test_collector_runtime_failure_is_logged_once_without_exception_text(caplog) -> None:
    secret = "collector-secret=must-not-appear"

    class FailedCoordinator:
        def __init__(self) -> None:
            self.fatal_error = RuntimeError(secret)
            self._supervisor = None

    coordinator = FailedCoordinator()

    async def completed_supervisor() -> None:
        return None

    coordinator._supervisor = asyncio.create_task(completed_supervisor())

    with caplog.at_level(logging.ERROR, logger="quickprice.service"):
        await QuickPriceService._monitor_collector_run(coordinator)

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["Collector runtime failed error_type=RuntimeError"]
    assert secret not in caplog.text
