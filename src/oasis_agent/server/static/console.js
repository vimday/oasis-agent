import { wireBatchResearch } from "./batch-research.js";
import { wireResearch } from "./research.js";
import { readinessModel, renderReadinessPath } from "./readiness.js";
import { loadCampaigns, loadConfiguration, loadRuns, loadOps, loadApprovals, wireOperations } from "./operations.js";
import { createPagedView, setPageLimit, showDetail } from "./tables.js";
import { $, esc, short, sleep, debounce, fmtTime, fmtInt, parseJsonExact, gateState, state, toast, apiGet, apiPost, ApiError, badgeHtml, setBadge, renderTable, renderStats, POLL_SECONDS, WS_RECONNECT_SECONDS, TOKEN_STORAGE_KEY, CHART_MAX_POINTS, FETCH_TIMEOUT_MS } from "./core.js";

/* ---------- view registry ---------- */
const views = new Map();

/* Register a console section. ``poll`` opts the view into the global
 * visibility-aware refresh loop; non-polling views reload on tab switch
 * and through their own actions/websocket events. */
function registerView(name, load, poll = false) {
  views.set(name, { load, poll });
}

function currentTab() {
  const active = document.querySelector("nav button[data-tab].active");
  return active ? active.dataset.tab : "overview";
}

/* ---------- domain loaders ---------- */
function decisionExplanation(decision) {
  const reasons = [
    ...(Array.isArray(decision.reasons) ? decision.reasons : []),
    ...(Array.isArray(decision.rebalance_reasons) ? decision.rebalance_reasons : []),
  ];
  const reasonText = reasons.length ? [...new Set(reasons)].map(esc).join(" · ") : "记录未提供原因";
  return `<p>决策依据：${reasonText}</p><p class="muted">策略验证：${esc(decision.strategy_qualification || "unresolved")} · 此处展示影子决策，不构成执行授权。</p>`;
}

async function loadStatus() {
  try {
    const st = await apiGet("/api/v1/status");
    setBadge($("st-stage"), "stage " + (st.stage || "–"),
      st.stage === "bounded_production" ? "b-green" : st.stage ? "b-amber" : "b-gray");
    setBadge($("st-eligible"), "eligible " + (st.execution_eligible === true ? "TRUE" : st.execution_eligible === false ? "false" : "未验证"),
      st.execution_eligible === true ? "b-green" : "b-gray");
    setBadge($("st-halt"), "halt " + (st.latest_health ? (st.halt_latched ? "LATCHED" : "clear") : "未知"),
      !st.latest_health ? "b-amber" : st.halt_latched ? "b-red" : "b-green");
    $("st-decisions").textContent = "decisions " + fmtInt(st.decision_count ?? 0);
    renderStats("ov-stats", [
      { label: "发布阶段", value: st.stage || "–", tone: st.stage === "bounded_production" ? "" : "warn" },
      { label: "execution_eligible", value: st.execution_eligible === true ? "TRUE" : st.execution_eligible === false ? "false" : "未验证", tone: st.execution_eligible === true ? "" : "warn" },
      { label: "决策 / 健康记录", value: fmtInt(st.decision_count ?? 0) + " / " + fmtInt(st.health_count ?? 0) },
      { label: "HALT 锁存", value: !st.latest_health ? "未知" : st.halt_latched ? "已锁存" : "无", tone: !st.latest_health ? "warn" : st.halt_latched ? "danger" : "" },
    ]);
    const ld = $("latest-decision");
    if (st.latest_decision) {
      const d = st.latest_decision;
      const evidenceExpanded = ld.querySelector("details")?.open === true;
      ld.innerHTML = `<div class="row">${badgeHtml(d.decision)}<span class="mono muted">seq ${esc(d.seq)} · ${esc(fmtTime(d.observed_at))}</span><span class="digest mono">${esc(d.digest)}</span></div>` +
        decisionExplanation(d) + `<details${evidenceExpanded ? " open" : ""}><summary>查看完整证据记录</summary><pre class="raw">${esc(JSON.stringify(d, null, 2))}</pre></details>`;
    } else {
      ld.innerHTML = `<div class="empty">尚无决策记录</div>`;
    }
  } catch {
    setBadge($("st-stage"), "stage 未知", "b-gray");
    setBadge($("st-eligible"), "eligible 未验证", "b-amber");
    setBadge($("st-halt"), "halt 未知", "b-amber");
    $("st-decisions").textContent = "决策数量未知";
    $("ov-stats").textContent = "状态读取失败；当前阶段、执行资格与 HALT 状态未验证。";
    $("latest-decision").textContent = "决策暂不可用，请恢复连接后刷新。";
  }
}

