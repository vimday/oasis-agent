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
const VERDICT = {
  insufficient_samples: "样本不足，尚无结论",
  flip_outperforms: "短线组 1 小时中位收益高于对照组",
  no_selection_edge: "短线组没有优于对照组：选股无优势",
};

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
  if (report.execution_eligible !== false || report.mode !== "shadow") {
    status.textContent = "响应声明了执行权限，已拒绝显示。";
    return;
  }
  const ops = report.operations, book = report.paper_book, quality = report.signal_quality;
  status.textContent = `影子模式 · 数据为提供商声明而非链上证明 · 最近 tick ${when(ops.last_at)} · 1 小时结论：${VERDICT[quality.verdict_1h] || quality.verdict_1h}`;
  renderStats("spot-stats", [
    { label: "纸面净值 USD", value: usd(book.final_nav_usd_micros) },
    { label: "已实现 PnL USD", value: usd(book.realized_pnl_usd_micros),
      tone: book.realized_pnl_usd_micros < 0 ? "danger" : "" },
    { label: "平仓 / 盈利", value: book.closed_trades + " / " + book.winning_trades },
    { label: "最大回撤 USD", value: usd(book.max_drawdown_usd_micros) },
    { label: "持仓", value: report.open_positions.length },
    { label: "tick / 调用", value: ops.ticks + " / " + ops.provider_calls },
    { label: "停止复核", value: ops.halts, tone: ops.halts ? "warn" : "" },
  ]);
  renderTable("spot-positions", ["代币", "数量", "成本 USD", "当前可卖 USD", "最高 USD", "开仓时间"],
    report.open_positions.map(p => [esc(p.symbol || p.token), esc(p.units), esc(usd(p.cost_usd_micros)),
      esc(usd(p.last_value_usd_micros)), esc(usd(p.high_water_usd_micros)), esc(when(p.opened_at))]),
    "无持仓");
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
