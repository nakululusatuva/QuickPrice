from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quickprice.api import create_app
from quickprice.auth import hash_api_key
from quickprice.config import Settings
from quickprice.service import QuickPriceService
from tests.helpers import API_KEY, seed_complete


@pytest.fixture
def settings(tmp_path):
    return Settings(
        production=False,
        require_free_threaded=False,
        background_enabled=False,
        database_path=tmp_path / "quickprice.db",
        api_key_hashes=(hash_api_key(API_KEY),),
        rate_limit_enabled=False,
    )


@pytest.fixture
def service(settings):
    result = QuickPriceService(settings)
    seed_complete(result)
    return result


@pytest.fixture
def client(settings, service):
    with TestClient(create_app(settings, service)) as value:
        yield value


@pytest.fixture
def auth_headers():
    return {"X-API-Key": API_KEY}
