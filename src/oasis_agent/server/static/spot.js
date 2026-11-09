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
  spot_live_execution_unreleased: "现货执行未发布，历史账簿只读",
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
  intent_without_id: "订单意图缺少 ID",
  live_fill_without_intent: "成交记录找不到对应订单",
  live_unresolved_without_intent: "未确认记录找不到对应订单",
  live_buy_without_intent: "买入入账找不到对应订单",
  live_sell_without_intent: "卖出入账找不到对应订单",
  fill_and_unresolved: "同一订单既有成交又有未确认记录",
  booking_without_fill: "有入账但无成交记录",
  fill_tx_hash_invalid: "成交交易哈希无效",
  fill_amount_invalid: "成交金额字段缺失或无效",
  fill_amount_mismatch: "成交金额与订单意图不符",
  booking_amount_invalid: "入账金额字段缺失或无效",
  booking_amount_mismatch: "入账金额与成交不符",
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

// One retained proof bundle (/api/v1/spot/fill-proof): the report CLI's three per-order
// checks. Evidence only; every refusal is a fixed reason shown with its raw code.
const PROOF_SCHEMA = "oasis.spot-fill-proof.v1";
const PROOF_COLUMNS = ["检查", "状态", "原因", "来源与范围", "数值（原始整数单位）"];
const PROOF_CHECKS = [
  ["cost_replay", "原生币费用（两份收据重放）"],
  ["receipt_transfers", "收据资产匹配（USDC 与代币 Transfer 净额对日志）"],
  ["block_settlement", "区块对账（成交所在区块的钱包余额）"],
];
const PROOF_STATUS = {
  replayed: ["已重放", "ok"], matched: ["与日志一致", "ok"], reconciled: ["范围内已对账", "ok"],
  unbound: ["未绑定", "warn"], unverified: ["未验证", "warn"],
  conflict: ["冲突", "bad"], journal_mismatch: ["与日志不符", "bad"], residual: ["有残差", "bad"],
};
const PROOF_BASIS = {
  frozen_input_reconstruction_not_provider_observation: "由冻结输入重建，非提供方实时观测",
  two_declared_providers_retained_raw_receipt_origin_unknown: "两个声明提供方的保留收据，来源未知",
  not_replayed: "未重放",
};
const PROOF_REASON = {
  binding_malformed: "成本绑定文件格式无效",
  provider_pair_invalid: "提供方声明无效或两份证明不是各一份",
  order_not_matched: "订单在日志中未完成匹配",
  receipt_block_unknown: "成交未记录收据区块哈希",
  block_mismatch: "证明区块不是成交所在区块",
  origin_invalid: "证明来源声明无效或两份不一致",
  providers_disagree: "两个提供方重建的交易成本不一致",
  identity_mismatch: "交易、付款钱包、链或原生币精度与成交不符",
  status_mismatch: "收据状态不是成功",
  single_receipt_conflict: "与日志内已记录的收据费用不符",
  receipt_logs_missing: "收据未保留日志",
  receipt_transaction_index_missing: "收据未声明 transactionIndex（交易在区块中的位置）",
  receipt_transaction_index_invalid: "收据的 transactionIndex 无效",
  log_malformed: "日志格式无效或已标记移除",
  duplicate_log_index: "日志序号重复",
  log_identity_mismatch: "日志不属于该交易或区块",
  log_transaction_index_mismatch: "日志位置与收据交易位置不符",
  transfer_abi_invalid: "Transfer 事件编码无效",
  token_is_settlement: "订单代币就是结算资产 USDC",
  providers_disagree_on_transaction_index: "两个提供方的交易位置不一致",
  providers_disagree_on_transfer_logs: "两个提供方的 Transfer 日志不一致",
  journal_delta_invalid: "日志记录的成交数量无效",
  no_in_scope_transfer_logs: "收据中没有涉及该钱包的 USDC 或代币转账",
  journal_delta_differs_from_receipt_transfers: "收据转账净额与日志成交不符",
  receipt_unreadable: "收据无法读取",
  settlement_binding_malformed: "结算绑定文件格式无效",
  settlement_binding_scope_mismatch: "结算绑定的订单或提供方与成本绑定不符",
  settlement_provider_pair_invalid: "结算证明不是两个提供方各一份",
  settlement_scope_invalid: "无法由成交确定结算范围",
  settlement_evidence_invalid: "结算证明无法按该范围重放",
  settlement_origin_differs_from_cost: "结算证明与成本证明来源不同",
  providers_disagree_on_block_settlement: "两个提供方的整块结算不一致",
  receipt_and_block_transfer_logs_differ: "收据日志与整块日志列表不一致",
  token_balance_or_supply_not_explained_by_transfers: "代币余额或供应变化无法由 Transfer 解释",
  native_balance_not_explained_by_receipt_fee: "原生币余额变化不等于收据费用",
  other_transactions_moved_wallet_in_block: "同一区块内其他交易也触及该钱包",
  block_balance_delta_differs_from_journal: "整块余额变化与日志成交不符",
  settlement_unreadable: "结算证明无法读取",
};
const REASON_SCOPE = [["cost_binding_", "成本证明："], ["receipt_transfers_", "收据转账："]];

