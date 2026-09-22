
"use strict";

/* =====================================================================
 * Oasis Agent · Observation Console
 *
 * Shared transport, formatting and rendering for the dependency-free console:
 *
 *   utilities -> formatters -> api client -> renderers
 *              -> view registry -> navigation
 *              -> websocket / polling -> action & form registries
 *
 * Extensibility contract: adding a console section means (1) adding the tab
 * markup and (2) registering a view (plus any actions/forms). The core never
 * changes. Every dynamic value is escaped before entering the DOM; only
 * whitelisted helpers may emit markup.
 * ===================================================================== */

/* ---------- constants ---------- */
const FETCH_TIMEOUT_MS = 15000;
const POLL_SECONDS = 30;
const WS_RECONNECT_SECONDS = 5000;
const RETRY_BASE_MS = 400;
const RETRY_MAX_MS = 3200;
const RETRY_ATTEMPTS = 2;
const TOKEN_STORAGE_KEY = "oasis_ops_token";
const CHART_MAX_POINTS = 80;

/* ---------- tiny DOM / string helpers ---------- */
const $ = (id) => document.getElementById(id);
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

function short(value, n) {
  const s = String(value == null ? "" : value);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/* Debounce a function; only the trailing invocation runs. */
function debounce(fn, waitMs) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...args); }, waitMs);
  };
}

/* ---------- Intl formatters (locale-aware, cached) ---------- */
const dateTimeFmt = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "medium" });
const numberFmt = new Intl.NumberFormat("zh-CN");

function fmtTime(value) {
  const ts = typeof value === "number" ? value * 1000 : Date.parse(String(value ?? ""));
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? "–" : dateTimeFmt.format(date);
}

function fmtInt(value) {
  if (typeof value === "string" && /^-?\d+$/.test(value)) return numberFmt.format(BigInt(value));
  const n = Number(value);
  return value != null && Number.isSafeInteger(n) ? numberFmt.format(n) : String(value ?? "–");
}

// JSON.parse rounds EVM-sized integers before a reviver can inspect them.
// Quote unsafe integer tokens while leaving JSON strings untouched. Financial
// values remain decimal strings in presentation; no browser arithmetic settles funds.
function parseJsonExact(text) {
  return JSON.parse(text.replace(/"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g, token => {
    if (token.startsWith('"') || !/^-?(0|[1-9]\d*)$/.test(token)) return token;
    const value = BigInt(token);
    return value > BigInt(Number.MAX_SAFE_INTEGER) || value < BigInt(Number.MIN_SAFE_INTEGER)
      ? JSON.stringify(token) : token;
  }));
}

function gateState(gate, asOf) {
  const expiry = Date.parse(gate.expires_at), evaluated = Date.parse(asOf);
  if (!Number.isFinite(expiry) || !Number.isFinite(evaluated)) return "unresolved";
  return expiry <= evaluated ? "expired" : gate.status;
}

/* ---------- app state (single mutable holder) ---------- */
const state = {
  token: "",
  chartItems: [],
  chainProbes: new Map(),
  ws: null,
  pollTimer: null,
  chartPending: false,
};

/* ---------- toast ---------- */
function toast(message, error) {
  const node = $("toast");
  node.textContent = message;
  node.className = "toast show" + (error ? " error" : "");
  clearTimeout(node._t);
  node._t = setTimeout(() => { node.className = "toast"; }, 4200);
}

