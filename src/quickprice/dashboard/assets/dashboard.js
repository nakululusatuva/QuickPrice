"use strict";

const SESSION_KEY = "quickprice-dashboard-api-key";
const THEME_KEY = "quickprice-dashboard-theme";
const REFRESH_INTERVAL_MS = 10_000;
const CATALOG_REFRESH_INTERVAL_MS = 30_000;
const ACCESS_REFRESH_INTERVAL_MS = 60_000;
const GENERATION_REFRESH_ATTEMPTS = 3;
const CATALOG_REVISION_HEADER = "X-QuickPrice-Catalog-Revision";
const LOG_LIMIT = 500;
const LEVEL_WEIGHT = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
const FIXTURE_SOURCE_PATTERN = /(^|[^a-z0-9])fixture([^a-z0-9]|$)/i;

function isFixtureSource(source) {
  if (!source || typeof source !== "object") return false;
  return [source.provider, source.feed].some(
    (value) => typeof value === "string" && FIXTURE_SOURCE_PATTERN.test(value),
  );
}

const state = {
  apiKey: sessionStorage.getItem(SESSION_KEY) || "",
  instruments: [],
  quotes: new Map(),
  quoteErrors: new Map(),
  expandedSymbols: new Set(),
  sortField: "instrument",
  ascending: true,
  refreshing: false,
  refreshTimer: null,
  catalogEtag: null,
  catalogRevision: null,
  catalogRefreshing: false,
  catalogRefreshTimer: null,
  access: null,
  accessRefreshTimer: null,
  logs: [],
  logCursor: null,
  logController: null,
  logReconnectTimer: null,
  logPaused: false,
  logsWhilePaused: 0,
};

const element = (id) => document.getElementById(id);
const ui = {
  apiKey: element("api-key"),
  credentialForm: element("credential-form"),
  forgetKey: element("forget-key"),
  connectionBadge: element("connection-badge"),
  apiKeyValidity: element("api-key-validity"),
  apiKeyName: element("api-key-name"),
  apiKeyExpiry: element("api-key-expiry"),
  themeToggle: element("theme-toggle"),
  tabs: [...document.querySelectorAll("[data-tab]")],
  marketPanel: element("market-panel"),
  logsPanel: element("logs-panel"),
  lastRefresh: element("last-refresh"),
  registered: element("kpi-registered"),
  priced: element("kpi-priced"),
  open: element("kpi-open"),
  stale: element("kpi-stale"),
  search: element("market-search"),
  assetFilter: element("asset-filter"),
  statusFilter: element("status-filter"),
  sortField: element("sort-field"),
  sortDirection: element("sort-direction"),
  sortHeaders: [...document.querySelectorAll(".sort-header[data-sort-field]")],
  refreshMarket: element("refresh-market"),
  fixtureWarning: element("fixture-warning"),
  fixtureWarningMessage: element("fixture-warning-message"),
  marketNotice: element("market-notice"),
  marketBody: element("market-body"),
  marketEmpty: element("market-empty"),
  resultCount: element("result-count"),
  logStreamBadge: element("log-stream-badge"),
  logLevel: element("log-level"),
  logSearch: element("log-search"),
  pauseLogs: element("pause-logs"),
  clearLogs: element("clear-logs"),
  reconnectLogs: element("reconnect-logs"),
  logNotice: element("log-notice"),
  logList: element("log-list"),
  logEmpty: element("log-empty"),
  logCount: element("log-count"),
  pausedCount: element("paused-count"),
};

function setBadge(target, text, kind = "neutral") {
  target.textContent = text;
  target.className = `status-badge status-${kind}`;
}

function setNotice(target, text, kind = "neutral") {
  target.textContent = text;
  target.className = "notice";
  if (kind === "error") target.classList.add("is-error");
  if (kind === "success") target.classList.add("is-success");
}

