import { $, esc, parseJsonExact, fmtTime, renderTable, FETCH_TIMEOUT_MS } from "./core.js";

export function batchRequestBody(raw) {
  if (raw.length > 10_000_000) throw new Error("input too large");
  const value = parseJsonExact(raw);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("object required");
  return raw; // Preserve original financial integer bytes.
}

export function batchView(value) {
  if (value?.schema_version !== "oasis.batch-score.v1" || value.execution_eligible !== false
      || !Array.isArray(value.items) || !Array.isArray(value.held_position_scores)) throw new Error("invalid batch");
  const candidateRows = value.items.map(item => {
    if (item.execution_eligible !== false || item.discovery.execution_eligible !== false
        || (item.score !== null && item.score.execution_eligible !== false)) throw new Error("unexpected authority");
    const discovery = item.discovery, score = item.score;
    return [discovery.chain_id, discovery.token, discovery.sources.join(" / "),
      discovery.attention_bps, score === null ? "未评分" : score.score_bps,
      item.status, score?.action ?? discovery.next_action,
      [...discovery.blockers, ...(score?.blockers ?? [])].join(" · ")].map(esc);
  });
  const exitRows = value.held_position_scores.map(score => {
    if (score.execution_eligible !== false) throw new Error("unexpected exit authority");
    return [score.chain_id, score.token, score.action, score.exit_reason ?? "无退出触发",
      score.blockers.join(" · ")].map(esc);
  });
  return {candidateRows, exitRows};
}

export function failureSummary(value) {
  if (!value || value.execution_eligible !== false || typeof value.error_type !== "string"
      || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(value.error_type)) throw new Error("invalid failure");
  const code = value.http_status;
  if (code !== null && (!Number.isInteger(code) || code < 100 || code > 599)) throw new Error("invalid HTTP status");
  return `采集失败 · ${value.error_type} · HTTP ${code ?? "未知"} · 此记录未建立可用行情`;
}

export function wireBatchResearch() {
  $("batch-score-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = $("batch-score-submit"), status = $("batch-score-status"), rawResult = $("batch-score-result");
    const clear = () => {
      rawResult.textContent = "";
      $("batch-score-table").replaceChildren();
      $("batch-exit-table").replaceChildren();
    };
    button.disabled = true;
    clear();
    status.textContent = "正在评估导入的候选与持仓…";
    try {
      const file = $("batch-score-input").files[0];
      if (!file || file.size > 10_000_000) throw new Error("invalid file");
      const response = await fetch("/api/v1/research/batch-score", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: batchRequestBody(await file.text()), signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!response.ok) throw new Error("batch failed");
      const value = parseJsonExact(await response.text()), view = batchView(value);
      renderTable("batch-score-table", ["链", "合约", "来源", "关注度", "研究分", "状态", "提案", "阻塞原因"],
        view.candidateRows, "此导入周期没有候选；不代表数据源可用或市场没有机会。");
      renderTable("batch-exit-table", ["链", "合约", "退出提案", "原因", "阻塞原因"],
        view.exitRows, "未提供持仓；不代表账户空仓。");
      status.textContent = `输入评估时间 ${fmtTime(value.evaluated_at)} · ${value.items.length} 个候选 · `
        + `排除过期/未来 ${value.omitted_stale_or_future}，其他链 ${value.omitted_chain}，容量延后 ${value.deferred_capacity} · 仅研究，未下单`;
      rawResult.textContent = JSON.stringify(value, null, 2);
    } catch {
      clear();
      status.textContent = "批量评估失败：检查输入、证据绑定和服务连接。旧结果已清除，交易资格未获验证。";
    } finally { button.disabled = false; }
  });
  $("feed-failure-input").addEventListener("change", async () => {
    const status = $("feed-failure-status");
    status.textContent = "正在读取采集记录…";
    try {
      const file = $("feed-failure-input").files[0];
      if (!file || file.size > 100_000) throw new Error("invalid file");
      status.textContent = failureSummary(parseJsonExact(await file.text()));
    } catch { status.textContent = "失败记录不可用；数据源状态未知。"; }
  });
}