// Own labels only: a code such as "constructor" is never read from the prototype.
const own = (table, code) => typeof code === "string" && Object.hasOwn(table, code) ? table[code] : undefined;

// The label of one code, scope prefixes included; undefined when any part is unlabelled.
function reasonText(code) {
  for (const [prefix, scope] of REASON_SCOPE) {
    if (code.startsWith(prefix)) {
      const inner = reasonText(code.slice(prefix.length));
      return inner && scope + inner;
    }
  }
  const proof = /^evidence_invalid_(\d)$/.exec(code);
  return proof ? `第 ${Number(proof[1]) + 1} 份证明无法重放` : own(PROOF_REASON, code);
}

// "<中文>（<full code>）", the same code the report CLI prints; unlabelled stays the raw code.
export function proofReason(reason) {
  if (reason == null) return "–";
  const code = String(reason), text = reasonText(code);
  return text ? `${text}（${code}）` : code;
}

const rootCode = code => {
  for (const [prefix] of REASON_SCOPE) {
    if (code.startsWith(prefix)) return rootCode(code.slice(prefix.length));
  }
  return code;
};

// A labelled code, the raw code when unlabelled, never "undefined".
const labelled = (labels, code) => own(labels, code) || (typeof code === "string" ? code : "未声明");

function abbrev(value) {
  const text = typeof value === "string" ? value : "";
  return text.length > 18 ? text.slice(0, 10) + "…" + text.slice(-6) : text || "–";
}

// Raw integer base units exactly as sent (decimal strings); never parsed into floats.
const units = value => typeof value === "string" && /^-?\d+$/.test(value) ? value
  : Number.isSafeInteger(value) ? String(value) : "–";
// Full identifiers (hashes, addresses, providers) so a fill can be located, never shortened.
const named = value => typeof value === "string" && value ? value : "–";

function proofValues(key, view) {
  const journal = view.journal
    ? ` · 日志 USDC ${units(view.journal.usdc_delta)} · 日志代币 ${units(view.journal.token_delta)}` : "";
  if (key === "cost_replay") {
    const w = view.native_wei;
    return w ? `总费用 ${units(w.total)} wei（执行 ${units(w.execution)} · L1 ${units(w.l1)}`
      + ` · blob ${units(w.blob)} · 运营方 ${units(w.operator)}）` : "–";
  }
  if (key === "receipt_transfers") {
    const a = view.assets;
    return a ? `收据 USDC 净额 ${units(a.usdc.net)} · 收据代币净额 ${units(a.token.net)}${journal}`
      + ` · 交易位置 ${units(view.transaction_index)} · 涉及钱包的日志 ${units(view.logs_in_scope)} 条` : "–";
  }
  const n = view.native_wei, t = view.tokens;
  if (!n || !t) return "–";
  const token = (label, row) => `${label}余额变化 ${units(row.balance_delta)}（残差 ${units(row.residual)}`
    + ` · 供应残差 ${units(row.supply_residual)} · 其他交易日志 ${units(row.other_transaction_logs)} 条）`;
  return `原生币整块净变化 ${units(n.delta)} wei（${units(n.before)} → ${units(n.after)}`
    + ` · 收据费 ${units(n.receipt_fee_total)} · 残差 ${units(n.residual)}）`
    + ` · ${token("USDC ", t.usdc)} · ${token("代币", t.token)}${journal}`;
}

// Who answered and what the check covers. A refusal reached no outcome and states no scope.
function proofScope(key, view) {
  if (!Array.isArray(view.providers)) return "–";
  const from = "提供方 " + view.providers.map(named).join(" + ");
  if (key === "cost_replay") return `${from} · 交易 ${named(view.tx_hash)} · 付款钱包 ${named(view.payer)}`;
  if (key === "receipt_transfers") {
    const a = view.assets || {};
    return `${from} · 钱包 ${named(view.wallet)} · USDC ${named(a.usdc?.token)} · 代币 ${named(a.token?.token)}`;
  }
  const t = view.tokens || {}, b = view.block || {};
  return `${from} · 区块 ${units(b.number)} ${named(b.block_hash)} · 钱包 ${named(view.wallet)}`
    + ` · 仅 USDC ${named(t.usdc?.token)} 与代币 ${named(t.token?.token)}`
    + " · 原生币只比较整块净余额，无逐交易追踪";
}

