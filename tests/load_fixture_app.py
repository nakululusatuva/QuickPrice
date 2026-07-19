"""Authenticated, fully populated ASGI fixture used only by local load tests."""

import os
from pathlib import Path

from quickprice.api import create_app
from quickprice.auth import hash_api_key
from quickprice.config import Settings
from quickprice.service import QuickPriceService
from tests.helpers import API_KEY, seed_complete

admin_origin = os.getenv("QUICKPRICE_ADMIN_ORIGIN")

settings = Settings(
    production=not bool(admin_origin),
    require_free_threaded=False,
    background_enabled=False,
    database_path=Path("data/load-fixture.db"),
    api_key_hashes=(hash_api_key(API_KEY),),
    rate_limit_enabled=False,
    admin_username=os.getenv("QUICKPRICE_ADMIN_USERNAME"),
    admin_password_verifier=os.getenv("QUICKPRICE_ADMIN_PASSWORD_VERIFIER"),
    admin_password_change_required=(
        os.getenv("QUICKPRICE_ADMIN_PASSWORD_CHANGE_REQUIRED", "true").lower() == "true"
    ),
    admin_totp_secret=os.getenv("QUICKPRICE_ADMIN_TOTP_SECRET"),
    admin_origin=admin_origin,
    admin_require_https=False,
    managed_config_path=Path("data/preview-config/quickprice.env"),
    managed_provider_keys_path=Path("data/preview-config/provider-keys.env"),
    managed_admin_account_path=Path("data/preview-config/admin-account.json"),
    managed_instruments_path=Path("data/preview-config/instruments.json"),
)
service = QuickPriceService(settings)
seed_complete(service)
app = create_app(settings, service)
