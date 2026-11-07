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
  observation_stale: "完整账户观测超过 120 秒；区间检查不会更新净值",
  nav_validity_unverified: "记录缺少完整 NAV 有效期；历史观测不能确认当前 USD 喂价时效",
  usd_feed_policy_mismatch: "记录的 ETH 或 USDC 喂价上限不符合 1,800 秒要求；需核查配置迁移",
  nav_validity_expired: "记录的完整 NAV 已到期；较新的区块或区间检查不能延长 USD 喂价有效期",
  recorded_loss_budget_exhausted: "记录中的亏损预算已耗尽",
  state_halted: "实盘循环已记录停机",
  pending_requires_reconciliation: "存在待对账交易",
  signing_outcome_unresolved: "签名尝试尚未确认结果",
  historical_native_cost_incomplete: "历史 gas 成本不完整",
};
const ACTIONS = {approve_usdc: "USDC 授权", approve_permit2: "Permit2 授权", swap: "兑换", mint: "建仓", burn: "撤仓", reverted: "交易回滚"};
const START_BLOCKERS = {
  release_gate_closed: "发布门禁未通过，本地待启动配置未检查",
  live_configuration_unconfigured: "未指定本地待启动配置",
  live_configuration_missing: "本地待启动配置不存在",
  live_configuration_invalid: "本地待启动配置无法校验",
  live_rpc_profile_missing: "本地 RPC 额度与间隔配置不存在",
  live_rpc_profile_invalid: "本地 RPC 额度与间隔配置无法校验",
  gas_oracle_runtime_pins_missing: "本地待启动配置缺少 Gas 预言机运行时指纹",
};
const GAS_ROLES = {
  gas_oracle_proxy: "Gas 预言机代理代码",
  gas_oracle_implementation: "Gas 预言机实现地址",
  gas_oracle_implementation_code: "Gas 预言机实现代码",
};
const UNVERIFIED = {
  runtime_image_identity: "实际镜像与代码身份",
  original_service_state: "双服务原始状态与配置迁移",
  stop_controls: "STOP 与停机控制状态",
  fresh_usd_nav: "新鲜 USD 喂价及完整净值",
  dual_source_runtime_simulation: "双源运行时与交易模拟",
  receipt_fund_recovery: "真实回执、资金及撤仓恢复",
  state_configuration_binding: "当前记录与待启动配置的绑定",
};
const PAIR_MAX_AGE = 300;
const PAIR_STATES = {
  unconfigured: "未配置双服务离线检查摘要",
  missing: "双服务检查摘要不存在",
  invalid: "双服务检查摘要无法校验",
  stale: "双服务检查摘要已过期",
  configuration_mismatch: "检查记录与待启动配置不一致",
  projection_incomplete: "日志已验证，JSON 投影尚未同步",
  aligned: "检查时双服务配置与投影一致",
};
const PAIR_BLOCKERS = {
  pair_inspection_unconfigured: "请先导出原始双服务离线检查摘要",
  pair_inspection_missing: "配置的检查摘要不存在",
  pair_inspection_invalid: "摘要格式、字段或内容哈希无法校验",
  pair_inspection_clock_invalid: "摘要检查时间在未来",
  pair_inspection_stale: "离线检查已超过 300 秒；页面刷新不会更新检查时间",
  start_configuration_unverified: "待启动配置未验证",
  start_configuration_mismatch: "检查目标与本地待启动配置不同",
  service_configuration_disagreement: "两服务绑定的配置不同",
  service_position_disagreement: "两服务记录的持仓不同",
};
for (const [role, label] of [["strategy", "策略"], ["authority", "签名服务"]]) {
  for (const [code, text] of Object.entries({configuration_mismatch: "绑定与检查目标不同",
    projection_incomplete: "JSON 投影落后于已验证日志", halted: "仍保留停机标记",
    stop_present: "检查时存在 STOP 文件", stop_unverified: "摘要未记录 STOP 文件状态",
    has_pending: "存在待对账交易", has_signing_attempt: "签名结果未知",
    legacy_gas_cost_incomplete: "历史 gas 成本不完整"})) PAIR_BLOCKERS[`${role}_${code}`] = `${label}：${text}`;
}
const digest = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);