function textNode(tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);
  ui.themeToggle.textContent = theme === "dark" ? "Light mode" : "Dark mode";
  ui.themeToggle.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} mode`);
}

function initializeTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  const theme = saved || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  applyTheme(theme);
}

async function apiJson(path, { allowUnavailable = false } = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "X-API-Key": state.apiKey,
    },
  });
  let body;
  try {
    body = await response.json();
  } catch (_error) {
    throw new Error(`Server returned an invalid response (${response.status})`);
  }
  if (response.status === 401) {
    const error = new Error("The API key was rejected.");
    error.unauthorized = true;
    throw error;
  }
  if (!response.ok && !(allowUnavailable && response.status === 503)) {
    const detail = body.errors?.map((item) => item.message).join("; ");
    throw new Error(detail || `Request failed with status ${response.status}`);
  }
  return {
    envelope: body,
    catalogRevision: response.headers.get(CATALOG_REVISION_HEADER),
  };
}

function revisionFromEtag(etag) {
  if (typeof etag !== "string") return null;
  let value = etag.trim();
  if (value.startsWith("W/")) value = value.slice(2);
  if (value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
  return value || null;
}

async function fetchInstrumentCatalog(etag = state.catalogEtag) {
  const headers = {
    Accept: "application/json",
    "X-API-Key": state.apiKey,
  };
  if (etag) headers["If-None-Match"] = etag;
  const response = await fetch("/v1/instruments", {
    cache: "no-store",
    headers,
  });
  if (response.status === 304) {
    return {
      notModified: true,
      etag,
      revision: revisionFromEtag(etag),
      data: null,
    };
  }
  let body;
  try {
    body = await response.json();
  } catch (_error) {
    throw new Error(`Server returned an invalid response (${response.status})`);
  }
  if (response.status === 401) {
    const error = new Error("The API key was rejected.");
    error.unauthorized = true;
    throw error;
  }
  if (!response.ok) {
    const detail = body.errors?.map((item) => item.message).join("; ");
    throw new Error(detail || `Request failed with status ${response.status}`);
  }
  const responseEtag = response.headers.get("ETag");
  return {
    notModified: false,
    etag: responseEtag,
    revision: response.headers.get(CATALOG_REVISION_HEADER) || revisionFromEtag(responseEtag),
    data: Array.isArray(body.data) ? body.data : [],
  };
}

function instrumentCatalogFingerprint(instruments) {
  return JSON.stringify(instruments);
}

function reconcileInstrumentCatalog(nextInstruments, { render = true } = {}) {
  if (
    instrumentCatalogFingerprint(state.instruments)
    === instrumentCatalogFingerprint(nextInstruments)
  ) return false;
  const activeSymbols = new Set(nextInstruments.map((item) => item.symbol));
  state.instruments = nextInstruments;
  for (const symbol of [...state.quotes.keys()]) {
    if (!activeSymbols.has(symbol)) state.quotes.delete(symbol);
  }
  for (const symbol of [...state.quoteErrors.keys()]) {
    if (!activeSymbols.has(symbol)) state.quoteErrors.delete(symbol);
  }
  for (const symbol of [...state.expandedSymbols]) {
    if (!activeSymbols.has(symbol)) state.expandedSymbols.delete(symbol);
  }
  if (render) {
    populateAssetFilter();
    updateFixtureWarning();
    updateSummary();
    renderMarket();
  }
  return true;
}

function dateTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  }).format(parsed);
}

function compactTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(parsed);
}

function renderApiKeyAccess(access) {
  state.access = access && typeof access === "object" ? access : null;
  ui.apiKeyValidity.hidden = state.access === null;
  ui.apiKeyValidity.classList.remove("is-warning");
  if (!state.access) {
    ui.apiKeyName.textContent = "Current API key";
    ui.apiKeyExpiry.textContent = "Not connected";
    return;
  }
  ui.apiKeyName.textContent = state.access.name || "Current API key";
  if (state.access.is_permanent || !state.access.expires_at) {
    ui.apiKeyExpiry.textContent = "Permanent";
    return;
  }
  const expiry = new Date(state.access.expires_at);
  ui.apiKeyExpiry.textContent = `Expires ${dateTime(state.access.expires_at)}`;
  if (!Number.isNaN(expiry.getTime()) && expiry.getTime() <= Date.now() + 7 * 86_400_000) {
    ui.apiKeyValidity.classList.add("is-warning");
  }
}

async function refreshApiKeyAccess() {
  if (!state.apiKey) return false;
  try {
    const result = await apiJson("/v1/access");
    renderApiKeyAccess(result.envelope.data);
    return true;
  } catch (error) {
    handleConnectionError(error);
    return false;
  }
}

function price(value) {
  if (!Number.isFinite(value)) return "-";
  const absolute = Math.abs(value);
  const digits = absolute >= 1000 ? 2 : absolute >= 1 ? 4 : 8;
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value);
}

function percent(value) {
  if (!Number.isFinite(value)) return "-";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function incomePercent(value) {
  if (!Number.isFinite(value)) return "-";
  return `${value.toFixed(2)}%`;
}

function changeClass(value) {
  if (!Number.isFinite(value) || value === 0) return "change-flat";
  return value > 0 ? "change-positive" : "change-negative";
}

function duration(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "unknown age";
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s old`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m old`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h old`;
  return `${Math.round(hours / 24)}d old`;
}

function populateAssetFilter() {
  const current = ui.assetFilter.value;
  const values = [...new Set(state.instruments.map((item) => item.asset_class))].sort();
  ui.assetFilter.replaceChildren(new Option("All classes", ""));
  for (const value of values) ui.assetFilter.add(new Option(value.toUpperCase(), value));
  ui.assetFilter.value = values.includes(current) ? current : "";
}

function rows() {
  return state.instruments.map((instrument) => ({
    instrument,
    quote: state.quotes.get(instrument.symbol) || null,
    error: state.quoteErrors.get(instrument.symbol) || null,
  }));
}

function instrumentExpansion(symbol) {
  const expanded = state.expandedSymbols.has(symbol);
  return { expanded, buttonText: expanded ? "Close" : "Inspect" };
}

function setInstrumentExpanded(symbol, expanded) {
  if (expanded) state.expandedSymbols.add(symbol);
  else state.expandedSymbols.delete(symbol);
  return instrumentExpansion(symbol);
}

function finiteNumber(value) {
  return Number.isFinite(value) ? value : null;
}

