from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import threading
from datetime import timedelta
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quickprice.api import create_app
from quickprice.dashboard_logs import DashboardLogBroker, DashboardLogCapacityError
from quickprice.domain import ProviderQuote, YieldMetric
from quickprice.plugin_api import AssetClass, InstrumentPlugin, InstrumentSpec, YieldStrategy
from quickprice.registry import InstrumentRegistry
from quickprice.service import DataUnavailableError, QuickPriceService
from tests.helpers import NOW, seed_complete


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


def test_root_redirects_to_public_dashboard(client) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["Location"] == "/dashboard"
    assert response.headers["Cache-Control"] == "no-store"
    _assert_security_headers(response)

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert dashboard.url.path == "/dashboard"


@pytest.mark.parametrize("path", ["/dashboard", "/dashboard/"])
def test_dashboard_shell_is_public_and_hardened(client, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["Cache-Control"] == "no-store"
    assert 'href="/dashboard/assets/dashboard.css"' in response.text
    assert 'src="/dashboard/assets/dashboard.js"' in response.text
    assert 'autocomplete="off"' in response.text
    assert 'method="post" action="/dashboard"' in response.text
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
    assert "/internal/dashboard/quotes?symbols=" in javascript.text
    assert "expandedSymbols: new Set()" in javascript.text
    assert "state.expandedSymbols.clear()" in javascript.text
    assert 'inspect.setAttribute("aria-expanded", String(expansion.expanded))' in javascript.text
    assert "FIXTURE_SOURCE_PATTERN" in javascript.text
    assert "isFixtureSource(quote.source)" in javascript.text
    assert "updateFixtureWarning();" in javascript.text
    assert "source.provider" in javascript.text
    assert "source.feed" in javascript.text
    assert "Yield is proxy" in javascript.text
    assert 'label.classList.add("is-proxy")' in javascript.text
    assert "not live market data" in javascript.text
    assert "JSON.stringify({ instrument, quote, error: item.error }" in javascript.text
    assert "availability-reason" in javascript.text
    assert "new EventSource" not in javascript.text
    assert "Chart(" not in javascript.text
    _assert_security_headers(css)
    _assert_security_headers(javascript)


def test_dashboard_income_help_labels_align_with_the_numeric_column(client) -> None:
    css = client.get("/dashboard/assets/dashboard.css")

    assert css.status_code == 200
    rule = re.search(r"\.income-label\.has-help\s*\{(?P<body>[^}]*)\}", css.text)
    assert rule is not None
    assert re.search(r"margin-left\s*:\s*auto\s*;", rule.group("body"))


def test_dashboard_quote_projection_keeps_price_with_exact_metadata_error(
    settings,
    auth_headers,
) -> None:
    service = QuickPriceService(settings)
    seed_complete(service, missing={"ETH:USDC"})
    aapl_quote = service._last_quotes["AAPL:USD"]
    service._dividends.pop("AAPL:USD")
    service.publish_quote(aapl_quote, persist=False)

    with TestClient(create_app(settings, service)) as local_client:
        unauthorized = local_client.get("/internal/dashboard/quotes")
        public = local_client.get("/v1/quotes/AAPL:USD", headers=auth_headers)
        dashboard = local_client.get(
            "/internal/dashboard/quotes?symbols=AAPL:USD,ETH:USDC",
            headers=auth_headers,
        )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["errors"][0]["code"] == "unauthorized"
    assert public.status_code == 503
    assert public.json()["errors"][0]["message"] == (
        "required latest regular dividend is unavailable"
    )
    assert dashboard.status_code == 200
    body = dashboard.json()
    assert body["partial"] is True
    assert [(item["symbol"], item["price"]) for item in body["data"]] == [("AAPL:USD", 225.0)]
    assert body["data"][0]["dividend"] is None
    assert {(item["symbol"], item["message"]) for item in body["errors"]} == {
        ("AAPL:USD", "required latest regular dividend is unavailable"),
        ("ETH:USDC", "no valid price has ever been received"),
    }
    _assert_security_headers(dashboard)


def test_best_effort_quote_projection_preserves_dynamic_stale_yield_quality(settings) -> None:
    symbol = "MIXED:USD"
    registry = InstrumentRegistry(
        (
            InstrumentPlugin(
                plugin_id="mixed-income-test",
                version="1",
                instruments=(
                    InstrumentSpec(
                        symbol=symbol,
                        base="MIXED",
                        quote="USD",
                        name="Mixed Income Test Fund",
                        description="Test instrument requiring both yield and dividend metadata.",
                        asset_class=AssetClass.BOND,
                        asset_type="income_bond_etf",
                        price_basis="last_trade",
                        yield_strategy=YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE,
                        dividend_strategy="latest_regular_cash_annualized_x4",
                    ),
                ),
                provider_installer=lambda _: None,
            ),
        )
    )
    service = QuickPriceService(settings, registry)
    service.publish_yield_metric(
        YieldMetric(
            symbol=symbol,
            value="4.25",
            as_of=NOW,
            method="DGS3MO",
            provider="fred",
            is_proxy=True,
        ),
        persist=False,
    )
    service.publish_quote(
        ProviderQuote(
            symbol=symbol,
            price="100",
            as_of=NOW,
            provider="fixture",
            feed="fixture",
            market_status="open",
        ),
        persist=False,
    )
    projected_at = NOW + timedelta(days=8)

    with pytest.raises(
        DataUnavailableError,
        match="required latest regular dividend is unavailable",
    ):
        service.get_quote(symbol, now=projected_at)
    quote = service.get_quote(
        symbol,
        now=projected_at,
        require_complete_metadata=False,
    )

    assert quote.dividend is None
    assert quote.quality.stale is True
    assert quote.quality.staleness_ms == 8 * 24 * 60 * 60 * 1000
    assert quote.estimated_annual_yield is not None
    assert quote.estimated_annual_yield.quality.stale is True
    assert quote.estimated_annual_yield.quality.staleness_ms == 8 * 24 * 60 * 60 * 1000
    assert quote.estimated_annual_yield.quality.stale_after_seconds == 7 * 24 * 60 * 60


def test_dashboard_static_files_are_installed_package_resources() -> None:
    root = files("quickprice.dashboard")
    assert root.joinpath("index.html").is_file()
    assert root.joinpath("assets", "dashboard.css").is_file()
    assert root.joinpath("assets", "dashboard.js").is_file()


def test_dashboard_sort_controls_offer_all_fields_and_accessible_headers(client) -> None:
    response = client.get("/dashboard")
    expected_fields = [
        "instrument",
        "price",
        "1h",
        "4h",
        "1d",
        "1w",
        "1m",
        "1y",
        "income",
        "market",
        "source",
    ]

    select = response.text.split('<select id="sort-field">', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'<option value="([^"]+)">', select) == expected_fields

    header_fields = re.findall(
        r'<button class="sort-header(?: is-active)?" type="button" '
        r'data-sort-field="([^"]+)">',
        response.text,
    )
    assert header_fields == expected_fields
    assert response.text.count('aria-sort="ascending"') == 1
    assert 'aria-sort="descending"' not in response.text
    assert (
        '<th scope="col" aria-sort="ascending"><button class="sort-header is-active" '
        'type="button" data-sort-field="instrument">'
    ) in response.text
    assert response.text.count('data-sort-indicator aria-hidden="true"') == len(expected_fields)


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
def test_dashboard_availability_distinguishes_incomplete_and_missing_prices() -> None:
    script_path = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    source = script_path.read_text(encoding="utf-8")
    helpers = source[
        source.index("function textValue") : source.index("\n\nfunction changePercent")
    ]
    assertions = r"""
function assertAvailability(item, kind, label, reason, code) {
  const actual = availability(item);
  if (
    actual.kind !== kind
    || actual.label !== label
    || actual.reason !== reason
    || actual.code !== code
  ) {
    throw new Error(`unexpected availability: ${JSON.stringify(actual)}`);
  }
}

assertAvailability(
  { quote: { price: 225 }, error: null },
  "available", "Available", null, null,
);
assertAvailability(
  {
    quote: { price: 225 },
    error: { code: "data_unavailable", message: "required dividend is unavailable" },
  },
  "incomplete", "Metadata incomplete", "required dividend is unavailable", "data_unavailable",
);
assertAvailability(
  {
    quote: null,
    error: { code: "data_unavailable", message: "no valid price has ever been received" },
  },
  "unavailable", "Price unavailable", "no valid price has ever been received", "data_unavailable",
);
assertAvailability(
  { quote: null, error: null },
  "unavailable", "Price unavailable", "No valid price snapshot is available.", "data_unavailable",
);
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
def test_dashboard_yield_labels_distinguish_official_estimates_and_proxies() -> None:
    script_path = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    source = script_path.read_text(encoding="utf-8")
    text_value = source[
        source.index("function textValue") : source.index("\n\nfunction availability")
    ]
    yield_label = source[
        source.index("function yieldLabel") : source.index("\n\nfunction incomeCell")
    ]
    assertions = r"""
const cases = [
  [{ rate_type: "apr", is_proxy: false, is_estimate: false }, "APR"],
  [{ rate_type: "apy", is_proxy: false, is_estimate: true }, "Estimated APY"],
  [{ rate_type: "apy", is_proxy: true, is_estimate: true }, "Proxy APY"],
  [{ method: "custom" }, "YIELD"],
];
for (const [value, expected] of cases) {
  const actual = yieldLabel(value);
  if (actual !== expected) throw new Error(`expected ${expected}, got ${actual}`);
}
"""
    result = subprocess.run(
        [shutil.which("node") or "node", "-e", text_value + yield_label + assertions],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_dashboard_income_percentage_does_not_use_a_price_change_sign() -> None:
    script_path = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    source = script_path.read_text(encoding="utf-8")
    helper = source[
        source.index("function incomePercent") : source.index("\n\nfunction changeClass")
    ]
    assertions = r"""
const cases = [
  [2.41868, "2.42%"],
  [0, "0.00%"],
  [-0.25, "-0.25%"],
  [Number.NaN, "-"],
];
for (const [value, expected] of cases) {
  const actual = incomePercent(value);
  if (actual !== expected) throw new Error(`expected ${expected}, got ${actual}`);
}
"""
    result = subprocess.run(
        [shutil.which("node") or "node", "-e", helper + assertions],
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
def test_dashboard_sorting_supports_all_fields_with_deterministic_missing_values() -> None:
    script_path = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    source = script_path.read_text(encoding="utf-8")
    helpers = source[
        source.index("function finiteNumber") : source.index("\n\nfunction updateSortControls")
    ]
    assertions = r"""
function assertEqual(actual, expected, label) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function row(symbol, quote = {}) {
  return { instrument: { symbol }, quote };
}

function order(items, field, ascending) {
  return [...items]
    .sort((left, right) => compareRows(left, right, field, ascending))
    .map((item) => item.instrument.symbol);
}

const expectedFields = [
  "instrument", "price", "1h", "4h", "1d", "1w", "1m", "1y", "income", "market", "source",
];
assertEqual(Object.keys(SORT_FIELDS), expectedFields, "sort field registry");

const instruments = [row("ZETA"), row("ALPHA"), row("BETA")];
assertEqual(order(instruments, "instrument", true), ["ALPHA", "BETA", "ZETA"], "instrument asc");
assertEqual(order(instruments, "instrument", false), ["ZETA", "BETA", "ALPHA"], "instrument desc");

const prices = [
  row("HIGH", { price: 5 }),
  row("MISSING"),
  row("LOW", { price: -2 }),
  row("ZERO", { price: 0 }),
  row("INVALID", { price: Number.POSITIVE_INFINITY }),
];
assertEqual(
  order(prices, "price", true),
  ["LOW", "ZERO", "HIGH", "INVALID", "MISSING"],
  "price asc with missing last",
);
assertEqual(
  order(prices, "price", false),
  ["HIGH", "ZERO", "LOW", "INVALID", "MISSING"],
  "price desc with missing last",
);

for (const field of ["1h", "4h", "1d", "1w", "1m", "1y"]) {
  const windowName = field === "1m" ? "1mo" : field;
  const changes = (value) => ({
    [windowName]: { percent: value },
    ...(field === "1m" ? { "1m": { percent: -value } } : {}),
  });
  const items = [
    row("HIGH", { changes: changes(5) }),
    row("MISSING", { changes: {} }),
    row("LOW", { changes: changes(-2) }),
    row("ZERO", { changes: changes(0) }),
    row("INVALID", { changes: changes(Number.NaN) }),
  ];
  assertEqual(
    order(items, field, true),
    ["LOW", "ZERO", "HIGH", "INVALID", "MISSING"],
    `${field} asc with missing last`,
  );
  assertEqual(
    order(items, field, false),
    ["HIGH", "ZERO", "LOW", "INVALID", "MISSING"],
    `${field} desc with missing last`,
  );
}

const income = [
  row("DIVIDEND", { dividend: { yield_percent: 5 } }),
  row("ANNUAL", { estimated_annual_yield: { percent: 3 } }),
  row("BOTH", {
    dividend: { yield_percent: 1 },
    estimated_annual_yield: { percent: 9 },
  }),
  row("ZERO", { estimated_annual_yield: { percent: 0 } }),
  row("INVALID", {
    dividend: { yield_percent: Number.NaN },
    estimated_annual_yield: { percent: 8 },
  }),
  row("MISSING"),
];
assertEqual(
  order(income, "income", true),
  ["ZERO", "BOTH", "ANNUAL", "DIVIDEND", "INVALID", "MISSING"],
  "income asc with visible dividend precedence and missing last",
);
assertEqual(
  order(income, "income", false),
  ["DIVIDEND", "ANNUAL", "BOTH", "ZERO", "INVALID", "MISSING"],
  "income desc with visible dividend precedence and missing last",
);

const markets = [
  row("UNKNOWN", { market_status: "unknown", as_of: "2026-07-20T08:00:00Z" }),
  row("OPEN_OLD", { market_status: "open", as_of: "2026-07-20T07:00:00Z" }),
  row("UNAVAILABLE", null),
  row("CLOSED", { market_status: "closed", as_of: "2026-07-20T08:00:00Z" }),
  row("OPEN_NEW", { market_status: "open", as_of: "2026-07-20T09:00:00Z" }),
];
assertEqual(
  order(markets, "market", true),
  ["OPEN_NEW", "OPEN_OLD", "CLOSED", "UNKNOWN", "UNAVAILABLE"],
  "market asc",
);
assertEqual(
  order(markets, "market", false),
  ["UNAVAILABLE", "UNKNOWN", "CLOSED", "OPEN_NEW", "OPEN_OLD"],
  "market desc",
);

const sources = [
  row("ALPHA_FALLBACK", {
    source: { provider: "Alpha", feed: "iex", fallback_level: 2 }, quality: { stale: false },
  }),
  row("BETA", {
    source: { provider: "Beta", feed: "spot", fallback_level: 0 }, quality: { stale: false },
  }),
  row("MISSING", { source: { feed: "unknown" } }),
  row("ALPHA_STALE", {
    source: { provider: "Alpha", feed: "iex", fallback_level: 0 }, quality: { stale: true },
  }),
  row("ALPHA_CURRENT", {
    source: { provider: "Alpha", feed: "iex", fallback_level: 0 }, quality: { stale: false },
  }),
  row("ALPHA_SIP", {
    source: { provider: "Alpha", feed: "sip", fallback_level: 0 }, quality: { stale: false },
  }),
];
const alphaQualityOrder = ["ALPHA_CURRENT", "ALPHA_STALE", "ALPHA_FALLBACK"];
assertEqual(
  order(sources, "source", true),
  [...alphaQualityOrder, "ALPHA_SIP", "BETA", "MISSING"],
  "source asc",
);
assertEqual(
  order(sources, "source", false),
  ["BETA", "ALPHA_SIP", ...alphaQualityOrder, "MISSING"],
  "source desc",
);

const tied = [row("BETA", { price: 10 }), row("ALPHA", { price: 10 })];
assertEqual(order(tied, "price", true), ["ALPHA", "BETA"], "ascending tie breaker");
assertEqual(order(tied, "price", false), ["ALPHA", "BETA"], "descending tie breaker");
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
def test_dashboard_refresh_reapplies_active_sort_after_live_values_change() -> None:
    script_path = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    source = script_path.read_text(encoding="utf-8")
    rows_helper = source[
        source.index("function rows()") : source.index("\n\nfunction instrumentExpansion")
    ]
    sort_helpers = source[
        source.index("function finiteNumber") : source.index("\n\nfunction updateSortControls")
    ]
    visible_rows_helper = source[
        source.index("function visibleRows") : source.index("\n\nfunction metadataItem")
    ]
    refresh_helper = source[
        source.index("async function refreshQuotes") : source.index("\n\nasync function connect")
    ]
    assertions = r"""
const instruments = [
  { symbol: "ALPHA", name: "Alpha", description: "Alpha", asset_class: "equity", asset_type: "stock" },
  { symbol: "BETA", name: "Beta", description: "Beta", asset_class: "equity", asset_type: "stock" },
];
const state = {
  apiKey: "test-key",
  refreshing: false,
  instruments,
  quotes: new Map(),
  quoteErrors: new Map(),
  sortField: "price",
  ascending: true,
};
const ui = {
  search: { value: "" },
  assetFilter: { value: "" },
  statusFilter: { value: "" },
  refreshMarket: { disabled: false },
  lastRefresh: { textContent: "" },
  marketNotice: {},
  connectionBadge: {},
};
const quote = (symbol, price, oneHour) => ({
  symbol,
  price,
  changes: { "1h": { percent: oneHour } },
  market_status: "open",
  source: { provider: "fixture", feed: "fixture" },
  quality: { stale: false },
});
const responses = [
  { data: [quote("ALPHA", 1, 0), quote("BETA", 2, 0)], errors: [] },
  { data: [quote("ALPHA", 3, 0), quote("BETA", 2, 0)], errors: [] },
  { data: [quote("ALPHA", 3, -1), quote("BETA", 2, 2)], errors: [] },
  { data: [quote("ALPHA", 3, 4), quote("BETA", 2, 2)], errors: [] },
];
const renderedOrders = [];

function chunks(values) { return [values]; }
async function apiJson() { return responses.shift(); }
function dateTime(value) { return value; }
function setNotice() {}
function setBadge() {}
function updateFixtureWarning() {}
function updateSummary() {}
function handleConnectionError(error) { throw error; }
function renderMarket() {
  renderedOrders.push(visibleRows().map((item) => item.instrument.symbol));
}
function assertOrder(index, expected, label) {
  const actual = renderedOrders[index];
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

(async () => {
  await refreshQuotes();
  await refreshQuotes();
  state.sortField = "1h";
  await refreshQuotes();
  await refreshQuotes();

  assertOrder(0, ["ALPHA", "BETA"], "initial price order");
  assertOrder(1, ["BETA", "ALPHA"], "updated price order");
  assertOrder(2, ["ALPHA", "BETA"], "initial change order");
  assertOrder(3, ["BETA", "ALPHA"], "updated change order");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            "-e",
            rows_helper + sort_helpers + visible_rows_helper + refresh_helper + assertions,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_dashboard_sort_header_accessibility_tracks_the_active_sort() -> None:
    script_path = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    source = script_path.read_text(encoding="utf-8")
    helper = source[
        source.index("function updateSortControls") : source.index("\n\nfunction selectSortField")
    ]
    assertions = r"""
function assertCondition(condition, label) {
  if (!condition) throw new Error(label);
}

function sortHeader(field) {
  const attributes = new Map();
  const indicator = { textContent: "stale" };
  const tableHeader = {
    setAttribute(name, value) { attributes.set(name, value); },
    removeAttribute(name) { attributes.delete(name); },
  };
  return {
    dataset: { sortField: field },
    classList: { toggle(_name, active) { this.active = active; } },
    closest(selector) { return selector === "th" ? tableHeader : null; },
    querySelector(selector) { return selector === "[data-sort-indicator]" ? indicator : null; },
    attributes,
    indicator,
  };
}

const fields = [
  "instrument", "price", "1h", "4h", "1d", "1w", "1m", "1y", "income", "market", "source",
];
const headers = fields.map(sortHeader);
const ui = {
  sortField: { value: "" },
  sortDirection: {
    textContent: "",
    attributes: new Map(),
    setAttribute(name, value) { this.attributes.set(name, value); },
  },
  sortHeaders: headers,
};
const state = { sortField: "1m", ascending: false };

updateSortControls();
assertCondition(ui.sortField.value === "1m", "select is synchronized");
assertCondition(ui.sortDirection.textContent === "Descending", "direction is synchronized");
assertCondition(
  ui.sortDirection.attributes.get("aria-label") ===
    "Sort ascending; current order descending",
  "direction control describes current and next order",
);
for (const header of headers) {
  const active = header.dataset.sortField === "1m";
  assertCondition(header.classList.active === active, `${header.dataset.sortField} active class`);
  assertCondition(
    header.attributes.get("aria-sort") === (active ? "descending" : undefined),
    `${header.dataset.sortField} aria-sort`,
  );
  assertCondition(
    header.indicator.textContent === (active ? "\u2193" : ""),
    `${header.dataset.sortField} indicator`,
  );
}
assertCondition(
  headers.filter((header) => header.attributes.has("aria-sort")).length === 1,
  "exactly one header owns aria-sort",
);
"""
    result = subprocess.run(
        [shutil.which("node") or "node", "-e", helper + assertions],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_dashboard_sort_selection_resets_new_fields_and_preserves_expansion() -> None:
    script_path = (
        Path(__file__).parents[1] / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"
    )
    source = script_path.read_text(encoding="utf-8")
    helper = source[
        source.index("function selectSortField") : source.index("\n\nfunction visibleRows")
    ]
    assertions = r"""
function assertCondition(condition, label) {
  if (!condition) throw new Error(label);
}

const SORT_FIELDS = { instrument: {}, price: {}, income: {}, market: {} };
const state = {
  sortField: "instrument",
  ascending: false,
  expandedSymbols: new Set(["BTC:USDC", "ETH:USDC"]),
};
let controlUpdates = 0;
let renders = 0;
function updateSortControls() { controlUpdates += 1; }
function renderMarket() { renders += 1; }

selectSortField("price");
assertCondition(state.sortField === "price", "new field is selected");
assertCondition(state.ascending === true, "new field starts ascending");
selectSortField("price", { toggleIfActive: true });
assertCondition(state.ascending === false, "active header toggles direction");
selectSortField("income", { toggleIfActive: true });
assertCondition(state.sortField === "income" && state.ascending, "income starts ascending");
selectSortField("market", { toggleIfActive: true });
assertCondition(state.sortField === "market" && state.ascending, "new header starts ascending");
selectSortField("unsupported", { toggleIfActive: true });
assertCondition(controlUpdates === 4 && renders === 4, "unsupported fields are ignored");
assertCondition(
  [...state.expandedSymbols].join() === "BTC:USDC,ETH:USDC",
  "sorting preserves expanded instruments",
);
"""
    result = subprocess.run(
        [shutil.which("node") or "node", "-e", helper + assertions],
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
