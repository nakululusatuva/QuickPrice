"use strict";

const SESSION_KEY = "quickprice-dashboard-api-key";
const THEME_KEY = "quickprice-dashboard-theme";
const REFRESH_INTERVAL_MS = 10_000;
const LOG_LIMIT = 500;
const LEVEL_WEIGHT = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

const state = {
  apiKey: sessionStorage.getItem(SESSION_KEY) || "",
  instruments: [],
  quotes: new Map(),
  quoteErrors: new Map(),
  sortField: "symbol",
  ascending: true,
  refreshing: false,
  refreshTimer: null,
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
  refreshMarket: element("refresh-market"),
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
  return body;
}

function chunks(values, size) {
  const result = [];
  for (let index = 0; index < values.length; index += size) {
    result.push(values.slice(index, index + size));
  }
  return result;
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

function sortValue(item) {
  const quote = item.quote;
  const values = {
    symbol: item.instrument.symbol,
    price: quote?.price ?? null,
    change1d: quote?.changes?.["1d"]?.percent ?? null,
    updated: quote?.as_of ? new Date(quote.as_of).getTime() : null,
    assetClass: item.instrument.asset_class,
  };
  return values[state.sortField];
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
    ].filter(Boolean).join(" ").toLowerCase();
    const itemStatus = item.quote?.market_status || "unavailable";
    return (!query || haystack.includes(query))
      && (!assetClass || item.instrument.asset_class === assetClass)
      && (!status || itemStatus === status);
  });
  filtered.sort((left, right) => {
    const a = sortValue(left);
    const b = sortValue(right);
    if (a === null || a === undefined) return b === null || b === undefined ? 0 : 1;
    if (b === null || b === undefined) return -1;
    const order = typeof a === "string" ? a.localeCompare(b) : a - b;
    return state.ascending ? order : -order;
  });
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
  const fields = [
    ["Symbol", instrument.symbol],
    ["Name", instrument.name],
    ["Description", instrument.description],
    ["Base / quote", `${instrument.base} / ${instrument.quote}`],
    ["Asset class", instrument.asset_class],
    ["Asset type", instrument.asset_type],
    ["Price basis", quote?.price_basis || instrument.price_basis],
    ["Change basis", instrument.change_basis],
    ["Market status", quote?.market_status || "unavailable"],
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
  const status = item.quote?.market_status || "unavailable";
  const statusNode = textNode("span", status.toUpperCase(), "market-state");
  if (item.quote?.quality?.stale) statusNode.classList.add("is-stale");
  cell.append(statusNode, textNode("small", dateTime(item.quote?.as_of), "source-feed"));
  return cell;
}

function incomeCell(item) {
  const cell = document.createElement("td");
  const dividend = item.quote?.dividend;
  const annualYield = item.quote?.estimated_annual_yield;
  if (dividend) {
    cell.append(
      textNode("strong", percent(dividend.yield_percent), "income-value"),
      textNode("small", `Dividend - ${dividend.frequency}`, "income-label"),
    );
  }
  if (annualYield) {
    cell.append(
      textNode("strong", percent(annualYield.percent), "income-value"),
      textNode("small", annualYield.rate_type || annualYield.method, "income-label"),
    );
  }
  if (!dividend && !annualYield) cell.append(textNode("span", "-", "value-empty"));
  return cell;
}