function textValue(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function availability(item) {
  const reason = textValue(item.error?.message);
  const code = textValue(item.error?.code);
  if (!item.quote) {
    return {
      kind: "unavailable",
      label: "Price unavailable",
      reason: reason ?? "No valid price snapshot is available.",
      code: code ?? "data_unavailable",
    };
  }
  if (item.error) {
    return {
      kind: "incomplete",
      label: "Metadata incomplete",
      reason: reason ?? "Required instrument metadata is unavailable.",
      code: code ?? "data_incomplete",
    };
  }
  return { kind: "available", label: "Available", reason: null, code: null };
}

function changePercent(item, windowName) {
  return finiteNumber(item.quote?.changes?.[windowName]?.percent);
}

function incomeSortPercent(item) {
  if (item.quote?.dividend) return finiteNumber(item.quote.dividend.yield_percent);
  return finiteNumber(item.quote?.estimated_annual_yield?.percent);
}

function marketStatus(item) {
  return textValue(item.quote?.market_status)?.toLowerCase() ?? "unavailable";
}

const MARKET_STATUS_ORDER = Object.freeze({
  open: 0,
  closed: 1,
  unknown: 2,
  unavailable: 3,
});

const SORT_FIELDS = Object.freeze({
  instrument: { kind: "text", read: (item) => textValue(item.instrument.symbol) },
  price: { kind: "number", read: (item) => finiteNumber(item.quote?.price) },
  "1h": { kind: "number", read: (item) => changePercent(item, "1h") },
  "4h": { kind: "number", read: (item) => changePercent(item, "4h") },
  "1d": { kind: "number", read: (item) => changePercent(item, "1d") },
  "1w": { kind: "number", read: (item) => changePercent(item, "1w") },
  "1m": { kind: "number", read: (item) => changePercent(item, "1mo") },
  "1y": { kind: "number", read: (item) => changePercent(item, "1y") },
  income: { kind: "number", read: incomeSortPercent },
  market: {
    kind: "market",
    read: marketStatus,
  },
  source: {
    kind: "source",
    read: (item) => {
      const provider = textValue(item.quote?.source?.provider);
      if (provider === null) return null;
      return { provider, feed: textValue(item.quote?.source?.feed) ?? "" };
    },
  },
});

function sortValue(item, field) {
  return (SORT_FIELDS[field] ?? SORT_FIELDS.instrument).read(item);
}

function compareText(left, right) {
  const insensitive = left.localeCompare(right, "en", { numeric: true, sensitivity: "base" });
  if (insensitive !== 0) return insensitive;
  return left.localeCompare(right, "en", { numeric: true, sensitivity: "variant" });
}

function comparePrimaryValues(left, right, field) {
  const kind = (SORT_FIELDS[field] ?? SORT_FIELDS.instrument).kind;
  if (kind === "number") return left - right;
  if (kind === "market") {
    const leftRank = MARKET_STATUS_ORDER[left] ?? 4;
    const rightRank = MARKET_STATUS_ORDER[right] ?? 4;
    return leftRank - rightRank || compareText(left, right);
  }
  if (kind === "source") {
    return compareText(left.provider, right.provider) || compareText(left.feed, right.feed);
  }
  return compareText(left, right);
}

function timestampValue(value) {
  if (!value) return null;
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : null;
}

function compareOptionalNumbers(left, right) {
  if (left === null) return right === null ? 0 : 1;
  if (right === null) return -1;
  return left - right;
}

function compareOptionalNumbersDescending(left, right) {
  if (left === null) return right === null ? 0 : 1;
  if (right === null) return -1;
  return right - left;
}

function compareFixedFieldQuality(left, right, field) {
  if (field === "market") {
    const leftTime = timestampValue(left.quote?.as_of);
    const rightTime = timestampValue(right.quote?.as_of);
    return compareOptionalNumbersDescending(leftTime, rightTime);
  }
  if (field === "source") {
    const fallbackOrder = compareOptionalNumbers(
      finiteNumber(left.quote?.source?.fallback_level),
      finiteNumber(right.quote?.source?.fallback_level),
    );
    if (fallbackOrder !== 0) return fallbackOrder;
    return Number(Boolean(left.quote?.quality?.stale))
      - Number(Boolean(right.quote?.quality?.stale));
  }
  return 0;
}

function compareSymbols(left, right) {
  return compareText(left.instrument.symbol, right.instrument.symbol);
}

function compareRows(left, right, field, ascending) {
  const leftValue = sortValue(left, field);
  const rightValue = sortValue(right, field);
  const leftMissing = leftValue === null;
  const rightMissing = rightValue === null;
  if (leftMissing || rightMissing) {
    if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
    return compareSymbols(left, right);
  }
  const primaryOrder = comparePrimaryValues(leftValue, rightValue, field);
  if (primaryOrder !== 0) return ascending ? primaryOrder : -primaryOrder;
  const qualityOrder = compareFixedFieldQuality(left, right, field);
  return qualityOrder || compareSymbols(left, right);
}

function updateSortControls() {
  ui.sortField.value = state.sortField;
  ui.sortDirection.textContent = state.ascending ? "Ascending" : "Descending";
  const currentDirection = state.ascending ? "ascending" : "descending";
  const nextDirection = state.ascending ? "descending" : "ascending";
  ui.sortDirection.setAttribute(
    "aria-label",
    `Sort ${nextDirection}; current order ${currentDirection}`,
  );
  for (const header of ui.sortHeaders) {
    const active = header.dataset.sortField === state.sortField;
    const tableHeader = header.closest("th");
    const indicator = header.querySelector("[data-sort-indicator]");
    header.classList.toggle("is-active", active);
    if (indicator) indicator.textContent = active ? (state.ascending ? "\u2191" : "\u2193") : "";
    if (active) tableHeader.setAttribute("aria-sort", state.ascending ? "ascending" : "descending");
    else tableHeader.removeAttribute("aria-sort");
  }
}

function selectSortField(field, { toggleIfActive = false } = {}) {
  if (!Object.hasOwn(SORT_FIELDS, field)) return;
  if (field === state.sortField) {
    if (toggleIfActive) state.ascending = !state.ascending;
  } else {
    state.sortField = field;
    state.ascending = true;
  }
  updateSortControls();
  renderMarket();
}

function visibleRows() {
  const query = ui.search.value.trim().toLowerCase();
  const assetClass = ui.assetFilter.value;
  const status = ui.statusFilter.value;
  const filtered = rows().filter((item) => {
    const haystack = [
      item.instrument.symbol,
      item.instrument.name,
      item.instrument.description,
      item.instrument.asset_class,
      item.instrument.asset_type,
      item.quote?.source?.provider,
      item.quote?.source?.feed,
      item.error?.code,
      item.error?.message,
      availability(item).label,
    ].filter(Boolean).join(" ").toLowerCase();
    const itemStatus = marketStatus(item);
    return (!query || haystack.includes(query))
      && (!assetClass || item.instrument.asset_class === assetClass)
      && (!status || itemStatus === status);
  });
  filtered.sort((left, right) => compareRows(left, right, state.sortField, state.ascending));
  return filtered;
}

function metadataItem(term, value) {
  const wrapper = document.createElement("div");
  wrapper.append(textNode("dt", term), textNode("dd", value ?? "-"));
  return wrapper;
}

function buildDetails(item) {
  const cell = document.createElement("td");
  cell.colSpan = 12;
  const panel = document.createElement("div");
  panel.className = "detail-panel";

  const summary = document.createElement("section");
  summary.append(textNode("h3", "Instrument metadata"));
  const metadata = document.createElement("dl");
  metadata.className = "metadata-grid";
  const instrument = item.instrument;
  const quote = item.quote;
  const dataAvailability = availability(item);
  const fields = [
    ["Symbol", instrument.symbol],
    ["Name", instrument.name],
    ["Description", instrument.description],
    ["Base / quote", `${instrument.base} / ${instrument.quote}`],
    ["Asset class", instrument.asset_class],
    ["Asset type", instrument.asset_type],
    ["Price basis", quote?.price_basis || instrument.price_basis],
    ["Change basis", instrument.change_basis],
    ["Market status", marketStatus(item)],
    ["Data status", dataAvailability.label],
    ["Status reason", dataAvailability.reason],
    ["Status code", dataAvailability.code],
    ["As of", dateTime(quote?.as_of)],
    ["Provider", quote?.source?.provider],
    ["Feed", quote?.source?.feed],
    ["Coverage", quote?.source?.coverage],
    ["License scope", quote?.source?.license_scope],
    ["Fallback level", quote?.source?.fallback_level ?? "-"],
    ["Derived", quote?.source?.is_derived === undefined ? "-" : String(quote.source.is_derived)],
    ["Stale", quote?.quality?.stale === undefined ? "-" : String(quote.quality.stale)],
    ["Snapshot age", quote ? duration(quote.quality?.staleness_ms) : "-"],
    ["Dividend method", instrument.dividend_method],
    ["Yield method", instrument.yield_method],
    ["Yield provider", quote?.estimated_annual_yield?.provider],
    ["Yield calculation", quote?.estimated_annual_yield?.method],
    ["Yield rate type", quote?.estimated_annual_yield?.rate_type],
    [
      "Yield observation window",
      quote?.estimated_annual_yield?.observation_window_days == null
        ? "-"
        : `${quote.estimated_annual_yield.observation_window_days} days`,
    ],
    [
      "Yield is proxy",
      quote?.estimated_annual_yield?.is_proxy === undefined
        ? "-"
        : String(quote.estimated_annual_yield.is_proxy),
    ],
    [
      "Yield is estimate",
      quote?.estimated_annual_yield?.is_estimate === undefined
        ? "-"
        : String(quote.estimated_annual_yield.is_estimate),
    ],
    ["Yield confidence", quote?.estimated_annual_yield?.quality?.confidence],
    ["Reward accrual", instrument.reward_accrual_mode],
    ["Underlying", instrument.underlying_asset],
  ];
  for (const [term, value] of fields) metadata.append(metadataItem(term, value));
  summary.append(metadata);

  const raw = document.createElement("section");
  raw.append(textNode("h3", "Complete API record"));
  const pre = document.createElement("pre");
  pre.className = "json-view";
  pre.textContent = JSON.stringify({ instrument, quote, error: item.error }, null, 2);
  raw.append(pre);
  panel.append(summary, raw);
  cell.append(panel);
  return cell;
}

function marketCell(item) {
  const cell = document.createElement("td");
  const status = marketStatus(item);
  const statusNode = textNode("span", status.toUpperCase(), "market-state");
  if (item.quote?.quality?.stale) statusNode.classList.add("is-stale");
  cell.append(statusNode, textNode("small", dateTime(item.quote?.as_of), "source-feed"));
  return cell;
}

function yieldLabel(value) {
  const rateType = textValue(value?.rate_type)?.toUpperCase() || "YIELD";
  if (value?.is_proxy) return `Proxy ${rateType}`;
  if (value?.is_estimate) return `Estimated ${rateType}`;
  return rateType;
}

function dividendExplanation(dividend) {
  const frequency = textValue(dividend?.frequency) || "reported";
  return `Annualized dividend yield. The latest regular cash dividend is projected using its ${frequency.toLowerCase()} payment frequency, then divided by the current price. This is an estimate, not a guaranteed return.`;
}

function yieldExplanation(value) {
  const rateType = textValue(value?.rate_type)?.toLowerCase();
  const convention = rateType === "apy"
    ? "APY includes the effect of compounding over one year."
    : rateType === "apr"
      ? "APR expresses a one-year rate without assuming compounding."
      : "The figure is expressed as an estimated annual rate.";
  const method = textValue(value?.method)?.toLowerCase() || "";
  const accrualMode = textValue(value?.accrual_mode)?.toLowerCase() || "";
  let calculation;
  if (method === "latest_distribution_annualized") {
    calculation = "It repeats the latest regular cash distribution over a full year and divides the result by the current price.";
  } else if (method.includes("treasury_3m") || method.includes("expense")) {
    calculation = "It uses the three-month US Treasury yield minus fund expenses as a proxy, rather than a yield published by the fund.";
  } else if (method.includes("ratio") || method.includes("growth") || value?.observation_window_days) {
    const window = Number.isFinite(value?.observation_window_days)
      ? ` over the most recent ${value.observation_window_days}-day observation window`
      : " over a recent observation window";
    calculation = `It annualizes the change in the token-to-underlying conversion ratio${window}. Short observation windows can produce unstable estimates.`;
  } else if (value?.is_proxy) {
    calculation = "It is derived from a related market input because a direct issuer or protocol rate was unavailable.";
  } else if (value?.is_estimate) {
    calculation = "It is an estimated forward rate based on the latest available provider observation.";
  } else {
    calculation = "It is the latest annual rate reported by the named provider.";
  }
  let rewards = "";
  if (accrualMode === "value_accruing") rewards = " Rewards normally accrue by increasing how much underlying asset each token represents, while the token count stays unchanged.";
  else if (accrualMode === "rebasing_balance") rewards = " Rewards normally accrue by increasing the number of tokens held; the token price does not need to rise for rewards to be earned.";
  else if (accrualMode === "distributed_units") rewards = " Rewards are normally delivered as additional token units.";
  else if (accrualMode === "claimable_rewards") rewards = " Rewards normally accumulate separately until claimed.";
  const provider = textValue(value?.provider);
  const source = provider ? ` Source: ${provider}.` : "";
  return `${convention} ${calculation}${rewards}${source} This is not a guaranteed return.`;
}

function incomeCell(item) {
  const cell = document.createElement("td");
  const dividend = item.quote?.dividend;
  const annualYield = item.quote?.estimated_annual_yield;
  const explanations = [];
  if (dividend) {
    const label = textNode("small", `Dividend - ${dividend.frequency}`, "income-label has-help");
    const explanation = dividendExplanation(dividend);
    label.title = explanation;
    explanations.push(explanation);
    cell.append(
      textNode("strong", incomePercent(dividend.yield_percent), "income-value"),
      label,
    );
  }
  if (annualYield) {
    const label = textNode("small", yieldLabel(annualYield), "income-label has-help");
    if (annualYield.is_proxy) label.classList.add("is-proxy");
    else if (annualYield.is_estimate) label.classList.add("is-estimate");
    const explanation = yieldExplanation(annualYield);
    label.title = explanation;
    explanations.push(explanation);
    cell.append(
      textNode("strong", incomePercent(annualYield.percent), "income-value"),
      label,
    );
  }
  if (!dividend && !annualYield) cell.append(textNode("span", "-", "value-empty"));
  if (explanations.length) {
    cell.title = explanations.join("\n\n");
    const displayedRates = [
      dividend ? `Dividend ${incomePercent(dividend.yield_percent)}` : null,
      annualYield ? `${yieldLabel(annualYield)} ${incomePercent(annualYield.percent)}` : null,
    ].filter(Boolean).join(". ");
    cell.tabIndex = 0;
    cell.setAttribute("aria-label", `${displayedRates}. ${explanations.join(" ")}`);
  }
  return cell;
}

function renderMarket() {
  const items = visibleRows();
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const expansion = instrumentExpansion(item.instrument.symbol);
    const dataAvailability = availability(item);
    const row = document.createElement("tr");
    row.className = `market-row availability-${dataAvailability.kind}`;
    const instrumentCell = document.createElement("td");
    instrumentCell.append(
      textNode("strong", item.instrument.symbol, "instrument-symbol"),
      textNode("span", item.instrument.name, "instrument-name"),
      textNode("span", `${item.instrument.asset_class} / ${item.instrument.asset_type}`, "source-feed"),
    );
    if (dataAvailability.kind !== "available") {
      instrumentCell.append(
        textNode(
          "span",
          dataAvailability.label,
          `availability-label is-${dataAvailability.kind}`,
        ),
      );
    }
    const priceCell = document.createElement("td");
    priceCell.append(
      textNode("strong", price(item.quote?.price), "price-value"),
      textNode("span", item.instrument.quote, "price-quote"),
    );
    row.append(instrumentCell, priceCell);
    for (const windowName of ["1h", "4h", "1d", "1w", "1mo", "1y"]) {
      const value = item.quote?.changes?.[windowName]?.percent;
      row.append(textNode("td", percent(value), changeClass(value)));
    }
    row.append(incomeCell(item), marketCell(item));
    const sourceCell = document.createElement("td");
    if (item.quote) {
      sourceCell.append(
        textNode("strong", item.quote.source?.provider || "-", "source-provider"),
        textNode("span", item.quote.source?.feed || "Feed unavailable", "source-feed"),
        textNode(
          "span",
          item.quote.quality?.stale ? duration(item.quote.quality.staleness_ms) : "Current",
          item.quote.quality?.stale ? "change-negative" : "change-flat",
        ),
      );
    } else {
      sourceCell.append(
        textNode("strong", "Unavailable", "source-provider source-unavailable"),
        textNode("span", dataAvailability.code, "source-feed"),
        textNode("span", "No price snapshot", "change-negative"),
      );
    }
    if (dataAvailability.reason) {
      const reason = textNode("span", dataAvailability.reason, "availability-reason");
      reason.title = dataAvailability.reason;
      sourceCell.append(reason);
    }
    row.append(sourceCell);
    const actionCell = document.createElement("td");
    const inspect = textNode("button", expansion.buttonText, "inspect-button");
    inspect.type = "button";
    inspect.setAttribute("aria-expanded", String(expansion.expanded));
    actionCell.append(inspect);
    row.append(actionCell);

    const detail = document.createElement("tr");
    detail.className = "detail-row";
    detail.id = `instrument-detail-${item.instrument.symbol.replace(/[^A-Z0-9]/gi, "-")}`;
    detail.hidden = !expansion.expanded;
    detail.append(buildDetails(item));
    inspect.setAttribute("aria-controls", detail.id);
    inspect.addEventListener("click", () => {
      const next = setInstrumentExpanded(item.instrument.symbol, detail.hidden);
      detail.hidden = !next.expanded;
      inspect.textContent = next.buttonText;
      inspect.setAttribute("aria-expanded", String(next.expanded));
    });
    fragment.append(row, detail);
  }
  ui.marketBody.replaceChildren(fragment);
  ui.marketEmpty.hidden = items.length !== 0;
  ui.resultCount.textContent = `${items.length} of ${state.instruments.length} instruments`;
}

