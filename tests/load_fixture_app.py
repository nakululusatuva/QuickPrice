"""Authenticated, fully populated ASGI fixture used only by local load tests."""

from pathlib import Path

from quickprice.api import create_app
from quickprice.auth import hash_api_key
from quickprice.config import Settings
from quickprice.service import QuickPriceService
from tests.helpers import API_KEY, seed_complete

settings = Settings(
    production=True,
    require_free_threaded=False,
    background_enabled=False,
    database_path=Path("data/load-fixture.db"),
    api_key_hashes=(hash_api_key(API_KEY),),
    rate_limit_enabled=False,
)
service = QuickPriceService(settings)
seed_complete(service)
app = create_app(settings, service)