const INDEPENDENCE = { not_established: "两个来源的独立性未确立" };
const PROOF_FAILURE = {
  missing: "证据包缺失或不完整（缺 binding.json、evidence.db，或 settlement.json 不是普通文件），未显示任何证明。",
  corrupt: "证据包或日志无法读取或校验失败，未显示任何证明。",
  busy: "服务器读取队列已满，本次未刷新单笔成交证明，未显示旧结果。",
};
// Kinds of `unbound`: the bundle names another fill, or declares itself badly, or a
// referenced record is gone or changed. Shown apart, never as one "unavailable".
const MISBOUND = new Set([
  "order_not_matched", "block_mismatch", "settlement_binding_scope_mismatch",
  "settlement_origin_differs_from_cost",
]);
const MALFORMED = new Set([
  "binding_malformed", "settlement_binding_malformed", "provider_pair_invalid",
  "settlement_provider_pair_invalid", "origin_invalid",
]);
const GAP = {
  conflict: "证据之间矛盾", residual: "有未解释的残差",
  unverified: "证据不足以核对", journal_mismatch: "证据与日志成交不符",
};
const passed = view => own(PROOF_STATUS, view.status)?.[1] === "ok";

// The journal's fill itself cannot be bound: no receipt block hash, or no settlement scope.
const JOURNAL_SIDE = new Set(["receipt_block_unknown", "settlement_scope_invalid"]);

function unboundKind(code) {
  if (/^evidence_invalid_\d$/.test(code) || code === "settlement_evidence_invalid") {
    return "证据记录缺失、过期或被修改";
  }
  if (MISBOUND.has(code)) return "证据包与这笔成交不匹配（错绑）";
  if (MALFORMED.has(code)) return "证据包声明无效";
  return JOURNAL_SIDE.has(code) ? "日志成交缺少可绑定的收据字段" : "证据未绑定";
}

// The first failing check and its kind of gap; "reconciled" only when every check passed.
function proofSummary(views) {
  const index = views.findIndex(view => view !== null && !passed(view));
  if (index < 0) {
    return [views[2] === null ? "收据核对通过；未提供整块结算证明，未做区块对账"
      : "范围内已对账：仅这笔交易、USDC 与该代币、原生币整块净额", false];
  }
  const view = views[index], code = rootCode(typeof view.reason === "string" ? view.reason : "");
  const kind = view.status === "unbound" ? unboundKind(code) : own(GAP, view.status) || "证据缺口";
  return [`${kind}：${PROOF_CHECKS[index][1]} · ${proofReason(view.reason)}`, true];
}

const isProofView = view => Boolean(view) && typeof view === "object"
  && view.execution_eligible === false && typeof view.status === "string";

function renderProof(result, mode) {
  const status = document.getElementById("spot-proof-status");
  const refuse = message => {
    status.textContent = message;
    status.classList.toggle("danger", true);
    document.getElementById("spot-proof").replaceChildren();
  };
  const malformed = "单笔成交证明响应不符合只读约定，已拒绝显示。";
  if ("error" in result) {
    // Missing, unreadable and busy are different states: never one "unavailable".
    const failure = result.error || {};
    refuse(failure.status === 404 ? "未配置现货运行目录，未显示单笔成交证明。"
      : own(PROOF_FAILURE, failure.code) || "单笔成交证明读取失败，未显示任何证明。");
    return;
  }
  const proof = result.value;
  if (proof?.schema !== PROOF_SCHEMA || proof.execution_eligible !== false
      || typeof proof.configured !== "boolean") {
    refuse(malformed);
    return;
  }
  if (!proof.configured) {
    status.textContent = "未配置单笔成交证明包（databases.spot_proof）。";
    status.classList.toggle("danger", false);
    renderTable("spot-proof", PROOF_COLUMNS, [], "未配置证明包：用 capture_spot_cost 生成后在 databases.spot_proof 指定。");
    return;
  }
  const views = PROOF_CHECKS.map(([key]) => proof[key]);
  // "Not provided" means no settlement.json was read: a null view with a digest would claim
  // absence for a file the server did read, so the two must agree.
  const settlementRead = (proof.bundle || {}).settlement_sha256 != null;
  if (proof.mode !== mode || !isProofView(views[0]) || !isProofView(views[1])
      || !(views[2] === null ? !settlementRead : settlementRead && isProofView(views[2]))) {
    refuse(malformed);
    return;
  }
  let rows, summary, gap;
  try {
    rows = PROOF_CHECKS.map(([key, label], i) => {
      const view = views[i];
      if (view === null) {
        // Absence is stated, never explained: the file may never have been captured.
        return [esc(label), '<span class="warn">未提供</span>',
          esc("证据包中没有 settlement.json，未做区块对账（采集时加 --with-settlement 才会生成）"), "–", "–"];
      }
      const [text, tone] = own(PROOF_STATUS, view.status) || [view.status, "bad"];
      return [esc(label), `<span class="${tone}">${esc(text)}</span>`, esc(proofReason(view.reason)),
        esc(proofScope(key, view)), esc(proofValues(key, view))];
    });
    [summary, gap] = proofSummary(views);
  } catch {
    refuse(malformed);
    return;
  }
  const cost = views[0], block = cost.block || {}, journal = proof.journal || {};
  status.textContent = `${summary} · 订单 ${named(cost.intent_id)} · 交易 ${named(cost.tx_hash)}`
    + ` · 区块 ${units(block.number)} · 日志快照 ${units(journal.records)} 条（链头 ${abbrev(journal.head_sha256)}）`
    + ` · 绑定文件 ${abbrev((proof.bundle || {}).binding_sha256)}`
    + ` · ${labelled(PROOF_BASIS, cost.basis)} · ${labelled(INDEPENDENCE, cost.provider_independence)}`
    + " · 仅为证据：不入账、不参与策略 PnL 或执行门禁，不授予执行权限，不是账户净值或最终性证明";
  status.classList.toggle("danger", gap);
  renderTable("spot-proof", PROOF_COLUMNS, rows, "无证明");
}

