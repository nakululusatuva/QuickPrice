"use strict";

const THEME_KEY = "quickprice-admin-theme";
const CATALOG_JOB_KEY = "quickprice-admin-catalog-job";
const STATISTICS_REFRESH_MS = 15_000;

const state = {
  csrfToken: null,
  sessionExpiresAt: null,
  activePanel: "api-keys",
  apiKeys: [],
  providerKeys: [],
  providerRevision: null,
  instruments: [],
  instrumentRevision: null,
  instrumentCatalog: null,
  instrumentEditingId: null,
  providerCatalog: [],
  catalogJobId: null,
  catalogJobLabel: null,
  catalogJobRetryCount: 0,
  catalogJobTimer: null,
  configuration: [],
  configurationRevision: null,
  configurationOriginal: new Map(),
  statisticsTimer: null,
  sessionTimer: null,
  toastTimer: null,
  confirmationResolve: null,
};

const element = (id) => document.getElementById(id);
const ui = {
  loginView: element("login-view"),
  consoleView: element("console-view"),
  loginForm: element("login-form"),
  adminKey: element("admin-key"),
  totp: element("totp"),
  loginSubmit: element("login-submit"),
  loginNotice: element("login-notice"),
  logout: element("logout-button"),
  themeToggle: element("theme-toggle"),
  sessionExpiry: element("session-expiry"),
  tabs: [...document.querySelectorAll(".admin-tab[data-panel]")],
  panels: [...document.querySelectorAll(".admin-panel")],
  globalNotice: element("global-notice"),
  keyActiveCount: element("key-active-count"),
  keyExpiringCount: element("key-expiring-count"),
  keyInactiveCount: element("key-inactive-count"),
  apiKeySearch: element("api-key-search"),
  apiKeysBody: element("api-keys-body"),
  apiKeysEmpty: element("api-keys-empty"),
  createApiKey: element("create-api-key"),
  importApiKeys: element("import-api-keys"),
  refreshApiKeys: element("refresh-api-keys"),
  createKeyDialog: element("create-key-dialog"),
  createKeyForm: element("create-key-form"),
  newKeyName: element("new-key-name"),
  newKeyValidity: element("new-key-validity"),
  newKeyExpiryField: element("new-key-expiry-field"),
  newKeyExpiry: element("new-key-expiry"),
  importKeysDialog: element("import-keys-dialog"),
  importKeysForm: element("import-keys-form"),
  importKeysJson: element("import-keys-json"),
  revealKeyDialog: element("reveal-key-dialog"),
  revealedApiKey: element("revealed-api-key"),
  copyApiKey: element("copy-api-key"),
  copyKeyStatus: element("copy-key-status"),
  closeRevealKey: element("close-reveal-key"),
  providerKeysBody: element("provider-keys-body"),
  providerKeysEmpty: element("provider-keys-empty"),
  refreshProviderKeys: element("refresh-provider-keys"),
  instrumentSearch: element("instrument-search"),
  instrumentFilter: element("instrument-filter"),
  instrumentsBody: element("instruments-body"),
  instrumentsEmpty: element("instruments-empty"),
  refreshInstruments: element("refresh-instruments"),
  createInstrument: element("create-instrument"),
  importInstrumentCatalog: element("import-instrument-catalog"),
  exportInstrumentCatalog: element("export-instrument-catalog"),
  validateInstrumentCatalog: element("validate-instrument-catalog"),
  activateInstrumentCatalog: element("activate-instrument-catalog"),
  rollbackInstrumentCatalog: element("rollback-instrument-catalog"),
  catalogActiveRevision: element("catalog-active-revision"),
  catalogStagedRevision: element("catalog-staged-revision"),
  catalogLastGoodRevision: element("catalog-last-good-revision"),
  catalogJobStatus: element("catalog-job-status"),
  catalogDiagnostics: element("catalog-diagnostics"),
  catalogDiagnosticsTitle: element("catalog-diagnostics-title"),
  catalogDiagnosticsList: element("catalog-diagnostics-list"),
  closeCatalogDiagnostics: element("close-catalog-diagnostics"),
  instrumentDialog: element("instrument-dialog"),
  instrumentForm: element("instrument-form"),
  instrumentDialogTitle: element("instrument-dialog-title"),
  instrumentId: element("instrument-id"),
  instrumentSymbol: element("instrument-symbol"),
  instrumentBase: element("instrument-base"),
  instrumentQuote: element("instrument-quote"),
  instrumentName: element("instrument-name"),
  instrumentDescription: element("instrument-description"),
  instrumentAssetClass: element("instrument-asset-class"),
  instrumentAssetType: element("instrument-asset-type"),
  instrumentPriceBasis: element("instrument-price-basis"),
  instrumentChangeBasis: element("instrument-change-basis"),
  instrumentAliases: element("instrument-aliases"),
  instrumentCalendar: element("instrument-calendar"),
  instrumentEnabled: element("instrument-enabled"),
  instrumentPoll: element("instrument-poll"),
  instrumentStale: element("instrument-stale"),
  instrumentHistoryEnabled: element("instrument-history-enabled"),
  instrumentHistoryPoll: element("instrument-history-poll"),
  instrumentHistoryDays: element("instrument-history-days"),
  routeQuote: element("route-quote"),
  routeHistory: element("route-history"),
  routeDividend: element("route-dividend"),
  routeYield: element("route-yield"),
  instrumentProviderSymbols: element("instrument-provider-symbols"),
  recommendInstrumentRoutes: element("recommend-instrument-routes"),
  instrumentCreditEstimate: element("instrument-credit-estimate"),
  providerSymbolProvider: element("provider-symbol-provider"),
  providerSymbolQuery: element("provider-symbol-query"),
  searchProviderSymbol: element("search-provider-symbol"),
  providerSymbolResults: element("provider-symbol-results"),
  instrumentYieldStrategy: element("instrument-yield-strategy"),
  instrumentDividendStrategy: element("instrument-dividend-strategy"),
  instrumentAccrualMode: element("instrument-accrual-mode"),
  instrumentUnderlying: element("instrument-underlying"),
  instrumentFredSeries: element("instrument-fred-series"),
  instrumentExpenseRatio: element("instrument-expense-ratio"),
  instrumentFallbackDays: element("instrument-fallback-days"),
  instrumentSyntheticOperation: element("instrument-synthetic-operation"),
  instrumentSyntheticInputs: element("instrument-synthetic-inputs"),
  instrumentSyntheticSkew: element("instrument-synthetic-skew"),
  instrumentSyntheticAges: element("instrument-synthetic-ages"),
  importInstrumentDialog: element("import-instrument-dialog"),
  importInstrumentForm: element("import-instrument-form"),
  importInstrumentMode: element("import-instrument-mode"),
  importInstrumentJson: element("import-instrument-json"),
  configurationForm: element("configuration-form"),
  configurationEmpty: element("configuration-empty"),
  restartBanner: element("restart-banner"),
  resetConfiguration: element("reset-configuration"),
  saveConfiguration: element("save-configuration"),
  statisticsUpdated: element("statistics-updated"),
  statsRequests: element("stats-requests"),
  statsSuccessRate: element("stats-success-rate"),
  statsP95: element("stats-p95"),
  statsFallbacks: element("stats-fallbacks"),
  providerStatisticsBody: element("provider-statistics-body"),
  providerStatisticsEmpty: element("provider-statistics-empty"),
  refreshStatistics: element("refresh-statistics"),
  auditEvents: element("audit-events"),
  auditEmpty: element("audit-empty"),
  refreshAudit: element("refresh-audit"),
  confirmDialog: element("confirm-dialog"),
  confirmTitle: element("confirm-title"),
  confirmMessage: element("confirm-message"),
  confirmCancel: element("confirm-cancel"),
  confirmAccept: element("confirm-accept"),
};

function textNode(tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = value == null ? "" : String(value);
  if (className) node.className = className;
  return node;
}

function nestedPayload(body) {
  if (body && typeof body === "object" && !Array.isArray(body) && body.data != null) {
    return body.data;
  }
  return body;
}

function collection(body, names) {
  const payload = nestedPayload(body);
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== "object") return [];
  for (const name of names) {
    if (Array.isArray(payload[name])) return payload[name];
  }
  return [];
}

function metadata(body, name, fallback = null) {
  const payload = nestedPayload(body);
  if (payload && typeof payload === "object" && !Array.isArray(payload) && payload[name] != null) {
    return payload[name];
  }
  if (body && typeof body === "object" && body[name] != null) return body[name];
  return fallback;
}

function errorMessage(body, status) {
  if (typeof body?.error?.message === "string") return body.error.message;
  if (typeof body?.detail === "string") return body.detail;
  if (typeof body?.message === "string") return body.message;
  if (Array.isArray(body?.errors)) {
    const messages = body.errors.map((item) => item?.message || item?.detail).filter(Boolean);
    if (messages.length) return messages.join("; ");
  }
  return `Request failed with status ${status}.`;
}

function updateSessionFromResponse(response, body) {
  const csrfToken = metadata(body, "csrf_token");
  const expiresAt = metadata(body, "expires_at") || response.headers.get("X-Admin-Session-Expires");
  if (typeof csrfToken === "string" && csrfToken) state.csrfToken = csrfToken;
  if (typeof expiresAt === "string" && expiresAt) state.sessionExpiresAt = expiresAt;
}