function pairValid(p, startup) {
  if (!p || p.schema !== "oasis.live-pair-status.v1" ||
      p.source !== "local_pair_inspection_declaration" || p.execution_eligible !== false ||
      p.evidence_replayed !== false || p.rpc_requests !== 0 || !Object.hasOwn(PAIR_STATES, p.status) ||
      !Array.isArray(p.blockers) || p.blockers.some(code => !Object.hasOwn(PAIR_BLOCKERS, code))) return false;
  if (["unconfigured", "missing", "invalid"].includes(p.status)) {
    const codes = {unconfigured: ["pair_inspection_unconfigured"], missing: ["pair_inspection_missing"],
      invalid: ["pair_inspection_invalid", "pair_inspection_clock_invalid"]};
    return p.inspection === null && p.inspection_age_seconds === null &&
      p.blockers.length === 1 && codes[p.status].includes(p.blockers[0]);
  }
  const r = p.inspection;
  if (!r || r.schema !== "oasis.live-pair-inspection.v1" || r.execution_eligible !== false ||
      r.historical_only !== true || r.rpc_requests !== 0 || !digest(r.expected_config_hash) || !digest(r.evidence_hash) ||
      !Number.isSafeInteger(r.inspected_at) || r.inspected_at < 0 ||
      !Number.isSafeInteger(p.inspection_age_seconds) || p.inspection_age_seconds < 0 ||
      !r.services || Object.keys(r.services).sort().join() !== "authority,strategy") return false;
  const blockers = [];
  for (const role of ["strategy", "authority"]) {
    const s = r.services[role];
    if (!s || ["config_hash", "journal_tip", "state_hash", "projection_hash", "position_identity_hash"].some(key => !digest(s[key])) ||
        ["projection_current", "runtime_epoch_present", "halted", "has_pending", "has_signing_attempt", "legacy_gas_cost_incomplete"].some(key => typeof s[key] !== "boolean") ||
        (s.stop_present !== undefined && typeof s.stop_present !== "boolean") ||
        s.projection_current !== (s.state_hash === s.projection_hash)) return false;
    if (s.config_hash !== r.expected_config_hash) blockers.push(`${role}_configuration_mismatch`);
    if (!s.projection_current) blockers.push(`${role}_projection_incomplete`);
    for (const flag of ["halted", "has_pending", "has_signing_attempt", "legacy_gas_cost_incomplete"]) if (s[flag]) blockers.push(`${role}_${flag}`);
    if (s.stop_present === undefined) blockers.push(`${role}_stop_unverified`);
    else if (s.stop_present) blockers.push(`${role}_stop_present`);
  }
  const a = r.services.strategy, b = r.services.authority;
  if (a.config_hash !== b.config_hash) blockers.push("service_configuration_disagreement");
  if (a.position_identity_hash !== b.position_identity_hash) blockers.push("service_position_disagreement");
  if (startup.configuration_hash === null) blockers.push("start_configuration_unverified");
  else if (startup.configuration_hash !== r.expected_config_hash) blockers.push("start_configuration_mismatch");
  let status = blockers.some(code => code.includes("configuration_mismatch") || code.includes("configuration_disagreement"))
    ? "configuration_mismatch" : a.projection_current && b.projection_current ? "aligned" : "projection_incomplete";
  if (p.inspection_age_seconds > PAIR_MAX_AGE) { status = "stale"; blockers.push("pair_inspection_stale"); }
  return p.status === status && JSON.stringify(p.blockers) === JSON.stringify(blockers);
}

