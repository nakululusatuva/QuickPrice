"use strict";

const THEME_KEY = "quickprice-admin-theme";
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
  instrumentDirty: new Map(),
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
  saveInstruments: element("save-instruments"),
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
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers["Content-Type"] = "application/json";
    if (options.csrf !== false && !state.csrfToken) throw new Error("The administrator session cannot authorize changes. Sign in again.");
    if (options.csrf !== false) headers["X-CSRF-Token"] = state.csrfToken;
  }
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
}

function clearSensitiveInputs() {
  ui.adminKey.value = "";
  ui.totp.value = "";
  ui.importKeysJson.value = "";
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
  state.configuration = [];
  state.instrumentDirty.clear();
  state.configurationOriginal.clear();
  window.clearInterval(state.statisticsTimer);
  window.clearInterval(state.sessionTimer);
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
  const expiryInput = document.createElement("input");
  expiryInput.type = "datetime-local";
  expiryInput.value = toLocalDateTimeValue(item.expires_at);
  expiryInput.setAttribute("aria-label", `Expiration for ${item.name || keyIdentifier(item)}`);
  const update = textNode("button", "Update", "button button-quiet button-small");
  update.type = "button";
  update.addEventListener("click", () => updateApiKeyExpiry(item, expiryInput, update));
  if (keyStatus(item).label === "Revoked") { expiryInput.disabled = true; update.disabled = true; }
  expiryControls.append(expiryInput, update);
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

async function updateApiKeyExpiry(item, input, button) {
  button.disabled = true;
  try {
    await adminRequest(`/api-keys/${encodeURIComponent(keyIdentifier(item))}`, {
      method: "PATCH",
      body: { expires_at: toUtcDateTime(input.value) },
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
      body: { name: ui.newKeyName.value.trim(), expires_at: toUtcDateTime(ui.newKeyExpiry.value) },
    });
    const rawKey = rawKeyFromResponse(body);
    if (!rawKey) throw new Error("The server created a key but did not return its one-time value.");
    ui.createKeyDialog.close();
    ui.createKeyForm.reset();
    revealApiKey(rawKey);
    await loadApiKeys({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
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

async function loadInstruments() {
  ui.refreshInstruments.disabled = true;
  try {
    const body = await adminRequest("/instruments");
    state.instruments = normalizeInstruments(body);
    state.instrumentRevision = metadata(body, "revision");
    state.instrumentDirty.clear();
    renderInstruments();
    updateInstrumentSaveState();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    ui.refreshInstruments.disabled = false;
  }
}

function renderInstruments() {
  const query = ui.instrumentSearch.value.trim().toLowerCase();
  const filter = ui.instrumentFilter.value;
  const items = state.instruments.filter((item) => {
    const enabled = state.instrumentDirty.has(item.symbol) ? state.instrumentDirty.get(item.symbol) : item.enabled !== false;
    const matchesState = !filter || (filter === "enabled" ? enabled : !enabled);
    const matchesQuery = [item.symbol, item.name, item.asset_class, item.asset_type, item.provider, item.route].some((value) => String(value || "").toLowerCase().includes(query));
    return matchesState && matchesQuery;
  });
  const fragment = document.createDocumentFragment();
  for (const item of items) fragment.append(instrumentRow(item));
  ui.instrumentsBody.replaceChildren(fragment);
  ui.instrumentsEmpty.hidden = items.length > 0;
}

function instrumentRow(item) {
  const row = document.createElement("tr");
  const instrument = document.createElement("td");
  instrument.append(textNode("span", item.symbol, "cell-primary"), textNode("span", item.name || item.description || "Installed instrument", "cell-secondary"));
  const classification = document.createElement("td");
  classification.append(textNode("span", [item.asset_class, item.asset_type].filter(Boolean).join(" / ") || "-", "cell-primary"), textNode("span", item.underlying_asset ? `Underlying ${item.underlying_asset}` : "", "cell-secondary"));
  const route = document.createElement("td");
  route.append(textNode("span", item.provider || item.route || item.plugin || "Built-in", "cell-primary"), textNode("span", item.price_basis || "", "cell-secondary"));
  const stateCell = document.createElement("td");
  const label = document.createElement("label");
  label.className = "switch";
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = state.instrumentDirty.has(item.symbol) ? state.instrumentDirty.get(item.symbol) : item.enabled !== false;
  toggle.setAttribute("aria-label", `${toggle.checked ? "Disable" : "Enable"} ${item.symbol}`);
  const track = document.createElement("span");
  track.className = "switch-track";
  const switchLabel = textNode("span", toggle.checked ? "Enabled" : "Disabled", "cell-secondary");
  toggle.addEventListener("change", () => {
    const original = item.enabled !== false;
    if (toggle.checked === original) state.instrumentDirty.delete(item.symbol);
    else state.instrumentDirty.set(item.symbol, toggle.checked);
    switchLabel.textContent = toggle.checked ? "Enabled" : "Disabled";
    toggle.setAttribute("aria-label", `${toggle.checked ? "Disable" : "Enable"} ${item.symbol}`);
    updateInstrumentSaveState();
  });
  label.append(toggle, track, switchLabel);
  stateCell.append(label);
  row.append(instrument, classification, route, stateCell);
  return row;
}

function updateInstrumentSaveState() {
  ui.saveInstruments.disabled = state.instrumentDirty.size === 0;
}

async function saveInstruments() {
  const changes = [...state.instrumentDirty].map(([symbol, enabled]) => {
    const item = state.instruments.find((candidate) => candidate.symbol === symbol);
    return {
      symbol,
      enabled,
      quote_poll_seconds: item?.quote_poll_seconds,
      stale_after_seconds: item?.stale_after_seconds,
    };
  });
  if (!changes.length) return;
  ui.saveInstruments.disabled = true;
  try {
    await adminRequest("/instruments", { method: "PATCH", body: { revision: state.instrumentRevision, instruments: changes } });
    showToast("Instrument state saved. A service restart may be required.", "success");
    await loadInstruments();
  } catch (error) {
    showToast(error.message, "error");
    updateInstrumentSaveState();
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
  ui.createApiKey.addEventListener("click", () => ui.createKeyDialog.showModal());
  ui.importApiKeys.addEventListener("click", () => ui.importKeysDialog.showModal());
  ui.createKeyForm.addEventListener("submit", createApiKey);
  ui.importKeysForm.addEventListener("submit", importApiKeys);
  ui.copyApiKey.addEventListener("click", copyApiKey);
  ui.closeRevealKey.addEventListener("click", closeApiKeyReveal);
  ui.revealKeyDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeApiKeyReveal(); });
  for (const close of document.querySelectorAll("[data-close-dialog]")) close.addEventListener("click", () => element(close.dataset.closeDialog)?.close());
  ui.createKeyDialog.addEventListener("close", () => ui.createKeyForm.reset());
  ui.importKeysDialog.addEventListener("close", () => { ui.importKeysJson.value = ""; });
  ui.refreshProviderKeys.addEventListener("click", loadProviderKeys);
  ui.instrumentSearch.addEventListener("input", renderInstruments);
  ui.instrumentFilter.addEventListener("change", renderInstruments);
  ui.refreshInstruments.addEventListener("click", loadInstruments);
  ui.saveInstruments.addEventListener("click", saveInstruments);
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
restoreSession();
