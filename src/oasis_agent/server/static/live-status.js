import { $, apiGet, esc, fmtTime, renderStats, renderTable } from "./core.js";

const STATES = {
  unconfigured: ["尚未连接实盘记录", "在服务配置中指定实盘状态文件，即可查看只读快照。"],
  missing: ["尚无实盘记录", "配置的状态文件尚不存在。此页面不会创建钱包或启动交易。"],
  invalid: ["状态记录无法校验", "记录损坏或格式不受支持，已清除数值。请检查本地状态文件。"],
  signing_unknown: ["签名结果未知，等待恢复核验", "保留当前记录，先核对签名服务的承诺与交易回执；禁止自动重签或重发。"],
  halted: ["已记录停机", "先核查停机原因与资金变化，保留原始记录；不要直接清除停机标记。"],
  pending: ["交易等待对账", "先核对已有交易的回执与资金证据，避免重复提交。"],
  stale: ["观测需要更新", "当前记录不能说明此刻的钱包状态，请运行只读检查并核查观测来源。"],
  observed: ["已取得近期观测", "仅展示本地记录，不代表进程正在运行、资金已完整对账或获得交易许可。"],
};
const BLOCKERS = {
  live_state_unconfigured: "未配置实盘状态文件",
  live_state_missing: "状态文件不存在",
  live_state_invalid: "记录格式或完整性读取失败",
  observation_missing: "缺少账户观测，或交易后尚未重新估值",
  observation_clock_invalid: "观测时间不一致",
  observation_stale: "账户观测超过 120 秒",
  recorded_loss_budget_exhausted: "记录中的亏损预算已耗尽",
  state_halted: "实盘循环已记录停机",
  pending_requires_reconciliation: "存在待对账交易",
  signing_outcome_unresolved: "签名尝试尚未确认结果",
  historical_native_cost_incomplete: "历史 gas 成本不完整",
};
const ACTIONS = {approve_usdc: "USDC 授权", approve_permit2: "Permit2 授权", swap: "兑换", mint: "建仓", burn: "撤仓", reverted: "交易回滚"};
const uint = value => Number.isSafeInteger(value) && value >= 0 || typeof value === "string" && /^(0|[1-9][0-9]{0,77})$/.test(value);
const hash = value => typeof value === "string" && /^0x[0-9a-f]{64}$/.test(value);
const usd = value => typeof value === "number" && Number.isFinite(value) && value >= 0
  ? "$" + value.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2}) : "未观测";

export function formatNative(raw) {
  if (typeof raw !== "string" || !/^(0|[1-9][0-9]*)$/.test(raw) || raw.length > 78) return "未记录";
  const padded = raw.padStart(19, "0");
  return padded.slice(0, -18) + "." + padded.slice(-18).replace(/0+$/, "").padEnd(1, "0") + " ETH";
}

export function liveStatusModel(data) {
  if (!data || data.schema !== "oasis.live-status.v1" || data.execution_eligible !== false ||
      data.evidence_replayed !== false || data.source !== "local_state_declaration" ||
      data.profitability !== "unresolved" || !Object.hasOwn(STATES, data.status) ||
      !Array.isArray(data.blockers) || data.blockers.some(x => !Object.hasOwn(BLOCKERS, x)) ||
      !Array.isArray(data.recent_actions)) return null;
  if (!data.release || data.release.schema_version !== "oasis.live-release.v1" ||
      data.release.new_actions_allowed !== false || !Array.isArray(data.release.blockers) ||
      data.release.blockers.length === 0 || data.release.blockers.some(x =>
        !x || typeof x.title !== "string" || typeof x.required !== "string")) return null;
  if (["unconfigured", "missing", "invalid"].includes(data.status)) return {...data, observation: null, pending: null, signing_unresolved: null, position: null, recent_actions: [], native_gas_units: null};
  if (typeof data.signing_unresolved !== "boolean" ||
      data.signing_unresolved !== (data.status === "signing_unknown") ||
      data.signing_unresolved !== data.blockers.includes("signing_outcome_unresolved")) return null;
  const o = data.observation;
  if (o && (!uint(o.block) || !hash(o.block_hash) || !/^0x[0-9a-f]{40}$/.test(o.account) ||
      !Number.isSafeInteger(o.observed_at) || !Number.isSafeInteger(o.block_timestamp) ||
      ["nav_usd", "daily_loss_usd", "cumulative_loss_usd", "capital_limit_usd", "daily_loss_limit_usd", "cumulative_loss_limit_usd"]
        .some(key => typeof o[key] !== "number" || !Number.isFinite(o[key]) || o[key] < 0))) return null;
  if (data.status === "observed" && (!o || data.blockers.length ||
      !Number.isSafeInteger(data.observation_age_seconds) || data.observation_age_seconds < 0 || data.observation_age_seconds > 120)) return null;
  if (data.pending && (!Object.hasOwn(ACTIONS, data.pending.kind) || !hash(data.pending.tx_hash) ||
      !uint(data.pending.nonce) || !Number.isSafeInteger(data.pending.signed_at))) return null;
  if (data.position && (!uint(data.position.token_id) || !uint(data.position.liquidity) ||
      !Number.isSafeInteger(data.position.lower) || !Number.isSafeInteger(data.position.upper))) return null;
  if (data.recent_actions.length > 20 || data.recent_actions.some(row => !row || !Object.hasOwn(ACTIONS, row.action) ||
      row.transaction_hash !== null && !hash(row.transaction_hash) ||
      [row.cost_references, row.cash_references, row.position_references].some(n => !Number.isSafeInteger(n) || n < 0 || n > 4))) return null;
  return data;
}