async function adminRequest(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { Accept: "application/json" };
  const readOnlyMethod = ["GET", "HEAD", "OPTIONS"].includes(method);
  const includeCsrf = options.csrf === true || (!readOnlyMethod && options.csrf !== false);
  if (!readOnlyMethod) {
    headers["Content-Type"] = "application/json";
  }
  if (includeCsrf && !state.csrfToken) throw new Error("The administrator session cannot authorize changes. Sign in again.");
  if (includeCsrf) headers["X-CSRF-Token"] = state.csrfToken;
  const response = await fetch(`/admin-api${path}`, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  let body = null;
  if (response.status !== 204) {
    try {
      body = await response.json();
    } catch (_error) {
      if (!response.ok) throw new Error(`Server returned an invalid response (${response.status}).`);
    }
  }
  if (response.status === 401 && options.handleUnauthorized !== false) endSession("Your administrator session has ended. Verify again to continue.");
  if (!response.ok) {
    const error = new Error(errorMessage(body, response.status));
    error.status = response.status;
    throw error;
  }
  updateSessionFromResponse(response, body);
  return body;
}

function setLoginNotice(message, kind = "neutral") {
  ui.loginNotice.textContent = message;
  ui.loginNotice.className = "notice";
  if (kind === "error") ui.loginNotice.classList.add("is-error");
  if (kind === "success") ui.loginNotice.classList.add("is-success");
}

function showToast(message, kind = "neutral") {
  window.clearTimeout(state.toastTimer);
  ui.globalNotice.textContent = message;
  ui.globalNotice.className = "toast";
  if (kind === "error") ui.globalNotice.classList.add("is-error");
  if (kind === "success") ui.globalNotice.classList.add("is-success");
  ui.globalNotice.hidden = false;
  state.toastTimer = window.setTimeout(() => { ui.globalNotice.hidden = true; }, 5500);
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);
  ui.themeToggle.textContent = theme === "dark" ? "Light mode" : "Dark mode";
  ui.themeToggle.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} mode`);
}

function initializeTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  applyTheme(saved || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
}

function showConsole() {
  ui.loginView.hidden = true;
  ui.consoleView.hidden = false;
  ui.adminKey.value = "";
  ui.totp.value = "";
  startSessionClock();
  selectPanel(state.activePanel, { force: true });
  resumeCatalogJob();
}

function clearSensitiveInputs() {
  ui.adminKey.value = "";
  ui.totp.value = "";
  ui.importKeysJson.value = "";
  ui.importInstrumentJson.value = "";
  ui.revealedApiKey.textContent = "";
  ui.copyKeyStatus.textContent = "";
  for (const input of document.querySelectorAll('#provider-keys-body input[type="password"]')) input.value = "";
}

function endSession(message = "Verify your administrator credentials to continue.") {
  state.csrfToken = null;
  state.sessionExpiresAt = null;
  state.apiKeys = [];
  state.providerKeys = [];
  state.instruments = [];
  state.instrumentCatalog = null;
  state.providerCatalog = [];
  state.instrumentEditingId = null;
  state.configuration = [];
  state.configurationOriginal.clear();
  window.clearInterval(state.statisticsTimer);
  window.clearInterval(state.sessionTimer);
  window.clearTimeout(state.catalogJobTimer);
  ui.consoleView.hidden = true;
  ui.loginView.hidden = false;
  clearSensitiveInputs();
  setLoginNotice(message);
  window.setTimeout(() => ui.adminKey.focus(), 0);
}

function startSessionClock() {
  window.clearInterval(state.sessionTimer);
  const update = () => {
    if (!state.sessionExpiresAt) {
      ui.sessionExpiry.textContent = "Session active";
      return;
    }
    const remaining = new Date(state.sessionExpiresAt).getTime() - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0) {
      endSession("Your administrator session expired.");
      return;
    }
    const minutes = Math.max(1, Math.ceil(remaining / 60_000));
    ui.sessionExpiry.textContent = `Session expires in ${minutes} min`;
  };
  update();
  state.sessionTimer = window.setInterval(update, 30_000);
}

async function restoreSession() {
  try {
    const body = await adminRequest("/session", { handleUnauthorized: false });
    const authenticated = metadata(body, "authenticated", true);
    if (!authenticated || !state.csrfToken) {
      endSession("Verify your administrator credentials to continue.");
      return;
    }
    showConsole();
  } catch (error) {
    if (error.status !== 401) setLoginNotice(error.message, "error");
    else endSession("Verify your administrator credentials to continue.");
  }
}

async function login(event) {
  event.preventDefault();
  const adminKey = ui.adminKey.value;
  const totp = ui.totp.value.replace(/\s+/g, "");
  if (!/^\d{6}$/.test(totp)) {
    setLoginNotice("Enter the current six-digit authenticator code.", "error");
    ui.totp.focus();
    return;
  }
  ui.loginSubmit.disabled = true;
  setLoginNotice("Verifying credentials...");
  try {
    const body = await adminRequest("/session", {
      method: "POST",
      csrf: false,
      handleUnauthorized: false,
      body: { admin_key: adminKey, totp },
    });
    if (!state.csrfToken) throw new Error("The server did not issue a CSRF token.");
    setLoginNotice("Verification complete.", "success");
    showConsole();
  } catch (error) {
    ui.adminKey.value = "";
    ui.totp.value = "";
    setLoginNotice(error.status === 401 ? "Administrator verification failed." : error.message, "error");
    ui.adminKey.focus();
  } finally {
    ui.loginSubmit.disabled = false;
  }
}

async function logout() {
  ui.logout.disabled = true;
  try {
    await adminRequest("/session", { method: "DELETE" });
  } catch (error) {
    if (error.status !== 401) showToast(error.message, "error");
  } finally {
    ui.logout.disabled = false;
    endSession("Signed out. Verify again to reopen the administration console.");
  }
}

function selectPanel(name, { force = false } = {}) {
  if (!force && state.activePanel === name) return;
  if (state.activePanel === "provider-keys" && name !== "provider-keys") {
    for (const input of document.querySelectorAll('#provider-keys-body input[type="password"]')) input.value = "";
  }
  state.activePanel = name;
  for (const tab of ui.tabs) {
    const active = tab.dataset.panel === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  for (const panel of ui.panels) panel.hidden = panel.id !== `${name}-panel`;
  window.clearInterval(state.statisticsTimer);
  const loaders = {
    "api-keys": loadApiKeys,
    "provider-keys": loadProviderKeys,
    instruments: loadInstruments,
    configuration: loadConfiguration,
    "provider-statistics": loadStatistics,
  };
  loaders[name]?.();
  if (name === "provider-statistics") {
    state.statisticsTimer = window.setInterval(() => {
      if (state.activePanel === "provider-statistics" && !document.hidden) loadStatistics({ quiet: true });
    }, STATISTICS_REFRESH_MS);
  }
}

function dateTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(parsed);
}

function compactNumber(value) {
  const number = finiteNumber(value);
  if (number == null) return "-";
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(number);
}

function percent(value) {
  const number = finiteNumber(value);
  return number == null ? "-" : `${number.toFixed(1)}%`;
}

function latency(value) {
  const milliseconds = finiteNumber(value);
  if (milliseconds == null) return "-";
  return milliseconds >= 1000 ? `${(milliseconds / 1000).toFixed(2)} s` : `${Math.round(milliseconds)} ms`;
}

function finiteNumber(value) {
  if (value == null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function toLocalDateTimeValue(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  const local = new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function toUtcDateTime(value) {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) throw new Error("Enter a valid expiration date and time.");
  return parsed.toISOString();
}

function statusPill(label, kind = "neutral") {
  const node = textNode("span", label, "status-pill");
  if (kind !== "neutral") node.classList.add(`is-${kind}`);
  return node;
}

function keyIdentifier(item) {
  return item.key_id ?? item.id ?? item.identifier;
}

function keyStatus(item) {
  if (item.revoked_at || item.revoked === true || item.status === "revoked") return { label: "Revoked", kind: "negative" };
  const expires = item.expires_at ? new Date(item.expires_at).getTime() : null;
  if (expires && expires <= Date.now()) return { label: "Expired", kind: "negative" };
  if (expires && expires <= Date.now() + 7 * 86_400_000) return { label: "Expiring", kind: "warning" };
  return { label: "Active", kind: "positive" };
}

function normalizeApiKeys(body) {
  return collection(body, ["api_keys", "keys", "items"])
    .filter((item) => item && keyIdentifier(item) != null)
    .sort((left, right) => String(left.name || "").localeCompare(String(right.name || "")));
}

async function loadApiKeys({ quiet = false } = {}) {
  if (!quiet) ui.refreshApiKeys.disabled = true;
  try {
    state.apiKeys = normalizeApiKeys(await adminRequest("/api-keys"));
    renderApiKeys();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    ui.refreshApiKeys.disabled = false;
  }
}

function renderApiKeys() {
  const query = ui.apiKeySearch.value.trim().toLowerCase();
  const items = state.apiKeys.filter((item) => [item.name, item.key_hint, item.hint, keyIdentifier(item)].some((value) => String(value || "").toLowerCase().includes(query)));
  const counts = state.apiKeys.reduce((result, item) => {
    const status = keyStatus(item).label;
    if (status === "Active") result.active += 1;
    else if (status === "Expiring") result.expiring += 1;
    else result.inactive += 1;
    return result;
  }, { active: 0, expiring: 0, inactive: 0 });
  ui.keyActiveCount.textContent = counts.active;
  ui.keyExpiringCount.textContent = counts.expiring;
  ui.keyInactiveCount.textContent = counts.inactive;
  const fragment = document.createDocumentFragment();
  for (const item of items) fragment.append(apiKeyRow(item));
  ui.apiKeysBody.replaceChildren(fragment);
  ui.apiKeysEmpty.hidden = items.length > 0;
}

function apiKeyRow(item) {
  const row = document.createElement("tr");
  const name = document.createElement("td");
  name.append(textNode("span", item.name || "Unnamed key", "cell-primary"), textNode("span", `ID ${keyIdentifier(item)}`, "cell-secondary"));
  const hint = document.createElement("td");
  hint.append(textNode("code", item.key_hint || item.hint || "Hash only", "key-hint"));
  const created = document.createElement("td");
  created.textContent = dateTime(item.created_at);
  const expiry = document.createElement("td");
  const expiryControls = document.createElement("div");
  expiryControls.className = "inline-expiry";
  const expiryMode = document.createElement("select");
  expiryMode.setAttribute("aria-label", `Validity mode for ${item.name || keyIdentifier(item)}`);
  for (const [value, label] of [["permanent", "Permanent"], ["expires", "Expires"]]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    expiryMode.append(option);
  }
  expiryMode.value = item.is_permanent || !item.expires_at ? "permanent" : "expires";
  const expiryInput = document.createElement("input");
  expiryInput.type = "datetime-local";
  expiryInput.value = toLocalDateTimeValue(item.expires_at);
  expiryInput.setAttribute("aria-label", `Expiration for ${item.name || keyIdentifier(item)}`);
  const syncExpiryMode = () => {
    const permanent = expiryMode.value === "permanent";
    expiryInput.disabled = permanent;
    expiryInput.required = !permanent;
    if (permanent) expiryInput.value = "";
  };
  expiryMode.addEventListener("change", syncExpiryMode);
  syncExpiryMode();
  const update = textNode("button", "Update", "button button-quiet button-small");
  update.type = "button";
  update.addEventListener("click", () => updateApiKeyExpiry(item, expiryMode, expiryInput, update));
  if (keyStatus(item).label === "Revoked") { expiryMode.disabled = true; expiryInput.disabled = true; update.disabled = true; }
  expiryControls.append(expiryMode, expiryInput, update);
  expiry.append(expiryControls);
  const status = document.createElement("td");
  const currentStatus = keyStatus(item);
  status.append(statusPill(currentStatus.label, currentStatus.kind));
  const actions = document.createElement("td");
  actions.className = "cell-actions";
  const revoke = textNode("button", "Revoke", "button button-danger button-small");
  revoke.type = "button";
  revoke.disabled = currentStatus.label === "Revoked";
  revoke.addEventListener("click", () => revokeApiKey(item, revoke));
  actions.append(revoke);
  row.append(name, hint, created, expiry, status, actions);
  return row;
}

async function updateApiKeyExpiry(item, mode, input, button) {
  button.disabled = true;
  try {
    if (mode.value !== "permanent" && !input.value) {
      throw new Error("Choose an expiration date and time.");
    }
    await adminRequest(`/api-keys/${encodeURIComponent(keyIdentifier(item))}`, {
      method: "PATCH",
      body: { expires_at: mode.value === "permanent" ? null : toUtcDateTime(input.value) },
    });
    showToast("API key expiration updated.", "success");
    await loadApiKeys({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function revokeApiKey(item, button) {
  const accepted = await confirmAction("Revoke API key", `Revoke "${item.name || keyIdentifier(item)}"? Existing clients using it will immediately lose access.`, "Revoke key");
  if (!accepted) return;
  button.disabled = true;
  try {
    await adminRequest(`/api-keys/${encodeURIComponent(keyIdentifier(item))}`, { method: "DELETE" });
    showToast("API key revoked.", "success");
    await loadApiKeys({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
    button.disabled = false;
  }
}

function rawKeyFromResponse(body) {
  const payload = nestedPayload(body);
  for (const candidate of [payload?.api_key, payload?.raw_key, payload?.key, body?.api_key, body?.raw_key]) {
    if (typeof candidate === "string" && candidate) return candidate;
  }
  return null;
}

async function createApiKey(event) {
  event.preventDefault();
  const submit = ui.createKeyForm.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const body = await adminRequest("/api-keys", {
      method: "POST",
      body: {
        name: ui.newKeyName.value.trim(),
        expires_at: ui.newKeyValidity.value === "permanent" ? null : toUtcDateTime(ui.newKeyExpiry.value),
      },
    });
    const rawKey = rawKeyFromResponse(body);
    if (!rawKey) throw new Error("The server created a key but did not return its one-time value.");
    ui.createKeyDialog.close();
    ui.createKeyForm.reset();
    syncNewKeyValidity();
    revealApiKey(rawKey);
    await loadApiKeys({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

function syncNewKeyValidity() {
  const permanent = ui.newKeyValidity.value === "permanent";
  ui.newKeyExpiryField.hidden = permanent;
  ui.newKeyExpiry.disabled = permanent;
  ui.newKeyExpiry.required = !permanent;
  if (permanent) ui.newKeyExpiry.value = "";
}

async function importApiKeys(event) {
  event.preventDefault();
  const submit = ui.importKeysForm.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const records = JSON.parse(ui.importKeysJson.value);
    if (!Array.isArray(records) || records.length === 0) throw new Error("Provide a non-empty JSON array.");
    for (const record of records) {
      if (!record || typeof record.name !== "string" || typeof record.api_key !== "string") throw new Error("Every record requires string name and api_key values.");
    }
    await adminRequest("/api-keys/import", { method: "POST", body: { keys: records } });
    ui.importKeysJson.value = "";
    ui.importKeysDialog.close();
    showToast(`${records.length} API key${records.length === 1 ? "" : "s"} imported.`, "success");
    await loadApiKeys({ quiet: true });
  } catch (error) {
    showToast(error instanceof SyntaxError ? "Import data must be valid JSON." : error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

function revealApiKey(rawKey) {
  ui.revealedApiKey.textContent = rawKey;
  ui.copyKeyStatus.textContent = "";
  ui.revealKeyDialog.showModal();
}

function closeApiKeyReveal() {
  ui.revealedApiKey.textContent = "";
  ui.copyKeyStatus.textContent = "";
  ui.revealKeyDialog.close();
}

async function copyApiKey() {
  const value = ui.revealedApiKey.textContent;
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    ui.copyKeyStatus.textContent = "Copied to the clipboard.";
  } catch (_error) {
    ui.copyKeyStatus.textContent = "Clipboard access was denied. Select and copy the value manually.";
  }
}

function normalizeProviderKeys(body) {
  const list = collection(body, ["provider_keys", "keys", "items"]);
  if (list.length) return list;
  const payload = nestedPayload(body);
  const values = payload?.values;
  if (!values || typeof values !== "object" || Array.isArray(values)) return [];
  return Object.entries(values).map(([name, configured]) => ({ name, configured }));
}

async function loadProviderKeys() {
  ui.refreshProviderKeys.disabled = true;
  try {
    const body = await adminRequest("/provider-keys");
    state.providerKeys = normalizeProviderKeys(body);
    state.providerRevision = metadata(body, "revision");
    renderProviderKeys();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    ui.refreshProviderKeys.disabled = false;
  }
}

function renderProviderKeys() {
  const fragment = document.createDocumentFragment();
  for (const item of state.providerKeys) fragment.append(providerKeyRow(item));
  ui.providerKeysBody.replaceChildren(fragment);
  ui.providerKeysEmpty.hidden = state.providerKeys.length > 0;
}

function providerKeyRow(item) {
  const row = document.createElement("tr");
  const provider = document.createElement("td");
  provider.append(textNode("span", item.provider || item.group || item.label || "Provider", "cell-primary"), textNode("span", item.description || item.source || "Managed credential", "cell-secondary"));
  const credential = document.createElement("td");
  credential.append(textNode("code", item.name || item.key || "Unknown", "key-hint"));
  const status = document.createElement("td");
  status.append(statusPill(item.configured ? "Configured" : "Not configured", item.configured ? "positive" : "warning"));
  const replacement = document.createElement("td");
  const controls = document.createElement("div");
  controls.className = "provider-secret";
  const input = document.createElement("input");
  input.type = "password";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.placeholder = "Enter replacement";
  input.setAttribute("aria-label", `Replacement value for ${item.name}`);
  input.disabled = item.editable === false;
  controls.append(input);
  replacement.append(controls);
  const actions = document.createElement("td");
  actions.className = "cell-actions";
  const save = textNode("button", "Replace", "button button-primary button-small");
  save.type = "button";
  save.disabled = item.editable === false;
  save.addEventListener("click", () => replaceProviderKey(item, input, save));
  const clear = textNode("button", "Clear", "button button-danger button-small");
  clear.type = "button";
  clear.disabled = !item.configured || item.editable === false;
  clear.addEventListener("click", () => clearProviderKey(item, clear));
  actions.append(save, clear);
  row.append(provider, credential, status, replacement, actions);
  return row;
}

async function replaceProviderKey(item, input, button) {
  const value = input.value;
  if (!value) {
    showToast("Enter a replacement value.", "error");
    input.focus();
    return;
  }
  button.disabled = true;
  try {
    await adminRequest("/provider-keys", { method: "PATCH", body: { revision: state.providerRevision, values: { [item.name]: value } } });
    input.value = "";
    showToast("Provider credential replaced. The plaintext value has been removed from the page.", "success");
    await loadProviderKeys();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    input.value = "";
    button.disabled = false;
  }
}

async function clearProviderKey(item, button) {
  const accepted = await confirmAction("Clear provider credential", `Clear ${item.name}? The provider route may become unavailable after restart.`, "Clear credential");
  if (!accepted) return;
  button.disabled = true;
  try {
    await adminRequest("/provider-keys", { method: "PATCH", body: { revision: state.providerRevision, values: { [item.name]: null } } });
    showToast("Provider credential cleared.", "success");
    await loadProviderKeys();
  } catch (error) {
    showToast(error.message, "error");
    button.disabled = false;
  }
}

function normalizeInstruments(body) {
  return collection(body, ["instruments", "items"])
    .filter((item) => item && typeof item.symbol === "string")
    .sort((left, right) => left.symbol.localeCompare(right.symbol));
}

function catalogGeneration(body, name) {
  const payload = nestedPayload(body);
  const generation = payload?.[name];
  if (generation && Array.isArray(generation.instruments)) return generation;
  if (name === "active" && Array.isArray(payload?.instruments)) {
    return { revision: payload.active_revision || payload.revision, instruments: payload.instruments };
  }
  return null;
}

function shortRevision(value) {
  return typeof value === "string" && value ? value.slice(0, 12) : "-";
}

async function loadInstruments() {
  ui.refreshInstruments.disabled = true;
  try {
    const body = await adminRequest("/instrument-catalog");
    const active = catalogGeneration(body, "active");
    const staged = catalogGeneration(body, "staged");
    const lastKnownGood = catalogGeneration(body, "last_known_good");
    const visible = staged || active || body;
    state.instruments = normalizeInstruments(visible);
    state.instrumentRevision = metadata(body, "revision");
    state.instrumentCatalog = { active, staged, lastKnownGood };
    renderCatalogStatus();
    renderInstruments();
    await loadProviderCatalog({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    ui.refreshInstruments.disabled = false;
  }
}

function renderCatalogStatus() {
  const { active, staged, lastKnownGood } = state.instrumentCatalog || {};
  ui.catalogActiveRevision.textContent = shortRevision(active?.revision);
  ui.catalogActiveRevision.title = active?.revision || "";
  ui.catalogStagedRevision.textContent = staged ? shortRevision(staged.revision) : "No draft";
  ui.catalogStagedRevision.title = staged?.revision || "";
  ui.catalogLastGoodRevision.textContent = shortRevision(lastKnownGood?.revision);
  ui.catalogLastGoodRevision.title = lastKnownGood?.revision || "";
  ui.validateInstrumentCatalog.disabled = !staged;
  ui.activateInstrumentCatalog.disabled = !staged || Boolean(state.catalogJobId);
  ui.rollbackInstrumentCatalog.disabled = !lastKnownGood || lastKnownGood.revision === active?.revision || Boolean(state.catalogJobId);
  if (!state.catalogJobId) ui.catalogJobStatus.textContent = staged ? "Draft ready" : "Idle";
}

function renderInstruments() {
  const query = ui.instrumentSearch.value.trim().toLowerCase();
  const filter = ui.instrumentFilter.value;
  const items = state.instruments.filter((item) => {
    const enabled = item.enabled !== false && item.archived !== true;
    const ownership = item.ownership || "builtin";
    const matchesState = !filter
      || (filter === "enabled" && enabled)
      || (filter === "disabled" && !enabled && !item.archived)
      || (filter === "custom" && ownership === "custom")
      || (filter === "builtin" && ownership === "builtin")
      || (filter === "archived" && item.archived === true);
    const routeText = (item.routes || []).flatMap((route) => [route.capability, ...(route.providers || [])]).join(" ");
    const matchesQuery = [item.id, item.symbol, item.name, item.asset_class, item.asset_type, routeText].some((value) => String(value || "").toLowerCase().includes(query));
    return matchesState && matchesQuery;
  });
  const fragment = document.createDocumentFragment();
  for (const item of items) fragment.append(instrumentRow(item));
  ui.instrumentsBody.replaceChildren(fragment);
  ui.instrumentsEmpty.hidden = items.length > 0;
}

function instrumentRow(item) {
  const row = document.createElement("tr");
  if (item.archived) row.classList.add("is-archived");
  const instrument = document.createElement("td");
  instrument.append(
    textNode("span", item.symbol, "cell-primary"),
    textNode("span", item.name || item.description || "Managed instrument", "cell-secondary"),
    textNode("code", item.id || "-", "catalog-id"),
  );
  const classification = document.createElement("td");
  classification.append(
    textNode("span", [item.asset_class, item.asset_type].filter(Boolean).join(" / ") || "-", "cell-primary"),
    textNode("span", item.income?.underlying_asset ? `Underlying ${item.income.underlying_asset}` : item.market_calendar || "", "cell-secondary"),
  );
  const route = document.createElement("td");
  const routes = item.routes || [];
  if (routes.length) {
    for (const capabilityRoute of routes) {
      route.append(textNode("span", `${capabilityRoute.capability}: ${(capabilityRoute.providers || []).join(" > ")}`, "route-line"));
    }
  } else if (item.synthetic) {
    route.append(textNode("span", `${item.synthetic.operation}: ${item.synthetic.inputs.join(" / ")}`, "route-line"));
  } else {
    route.append(textNode("span", "Automatic recommended route", "cell-secondary"));
  }
  const policy = document.createElement("td");
  policy.append(
    textNode("span", `${item.quote_poll_seconds ?? "-"}s polling`, "cell-primary"),
    textNode("span", `${item.stale_after_seconds ?? "-"}s stale - history ${item.history?.enabled === false ? "off" : "on"}`, "cell-secondary"),
  );
  const stateCell = document.createElement("td");
  const label = document.createElement("label");
  label.className = "switch";
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = item.enabled !== false && item.archived !== true;
  toggle.disabled = item.archived === true;
  toggle.setAttribute("aria-label", `${toggle.checked ? "Disable" : "Enable"} ${item.symbol}`);
  const track = document.createElement("span");
  track.className = "switch-track";
  const switchLabel = textNode("span", item.archived ? "Archived" : toggle.checked ? "Enabled" : "Disabled", "cell-secondary");
  toggle.addEventListener("change", async () => {
    toggle.disabled = true;
    try {
      await stageInstrumentUpdate(item.id, { enabled: toggle.checked });
      showToast(`${item.symbol} ${toggle.checked ? "enabled" : "disabled"} in the staged catalog.`, "success");
    } catch (error) {
      toggle.checked = !toggle.checked;
      toggle.disabled = false;
      showToast(error.message, "error");
    }
  });
  label.append(toggle, track, switchLabel);
  stateCell.append(label, statusPill(item.ownership || "builtin", item.ownership === "custom" ? "warning" : "neutral"));
  const actions = document.createElement("td");
  actions.className = "cell-actions";
  const edit = textNode("button", "Edit", "button button-quiet button-small");
  edit.type = "button";
  edit.addEventListener("click", () => openInstrumentEditor(item));
  actions.append(edit);
  if ((item.ownership || "builtin") === "custom") {
    const archive = textNode("button", item.archived ? "Restore" : "Archive", item.archived ? "button button-secondary button-small" : "button button-danger button-small");
    archive.type = "button";
    archive.addEventListener("click", () => item.archived ? restoreInstrument(item, archive) : archiveInstrument(item, archive));
    actions.append(archive);
  }
  row.append(instrument, classification, route, policy, stateCell, actions);
  return row;
}

async function stageInstrumentUpdate(instrumentId, changes) {
  const body = await adminRequest(`/instrument-catalog/instruments/${encodeURIComponent(instrumentId)}`, {
    method: "PATCH",
    body: { revision: state.instrumentRevision, changes },
  });
  applyCatalogResponse(body);
  return body;
}

function applyCatalogResponse(body) {
  const active = catalogGeneration(body, "active");
  const staged = catalogGeneration(body, "staged");
  const lastKnownGood = catalogGeneration(body, "last_known_good");
  state.instrumentRevision = metadata(body, "revision", state.instrumentRevision);
  state.instrumentCatalog = {
    active: active || state.instrumentCatalog?.active || null,
    staged,
    lastKnownGood: lastKnownGood || state.instrumentCatalog?.lastKnownGood || null,
  };
  const visible = staged || active;
  if (visible) state.instruments = normalizeInstruments(visible);
  renderCatalogStatus();
  renderInstruments();
}

function splitList(value, { lower = false, upper = false, maximum = 32 } = {}) {
  let items = value.split(",").map((item) => item.trim()).filter(Boolean);
  if (lower) items = items.map((item) => item.toLowerCase());
  if (upper) items = items.map((item) => item.toUpperCase());
  if (items.length > maximum || new Set(items).size !== items.length) throw new Error("List contains duplicates or exceeds its limit.");
  return items;
}

function nullableNumber(input, { integer = false } = {}) {
  if (!input.value.trim()) return null;
  const value = integer ? Number.parseInt(input.value, 10) : Number(input.value);
  if (!Number.isFinite(value)) throw new Error(`Enter a valid number for ${input.labels?.[0]?.textContent || "the field"}.`);
  return value;
}

function routesFromForm() {
  const controls = {
    quote: ui.routeQuote,
    history: ui.routeHistory,
    dividend: ui.routeDividend,
    yield: ui.routeYield,
  };
  return Object.entries(controls).flatMap(([capability, control]) => {
    const providers = splitList(control.value, { lower: true, maximum: 4 });
    return providers.length ? [{ capability, providers }] : [];
  });
}

function providerSymbolsFromForm() {
  const bindings = [];
  const providers = new Set();
  for (const rawLine of ui.instrumentProviderSymbols.value.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    if (separator <= 0 || separator === line.length - 1) throw new Error("Each vendor binding must use provider=symbol.");
    const provider = line.slice(0, separator).trim().toLowerCase();
    const symbol = line.slice(separator + 1).trim();
    if (providers.has(provider)) throw new Error(`Provider ${provider} has more than one vendor symbol.`);
    providers.add(provider);
    bindings.push({ provider, symbol });
  }
  return bindings;
}

function treasurySeriesConstraint(strategy, currentSeries) {
  const legacy = strategy === "treasury_3m_proxy_minus_expense";
  return {
    legacy,
    series: legacy ? "DGS3MO" : currentSeries,
  };
}

function syncTreasurySeriesConstraint() {
  const constraint = treasurySeriesConstraint(
    ui.instrumentYieldStrategy.value,
    ui.instrumentFredSeries.value,
  );
  ui.instrumentFredSeries.value = constraint.series;
  for (const option of ui.instrumentFredSeries.options) {
    option.disabled = constraint.legacy && option.value !== "DGS3MO";
  }
  ui.instrumentFredSeries.title = constraint.legacy
    ? "The legacy three-month Treasury strategy requires DGS3MO."
    : "Select a controlled FRED Treasury maturity for the Treasury proxy strategy.";
}

function incomeFromForm() {
  syncTreasurySeriesConstraint();
  const income = {
    yield_strategy: ui.instrumentYieldStrategy.value || null,
    dividend_strategy: ui.instrumentDividendStrategy.value.trim().toLowerCase() || null,
    reward_accrual_mode: ui.instrumentAccrualMode.value || null,
    underlying_asset: ui.instrumentUnderlying.value.trim().toUpperCase() || null,
    fred_series: ui.instrumentFredSeries.value || null,
    expense_ratio_percent: nullableNumber(ui.instrumentExpenseRatio),
    fallback_ratio_days: nullableNumber(ui.instrumentFallbackDays, { integer: true }),
  };
  return Object.values(income).some((value) => value != null) ? income : null;
}

function syntheticFromForm() {
  const operation = ui.instrumentSyntheticOperation.value;
  if (!operation) return null;
  const ages = splitList(ui.instrumentSyntheticAges.value).map((value) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) throw new Error("Synthetic input ages must be numbers.");
    return parsed;
  });
  return {
    operation,
    inputs: splitList(ui.instrumentSyntheticInputs.value, { upper: true, maximum: 2 }),
    max_skew_seconds: nullableNumber(ui.instrumentSyntheticSkew) ?? 2,
    input_max_age_seconds: ages,
  };
}

function instrumentFromForm() {
  return {
    id: ui.instrumentId.value.trim().toLowerCase(),
    symbol: ui.instrumentSymbol.value.trim().toUpperCase().replace("/", ":"),
    base: ui.instrumentBase.value.trim().toUpperCase(),
    quote: ui.instrumentQuote.value.trim().toUpperCase(),
    name: ui.instrumentName.value.trim(),
    description: ui.instrumentDescription.value.trim(),
    asset_class: ui.instrumentAssetClass.value,
    asset_type: ui.instrumentAssetType.value.trim().toLowerCase(),
    price_basis: ui.instrumentPriceBasis.value.trim().toLowerCase(),
    change_basis: ui.instrumentChangeBasis.value.trim().toLowerCase(),
    ownership: state.instrumentEditingId
      ? state.instruments.find((item) => item.id === state.instrumentEditingId)?.ownership || "custom"
      : "custom",
    enabled: ui.instrumentEnabled.checked,
    archived: state.instrumentEditingId
      ? state.instruments.find((item) => item.id === state.instrumentEditingId)?.archived === true
      : false,
    aliases: splitList(ui.instrumentAliases.value, { upper: true }),
    market_calendar: ui.instrumentCalendar.value,
    quote_poll_seconds: nullableNumber(ui.instrumentPoll),
    stale_after_seconds: nullableNumber(ui.instrumentStale),
    history: {
      enabled: ui.instrumentHistoryEnabled.checked,
      poll_seconds: nullableNumber(ui.instrumentHistoryPoll),
      backfill_days: nullableNumber(ui.instrumentHistoryDays, { integer: true }),
    },
    routes: routesFromForm(),
    provider_symbols: providerSymbolsFromForm(),
    income: incomeFromForm(),
    synthetic: syntheticFromForm(),
  };
}

function setInstrumentCoreReadOnly(readOnly) {
  for (const control of [
    ui.instrumentId, ui.instrumentSymbol, ui.instrumentBase, ui.instrumentQuote,
    ui.instrumentName, ui.instrumentDescription, ui.instrumentAssetClass,
    ui.instrumentAssetType, ui.instrumentPriceBasis, ui.instrumentChangeBasis,
    ui.instrumentAliases, ui.instrumentCalendar, ui.instrumentProviderSymbols,
    ui.instrumentYieldStrategy, ui.instrumentDividendStrategy, ui.instrumentAccrualMode,
    ui.instrumentUnderlying, ui.instrumentFredSeries, ui.instrumentExpenseRatio,
    ui.instrumentFallbackDays, ui.instrumentSyntheticOperation,
    ui.instrumentSyntheticInputs, ui.instrumentSyntheticSkew, ui.instrumentSyntheticAges,
  ]) control.disabled = readOnly;
  ui.instrumentId.disabled = false;
  ui.instrumentId.readOnly = true;
}

function openInstrumentEditor(item = null) {
  state.instrumentEditingId = item?.id || null;
  ui.instrumentForm.reset();
  ui.instrumentDialogTitle.textContent = item ? `Edit ${item.symbol}` : "Add instrument";
  const definition = item || {
    id: "", symbol: "", base: "", quote: "", name: "", description: "",
    asset_class: "crypto", asset_type: "spot_crypto", price_basis: "market_price",
    change_basis: "unadjusted_market_price", aliases: [], market_calendar: "always_open",
    enabled: true, quote_poll_seconds: 5, stale_after_seconds: 10,
    history: { enabled: true, poll_seconds: null, backfill_days: null },
    routes: [], provider_symbols: [], income: null, synthetic: null,
  };
  ui.instrumentId.value = definition.id || "";
  ui.instrumentSymbol.value = definition.symbol || "";
  ui.instrumentBase.value = definition.base || "";
  ui.instrumentQuote.value = definition.quote || "";
  ui.instrumentName.value = definition.name || "";
  ui.instrumentDescription.value = definition.description || "";
  ui.instrumentAssetClass.value = definition.asset_class || "crypto";
  ui.instrumentAssetType.value = definition.asset_type || "spot_crypto";
  ui.instrumentPriceBasis.value = definition.price_basis || "market_price";
  ui.instrumentChangeBasis.value = definition.change_basis || "unadjusted_market_price";
  ui.instrumentAliases.value = (definition.aliases || []).join(", ");
  ui.instrumentCalendar.value = definition.market_calendar || "always_open";
  ui.instrumentEnabled.checked = definition.enabled !== false;
  ui.instrumentPoll.value = definition.quote_poll_seconds ?? 5;
  ui.instrumentStale.value = definition.stale_after_seconds ?? 10;
  ui.instrumentHistoryEnabled.checked = definition.history?.enabled !== false;
  ui.instrumentHistoryPoll.value = definition.history?.poll_seconds ?? "";
  ui.instrumentHistoryDays.value = definition.history?.backfill_days ?? "";
  const routes = new Map((definition.routes || []).map((route) => [route.capability, route.providers || []]));
  ui.routeQuote.value = (routes.get("quote") || []).join(", ");
  ui.routeHistory.value = (routes.get("history") || []).join(", ");
  ui.routeDividend.value = (routes.get("dividend") || []).join(", ");
  ui.routeYield.value = (routes.get("yield") || []).join(", ");
  ui.instrumentProviderSymbols.value = (definition.provider_symbols || []).map((binding) => `${binding.provider}=${binding.symbol}`).join("\n");
  ui.instrumentYieldStrategy.value = definition.income?.yield_strategy || "";
  ui.instrumentDividendStrategy.value = definition.income?.dividend_strategy || "";
  ui.instrumentAccrualMode.value = definition.income?.reward_accrual_mode || "";
  ui.instrumentUnderlying.value = definition.income?.underlying_asset || "";
  ui.instrumentFredSeries.value = definition.income?.fred_series || "";
  syncTreasurySeriesConstraint();
  ui.instrumentExpenseRatio.value = definition.income?.expense_ratio_percent ?? "";
  ui.instrumentFallbackDays.value = definition.income?.fallback_ratio_days ?? "";
  ui.instrumentSyntheticOperation.value = definition.synthetic?.operation || "";
  ui.instrumentSyntheticInputs.value = (definition.synthetic?.inputs || []).join(", ");
  ui.instrumentSyntheticSkew.value = definition.synthetic?.max_skew_seconds ?? 2;
  ui.instrumentSyntheticAges.value = (definition.synthetic?.input_max_age_seconds || []).map((age) => age ?? "").join(", ");
  setInstrumentCoreReadOnly(definition.ownership === "builtin");
  updateCreditEstimate();
  ui.providerSymbolResults.replaceChildren();
  ui.instrumentDialog.showModal();
}

function changedInstrumentFields(original, current) {
  const editable = original.ownership === "builtin"
    ? new Set(["enabled", "quote_poll_seconds", "stale_after_seconds", "history", "routes"])
    : new Set(Object.keys(current).filter((name) => !["id", "ownership"].includes(name)));
  return Object.fromEntries(
    [...editable]
      .filter((name) => JSON.stringify(original[name]) !== JSON.stringify(current[name]))
      .map((name) => [name, current[name]]),
  );
}

async function saveInstrument(event) {
  event.preventDefault();
  if (!ui.instrumentForm.reportValidity()) return;
  const submit = ui.instrumentForm.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const definition = instrumentFromForm();
    let body;
    if (state.instrumentEditingId) {
      const original = state.instruments.find((item) => item.id === state.instrumentEditingId);
      if (!original) throw new Error("The instrument changed while the editor was open. Refresh and try again.");
      const changes = changedInstrumentFields(original, definition);
      if (!Object.keys(changes).length) {
        ui.instrumentDialog.close();
        return;
      }
      body = await adminRequest(`/instrument-catalog/instruments/${encodeURIComponent(original.id)}`, {
        method: "PATCH", body: { revision: state.instrumentRevision, changes },
      });
    } else {
      delete definition.id;
      delete definition.ownership;
      body = await adminRequest("/instrument-catalog/instruments", {
        method: "POST", body: { revision: state.instrumentRevision, instrument: definition },
      });
    }
    ui.instrumentDialog.close();
    applyCatalogResponse(body);
    showToast("Instrument definition staged. Validate the complete catalog before activation.", "success");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

async function archiveInstrument(item, button) {
  const accepted = await confirmAction("Archive instrument", `Archive ${item.symbol}? It will disappear from the public API after activation, while stored history remains subject to normal retention.`, "Archive instrument");
  if (!accepted) return;
  button.disabled = true;
  try {
    const body = await adminRequest(`/instrument-catalog/instruments/${encodeURIComponent(item.id)}`, {
      method: "DELETE", body: { revision: state.instrumentRevision },
    });
    applyCatalogResponse(body);
    showToast(`${item.symbol} archived in the staged catalog.`, "success");
  } catch (error) {
    button.disabled = false;
    showToast(error.message, "error");
  }
}

async function restoreInstrument(item, button) {
  button.disabled = true;
  try {
    await stageInstrumentUpdate(item.id, { archived: false, enabled: false });
    showToast(`${item.symbol} restored as disabled in the staged catalog.`, "success");
  } catch (error) {
    button.disabled = false;
    showToast(error.message, "error");
  }
}

function normalizeProviderCatalog(body) {
  return collection(body, ["providers", "items"])
    .filter((provider) => provider && typeof (provider.name || provider.provider) === "string")
    .map((provider) => ({ ...provider, name: provider.name || provider.provider }))
    .sort((left, right) => left.name.localeCompare(right.name));
}

async function loadProviderCatalog({ quiet = false } = {}) {
  try {
    state.providerCatalog = normalizeProviderCatalog(await adminRequest("/provider-catalog"));
    const selected = ui.providerSymbolProvider.value;
    ui.providerSymbolProvider.replaceChildren(...state.providerCatalog.map((provider) => new Option(provider.display_name || provider.name, provider.name)));
    if (state.providerCatalog.some((provider) => provider.name === selected)) ui.providerSymbolProvider.value = selected;
    updateCreditEstimate();
  } catch (error) {
    if (!quiet) showToast(error.message, "error");
  }
}

function providerSupports(provider, capability, assetClass) {
  const capabilities = provider.capabilities || [];
  const assetClasses = provider.asset_classes || [];
  return capabilities.includes(capability) && (!assetClasses.length || assetClasses.includes(assetClass));
}

function providerBindingNames(value) {
  return new Set(value.split(/\r?\n/).flatMap((line) => {
    const separator = line.indexOf("=");
    const provider = separator > 0 ? line.slice(0, separator).trim().toLowerCase() : "";
    return provider ? [provider] : [];
  }));
}

function stakingYieldCandidates(symbol, accrualMode) {
  const base = symbol.split(":", 1)[0];
  if (base === "WBETH") {
    return [
      "binance_wbeth_rate",
      "ethereum_exchange_rate",
      ...(accrualMode === "value_accruing" ? ["staking_market_ratio_proxy"] : []),
    ];
  }
  if (base === "BETH" && accrualMode === "distributed_units") return ["okx_beth_yield"];
  if (base === "STETH") return ["lido"];
  if (base === "WSTETH") {
    return [
      "lido",
      ...(accrualMode === "value_accruing" ? ["staking_market_ratio_proxy"] : []),
    ];
  }
  return accrualMode === "value_accruing" ? ["staking_market_ratio_proxy"] : [];
}

function recommendedRoutesForDraft(draft, providerCatalog) {
  const providers = new Map(providerCatalog.map((provider) => [provider.name, provider]));
  const bound = providerBindingNames(draft.providerSymbols || "");
  const assetClass = draft.assetClass;
  const [base = "", quote = ""] = draft.symbol.toUpperCase().replace("/", ":").split(":", 2);
  const autoBound = new Set();
  if (assetClass === "fx") autoBound.add("twelve_data").add("alpha_vantage");
  if (draft.fredSeries) autoBound.add("fred");
  const available = (name, capability) => {
    const provider = providers.get(name);
    if (!provider || provider.credentials_configured === false) return false;
    if (!providerSupports(provider, capability, assetClass)) return false;
    const needsBinding = provider.kind === "market_data" || name === "fred";
    return !needsBinding || bound.has(name) || autoBound.has(name);
  };
  const select = (capability, candidates) => candidates
    .filter((name) => available(name, capability))
    .slice(0, 4);

  let quoteCandidates;
  let historyCandidates;
  if (draft.synthetic) {
    quoteCandidates = ["synthetic"];
    historyCandidates = ["synthetic"];
  } else if (assetClass === "crypto") {
    quoteCandidates = ["binance", "okx", "kraken", "coingecko"];
    historyCandidates = quoteCandidates;
  } else if (assetClass === "fx") {
    const usdSpoke = base === "USD" || quote === "USD";
    quoteCandidates = usdSpoke ? ["twelve_data", "alpha_vantage"] : ["synthetic_fx"];
    historyCandidates = quoteCandidates;
  } else {
    quoteCandidates = ["alpaca", "finnhub", "twelve_data", "alpha_vantage"];
    historyCandidates = ["alpaca", "twelve_data", "alpha_vantage"];
  }

  const needsDividend = Boolean(draft.dividendStrategy)
    || draft.yieldStrategy === "latest_distribution_annualized";
  let yieldCandidates = [];
  if (assetClass === "crypto" && draft.assetType.toLowerCase().includes("staking")) {
    yieldCandidates = stakingYieldCandidates(`${base}:${quote}`, draft.accrualMode);
  } else if (
    assetClass === "bond"
    && ["treasury_proxy_minus_expense", "treasury_3m_proxy_minus_expense"]
      .includes(draft.yieldStrategy)
  ) {
    yieldCandidates = ["fred"];
  }
  return {
    quote: select("quote", quoteCandidates),
    history: draft.historyEnabled ? select("history", historyCandidates) : [],
    dividend: needsDividend ? select("dividend", ["alpaca"]) : [],
    yield: draft.yieldStrategy ? select("yield", yieldCandidates) : [],
  };
}

function recommendRoutes() {
  const routes = recommendedRoutesForDraft({
    symbol: ui.instrumentSymbol.value,
    assetClass: ui.instrumentAssetClass.value,
    assetType: ui.instrumentAssetType.value,
    historyEnabled: ui.instrumentHistoryEnabled.checked,
    providerSymbols: ui.instrumentProviderSymbols.value,
    yieldStrategy: ui.instrumentYieldStrategy.value,
    dividendStrategy: ui.instrumentDividendStrategy.value,
    accrualMode: ui.instrumentAccrualMode.value,
    fredSeries: ui.instrumentFredSeries.value,
    synthetic: Boolean(ui.instrumentSyntheticOperation.value),
  }, state.providerCatalog);
  ui.routeQuote.value = routes.quote.join(", ");
  ui.routeHistory.value = routes.history.join(", ");
  ui.routeDividend.value = routes.dividend.join(", ");
  ui.routeYield.value = routes.yield.join(", ");
  updateCreditEstimate();
  showToast(
    "Recommended only configured providers supported by the draft's current bindings and income policy.",
    "success",
  );
}

function providerCredit(provider, capability) {
  const value = provider.credit_costs ?? provider.per_call_credits ?? provider.credits_per_call ?? provider.credit_cost;
  if (value && typeof value === "object") return finiteNumber(value[capability]);
  return finiteNumber(value);
}

function updateCreditEstimate() {
  let total = 0;
  let tracked = false;
  const dailyCalls = {
    quote: 86_400 / Math.max(.25, finiteNumber(ui.instrumentPoll.value) ?? 5),
    history: 86_400 / Math.max(1, finiteNumber(ui.instrumentHistoryPoll.value) ?? 300),
    dividend: 1,
    yield: 24,
  };
  const routes = [
    ["quote", ui.routeQuote], ["history", ui.routeHistory],
    ["dividend", ui.routeDividend], ["yield", ui.routeYield],
  ];
  for (const [capability, control] of routes) {
    for (const name of control.value.split(",").map((value) => value.trim().toLowerCase()).filter(Boolean).slice(0, 4)) {
      const provider = state.providerCatalog.find((item) => item.name === name);
      const credit = provider ? providerCredit(provider, capability) : null;
      if (credit != null) { tracked = true; total += credit * dailyCalls[capability]; }
    }
  }
  ui.instrumentCreditEstimate.textContent = tracked
    ? `Pre-validation estimate: approximately ${compactNumber(total)} fallback credits per day. Validate for cache-aware provider budgets.`
    : "Pre-validation estimate unavailable. Validate the staged catalog for compiler-aware provider budgets.";
}

function providerSearchCompatibility(item, assetClass, selectedCapabilities) {
  const capabilities = Array.isArray(item?.capabilities)
    ? [...new Set(item.capabilities.map((value) => String(value).trim().toLowerCase()).filter(Boolean))]
    : [];
  const resultAssetClass = String(item?.asset_class || "").trim().toLowerCase();
  const resultAssetClasses = Array.isArray(item?.asset_classes)
    ? [...new Set(item.asset_classes.map((value) => String(value).trim().toLowerCase()).filter(Boolean))]
    : resultAssetClass ? [resultAssetClass] : [];
  const assetCompatible = !resultAssetClasses.length || resultAssetClasses.includes(assetClass);
  const missing = selectedCapabilities.filter((capability) => !capabilities.includes(capability));
  let reason = "";
  if (!assetCompatible) reason = `Result is compatible with ${resultAssetClasses.join(", ")}, not ${assetClass}.`;
  else if (!capabilities.length) reason = "The provider did not confirm any compatible capability.";
  else if (missing.length) reason = `Missing selected capabilities: ${missing.join(", ")}.`;
  return {
    capabilities,
    assetClasses: resultAssetClasses,
    compatible: assetCompatible && capabilities.length > 0 && missing.length === 0,
    reason,
  };
}

function selectedCapabilitiesForProvider(provider) {
  return [
    ["quote", ui.routeQuote],
    ["history", ui.routeHistory],
    ["dividend", ui.routeDividend],
    ["yield", ui.routeYield],
  ].flatMap(([capability, control]) => (
    control.value.split(",").map((value) => value.trim().toLowerCase()).includes(provider)
      ? [capability]
      : []
  ));
}

async function searchProviderSymbols() {
  const provider = ui.providerSymbolProvider.value;
  const query = ui.providerSymbolQuery.value.trim();
  if (!provider || !query) {
    showToast("Select a provider and enter a vendor symbol or ticker.", "error");
    return;
  }
  ui.searchProviderSymbol.disabled = true;
  try {
    const path = `/provider-catalog/${encodeURIComponent(provider)}/search?q=${encodeURIComponent(query)}&asset_class=${encodeURIComponent(ui.instrumentAssetClass.value)}&limit=20`;
    const items = collection(await adminRequest(path, { csrf: true }), ["items", "results", "symbols"]);
    const fragment = document.createDocumentFragment();
    let resultCount = 0;
    const selectedCapabilities = selectedCapabilitiesForProvider(provider);
    for (const item of items) {
      const vendorSymbol = item.vendor_symbol || item.symbol;
      if (!vendorSymbol) continue;
      const compatibility = providerSearchCompatibility(
        item,
        ui.instrumentAssetClass.value,
        selectedCapabilities,
      );
      const row = document.createElement("li");
      const details = document.createElement("div");
      details.append(textNode("strong", vendorSymbol), textNode("span", item.display_name || item.name || "Validated vendor identifier", "cell-secondary"));
      const capabilityList = document.createElement("span");
      capabilityList.className = "capability-list";
      for (const capability of compatibility.capabilities) {
        capabilityList.append(textNode("span", capability, "capability-chip"));
      }
      details.append(capabilityList);
      if (!compatibility.compatible) {
        details.append(textNode("span", compatibility.reason, "search-result-warning"));
        row.classList.add("is-incompatible");
      }
      const use = textNode("button", "Use", "button button-secondary button-small");
      use.type = "button";
      use.disabled = !compatibility.compatible;
      if (!compatibility.compatible) use.title = compatibility.reason;
      use.addEventListener("click", () => addProviderBinding(provider, vendorSymbol));
      row.append(details, use);
      fragment.append(row);
      resultCount += 1;
    }
    ui.providerSymbolResults.replaceChildren(fragment);
    if (!resultCount) ui.providerSymbolResults.append(textNode("li", "No compatible symbols found.", "cell-secondary"));
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    ui.searchProviderSymbol.disabled = false;
  }
}

function addProviderBinding(provider, symbol) {
  const lines = ui.instrumentProviderSymbols.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const replacement = `${provider}=${symbol}`;
  const index = lines.findIndex((line) => line.toLowerCase().startsWith(`${provider.toLowerCase()}=`));
  if (index >= 0) lines[index] = replacement;
  else lines.push(replacement);
  ui.instrumentProviderSymbols.value = lines.join("\n");
  showToast(`${provider} binding added to the draft.`, "success");
}

async function importInstrumentCatalog(event) {
  event.preventDefault();
  const submit = ui.importInstrumentForm.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const catalog = JSON.parse(ui.importInstrumentJson.value);
    if (!catalog || (typeof catalog !== "object" && !Array.isArray(catalog))) throw new Error("Catalog JSON must be an object or an array of definitions.");
    const body = await adminRequest("/instrument-catalog/import", {
      method: "POST",
      body: { revision: state.instrumentRevision, mode: ui.importInstrumentMode.value, catalog },
    });
    ui.importInstrumentDialog.close();
    ui.importInstrumentJson.value = "";
    applyCatalogResponse(body);
    showToast("Catalog import staged. Validate it before activation.", "success");
  } catch (error) {
    showToast(error instanceof SyntaxError ? "Catalog import must be valid JSON." : error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

async function exportInstrumentCatalog() {
  ui.exportInstrumentCatalog.disabled = true;
  try {
    const exportState = state.instrumentCatalog?.staged ? "staged" : "active";
    const body = await adminRequest(`/instrument-catalog/export?state=${exportState}`);
    const blob = new Blob([`${JSON.stringify(body, null, 2)}\n`], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `quickprice-instruments-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    ui.exportInstrumentCatalog.disabled = false;
  }
}