function renderPair(p) {
  const r = p.inspection;
  $("live-pair").innerHTML = `<strong>${PAIR_STATES[p.status]}</strong>` +
    (r ? `<p>检查于 ${fmtTime(r.inspected_at)} · 检查年龄 ${p.inspection_age_seconds} 秒 · 摘要 ${esc(r.evidence_hash.slice(0, 12))}</p>` +
      ["strategy", "authority"].map(role => {
        const s = r.services[role];
        return `<p>${role === "strategy" ? "策略" : "签名服务"}：配置 ${esc(s.config_hash.slice(0, 12))} · 日志 ${esc(s.journal_tip.slice(0, 12))} · ${s.projection_current ? "投影一致" : "投影落后"} · ${s.runtime_epoch_present ? "已记录运行时迁移" : "未记录运行时迁移"}</p>`;
      }).join("") : "") +
    `<ul>${p.blockers.map(code => `<li>${esc(PAIR_BLOCKERS[code])}</li>`).join("")}</ul>` +
    `<p>仅展示本地离线检查声明；服务器未重放日志或链上证据。检查时间不能更新 USD 喂价或 NAV 时效，不解除停机或授予交易许可。</p>`;
}
const uint = value => Number.isSafeInteger(value) && value >= 0 || typeof value === "string" && /^(0|[1-9][0-9]{0,77})$/.test(value);
const hash = value => typeof value === "string" && /^0x[0-9a-f]{64}$/.test(value);
const usd = value => typeof value === "number" && Number.isFinite(value) && value >= 0
  ? "$" + value.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2}) : "未观测";

function startupValid(p, release) {
  if (!p || p.schema !== "oasis.live-startup-readiness.v1" ||
      p.source !== "local_start_configuration" || p.execution_eligible !== false ||
      p.startup_prerequisites_passed !== false || p.evidence_replayed !== false || p.rpc_requests !== 0 ||
      !Array.isArray(p.blockers) || p.blockers.some(x => !Object.hasOwn(START_BLOCKERS, x)) ||
      !Array.isArray(p.missing_runtime_roles) || p.missing_runtime_roles.some(x => !Object.hasOwn(GAS_ROLES, x)) ||
      new Set(p.missing_runtime_roles).size !== p.missing_runtime_roles.length ||
      !Array.isArray(p.unverified_checks) || p.unverified_checks.length !== Object.keys(UNVERIFIED).length ||
      p.unverified_checks.some(x => !Object.hasOwn(UNVERIFIED, x)) ||
      new Set(p.unverified_checks).size !== p.unverified_checks.length) return false;
  const permit = release.acceptance_permit;
  if (permit != null && (!Object.hasOwn(ACTIONS, permit.kind) || !uint(permit.account_nonce) ||
      typeof permit.authorized_by !== "string" || !permit.authorized_by)) return false;
  const releasePassed = permit != null || release.new_actions_allowed && release.blockers.length === 0;
  if (p.release_gate_passed !== releasePassed ||
      p.local_guards_passed !== (releasePassed && p.configuration_status === "declared_execution_policy" && p.blockers.length === 0)) return false;
  const empty = p.configuration_hash === null && p.configured_runtime_roles === null && p.missing_runtime_roles.length === 0;
  if (!releasePassed) return p.configuration_status === "not_checked" && empty &&
    p.blockers.length === 1 && p.blockers[0] === "release_gate_closed";
  const unknown = {unconfigured: "live_configuration_unconfigured", missing: "live_configuration_missing", invalid: "live_configuration_invalid"};
  if (Object.hasOwn(unknown, p.configuration_status)) return empty && p.blockers.length === 1 &&
    p.blockers[0] === unknown[p.configuration_status];
  if (typeof p.configuration_hash !== "string" || !/^[0-9a-f]{64}$/.test(p.configuration_hash)) return false;
  if (p.configuration_status === "legacy_runtime_policy") return p.configured_runtime_roles === 5 &&
    p.missing_runtime_roles.length === 3 && p.blockers.length === 1 && p.blockers[0] === "gas_oracle_runtime_pins_missing";
  return p.configuration_status === "declared_execution_policy" && p.configured_runtime_roles === 8 &&
    p.missing_runtime_roles.length === 0 && (p.blockers.length === 0 ||
    p.blockers.length === 1 && ["live_rpc_profile_missing", "live_rpc_profile_invalid"].includes(p.blockers[0]));
}

export function formatNative(raw) {
  if (typeof raw !== "string" || !/^(0|[1-9][0-9]*)$/.test(raw) || raw.length > 78) return "未记录";
  const padded = raw.padStart(19, "0");
  return padded.slice(0, -18) + "." + padded.slice(-18).replace(/0+$/, "").padEnd(1, "0") + " ETH";
}