function updateSummary() {
  const values = rows();
  ui.registered.textContent = String(values.length);
  ui.priced.textContent = String(values.filter((item) => item.quote).length);
  ui.open.textContent = String(values.filter((item) => item.quote?.market_status === "open").length);
  ui.stale.textContent = String(values.filter((item) => item.quote?.quality?.stale).length);
}

function updateFixtureWarning() {
  const fixtureCount = [...state.quotes.values()].filter((quote) => isFixtureSource(quote.source)).length;
  ui.fixtureWarning.hidden = fixtureCount === 0;
  if (fixtureCount === 0) return;
  const allQuotesAreFixtures = fixtureCount === state.quotes.size;
  ui.fixtureWarningMessage.textContent = allQuotesAreFixtures
    ? "All displayed prices are generated test fixtures, not live market data. Do not use them for trading, valuation, or financial decisions."
    : `${fixtureCount} displayed ${fixtureCount === 1 ? "quote comes" : "quotes come"} from a test fixture and ${fixtureCount === 1 ? "is" : "are"} not live market data. Do not use fixture prices for trading, valuation, or financial decisions.`;
}

function currentCatalogSnapshot() {
  return {
    notModified: false,
    etag: state.catalogEtag,
    revision: state.catalogRevision,
    data: state.instruments,
  };
}