function summarizedSymbols(label, values) {
  if (!Array.isArray(values) || values.length === 0) return null;
  const visible = values.slice(0, 20).map(String);
  const remainder = values.length - visible.length;
  return `${label} (${values.length}): ${visible.join(", ")}${remainder ? `, plus ${remainder} more` : ""}.`;
}

function catalogDiffDiagnostics(diff) {
  if (!diff || typeof diff !== "object") return ["No staged diff is available."];
  if (diff.available !== true && diff.counts && typeof diff.counts === "object") {
    const messages = [
      `Staged diff: ${diff.counts.total || 0} active definition changes.`,
    ];
    for (const [field, label] of [
      ["added", "Added or enabled"],
      ["changed", "Definition updated"],
      ["archived_or_disabled", "Archived or disabled"],
    ]) {
      const summary = summarizedSymbols(label, diff[field]);
      if (summary) messages.push(summary);
    }
    return messages;
  }
  if (diff.available !== true) return ["No staged diff is available."];
  const messages = [
    `Staged diff: ${diff.changed_count || 0} changed and ${diff.unchanged_count || 0} unchanged definitions.`,
  ];
  for (const [field, label] of [
    ["added", "Added"], ["removed", "Removed"], ["archived", "Archived"],
    ["restored", "Restored"], ["enabled", "Enabled"], ["disabled", "Disabled"],
    ["modified", "Definition updated"],
  ]) {
    const summary = summarizedSymbols(label, diff[field]);
    if (summary) messages.push(summary);
  }
  return messages;
}