/* ---------- api client (bounded retry with exponential backoff) ---------- */
class ApiError extends Error {
  constructor(status, detail, code) {
    super("http " + status);
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}

async function api(path, opts = {}, attempt = 0) {
  const method = opts.method || "GET";
  // Reads are idempotent and may be retried; writes never are, so a
  // duplicate trigger/approval cannot be issued by the retry loop.
  const retryable = method === "GET" && attempt < RETRY_ATTEMPTS;
  const headers = { "Accept": "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (state.token && method !== "GET") headers["Authorization"] = "Bearer " + state.token;
  let res;
  try {
    res = await fetch(path, { ...opts, headers, signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  } catch (err) {
    if (retryable) {
      await sleep(Math.min(RETRY_BASE_MS * 2 ** attempt, RETRY_MAX_MS));
      return api(path, opts, attempt + 1);
    }
    toast(err && err.name === "TimeoutError" ? "请求超时" : "网络错误", true);
    throw err;
  }
  if (res.status === 401) { toast("未授权：请检查操作 Token", true); throw new Error("unauthorized"); }
  if (res.status >= 500 && retryable) {
    await sleep(Math.min(RETRY_BASE_MS * 2 ** attempt, RETRY_MAX_MS));
    return api(path, opts, attempt + 1);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : res.status;
    toast("请求失败: " + detail, true);
    throw new ApiError(res.status, typeof body.detail === "string" ? body.detail : "", typeof body.code === "string" ? body.code : "");
  }
  return parseJsonExact(await res.text());
}

// Share only in-flight reads. Settled values are never cached: the next read
// must recheck server integrity and freshness. Writes always remain separate.
const pendingReads = new Map();
function apiGet(path) {
  if (pendingReads.has(path)) return pendingReads.get(path);
  const pending = api(path).finally(() => pendingReads.delete(path));
  pendingReads.set(path, pending);
  return pending;
}
const apiPost = (path, body = {}) => api(path, { method: "POST", body: JSON.stringify(body) });

/* ---------- status badges ---------- */
const BADGE_MAP = {
  expired: ["b-red", "已过期"],
  pass: ["b-green", "通过"], fail: ["b-red", "未过"], unresolved: ["b-amber", "未决"],
  HOLD: ["b-green", "HOLD"], HALT: ["b-red", "HALT"], hold: ["b-green", "hold"], halt: ["b-red", "halt"],
  running: ["b-amber", "运行中"], succeeded: ["b-green", "成功"], failed: ["b-red", "失败"], queued: ["b-gray", "排队"],
  stopping: ["b-amber", "停止中"], stopped: ["b-gray", "已停止"],
  pending: ["b-amber", "待确认"], approved: ["b-green", "已确认"],
};

function badgeHtml(status) {
  const [cls, label] = BADGE_MAP[status] || ["b-gray", status];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function setBadge(node, text, tone) {
  node.textContent = text;
  node.className = "badge " + tone;
}

/* ---------- renderers (DocumentFragment batching) ---------- */

/* Rebuild a table from column labels and pre-escaped cell markup. Cells may
 * contain trusted markup (badges/digests/actions); everything dynamic has
 * already passed through esc(). A single fragment is appended once so the
 * browser performs one layout pass. */
function renderTable(id, columns, rows, emptyText) {
  const table = $(id);
  const fragment = document.createDocumentFragment();
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of columns) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = label;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  fragment.appendChild(thead);
  const tbody = document.createElement("tbody");
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = columns.length;
    td.className = "empty";
    td.textContent = emptyText;
    tr.appendChild(td);
    tbody.appendChild(tr);
  } else {
    for (const cells of rows) {
      const tr = document.createElement("tr");
      for (const cell of cells) {
        const td = document.createElement("td");
        td.innerHTML = cell;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }
  fragment.appendChild(tbody);
  table.replaceChildren(fragment);
}

/* Rebuild a stats grid from plain-text card descriptors (never markup). */
function renderStats(id, cards) {
  const grid = $(id);
  const fragment = document.createDocumentFragment();
  for (const card of cards) {
    const stat = document.createElement("div");
    stat.className = "stat" + (card.tone ? " " + card.tone : "");
    const num = document.createElement("div");
    num.className = "num";
    num.textContent = String(card.value == null ? "–" : card.value);
    const label = document.createElement("div");
    label.className = "lbl";
    label.textContent = card.label;
    stat.append(num, label);
    fragment.appendChild(stat);
  }
  grid.replaceChildren(fragment);
}


export { $, esc, short, sleep, debounce, fmtTime, fmtInt, parseJsonExact, gateState, state, toast, apiGet, apiPost, ApiError, badgeHtml, setBadge, renderTable, renderStats, POLL_SECONDS, WS_RECONNECT_SECONDS, TOKEN_STORAGE_KEY, CHART_MAX_POINTS, FETCH_TIMEOUT_MS };