async function loadGates() {
  const grid = $("gates-grid");
  try {
    const r = await apiGet("/api/v1/readiness");
    if (!readinessModel(r).valid) throw new Error("incomplete readiness assessment");
    renderReadinessPath(r);
    grid.innerHTML = r.gates.map((g) => {
      const effective = gateState(g, r.as_of);
      const cls = effective === "pass" ? "pass" : ["fail", "expired"].includes(effective) ? "fail" : "";
      const findings = effective === "expired" ? "证据已过期，需要重新验证" : (g.findings || []).length ? g.findings.join("; ") : (g.status === "pass" ? "证据就绪" : "");
      return `<div class="gate-card ${cls}">
        <span class="gname">${esc(g.gate)}</span>
        <span class="gstatus">${badgeHtml(effective)}</span>
        <div class="gdetail">expires ${esc(g.expires_at)}${g.independent_reviewer ? " · 独立审查 " + esc(g.independent_reviewer) : ""}</div>
        <div class="gdetail">${esc(findings)}</div>
        <div class="gdetail digest">${esc(g.evidence && g.evidence.length ? g.evidence.map((e) => e.content_hash).join(" ") : "")}</div>
      </div>`;
    }).join("");
  } catch (err) {
    renderReadinessPath(null);
    if (err instanceof ApiError && err.code === "unconfigured") {
      grid.innerHTML = `<div class="empty">门禁清单未配置（configs/server.local.toml → databases.readiness_manifest）</div>`;
    } else if (err instanceof ApiError && err.code === "corrupt") {
      grid.innerHTML = `<div class="empty">门禁清单校验失败，已 fail-closed（请检查 readiness 证据文件）</div>`;
    } else {
      grid.innerHTML = `<div class="empty">门禁清单不可用</div>`;
    }
  }
}

/* fee chart */
function drawFeeChart(items) {
  const canvas = $("fee-chart");
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.round(220 * dpr);
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, 220);
  const W = rect.width, H = 220, padL = 44, padR = 12, padT = 12, padB = 24;
  const series = items.map((d) => {
    const q = d.fee_quote || {};
    return { seq: d.seq, eff: q.effective_fee_pips, min: q.min_fee_pips, max: q.max_fee_pips, ts: d.observed_at };
  }).slice(-CHART_MAX_POINTS);
  if (!series.length) { ctx.fillStyle = "#8b949e"; ctx.font = "12px monospace"; ctx.fillText("无决策数据", padL, H / 2); return; }
  const all = series.flatMap((s) => [s.eff, s.min, s.max]).filter((v) => v != null);
  const lo = Math.min(...all), hi = Math.max(...all);
  const span = Math.max(hi - lo, 1);
  const X = (i) => padL + (i / Math.max(series.length - 1, 1)) * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - lo) / span) * (H - padT - padB);
  ctx.strokeStyle = "#30363d"; ctx.beginPath();
  for (let g = 0; g <= 4; g++) { const y = padT + (g / 4) * (H - padT - padB); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); }
  ctx.stroke();
  const drawSeries = (key, color, dash) => {
    ctx.strokeStyle = color; ctx.setLineDash(dash || []); ctx.beginPath();
    series.forEach((s, i) => { const v = s[key]; if (v == null) return; const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke(); ctx.setLineDash([]);
  };
  drawSeries("eff", "#4fd1c5", []);
  drawSeries("min", "#8b949e", [4, 4]);
  drawSeries("max", "#e3b341", [4, 4]);
  ctx.fillStyle = "#8b949e"; ctx.font = "10px monospace";
  ctx.fillText(hi.toFixed(1), 4, padT + 8); ctx.fillText(lo.toFixed(1), 4, H - padB);
  ctx.fillStyle = "#4fd1c5"; ctx.fillText("effective", W - 92, 12);
  ctx.fillStyle = "#8b949e"; ctx.fillText("min", W - 92, 26);
  ctx.fillStyle = "#e3b341"; ctx.fillText("max", W - 92, 40);
}

function scheduleChartRedraw() {
  if (state.chartPending) return;
  state.chartPending = true;
  requestAnimationFrame(() => { state.chartPending = false; drawFeeChart(state.chartItems); });
}

if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(scheduleChartRedraw).observe($("fee-chart"));
}