function creditPlanDiagnostics(providerRoutes) {
  const plan = providerRoutes?.credit_plan;
  if (!plan || typeof plan !== "object") return [];
  const worstCase = plan.worst_case_daily_credits || {};
  const hardCaps = plan.hard_capped_daily_credits || {};
  const budgets = plan.budgets || {};
  const providers = Object.keys(worstCase).sort();
  const messages = providers.map((provider) => {
    const parts = [
      `Provider credits - ${provider}: ${compactNumber(worstCase[provider])} worst-case per day`,
    ];
    if (Number.isFinite(hardCaps[provider])) {
      parts.push(`${compactNumber(hardCaps[provider])} daily hard cap`);
    }
    const budget = budgets[provider];
    if (budget && Number.isFinite(budget.reserved_for_fx) && budget.reserved_for_fx > 0) {
      parts.push(`${compactNumber(budget.reserved_for_fx)} reserved for FX`);
    }
    return `${parts.join("; ")}.`;
  });
  for (const assumption of Array.isArray(plan.assumptions) ? plan.assumptions.slice(0, 8) : []) {
    messages.push(`Credit assumption: ${String(assumption)}`);
  }
  return messages;
}

function diagnosticsFrom(body) {
  const payload = nestedPayload(body) || {};
  const messages = [];
  for (const field of ["diagnostics", "errors", "warnings"]) {
    if (!Array.isArray(payload[field])) continue;
    for (const item of payload[field]) {
      messages.push(typeof item === "string"
        ? item
        : item?.message || item?.detail || item?.code || "Catalog diagnostic");
    }
  }
  if (payload.error) {
    const item = payload.error;
    messages.push(typeof item === "string"
      ? item
      : item.message || item.detail || item.code || "Catalog diagnostic");
  }
  if (payload.valid === true) {
    messages.unshift("The complete staged catalog passed structural, routing, dependency, and budget validation.");
  } else if (payload.valid === false && messages.length === 0) {
    messages.push("The staged catalog is not valid.");
  }
  if (payload.diff) messages.push(...catalogDiffDiagnostics(payload.diff));
  messages.push(...creditPlanDiagnostics(payload.provider_routes));
  return messages.length ? messages : ["Catalog operation completed without additional diagnostics."];
}