const BOUNDED_STATUS = {
  unconfigured: "未配置本地有界运行记录",
  missing: "暂无本轮运行记录",
  invalid: "本轮记录无法校验",
  launch_requested: "本轮启动已发起 · 等待停机证据",
  cleanup_unverified: "本轮停机或双 STOP 恢复待核对",
  stopped: "本轮已停机 · 双 STOP 已恢复",
};
const BOUNDED_RESULT = {
  completed: "单轮完成", failed: "失败", timed_out: "超时", interrupted: "已中断",
};

const BOUNDED_BLOCKER = {
  rpc_budget_insufficient: "累计 RPC 额度不足",
  rpc_budget_unverified: "累计 RPC 预算未核验",
  post_stop_reconciliation_required: "待核查停机或对账",
  post_stop_replay_unverified: "停机后对账未核验",
};
const BOUNDED_ROLE = {strategy: "策略服务", authority: "签名服务"};

export function boundedRunLabel(row) {
  if (!row || row.execution_eligible !== false || !Object.hasOwn(BOUNDED_STATUS, row.status)) return null;
  const attempted = row.start_attempted ?? null;
  const blocker = row.blocker ?? null;
  const role = row.blocked_service ?? null;
  if (attempted !== null && typeof attempted !== "boolean" ||
      blocker !== null && (typeof blocker !== "string" || !Object.hasOwn(BOUNDED_BLOCKER, blocker)) ||
      role !== null && (typeof role !== "string" || !Object.hasOwn(BOUNDED_ROLE, role))) return null;
  if (["unconfigured", "missing", "invalid"].includes(row.status)) {
    return row.attempt_id === null && row.as_of === null && row.result === null &&
      row.stop_present === null && attempted === null && blocker === null && role === null
      ? BOUNDED_STATUS[row.status] : null;
  }
  if (!/^[0-9a-f]{32}$/.test(row.attempt_id ?? "") || !Number.isSafeInteger(row.as_of) ||
      row.as_of < 0 || row.result !== null && !Object.hasOwn(BOUNDED_RESULT, row.result)) return null;
  const stops = row.stop_present;
  if (row.result === null ? stops !== null : !stops || typeof stops.strategy !== "boolean" ||
      typeof stops.authority !== "boolean") return null;
  if (row.status === "stopped" && (!stops?.strategy || !stops?.authority) ||
      row.result === "completed" && (row.status !== "stopped" || attempted === false || blocker !== null) ||
      row.status === "launch_requested" && row.result !== null) return null;
  if (row.result === null) {
    return attempted === null && blocker === null && role === null ? BOUNDED_STATUS[row.status] : null;
  }
  if (blocker?.startsWith("rpc_budget_") && attempted !== false ||
      blocker?.startsWith("post_stop_") && (attempted !== true || row.status !== "stopped") ||
      (blocker === "post_stop_reconciliation_required") !== (role !== null)) return null;
  const status = row.status === "stopped" && attempted === false
    ? "本轮未发起启动 · 双 STOP 已核对" : BOUNDED_STATUS[row.status];
  const labels = [status, BOUNDED_RESULT[row.result]];
  if (blocker !== null) labels.push(role === null ? BOUNDED_BLOCKER[blocker] :
    BOUNDED_ROLE[role] + " " + BOUNDED_BLOCKER[blocker]);
  return labels.join(" · ");
}