const pages = new Map();
const decisionPage = createPagedView("decisions", "/api/v1/decisions", {
  query: () => ({order: "desc", ...($("decision-filter").value ? {decision: $("decision-filter").value} : {})}),
  columns: ["序号", "决策", "费率 pips", "区间动作", "证据"],
  row: d => [esc(d.seq), badgeHtml(d.decision), esc(d.fee_quote?.effective_fee_pips ?? "–"),
    esc(d.range_action?.action ?? "–"),
    `<button type="button" class="action" data-action="decision-detail" data-seq="${esc(d.seq)}">查看原因与证据</button>`],
  onData: items => { state.chartItems = [...items].reverse(); drawFeeChart(state.chartItems); },
});
const evidencePage = createPagedView("evidence", "/api/v1/evidence", {
  query: () => Object.fromEntries([["kind", $("ev-kind").value.trim()], ["chain_id", $("ev-chain").value.trim()], ["source", $("ev-source").value.trim()]].filter(([, value]) => value)),
  columns: ["类型", "链", "区块", "来源", "获取时间", "证据"],
  row: e => [esc(e.kind), esc(e.chain_id ?? "–"), esc(e.block?.number ?? "–"), esc(e.source),
    esc(fmtTime(e.retrieved_at)), `<button type="button" class="action" data-action="evidence-detail" data-key="${esc(e.evidence_key)}">查看原始记录</button>`],
});
const observationsPage = createPagedView("observations", "/api/v1/observations", {
  query: () => ({order: "desc"}),
  columns: ["序号", "区块", "健康决策", "原因", "观测时间", "摘要"],
  row: h => [esc(h.seq), esc(h.block?.number ?? "–"), badgeHtml(h.decision),
    esc((h.reasons || []).join(" · ") || "未提供"), esc(fmtTime(h.observed_at)), esc(h.digest)],
});
const executionPage = createPagedView("execution", "/api/v1/journal/lp/events", {
  query: () => ({order: "desc"}),
  columns: ["序号", "状态", "意图", "Nonce", "交易哈希"],
  row: e => [esc(e.seq), badgeHtml(e.state), esc(e.intent_hash), esc(e.nonce), esc(e.transaction_hash ?? "未提交")],
});
pages.set("decisions", decisionPage);
pages.set("evidence", evidencePage);
pages.set("observations", observationsPage);
pages.set("execution", executionPage);
async function loadDecisions() { await decisionPage.load(); }
async function loadEvidence() { await evidencePage.load(); }

async function loadExecution() {
  try {
    const r = await apiGet("/api/v1/journal/lp");
    renderStats("ex-stats", [
      { label: "事件总数", value: fmtInt(r.total_events) },
      { label: "意图数", value: fmtInt(r.intent_count) },
      { label: "未关闭意图", value: fmtInt(r.open_intents), tone: r.open_intents > 0 ? "warn" : "" },
      { label: "policy_hash", value: short(r.policy_hash, 14) },
    ]);
    await executionPage.load();
  } catch {
    $("ex-stats").textContent = "执行概览不可用";
    renderTable("execution-table", [], [], "执行记录未验证");
    $("execution-pager").replaceChildren();
    $("execution-status").textContent = "读取失败";
  }
}

async function loadHaltProofs() {
  try {
    const r = await apiGet("/api/v1/p2/halt-proofs");
    const rows = r.items.map((p) => {
      const rc = p.recovery || {};
      return [
        esc(short(p.intent_hash, 20)),
        badgeHtml(p.state),
        esc(short(rc.recovery_transaction_hash || "–", 20)),
        rc.closes_exposure ? '<span class="ok">已关闭敞口</span>' : '<span class="bad">未关闭</span>',
        esc(rc.finalized_block ? rc.finalized_block.number : "–"),
      ];
    });
    renderTable("halt-table", ["intent", "state", "recovery tx", "敞口", "final block"], rows, "无恢复证明");
  } catch { renderTable("halt-table", [], [], "读取失败，旧结果已清除"); }
}