function showCatalogDiagnostics(title, body) {
  ui.catalogDiagnosticsTitle.textContent = title;
  ui.catalogDiagnosticsList.replaceChildren(...diagnosticsFrom(body).map((message) => textNode("li", message)));
  ui.catalogDiagnostics.hidden = false;
}

async function validateInstrumentCatalog() {
  ui.validateInstrumentCatalog.disabled = true;
  try {
    const body = await adminRequest("/instrument-catalog/validate", {
      method: "POST", body: { revision: state.instrumentRevision },
    });
    showCatalogDiagnostics("Catalog validation passed", body);
    showToast("The staged catalog is valid and ready for warm-up.", "success");
  } catch (error) {
    showCatalogDiagnostics("Catalog validation failed", { errors: [error.message] });
    showToast(error.message, "error");
  } finally {
    renderCatalogStatus();
  }
}

async function activateInstrumentCatalog() {
  const accepted = await confirmAction("Activate staged catalog", "QuickPrice will validate and warm every changed instrument while the current generation continues serving traffic. The switch occurs only after required quote and income data are ready.", "Start warm-up");
  if (!accepted) return;
  ui.activateInstrumentCatalog.disabled = true;
  try {
    const body = await adminRequest("/instrument-catalog/activate", {
      method: "POST", body: { revision: state.instrumentRevision },
    });
    startCatalogJob(body, "Activation");
  } catch (error) {
    showCatalogDiagnostics("Activation could not start", { errors: [error.message] });
    showToast(error.message, "error");
    renderCatalogStatus();
  }
}