function commitMarketGeneration(catalog, envelope) {
  const catalogChanged = reconcileInstrumentCatalog(catalog.data, { render: false });
  state.catalogEtag = catalog.etag;
  state.catalogRevision = catalog.revision;
  state.quotes.clear();
  state.quoteErrors.clear();
  const activeSymbols = new Set(catalog.data.map((item) => item.symbol));
  for (const quote of envelope.data || []) {
    if (activeSymbols.has(quote.symbol)) state.quotes.set(quote.symbol, quote);
  }
  for (const error of envelope.errors || []) {
    if (error.symbol && activeSymbols.has(error.symbol)) {
      state.quoteErrors.set(error.symbol, error);
    }
  }
  ui.lastRefresh.textContent = dateTime(envelope.generated_at || new Date().toISOString());
  const incomplete = [...state.quoteErrors.keys()].filter(
    (symbol) => state.quotes.has(symbol),
  ).length;
  const unavailable = state.instruments.length - state.quotes.size;
  const issueSummary = [
    incomplete ? `${incomplete} metadata incomplete` : null,
    unavailable ? `${unavailable} unavailable` : null,
  ].filter(Boolean).join("; ");
  setNotice(
    ui.marketNotice,
    state.instruments.length === 0
      ? "No instruments are active in the current catalog."
      : issueSummary
      ? `${state.quotes.size} priced; ${issueSummary}. Exact reasons are shown with each instrument.`
      : "All registered instruments are priced and complete.",
    issueSummary ? "neutral" : "success",
  );
  setBadge(
    ui.connectionBadge,
    issueSummary ? "Partial" : "Connected",
    issueSummary ? "warn" : "good",
  );
  if (catalogChanged) populateAssetFilter();
  updateFixtureWarning();
  updateSummary();
  renderMarket();
  return catalogChanged;
}