async function loadChains() {
  try {
    const r = await apiGet("/api/v1/chains");
    const rows = r.items.map((c) => [
      esc(c.chain_id),
      esc(c.name),
      esc(c.rpc_origin || "–"),
      c.rpc_configured ? '<span class="ok">已配置</span>' : '<span class="warn">未配置</span>',
      `<span class="digest">${esc(short(c.pool_manager || "–", 22))}</span>`,
      `<button type="button" class="action" data-action="probe-chain" data-chain-id="${esc(c.chain_id)}" ${c.rpc_configured ? "" : "disabled"}>探测</button> <button type="button" class="action" data-action="pool-detail" data-chain-id="${esc(c.chain_id)}" ${c.rpc_configured ? "" : "disabled"}>池部署信息</button>`,
    ]);
    renderTable("chains-table", ["chain_id", "name", "RPC origin", "RPC", "pool_manager", ""], rows, "未配置链");
    $("st-chain").textContent = "chains " + fmtInt(r.items.length);
  } catch { renderTable("chains-table", [], [], "读取失败，旧结果已清除"); }
}

async function probeChain(chainId) {
  const key = String(chainId);
  const label = "chain " + chainId;
  try {
    const s = await apiGet(`/api/v1/chains/${chainId}/chain`);
    const matched = s.rpc_chain_id === Number(chainId)
      ? '<span class="ok">chainId 匹配</span>'
      : `<span class="bad">chainId 不匹配: ${esc(s.rpc_chain_id)}</span>`;
    state.chainProbes.set(key, {
      label,
      value: "block " + fmtInt(s.block_number),
      extra: matched + " · balance " + esc(fmtInt(s.sample_balance_wei)) + " wei",
    });
  } catch (err) {
    state.chainProbes.set(key, {
      label,
      value: "探测失败",
      tone: "danger",
      extra: err instanceof ApiError && err.detail ? esc(err.detail) : "",
    });
  }
  renderChainProbes();
}

async function probeAllChains() {
  const r = await apiGet("/api/v1/chains");
  for (const c of r.items) {
    if (!c.rpc_configured) continue;
    await probeChain(c.chain_id);
  }
}

function renderChainProbes() {
  const grid = $("chain-probes");
  const fragment = document.createDocumentFragment();
  if (state.chainProbes.size === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "尚未探测";
    fragment.appendChild(empty);
  } else {
    for (const p of state.chainProbes.values()) {
      const stat = document.createElement("div");
      stat.className = "stat" + (p.tone ? " " + p.tone : "");
      const num = document.createElement("div");
      num.className = "num";
      num.textContent = String(p.value);
      const label = document.createElement("div");
      label.className = "lbl";
      label.textContent = p.label;
      stat.append(num, label);
      if (p.extra) {
        const extra = document.createElement("div");
        extra.className = "gdetail";
        extra.innerHTML = p.extra;
        stat.appendChild(extra);
      }
      fragment.appendChild(stat);
    }
  }
  grid.replaceChildren(fragment);
}

async function loadAlerts() {
  try {
    const r = await apiGet("/api/v1/alerts");
    $("alerts-list").innerHTML = r.items.length
      ? r.items.map((a) => `<div class="alert-item ${esc(a.level)}">[${esc(a.level)}] ${esc(a.rule)} — ${esc(a.message)}</div>`).join("")
      : `<div class="empty">无警报</div>`;
  } catch { $("alerts-list").textContent = "警报数据不可用，不能据此判断系统健康"; }
}