async function rollbackInstrumentCatalog() {
  const accepted = await confirmAction("Roll back catalog", "Warm and atomically restore the last-known-good catalog generation?", "Start rollback");
  if (!accepted) return;
  ui.rollbackInstrumentCatalog.disabled = true;
  try {
    const body = await adminRequest("/instrument-catalog/rollback", {
      method: "POST", body: { revision: state.instrumentRevision },
    });
    startCatalogJob(body, "Rollback");
  } catch (error) {
    showCatalogDiagnostics("Rollback could not start", { errors: [error.message] });
    showToast(error.message, "error");
    renderCatalogStatus();
  }
}

function startCatalogJob(body, label) {
  const jobId = metadata(body, "job_id");
  const status = String(metadata(body, "status", metadata(body, "state", jobId ? "queued" : "succeeded")));
  state.catalogJobId = jobId || null;
  state.catalogJobLabel = label;
  state.catalogJobRetryCount = 0;
  ui.catalogJobStatus.textContent = `${label}: ${status.replaceAll("_", " ")}`;
  renderCatalogStatus();
  if (!jobId) {
    clearPersistedCatalogJob();
    loadInstruments();
    return;
  }
  persistCatalogJob();
  window.clearTimeout(state.catalogJobTimer);
  state.catalogJobTimer = window.setTimeout(() => pollCatalogJob(label), 800);
}

function persistCatalogJob() {
  if (!state.catalogJobId || !state.catalogJobLabel) return;
  localStorage.setItem(CATALOG_JOB_KEY, JSON.stringify({
    job_id: state.catalogJobId,
    label: state.catalogJobLabel,
  }));
}

function clearPersistedCatalogJob() {
  localStorage.removeItem(CATALOG_JOB_KEY);
}

function clearCatalogJob() {
  window.clearTimeout(state.catalogJobTimer);
  state.catalogJobTimer = null;
  state.catalogJobId = null;
  state.catalogJobLabel = null;
  state.catalogJobRetryCount = 0;
  clearPersistedCatalogJob();
}

function resumeCatalogJob() {
  if (!state.catalogJobId) {
    try {
      const saved = JSON.parse(localStorage.getItem(CATALOG_JOB_KEY) || "null");
      const jobId = typeof saved?.job_id === "string" ? saved.job_id : "";
      const label = saved?.label === "Rollback" ? "Rollback" : saved?.label === "Activation" ? "Activation" : null;
      if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(jobId) || !label) {
        clearPersistedCatalogJob();
        return;
      }
      state.catalogJobId = jobId;
      state.catalogJobLabel = label;
      state.catalogJobRetryCount = 0;
    } catch (_error) {
      clearPersistedCatalogJob();
      return;
    }
  }
  ui.catalogJobStatus.textContent = `${state.catalogJobLabel}: reconnecting`;
  renderCatalogStatus();
  window.clearTimeout(state.catalogJobTimer);
  state.catalogJobTimer = window.setTimeout(
    () => pollCatalogJob(state.catalogJobLabel || "Activation"),
    250,
  );
}