export function liveStatusModel(data) {
  if (!data || data.schema !== "oasis.live-status.v1" || data.execution_eligible !== false ||
      data.evidence_replayed !== false || data.source !== "local_state_declaration" ||
      data.profitability !== "unresolved" || !Object.hasOwn(STATES, data.status) ||
      !Array.isArray(data.blockers) || data.blockers.some(x => !Object.hasOwn(BLOCKERS, x)) ||
      !Array.isArray(data.recent_actions)) return null;
  const r = data.release;
  if (!r || r.schema_version !== "oasis.live-release.v1" || !Array.isArray(r.blockers) ||
      typeof r.new_actions_allowed !== "boolean" || r.blockers.some(x =>
        !x || typeof x.title !== "string" || typeof x.required !== "string")) return null;
  // Exactly two coherent shapes: closed with blockers, or open with none and its evidence.
  if (r.new_actions_allowed ? r.blockers.length !== 0 || typeof r.release_evidence !== "string" ||
      !r.release_evidence : r.blockers.length === 0) return null;
  if (!startupValid(data.startup, r) || !pairValid(data.pair, data.startup)) return null;
  if (["unconfigured", "missing", "invalid"].includes(data.status)) return {...data, observation: null, pending: null, signing_unresolved: null, position: null, recent_actions: [], native_gas_units: null};
  if (typeof data.signing_unresolved !== "boolean" ||
      data.signing_unresolved !== (data.status === "signing_unknown") ||
      data.signing_unresolved !== data.blockers.includes("signing_outcome_unresolved")) return null;
  const o = data.observation;
  if (o && (!uint(o.block) || !hash(o.block_hash) || !/^0x[0-9a-f]{40}$/.test(o.account) ||
      !Number.isSafeInteger(o.observed_at) || !Number.isSafeInteger(o.block_timestamp) ||
      ["nav_usd", "daily_loss_usd", "cumulative_loss_usd", "capital_limit_usd", "daily_loss_limit_usd", "cumulative_loss_limit_usd"]
        .some(key => typeof o[key] !== "number" || !Number.isFinite(o[key]) || o[key] < 0))) return null;
  if (o && ["nav_valid_until", "eth_feed_max_age_seconds", "usdc_feed_max_age_seconds"].some(key =>
    o[key] !== null && o[key] !== undefined && (!Number.isSafeInteger(o[key]) || o[key] < 0))) return null;
  const l = data.light_check;
  if (l !== null && l !== undefined && (!o || !uint(l.block) || !Number.isSafeInteger(l.observed_at) ||
      !Number.isSafeInteger(l.block_timestamp) || !Number.isSafeInteger(l.tick) ||
      l.block <= o.block || l.observed_at < o.observed_at)) return null;
  // Tick-only activity does not renew wallet, USD-feed or full NAV freshness.
  if (data.status === "observed" && (!o || data.blockers.length ||
      !Number.isSafeInteger(data.observation_age_seconds) || data.observation_age_seconds < 0 || data.observation_age_seconds > 120 ||
      o.valuation_basis !== "two_feed_principal_and_fees" || !Number.isSafeInteger(o.nav_valid_until) ||
      o.block_timestamp > o.observed_at ||
      o.nav_valid_until < o.block_timestamp || o.nav_valid_until > o.block_timestamp + 120 ||
      o.observed_at + data.observation_age_seconds > o.nav_valid_until ||
      [o.eth_feed_max_age_seconds, o.usdc_feed_max_age_seconds].some(limit =>
        !Number.isSafeInteger(limit) || limit < 1 || limit > 1800))) return null;
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
    for (const id of ["live-stats", "live-release", "live-startup", "live-bounded-run", "live-pair", "live-blockers", "live-pending", "live-position", "live-observation"]) $(id).textContent = "";
    renderTable("live-actions", [], [], "记录未验证");
    return;
  }
  const release = model.release;
  const startup = model.startup;
  renderPair(model.pair);
  $("live-bounded-run").textContent = boundedRunLabel(model.bounded_run) ?? BOUNDED_STATUS.invalid;
  $("live-startup").innerHTML = `<strong>${startup.local_guards_passed ? "本地静态检查通过 · 执行资格未核验" : "本地静态检查未通过"}</strong>` +
    (startup.configured_runtime_roles === null ? "" : `<p>本地待启动配置指纹 ${startup.configured_runtime_roles} / 8 · 配置摘要 ${esc(startup.configuration_hash.slice(0, 12))}</p>`) +
    `<ul>${startup.blockers.map(code => `<li>${esc(START_BLOCKERS[code])}</li>`).join("")}` +
    startup.missing_runtime_roles.map(role => `<li>缺少：${esc(GAS_ROLES[role])}</li>`).join("") + `</ul>` +
    `<details><summary>仍未核验的运行条件</summary><ul>${startup.unverified_checks.map(code => `<li>${esc(UNVERIFIED[code])}</li>`).join("")}</ul></details>` +
    `<p>这里只读取本地待启动配置和 RPC 限额声明；不代表生产镜像、链上状态或交易许可。</p>`;
  // Static release approval says nothing about current service health or admission.
  const runtimeStatus = model.status === "observed" && startup.local_guards_passed &&
      model.pair.status === "aligned" && model.pair.blockers.length === 0
    ? "当前执行资格未核验" : "当前运行条件未满足";
  $("live-release").innerHTML = release.new_actions_allowed
    ? `<strong>${runtimeStatus} · USD ${esc(release.capital_limit_usd)} 金丝雀发布授权已记录 · 日亏损上限 USD ${esc(release.daily_loss_limit_usd)} · 累计 USD ${esc(release.cumulative_loss_limit_usd)}</strong><ul><li>${esc(release.release_evidence)}</li><li>启动仍需当前喂价、完整净值及双服务状态通过核验。</li></ul>`
    : `<strong>新交易启动已阻断 · ${release.blockers.length} 项发布验收项</strong><ul>${release.blockers.map(item => `<li>${esc(item.title)}：${esc(item.required)}</li>`).join("")}</ul>`;
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
    ? `完整观测于 ${fmtTime(o.observed_at)} · 区块 ${o.block} · 数据年龄 ${model.observation_age_seconds} 秒 · 配置资本上限 ${usd(o.capital_limit_usd)}` +
      `\n记录 NAV 有效至 ${o.nav_valid_until == null ? "未记录" : fmtTime(o.nav_valid_until)} · ETH / USDC 喂价上限 ${o.eth_feed_max_age_seconds ?? "未记录"} / ${o.usdc_feed_max_age_seconds ?? "未记录"} 秒（原始喂价未重放）` +
      (model.light_check ? `\n区间检查于 ${fmtTime(model.light_check.observed_at)} · 区块 ${model.light_check.block} · tick ${model.light_check.tick}（双源一致，仅确认仍在区间，不更新净值或喂价时效）` : "") +
      `\n账户 ${o.account}\n区块哈希 ${o.block_hash}`
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