/* ---------- view wiring (the only place that maps tabs to loaders) ---------- */
function launchNewest(items, field = "observed_at") {
  const timestamp = item => Number.isInteger(item[field]) && item[field] > 0 ? item[field] : -1;
  return [...items].sort((a, b) => timestamp(b) - timestamp(a) || (b.seq || 0) - (a.seq || 0));
}
function launchTime(value) {
  if (!Number.isInteger(value) || value <= 0) return "未记录时间";
  return new Date(value * 1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}) + " UTC+8";
}
function launchReason(value) {
  const raw = String(value || "原因未记录");
  const labels = [
    ["holder_two_sources_required", "缺少双源 holders 数据"],
    ["holder_snapshot_mismatch", "holders 与行情不属于同一链或区块"],
    ["holder_identity_mismatch", "holders 代币身份与冻结配置不一致"],
    ["holder_time_incoherent", "holders 采集时间不一致"],
    ["holder_stale_or_future_snapshot", "holders 数据过期或来自未来区块"],
    ["holder_freshness_limits_unfrozen", "holders 时效阈值尚未冻结"],
    ["holder_coverage_incomplete", "holders 名单不完整"],
    ["holder_supply_unreconciled", "holders 余额与总供应量未核平"],
    ["holder_provider_disagreement", "双源 holders 数据不一致"],
    ["holder_count_below_minimum", "非零持币地址数低于下限"],
    ["holder_single_limit_exceeded", "单地址持币集中度超限"],
    ["holder_top10_limit_exceeded", "前十地址持币集中度超限"],
    ["holder_hhi_limit_exceeded", "整体持币集中度 HHI 超限"],
    ["holder_raw_proof_unverified", "holders 原始链证据尚未验证"],
    ["holder_common_control_unresolved", "持币地址共同控制关系未验证"],
    ["holder_policy_v3_required", "holders 分析需要 v3 冻结策略"],
    ["details redacted", "旧日志未保留节点原因，无法区分合约回退、参数错误或节点限制"],
    ["revert=SPL", "池拒绝价格限制（SPL）；需核对当前价格边界与交易方向"],
    ["execution_reverted", "合约调用回退；仅凭此错误不能确定是否缺少流动性"],
    ["rate_limited", "RPC 请求被限流"], ["HTTP=429", "RPC 请求被限流"],
    ["insufficient_funds", "节点报告余额不足"],
    ["state_unavailable", "节点无法提供指定区块状态"],
    ["unsupported_method", "节点不支持此 RPC 方法"],
    ["invalid_params", "节点拒绝请求参数"],
    ["timeout", "RPC 请求超时"], ["Timeout", "RPC 请求超时"],
    ["fdv_at_or_above_cap", "FDV 达到或超过上限，跳过买入"],
    ["node_rejected", "节点拒绝请求；已保留错误码，具体原因未识别"],
  ];
  const matched = labels.find(([code]) => raw.includes(code));
  return matched ? matched[1] + "（" + raw + "）" : raw;
}
function launchHolders(assessment) {
  const m = assessment?.metrics;
  if (!m || ![m.holder_count, m.single_bps, m.top10_bps, m.hhi_bps].every(Number.isInteger)) {
    return "未验证 / 无完整 holders 数据";
  }
  return `导入数据（未验证）：${m.holder_count} 个地址；单地址 ${m.single_bps} bps；前十 ${m.top10_bps} bps；HHI ${m.hhi_bps} bps`;
}
let launchLoading = false;
let launchLastSuccess = 0;
async function loadLaunch() {
  if (launchLoading) return;
  launchLoading = true;
  const sections = ["targets", "candidates", "quotes", "decisions", "approvals"];
  try {
    const includeLegacy = $("launch-legacy").checked;
    const response = await fetch("/api/v1/launch/dashboard?include_legacy=" + includeLegacy, {
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error("Launch HTTP " + response.status);
    const snapshot = parseJsonExact(await response.text());
    const results = sections.map(section => snapshot.sections[section]);
    $("launch-cleanup").textContent = "已隐藏旧版错误 " + snapshot.hidden_legacy_count + " 条；审计及仓位恢复记录保留。"
      + (snapshot.last_legacy_error_at ? " 最后一次旧版错误：" + launchTime(snapshot.last_legacy_error_at) : "")
      + (snapshot.last_legacy_error_at && Date.now()/1000 - snapshot.last_legacy_error_at < 120 ? " · ⚠ 旧格式仍在产生，隐藏不代表错误已解决" : "");
    launchLastSuccess = Date.now();
    renderTable("launch-targets", ["目标", "CA", "链", "最近日志时间", "日志状态", "停止"],
      results[0].items.map(t => [esc(t.symbol), esc(t.target_ca), esc(t.chain_id), esc(launchTime(t.last_observed_at)), esc(t.watcher_status === "recent_journal_activity" ? "近 120 秒有活动" : "日志陈旧或缺失，运行状态未知"), esc(t.stand_down)]), "未注册");
    renderTable("launch-candidates", ["池", "最近时间 ↓", "区块", "token0 / token1", "费率参数", "最近状态（非流动性证明）", "持币集中度（非安全证明）", "核验 / 失败原因"],
      launchNewest(results[1].items, "last_observed_at").map(c => [esc(c.pool), esc(launchTime(c.last_observed_at) + (Number.isInteger(c.last_observed_at) ? " · " + Math.max(0, Math.floor(Date.now()/1000) - c.last_observed_at) + " 秒前" : "")), esc(c.snapshot ?? "未记录"), esc(c.token0 + " / " + c.token1), esc(c.fee ?? "未记录"), esc(c.tradable), esc(launchHolders(c.holders)),
        esc(Object.entries(c.gates).map(([k,v]) => k + ": " + (v.passed ? "通过" : v.reasons.map(launchReason).join(", "))).join(" · "))]), "尚无候选证据");
    renderTable("launch-quotes", ["来源", "时间", "支出 USD micros", "ETH wei", "收到 base units", "FDV USD micros（分数）", "严格低于上限"],
      launchNewest(results[2].items).slice(0, 100).map(q => [esc(q.source), esc(launchTime(q.observed_at)), esc(q.spend_usd_micros), esc(q.native_wei), esc(q.received_base_units),
        esc(q.fdv_usd_micros_numerator + " / " + q.fdv_denominator), esc(q.fdv_below_cap)]), "尚无报价证据");
    renderTable("launch-decisions", ["seq", "时间 ↓", "区块", "事件", "原因", "digest"],
      launchNewest(results[3].items).map(d => [esc(d.seq), esc(launchTime(d.observed_at)), esc(d.block ?? "未记录"), esc(d.decision), esc(d.stand_down_reasons.map(launchReason).join(", ")), esc(d.digest)]), "尚无影子决策");
    renderTable("launch-approvals", ["目标", "授权状态", "原因"],
      results[4].items.map(a => [esc(a.target_ca), esc(a.status), esc(a.reason)]), "未验证授权");
    const target = results[0].items[0] || {};
    const isExecution = results[0].status === "execution_journal";
    const modeText = {live: "实盘模式", observe: "观察模式", unknown: "模式未记录"}[target.execution_mode] || "影子记录";
    $("launch-title").textContent = "Launch · " + (isExecution ? "执行监控" : "观察 / 影子");
    $("launch-description").textContent = isExecution
      ? "此页面只读，不控制交易开关。模式来自执行日志策略；日志活动不等于进程健康，也不证明满足买入条件。"
      : "此页面只读。保留的影子或导入记录不代表链上已核实。";
    $("launch-status").textContent = isExecution
      ? modeText + " · 已记录买入 " + (target.buy_confirmed_count || 0) + " 笔 / 卖出 " + (target.sell_confirmed_count || 0)
        + " 笔 · 最近事件: " + (target.last_event || "无") + " · 日志序号 " + (target.last_seq ?? "未知")
        + " · 最近日志 " + launchTime(target.last_observed_at)
        + " · " + (target.activity_age_seconds == null ? "时间未知" : target.activity_age_seconds < 0 ? "日志时间超前，请检查时钟" : target.activity_age_seconds + " 秒前")
        + " · 页面读取 " + launchTime(target.server_time)
        + (target.current_cycle == null ? "" : " · 扫描轮次 " + target.current_cycle + "，本轮记录跨度 " + target.cycle_record_span_seconds + " 秒")
        + (target.cycle_record_span_seconds > 120 ? " · ⚠ 扫描轮次耗时过长，各池状态可能明显落后" : "")
        + (target.watcher_status === "recent_journal_activity" ? " · 日志近期有活动，进程健康未独立验证" : " · ⚠ 日志超过 120 秒未更新或缺失，进程可能停止或卡住")
      : "数据状态: " + results[0].status + " · 面板无签名或广播能力";
  } catch {
    sections.forEach(s => { $("launch-" + s).textContent = "数据不可用"; });
    $("launch-cleanup").textContent = "数据读取失败，隐藏数量不可用";
    $("launch-status").textContent = "读取失败或超时，停止展示旧数据";
  } finally {
    launchLoading = false;
  }
}
registerView("observations", () => observationsPage.load());
registerView("system", loadSystem, true);
registerView("research", async () => {});
registerView("launch", loadLaunch, true);
registerView("overview", async () => { await Promise.all([loadStatus(), loadGates()]); }, true);
registerView("decisions", loadDecisions);
registerView("evidence", loadEvidence);
registerView("execution", async () => { await Promise.all([loadExecution(), loadHaltProofs()]); });
registerView("ops", async () => { await Promise.all([loadCampaigns(), loadConfiguration(), loadRuns(), loadOps()]); }, true);
registerView("gates", loadApprovals);
registerView("chains", async () => { await Promise.all([loadChains(), loadAlerts()]); }, true);

async function loadSystem() {
  const results = await Promise.allSettled([apiGet("/api/v1/health"), apiGet("/api/v1/meta"), apiGet("/api/v1/metrics")]);
  if (results[0].status === "fulfilled") {
    const health = results[0].value;
    $("system-status").textContent = health.ok ? "已配置数据源检查通过（不代表策略可执行）" : "部分数据源未配置或校验失败";
    renderTable("sources-table", ["数据源", "可用", "完整性"], Object.entries(health.sources).map(([name, source]) =>
      [esc(name), source.available ? "是" : "否", esc(source.integrity)]), "无数据源");
  } else {
    $("system-status").textContent = "数据源健康未知";
    renderTable("sources-table", [], [], "读取失败，旧结果已清除");
  }
  if (results[2].status === "fulfilled") {
    const measured = results[2].value;
    $("reader-metrics").textContent = "最近 " + measured.sample_count + "/" + measured.window + " 次读取 · 队列 p95 "
      + (measured.queue.p95 == null ? "未知" : (measured.queue.p95 / 1e6).toFixed(3) + " ms")
      + " · 读取 p95 " + (measured.read.p95 == null ? "未知" : (measured.read.p95 / 1e6).toFixed(3) + " ms")
      + " · 有界容量 " + measured.capacity + "（仅观测读取，不代表交易延迟）";
  } else $("reader-metrics").textContent = "读取计时不可用";
  if (results[1].status === "fulfilled") {
    const meta = results[1].value;
    $("system-version").textContent = meta.server + " · " + meta.version + " · 最大分页 " + meta.max_rows_per_page;
    renderTable("capabilities-table", ["接口", "方法", "类别", "页面"], meta.endpoints.map(endpoint => [
      esc(endpoint.path), esc(endpoint.methods.join(" / ")),
      endpoint.methods.some(method => method !== "GET") ? "授权操作" : "只读查询",
      endpoint.console_view && views.has(endpoint.console_view) ? `<button type="button" class="action" data-action="navigate" data-view="${esc(endpoint.console_view)}">${esc(endpoint.console_view)}</button>` : "未映射",
    ]), "无接口");
  } else {
    $("system-version").textContent = "后端能力不可用";
    renderTable("capabilities-table", [], [], "能力映射未验证");
  }
}

/* ---------- navigation ---------- */
function switchTab(name) {
  if (!views.has(name)) return;
  history.replaceState(null, "", "#" + name);
  document.querySelectorAll("nav button[data-tab]").forEach((btn) => {
    const isActive = btn.dataset.tab === name;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", isActive ? "true" : "false");
    btn.tabIndex = isActive ? 0 : -1;
  });
  document.querySelectorAll(".tab").forEach((panel) => {
    panel.classList.toggle("active", panel.id === "tab-" + name);
  });
  const view = views.get(name);
  if (view) view.load();
}

document.querySelectorAll("nav button[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  btn.addEventListener("keydown", event => {
    const tabs = [...document.querySelectorAll("nav button[data-tab]")];
    const index = tabs.indexOf(btn);
    const next = ["ArrowRight", "ArrowDown"].includes(event.key) ? (index + 1) % tabs.length
      : ["ArrowLeft", "ArrowUp"].includes(event.key) ? (index + tabs.length - 1) % tabs.length
      : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
    if (next === null) return;
    event.preventDefault(); switchTab(tabs[next].dataset.tab); tabs[next].focus();
  });
});

