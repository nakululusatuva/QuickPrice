from __future__ import annotations

import sys

from quickprice import __main__
from quickprice.config import Settings


def test_serve_disables_uvicorn_implicit_proxy_header_trust(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def run(application: str, **kwargs) -> None:
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setattr(sys, "argv", ["quickprice", "serve"])
    monkeypatch.setattr(
        __main__.Settings,
        "from_env",
        classmethod(
            lambda _cls: Settings(
                production=False,
                require_free_threaded=False,
                background_enabled=False,
            )
        ),
    )
    monkeypatch.setattr(__main__.uvicorn, "run", run)

    __main__.main()

    assert captured["application"] == "quickprice.api:app"
    assert captured["proxy_headers"] is False
    assert captured["workers"] == 1
    assert captured["http"] == "h11"