// "<wei> (<observed>/<matched> 笔)": orders without an observed item stay visible, never zero.
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

// Each load supersedes the previous one: an older, slower response never renders.
let spotGeneration = 0;

export async function loadSpot() {
  const generation = ++spotGeneration;
  const status = document.getElementById("spot-status");
  const unavailable = message => {
    status.textContent = message;
    status.classList.toggle("danger", true);
    for (const id of ["spot-stats", "spot-positions", "spot-quality", "spot-decisions", "spot-flips", "spot-proof"]) {
      document.getElementById(id).replaceChildren();
    }
    const proofStatus = document.getElementById("spot-proof-status");
    proofStatus.textContent = "现货日志未配置或不可用，未显示单笔成交证明。";
    proofStatus.classList.toggle("danger", true);
  };
  // A separate, optional source: its failure clears only its own panel.
  const proofRead = apiGet("/api/v1/spot/fill-proof").then(value => ({ value }), error => ({ error }));
  let report, decisions, flips;
  try {
    [report, decisions, flips] = await Promise.all([
      apiGet("/api/v1/spot/report"),
      apiGet("/api/v1/spot/decisions?limit=50"),
      apiGet("/api/v1/spot/assessments?limit=30&action=flip"),
    ]);
  } catch (error) {
    if (generation !== spotGeneration) return;
    unavailable(error && error.status === 404
      ? "未配置现货运行目录（databases.spot_run）。"
      : "现货日志不可用或校验失败，未显示任何数据。");
    return;
  }
  const proof = await proofRead;
  if (generation !== spotGeneration) return;
  if (report?.execution_eligible !== false || !["shadow", "live"].includes(report.mode)
      || !report.operations || !report.paper_book || !report.signal_quality?.by_horizon_seconds
      || !Array.isArray(report.open_positions)
      || ![decisions, flips].every(result => result?.mode === report.mode
        && result.execution_eligible === false && Array.isArray(result.items))) {
    unavailable("现货响应不符合只读报告约定，已拒绝显示。");
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
  const banner = (live ? "历史实盘账簿（只读）" : "影子模式")
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
    // Scope: orders whose intent/fill/booking match inside this journal. Funds settlement
    // itself is still unverified (balance delta plus sender receipt), not proven here.
    ...(live ? [{ label: "收据执行 gas wei（日志内已匹配订单，仅执行分项，未入账，资金结算未验证）",
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
  renderTable("spot-decisions", ["时间", "动作", "代币", "金额 USD", "PnL USD", "原因"],
    decisions.items.map(d => [esc(when(d.at)), esc(KIND_LABEL[d.kind] || d.kind), esc(d.symbol || d.token || "–"),
      esc(usd(d.cost_usd_micros ?? d.proceeds_usd_micros)), esc(usd(d.pnl_usd_micros)),
      esc(d.reason || (d.reasons || []).join(" · ") || "–")]), "尚无交易决策");
  renderTable("spot-flips", ["时间", "代币", "评分", "1h", "5m", "聪明钱钱包", "流动性 USD"],
    flips.items.map(a => [esc(when(a.at)), esc(a.symbol), esc(a.score_bps), esc(bps(a.change_1h_bps)),
      esc(bps(a.change_5m_bps)), esc(a.smart_wallets), esc(usd(a.liquidity_usd_micros))]), "尚无买入信号");
  renderProof(proof, report.mode);
}
