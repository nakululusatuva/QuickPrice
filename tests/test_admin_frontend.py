from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
ADMIN_ROOT = ROOT / "src" / "quickprice" / "admin"
ADMIN_HTML = ADMIN_ROOT / "index.html"
ADMIN_CSS = ADMIN_ROOT / "assets" / "admin.css"
ADMIN_JS = ADMIN_ROOT / "assets" / "admin.js"
DASHBOARD_JS = ROOT / "src" / "quickprice" / "dashboard" / "assets" / "dashboard.js"


class _AdminShellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts = 0
        self.inline_styles = 0
        self.panel_ids: set[str] = set()
        self.forms: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and not values.get("src"):
            self.inline_scripts += 1
        if tag == "style" or "style" in values:
            self.inline_styles += 1
        if values.get("role") == "tabpanel" and values.get("id"):
            self.panel_ids.add(values["id"] or "")
        if tag == "form" and values.get("id"):
            self.forms[values["id"] or ""] = values


def test_admin_shell_is_separate_and_csp_compatible() -> None:
    source = ADMIN_HTML.read_text(encoding="utf-8")
    parser = _AdminShellParser()
    parser.feed(source)

    assert parser.inline_scripts == 0
    assert parser.inline_styles == 0
    assert parser.panel_ids == {
        "api-keys-panel",
        "provider-keys-panel",
        "instruments-panel",
        "configuration-panel",
        "provider-statistics-panel",
    }
    assert 'href="/dashboard"' in source
    assert 'href="/admin/assets/admin.css"' in source
    assert 'src="/admin/assets/admin.js"' in source
    assert "Administrator verification" in source
    assert 'id="admin-key" type="password" autocomplete="off"' in source
    assert 'id="admin-key" name=' not in source
    assert 'id="totp" name=' not in source
    assert "Write-only secret handling" in source
    assert "Trusted definitions only" in source
    for form_id in ("login-form", "configuration-form", "create-key-form", "import-keys-form"):
        assert parser.forms[form_id]["method"] == "post"
        assert parser.forms[form_id]["action"] == "/admin"


def test_admin_client_does_not_persist_credentials_or_render_untrusted_html() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    assert "sessionStorage" not in source
    assert "localStorage.setItem(THEME_KEY" in source
    assert 'localStorage.setItem("admin' not in source
    assert ".innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert 'credentials: "same-origin"' in source
    assert 'headers["X-CSRF-Token"] = state.csrfToken' in source
    assert "state.csrfToken = null" in source
    assert 'ui.revealedApiKey.textContent = ""' in source
    assert 'input.autocomplete = "off"' in source
    assert "rawKeyFromResponse" in source


@pytest.mark.parametrize(
    "path",
    [
        '"/session"',
        '"/api-keys"',
        '"/api-keys/import"',
        '"/configuration"',
        '"/provider-keys"',
        '"/instruments"',
        '"/provider-statistics"',
        '"/audit-events"',
    ],
)
def test_admin_client_declares_expected_api_contract(path: str) -> None:
    assert path in ADMIN_JS.read_text(encoding="utf-8")


def test_admin_styles_cover_responsive_and_accessible_states() -> None:
    source = ADMIN_CSS.read_text(encoding="utf-8")

    assert ':root[data-theme="dark"]' in source
    assert "@media (max-width: 700px)" in source
    assert "@media (prefers-reduced-motion: reduce)" in source
    assert ":focus-visible" in source
    assert ".status-pill.is-negative" in source
    assert ".security-banner" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_admin_javascript_has_valid_syntax() -> None:
    result = subprocess.run(
        [shutil.which("node") or "node", "--check", str(ADMIN_JS)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_income_hover_explains_distribution_proxy_and_staking_mechanics() -> None:
    source = DASHBOARD_JS.read_text(encoding="utf-8")
    helpers = source[
        source.index("function dividendExplanation") : source.index("\n\nfunction incomeCell")
    ]
    assertions = r"""
function textValue(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}
function includes(actual, expected) {
  if (!actual.includes(expected)) throw new Error(`missing ${expected}: ${actual}`);
}
includes(dividendExplanation({ frequency: "quarterly" }), "latest regular cash dividend");
includes(
  yieldExplanation({ rate_type: "apy", method: "latest_distribution_annualized" }),
  "repeats the latest regular cash distribution",
);
includes(
  yieldExplanation({ rate_type: "apr", method: "treasury_3m_proxy_minus_expense", is_proxy: true }),
  "three-month US Treasury yield minus fund expenses",
);
includes(
  yieldExplanation({ rate_type: "apy", method: "official", accrual_mode: "rebasing_balance" }),
  "increasing the number of tokens held",
);
includes(
  yieldExplanation({ rate_type: "apy", method: "exchange_ratio_growth", observation_window_days: 30 }),
  "Short observation windows can produce unstable estimates",
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
def test_provider_statistics_keep_percent_units_and_surfaces_separate() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    payload_helpers = source[
        source.index("function nestedPayload") : source.index("\n\nfunction errorMessage")
    ]
    number_helpers = source[
        source.index("function compactNumber") : source.index("\n\nfunction toLocalDateTimeValue")
    ]
    provider_helpers = source[
        source.index("function normalizeProviders") : source.index(
            "\n\nasync function loadStatistics"
        )
    ]
    assertions = r"""
if (percent(1) !== "1.0%") throw new Error(`one percent rendered as ${percent(1)}`);
if (latency(null) !== "-") throw new Error(`null latency rendered as ${latency(null)}`);
const rows = normalizeProviders({providers: {sample: {
  operations: {lifetime: {attempts: 10, successful: 1, success_rate: 10,
    last_outcome: "unavailable", last_attempt_at: "2026-07-21T01:00:00Z",
    latency_ms: {p50: 20, p95: 30, percentile_sample_size: 10}}},
  upstream_http: {lifetime: {attempts: 12, successful: 12, success_rate: 100,
    last_outcome: "success", last_attempt_at: "2026-07-21T00:59:00Z",
    latency_ms: {p50: 5, p95: 9, percentile_sample_size: 12}}},
  status: {fallbacks: 2, circuit_state: "closed", websocket_reconnects: 3,
    quota: {tracked: false}},
}}});
if (rows.length !== 1) throw new Error(`expected one provider row, got ${rows.length}`);
if (providerRequests(rows[0]) !== 10) throw new Error("HTTP calls were double counted");
if (providerSuccessRate(rows[0]) !== 10) throw new Error("percent unit changed");
if (providerFallbacks(rows[0]) !== 2) throw new Error("fallbacks were duplicated");
if (rows[0].last_status !== "unavailable") throw new Error("last outcome is not chronological");
"""
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            "-e",
            payload_helpers + number_helpers + provider_helpers + assertions,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