export function renderLiveStatus(data) {
  const model = liveStatusModel(data);
  $("live-source").textContent = "本地状态记录 · 未重放原始证据 · 净收益未验证";
  if (!model) {
    $("live-title").textContent = "实盘状态暂不可用";
    $("live-next").textContent = "连接或数据校验失败，已清除旧结果；恢复后刷新。";
    for (const id of ["live-stats", "live-release", "live-blockers", "live-pending", "live-position", "live-observation"]) $(id).textContent = "";
    renderTable("live-actions", [], [], "记录未验证");
    return;
  }
  $("live-release").innerHTML = `<strong>新交易启动已阻断 · ${model.release.blockers.length} 项发布验收项</strong><ul>${model.release.blockers.map(item => `<li>${esc(item.title)}：${esc(item.required)}</li>`).join("")}</ul>`;
  $("live-title").textContent = STATES[model.status][0];
  $("live-next").textContent = STATES[model.status][1];
  $("live-blockers").innerHTML = model.blockers.map(code => `<li>${esc(BLOCKERS[code])}</li>`).join("");
  const o = model.observation;
  renderStats("live-stats", [
    {label: o?.valuation_basis === "two_feed_principal_and_fees" ? "记录 NAV · 双喂价、本金与未收手续费" : "历史 NAV · 未含完整未收手续费", value: usd(o?.nav_usd), tone: o ? "" : "warn"},
    {label: "当日检查点累计亏损 / 上限", value: usd(o?.daily_loss_usd) + " / " + usd(o?.daily_loss_limit_usd), tone: "warn"},
    {label: "累计净资本亏损 / 上限", value: usd(o?.cumulative_loss_usd) + " / " + usd(o?.cumulative_loss_limit_usd), tone: "warn"},
    {label: "已记账交易 gas · ETH", value: formatNative(model.native_gas_units)},
  ]);
  $("live-observation").textContent = o
    ? `观测于 ${fmtTime(o.observed_at)} · 区块 ${o.block} · 数据年龄 ${model.observation_age_seconds} 秒 · 配置资本上限 ${usd(o.capital_limit_usd)}\n账户 ${o.account}\n区块哈希 ${o.block_hash}`
    : "没有可用账户观测；数值缺失不等于余额或亏损为零。";
  const p = model.pending;
  $("live-pending").textContent = model.signing_unresolved
    ? "签名结果未知；尚不能认定交易未签名或未提交。先核对签名服务日志与回执，禁止自动重签或重发。"
    : p
    ? `${ACTIONS[p.kind] || "待处理操作"} · nonce ${p.nonce}\n${p.tx_hash}\n记录签名时间 ${fmtTime(p.signed_at)}；回执、资金和持仓仍需核验。`
    : ["missing", "invalid", "unconfigured"].includes(model.status) ? "待处理状态未知" : "本地记录无待处理交易；尚未查询链上。";
  const position = model.position;
  $("live-position").textContent = position
    ? `NFT #${position.token_id} · tick [${position.lower}, ${position.upper})\n记录流动性 ${position.liquidity}；当前链上持仓尚未重放。`
    : ["missing", "invalid", "unconfigured"].includes(model.status) ? "持仓状态未知" : "本地未记录 LP 持仓。";
  renderTable("live-actions", ["动作", "交易哈希", "完整 gas 记录", "成本 / 资金 / 持仓引用"], model.recent_actions.map(row => [
    esc(ACTIONS[row.action] || "未知动作"), esc(row.transaction_hash || "未记录"), esc(formatNative(row.native_gas_units)),
    esc(`${row.cost_references} / ${row.cash_references} / ${row.position_references} · 未重放`),
  ]), "暂无已记账动作");
}

export async function loadLiveStatus() {
  try { renderLiveStatus(await apiGet("/api/v1/live/status")); }
  catch { renderLiveStatus(null); }
}
