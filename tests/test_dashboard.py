from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quickprice.api import create_app
from quickprice.dashboard_logs import DashboardLogBroker, DashboardLogCapacityError


def _assert_security_headers(response) -> None:
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert response.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=()"
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "style-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy


@pytest.mark.parametrize("path", ["/dashboard", "/dashboard/"])
def test_dashboard_shell_is_public_and_hardened(client, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["Cache-Control"] == "no-store"
    assert 'href="/dashboard/assets/dashboard.css"' in response.text
    assert 'src="/dashboard/assets/dashboard.js"' in response.text
    assert 'autocomplete="off"' in response.text
    assert 'autocomplete="current-password"' not in response.text
    assert "<canvas" not in response.text.lower()
    assert 'id="fixture-warning"' in response.text
    assert 'role="alert"' in response.text
    assert 'aria-live="assertive"' in response.text
    assert "Non-live test fixture data" in response.text
    assert "not live market data" in response.text
    _assert_security_headers(response)


def test_dashboard_assets_are_public_and_define_the_client_security_contract(client) -> None:
    css = client.get("/dashboard/assets/dashboard.css")
    javascript = client.get("/dashboard/assets/dashboard.js")

    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert ".market-table" in css.text
    assert ".log-list" in css.text
    assert ".fixture-warning" in css.text
    assert javascript.status_code == 200
    assert "javascript" in javascript.headers["content-type"]
    assert "sessionStorage.setItem(SESSION_KEY" in javascript.text
    assert "sessionStorage.removeItem(SESSION_KEY" in javascript.text
    assert "localStorage.setItem(THEME_KEY" in javascript.text
    assert "localStorage.setItem(SESSION_KEY" not in javascript.text
    assert 'fetch("/internal/logs/stream"' in javascript.text
    assert '"X-API-Key"' in javascript.text
    assert 'headers["Last-Event-ID"]' in javascript.text
    assert "chunks(symbols, 100)" in javascript.text
    assert "expandedSymbols: new Set()" in javascript.text
    assert "state.expandedSymbols.clear()" in javascript.text
    assert 'inspect.setAttribute("aria-expanded", String(expansion.expanded))' in javascript.text
    assert "FIXTURE_SOURCE_PATTERN" in javascript.text
    assert "isFixtureSource(quote.source)" in javascript.text
    assert "updateFixtureWarning();" in javascript.text
    assert "source.provider" in javascript.text
    assert "source.feed" in javascript.text
    assert "not live market data" in javascript.text
    assert "JSON.stringify({ instrument, quote, error: item.error }" in javascript.text
    assert "new EventSource" not in javascript.text
    assert "Chart(" not in javascript.text
    _assert_security_headers(css)
    _assert_security_headers(javascript)


def test_dashboard_static_files_are_installed_package_resources() -> None:
    root = files("quickprice.dashboard")
    assert root.joinpath("index.html").is_file()
    assert root.joinpath("assets", "dashboard.css").is_file()
    assert root.joinpath("assets", "dashboard.js").is_file()


def test_log_stream_requires_authentication(client) -> None:
    response = client.get("/internal/logs/stream")

    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "unauthorized"
    _assert_security_headers(response)


def test_log_stream_replays_from_last_event_id_and_sets_sse_headers(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    observed: list[int | None] = []

    async def finite_stream(*, after_id=None, heartbeat_seconds=15.0):
        del heartbeat_seconds
        observed.append(after_id)
        yield 'id: 42\nevent: log\ndata: {"id":42,"message":"ready"}\n\n'

    monkeypatch.setattr(client.app.state.dashboard_logs, "stream", finite_stream)
    response = client.get(
        "/internal/logs/stream",
        headers={**auth_headers, "Last-Event-ID": "41"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["Cache-Control"] == "no-cache, no-transform"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert observed == [41]
    assert response.text.startswith("id: 42\nevent: log\n")
    _assert_security_headers(response)


def test_log_stream_rejects_excess_subscribers_before_streaming(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    def saturated_stream(*, after_id=None, heartbeat_seconds=15.0):
        del after_id, heartbeat_seconds
        raise DashboardLogCapacityError("full")

    monkeypatch.setattr(client.app.state.dashboard_logs, "stream", saturated_stream)
    response = client.get("/internal/logs/stream", headers=auth_headers)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    assert response.json()["errors"][0]["code"] == "log_stream_limit_reached"
    _assert_security_headers(response)


@pytest.mark.parametrize("value", ["not-an-integer", "-1"])
def test_log_stream_rejects_invalid_last_event_id(client, auth_headers, value: str) -> None:
    response = client.get(
        "/internal/logs/stream",
        headers={**auth_headers, "Last-Event-ID": value},
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "invalid_last_event_id"


@pytest.mark.asyncio
async def test_log_broker_redacts_bounds_and_fans_out_from_another_thread() -> None:
    broker = DashboardLogBroker(
        capacity=3,
        client_queue_size=2,
        redacted_values=("configured-secret",),
    )
    subscriber_id, queue = broker.subscribe()

    def publish() -> None:
        for index in range(4):
            broker.emit(
                logging.LogRecord(
                    name="quickprice.test",
                    level=logging.INFO,
                    pathname=__file__,
                    lineno=1,
                    msg=(
                        "token=configured-secret "
                        f"url=https://provider.invalid/data?api_key=remote-{index}"
                    ),
                    args=(),
                    exc_info=None,
                )
            )

    thread = threading.Thread(target=publish)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    await asyncio.sleep(0.01)

    events = broker.snapshot()
    assert [event.id for event in events] == [2, 3, 4]
    assert all("configured-secret" not in event.message for event in events)
    assert all("remote-" not in event.message for event in events)
    assert all("token=[REDACTED]" in event.message for event in events)
    assert all("https://provider.invalid/data?[REDACTED]" in event.message for event in events)
    assert [(await queue.get()).id, (await queue.get()).id] == [3, 4]

    broker.unsubscribe(subscriber_id)
    assert broker.subscriber_count == 0
    replay_id, replay = broker.subscribe(after_id=3)
    assert (await replay.get()).id == 4
    broker.unsubscribe(replay_id)
    broker.close()


@pytest.mark.asyncio
async def test_log_stream_emits_heartbeats_and_releases_subscriber() -> None:
    broker = DashboardLogBroker()
    stream = broker.stream(after_id=999, heartbeat_seconds=0.001)

    heartbeat = await anext(stream)
    assert heartbeat.startswith("event: heartbeat\ndata: ")
    assert '"timestamp":' in heartbeat
    assert broker.subscriber_count == 1

    await stream.aclose()
    assert broker.subscriber_count == 0
    broker.close()


@pytest.mark.asyncio
async def test_log_stream_subscriber_limit_recovers_after_cleanup() -> None:
    broker = DashboardLogBroker(max_subscribers=1)
    first = broker.stream(after_id=999, heartbeat_seconds=60)
    pending_event = asyncio.create_task(anext(first))
    await asyncio.sleep(0)

    assert broker.subscriber_count == 1
    with pytest.raises(DashboardLogCapacityError):
        broker.stream(after_id=999)

    pending_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_event
    assert broker.subscriber_count == 0

    replacement = broker.stream(after_id=999)
    assert broker.subscriber_count == 1
    await replacement.aclose()
    assert broker.subscriber_count == 0
    broker.close()


def test_lifespan_attaches_and_removes_log_handler_and_request_logs_omit_queries(
    settings,
    service,
) -> None:
    app = create_app(settings, service)
    broker = app.state.dashboard_logs
    logger = logging.getLogger("quickprice")
    assert broker not in logger.handlers

    with TestClient(app) as local_client:
        assert broker in logger.handlers
        response = local_client.get("/dashboard?api_key=must-not-appear")
        assert response.status_code == 200
        messages = [event.message for event in broker.snapshot()]
        assert "QuickPrice startup initiated" in messages
        assert "QuickPrice startup complete" in messages
        request_messages = [message for message in messages if message.startswith("HTTP GET")]
        assert any("HTTP GET /dashboard returned 200" in message for message in request_messages)
        assert all("?" not in message for message in request_messages)
        assert all("must-not-appear" not in message for message in messages)

    assert broker not in logger.handlers
    messages = [event.message for event in broker.snapshot()]
    assert "QuickPrice shutdown initiated" in messages
    assert "QuickPrice shutdown complete" in messages


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_dashboard_fixture_source_detection_uses_provider_or_feed() -> None:
    script_path = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    source = script_path.read_text(encoding="utf-8")
    classifier = source[
        source.index("const FIXTURE_SOURCE_PATTERN") : source.index("\n\nconst state =")
    ]
    assertions = """
const cases = [
  [null, false],
  [{ provider: "fixture", feed: "fixture_feed" }, true],
  [{ provider: "fixture_staking", feed: "derived" }, true],
  [{ provider: "binance", feed: "fixture_feed" }, true],
  [{ provider: "binance", feed: "spot" }, false],
  [{ provider: "fixtureless", feed: "spot" }, false],
];
for (const [source, expected] of cases) {
  if (isFixtureSource(source) !== expected) process.exit(1);
}
"""
    result = subprocess.run(
        [shutil.which("node") or "node", "-e", classifier + assertions],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_dashboard_instrument_expansion_persists_independently_by_symbol() -> None:
    script_path = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    source = script_path.read_text(encoding="utf-8")
    helpers = source[
        source.index("function instrumentExpansion") : source.index("\n\nfunction sortValue")
    ]
    assertions = """
const state = { expandedSymbols: new Set() };
function assertExpansion(symbol, expanded, buttonText) {
  const actual = instrumentExpansion(symbol);
  if (actual.expanded !== expanded || actual.buttonText !== buttonText) process.exit(1);
}
assertExpansion("BTC:USDC", false, "Inspect");
setInstrumentExpanded("BTC:USDC", true);
setInstrumentExpanded("ETH:USDC", true);
assertExpansion("BTC:USDC", true, "Close");
assertExpansion("ETH:USDC", true, "Close");
for (const symbol of ["ETH:USDC", "BTC:USDC"]) assertExpansion(symbol, true, "Close");
setInstrumentExpanded("BTC:USDC", false);
assertExpansion("BTC:USDC", false, "Inspect");
assertExpansion("ETH:USDC", true, "Close");
state.expandedSymbols.clear();
assertExpansion("ETH:USDC", false, "Inspect");
"""
    result = subprocess.run(
        [shutil.which("node") or "node", "-e", helpers + assertions],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_dashboard_javascript_has_valid_syntax() -> None:
    script = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    result = subprocess.run(
        [shutil.which("node") or "node", "--check", str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