let liveRead = null;
let statusExpiry = null;

export function loadLiveStatus() {
  if (liveRead) return liveRead;
  const requestedAt = performance.now();
  liveRead = apiGet("/api/v1/live/status").then(data => {
    clearTimeout(statusExpiry);
    const model = liveStatusModel(data);
    if (!model) {
      renderLiveStatus(null);
      return;
    }
    const {observation: o, observation_age_seconds: age, pair} = model;
    // Age both declarations from the start of the read. An in-flight refresh
    // leaves the displayed snapshot's expiry active.
    const navDeadline = model.status === "observed"
      ? (o.nav_valid_until - o.observed_at - age) * 1000 : Infinity;
    // The API reports whole seconds and marks the pair stale above the limit.
    const pairDeadline = pair.inspection && pair.status !== "stale"
      ? (PAIR_MAX_AGE + 1 - pair.inspection_age_seconds) * 1000 : Infinity;
    const renderSnapshot = () => {
      const elapsed = performance.now() - requestedAt;
      const seconds = Math.floor(elapsed / 1000);
      const snapshot = {...model};
      if (elapsed >= navDeadline) {
        snapshot.status = "stale";
        snapshot.blockers = ["nav_validity_expired"];
        snapshot.observation_age_seconds = age + seconds;
      }
      if (elapsed >= pairDeadline) {
        snapshot.pair = {...pair, status: "stale", inspection_age_seconds: pair.inspection_age_seconds + seconds,
          blockers: [...pair.blockers, "pair_inspection_stale"]};
      }
      renderLiveStatus(snapshot);
      const next = Math.min(elapsed < navDeadline ? navDeadline : Infinity,
        elapsed < pairDeadline ? pairDeadline : Infinity);
      if (Number.isFinite(next)) {
        statusExpiry = setTimeout(renderSnapshot, Math.max(0, requestedAt + next - performance.now()));
      }
    };
    renderSnapshot();
  }).catch(() => {
    clearTimeout(statusExpiry);
    renderLiveStatus(null);
  }).finally(() => { liveRead = null; });
  return liveRead;
}
