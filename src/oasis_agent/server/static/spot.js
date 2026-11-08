import { esc, apiGet, renderTable, renderStats } from "./core.js";

// Integer USD micros -> "12.345678" without binary-float rounding of money.
export function usd(micros) {
  if (!Number.isSafeInteger(micros)) return "–";
  const sign = micros < 0 ? "-" : "";
  const abs = Math.abs(micros);
  return sign + Math.floor(abs / 1e6) + "." + String(abs % 1e6).padStart(6, "0").slice(0, 2);
}

export function bps(value) {
  if (!Number.isSafeInteger(value)) return "–";
  return (value / 100).toFixed(2) + "%";
}

function when(seconds) {
  if (!Number.isSafeInteger(seconds)) return "–";
  return new Date(seconds * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

const ACTION_LABEL = { flip: "短线买入", watch: "观望", skip: "跳过", lp_candidate: "转 LP 评估" };
const KIND_LABEL = {
  paper_buy: "纸面买入", paper_sell: "纸面卖出", live_buy: "实盘买入", live_sell: "实盘卖出",
  live_unresolved: "实盘未确认（需人工复核）", halt_review: "停止复核",
  skip_entry: "放弃入场", mark_missed: "未能估值",
};
const BLOCK_LABEL = {
  live_halt_recorded: "已记录实盘停机",
  tx_reused_across_intents: "同一交易被多个订单引用",
  intent_identity_invalid: "订单身份字段缺失或无效",
  human_review_halt: "卖出被拒，需人工处理持仓",
  live_intent_without_outcome: "有已提交但无结果的订单",
  live_order_unresolved: "有结果未确认的订单",
  live_fill_not_booked: "有成交未入账",
  interrupted_tick: "上一轮在交易中途中断",
  trades_without_tick_summary: "有交易但无 tick 汇总",
  legacy_live_lifecycle_v1: "旧版实盘日志（无订单关联，只读）",
  booking_mismatch: "入账与成交不符",
  fill_mismatch: "成交与意图不符",
  duplicate_fill: "重复成交记录",
  duplicate_booking: "重复入账",
  duplicate_intent: "重复订单意图",
  status_unavailable: "状态不可用",
};
const VERDICT = {
  // Descriptive only: overlapping label rows of rule-selected groups, not an edge test.
  insufficient_label_rows: "标签行不足，仅可描述",
  flip_median_higher_descriptive: "描述性：短线组 1 小时中位标签收益较高（重叠观测、非因果，不代表选股优势）",
  flip_median_not_higher_descriptive: "描述性：短线组 1 小时中位标签收益不高于其余组（重叠观测、非因果，不能据此判定选股无优势）",
  // Older servers' verdicts, shown with the same caveat.
  insufficient_samples: "标签行不足，仅可描述",
  flip_outperforms: "描述性：短线组中位标签收益较高（非因果，不代表选股优势）",
  no_selection_edge: "描述性：短线组中位标签收益不高（非因果，不能据此判定选股无优势）",
};

// "<wei> (<observed>/<settled> 笔)": orders without an observed item stay visible, never zero.
export function receiptFees(report) {
  const fees = (report.accounting || {}).receipt_fees;
  if (!fees || !fees.execution) return "未记录";
  const n = fees.execution.observed_orders, total = fees.settled_orders;
  return n ? `${fees.execution.observed_wei}（${n}/${total} 笔）` : `未观测（0/${total} 笔）`;
}

function horizonRows(table) {
  const rows = [];
  for (const [horizon, groups] of Object.entries(table)) {
    for (const action of ["flip", "watch", "skip", "lp_candidate"]) {
      const g = groups[action];
      if (!g || !g.n) continue;
      rows.push([esc(Number(horizon) / 60 + " 分钟"), esc(ACTION_LABEL[action]), esc(g.n),
        esc(bps(g.median_bps)), esc(bps(g.mean_bps)), esc(bps(g.p10_bps) + " / " + bps(g.p90_bps)),
        esc(bps(g.hit_rate_bps))]);
    }
  }
  return rows;
}

export async function loadSpot() {
  const status = document.getElementById("spot-status");
  let report;
  try {
    report = await apiGet("/api/v1/spot/report");
  } catch (error) {
    status.textContent = error && error.status === 404
      ? "未配置影子运行目录（databases.spot_run）。"
      : "影子日志不可用或校验失败，未显示任何数据。";
    return;
  }
  if (report.execution_eligible !== false || !["shadow", "live"].includes(report.mode)) {
    status.textContent = "响应声明了执行权限，已拒绝显示。";
    return;
  }
  const ops = report.operations, book = report.paper_book, quality = report.signal_quality;
  const live = report.mode === "live";
  // The journal's own recovery verdict (same derivation as resume). A missing status
  // is treated as blocked: the console never infers "fine" from absent data.
  const run = report.run_status || { resumable: false, blocking_reasons: ["status_unavailable"] };
  const reasons = (run.blocking_reasons || []).map(r => BLOCK_LABEL[r] || r);
  const unknown = run.unknown_live_orders || 0;
  const gap = (run.transient_unpriceable || []).length;
  // A live run that stopped before reading the wallet has no observed book: never show
  // paper cash, a zero or "no positions" for it. Absent flag = observed (older servers).
  const observed = (report.accounting || {}).book_observed !== false;
  const UNOBSERVED = "未观测（尚无持久化的钱包余额观测）";
  const money = v => observed ? usd(v) : UNOBSERVED;
  const stopInfo = ops.budget_state || {};
  const stopHeld = Number.isSafeInteger(stopInfo.open_positions)
    ? `停止时策略持仓 ${stopInfo.open_positions}` : "停止时持仓未知";
  const stopStage = (ops.last_budget_stop || {}).stage === "live_start_balance"
    ? "启动前未能读取钱包余额" : "调用预算不足已停止";
  const banner = (live ? "实盘模式（Agentic Wallet）" : "影子模式")
    + (run.resumable ? "" : ` · ⛔ 不可恢复交易：${reasons.join("；")}`)
    + (unknown ? ` · ${unknown} 笔实盘订单结果未知` : "")
    + (gap ? ` · ${gap} 个持仓暂时无法估值（继续观察）` : "")
    + (stopInfo.stopped ? ` · 最近记录：${stopStage}（${stopHeld}）` : "")
    + (observed ? "" : " · 实盘账簿未初始化：现金、权益与钱包持仓均未观测");
  // Process liveness is not provable from a journal; say so instead of claiming "running".
  status.textContent = `${banner} · 进程状态未知（仅日志） · 信号为提供商声明而非链上证明 · 最近 tick ${when(ops.last_at)} · 1 小时标签描述：${VERDICT[quality.verdict_1h] || quality.verdict_1h}`;
  status.classList.toggle("danger", !run.resumable);
  renderStats("spot-stats", [
    { label: live ? "策略权益 USD（非全账户净值）" : "纸面净值 USD", value: money(book.final_nav_usd_micros) },
    ...(live ? [{ label: "估算 gas USD（声明值，非收据）",
      value: usd((report.accounting || {}).estimated_gas_usd_micros) }] : []),
    // Receipt execution fee of settled orders, in wei: evidence only, not booked, no USD.
    ...(live ? [{ label: "收据执行 gas wei（已结算订单，仅执行分项，未入账）",
      value: receiptFees(report) }] : []),
    { label: "已实现 PnL USD", value: usd(book.realized_pnl_usd_micros),
      tone: book.realized_pnl_usd_micros < 0 ? "danger" : "" },
    { label: "平仓 / 盈利", value: book.closed_trades + " / " + book.winning_trades },
    { label: "最大回撤 USD", value: money(book.max_drawdown_usd_micros) },
    // The strategy book's own positions, not a wallet inventory.
    { label: "策略账簿持仓（非钱包盘点）", value: observed ? report.open_positions.length : UNOBSERVED },
    { label: "tick / 调用", value: ops.ticks + " / " + ops.provider_calls },
    { label: "历史停止复核次数", value: ops.halts, tone: ops.halts ? "warn" : "" },
    { label: "已计调用 / 其中未结算预留",
      value: ((ops.budget || {}).charged ?? "–") + " / " + ((ops.budget || {}).unsettled_reserved ?? "–") },
    { label: "当前可恢复", value: run.resumable ? "是" : "否", tone: run.resumable ? "" : "danger" },
  ]);
  renderTable("spot-positions", ["代币", "数量", "成本 USD", "当前可卖 USD", "最高 USD", "开仓时间"],
    report.open_positions.map(p => [esc(p.symbol || p.token), esc(p.units), esc(usd(p.cost_usd_micros)),
      esc(usd(p.last_value_usd_micros)), esc(usd(p.high_water_usd_micros)), esc(when(p.opened_at))]),
    observed ? "策略账簿无持仓（不代表钱包无持仓）" : "实盘账簿未初始化，钱包持仓未观测");
  renderTable("spot-quality", ["周期", "决策", "样本", "中位收益", "平均收益", "P10 / P90", "上涨占比"],
    horizonRows(quality.by_horizon_seconds), "尚无到期的前向标签");
  const [decisions, flips] = await Promise.all([
    apiGet("/api/v1/spot/decisions?limit=50"),
    apiGet("/api/v1/spot/assessments?limit=30&action=flip"),
  ]);
  renderTable("spot-decisions", ["时间", "动作", "代币", "金额 USD", "PnL USD", "原因"],
    decisions.items.map(d => [esc(when(d.at)), esc(KIND_LABEL[d.kind] || d.kind), esc(d.symbol || d.token || "–"),
      esc(usd(d.cost_usd_micros ?? d.proceeds_usd_micros)), esc(usd(d.pnl_usd_micros)),
      esc(d.reason || (d.reasons || []).join(" · ") || "–")]), "尚无交易决策");
  renderTable("spot-flips", ["时间", "代币", "评分", "1h", "5m", "聪明钱钱包", "流动性 USD"],
    flips.items.map(a => [esc(when(a.at)), esc(a.symbol), esc(a.score_bps), esc(bps(a.change_1h_bps)),
      esc(bps(a.change_5m_bps)), esc(a.smart_wallets), esc(usd(a.liquidity_usd_micros))]), "尚无买入信号");
}