/* ---------- websocket ---------- */
function setWsBadge(text, tone) {
  setBadge($("st-ws"), text, tone);
}

function connectWs() {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/v1/stream`);
  state.ws = ws;
  ws.onopen = () => setWsBadge("事件流已连接", "b-green");
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "launch_decisions" && currentTab() === "launch" && !document.hidden && Date.now() - launchLastSuccess >= 2000) loadLaunch();
      if (msg.type === "decisions" && msg.items && msg.items.length) {
        if (document.hidden) return;
        toast("新决策流: " + msg.items.length + " 条");
        loadStatus();
        const view = views.get("decisions");
        if (view && currentTab() === "decisions") view.load();
      }
    } catch { /* ignore malformed frames */ }
  };
  ws.onclose = () => {
    setWsBadge("ws reconnecting", "b-amber");
    setTimeout(connectWs, WS_RECONNECT_SECONDS);
  };
  ws.onerror = () => ws.close();
}

/* ---------- polling (visibility-aware) ---------- */
function tick() {
  if (document.hidden) return;
  const tab = currentTab();
  if (tab !== "overview") loadStatus();
  const view = views.get(tab);
  if (view && view.poll) view.load();
}

function refreshVisibleViews() {
  if (document.hidden) return;
  tick();
  // Non-polling tables may have missed stream notifications while hidden.
  const view = views.get(currentTab());
  if (view && !view.poll) view.load();
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(tick, POLL_SECONDS * 1000);
}

function stopPolling() {
  if (state.pollTimer !== null) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling();
  else { refreshVisibleViews(); startPolling(); }
});

setInterval(() => {
  if (!document.hidden && currentTab() === "launch") {
    loadLaunch();
  }
}, 3000);
setInterval(() => {
  const age = launchLastSuccess ? Math.floor((Date.now() - launchLastSuccess) / 1000) : null;
  $("launch-refresh").textContent = "当前浏览器时间 " + launchTime(Math.floor(Date.now()/1000))
    + " · 自动刷新 3 秒 · " + (launchLoading ? "正在读取" : "等待下次刷新")
    + " · 上次页面成功读取 " + (age === null ? "尚无" : age + " 秒前")
    + (age !== null && age > 15 ? " · ⚠ 页面数据已陈旧" : "");
}, 1000);

/* ---------- delegated action registry ---------- */
const actions = new Map();

function registerAction(name, handler) {
  actions.set(name, handler);
}

$("launch-legacy").addEventListener("change", () => { loadLaunch(); });
registerAction("refresh-launch", () => { loadLaunch(); });
registerAction("refresh-decisions", () => decisionPage.load({reset: true}));
registerAction("refresh-system", loadSystem);
registerAction("navigate", button => switchTab(button.dataset.view));
registerAction("page", button => pages.get(button.dataset.page)?.load({delta: Number(button.dataset.delta)}));
registerAction("page-reset", button => pages.get(button.dataset.page)?.load({reset: true}));
registerAction("close-detail", () => $("record-dialog").close());
registerAction("decision-detail", button => showDetail("决策与关联健康证据", "/api/v1/decisions/" + encodeURIComponent(button.dataset.seq)));
registerAction("evidence-detail", button => showDetail("证据原始记录", "/api/v1/evidence/" + encodeURIComponent(button.dataset.key)));
registerAction("run-detail", button => showDetail("运行结果与脱敏日志", "/api/v1/runs/" + encodeURIComponent(button.dataset.id)));
registerAction("pool-detail", button => showDetail("池部署探测（非交易报价）", "/api/v1/chains/" + encodeURIComponent(button.dataset.chainId) + "/pool"));
registerAction("probe-chain", (el) => { probeChain(Number(el.dataset.chainId)); });
registerAction("probe-all-chains", () => { probeAllChains(); });

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-action]");
  if (!trigger) return;
  const handler = actions.get(trigger.dataset.action);
  if (handler) Promise.resolve(handler(trigger, event)).catch(() => toast("操作未完成，请检查输入与服务状态", true));
});

/* ---------- delegated form registry ---------- */
const forms = new Map();

function registerForm(name, handler) {
  forms.set(name, handler);
}

registerForm("query-evidence", () => evidencePage.load({reset: true}));
wireOperations(registerAction, registerForm);
wireResearch();
wireBatchResearch();

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-action]");
  if (!form) return;
  event.preventDefault();
  const handler = forms.get(form.dataset.action);
  if (handler) Promise.resolve(handler(form, event)).catch(() => toast("操作未完成，请检查输入与服务状态", true));
});

/* ---------- token (memory only; remove legacy session persistence) ---------- */
sessionStorage.removeItem(TOKEN_STORAGE_KEY);
const syncToken = (value) => {
  state.token = value;
  loadConfiguration();
};

const tokenInput = $("ops-token");
tokenInput.value = state.token;
tokenInput.addEventListener("input", (event) => syncToken(event.target.value));

/* decision filter: reload automatically with debounce */
$("decision-filter").addEventListener("change", debounce(() => { decisionPage.load({reset: true}); }, 250));

/* ---------- boot ---------- */
async function boot() {
  try {
    const meta = await apiGet("/api/v1/meta");
    setPageLimit(meta.max_rows_per_page);
  } catch {
    $("system-status").textContent = "能力读取失败；请在系统页面重试";
    switchTab("system");
    return;
  }
  // Every navigation button must have a registered view; a console section
  // that is rendered but not wired fails loudly at startup instead of
  // silently showing stale data.
  for (const btn of document.querySelectorAll("nav button[data-tab]")) {
    if (!views.has(btn.dataset.tab)) throw new Error("missing view for tab: " + btn.dataset.tab);
  }
  const requestedTab = window.location.hash.slice(1);
  const initialTab = views.has(requestedTab) ? requestedTab : "overview";
  switchTab(initialTab);
  if (initialTab !== "overview") loadStatus();
  connectWs();
  startPolling();
}

boot();
