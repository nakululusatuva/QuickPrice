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
    for form_id in (
        "login-form",
        "configuration-form",
        "create-key-form",
        "import-keys-form",
        "instrument-form",
        "import-instrument-form",
    ):
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
    assert "CATALOG_JOB_KEY" in source
    assert "adminRequest(path, { csrf: true })" in source


@pytest.mark.parametrize(
    "path",
    [
        '"/session"',
        '"/api-keys"',
        '"/api-keys/import"',
        '"/configuration"',
        '"/provider-keys"',
        '"/instrument-catalog"',
        '"/instrument-catalog/instruments"',
        '"/instrument-catalog/import"',
        '"/instrument-catalog/validate"',
        '"/instrument-catalog/activate"',
        '"/instrument-catalog/rollback"',
        '"/provider-catalog"',
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
    assert ".catalog-status" in source
    assert ".provider-route-grid" in source
    assert ".capability-chip" in source
    assert ".search-results li.is-incompatible" in source


def test_instrument_catalog_ui_preserves_the_data_only_security_boundary() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    javascript = ADMIN_JS.read_text(encoding="utf-8")

    assert "Only built-in providers and their fixed upstream hosts are available" in html
    assert "cannot contain URLs, request headers, credentials, module paths, Python" in html
    assert 'id="instrument-provider-symbols"' in html
    assert 'id="instrument-yield-strategy"' in html
    assert (
        '<option value="treasury_proxy_minus_expense">'
        "Treasury proxy minus expense (select maturity)</option>" in html
    )
    assert (
        '<option value="treasury_3m_proxy_minus_expense">'
        "Legacy 3-month Treasury proxy minus expense (DGS3MO only)</option>" in html
    )
    assert 'id="instrument-fred-series" aria-describedby="instrument-fred-series-help"' in html
    assert (
        'id="instrument-dividend-strategy"><option value="">None</option>'
        '<option value="latest_regular_cash_annualized_x4">' in html
    )
    assert 'latest_regular_cash_annualized"' not in html
    assert 'id="instrument-synthetic-operation"' in html
    assert 'value="replace_custom"' in html
    assert "providerSymbolsFromForm" in javascript
    assert "routesFromForm" in javascript
    assert ".innerHTML" not in javascript
    assert 'id="catalog-job-status" role="status" aria-live="polite"' in html
    assert 'id="provider-symbol-results" class="search-results" aria-live="polite"' in html
    assert "providerSearchCompatibility" in javascript
    assert 'textNode("span", capability, "capability-chip")' in javascript
    assert "use.disabled = !compatibility.compatible" in javascript
    assert "Pre-validation estimate:" in javascript


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_route_recommendations_require_credentials_bindings_and_managed_semantics() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    helper = source[
        source.index("function providerSupports") : source.index("\n\nfunction recommendRoutes")
    ]
    assertions = r"""
function descriptor(
  name,
  capabilities,
  assetClasses,
  { configured = true, kind = "market_data" } = {},
) {
  return {
    name,
    capabilities,
    asset_classes: assetClasses,
    credentials_configured: configured,
    kind,
  };
}
function assertEqual(actual, expected, label) {
  const left = JSON.stringify(actual);
  const right = JSON.stringify(expected);
  if (left !== right) throw new Error(`${label}: expected ${right}, got ${left}`);
}

const providers = [
  descriptor("binance", ["quote", "history"], ["crypto"]),
  descriptor("okx", ["quote", "history"], ["crypto"], { configured: false }),
  descriptor("kraken", ["quote", "history"], ["crypto"]),
  descriptor("coingecko", ["quote", "history"], ["crypto"]),
  descriptor("twelve_data", ["quote", "history"], ["fx", "equity", "bond"]),
  descriptor("alpha_vantage", ["quote", "history"], ["fx", "equity", "bond"], { configured: false }),
  descriptor("alpaca", ["quote", "history", "dividend"], ["equity", "bond"]),
  descriptor("finnhub", ["quote"], ["equity", "bond"]),
  descriptor("fred", ["yield"], ["bond"], { kind: "income_data" }),
  descriptor("synthetic", ["quote", "history"], ["crypto", "fx", "equity", "bond"], { kind: "derived" }),
  descriptor("synthetic_fx", ["quote", "history"], ["fx"], { kind: "derived" }),
  descriptor("binance_wbeth_rate", ["yield"], ["crypto"], { kind: "income_data" }),
  descriptor("ethereum_exchange_rate", ["yield"], ["crypto"], { configured: false, kind: "income_data" }),
  descriptor("okx_beth_yield", ["yield"], ["crypto"], { kind: "income_data" }),
  descriptor("lido", ["yield"], ["crypto"], { kind: "income_data" }),
  descriptor("staking_market_ratio_proxy", ["yield"], ["crypto"], { kind: "derived" }),
];
const draft = (updates = {}) => ({
  symbol: "AVAX:USDC",
  assetClass: "crypto",
  assetType: "spot_crypto",
  historyEnabled: true,
  providerSymbols: "binance=AVAXUSDC\ncoingecko=avalanche-2",
  yieldStrategy: "",
  dividendStrategy: "",
  accrualMode: "",
  fredSeries: "",
  synthetic: false,
  ...updates,
});

const crypto = recommendedRoutesForDraft(draft(), providers);
assertEqual(crypto.quote, ["binance", "coingecko"], "crypto quote filters unconfigured and unbound providers");
assertEqual(crypto.history, ["binance", "coingecko"], "crypto history filters unconfigured and unbound providers");

const cross = recommendedRoutesForDraft(draft({ symbol: "EUR:GBP", assetClass: "fx", assetType: "forex_pair", providerSymbols: "" }), providers);
assertEqual(cross.quote, ["synthetic_fx"], "non-USD FX uses the USD hub");
assertEqual(cross.history, ["synthetic_fx"], "non-USD FX history uses the USD hub");
const spoke = recommendedRoutesForDraft(draft({ symbol: "EUR:USD", assetClass: "fx", assetType: "forex_pair", providerSymbols: "" }), providers);
assertEqual(spoke.quote, ["twelve_data"], "USD spokes use only configured direct providers");

const listed = recommendedRoutesForDraft(draft({
  symbol: "QQQM:USD", assetClass: "equity", assetType: "equity_etf",
  providerSymbols: "alpaca=QQQM", dividendStrategy: "latest_regular_cash_annualized_x4",
}), providers);
assertEqual(listed.quote, ["alpaca"], "listed quotes require bindings");
assertEqual(listed.dividend, ["alpaca"], "dividend route follows income policy");

const wbeth = recommendedRoutesForDraft(draft({
  symbol: "WBETH:USDC", assetType: "staking_token", yieldStrategy: "staking_provider_metric",
  accrualMode: "value_accruing", providerSymbols: "binance=WBETHUSDC",
}), providers);
assertEqual(wbeth.yield, ["binance_wbeth_rate", "staking_market_ratio_proxy"], "WBETH staking semantics");
const beth = recommendedRoutesForDraft(draft({
  symbol: "BETH:USDC", assetType: "staking_token", yieldStrategy: "staking_provider_metric",
  accrualMode: "distributed_units", providerSymbols: "",
}), providers);
assertEqual(beth.yield, ["okx_beth_yield"], "BETH staking semantics");
const generic = recommendedRoutesForDraft(draft({
  symbol: "LST:USDC", assetType: "staking_token", yieldStrategy: "staking_provider_metric",
  accrualMode: "value_accruing", providerSymbols: "",
}), providers);
assertEqual(generic.yield, ["staking_market_ratio_proxy"], "generic value-accruing fallback");

const treasury = recommendedRoutesForDraft(draft({
  symbol: "TBND:USD", assetClass: "bond", assetType: "growth_bond_etf",
  providerSymbols: "alpaca=TBND", yieldStrategy: "treasury_proxy_minus_expense",
  fredSeries: "DGS6MO",
}), providers);
assertEqual(treasury.yield, ["fred"], "controlled Treasury proxy route");
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
def test_validation_diagnostics_render_staged_diff_and_compiler_credit_plan() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    helper = source[
        source.index("function summarizedSymbols") : source.index(
            "\n\nfunction showCatalogDiagnostics"
        )
    ]
    assertions = r"""
function nestedPayload(value) { return value; }
function compactNumber(value) { return String(value); }
function includes(messages, expected) {
  if (!messages.some((message) => message.includes(expected))) {
    throw new Error(`missing ${expected}: ${JSON.stringify(messages)}`);
  }
}
const messages = diagnosticsFrom({
  valid: true,
  errors: [],
  warnings: ["One controlled warning."],
  diff: {
    added: ["DOGE:USDC"],
    changed: [],
    archived_or_disabled: ["BTC:USDC"],
    counts: { added: 1, changed: 0, archived_or_disabled: 1, total: 2 },
  },
  provider_routes: {
    credit_plan: {
      worst_case_daily_credits: { twelve_data: 768 },
      hard_capped_daily_credits: { twelve_data: 790 },
      budgets: { twelve_data: { reserved_for_fx: 200 } },
      assumptions: ["Fallback chains are budgeted at worst-case full use."],
    },
  },
});
includes(messages, "passed structural, routing, dependency, and budget validation");
includes(messages, "One controlled warning");
includes(messages, "2 active definition changes");
includes(messages, "Added or enabled (1): DOGE:USDC");
includes(messages, "Archived or disabled (1): BTC:USDC");
includes(messages, "twelve_data: 768 worst-case per day; 790 daily hard cap; 200 reserved for FX");
includes(messages, "Credit assumption: Fallback chains");
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
def test_provider_symbol_search_rejects_incompatible_bindings() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    helper = source[
        source.index("function providerSearchCompatibility") : source.index(
            "\n\nfunction selectedCapabilitiesForProvider"
        )
    ]
    assertions = r"""
const supported = providerSearchCompatibility(
  {asset_class: "crypto", capabilities: ["QUOTE", "history", "quote"]},
  "crypto",
  ["quote", "history"],
);
if (!supported.compatible) throw new Error(`compatible result rejected: ${supported.reason}`);
if (supported.capabilities.join(",") !== "quote,history") throw new Error("capabilities were not normalized");

const listedBond = providerSearchCompatibility(
  {asset_class: "equity", asset_classes: ["bond", "equity"], capabilities: ["quote", "history"]},
  "bond",
  ["quote", "history"],
);
if (!listedBond.compatible) throw new Error(`listed bond result rejected: ${listedBond.reason}`);
if (listedBond.assetClasses.join(",") !== "bond,equity") throw new Error("asset classes were not normalized");

const missing = providerSearchCompatibility(
  {asset_class: "crypto", capabilities: ["quote"]},
  "crypto",
  ["quote", "history"],
);
if (missing.compatible || !missing.reason.includes("history")) throw new Error("missing capability accepted");

const wrongAsset = providerSearchCompatibility(
  {asset_class: "equity", capabilities: ["quote", "history"]},
  "crypto",
  ["quote"],
);
if (wrongAsset.compatible || !wrongAsset.reason.includes("equity")) throw new Error("wrong asset class accepted");

const unverified = providerSearchCompatibility(
  {asset_class: "crypto"},
  "crypto",
  [],
);
if (unverified.compatible) throw new Error("capability-free result accepted");
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
def test_legacy_treasury_strategy_is_constrained_to_dgs3mo() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    helper = source[
        source.index("function treasurySeriesConstraint") : source.index(
            "\n\nfunction syncTreasurySeriesConstraint"
        )
    ]
    assertions = r"""
const legacy = treasurySeriesConstraint("treasury_3m_proxy_minus_expense", "DGS1");
if (!legacy.legacy || legacy.series !== "DGS3MO") throw new Error("legacy strategy was not constrained");
const flexible = treasurySeriesConstraint("treasury_proxy_minus_expense", "DGS6MO");
if (flexible.legacy || flexible.series !== "DGS6MO") throw new Error("generic strategy lost its maturity");
const unrelated = treasurySeriesConstraint("staking_provider_metric", "");
if (unrelated.legacy || unrelated.series !== "") throw new Error("unrelated strategy was changed");
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