async function refreshQuotes(candidateCatalog = null) {
  const firstCatalog = candidateCatalog?.data ? candidateCatalog : currentCatalogSnapshot();
  if (!state.apiKey || state.refreshing) return false;
  state.refreshing = true;
  ui.refreshMarket.disabled = true;
  try {
    let catalog = firstCatalog;
    for (let attempt = 0; attempt < GENERATION_REFRESH_ATTEMPTS; attempt += 1) {
      if (!catalog.revision) {
        throw new Error("The instrument catalog response omitted its generation revision.");
      }
      const result = await apiJson("/v1/quotes", { allowUnavailable: true });
      if (!result.catalogRevision) {
        throw new Error("The quote response omitted its catalog generation revision.");
      }
      if (result.catalogRevision === catalog.revision) {
        return commitMarketGeneration(catalog, result.envelope);
      }
      const latest = await fetchInstrumentCatalog(catalog.etag);
      if (!latest.notModified) catalog = latest;
    }
    throw new Error("The instrument catalog changed repeatedly while prices were loading. Retry the refresh.");
  } catch (error) {
    handleConnectionError(error);
    return false;
  } finally {
    state.refreshing = false;
    ui.refreshMarket.disabled = false;
  }
}

async function refreshInstrumentCatalog() {
  if (!state.apiKey || state.catalogRefreshing || state.refreshing) return false;
  state.catalogRefreshing = true;
  try {
    const result = await fetchInstrumentCatalog();
    if (result.notModified) return false;
    const changed = result.revision !== state.catalogRevision;
    setNotice(ui.marketNotice, "The instrument catalog changed. Refreshing market data...");
    await refreshQuotes(result);
    return changed;
  } catch (error) {
    handleConnectionError(error);
    return false;
  } finally {
    state.catalogRefreshing = false;
  }
}