async function pollCatalogJob(label) {
  if (!state.catalogJobId) return;
  try {
    const body = await adminRequest(`/instrument-catalog/jobs/${encodeURIComponent(state.catalogJobId)}`);
    const status = String(metadata(body, "status", metadata(body, "state", "unknown")));
    const progress = finiteNumber(metadata(body, "progress_percent"));
    state.catalogJobRetryCount = 0;
    ui.catalogJobStatus.textContent = `${label}: ${status.replaceAll("_", " ")}${progress == null ? "" : ` ${Math.round(progress)}%`}`;
    if (["succeeded", "failed", "cancelled"].includes(status)) {
      clearCatalogJob();
      showCatalogDiagnostics(`${label} ${status}`, body);
      showToast(`${label} ${status}.`, status === "succeeded" ? "success" : "error");
      await loadInstruments();
      return;
    }
    state.catalogJobTimer = window.setTimeout(() => pollCatalogJob(label), 1000);
  } catch (error) {
    if (error.status === 401) return;
    if (error.status === 404) {
      clearCatalogJob();
      showToast("The activation job is no longer available.", "error");
      renderCatalogStatus();
      return;
    }
    state.catalogJobRetryCount += 1;
    const delay = Math.min(15_000, 1000 * (2 ** Math.min(state.catalogJobRetryCount, 4)));
    ui.catalogJobStatus.textContent = `${label}: connection interrupted, retrying`;
    state.catalogJobTimer = window.setTimeout(() => pollCatalogJob(label), delay);
  }
}

function normalizeConfiguration(body) {
  const list = collection(body, ["fields", "configuration", "settings"]);
  if (list.length) return list;
  const payload = nestedPayload(body);
  const values = payload?.values;
  if (!values || typeof values !== "object" || Array.isArray(values)) return [];
  return Object.entries(values).map(([name, value]) => ({ name, value, type: typeof value }));
}