function renderMarket() {
  const items = visibleRows();
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const row = document.createElement("tr");
    row.className = "market-row";
    const instrumentCell = document.createElement("td");
    instrumentCell.append(
      textNode("strong", item.instrument.symbol, "instrument-symbol"),
      textNode("span", item.instrument.name, "instrument-name"),
      textNode("span", `${item.instrument.asset_class} / ${item.instrument.asset_type}`, "source-feed"),
    );
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
    sourceCell.append(
      textNode("strong", item.quote?.source?.provider || "-", "source-provider"),
      textNode("span", item.quote?.source?.feed || item.error?.code || "No snapshot", "source-feed"),
      textNode(
        "span",
        item.quote?.quality?.stale ? duration(item.quote.quality.staleness_ms) : "Current",
        item.quote?.quality?.stale ? "change-negative" : "change-flat",
      ),
    );
    row.append(sourceCell);
    const actionCell = document.createElement("td");
    const inspect = textNode("button", "Inspect", "inspect-button");
    inspect.type = "button";
    inspect.setAttribute("aria-expanded", "false");
    actionCell.append(inspect);
    row.append(actionCell);

    const detail = document.createElement("tr");
    detail.className = "detail-row";
    detail.id = `instrument-detail-${item.instrument.symbol.replace(/[^A-Z0-9]/gi, "-")}`;
    detail.hidden = true;
    detail.append(buildDetails(item));
    inspect.setAttribute("aria-controls", detail.id);
    inspect.addEventListener("click", () => {
      detail.hidden = !detail.hidden;
      inspect.textContent = detail.hidden ? "Inspect" : "Close";
      inspect.setAttribute("aria-expanded", String(!detail.hidden));
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

async function refreshQuotes() {
  if (!state.apiKey || state.refreshing || state.instruments.length === 0) return;
  state.refreshing = true;
  ui.refreshMarket.disabled = true;
  try {
    const symbols = state.instruments.map((item) => item.symbol);
    const envelopes = await Promise.all(
      chunks(symbols, 100).map((group) => apiJson(
        `/v1/quotes?symbols=${encodeURIComponent(group.join(","))}`,
        { allowUnavailable: true },
      )),
    );
    state.quotes.clear();
    state.quoteErrors.clear();
    for (const envelope of envelopes) {
      for (const quote of envelope.data || []) state.quotes.set(quote.symbol, quote);
      for (const error of envelope.errors || []) {
        if (error.symbol) state.quoteErrors.set(error.symbol, error);
      }
    }
    const generated = envelopes.map((item) => item.generated_at).filter(Boolean).sort().at(-1);
    ui.lastRefresh.textContent = dateTime(generated || new Date().toISOString());
    const missing = state.instruments.length - state.quotes.size;
    setNotice(
      ui.marketNotice,
      missing ? `${state.quotes.size} priced; ${missing} unavailable. Details remain visible.` : "All registered instruments are priced.",
      missing ? "neutral" : "success",
    );
    setBadge(ui.connectionBadge, missing ? "Partial" : "Connected", missing ? "warn" : "good");
    updateSummary();
    renderMarket();
  } catch (error) {
    handleConnectionError(error);
  } finally {
    state.refreshing = false;
    ui.refreshMarket.disabled = false;
  }
}

async function connect() {
  if (!state.apiKey) return;
  setBadge(ui.connectionBadge, "Connecting", "neutral");
  setNotice(ui.marketNotice, "Loading the installed instrument catalog...");
  try {
    const envelope = await apiJson("/v1/instruments");
    state.instruments = envelope.data || [];
    sessionStorage.setItem(SESSION_KEY, state.apiKey);
    populateAssetFilter();
    await refreshQuotes();
    startRefreshTimer();
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
    stopLogStream();
    clearDashboardData();
  }
}

function startRefreshTimer() {
  window.clearInterval(state.refreshTimer);
  state.refreshTimer = window.setInterval(refreshQuotes, REFRESH_INTERVAL_MS);
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
  state.quotes.clear();
  state.quoteErrors.clear();
  state.logs = [];
  state.logCursor = null;
  state.logsWhilePaused = 0;
  populateAssetFilter();
  updateSummary();
  renderMarket();
  renderLogs();
  ui.lastRefresh.textContent = "Never";
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
  window.clearInterval(state.refreshTimer);
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
  state.sortField = ui.sortField.value;
  renderMarket();
});
ui.sortDirection.addEventListener("click", () => {
  state.ascending = !state.ascending;
  ui.sortDirection.textContent = state.ascending ? "Ascending" : "Descending";
  renderMarket();
});
ui.refreshMarket.addEventListener("click", refreshQuotes);
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
window.addEventListener("beforeunload", stopLogStream);

initializeTheme();
ui.apiKey.value = state.apiKey;
updateSummary();
renderMarket();
renderLogs();
if (state.apiKey) connect();