async function connect() {
  if (!state.apiKey) return;
  setBadge(ui.connectionBadge, "Connecting", "neutral");
  setNotice(ui.marketNotice, "Loading the installed instrument catalog...");
  try {
    const [catalog, access] = await Promise.all([
      fetchInstrumentCatalog(),
      apiJson("/v1/access"),
    ]);
    renderApiKeyAccess(access.envelope.data);
    const candidate = catalog.notModified ? currentCatalogSnapshot() : catalog;
    sessionStorage.setItem(SESSION_KEY, state.apiKey);
    await refreshQuotes(candidate);
    startRefreshTimers();
    connectLogStream();
  } catch (error) {
    handleConnectionError(error);
  }
}

function handleConnectionError(error) {
  setBadge(ui.connectionBadge, "Disconnected", "bad");
  setNotice(ui.marketNotice, error.message || "Unable to connect to QuickPrice.", "error");
  if (error.unauthorized) {
    sessionStorage.removeItem(SESSION_KEY);
    state.apiKey = "";
    ui.apiKey.value = "";
    stopRefreshTimers();
    stopLogStream();
    clearDashboardData();
  }
}

function startRefreshTimers() {
  window.clearInterval(state.refreshTimer);
  window.clearInterval(state.catalogRefreshTimer);
  window.clearInterval(state.accessRefreshTimer);
  state.refreshTimer = window.setInterval(refreshQuotes, REFRESH_INTERVAL_MS);
  state.catalogRefreshTimer = window.setInterval(
    refreshInstrumentCatalog,
    CATALOG_REFRESH_INTERVAL_MS,
  );
  state.accessRefreshTimer = window.setInterval(
    refreshApiKeyAccess,
    ACCESS_REFRESH_INTERVAL_MS,
  );
}

function stopRefreshTimers() {
  window.clearInterval(state.refreshTimer);
  window.clearInterval(state.catalogRefreshTimer);
  window.clearInterval(state.accessRefreshTimer);
  state.refreshTimer = null;
  state.catalogRefreshTimer = null;
  state.accessRefreshTimer = null;
}

function stopLogStream() {
  window.clearTimeout(state.logReconnectTimer);
  state.logReconnectTimer = null;
  if (state.logController) state.logController.abort();
  state.logController = null;
  setBadge(ui.logStreamBadge, "Disconnected", "neutral");
}

function clearDashboardData() {
  state.instruments = [];
  state.catalogEtag = null;
  state.catalogRevision = null;
  renderApiKeyAccess(null);
  state.quotes.clear();
  state.quoteErrors.clear();
  state.expandedSymbols.clear();
  state.logs = [];
  state.logCursor = null;
  state.logsWhilePaused = 0;
  populateAssetFilter();
  updateSummary();
  renderMarket();
  renderLogs();
  ui.lastRefresh.textContent = "Never";
  updateFixtureWarning();
  ui.pausedCount.textContent = "Live view";
  setNotice(ui.logNotice, "Connect with an API key to open the log stream.");
}

function parseSseBlock(block) {
  let eventName = "message";
  let eventId = null;
  const dataLines = [];
  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    if (field === "id") eventId = value;
    if (field === "data") dataLines.push(value);
  }
  return { eventName, eventId, data: dataLines.join("\n") };
}

function acceptLog(raw, eventId) {
  if (!raw || typeof raw !== "object") return;
  const item = {
    id: Number(raw.id ?? eventId),
    timestamp: String(raw.timestamp || new Date().toISOString()),
    level: String(raw.level || "INFO").toUpperCase(),
    logger: String(raw.logger || "quickprice"),
    message: String(raw.message || ""),
  };
  if (Number.isFinite(item.id)) state.logCursor = item.id;
  state.logs.push(item);
  if (state.logs.length > LOG_LIMIT) state.logs.splice(0, state.logs.length - LOG_LIMIT);
  if (state.logPaused) {
    state.logsWhilePaused += 1;
    ui.pausedCount.textContent = `${state.logsWhilePaused} new while paused`;
  } else {
    renderLogs();
  }
}