async function loadConfiguration() {
  try {
    const body = await adminRequest("/configuration");
    state.configuration = normalizeConfiguration(body).filter((field) => field.secret !== true && field.sensitive !== true);
    state.configurationRevision = metadata(body, "revision");
    state.configurationOriginal = new Map(state.configuration.map((field) => [field.name, field.value]));
    ui.restartBanner.hidden = !Boolean(metadata(body, "restart_required", false));
    renderConfiguration();
    updateConfigurationSaveState();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function configurationGroups() {
  const groups = new Map();
  for (const field of state.configuration) {
    const name = field.group || "General";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(field);
  }
  return groups;
}

function renderConfiguration() {
  const fragment = document.createDocumentFragment();
  for (const [name, fields] of configurationGroups()) {
    const group = document.createElement("section");
    group.className = "settings-group";
    group.append(textNode("h2", name), textNode("p", fields[0]?.group_description || "Validated runtime settings."));
    const grid = document.createElement("div");
    grid.className = "setting-fields";
    for (const field of fields) grid.append(configurationField(field));
    group.append(grid);
    fragment.append(group);
  }
  ui.configurationForm.replaceChildren(fragment);
  ui.configurationEmpty.hidden = state.configuration.length > 0;
}

function configurationField(field) {
  const wrapper = document.createElement("label");
  wrapper.className = "setting-field";
  if (field.wide) wrapper.classList.add("is-wide");
  wrapper.append(textNode("span", field.label || field.name));
  let control;
  const type = field.kind || field.type || typeof field.value;
  const options = field.options || field.choices;
  if (Array.isArray(options) && options.length) {
    control = document.createElement("select");
    if (field.value == null) control.add(new Option("Use application default", ""));
    for (const option of options) {
      const value = typeof option === "object" ? option.value : option;
      const label = typeof option === "object" ? option.label : option;
      control.add(new Option(String(label), String(value)));
    }
    control.value = String(field.value ?? "");
  } else if (type === "boolean" || type === "bool") {
    control = document.createElement("select");
    control.add(new Option("Use application default", ""));
    control.add(new Option("Enabled", "true"));
    control.add(new Option("Disabled", "false"));
    control.value = field.value == null ? "" : field.value ? "true" : "false";
  } else {
    control = document.createElement("input");
    control.type = ["integer", "number", "float"].includes(type) ? "number" : "text";
    if (control.type === "number") {
      if (field.minimum != null) control.min = String(field.minimum);
      if (field.maximum != null) control.max = String(field.maximum);
      control.step = field.step != null ? String(field.step) : (type === "float" ? "any" : "1");
    }
    control.value = Array.isArray(field.value) ? field.value.join(", ") : field.value == null ? "" : String(field.value);
  }
  control.dataset.settingName = field.name;
  control.dataset.settingType = type;
  control.disabled = field.read_only === true;
  control.addEventListener("input", updateConfigurationSaveState);
  wrapper.append(control);
  if (field.description) wrapper.append(textNode("small", field.description, "field-help"));
  return wrapper;
}

function configurationValue(control) {
  const type = control.dataset.settingType;
  if (!control.value.trim()) return null;
  if (type === "boolean" || type === "bool") return control.value === "true";
  if (type === "integer") {
    const value = Number.parseInt(control.value, 10);
    if (!Number.isFinite(value)) throw new Error("Enter a valid integer.");
    return value;
  }
  if (["float", "number"].includes(type)) {
    const value = Number(control.value);
    if (!Number.isFinite(value)) throw new Error("Enter a valid number.");
    return value;
  }
  if (type === "provider_list") return control.value.split(",").map((value) => value.trim()).filter(Boolean);
  return control.value;
}

function currentConfigurationValues() {
  const values = {};
  for (const control of ui.configurationForm.querySelectorAll("[data-setting-name]")) {
    if (!control.disabled) values[control.dataset.settingName] = configurationValue(control);
  }
  return values;
}

function updateConfigurationSaveState() {
  let dirty = false;
  try {
    const current = currentConfigurationValues();
    dirty = Object.entries(current).some(([name, value]) => JSON.stringify(value) !== JSON.stringify(state.configurationOriginal.get(name)));
  } catch (_error) {
    dirty = true;
  }
  ui.saveConfiguration.disabled = !dirty;
  ui.resetConfiguration.disabled = !dirty;
}

function resetConfiguration() {
  renderConfiguration();
  updateConfigurationSaveState();
}

async function saveConfiguration() {
  ui.saveConfiguration.disabled = true;
  try {
    const values = currentConfigurationValues();
    const changed = Object.fromEntries(Object.entries(values).filter(([name, value]) => JSON.stringify(value) !== JSON.stringify(state.configurationOriginal.get(name))));
    if (!Object.keys(changed).length) return;
    const body = await adminRequest("/configuration", { method: "PATCH", body: { revision: state.configurationRevision, values: changed } });
    ui.restartBanner.hidden = !Boolean(metadata(body, "restart_required", true));
    showToast("Configuration saved and validated.", "success");
    await loadConfiguration();
  } catch (error) {
    showToast(error.message, "error");
    updateConfigurationSaveState();
  }
}

function normalizeProviders(body) {
  const list = collection(body, ["providers", "statistics", "items"]);
  if (list.length) return list
    .filter((item) => item && (item.provider || item.name))
    .sort((left, right) => String(left.provider || left.name).localeCompare(String(right.provider || right.name)));
  const payload = nestedPayload(body);
  const providers = payload?.providers;
  if (!providers || typeof providers !== "object" || Array.isArray(providers)) return [];
  const flattened = [];
  for (const [provider, surfaces] of Object.entries(providers)) {
    if (!surfaces || typeof surfaces !== "object" || Array.isArray(surfaces)) continue;
    const operations = surfaces.operations || {};
    const upstream = surfaces.upstream_http || surfaces.http || {};
    const status = surfaces.status || {};
    const operationLifetime = operations.lifetime || operations.recent || {};
    const upstreamLifetime = upstream.lifetime || upstream.recent || {};
    const upstreamLatency = upstreamLifetime.latency_ms || {};
    const operationLatency = operationLifetime.latency_ms || {};
    const stream = status.stream && typeof status.stream === "object" ? status.stream : null;
    const reconnects = finiteNumber(status.websocket_reconnects) ?? 0;
    const upstreamAttempts = finiteNumber(upstreamLifetime.attempts) ?? 0;
    const streamSummary = stream
      ? `Stream ${String(stream.state || "unknown").toLowerCase()} - ${compactNumber(reconnects)} reconnects`
      : `${compactNumber(upstreamAttempts)} upstream HTTP calls`;
    const operationAt = operationLifetime.last_attempt_at;
    const upstreamAt = upstreamLifetime.last_attempt_at;
    const operationEpoch = operationAt ? Date.parse(operationAt) : Number.NaN;
    const upstreamEpoch = upstreamAt ? Date.parse(upstreamAt) : Number.NaN;
    const useUpstreamOutcome = !operationAt || (Number.isFinite(upstreamEpoch) && upstreamEpoch > operationEpoch);
    flattened.push({
      provider,
      capability: streamSummary,
      requests: operationLifetime.attempts,
      successes: operationLifetime.successful,
      success_rate: operationLifetime.success_rate,
      latency: upstreamAttempts > 0 ? upstreamLatency : operationLatency,
      last_status: useUpstreamOutcome
        ? upstreamLifetime.last_outcome || "No outcomes"
        : operationLifetime.last_outcome || "No outcomes",
      last_request_at: useUpstreamOutcome ? upstreamAt : operationAt,
      quota: status.quota || operations.quota || upstream.quota,
      fallbacks: status.fallbacks ?? operations.fallbacks ?? upstream.fallbacks,
      circuit_state: status.circuit_state ?? operations.circuit_state ?? upstream.circuit_state,
      stream,
      reconnects,
    });
  }
  return flattened.sort((left, right) => left.provider.localeCompare(right.provider));
}

function providerRequests(item) {
  return Number(item.requests ?? item.total_requests ?? item.operations ?? item.lifetime?.requests ?? item.lifetime?.attempts ?? 0);
}

function providerSuccessRate(item) {
  const direct = finiteNumber(item.success_rate);
  if (direct != null) return direct;
  const availability = finiteNumber(item.availability);
  if (availability != null) return availability;
  const requests = providerRequests(item);
  const successes = finiteNumber(item.successes ?? item.success_count ?? item.lifetime?.successes);
  return requests > 0 && successes != null ? successes * 100 / requests : null;
}

function providerLatency(item, percentile) {
  return item[`latency_${percentile}_ms`] ?? item.latency?.[`${percentile}_ms`] ?? item.latency?.[percentile] ?? item.rolling_60m?.[`latency_${percentile}_ms`];
}

function providerFallbacks(item) {
  return Number(item.fallbacks ?? item.fallback_count ?? item.lifetime?.fallbacks ?? 0);
}

function creditUsage(item) {
  const quota = item.quota || item.credits || {};
  const tracked = quota.tracked ?? quota.accounting !== "untracked";
  if (!tracked) return { tracked: false };
  const used = finiteNumber(quota.used ?? quota.consumed ?? item.credits_used);
  const limit = finiteNumber(quota.limit ?? quota.budget ?? item.credit_limit);
  const remaining = finiteNumber(quota.remaining ?? item.credits_remaining);
  const reserve = finiteNumber(quota.reserve);
  const usableRemaining = finiteNumber(quota.usable_remaining);
  const periodSeconds = finiteNumber(quota.period_seconds);
  if ([used, limit, remaining].every((value) => value == null)) {
    return { tracked: true, unavailable: true };
  }
  return {
    tracked: true,
    used,
    limit,
    remaining,
    reserve,
    usableRemaining,
    periodSeconds,
    providerReported: quota.provider_reported === true,
  };
}

async function loadStatistics({ quiet = false } = {}) {
  if (!quiet) ui.refreshStatistics.disabled = true;
  try {
    const body = await adminRequest("/provider-statistics");
    const providers = normalizeProviders(body);
    renderStatistics(providers);
    const collectedAt = metadata(body, "collected_at");
    const quotaUpdatedAt = metadata(body, "quota_updated_at");
    const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const collectedLabel = collectedAt ? timeFormat.format(new Date(collectedAt)) : timeFormat.format(new Date());
    const quotaLabel = quotaUpdatedAt ? timeFormat.format(new Date(quotaUpdatedAt)) : "not available";
    ui.statisticsUpdated.textContent = `Telemetry ${collectedLabel} - quota ${quotaLabel}`;
    await loadAudit({ quiet: true });
  } catch (error) {
    if (!quiet) showToast(error.message, "error");
  } finally {
    ui.refreshStatistics.disabled = false;
  }
}

function renderStatistics(providers) {
  const requests = providers.reduce((sum, item) => sum + providerRequests(item), 0);
  const weightedSuccesses = providers.reduce((sum, item) => {
    const rate = providerSuccessRate(item);
    return sum + (rate == null ? 0 : rate * providerRequests(item));
  }, 0);
  const p95Values = providers.map((item) => finiteNumber(providerLatency(item, "p95"))).filter((value) => value != null);
  const fallbacks = providers.reduce((sum, item) => sum + providerFallbacks(item), 0);
  ui.statsRequests.textContent = compactNumber(requests);
  ui.statsSuccessRate.textContent = requests > 0 ? percent(weightedSuccesses / requests) : "-";
  ui.statsP95.textContent = p95Values.length ? latency(Math.max(...p95Values)) : "-";
  ui.statsFallbacks.textContent = compactNumber(fallbacks);
  const fragment = document.createDocumentFragment();
  for (const item of providers) fragment.append(providerStatisticsRow(item));
  ui.providerStatisticsBody.replaceChildren(fragment);
  ui.providerStatisticsEmpty.hidden = providers.length > 0;
}

function providerStatisticsRow(item) {
  const row = document.createElement("tr");
  const provider = document.createElement("td");
  provider.append(textNode("span", item.provider || item.name, "cell-primary"), textNode("span", item.capability || item.feed || "Observed operations", "cell-secondary"));
  const availability = document.createElement("td");
  const successRate = providerSuccessRate(item);
  const normalizedRate = successRate == null ? null : successRate / 100;
  availability.append(statusPill(successRate == null ? "No samples" : percent(successRate), normalizedRate == null ? "neutral" : normalizedRate >= .99 ? "positive" : normalizedRate >= .9 ? "warning" : "negative"));
  const requests = document.createElement("td");
  requests.textContent = compactNumber(providerRequests(item));
  const responseTime = document.createElement("td");
  responseTime.className = "metric-pair";
  responseTime.append(textNode("span", `${latency(providerLatency(item, "p50"))} / ${latency(providerLatency(item, "p95"))}`, "cell-primary"));
  const sampleSize = finiteNumber(item.latency?.percentile_sample_size);
  responseTime.append(textNode("span", sampleSize == null ? "No latency samples" : `${compactNumber(sampleSize)} recent samples`, "cell-secondary"));
  const credits = document.createElement("td");
  const quota = creditUsage(item);
  if (!quota.tracked) credits.append(textNode("span", "Not locally tracked", "cell-secondary"));
  else if (quota.unavailable) credits.append(textNode("span", "Snapshot unavailable", "cell-secondary"));
  else {
    const label = quota.limit != null ? `${compactNumber(quota.used)} / ${compactNumber(quota.limit)}` : quota.remaining != null ? `${compactNumber(quota.remaining)} left` : compactNumber(quota.used);
    credits.append(textNode("span", label, "cell-primary"));
    const details = [];
    if (quota.usableRemaining != null) details.push(`${compactNumber(quota.usableRemaining)} standard remaining`);
    if (quota.reserve != null && quota.reserve > 0) details.push(`${compactNumber(quota.reserve)} reserved`);
    if (quota.periodSeconds === 60) details.push("per minute");
    else if (quota.periodSeconds === 86400) details.push("per day");
    else if (quota.periodSeconds != null) details.push(`per ${compactNumber(quota.periodSeconds)} s window`);
    details.push(quota.providerReported ? "provider reported" : "locally accounted");
    credits.append(textNode("span", details.join(" - "), "cell-secondary"));
    if (quota.used != null && quota.limit != null && quota.limit > 0) {
      const meter = document.createElement("progress");
      meter.className = "quota-meter";
      meter.max = quota.limit;
      meter.value = Math.min(quota.limit, Math.max(0, quota.used));
      meter.setAttribute("aria-label", `${quota.used} of ${quota.limit} locally accounted credits used`);
      credits.append(meter);
    }
  }
  const fallbacks = document.createElement("td");
  fallbacks.textContent = compactNumber(providerFallbacks(item));
  const circuit = document.createElement("td");
  const circuitState = String(item.circuit_state ?? item.circuit?.state ?? "unknown").toLowerCase();
  circuit.append(statusPill(circuitState, circuitState === "closed" ? "positive" : circuitState === "open" ? "negative" : "warning"));
  const outcome = document.createElement("td");
  outcome.append(textNode("span", item.last_outcome || item.last_status || "-", "cell-primary"), textNode("span", dateTime(item.last_observed_at || item.last_request_at), "cell-secondary"));
  row.append(provider, availability, requests, responseTime, credits, fallbacks, circuit, outcome);
  return row;
}

async function loadAudit({ quiet = false } = {}) {
  if (!quiet) ui.refreshAudit.disabled = true;
  try {
    const events = collection(await adminRequest("/audit-events"), ["events", "audit_events", "items"]);
    renderAudit(events);
  } catch (error) {
    if (!quiet) showToast(error.message, "error");
  } finally {
    ui.refreshAudit.disabled = false;
  }
}

function renderAudit(events) {
  const fragment = document.createDocumentFragment();
  for (const event of events) {
    const item = document.createElement("li");
    item.append(textNode("time", dateTime(event.occurred_at || event.created_at || event.timestamp), "audit-time"), textNode("span", event.actor || event.client_ip || event.remote_address || "Administrator", "audit-actor"), textNode("span", event.summary || event.action || event.event_type || "Administration event", "audit-action"));
    fragment.append(item);
  }
  ui.auditEvents.replaceChildren(fragment);
  ui.auditEmpty.hidden = events.length > 0;
}

function confirmAction(title, message, acceptLabel = "Confirm") {
  if (state.confirmationResolve) state.confirmationResolve(false);
  ui.confirmTitle.textContent = title;
  ui.confirmMessage.textContent = message;
  ui.confirmAccept.textContent = acceptLabel;
  ui.confirmDialog.showModal();
  return new Promise((resolve) => { state.confirmationResolve = resolve; });
}

function finishConfirmation(accepted) {
  ui.confirmDialog.close();
  const resolve = state.confirmationResolve;
  state.confirmationResolve = null;
  resolve?.(accepted);
}

function bindEvents() {
  ui.loginForm.addEventListener("submit", login);
  ui.logout.addEventListener("click", logout);
  ui.themeToggle.addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  for (const [index, tab] of ui.tabs.entries()) {
    tab.addEventListener("click", () => selectPanel(tab.dataset.panel));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const backward = ["ArrowUp", "ArrowLeft"].includes(event.key);
      const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? ui.tabs.length - 1 : (index + (backward ? -1 : 1) + ui.tabs.length) % ui.tabs.length;
      ui.tabs[nextIndex].focus();
      selectPanel(ui.tabs[nextIndex].dataset.panel);
    });
  }
  ui.apiKeySearch.addEventListener("input", renderApiKeys);
  ui.refreshApiKeys.addEventListener("click", () => loadApiKeys());
  ui.createApiKey.addEventListener("click", () => {
    syncNewKeyValidity();
    ui.createKeyDialog.showModal();
  });
  ui.importApiKeys.addEventListener("click", () => ui.importKeysDialog.showModal());
  ui.newKeyValidity.addEventListener("change", syncNewKeyValidity);
  ui.createKeyForm.addEventListener("submit", createApiKey);
  ui.importKeysForm.addEventListener("submit", importApiKeys);
  ui.copyApiKey.addEventListener("click", copyApiKey);
  ui.closeRevealKey.addEventListener("click", closeApiKeyReveal);
  ui.revealKeyDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeApiKeyReveal(); });
  for (const close of document.querySelectorAll("[data-close-dialog]")) close.addEventListener("click", () => element(close.dataset.closeDialog)?.close());
  ui.createKeyDialog.addEventListener("close", () => {
    ui.createKeyForm.reset();
    syncNewKeyValidity();
  });
  ui.importKeysDialog.addEventListener("close", () => { ui.importKeysJson.value = ""; });
  ui.refreshProviderKeys.addEventListener("click", loadProviderKeys);
  ui.instrumentSearch.addEventListener("input", renderInstruments);
  ui.instrumentFilter.addEventListener("change", renderInstruments);
  ui.refreshInstruments.addEventListener("click", loadInstruments);
  ui.createInstrument.addEventListener("click", () => openInstrumentEditor());
  ui.instrumentForm.addEventListener("submit", saveInstrument);
  ui.instrumentDialog.addEventListener("close", () => {
    state.instrumentEditingId = null;
    setInstrumentCoreReadOnly(false);
    ui.instrumentForm.reset();
    ui.providerSymbolResults.replaceChildren();
  });
  ui.importInstrumentCatalog.addEventListener("click", () => ui.importInstrumentDialog.showModal());
  ui.importInstrumentForm.addEventListener("submit", importInstrumentCatalog);
  ui.importInstrumentDialog.addEventListener("close", () => { ui.importInstrumentJson.value = ""; });
  ui.exportInstrumentCatalog.addEventListener("click", exportInstrumentCatalog);
  ui.validateInstrumentCatalog.addEventListener("click", validateInstrumentCatalog);
  ui.activateInstrumentCatalog.addEventListener("click", activateInstrumentCatalog);
  ui.rollbackInstrumentCatalog.addEventListener("click", rollbackInstrumentCatalog);
  ui.closeCatalogDiagnostics.addEventListener("click", () => { ui.catalogDiagnostics.hidden = true; });
  ui.recommendInstrumentRoutes.addEventListener("click", recommendRoutes);
  ui.searchProviderSymbol.addEventListener("click", searchProviderSymbols);
  ui.instrumentYieldStrategy.addEventListener("change", syncTreasurySeriesConstraint);
  for (const control of [ui.routeQuote, ui.routeHistory, ui.routeDividend, ui.routeYield, ui.instrumentPoll, ui.instrumentHistoryPoll]) {
    control.addEventListener("input", updateCreditEstimate);
  }
  ui.instrumentSymbol.addEventListener("change", () => {
    const [base, quote] = ui.instrumentSymbol.value.trim().toUpperCase().replace("/", ":").split(":");
    if (base && quote) {
      ui.instrumentSymbol.value = `${base}:${quote}`;
      if (!ui.instrumentBase.value) ui.instrumentBase.value = base;
      if (!ui.instrumentQuote.value) ui.instrumentQuote.value = quote;
    }
  });
  ui.resetConfiguration.addEventListener("click", resetConfiguration);
  ui.saveConfiguration.addEventListener("click", saveConfiguration);
  ui.refreshStatistics.addEventListener("click", () => loadStatistics());
  ui.refreshAudit.addEventListener("click", () => loadAudit());
  ui.confirmCancel.addEventListener("click", () => finishConfirmation(false));
  ui.confirmAccept.addEventListener("click", () => finishConfirmation(true));
  ui.confirmDialog.addEventListener("cancel", (event) => { event.preventDefault(); finishConfirmation(false); });
  window.addEventListener("pagehide", () => {
    state.csrfToken = null;
    clearSensitiveInputs();
  });
}

initializeTheme();
bindEvents();
syncNewKeyValidity();
restoreSession();