async function connectLogStream() {
  stopLogStream();
  if (!state.apiKey) return;
  const controller = new AbortController();
  state.logController = controller;
  setBadge(ui.logStreamBadge, "Connecting", "neutral");
  setNotice(ui.logNotice, "Opening authenticated event stream...");
  try {
    const headers = { Accept: "text/event-stream", "X-API-Key": state.apiKey };
    if (state.logCursor !== null) headers["Last-Event-ID"] = String(state.logCursor);
    const response = await fetch("/internal/logs/stream", {
      cache: "no-store",
      headers,
      signal: controller.signal,
    });
    if (response.status === 401) {
      const error = new Error("The API key was rejected by the log stream.");
      error.unauthorized = true;
      throw error;
    }
    if (!response.ok || !response.body) throw new Error(`Log stream failed with status ${response.status}`);
    setBadge(ui.logStreamBadge, "Live", "good");
    setNotice(ui.logNotice, "Receiving redacted application events.", "success");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseSseBlock(block);
        if (parsed.eventName !== "log" || !parsed.data) continue;
        try {
          acceptLog(JSON.parse(parsed.data), parsed.eventId);
        } catch (_error) {
          continue;
        }
      }
    }
    if (!controller.signal.aborted) throw new Error("Log stream closed unexpectedly.");
  } catch (error) {
    if (controller.signal.aborted) return;
    if (error.unauthorized) {
      handleConnectionError(error);
      setBadge(ui.logStreamBadge, "Unauthorized", "bad");
      setNotice(ui.logNotice, error.message, "error");
      return;
    }
    setBadge(ui.logStreamBadge, "Reconnecting", "warn");
    setNotice(ui.logNotice, error.message || "Log stream interrupted.", "error");
    state.logReconnectTimer = window.setTimeout(connectLogStream, 2_000);
  }
}

function renderLogs() {
  const minimum = LEVEL_WEIGHT[ui.logLevel.value] || 20;
  const query = ui.logSearch.value.trim().toLowerCase();
  const values = state.logs.filter((item) => {
    const searchable = `${item.logger} ${item.message}`.toLowerCase();
    return (LEVEL_WEIGHT[item.level] || 20) >= minimum && (!query || searchable.includes(query));
  });
  const fragment = document.createDocumentFragment();
  for (const item of values) {
    const row = document.createElement("li");
    row.className = "log-entry";
    row.append(
      textNode("time", compactTime(item.timestamp), "log-time"),
      textNode("span", item.level, `log-level log-level-${item.level.toLowerCase()}`),
      textNode("span", item.logger, "log-logger"),
      textNode("span", item.message, "log-message"),
    );
    fragment.append(row);
  }
  ui.logList.replaceChildren(fragment);
  ui.logEmpty.hidden = values.length !== 0;
  ui.logCount.textContent = `${values.length} shown / ${state.logs.length} buffered`;
  ui.logList.scrollTop = ui.logList.scrollHeight;
}

function switchTab(name) {
  const market = name === "market";
  ui.marketPanel.hidden = !market;
  ui.logsPanel.hidden = market;
  for (const tab of ui.tabs) {
    const selected = tab.dataset.tab === name;
    tab.classList.toggle("is-active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  if (!market && state.apiKey && !state.logController) connectLogStream();
}

ui.credentialForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.apiKey = ui.apiKey.value.trim();
  connect();
});
ui.forgetKey.addEventListener("click", () => {
  sessionStorage.removeItem(SESSION_KEY);
  state.apiKey = "";
  ui.apiKey.value = "";
  stopRefreshTimers();
  stopLogStream();
  clearDashboardData();
  setBadge(ui.connectionBadge, "Not connected", "neutral");
  setNotice(ui.marketNotice, "Enter an API key to load market data.");
});
ui.themeToggle.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});
for (const tab of ui.tabs) {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = ui.tabs.indexOf(tab);
    let target = current;
    if (event.key === "ArrowLeft") target = (current - 1 + ui.tabs.length) % ui.tabs.length;
    if (event.key === "ArrowRight") target = (current + 1) % ui.tabs.length;
    if (event.key === "Home") target = 0;
    if (event.key === "End") target = ui.tabs.length - 1;
    ui.tabs[target].focus();
    switchTab(ui.tabs[target].dataset.tab);
  });
}
for (const control of [ui.search, ui.assetFilter, ui.statusFilter]) {
  control.addEventListener("input", renderMarket);
}
ui.sortField.addEventListener("change", () => {
  selectSortField(ui.sortField.value);
});
ui.sortDirection.addEventListener("click", () => {
  state.ascending = !state.ascending;
  updateSortControls();
  renderMarket();
});
for (const header of ui.sortHeaders) {
  header.addEventListener("click", () => {
    selectSortField(header.dataset.sortField, { toggleIfActive: true });
  });
}
ui.refreshMarket.addEventListener("click", () => refreshQuotes());
ui.logLevel.addEventListener("change", renderLogs);
ui.logSearch.addEventListener("input", renderLogs);
ui.pauseLogs.addEventListener("click", () => {
  state.logPaused = !state.logPaused;
  ui.pauseLogs.textContent = state.logPaused ? "Resume" : "Pause";
  if (!state.logPaused) {
    state.logsWhilePaused = 0;
    ui.pausedCount.textContent = "Live view";
    renderLogs();
  } else {
    ui.pausedCount.textContent = "Paused";
  }
});
ui.clearLogs.addEventListener("click", () => {
  state.logs = [];
  state.logsWhilePaused = 0;
  renderLogs();
});
ui.reconnectLogs.addEventListener("click", connectLogStream);
window.addEventListener("beforeunload", () => {
  stopRefreshTimers();
  stopLogStream();
});

initializeTheme();
updateSortControls();
ui.apiKey.value = state.apiKey;
updateSummary();
renderMarket();
renderLogs();
if (state.apiKey) connect();
